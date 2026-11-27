from datetime import datetime
import uuid
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field

FollowupType = Literal["initial_followup", "second_followup", "interview_thank_you", "custom"]
FollowupStatus = Literal["pending", "completed", "snoozed", "cancelled"]
InterviewRoundType = Literal["technical", "dsa", "backend_swe", "ai_ml", "resume_deep_dive", "behavioral", "mixed"]
InterviewDifficulty = Literal["easy", "medium", "hard"]
InterviewMemoryType = Literal[
    "weak_topic",
    "recurring_mistake",
    "strong_area",
    "behavioral_example",
    "asked_question",
    "feedback_note",
]


class FollowupCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    application_id: uuid.UUID
    reminder_type: FollowupType = "initial_followup"
    interval_days: int = Field(default=7, ge=1, le=60)
    due_at: datetime | None = None
    draft_subject: str | None = Field(default=None, max_length=300)
    draft_body: str | None = Field(default=None, max_length=5000)


class FollowupUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    draft_subject: str | None = Field(default=None, max_length=300)
    draft_body: str | None = Field(default=None, max_length=5000)
    snooze_days: int | None = Field(default=None, ge=1, le=30)
    due_at: datetime | None = None


class FollowupResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    application_id: uuid.UUID
    reminder_type: FollowupType
    status: FollowupStatus
    due_at: datetime
    snoozed_until: datetime | None = None
    completed_at: datetime | None = None
    interval_days: int
    draft_subject: str
    draft_body: str
    recruiter_context: dict[str, Any] = Field(default_factory=dict)
    is_overdue: bool = False
    created_at: datetime
    updated_at: datetime


class InterviewKitGenerateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    application_id: uuid.UUID
    module_key: str | None = Field(default=None, max_length=64)


class InterviewKitProgressRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    completed_topics: list[str] | None = None
    revision_notes: dict[str, str] | None = None
    user_feedback: str | None = Field(default=None, max_length=2000)


class InterviewKitResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    application_id: uuid.UUID
    job_id: uuid.UUID
    resume_id: uuid.UUID
    resume_checksum: str
    version: int
    is_active: bool
    modules_json: dict[str, Any] = Field(default_factory=dict)
    progress_json: dict[str, Any] = Field(default_factory=dict)
    user_feedback: str | None = None
    readiness_estimate: float
    created_at: datetime
    updated_at: datetime


class MockSessionStartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kit_id: uuid.UUID
    round_type: InterviewRoundType = "mixed"
    difficulty: InterviewDifficulty = "medium"


class MockAnswerSubmitRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answer: str = Field(..., min_length=1, max_length=8000)


class InterviewAttemptResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    session_id: uuid.UUID
    question_index: int
    module_key: str
    difficulty: str
    question_text: str
    expected_structure: list[str] = Field(default_factory=list)
    hints: list[str] = Field(default_factory=list)
    candidate_answer: str
    feedback_json: dict[str, Any] = Field(default_factory=dict)
    score: float
    weak_topics: list[str] = Field(default_factory=list)
    strong_topics: list[str] = Field(default_factory=list)
    attempted_at: datetime


class InterviewSessionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    kit_id: uuid.UUID
    round_type: InterviewRoundType
    difficulty: InterviewDifficulty
    status: Literal["active", "completed"]
    current_question_index: int
    current_question_json: dict[str, Any] = Field(default_factory=dict)
    report_json: dict[str, Any] = Field(default_factory=dict)
    overall_score: float | None = None
    attempts: list[InterviewAttemptResponse] = Field(default_factory=list)
    started_at: datetime
    completed_at: datetime | None = None


class InterviewMemoryCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    memory_type: InterviewMemoryType
    topic: str = Field(..., min_length=1, max_length=160)
    summary: str = Field(..., min_length=1, max_length=2000)
    confidence: float = Field(default=0.8, ge=0.0, le=1.0)


class InterviewMemoryUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    topic: str | None = Field(default=None, min_length=1, max_length=160)
    summary: str | None = Field(default=None, min_length=1, max_length=2000)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)


class InterviewMemoryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    memory_type: InterviewMemoryType
    topic: str
    summary: str
    source: str
    confidence: float
    evidence_refs: list[str] = Field(default_factory=list)
    attempt_count: int
    last_score: float | None = None
    created_at: datetime
    updated_at: datetime
