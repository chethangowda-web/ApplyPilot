from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
import secrets
from typing import Literal
import uuid

from app.database.models import Application, ApprovalRecord, Job, JobSource, Resume
from app.schemas.applications import (
    ApplicationMode,
    ExecutionOptions,
    ExecutionResult,
    ManualSubmitAuthorizationResponse,
)


class ExecutionError(RuntimeError):
    """Base error for application execution."""


class ApprovalValidationError(ExecutionError):
    """Raised when an approval record is missing, expired, or invalidated."""


class SSRFBlockedError(ExecutionError):
    """Raised when a URL, redirect, or resolved IP violates browser egress rules."""


class RestrictionDetectedError(ExecutionError):
    """Raised when CAPTCHA, MFA, login wall, or anti-bot gate requires manual action."""


class SubmitBlockedViolationError(ExecutionError):
    """Raised when an unauthorized submission attempt is intercepted."""


class UnsupportedPlatformError(ExecutionError):
    """Raised when the target platform/environment cannot execute safely."""


@dataclass
class ManualSubmitAuthorization:
    """Internal server-only single-use manual submit authorization record.

    Not a Pydantic schema: cannot be deserialized from HTTP requests or serialized in responses.
    """

    application_id: uuid.UUID
    handoff_session_id: uuid.UUID
    user_id: uuid.UUID
    destination_url: str
    form_action: str
    http_method: Literal["GET", "POST"]
    answers_hash: str
    resume_checksum: str
    approval_snapshot_hash: str
    issued_at: datetime
    expires_at: datetime
    authorization_id: uuid.UUID = field(default_factory=uuid.uuid4, init=False)
    _authorization_token: str = field(default_factory=lambda: secrets.token_urlsafe(32), init=False, repr=False)
    _consumed: bool = field(default=False, init=False)

    @property
    def authorization_token(self) -> str:
        return self._authorization_token

    @property
    def consumed(self) -> bool:
        return self._consumed

    def mark_consumed(self) -> None:
        if self._consumed:
            raise SubmitBlockedViolationError("Manual submit authorization token has already been consumed")
        self._consumed = True

    def to_safe_response(self) -> ManualSubmitAuthorizationResponse:
        return ManualSubmitAuthorizationResponse(
            authorization_id=self.authorization_id,
            application_id=self.application_id,
            handoff_session_id=self.handoff_session_id,
            destination_url=self.destination_url,
            form_action=self.form_action,
            http_method=self.http_method,
            issued_at=self.issued_at,
            expires_at=self.expires_at,
            consumed=self._consumed,
        )


@dataclass(frozen=True)
class ExecutionContext:
    """Internal server-only execution bundle (never serialized to public API responses/logs)."""

    application: Application
    job: Job
    resume: Resume
    verified_resume_path: Path
    verified_answers: dict[str, str]
    answers_hash: str
    approval_record: ApprovalRecord
    options: ExecutionOptions = field(default_factory=ExecutionOptions)


class ApplicationExecutionAdapter(ABC):
    """Abstract execution adapter contract (distinct from read-only JobSourceConnector)."""

    adapter_name: str
    mode: ApplicationMode

    @abstractmethod
    def execute(self, ctx: ExecutionContext) -> ExecutionResult:
        """Execute an approved application within this adapter's safety boundaries."""


class OfficialApiSubmissionAdapter(ApplicationExecutionAdapter, ABC):
    """Contract for officially documented, explicitly authorized partner submission APIs."""

    mode: ApplicationMode = "official_api"
    documented_api_spec_url: str

    @abstractmethod
    def supports_job_source(self, source: JobSource | None) -> bool:
        """Return True only when the source explicitly authorizes this documented API."""


# Server-owned official API adapter registry.
# Stage 3 intentionally keeps this registry empty until documented ATS partner credentials exist.
_OFFICIAL_API_ADAPTER_REGISTRY: dict[str, OfficialApiSubmissionAdapter] = {}


def list_registered_official_api_adapters() -> frozenset[str]:
    """Return names of registered official submission adapters (empty in Stage 3)."""
    return frozenset(_OFFICIAL_API_ADAPTER_REGISTRY.keys())


def get_authorized_official_api_adapter(
    source: JobSource | None,
) -> OfficialApiSubmissionAdapter | None:
    """Return an official submission adapter only if registered AND explicitly authorized by source."""
    if source is None or not isinstance(source.config, dict):
        return None
    if source.config.get("submission_api_authorized") is not True:
        return None
    connector = (source.connector_type or "").strip().lower()
    adapter = _OFFICIAL_API_ADAPTER_REGISTRY.get(connector)
    if adapter is None:
        return None
    if not adapter.supports_job_source(source):
        return None
    return adapter
