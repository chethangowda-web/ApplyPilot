from fastapi import APIRouter, Depends, Response, status
import redis
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.api.dependencies.auth import get_current_user
from app.core.config import get_settings
from app.core.resilience import dead_letter_buffer
from app.database.models import User
from app.database.session import get_db
from app.services.observability_service import observability_registry

router = APIRouter(tags=["operations"])


@router.get("/health/ready")
def readiness_check(response: Response, db: Session = Depends(get_db)) -> dict[str, str]:
    """Readiness probe verifying PostgreSQL and Redis connectivity without exposing secrets."""
    db_ok = False
    redis_ok = False
    try:
        db.execute(text("SELECT 1"))
        db_ok = True
    except Exception:
        db_ok = False

    try:
        cfg = get_settings()
        r = redis.Redis.from_url(cfg.redis_url, socket_connect_timeout=1, socket_timeout=1)
        redis_ok = bool(r.ping())
    except Exception:
        redis_ok = False

    if not (db_ok and redis_ok):
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {
            "status": "degraded",
            "database": "ok" if db_ok else "unreachable",
            "redis": "ok" if redis_ok else "unreachable",
        }
    return {"status": "ready", "database": "ok", "redis": "ok"}


@router.get("/api/ops/observability")
def get_observability_summary(current_user: User = Depends(get_current_user)) -> dict:
    """Return provider-independent AI cost/latency metrics and recent dead-letter task summaries."""
    _ = current_user.id
    return {
        "ai": observability_registry.summary(),
        "dead_letters": dead_letter_buffer.list_recent(limit=20),
    }
