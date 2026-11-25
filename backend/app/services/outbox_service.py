from collections.abc import Callable
from datetime import date, timedelta
from pathlib import Path
from typing import Any
import uuid
from pydantic import BaseModel
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.database.models import ResumeGenerationJob, utc_now
from app.services.auto_tailor_trigger import AutoTailorDispatchResult
from app.services.fingerprint_service import TEMPLATE_VERSION
from app.services.quota_service import (
    OutboxEnqueueOutcome,
    enqueue_resume_generation_in_tx,
)
from app.services.resume_vault_service import reconcile_orphaned_tailored_pdfs

logger = get_logger("applypilot.outbox")
PROCESS_RESUME_TASK_NAME = "app.workers.resume_tasks.process_resume_generation"


class ClaimResult(BaseModel):
    claimed: bool
    reason: str
    generation_job_id: uuid.UUID
    user_id: uuid.UUID | None = None
    profile_id: uuid.UUID | None = None
    job_id: uuid.UUID | None = None
    master_resume_id: uuid.UUID | None = None
    trigger_mode: str | None = None
    thread_id: str | None = None
    attempt_count: int = 0
    max_attempts: int = 3
    status: str | None = None


def dispatch_outbox_job_to_broker(
    generation_job_id: uuid.UUID,
    *,
    countdown: int | None = None,
) -> bool:
    """Best-effort enqueue of an ALREADY-COMMITTED outbox job to Celery/Redis.

    Never raises on Redis outage; committed outbox rows stay safe in PostgreSQL for recovery.
    """
    try:
        from app.workers.celery_app import celery_app

        opts: dict[str, Any] = {
            "args": [str(generation_job_id)],
            "retry": False,  # Do not hang FastAPI/discovery threads when Redis is unreachable
        }
        if countdown is not None and countdown > 0:
            opts["countdown"] = int(countdown)
        celery_app.send_task(PROCESS_RESUME_TASK_NAME, **opts)
        return True
    except Exception as exc:
        logger.warning(
            "outbox_celery_dispatch_deferred",
            extra={
                "extra_fields": {
                    "generation_job_id": str(generation_job_id),
                    "error_type": type(exc).__name__,
                }
            },
        )
        return False


def enqueue_and_commit_resume_generation(
    db: Session,
    *,
    user_id: uuid.UUID,
    profile_id: uuid.UUID,
    job_id: uuid.UUID,
    trigger_mode: str = "manual",
    force_regenerate: bool = False,
    match_score: float | None = None,
    job_eligible: bool = True,
    master_resume_id: uuid.UUID | None = None,
    template_version: str = TEMPLATE_VERSION,
    max_attempts: int = 3,
    settings: Settings | None = None,
    dispatch: bool = True,
    broker_dispatch_fn: Callable[[uuid.UUID], bool] | None = None,
) -> tuple[OutboxEnqueueOutcome, bool]:
    """Commit the PostgreSQL outbox + atomic quota FIRST, then best-effort dispatch to Celery."""
    outcome = enqueue_resume_generation_in_tx(
        db,
        user_id=user_id,
        profile_id=profile_id,
        job_id=job_id,
        trigger_mode=trigger_mode,
        force_regenerate=force_regenerate,
        match_score=match_score,
        job_eligible=job_eligible,
        master_resume_id=master_resume_id,
        template_version=template_version,
        max_attempts=max_attempts,
        settings=settings,
    )
    if not outcome.created:
        db.rollback()
        return outcome, False

    # Commit BEFORE broker dispatch so queued row survives crash/Redis outage
    db.commit()

    dispatched_to_broker = False
    if dispatch and outcome.generation_job_id is not None:
        fn = broker_dispatch_fn or dispatch_outbox_job_to_broker
        dispatched_to_broker = bool(fn(outcome.generation_job_id))

    return outcome, dispatched_to_broker


class TransactionalOutboxDispatcher:
    """Production Stage 5 dispatcher implementing Stage 4's `AutoTailorDispatcher` protocol."""

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        broker_dispatch_fn: Callable[[uuid.UUID], bool] | None = None,
        dispatch_to_broker: bool | None = None,
    ) -> None:
        self._settings = settings
        self._broker_dispatch_fn = broker_dispatch_fn
        self._dispatch_to_broker = dispatch_to_broker

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
    ) -> AutoTailorDispatchResult:
        if self._dispatch_to_broker is not None:
            should_dispatch = self._dispatch_to_broker
        else:
            # Skip cross-process Celery dispatch when running against an isolated in-memory SQLite test DB
            url_str = str(db.bind.url) if db.bind is not None and db.bind.url else ""
            should_dispatch = not url_str.endswith(":memory:")

        outcome, broker_ok = enqueue_and_commit_resume_generation(
            db,
            user_id=user_id,
            profile_id=profile_id,
            job_id=job_id,
            trigger_mode="auto",
            force_regenerate=False,
            match_score=match_score,
            job_eligible=True,
            master_resume_id=master_resume_id,
            settings=self._settings,
            dispatch=should_dispatch,
            broker_dispatch_fn=self._broker_dispatch_fn,
        )
        if outcome.created:
            reason = "queued" if broker_ok else "outbox_committed_broker_deferred"
            return AutoTailorDispatchResult(
                job_id=job_id,
                dispatched=True,
                status="queued",
                reason=reason,
                generation_job_id=outcome.generation_job_id,
                quota_date=outcome.quota_date or quota_date,
            )

        return AutoTailorDispatchResult(
            job_id=job_id,
            dispatched=False,
            status=outcome.status,
            reason=outcome.reason,
            generation_job_id=outcome.generation_job_id,
            quota_date=outcome.quota_date or quota_date,
        )


def claim_generation_job(
    db: Session,
    generation_job_id: uuid.UUID,
    *,
    worker_id: str,
    lease_seconds: int = 120,
) -> ClaimResult:
    """Safely claim a `ResumeGenerationJob` using `FOR UPDATE SKIP LOCKED` on PostgreSQL."""
    stmt = select(ResumeGenerationJob).where(ResumeGenerationJob.id == generation_job_id)
    dialect = db.bind.dialect.name if db.bind is not None else "sqlite"
    if dialect == "postgresql":
        stmt = stmt.with_for_update(skip_locked=True)

    row = db.execute(stmt).scalar_one_or_none()
    if row is None:
        return ClaimResult(
            claimed=False,
            reason="locked_or_missing",
            generation_job_id=generation_job_id,
        )

    now = utc_now()
    if row.status in ("completed", "validation_failed"):
        return ClaimResult(
            claimed=False,
            reason=f"already_terminal_{row.status}",
            generation_job_id=row.id,
            status=row.status,
            attempt_count=row.attempt_count,
            max_attempts=row.max_attempts,
        )

    if row.status == "running":
        lease_exp = row.lease_expires_at
        if lease_exp is not None:
            if lease_exp.tzinfo is None:
                lease_exp = lease_exp.replace(tzinfo=now.tzinfo)
            if lease_exp > now:
                return ClaimResult(
                    claimed=False,
                    reason="already_running_valid_lease",
                    generation_job_id=row.id,
                    status=row.status,
                    attempt_count=row.attempt_count,
                    max_attempts=row.max_attempts,
                )

    if row.attempt_count >= row.max_attempts:
        if row.status != "failed":
            row.status = "failed"
            row.worker_id = None
            row.lease_expires_at = None
            row.completed_at = row.completed_at or now
            row.error_message = row.error_message or "Max retry attempts exhausted"
            db.commit()
        return ClaimResult(
            claimed=False,
            reason="max_attempts_exhausted",
            generation_job_id=row.id,
            status="failed",
            attempt_count=row.attempt_count,
            max_attempts=row.max_attempts,
        )

    row.status = "running"
    row.attempt_count += 1
    row.worker_id = worker_id[:120]
    row.started_at = row.started_at or now
    row.last_heartbeat_at = now
    row.lease_expires_at = now + timedelta(seconds=max(5, lease_seconds))
    db.commit()

    return ClaimResult(
        claimed=True,
        reason="claimed",
        generation_job_id=row.id,
        user_id=row.user_id,
        profile_id=row.profile_id,
        job_id=row.job_id,
        master_resume_id=row.master_resume_id,
        trigger_mode=row.trigger_mode,
        thread_id=row.thread_id,
        attempt_count=row.attempt_count,
        max_attempts=row.max_attempts,
        status=row.status,
    )


def renew_worker_lease(
    db: Session,
    generation_job_id: uuid.UUID,
    *,
    worker_id: str,
    lease_seconds: int = 120,
) -> bool:
    """Extend worker heartbeat and lease expiry while actively executing."""
    now = utc_now()
    res = db.execute(
        update(ResumeGenerationJob)
        .where(
            ResumeGenerationJob.id == generation_job_id,
            ResumeGenerationJob.worker_id == worker_id[:120],
            ResumeGenerationJob.status == "running",
        )
        .values(
            last_heartbeat_at=now,
            lease_expires_at=now + timedelta(seconds=max(5, lease_seconds)),
        )
    )
    db.commit()
    return bool(res.rowcount and res.rowcount > 0)


def release_worker_lease_on_shutdown(
    db: Session,
    generation_job_id: uuid.UUID,
    *,
    worker_id: str,
    reason: str = "worker_graceful_shutdown",
) -> bool:
    """Return an interrupted `running` job to `queued` on graceful shutdown without losing quota."""
    res = db.execute(
        update(ResumeGenerationJob)
        .where(
            ResumeGenerationJob.id == generation_job_id,
            ResumeGenerationJob.worker_id == worker_id[:120],
            ResumeGenerationJob.status == "running",
        )
        .values(
            status="queued",
            worker_id=None,
            lease_expires_at=None,
            error_message=reason[:500],
        )
    )
    db.commit()
    return bool(res.rowcount and res.rowcount > 0)


def handle_generation_job_error(
    db: Session,
    generation_job_id: uuid.UUID,
    *,
    worker_id: str,
    error_message: str,
    transient: bool = True,
) -> tuple[str, bool]:
    """Handle transient vs terminal worker errors while preserving original quota reservation.

    Returns `(final_job_status, should_retry_in_celery)`.
    """
    stmt = select(ResumeGenerationJob).where(ResumeGenerationJob.id == generation_job_id)
    if db.bind is not None and db.bind.dialect.name == "postgresql":
        stmt = stmt.with_for_update()

    row = db.execute(stmt).scalar_one_or_none()
    if row is None:
        return "missing", False
    if row.status in ("completed", "validation_failed"):
        return row.status, False
    if row.worker_id and row.worker_id != worker_id[:120]:
        # Another worker already reclaimed expired lease
        return row.status, False

    safe_err = error_message[:500]
    row.worker_id = None
    row.lease_expires_at = None
    row.error_message = safe_err

    if transient and row.attempt_count < row.max_attempts:
        row.status = "queued"
        db.commit()
        return "queued", True

    row.status = "failed"
    row.completed_at = utc_now()
    db.commit()
    return "failed", False


def recover_outbox_jobs(
    db: Session,
    *,
    dispatch_fn: Callable[[uuid.UUID], bool] | None = None,
    stale_queued_after_seconds: int = 0,
    limit: int = 50,
    storage_dir: Path | None = None,
    orphan_min_age_seconds: int = 300,
) -> dict[str, int]:
    """Recover expired worker leases, dispatch pending `queued` outbox jobs, and clean aged orphan PDFs."""
    now = utc_now()
    dialect = db.bind.dialect.name if db.bind is not None else "sqlite"

    # 1. Recover expired or orphaned `running` leases
    stale_stmt = (
        select(ResumeGenerationJob)
        .where(
            ResumeGenerationJob.status == "running",
            (ResumeGenerationJob.lease_expires_at < now) | (ResumeGenerationJob.lease_expires_at.is_(None)),
        )
        .limit(limit)
    )
    if dialect == "postgresql":
        stale_stmt = stale_stmt.with_for_update(skip_locked=True)

    stale_rows = list(db.execute(stale_stmt).scalars().all())
    expired_requeued = 0
    expired_failed = 0
    for row in stale_rows:
        row.worker_id = None
        row.lease_expires_at = None
        if row.attempt_count >= row.max_attempts:
            row.status = "failed"
            row.completed_at = now
            row.error_message = "Worker lease expired after max retry attempts"
            expired_failed += 1
        else:
            row.status = "queued"
            row.error_message = "Recovered after worker lease expiration"
            expired_requeued += 1
    if stale_rows:
        db.commit()

    # 2. Dispatch committed `queued` outbox entries (including newly requeued & post-outage rows)
    cutoff = now - timedelta(seconds=max(0, stale_queued_after_seconds))
    queued_stmt = (
        select(ResumeGenerationJob)
        .where(
            ResumeGenerationJob.status == "queued",
            ResumeGenerationJob.created_at <= cutoff,
        )
        .order_by(ResumeGenerationJob.created_at.asc())
        .limit(limit)
    )
    queued_rows = list(db.execute(queued_stmt).scalars().all())
    fn = dispatch_fn or dispatch_outbox_job_to_broker

    queued_dispatched = 0
    queued_deferred = 0
    for row in queued_rows:
        ok = bool(fn(row.id))
        if ok:
            queued_dispatched += 1
        else:
            queued_deferred += 1

    orphans_removed = 0
    try:
        resolved_storage = storage_dir if storage_dir is not None else Path(get_settings().storage_dir)
        orphan_stats = reconcile_orphaned_tailored_pdfs(
            db,
            storage_root=resolved_storage,
            min_age_seconds=orphan_min_age_seconds,
        )
        orphans_removed = orphan_stats["removed"]
    except Exception as exc:
        logger.warning(
            "orphan_pdf_reconciliation_skipped",
            extra={"extra_fields": {"error_type": type(exc).__name__}},
        )

    return {
        "expired_requeued": expired_requeued,
        "expired_failed": expired_failed,
        "queued_dispatched": queued_dispatched,
        "queued_deferred": queued_deferred,
        "orphans_removed": orphans_removed,
    }


def get_default_settings_safe() -> Settings:
    return get_settings()
