from contextlib import nullcontext
from pathlib import Path
import threading
import uuid
from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.database.models import (
    CandidateProfile,
    Job,
    JobMatch,
    Resume,
    ResumeGenerationJob,
    ResumeJobMapping,
    User,
    utc_now,
)
from app.schemas.resume_intelligence import (
    ApprovalDecisionResponse,
    FactualValidationReportResponse,
    JobResumeHistoryResponse,
    OutboxJobStatusResponse,
    ResumeGenerationHistoryResponse,
    TailoredResumeDetailResponse,
    TailoredResumeSummaryResponse,
    TailorEnqueueResponse,
    TailoringDiffResponse,
)
from app.services.audit_service import record_audit_event
from app.services.outbox_service import enqueue_and_commit_resume_generation
from app.services.resume_vault_service import compute_sha256, resolve_owned_resume_path
from app.services.scoring_service import upsert_job_match

_SQLITE_APPROVAL_LOCK = threading.Lock()


def _decision_lock(db: Session):
    dialect = db.bind.dialect.name if db.bind is not None else "sqlite"
    return _SQLITE_APPROVAL_LOCK if dialect == "sqlite" else nullcontext()


def get_owned_profile(db: Session, user: User) -> CandidateProfile:
    profile = db.scalar(select(CandidateProfile).where(CandidateProfile.user_id == user.id))
    if profile is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Candidate profile not found; create one via POST /api/profile first",
        )
    return profile


def get_owned_resume_and_mapping(
    db: Session,
    *,
    user: User,
    resume_id: uuid.UUID,
    require_tailored: bool = True,
    lock_mapping: bool = False,
) -> tuple[Resume, CandidateProfile, ResumeJobMapping | None]:
    """Fetch owner-scoped Resume and its canonical ResumeJobMapping (404 on cross-user/profile access)."""
    row = db.execute(
        select(Resume, CandidateProfile)
        .join(CandidateProfile, Resume.profile_id == CandidateProfile.id)
        .where(Resume.id == resume_id, CandidateProfile.user_id == user.id)
    ).first()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Resume not found")
    resume, profile = row
    if require_tailored and resume.is_master:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Operation applies only to tailored resumes, not master resumes",
        )

    map_stmt = (
        select(ResumeJobMapping)
        .where(
            ResumeJobMapping.resume_id == resume.id,
            ResumeJobMapping.profile_id == profile.id,
        )
        .order_by(ResumeJobMapping.created_at.desc())
    )
    if lock_mapping and db.bind is not None and db.bind.dialect.name == "postgresql":
        map_stmt = map_stmt.with_for_update()

    mapping = db.scalars(map_stmt).first()
    return resume, profile, mapping


def enqueue_tailored_resume_for_job(
    db: Session,
    *,
    user: User,
    job_id: uuid.UUID,
    force_regenerate: bool = False,
) -> TailorEnqueueResponse:
    """Validate ownership/eligibility and persist a manual or regenerate outbox request."""
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")

    profile = get_owned_profile(db, user)
    match = db.scalar(
        select(JobMatch).where(JobMatch.job_id == job.id, JobMatch.profile_id == profile.id)
    )
    if match is None:
        match = upsert_job_match(db, job=job, req=job.requirements, profile=profile)
        db.commit()
        db.refresh(match)

    if not match.eligible:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Job is not eligible for resume tailoring under hard constraints",
        )

    trigger_mode = "regenerate" if force_regenerate else "manual"
    url_str = str(db.bind.url) if db.bind is not None and db.bind.url else ""
    should_dispatch = not url_str.endswith(":memory:")

    outcome, broker_ok = enqueue_and_commit_resume_generation(
        db,
        user_id=user.id,
        profile_id=profile.id,
        job_id=job.id,
        trigger_mode=trigger_mode,
        force_regenerate=force_regenerate,
        dispatch=should_dispatch,
    )
    outbox_row = (
        db.get(ResumeGenerationJob, outcome.generation_job_id)
        if outcome.generation_job_id is not None
        else None
    )

    if outcome.created:
        record_audit_event(
            db,
            user_id=user.id,
            actor="user",
            action="tailored_resume_requested",
            resource_type="resume_generation_job",
            resource_id=str(outcome.generation_job_id),
            metadata={
                "job_id": str(job.id),
                "trigger_mode": trigger_mode,
                "force_regenerate": force_regenerate,
                "dispatched_to_broker": broker_ok,
            },
        )
        db.commit()

    return TailorEnqueueResponse(
        accepted=outcome.accepted,
        created=outcome.created,
        idempotency_hit=not outcome.created,
        status=outcome.status,
        reason=outcome.reason,
        job_id=job.id,
        generation_job_id=outcome.generation_job_id,
        existing_resume_id=outcome.existing_resume_id,
        evidence_fingerprint=outcome.evidence_fingerprint,
        dispatched_to_broker=broker_ok,
        outbox_job=OutboxJobStatusResponse.model_validate(outbox_row) if outbox_row else None,
    )


def regenerate_tailored_resume(
    db: Session,
    *,
    user: User,
    resume_id: uuid.UUID,
) -> TailorEnqueueResponse:
    """Create an intentional new generation request for an existing tailored resume."""
    resume, profile, mapping = get_owned_resume_and_mapping(
        db, user=user, resume_id=resume_id, require_tailored=True
    )
    if mapping is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Job mapping for this tailored resume was not found",
        )
    job = db.get(Job, mapping.job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Associated job not found")

    url_str = str(db.bind.url) if db.bind is not None and db.bind.url else ""
    should_dispatch = not url_str.endswith(":memory:")
    outcome, broker_ok = enqueue_and_commit_resume_generation(
        db,
        user_id=user.id,
        profile_id=profile.id,
        job_id=job.id,
        trigger_mode="regenerate",
        force_regenerate=True,
        master_resume_id=resume.master_resume_id,
        dispatch=should_dispatch,
    )
    outbox_row = (
        db.get(ResumeGenerationJob, outcome.generation_job_id)
        if outcome.generation_job_id is not None
        else None
    )
    if outcome.created:
        record_audit_event(
            db,
            user_id=user.id,
            actor="user",
            action="tailored_resume_regeneration_requested",
            resource_type="resume_generation_job",
            resource_id=str(outcome.generation_job_id),
            metadata={
                "source_resume_id": str(resume.id),
                "job_id": str(job.id),
                "dispatched_to_broker": broker_ok,
            },
        )
        db.commit()

    return TailorEnqueueResponse(
        accepted=outcome.accepted,
        created=outcome.created,
        idempotency_hit=False,
        status=outcome.status,
        reason=outcome.reason,
        job_id=job.id,
        generation_job_id=outcome.generation_job_id,
        evidence_fingerprint=outcome.evidence_fingerprint,
        dispatched_to_broker=broker_ok,
        outbox_job=OutboxJobStatusResponse.model_validate(outbox_row) if outbox_row else None,
    )


def list_tailored_resumes(
    db: Session,
    *,
    user: User,
    job_id: uuid.UUID | None = None,
    approval_status: str | None = None,
    active_only: bool = False,
) -> list[TailoredResumeSummaryResponse]:
    profile = db.scalar(select(CandidateProfile).where(CandidateProfile.user_id == user.id))
    if profile is None:
        return []

    stmt = (
        select(ResumeJobMapping, Resume)
        .join(Resume, ResumeJobMapping.resume_id == Resume.id)
        .where(
            ResumeJobMapping.profile_id == profile.id,
            Resume.is_master.is_(False),
        )
        .order_by(ResumeJobMapping.created_at.desc())
    )
    if job_id is not None:
        stmt = stmt.where(ResumeJobMapping.job_id == job_id)
    if approval_status is not None:
        stmt = stmt.where(ResumeJobMapping.approval_status == approval_status)
    if active_only:
        stmt = stmt.where(ResumeJobMapping.is_active.is_(True))

    rows = db.execute(stmt).all()
    return [
        TailoredResumeSummaryResponse(
            resume_id=res.id,
            mapping_id=mp.id,
            job_id=mp.job_id,
            master_resume_id=mp.master_resume_id or res.master_resume_id,
            version_number=mp.version_number,
            version_label=res.version_label,
            is_active=mp.is_active,
            status=mp.status,
            approval_status=mp.approval_status,
            template_version=res.template_version,
            evidence_fingerprint=mp.evidence_fingerprint,
            validation_passed=res.validation_passed,
            evaluation_report=dict(res.evaluation_report or {}),
            created_at=mp.created_at,
        )
        for mp, res in rows
    ]


def get_tailored_resume_detail(
    db: Session,
    *,
    user: User,
    resume_id: uuid.UUID,
) -> TailoredResumeDetailResponse:
    resume, _profile, mapping = get_owned_resume_and_mapping(
        db, user=user, resume_id=resume_id, require_tailored=True
    )
    if mapping is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Tailored resume mapping not found",
        )
    return TailoredResumeDetailResponse(
        resume_id=resume.id,
        mapping_id=mapping.id,
        job_id=mapping.job_id,
        master_resume_id=mapping.master_resume_id or resume.master_resume_id,
        version_number=mapping.version_number,
        version_label=resume.version_label,
        is_active=mapping.is_active,
        status=mapping.status,
        approval_status=mapping.approval_status,
        template_version=resume.template_version,
        evidence_fingerprint=mapping.evidence_fingerprint,
        validation_passed=resume.validation_passed,
        evaluation_report=dict(resume.evaluation_report or {}),
        created_at=mapping.created_at,
        run_id=mapping.run_id,
        trigger_mode=mapping.trigger_mode,
        sha256_checksum=resume.sha256_checksum,
        file_size_bytes=resume.file_size_bytes,
        ats_report=dict(resume.ats_report or {}),
        parsed_metadata=dict(resume.parsed_metadata or {}),
        validation_report=dict(resume.validation_report or {}),
        tailoring_diff=dict(mapping.tailoring_diff or {}),
        token_usage=dict(mapping.token_usage or {}),
        model_provider=mapping.model_provider,
        approval_notes=mapping.approval_notes,
        approved_at=mapping.approved_at,
        rejected_at=mapping.rejected_at,
    )


def get_tailored_resume_diff(
    db: Session,
    *,
    user: User,
    resume_id: uuid.UUID,
) -> TailoringDiffResponse:
    resume, _profile, mapping = get_owned_resume_and_mapping(
        db, user=user, resume_id=resume_id, require_tailored=True
    )
    if mapping is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tailoring diff not found")
    return TailoringDiffResponse(
        resume_id=resume.id,
        mapping_id=mapping.id,
        job_id=mapping.job_id,
        version_number=mapping.version_number,
        evidence_fingerprint=mapping.evidence_fingerprint,
        tailoring_diff=dict(mapping.tailoring_diff or {}),
    )


def get_resume_validation_report(
    db: Session,
    *,
    user: User,
    resume_id: uuid.UUID,
) -> FactualValidationReportResponse:
    resume, _profile, mapping = get_owned_resume_and_mapping(
        db, user=user, resume_id=resume_id, require_tailored=False
    )
    return FactualValidationReportResponse(
        resume_id=resume.id,
        mapping_id=mapping.id if mapping else None,
        job_id=mapping.job_id if mapping else None,
        validation_passed=resume.validation_passed,
        validation_report=dict(resume.validation_report or {}),
        ats_report=dict(resume.ats_report or {}),
    )


def get_job_resumes_history(
    db: Session,
    *,
    user: User,
    job_id: uuid.UUID,
) -> JobResumeHistoryResponse:
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")

    profile = db.scalar(select(CandidateProfile).where(CandidateProfile.user_id == user.id))
    if profile is None:
        return JobResumeHistoryResponse(job_id=job_id, versions=[], outbox_jobs=[])

    mappings = list(
        db.scalars(
            select(ResumeJobMapping)
            .where(
                ResumeJobMapping.job_id == job_id,
                ResumeJobMapping.profile_id == profile.id,
            )
            .order_by(ResumeJobMapping.version_number.desc())
        ).all()
    )
    outbox = list(
        db.scalars(
            select(ResumeGenerationJob)
            .where(
                ResumeGenerationJob.job_id == job_id,
                ResumeGenerationJob.profile_id == profile.id,
                ResumeGenerationJob.user_id == user.id,
            )
            .order_by(ResumeGenerationJob.created_at.desc())
        ).all()
    )
    return JobResumeHistoryResponse(
        job_id=job_id,
        versions=[
            ResumeGenerationHistoryResponse(
                mapping_id=m.id,
                resume_id=m.resume_id,
                job_id=m.job_id,
                master_resume_id=m.master_resume_id,
                generation_job_id=m.generation_job_id,
                run_id=m.run_id,
                trigger_mode=m.trigger_mode,
                version_number=m.version_number,
                is_active=m.is_active,
                evidence_fingerprint=m.evidence_fingerprint,
                status=m.status,
                approval_status=m.approval_status,
                approval_notes=m.approval_notes,
                approved_at=m.approved_at,
                rejected_at=m.rejected_at,
                model_provider=m.model_provider,
                token_usage=dict(m.token_usage or {}),
                error_message=m.error_message,
                created_at=m.created_at,
            )
            for m in mappings
        ],
        outbox_jobs=[OutboxJobStatusResponse.model_validate(o) for o in outbox],
    )


def resolve_verified_tailored_download(
    db: Session,
    *,
    user: User,
    resume_id: uuid.UUID,
    approved_only: bool = False,
) -> tuple[Resume, Path]:
    """Verify ownership, completion, validation, optional approval, path, and SHA-256 checksum."""
    settings = get_settings()
    resume, _profile, mapping = get_owned_resume_and_mapping(
        db, user=user, resume_id=resume_id, require_tailored=True
    )
    if mapping is None or mapping.status != "completed":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Failed or incomplete tailored resume is not downloadable",
        )
    if resume.generation_status != "completed" or not resume.validation_passed:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Tailored resume did not pass factual validation and cannot be downloaded",
        )
    if approved_only and mapping.approval_status != "approved":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Tailored resume has not been approved by the owner",
        )

    resolved = resolve_owned_resume_path(Path(settings.storage_dir), resume.pdf_path)
    actual_sha256 = compute_sha256(resolved.read_bytes())
    if actual_sha256 != resume.sha256_checksum:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Resume file integrity checksum verification failed",
        )
    return resume, resolved


def require_approved_tailored_resume_for_submission(
    db: Session,
    *,
    user: User,
    job_id: uuid.UUID,
    resume_id: uuid.UUID,
) -> tuple[Resume, ResumeJobMapping]:
    """Server-side authoritative check for downstream services (never trusts client flags)."""
    resume, _profile, mapping = get_owned_resume_and_mapping(
        db, user=user, resume_id=resume_id, require_tailored=True
    )
    if (
        mapping is None
        or mapping.job_id != job_id
        or mapping.status != "completed"
        or not resume.validation_passed
        or resume.generation_status != "completed"
        or mapping.approval_status != "approved"
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Explicit user approval of a completed, validated tailored resume is required",
        )
    return resume, mapping


def approve_tailored_resume(
    db: Session,
    *,
    user: User,
    resume_id: uuid.UUID,
    notes: str | None = None,
) -> ApprovalDecisionResponse:
    """Owner-only, locked, transactional approval backed solely by ResumeJobMapping."""
    with _decision_lock(db):
        return _approve_tailored_resume_impl(db, user=user, resume_id=resume_id, notes=notes)


def _approve_tailored_resume_impl(
    db: Session,
    *,
    user: User,
    resume_id: uuid.UUID,
    notes: str | None = None,
) -> ApprovalDecisionResponse:
    resume, _profile, mapping = get_owned_resume_and_mapping(
        db, user=user, resume_id=resume_id, require_tailored=True, lock_mapping=True
    )
    if mapping is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tailored resume mapping not found")

    if resume.generation_status != "completed" or mapping.status != "completed":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cannot approve an incomplete or failed tailored resume",
        )
    if not resume.validation_passed:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cannot approve a tailored resume that failed factual validation",
        )

    if mapping.approval_status == "rejected":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cannot approve a previously rejected tailored resume; regenerate a new version",
        )

    if mapping.approval_status == "approved":
        db.commit()
        return ApprovalDecisionResponse(
            mapping_id=mapping.id,
            resume_id=resume.id,
            job_id=mapping.job_id,
            version_number=mapping.version_number,
            status=mapping.status,
            approval_status=mapping.approval_status,
            approval_notes=mapping.approval_notes,
            approved_at=mapping.approved_at,
            rejected_at=None,
            idempotent=True,
        )

    now = utc_now()
    mapping.approval_status = "approved"
    mapping.approved_at = now
    mapping.rejected_at = None
    if notes is not None:
        mapping.approval_notes = notes.strip()

    record_audit_event(
        db,
        user_id=user.id,
        actor="user",
        action="tailored_resume_approved",
        resource_type="resume_job_mapping",
        resource_id=str(mapping.id),
        metadata={
            "resume_id": str(resume.id),
            "job_id": str(mapping.job_id),
            "version_number": mapping.version_number,
            "approval_status": "approved",
        },
    )
    db.commit()
    db.refresh(mapping)

    return ApprovalDecisionResponse(
        mapping_id=mapping.id,
        resume_id=resume.id,
        job_id=mapping.job_id,
        version_number=mapping.version_number,
        status=mapping.status,
        approval_status=mapping.approval_status,
        approval_notes=mapping.approval_notes,
        approved_at=mapping.approved_at,
        rejected_at=mapping.rejected_at,
        idempotent=False,
    )


def reject_tailored_resume(
    db: Session,
    *,
    user: User,
    resume_id: uuid.UUID,
    notes: str | None = None,
) -> ApprovalDecisionResponse:
    """Owner-only, locked, transactional rejection updating canonical ResumeJobMapping."""
    with _decision_lock(db):
        return _reject_tailored_resume_impl(db, user=user, resume_id=resume_id, notes=notes)


def _reject_tailored_resume_impl(
    db: Session,
    *,
    user: User,
    resume_id: uuid.UUID,
    notes: str | None = None,
) -> ApprovalDecisionResponse:
    resume, _profile, mapping = get_owned_resume_and_mapping(
        db, user=user, resume_id=resume_id, require_tailored=True, lock_mapping=True
    )
    if mapping is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tailored resume mapping not found")

    if mapping.approval_status == "approved":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cannot reject an already approved tailored resume",
        )

    if mapping.approval_status == "rejected":
        if notes is not None and notes.strip() != (mapping.approval_notes or ""):
            mapping.approval_notes = notes.strip()
        db.commit()
        db.refresh(mapping)
        return ApprovalDecisionResponse(
            mapping_id=mapping.id,
            resume_id=resume.id,
            job_id=mapping.job_id,
            version_number=mapping.version_number,
            status=mapping.status,
            approval_status=mapping.approval_status,
            approval_notes=mapping.approval_notes,
            approved_at=None,
            rejected_at=mapping.rejected_at,
            idempotent=True,
        )

    now = utc_now()
    mapping.approval_status = "rejected"
    mapping.rejected_at = now
    mapping.approved_at = None
    if notes is not None:
        mapping.approval_notes = notes.strip()

    record_audit_event(
        db,
        user_id=user.id,
        actor="user",
        action="tailored_resume_rejected",
        resource_type="resume_job_mapping",
        resource_id=str(mapping.id),
        metadata={
            "resume_id": str(resume.id),
            "job_id": str(mapping.job_id),
            "version_number": mapping.version_number,
            "approval_status": "rejected",
            "has_notes": bool(notes and notes.strip()),
        },
    )
    db.commit()
    db.refresh(mapping)

    return ApprovalDecisionResponse(
        mapping_id=mapping.id,
        resume_id=resume.id,
        job_id=mapping.job_id,
        version_number=mapping.version_number,
        status=mapping.status,
        approval_status=mapping.approval_status,
        approval_notes=mapping.approval_notes,
        approved_at=mapping.approved_at,
        rejected_at=mapping.rejected_at,
        idempotent=False,
    )
