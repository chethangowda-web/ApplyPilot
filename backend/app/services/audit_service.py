import uuid
from typing import Any
from sqlalchemy.orm import Session
from app.core.logging import get_logger, redact_value
from app.database.models import AuditLog

logger = get_logger("applypilot.audit")


def record_audit_event(
    db: Session,
    *,
    user_id: uuid.UUID | None,
    actor: str,
    action: str,
    resource_type: str,
    resource_id: str,
    metadata: dict[str, Any] | None = None,
) -> AuditLog:
    """Persist a PII-redacted audit record and emit a structured log entry."""
    safe_metadata = redact_value(metadata or {})
    entry = AuditLog(
        user_id=user_id,
        actor=actor,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        event_metadata=safe_metadata,
    )
    db.add(entry)
    logger.info(
        "audit_event",
        extra={
            "extra_fields": {
                "actor": actor,
                "action": action,
                "resource_type": resource_type,
                "resource_id": resource_id,
            }
        },
    )
    return entry
