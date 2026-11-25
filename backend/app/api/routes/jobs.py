import uuid
import httpx
from fastapi import APIRouter, Body, Depends, HTTPException, Query, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload
from app.api.dependencies.auth import get_current_user
from app.connectors.base import BlockedSourceError, assert_permitted_url
from app.database.models import (
    CandidateProfile,
    Job,
    JobAnalysisHistory,
    JobMatch,
    JobSource,
    User,
)
from app.database.session import get_db
from app.graphs.discovery_graph import run_discovery_workflow
from app.graphs.intelligence_graph import run_job_intelligence_workflow
from app.schemas.jobs import (
    DiscoveryRunResponse,
    DiscoveryTriggerRequest,
    JobAnalysisHistoryResponse,
    JobDetailResponse,
    JobEligibilityResponse,
    JobEvidenceResponse,
    JobIntelligenceResponse,
    JobMatchResponse,
    JobRequirementResponse,
    JobSourceCreateRequest,
    JobSourceHealthResponse,
    JobStatusUpdateRequest,
    JobSummaryResponse,
)
from app.schemas.resume_intelligence import (
    JobResumeHistoryResponse,
    TailorEnqueueResponse,
    TailorJobRequest,
)
from app.services.audit_service import record_audit_event
from app.services.deduplication_service import mark_job_expired
from app.services.resume_intelligence_service import (
    enqueue_tailored_resume_for_job,
    get_job_resumes_history,
)
from app.services.scoring_service import upsert_job_match

router = APIRouter(prefix="/api/jobs", tags=["jobs"])


def _load_profile_with_evidence(db: Session, user: User) -> CandidateProfile | None:
    return db.scalar(
        select(CandidateProfile)
        .where(CandidateProfile.user_id == user.id)
        .options(
            selectinload(CandidateProfile.skills),
            selectinload(CandidateProfile.projects),
            selectinload(CandidateProfile.experiences),
            selectinload(CandidateProfile.certifications),
        )
    )


def _to_job_summary(job: Job, match: JobMatch | None) -> JobSummaryResponse:
    return JobSummaryResponse(
        id=job.id,
        source_id=job.source_id,
        source_job_id=job.source_job_id,
        canonical_url=job.canonical_url,
        company=job.company,
        role_title=job.role_title,
        location=job.location,
        remote_policy=job.remote_policy,
        is_remote=job.is_remote,
        employment_type=job.employment_type,
        salary_min=job.salary_min,
        salary_max=job.salary_max,
        salary_currency=job.salary_currency,
        salary_text=job.salary_text,
        application_url=job.application_url,
        posted_at=job.posted_at,
        first_seen_at=job.first_seen_at,
        last_seen_at=job.last_seen_at,
        repost_count=job.repost_count,
        status=job.status,
        requirements=JobRequirementResponse.model_validate(job.requirements) if job.requirements else None,
        match=JobMatchResponse.model_validate(match) if match else None,
    )


@router.post("/sources", response_model=JobSourceHealthResponse, status_code=status.HTTP_201_CREATED)
def register_job_source(
    payload: JobSourceCreateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> JobSource:
    url_to_check = payload.base_url or payload.config.get("careers_url")
    if url_to_check:
        try:
            assert_permitted_url(str(url_to_check))
        except BlockedSourceError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    existing = db.scalar(select(JobSource).where(JobSource.source_name == payload.source_name))
    if existing is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Job source already exists")

    source = JobSource(**payload.model_dump())
    db.add(source)
    db.flush()
    record_audit_event(
        db,
        user_id=current_user.id,
        actor="user",
        action="job_source_registered",
        resource_type="job_source",
        resource_id=str(source.id),
        metadata={"connector_type": source.connector_type, "source_name": source.source_name},
    )
    db.commit()
    db.refresh(source)
    return source


@router.get("/sources/health", response_model=list[JobSourceHealthResponse])
def get_sources_health(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[JobSource]:
    return list(db.scalars(select(JobSource).order_by(JobSource.source_name.asc())).all())


def get_discovery_http_client() -> httpx.AsyncClient | None:
    """Dependency hook allowing test injection of MockTransport HTTP clients."""
    return None


@router.post("/discover", response_model=DiscoveryRunResponse)
async def trigger_discovery(
    payload: DiscoveryTriggerRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    http_client: httpx.AsyncClient | None = Depends(get_discovery_http_client),
) -> DiscoveryRunResponse:
    report = await run_discovery_workflow(
        db,
        user_id=current_user.id,
        query=payload.query,
        location=payload.location,
        source_ids=payload.source_ids,
        max_pages=payload.max_pages,
        http_client=http_client,
    )
    return DiscoveryRunResponse(**report)


@router.get("", response_model=list[JobSummaryResponse])
def list_jobs(
    status_filter: str | None = Query(default=None, alias="status"),
    remote_policy: str | None = Query(default=None),
    employment_type: str | None = Query(default=None),
    role_category: str | None = Query(default=None),
    eligible_only: bool = Query(default=False),
    min_score: float | None = Query(default=None, ge=0, le=100),
    search: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[JobSummaryResponse]:
    stmt = (
        select(Job)
        .options(selectinload(Job.requirements))
        .order_by(Job.last_seen_at.desc())
    )
    if status_filter:
        stmt = stmt.where(Job.status == status_filter)
    if remote_policy:
        stmt = stmt.where(Job.remote_policy == remote_policy)
    if employment_type:
        stmt = stmt.where(Job.employment_type == employment_type)
    if search:
        term = f"%{search.strip()}%"
        stmt = stmt.where(Job.company.ilike(term) | Job.role_title.ilike(term))

    jobs = list(db.scalars(stmt).all())
    profile = _load_profile_with_evidence(db, current_user)

    matches_by_job: dict[uuid.UUID, JobMatch] = {}
    if profile is not None and jobs:
        job_ids = [j.id for j in jobs]
        matches = db.scalars(
            select(JobMatch).where(JobMatch.profile_id == profile.id, JobMatch.job_id.in_(job_ids))
        ).all()
        matches_by_job = {m.job_id: m for m in matches}

    filtered: list[JobSummaryResponse] = []
    for job in jobs:
        if role_category and (job.requirements is None or job.requirements.role_category != role_category):
            continue
        match = matches_by_job.get(job.id)
        if eligible_only and (match is None or not match.eligible):
            continue
        if min_score is not None and (match is None or match.score < min_score):
            continue
        filtered.append(_to_job_summary(job, match))

    if any(m is not None for m in matches_by_job.values()):
        filtered.sort(key=lambda j: j.match.score if j.match else -1.0, reverse=True)

    return filtered[offset : offset + limit]


@router.get("/{job_id}", response_model=JobDetailResponse)
def get_job_detail(
    job_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> JobDetailResponse:
    job = db.scalar(select(Job).where(Job.id == job_id).options(selectinload(Job.requirements)))
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")

    profile = _load_profile_with_evidence(db, current_user)
    match = (
        db.scalar(select(JobMatch).where(JobMatch.job_id == job.id, JobMatch.profile_id == profile.id))
        if profile
        else None
    )
    summary = _to_job_summary(job, match)
    return JobDetailResponse(
        **summary.model_dump(),
        raw_jd=job.raw_jd,
        updated_at_source=job.updated_at_source,
        expired_at=job.expired_at,
        source_provenance=job.source_provenance,
    )


@router.post("/{job_id}/analyze", response_model=JobMatchResponse)
async def analyze_and_score_job(
    job_id: uuid.UUID,
    use_llm: bool = Query(default=True),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> JobMatch:
    job = db.scalar(select(Job).where(Job.id == job_id).options(selectinload(Job.requirements)))
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")

    profile = _load_profile_with_evidence(db, current_user)
    if profile is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Candidate profile not found; create one via POST /api/profile first",
        )

    await run_job_intelligence_workflow(
        db,
        user_id=current_user.id,
        job_id=job.id,
        use_llm=use_llm,
        allow_reanalysis=True,
    )
    match = db.scalar(select(JobMatch).where(JobMatch.job_id == job.id, JobMatch.profile_id == profile.id))
    assert match is not None
    return match


@router.get("/{job_id}/intelligence", response_model=JobIntelligenceResponse)
async def get_job_intelligence(
    job_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> JobIntelligenceResponse:
    job = db.scalar(select(Job).where(Job.id == job_id).options(selectinload(Job.requirements)))
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")

    profile = _load_profile_with_evidence(db, current_user)
    if profile is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Candidate profile not found")

    match = db.scalar(select(JobMatch).where(JobMatch.job_id == job.id, JobMatch.profile_id == profile.id))
    if match is None:
        await run_job_intelligence_workflow(db, user_id=current_user.id, job_id=job.id, use_llm=True)
        db.refresh(job)
        match = db.scalar(select(JobMatch).where(JobMatch.job_id == job.id, JobMatch.profile_id == profile.id))
    assert match is not None

    return JobIntelligenceResponse(
        job_id=job.id,
        company=job.company,
        role_title=job.role_title,
        status=job.status,
        requirements=JobRequirementResponse.model_validate(job.requirements) if job.requirements else None,
        match=JobMatchResponse.model_validate(match),
        skill_coverage={
            "required_skill_coverage": match.required_skill_coverage,
            "preferred_skill_coverage": match.preferred_skill_coverage,
            "matched_skills": match.matched_skills,
            "missing_required_skills": match.missing_required_skills,
            "preferred_gaps": match.preferred_gaps,
        },
    )


@router.get("/{job_id}/evidence", response_model=JobEvidenceResponse)
def get_job_evidence(
    job_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> JobEvidenceResponse:
    job = db.scalar(select(Job).where(Job.id == job_id).options(selectinload(Job.requirements)))
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")

    profile = _load_profile_with_evidence(db, current_user)
    if profile is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Candidate profile not found")

    match = db.scalar(select(JobMatch).where(JobMatch.job_id == job.id, JobMatch.profile_id == profile.id))
    if match is None:
        match = upsert_job_match(db, job=job, req=job.requirements, profile=profile)
        db.commit()
        db.refresh(match)

    return JobEvidenceResponse(
        job_id=job.id,
        profile_id=profile.id,
        evidence_mapping=match.evidence_mapping,
        missing_evidence=match.missing_evidence,
        confirmed_missing_skills=match.missing_required_skills,
        jd_evidence_items=job.requirements.evidence_items if job.requirements else {},
    )


@router.get("/{job_id}/eligibility", response_model=JobEligibilityResponse)
def get_job_eligibility(
    job_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> JobEligibilityResponse:
    job = db.scalar(select(Job).where(Job.id == job_id).options(selectinload(Job.requirements)))
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")

    profile = _load_profile_with_evidence(db, current_user)
    if profile is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Candidate profile not found")

    match = db.scalar(select(JobMatch).where(JobMatch.job_id == job.id, JobMatch.profile_id == profile.id))
    if match is None:
        match = upsert_job_match(db, job=job, req=job.requirements, profile=profile)
        db.commit()
        db.refresh(match)

    return JobEligibilityResponse(
        job_id=job.id,
        eligible=match.eligible,
        hard_constraints_passed=match.hard_constraints_passed,
        eligibility_blockers=match.eligibility_blockers,
        uncertainties=match.uncertainties,
        confidence=match.confidence,
        recommendation=match.recommendation,
        explanation=match.explanation,
    )


@router.get("/{job_id}/history", response_model=list[JobAnalysisHistoryResponse])
def get_job_analysis_history(
    job_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[JobAnalysisHistory]:
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    profile = _load_profile_with_evidence(db, current_user)
    if profile is None:
        return []
    stmt = (
        select(JobAnalysisHistory)
        .where(JobAnalysisHistory.job_id == job_id, JobAnalysisHistory.profile_id == profile.id)
        .order_by(JobAnalysisHistory.created_at.desc())
    )
    return list(db.scalars(stmt).all())


@router.get("/{job_id}/match", response_model=JobMatchResponse)
def get_job_match(
    job_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> JobMatch:
    job = db.scalar(select(Job).where(Job.id == job_id).options(selectinload(Job.requirements)))
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")

    profile = _load_profile_with_evidence(db, current_user)
    if profile is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Candidate profile not found",
        )

    match = db.scalar(select(JobMatch).where(JobMatch.job_id == job.id, JobMatch.profile_id == profile.id))
    if match is None:
        match = upsert_job_match(db, job=job, req=job.requirements, profile=profile)
        db.commit()
        db.refresh(match)
    return match


@router.patch("/{job_id}/status", response_model=JobSummaryResponse)
def update_job_status(
    job_id: uuid.UUID,
    payload: JobStatusUpdateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> JobSummaryResponse:
    job = db.scalar(select(Job).where(Job.id == job_id).options(selectinload(Job.requirements)))
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")

    if payload.status == "expired":
        mark_job_expired(db, job, reason=payload.reason or "manual_status_update")
    else:
        job.status = payload.status
        if payload.status == "active":
            job.expired_at = None

    record_audit_event(
        db,
        user_id=current_user.id,
        actor="user",
        action="job_status_updated",
        resource_type="job",
        resource_id=str(job.id),
        metadata={"status": job.status, "reason": payload.reason},
    )
    db.commit()
    db.refresh(job)
    return _to_job_summary(job, None)


@router.post("/{job_id}/tailor", response_model=TailorEnqueueResponse, status_code=status.HTTP_202_ACCEPTED)
def request_tailored_resume_for_job(
    job_id: uuid.UUID,
    response: Response,
    payload: TailorJobRequest | None = Body(default=None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> TailorEnqueueResponse:
    """Queue a durable manual or forced resume tailoring request for an eligible job."""
    req_body = payload or TailorJobRequest()
    result = enqueue_tailored_resume_for_job(
        db,
        user=current_user,
        job_id=job_id,
        force_regenerate=req_body.force_regenerate,
    )
    if not result.created:
        response.status_code = status.HTTP_200_OK
    return result


@router.get("/{job_id}/resumes/history", response_model=JobResumeHistoryResponse)
def get_job_tailored_resume_history(
    job_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> JobResumeHistoryResponse:
    """List profile-scoped tailored resume versions and outbox generation jobs for a job."""
    return get_job_resumes_history(db, user=current_user, job_id=job_id)
