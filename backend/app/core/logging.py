import json
import logging
import re
from datetime import datetime, timezone
from typing import Any

SENSITIVE_KEYS = frozenset(
    {
        "password",
        "hashed_password",
        "secret",
        "secret_key",
        "jwt",
        "access_token",
        "refresh_token",
        "token",
        "authorization",
        "api_key",
        "encryption_key",
        "resume_text",
        "raw_text",
        "extracted_text",
        "phone",
        "email",
        "full_name",
    }
)

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_BEARER_RE = re.compile(r"Bearer\s+[A-Za-z0-9\-._~+/]+=*", re.IGNORECASE)


def redact_value(value: Any) -> Any:
    """Redact sensitive strings or nested structures for safe logging."""
    if isinstance(value, dict):
        return {
            k: ("[REDACTED]" if k.lower() in SENSITIVE_KEYS else redact_value(v))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    if isinstance(value, str):
        masked = _BEARER_RE.sub("Bearer [REDACTED]", value)
        masked = _EMAIL_RE.sub("[REDACTED_EMAIL]", masked)
        return masked
    return value


class StructuredJsonFormatter(logging.Formatter):
    """Formats log records as single-line JSON with automatic PII/secret redaction."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": redact_value(record.getMessage()),
        }
        extra_fields = getattr(record, "extra_fields", None)
        if isinstance(extra_fields, dict):
            payload["context"] = redact_value(extra_fields)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def get_logger(name: str) -> logging.Logger:
    """Return a configured structured logger without duplicate handlers."""
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(StructuredJsonFormatter())
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    return logger
