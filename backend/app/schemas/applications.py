from datetime import datetime
import hashlib
import json
import uuid
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator

ApplicationMode = Literal["official_api", "human_approved_browser", "manual"]

ApplicationStatus = Literal[
    "ELIGIBLE",
    "MATCHED",
    "RESUME_READY",
    "NEEDS_USER_INPUT",
    "AWAITING_APPROVAL",
    "APPROVED",
    "SUBMISSION_PENDING",
    "SUBMITTED",
    "USER_REPORTED_SUBMITTED",
    "UNVERIFIED",
    "CONFIRMED",
    "INTERVIEW",
    "REJECTED",
    "WITHDRAWN",
    "EXPIRED",
    "FAILED",
    "DUPLICATE",
    "MANUAL_ACTION_REQUIRED",
]

ALLOWED_APPLICATION_TRANSITIONS: dict[str, frozenset[str]] = {
    "ELIGIBLE": frozenset({"MATCHED", "RESUME_READY", "EXPIRED", "DUPLICATE"}),
    "MATCHED": frozenset({"RESUME_READY", "EXPIRED", "DUPLICATE"}),
    "RESUME_READY": frozenset({"NEEDS_USER_INPUT", "AWAITING_APPROVAL", "EXPIRED", "DUPLICATE"}),
    "NEEDS_USER_INPUT": frozenset({"AWAITING_APPROVAL", "EXPIRED", "REJECTED"}),
    "AWAITING_APPROVAL": frozenset({"APPROVED", "REJECTED", "EXPIRED"}),
    "APPROVED": frozenset({"SUBMISSION_PENDING", "AWAITING_APPROVAL", "EXPIRED"}),
    "SUBMISSION_PENDING": frozenset(
        {"SUBMITTED", "UNVERIFIED", "MANUAL_ACTION_REQUIRED", "FAILED"}
    ),
    "SUBMITTED": frozenset({"CONFIRMED", "UNVERIFIED", "FAILED"}),
    "USER_REPORTED_SUBMITTED": frozenset({"UNVERIFIED", "FAILED"}),
    "UNVERIFIED": frozenset({"CONFIRMED", "FAILED", "MANUAL_ACTION_REQUIRED"}),
    "MANUAL_ACTION_REQUIRED": frozenset({"USER_REPORTED_SUBMITTED", "UNVERIFIED", "FAILED", "WITHDRAWN"}),
    "CONFIRMED": frozenset({"INTERVIEW", "REJECTED", "WITHDRAWN"}),
    "INTERVIEW": frozenset({"REJECTED", "WITHDRAWN"}),
    "REJECTED": frozenset(),
    "WITHDRAWN": frozenset(),
    "EXPIRED": frozenset(),
    "FAILED": frozenset({"AWAITING_APPROVAL"}),
    "DUPLICATE": frozenset(),
}


def validate_application_transition(current_status: str, next_status: str) -> None:
    """Validate an application status transition against ALLOWED_APPLICATION_TRANSITIONS."""
    allowed = ALLOWED_APPLICATION_TRANSITIONS.get(current_status)
    if allowed is None or next_status not in allowed:
        raise ValueError(f"Invalid application status transition: {current_status} -> {next_status}")


def compute_answers_hash(answers: list[tuple[str, str]]) -> str:
    """Deterministic SHA-256 hash over sorted (question_key, answer_value) pairs."""
    normalized = sorted((k.strip(), v.strip()) for k, v in answers)
    payload = json.dumps(normalized, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def compute_approval_snapshot_hash(
    *,
    application_id: uuid.UUID | str,
    user_id: uuid.UUID | str,
    resume_id: uuid.UUID | str,
    resume_checksum: str,
    canonical_job_key: str,
    destination_url: str,
    answers_hash: str,
    application_mode: str,
) -> str:
    """Bind approval record to exact resume, answers, destination, and mode."""
    payload = json.dumps(
        {
            "application_id": str(application_id),
            "user_id": str(user_id),
            "resume_id": str(resume_id),
            "resume_checksum": resume_checksum.strip(),
            "canonical_job_key": canonical_job_key.strip(),
            "destination_url": destination_url.strip(),
            "answers_hash": answers_hash.strip(),
            "application_mode": application_mode.strip(),
        },
        sort_keys=True,
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class ApplicationPrepareRequest(BaseModel):
    job_id: uuid.UUID
    resume_id: uuid.UUID
    preferred_mode: ApplicationMode | None = None


class ApplicationAnswerInput(BaseModel):
    question_key: str = Field(..., min_length=1, max_length=160)
    question: str = Field(..., min_length=1, max_length=1000)
    answer: str = Field(..., min_length=1, max_length=4000)
    is_sensitive: bool = False
    evidence_source: str | None = Field(default=None, max_length=160)

    @model_validator(mode="after")
    def validate_non_empty_answer(self) -> "ApplicationAnswerInput":
        if not self.answer.strip():
            raise ValueError("Answer text cannot be blank")
        return self


class ApplicationDecisionRequest(BaseModel):
    notes: str | None = Field(default=None, max_length=1000)


class ApplicationAnswerResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    question_key: str
    question: str
    is_sensitive: bool
    answer_preview: str | None = None
    evidence_source: str | None = None
    user_verified: bool
    created_at: datetime


class ApprovalRecordResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    application_id: uuid.UUID
    decision: Literal["approved", "rejected"]
    actor: str
    resume_id: uuid.UUID
    resume_checksum: str
    destination_url: str
    application_mode: ApplicationMode
    approval_snapshot_hash: str
    notes: str | None = None
    decided_at: datetime
    expires_at: datetime


class ApplicationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    user_id: uuid.UUID | None
    job_id: uuid.UUID
    resume_id: uuid.UUID
    canonical_job_key: str
    idempotency_key: str
    application_mode: ApplicationMode
    status: ApplicationStatus
    destination_url: str
    thread_id: str
    external_ref_id: str | None = None
    confirmation_evidence: dict[str, Any] = Field(default_factory=dict)
    submitted_at: datetime | None = None
    confirmed_at: datetime | None = None
    next_followup_at: datetime | None = None
    error_message: str | None = None
    created_at: datetime
    updated_at: datetime


AnswerVerificationState = Literal["verified", "unverified", "missing"]
FormFieldType = Literal[
    "text",
    "textarea",
    "select",
    "radio",
    "checkbox",
    "number",
    "date",
    "file",
    "url",
    "email",
    "phone",
]
SensitiveCategory = Literal[
    "work_authorization",
    "sponsorship",
    "demographic",
    "legal",
    "salary",
    "experience",
    "identity",
]


class ExtractedFormQuestion(BaseModel):
    question_key: str = Field(..., min_length=1, max_length=160)
    question: str = Field(..., min_length=1, max_length=1000)
    field_type: FormFieldType = "text"
    required: bool = True
    is_sensitive: bool = False
    sensitive_category: SensitiveCategory | None = None
    options: list[str] = Field(default_factory=list)


class MappedApplicationAnswer(BaseModel):
    question_key: str
    question: str
    field_type: FormFieldType
    required: bool
    is_sensitive: bool
    sensitive_category: SensitiveCategory | None = None
    verification_state: AnswerVerificationState
    proposed_answer: str | None = None
    evidence_source: str | None = None
    requires_user_confirmation: bool = False
    reason: str | None = None


class FormMappingResult(BaseModel):
    questions: list[ExtractedFormQuestion] = Field(default_factory=list)
    mapped_answers: list[MappedApplicationAnswer] = Field(default_factory=list)
    missing_required_keys: list[str] = Field(default_factory=list)
    missing_optional_keys: list[str] = Field(default_factory=list)
    unverified_keys: list[str] = Field(default_factory=list)
    sensitive_keys_requiring_confirmation: list[str] = Field(default_factory=list)
    ready_for_approval: bool = False


class PolicyDecision(BaseModel):
    allowed: bool
    selected_mode: ApplicationMode | None = None
    submission_paused: bool = False
    blocker_reasons: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    canonical_job_key: str
    idempotency_key: str
    daily_used: int = 0
    daily_limit: int = 15


class ApplicationPreparationResult(BaseModel):
    application_id: uuid.UUID
    status: ApplicationStatus
    policy: PolicyDecision
    form_mapping: FormMappingResult


BrowserExecutionMode = Literal["headless", "visible_handoff"]
FieldFillStatus = Literal["filled", "skipped_unverified", "skipped_missing", "unsupported"]


class DetectedFormField(BaseModel):
    selector: str = Field(..., min_length=1, max_length=500)
    question_key: str = Field(..., min_length=1, max_length=160)
    label: str = Field(..., min_length=1, max_length=1000)
    field_type: FormFieldType = "text"
    required: bool = True
    options: list[str] = Field(default_factory=list)


class FieldFillOutcome(BaseModel):
    question_key: str
    status: FieldFillStatus
    reason: str | None = None


class ExecutionOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    browser_mode: BrowserExecutionMode = "visible_handoff"
    capture_artifacts: bool = False
    timeout_seconds: int | None = Field(default=None, ge=10, le=600)


class PendingSubmissionInfo(BaseModel):
    """Exact intercepted navigation submission held awaiting explicit user confirmation."""

    model_config = ConfigDict(extra="forbid")

    pending_submission_id: uuid.UUID
    target_url: str
    http_method: Literal["GET", "POST"]
    payload_sha256: str = Field(..., min_length=64, max_length=64)
    detected_at: datetime
    expires_at: datetime


class ManualSubmitAuthorizationRequest(BaseModel):
    """Client request schema to confirm and release a single intercepted pending submission."""

    model_config = ConfigDict(extra="forbid")

    handoff_session_id: uuid.UUID
    pending_submission_id: uuid.UUID
    expected_form_action: str = Field(..., min_length=1, max_length=1000)
    expected_http_method: Literal["GET", "POST"]
    expected_payload_sha256: str = Field(..., min_length=64, max_length=64, pattern=r"^[0-9a-fA-F]{64}$")


class ManualSubmitAuthorizationResponse(BaseModel):
    """Safe API response schema exposing authorization status without raw token credentials."""

    model_config = ConfigDict(extra="forbid")

    authorization_id: uuid.UUID
    application_id: uuid.UUID
    handoff_session_id: uuid.UUID
    destination_url: str
    form_action: str
    http_method: Literal["GET", "POST"]
    issued_at: datetime
    expires_at: datetime
    consumed: bool = False


class HandoffSessionInfo(BaseModel):
    handoff_session_id: uuid.UUID
    application_id: uuid.UUID
    status: Literal["active", "closed", "expired", "crashed"]
    created_at: datetime
    expires_at: datetime
    manual_submit_armed: bool = False
    pending_submission: PendingSubmissionInfo | None = None


class ExecutionResult(BaseModel):
    application_id: uuid.UUID
    status: ApplicationStatus
    adapter_name: str
    mode: ApplicationMode
    browser_mode: BrowserExecutionMode | None = None
    stopped_before_submit: bool = True
    blocked_submit_attempts: int = 0
    restriction_detected: str | None = None
    detected_form_action: str | None = None
    detected_http_method: Literal["GET", "POST"] | None = None
    fields_filled: list[FieldFillOutcome] = Field(default_factory=list)
    unsupported_fields: list[str] = Field(default_factory=list)
    resume_attached: bool = False
    resume_checksum: str | None = None
    os_firewall_enforced: bool = False
    artifact_ids: list[str] = Field(default_factory=list)
    handoff_session: HandoffSessionInfo | None = None
    error_message: str | None = None


class ApplicationAnswersSubmitRequest(BaseModel):
    """Client request to submit or verify application answers (user_id derived server-side only)."""

    model_config = ConfigDict(extra="forbid")

    answers: list[ApplicationAnswerInput] = Field(..., min_length=1, max_length=100)


class ApplicationExecuteRequest(BaseModel):
    """Client request to execute an approved application (forbids caller-supplied identity/tokens)."""

    model_config = ConfigDict(extra="forbid")

    options: ExecutionOptions = Field(default_factory=ExecutionOptions)


class ApplicationSubmitOutcomeRequest(BaseModel):
    """Client report after manual browser interaction; never grants CONFIRMED status."""

    model_config = ConfigDict(extra="forbid")

    outcome: Literal["submitted_in_browser", "failed", "interrupted"]
    handoff_session_id: uuid.UUID | None = None
    notes: str | None = Field(default=None, max_length=1000)


class ApplicationReconcileRequest(BaseModel):
    """Client/webhook payload submitted for server-side ConfirmationVerifier validation."""

    model_config = ConfigDict(extra="forbid")

    confirmation_evidence: dict[str, Any]


class ApplicationDetailResponse(BaseModel):
    """Owner-scoped application detail with PII-masked sensitive answer previews and approval history."""

    model_config = ConfigDict(extra="forbid")

    application: ApplicationResponse
    answers: list[ApplicationAnswerResponse] = Field(default_factory=list)
    approval_records: list[ApprovalRecordResponse] = Field(default_factory=list)
    active_handoff_sessions: list[HandoffSessionInfo] = Field(default_factory=list)
    execution_history: list[dict[str, Any]] = Field(default_factory=list)
