import asyncio
from collections.abc import Callable
import os
from pathlib import Path
import threading
import traceback
from typing import Any
import uuid
from celery.exceptions import SoftTimeLimitExceeded
from celery.signals import worker_ready, worker_shutting_down
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.core.logging import get_logger
from app.database.session import get_session_factory
from app.graphs.checkpointer import ensure_postgres_checkpointer_setup
from app.graphs.resume_intelligence_graph import run_resume_intelligence_workflow
from app.llm.base import ProviderError
from app.llm.router import ModelRouter
from app.services.outbox_service import (
    PROCESS_RESUME_TASK_NAME,
    claim_generation_job,
    handle_generation_job_error,
    recover_outbox_jobs,
    release_worker_lease_on_shutdown,
    renew_worker_lease,
)
from app.workers.celery_app import celery_app

logger = get_logger("applypilot.resume_worker")
_SHUTDOWN_EVENT = threading.Event()


def _is_transient_exception(exc: Exception) -> bool:
    if isinstance(
        exc,
        (
            SoftTimeLimitExceeded,
            TimeoutError,
            OSError,
            ConnectionError,
            ProviderError,
            SQLAlchemyError,
        ),
    ):
        return True
    if isinstance(exc, (ValueError, KeyError)):
        return False
    return True


def execute_resume_generation_job(
    generation_job_id: uuid.UUID,
    *,
    worker_id: str | None = None,
    session_factory: sessionmaker[Session] | None = None,
    lease_seconds: int = 120,
    heartbeat_interval: float = 20.0,
    use_llm: bool = True,
    force_regenerate: bool = False,
    storage_dir: Path | None = None,
    router: ModelRouter | None = None,
    checkpointer: Any | None = None,
    workflow_runner: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Claim, heartbeat-protect, execute, and finalize one durable `ResumeGenerationJob`."""
    factory = session_factory or get_session_factory()
    resolved_worker_id = worker_id or f"worker-{os.getpid()}-{uuid.uuid4().hex[:8]}"

    with factory() as claim_db:
        claim = claim_generation_job(
            claim_db,
            generation_job_id,
            worker_id=resolved_worker_id,
            lease_seconds=lease_seconds,
        )

    if not claim.claimed:
        return {
            "generation_job_id": str(generation_job_id),
            "claimed": False,
            "status": claim.status or "skipped",
            "reason": claim.reason,
            "retry_scheduled": False,
        }

    assert claim.user_id is not None and claim.job_id is not None

    stop_hb = threading.Event()

    def _heartbeat_loop() -> None:
        while not stop_hb.wait(max(0.5, heartbeat_interval)):
            if _SHUTDOWN_EVENT.is_set():
                break
            try:
                with factory() as hb_db:
                    renewed = renew_worker_lease(
                        hb_db,
                        generation_job_id,
                        worker_id=resolved_worker_id,
                        lease_seconds=lease_seconds,
                    )
                    if not renewed:
                        break
            except Exception as hb_exc:
                logger.warning(
                    "worker_heartbeat_error",
                    extra={"extra_fields": {"job_id": str(generation_job_id), "error_type": type(hb_exc).__name__}},
                )

    hb_thread = threading.Thread(
        target=_heartbeat_loop,
        name=f"hb-{generation_job_id.hex[:8]}",
        daemon=True,
    )
    hb_thread.start()

    try:
        runner = workflow_runner or run_resume_intelligence_workflow
        with factory() as exec_db:
            final_state = asyncio.run(
                runner(
                    exec_db,
                    user_id=claim.user_id,
                    job_id=claim.job_id,
                    master_resume_id=claim.master_resume_id,
                    generation_job_id=generation_job_id,
                    trigger_mode=claim.trigger_mode or "auto",
                    run_id=claim.thread_id,
                    use_llm=use_llm,
                    force_regenerate=(force_regenerate or claim.trigger_mode == "regenerate"),
                    storage_dir=storage_dir,
                    router=router,
                    checkpointer=checkpointer,
                )
            )
        return {
            "generation_job_id": str(generation_job_id),
            "claimed": True,
            "status": final_state.get("status", "completed"),
            "resume_id": final_state.get("resume_id"),
            "mapping_id": final_state.get("mapping_id"),
            "pdf_generated": bool(final_state.get("pdf_generated", False)),
            "retry_scheduled": False,
        }
    except SoftTimeLimitExceeded:
        with factory() as shut_db:
            release_worker_lease_on_shutdown(
                shut_db,
                generation_job_id,
                worker_id=resolved_worker_id,
                reason="soft_time_limit_exceeded",
            )
        return {
            "generation_job_id": str(generation_job_id),
            "claimed": True,
            "status": "queued",
            "reason": "soft_time_limit_exceeded",
            "retry_scheduled": True,
        }
    except Exception as exc:
        transient = _is_transient_exception(exc)
        err_summary = f"{type(exc).__name__}: {str(exc)}"[:500]
        logger.warning(
            "resume_generation_worker_error",
            extra={
                "extra_fields": {
                    "generation_job_id": str(generation_job_id),
                    "worker_id": resolved_worker_id,
                    "transient": transient,
                    "error_type": type(exc).__name__,
                }
            },
        )
        with factory() as err_db:
            final_status, should_retry = handle_generation_job_error(
                err_db,
                generation_job_id,
                worker_id=resolved_worker_id,
                error_message=err_summary,
                transient=transient,
            )
        return {
            "generation_job_id": str(generation_job_id),
            "claimed": True,
            "status": final_status,
            "error": err_summary,
            "traceback_tail": traceback.format_exc().splitlines()[-1][:200],
            "retry_scheduled": should_retry,
        }
    finally:
        stop_hb.set()
        hb_thread.join(timeout=1.5)


@celery_app.task(
    name=PROCESS_RESUME_TASK_NAME,
    bind=True,
    max_retries=3,
    acks_late=True,
    reject_on_worker_lost=True,
)
def process_resume_generation(self, generation_job_id: str) -> dict[str, Any]:
    """Celery task entrypoint with durable late ack, lease renewal, and bounded retry."""
    task_id = getattr(self.request, "id", None) or uuid.uuid4().hex[:8]
    hostname = getattr(self.request, "hostname", None) or "celery"
    worker_id = f"{hostname}:{task_id}"
    gen_uuid = uuid.UUID(generation_job_id)

    result = execute_resume_generation_job(gen_uuid, worker_id=worker_id)
    if result.get("retry_scheduled"):
        retries = int(getattr(self.request, "retries", 0) or 0)
        countdown = min(30, 2 ** retries)
        raise self.retry(
            exc=RuntimeError(result.get("error") or result.get("reason") or "transient_error"),
            countdown=countdown,
        )
    return result


@celery_app.task(name="app.workers.resume_tasks.recover_pending_outbox")
def recover_pending_outbox() -> dict[str, int]:
    """Sweep expired worker leases and dispatch committed pending outbox entries."""
    factory = get_session_factory()
    with factory() as db:
        return recover_outbox_jobs(db)


@worker_ready.connect
def _on_worker_ready(**_kwargs) -> None:
    """Ensure LangGraph checkpoints exist and sweep orphaned/pending outbox jobs on worker boot."""
    _SHUTDOWN_EVENT.clear()
    try:
        ensure_postgres_checkpointer_setup()
    except Exception as exc:
        logger.warning(
            "worker_ready_checkpointer_setup_skipped",
            extra={"extra_fields": {"error_type": type(exc).__name__}},
        )
    try:
        with get_session_factory()() as db:
            stats = recover_outbox_jobs(db)
            logger.info("worker_ready_outbox_recovery", extra={"extra_fields": stats})
    except Exception as exc:
        logger.warning(
            "worker_ready_outbox_recovery_failed",
            extra={"extra_fields": {"error_type": type(exc).__name__}},
        )


@worker_shutting_down.connect
def _on_worker_shutting_down(**_kwargs) -> None:
    """Signal active heartbeat threads to halt cleanly on worker SIGTERM/SIGINT."""
    _SHUTDOWN_EVENT.set()
