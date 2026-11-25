from datetime import date, datetime, timezone
import hashlib
import uuid
from zoneinfo import ZoneInfo
from pydantic import BaseModel
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.agents.resume_intelligence.resume_analyzer import ResumeAnalyzerAgent
from app.core.config import Settings, get_settings
from app.database.models import (
    AutoTailorDailyQuota,
    CandidateProfile,
    Job,
    JobRequirement,
    ResumeGenerationJob,
    ResumeJobMapping,
    utc_now,
)
from app.services.fingerprint_service import (
    TEMPLATE_VERSION,
    compute_evidence_fingerprint,
    compute_generation_idempotency_key,
)

ASIA_KOLKATA = ZoneInfo("Asia/Kolkata")


def resolve_kolkata_quota_date(now_utc: datetime | None = None) -> date:
    """Compute canonical quota date in Asia/Kolkata regardless of server timezone."""
    ref = now_utc or datetime.now(timezone.utc)
    if ref.tzinfo is None:
        ref = ref.replace(tzinfo=timezone.utc)
    return ref.astimezone(ASIA_KOLKATA).date()


def _acquire_logical_advisory_lock(db: Session, lock_key: str) -> None:
    """Acquire transaction-scoped PostgreSQL advisory lock to serialize concurrent logical requests."""
    dialect = db.bind.dialect.name if db.bind is not None else "sqlite"
    if dialect != "postgresql":
        return
    digest = hashlib.sha256(lock_key.encode("utf-8")).digest()
    signed_i64 = int.from_bytes(digest[:8], byteorder="big", signed=True)
    db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": signed_i64})


def reserve_daily_quota_atomic(
    db: Session,
    *,
    user_id: uuid.UUID,
    quota_date: date,
    daily_limit: int,
) -> tuple[bool, int]:
    """Atomically reserve one auto-tailor slot in `auto_tailor_daily_quotas`.

    Safe under concurrent PostgreSQL writers:
    1. `INSERT ... ON CONFLICT DO NOTHING` guarantees the composite-PK row exists.
    2. `SELECT ... FOR UPDATE` locks that single user+date quota row until commit/rollback.
    """
    if daily_limit <= 0:
        return False, 0

    dialect = db.bind.dialect.name if db.bind is not None else "sqlite"
    now = utc_now()
    values = {
        "user_id": user_id,
        "quota_date": quota_date,
        "reserved_count": 0,
        "updated_at": now,
    }
    if dialect == "postgresql":
        stmt = (
            pg_insert(AutoTailorDailyQuota)
            .values(**values)
            .on_conflict_do_nothing(index_elements=["user_id", "quota_date"])
        )
        db.execute(stmt)
    elif dialect == "sqlite":
        stmt = (
            sqlite_insert(AutoTailorDailyQuota)
            .values(**values)
            .on_conflict_do_nothing(index_elements=["user_id", "quota_date"])
        )
        db.execute(stmt)
    else:
        existing = db.scalar(
            select(AutoTailorDailyQuota).where(
                AutoTailorDailyQuota.user_id == user_id,
                AutoTailorDailyQuota.quota_date == quota_date,
            )
        )
        if existing is None:
            db.add(AutoTailorDailyQuota(**values))
            db.flush()

    sel = select(AutoTailorDailyQuota).where(
        AutoTailorDailyQuota.user_id == user_id,
        AutoTailorDailyQuota.quota_date == quota_date,
    )
    if dialect == "postgresql":
        sel = sel.with_for_update()
    quota_row = db.execute(sel).scalar_one()

    if quota_row.reserved_count >= daily_limit:
        return False, int(quota_row.reserved_count)

    quota_row.reserved_count += 1
    quota_row.updated_at = utc_now()
    db.flush()
    return True, int(quota_row.reserved_count)


class OutboxEnqueueOutcome(BaseModel):
    created: bool
    accepted: bool
    status: str  # queued | existing | skipped | quota_exhausted | disabled | below_threshold
    reason: str
    generation_job_id: uuid.UUID | None = None
    existing_resume_id: uuid.UUID | None = None
    evidence_fingerprint: str | None = None
    idempotency_key: str | None = None
    quota_date: date | None = None
    reserved_count_after: int | None = None


def enqueue_resume_generation_in_tx(
    db: Session,
    *,
    user_id: uuid.UUID,
    profile_id: uuid.UUID,
    job_id: uuid.UUID,
    trigger_mode: str = "manual",  # auto | manual | regenerate
    force_regenerate: bool = False,
    match_score: float | None = None,
    job_eligible: bool = True,
    master_resume_id: uuid.UUID | None = None,
    template_version: str = TEMPLATE_VERSION,
    max_attempts: int = 3,
    settings: Settings | None = None,
) -> OutboxEnqueueOutcome:
    """Persist an outbox `ResumeGenerationJob` + optional atomic auto quota inside ONE DB transaction.

    Caller must commit (or let this commit) before dispatching to Celery.
    Never consumes quota for `manual` or `regenerate` requests, nor when an existing active/completed
    logical request already satisfies the same `(job_id, profile_id, evidence_fingerprint)`.
    """
    cfg = settings or get_settings()
    if trigger_mode not in ("auto", "manual", "regenerate"):
        raise ValueError(f"Unsupported trigger_mode: {trigger_mode}")

    effective_mode = "regenerate" if (force_regenerate or trigger_mode == "regenerate") else trigger_mode
    q_date = resolve_kolkata_quota_date() if effective_mode == "auto" else None

    if effective_mode == "auto":
        if not cfg.auto_tailor_enabled:
            return OutboxEnqueueOutcome(
                created=False,
                accepted=False,
                status="disabled",
                reason="auto_tailor_disabled",
                quota_date=q_date,
            )
        if not job_eligible:
            return OutboxEnqueueOutcome(
                created=False,
                accepted=False,
                status="skipped",
                reason="job_ineligible",
                quota_date=q_date,
            )
        if match_score is None or float(match_score) < float(cfg.auto_tailor_min_score):
            return OutboxEnqueueOutcome(
                created=False,
                accepted=False,
                status="below_threshold",
                reason="below_auto_tailor_min_score",
                quota_date=q_date,
            )

    profile = db.scalar(
        select(CandidateProfile)
        .where(CandidateProfile.id == profile_id, CandidateProfile.user_id == user_id)
        .options(
            selectinload(CandidateProfile.skills),
            selectinload(CandidateProfile.projects),
            selectinload(CandidateProfile.experiences),
            selectinload(CandidateProfile.certifications),
        )
    )
    job = db.get(Job, job_id)
    if profile is None or job is None:
        raise ValueError("Candidate profile or job not found")

    req = db.scalar(select(JobRequirement).where(JobRequirement.job_id == job.id))
    master = ResumeAnalyzerAgent.select_active_master_resume(
        db, profile.id, master_resume_id=master_resume_id
    )
    fingerprint = compute_evidence_fingerprint(
        profile=profile,
        master_resume=master,
        job=job,
        req=req,
        template_version=template_version,
    )

    is_forced = effective_mode == "regenerate"
    idem_key = (
        None
        if is_forced
        else compute_generation_idempotency_key(
            profile_id=profile.id,
            job_id=job.id,
            evidence_fingerprint=fingerprint,
        )
    )

    if not is_forced and idem_key is not None:
        _acquire_logical_advisory_lock(db, f"tailor-idem:{idem_key}")

        existing_job = db.scalar(
            select(ResumeGenerationJob).where(
                ResumeGenerationJob.idempotency_key == idem_key,
                ResumeGenerationJob.status.in_(("queued", "running", "completed")),
            )
        )
        if existing_job is not None:
            return OutboxEnqueueOutcome(
                created=False,
                accepted=True,
                status="existing",
                reason=f"existing_generation_job_{existing_job.status}",
                generation_job_id=existing_job.id,
                existing_resume_id=existing_job.resume_id,
                evidence_fingerprint=fingerprint,
                idempotency_key=idem_key,
                quota_date=existing_job.quota_date,
            )

        existing_mapping = db.scalar(
            select(ResumeJobMapping).where(
                ResumeJobMapping.job_id == job.id,
                ResumeJobMapping.profile_id == profile.id,
                ResumeJobMapping.evidence_fingerprint == fingerprint,
                ResumeJobMapping.status == "completed",
            )
        )
        if existing_mapping is not None and existing_mapping.resume_id is not None:
            return OutboxEnqueueOutcome(
                created=False,
                accepted=True,
                status="existing",
                reason="existing_completed_mapping",
                generation_job_id=existing_mapping.generation_job_id,
                existing_resume_id=existing_mapping.resume_id,
                evidence_fingerprint=fingerprint,
                idempotency_key=idem_key,
                quota_date=q_date,
            )

    reserved_after: int | None = None
    if effective_mode == "auto":
        assert q_date is not None
        ok, reserved_after = reserve_daily_quota_atomic(
            db,
            user_id=user_id,
            quota_date=q_date,
            daily_limit=int(cfg.auto_tailor_daily_limit),
        )
        if not ok:
            return OutboxEnqueueOutcome(
                created=False,
                accepted=False,
                status="quota_exhausted",
                reason="daily_quota_exhausted",
                evidence_fingerprint=fingerprint,
                idempotency_key=idem_key,
                quota_date=q_date,
                reserved_count_after=reserved_after,
            )

    gen_id = uuid.uuid4()
    gen_job = ResumeGenerationJob(
        id=gen_id,
        user_id=user_id,
        profile_id=profile.id,
        job_id=job.id,
        master_resume_id=master.id if master else None,
        trigger_mode=effective_mode,
        evidence_fingerprint=fingerprint,
        quota_date=q_date,
        idempotency_key=idem_key,
        status="queued",
        attempt_count=0,
        max_attempts=max(1, max_attempts),
        thread_id=f"res-{gen_id.hex[:12]}",
    )

    try:
        with db.begin_nested():
            db.add(gen_job)
            db.flush()
    except IntegrityError:
        # Rare race on SQLite or partial unique index where two non-forced writers raced
        if idem_key is not None:
            existing_job = db.scalar(
                select(ResumeGenerationJob).where(
                    ResumeGenerationJob.idempotency_key == idem_key,
                    ResumeGenerationJob.status.in_(("queued", "running", "completed")),
                )
            )
            if existing_job is not None:
                outcome = OutboxEnqueueOutcome(
                    created=False,
                    accepted=True,
                    status="existing",
                    reason=f"existing_generation_job_{existing_job.status}",
                    generation_job_id=existing_job.id,
                    existing_resume_id=existing_job.resume_id,
                    evidence_fingerprint=fingerprint,
                    idempotency_key=idem_key,
                    quota_date=existing_job.quota_date,
                )
                # Roll back whole transaction if auto quota was incremented before duplicate collision
                db.rollback()
                return outcome
        raise

    return OutboxEnqueueOutcome(
        created=True,
        accepted=True,
        status="queued",
        reason="outbox_queued",
        generation_job_id=gen_job.id,
        evidence_fingerprint=fingerprint,
        idempotency_key=idem_key,
        quota_date=q_date,
        reserved_count_after=reserved_after,
    )
