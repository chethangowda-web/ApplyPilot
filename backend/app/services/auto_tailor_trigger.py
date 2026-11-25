from datetime import date
from typing import Any, Protocol
import uuid
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.agents.resume_intelligence.resume_analyzer import ResumeAnalyzerAgent
from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.database.models import (
    AutoTailorDailyQuota,
    CandidateProfile,
    Job,
    JobRequirement,
    ResumeGenerationJob,
    ResumeJobMapping,
)
from app.services.fingerprint_service import (
    compute_evidence_fingerprint,
    compute_generation_idempotency_key,
)
from app.services.quota_service import resolve_kolkata_quota_date

logger = get_logger("applypilot.auto_tailor_trigger")


class AutoTailorDispatchResult(BaseModel):
    job_id: uuid.UUID
    dispatched: bool
    status: str  # queued | skipped | deferred_no_dispatcher | failed
    reason: str | None = None
    generation_job_id: uuid.UUID | None = None
    quota_date: date | None = None


class AutoTailorDispatcher(Protocol):
    """Integration boundary implemented in Stage 5 by the transactional outbox + Celery dispatcher."""

    async def dispatch_auto_tailoring(
        self,
        db: Session,
        *,
        user_id: uuid.UUID,
        profile_id: uuid.UUID,
        job_id: uuid.UUID,
        match_score: float,
        evidence_fingerprint: str,
        idempotency_key: str,
        quota_date: date,
        master_resume_id: uuid.UUID | None,
    ) -> AutoTailorDispatchResult: ...


class AutoTailorEligibilityDecision(BaseModel):
    eligible_for_auto_tailor: bool
    reason: str
    quota_date: date
    evidence_fingerprint: str | None = None
    idempotency_key: str | None = None
    master_resume_id: uuid.UUID | None = None
    remaining_quota: int = 0


def resolve_user_quota_date(_db: Session, _user_id: uuid.UUID, _settings: Settings | None = None) -> date:
    """Return canonical daily quota date in Asia/Kolkata."""
    return resolve_kolkata_quota_date()


def evaluate_auto_tailor_eligibility(
    db: Session,
    *,
    user_id: uuid.UUID,
    profile_id: uuid.UUID,
    job_id: uuid.UUID,
    match_score: float,
    job_eligible: bool,
    settings: Settings | None = None,
    batch_reserved: int = 0,
) -> AutoTailorEligibilityDecision:
    """Evaluate all auto-tailoring safeguards without touching MIN_JOB_MATCH_SCORE."""
    cfg = settings or get_settings()
    q_date = resolve_user_quota_date(db, user_id, cfg)

    if not cfg.auto_tailor_enabled:
        return AutoTailorEligibilityDecision(
            eligible_for_auto_tailor=False,
            reason="auto_tailor_disabled",
            quota_date=q_date,
        )

    if not job_eligible:
        return AutoTailorEligibilityDecision(
            eligible_for_auto_tailor=False,
            reason="job_ineligible",
            quota_date=q_date,
        )

    # Strictly use AUTO_TAILOR_MIN_SCORE (never MIN_JOB_MATCH_SCORE)
    if float(match_score) < float(cfg.auto_tailor_min_score):
        return AutoTailorEligibilityDecision(
            eligible_for_auto_tailor=False,
            reason="below_auto_tailor_min_score",
            quota_date=q_date,
        )

    quota_row = db.scalar(
        select(AutoTailorDailyQuota).where(
            AutoTailorDailyQuota.user_id == user_id,
            AutoTailorDailyQuota.quota_date == q_date,
        )
    )
    used_today = int(quota_row.reserved_count if quota_row else 0) + max(0, batch_reserved)
    remaining = max(0, int(cfg.auto_tailor_daily_limit) - used_today)
    if cfg.auto_tailor_daily_limit <= 0 or remaining <= 0:
        return AutoTailorEligibilityDecision(
            eligible_for_auto_tailor=False,
            reason="daily_quota_exhausted",
            quota_date=q_date,
            remaining_quota=0,
        )

    profile = db.scalar(
        select(CandidateProfile)
        .where(CandidateProfile.id == profile_id)
        .options(
            selectinload(CandidateProfile.skills),
            selectinload(CandidateProfile.projects),
            selectinload(CandidateProfile.experiences),
            selectinload(CandidateProfile.certifications),
        )
    )
    job = db.get(Job, job_id)
    if profile is None or job is None:
        return AutoTailorEligibilityDecision(
            eligible_for_auto_tailor=False,
            reason="missing_profile_or_job",
            quota_date=q_date,
            remaining_quota=remaining,
        )

    req = db.scalar(select(JobRequirement).where(JobRequirement.job_id == job.id))
    master = ResumeAnalyzerAgent.select_active_master_resume(db, profile.id)
    fingerprint = compute_evidence_fingerprint(
        profile=profile,
        master_resume=master,
        job=job,
        req=req,
    )
    idem_key = compute_generation_idempotency_key(
        profile_id=profile.id,
        job_id=job.id,
        evidence_fingerprint=fingerprint,
    )

    existing_gen = db.scalar(
        select(ResumeGenerationJob).where(
            ResumeGenerationJob.idempotency_key == idem_key,
            ResumeGenerationJob.status.in_(("queued", "running", "completed")),
        )
    )
    existing_mapping = db.scalar(
        select(ResumeJobMapping).where(
            ResumeJobMapping.job_id == job.id,
            ResumeJobMapping.profile_id == profile.id,
            ResumeJobMapping.evidence_fingerprint == fingerprint,
            ResumeJobMapping.status == "completed",
        )
    )
    if existing_gen is not None or existing_mapping is not None:
        return AutoTailorEligibilityDecision(
            eligible_for_auto_tailor=False,
            reason="already_generated_or_queued",
            quota_date=q_date,
            evidence_fingerprint=fingerprint,
            idempotency_key=idem_key,
            master_resume_id=master.id if master else None,
            remaining_quota=remaining,
        )

    return AutoTailorEligibilityDecision(
        eligible_for_auto_tailor=True,
        reason="eligible",
        quota_date=q_date,
        evidence_fingerprint=fingerprint,
        idempotency_key=idem_key,
        master_resume_id=master.id if master else None,
        remaining_quota=remaining,
    )


class AutoTailorBatchSummary(BaseModel):
    enabled: bool
    min_score: int
    daily_limit: int
    evaluated_count: int = 0
    dispatched_count: int = 0
    skipped_count: int = 0
    failed_count: int = 0
    results: list[AutoTailorDispatchResult] = Field(default_factory=list)


async def trigger_auto_tailor_candidates(
    db: Session,
    *,
    user_id: uuid.UUID,
    profile_id: uuid.UUID,
    scored_candidates: list[dict[str, Any]],
    dispatcher: AutoTailorDispatcher | None = None,
    settings: Settings | None = None,
) -> AutoTailorBatchSummary:
    """Non-blocking auto-tailoring trigger prioritizing highest-scoring eligible candidates.

    Never raises into job discovery or job intelligence callers.
    When `dispatcher` is None (prior to Stage 5 transactional outbox wiring), records
    eligible candidates as `deferred_no_dispatcher` instead of pretending they were queued.
    """
    cfg = settings or get_settings()
    summary = AutoTailorBatchSummary(
        enabled=cfg.auto_tailor_enabled,
        min_score=cfg.auto_tailor_min_score,
        daily_limit=cfg.auto_tailor_daily_limit,
    )

    ordered = sorted(
        scored_candidates,
        key=lambda c: float(c.get("score") or 0.0),
        reverse=True,
    )
    dispatched_in_batch = 0

    for item in ordered:
        summary.evaluated_count += 1
        try:
            jid = uuid.UUID(str(item["job_id"]))
            score = float(item.get("score") or 0.0)
            job_eligible = bool(item.get("eligible", False))

            decision = evaluate_auto_tailor_eligibility(
                db,
                user_id=user_id,
                profile_id=profile_id,
                job_id=jid,
                match_score=score,
                job_eligible=job_eligible,
                settings=cfg,
                batch_reserved=dispatched_in_batch if dispatcher is not None else 0,
            )
            if not decision.eligible_for_auto_tailor:
                summary.skipped_count += 1
                summary.results.append(
                    AutoTailorDispatchResult(
                        job_id=jid,
                        dispatched=False,
                        status="skipped",
                        reason=decision.reason,
                        quota_date=decision.quota_date,
                    )
                )
                continue

            if dispatcher is None:
                summary.skipped_count += 1
                summary.results.append(
                    AutoTailorDispatchResult(
                        job_id=jid,
                        dispatched=False,
                        status="deferred_no_dispatcher",
                        reason="outbox_dispatcher_not_configured",
                        quota_date=decision.quota_date,
                    )
                )
                continue

            res = await dispatcher.dispatch_auto_tailoring(
                db,
                user_id=user_id,
                profile_id=profile_id,
                job_id=jid,
                match_score=score,
                evidence_fingerprint=decision.evidence_fingerprint or "",
                idempotency_key=decision.idempotency_key or "",
                quota_date=decision.quota_date,
                master_resume_id=decision.master_resume_id,
            )
            if res.dispatched:
                dispatched_in_batch += 1
                summary.dispatched_count += 1
            else:
                summary.skipped_count += 1
            summary.results.append(res)
        except Exception as exc:
            summary.failed_count += 1
            logger.warning(
                "auto_tailor_trigger_non_blocking_error",
                extra={"extra_fields": {"error_type": type(exc).__name__}},
            )
            try:
                jid_fallback = uuid.UUID(str(item["job_id"]))
            except Exception:
                jid_fallback = uuid.UUID(int=0)
            summary.results.append(
                AutoTailorDispatchResult(
                    job_id=jid_fallback,
                    dispatched=False,
                    status="failed",
                    reason=f"trigger_error:{type(exc).__name__}",
                )
            )

    return summary
