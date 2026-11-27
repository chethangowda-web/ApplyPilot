import uuid

from sqlalchemy import select

from app.core.config import get_settings
from app.database.models import OAuthConnection
from app.database.session import get_session_factory
from app.services.gmail_confirmation_verifier import register_gmail_confirmation_verifier
from app.services.gmail_sync_service import GmailSyncService
from app.workers.celery_app import celery_app


@celery_app.task(
    name="applypilot.gmail.sync_connection",
    bind=True,
    max_retries=3,
    default_retry_delay=30,
)
def sync_gmail_connection_task(self, connection_id: str) -> dict[str, int]:
    """Synchronize a single connected Gmail mailbox with bounded retries."""
    register_gmail_confirmation_verifier()
    cid = uuid.UUID(connection_id)
    with get_session_factory()() as db:
        conn = db.scalar(select(OAuthConnection).where(OAuthConnection.id == cid))
        if conn is None or conn.status != "connected":
            return {"processed": 0, "confirmed": 0, "ambiguous": 0}
        try:
            return GmailSyncService().sync_connection(db, conn=conn)
        except Exception as exc:
            raise self.retry(exc=exc, countdown=min(300, 30 * (2**self.request.retries)))


@celery_app.task(name="applypilot.gmail.sync_all_enabled")
def sync_all_enabled_gmail_connections_task() -> dict[str, int]:
    """Optional Celery Beat periodic sync across connected Gmail accounts."""
    register_gmail_confirmation_verifier()
    cfg = get_settings()
    if not cfg.gmail_sync_beat_enabled:
        return {"connections_checked": 0, "confirmed": 0}
    checked = 0
    confirmed = 0
    with get_session_factory()() as db:
        conns = list(
            db.scalars(
                select(OAuthConnection).where(
                    OAuthConnection.provider == "google_gmail",
                    OAuthConnection.status == "connected",
                )
            )
        )
        svc = GmailSyncService(settings=cfg)
        for conn in conns:
            res = svc.sync_connection(db, conn=conn)
            checked += 1
            confirmed += res["confirmed"]
    return {"connections_checked": checked, "confirmed": confirmed}
