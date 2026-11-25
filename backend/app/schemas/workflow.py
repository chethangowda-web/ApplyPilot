from datetime import datetime
from enum import Enum
from typing import Any, Literal, TypedDict
from pydantic import BaseModel, Field


class ApplicationStatus(str, Enum):
    """Explicit application lifecycle states (SUBMITTED and CONFIRMED are strictly distinct)."""

    DISCOVERED = "DISCOVERED"
    ANALYZED = "ANALYZED"
    ELIGIBLE = "ELIGIBLE"
    MATCHED = "MATCHED"
    RESUME_READY = "RESUME_READY"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    APPROVED = "APPROVED"
    SUBMISSION_PENDING = "SUBMISSION_PENDING"
    SUBMITTED = "SUBMITTED"
    CONFIRMED = "CONFIRMED"
    INTERVIEW = "INTERVIEW"
    REJECTED = "REJECTED"
    WITHDRAWN = "WITHDRAWN"
    EXPIRED = "EXPIRED"
    FAILED = "FAILED"
    NEEDS_USER_INPUT = "NEEDS_USER_INPUT"
    DUPLICATE = "DUPLICATE"
    MANUAL_ACTION_REQUIRED = "MANUAL_ACTION_REQUIRED"


class NormalizedJob(BaseModel):
    """Canonical normalized job record produced by Discovery connectors."""

    source: str
    source_job_id: str | None = None
    external_job_id: str | None = None
    canonical_url: str
    company: str
    job_title: str
    description: str = ""
    location: str | None = None
    remote_policy: str | None = None  # remote | hybrid | onsite | unknown
    is_remote: bool | None = None
    employment_type: str | None = None  # internship | full_time | contract | unknown
    experience_min: float | None = None
    experience_max: float | None = None
    required_skills: list[str] = Field(default_factory=list)
    preferred_skills: list[str] = Field(default_factory=list)
    education_requirements: list[str] = Field(default_factory=list)
    salary_min: float | None = None
    salary_max: float | None = None
    salary_currency: str | None = None
    salary_text: str | None = None
    posted_at: datetime | None = None
    updated_at: datetime | None = None
    discovered_at: datetime
    application_url: str | None = None
    status: str = "active"  # active | expired | unavailable
    raw_metadata: dict[str, Any] = Field(default_factory=dict)


class JobRequirements(BaseModel):
    """Structured JD intelligence extracted before eligibility and match scoring."""

    job_title: str
    company: str
    location: str | None = None
    role_category: str
    required_skills: list[str] = Field(default_factory=list)
    preferred_skills: list[str] = Field(default_factory=list)
    trainable_skills: list[str] = Field(default_factory=list)
    responsibilities: list[str] = Field(default_factory=list)
    technical_stack: list[str] = Field(default_factory=list)
    experience_min: float | None = None
    experience_max: float | None = None
    education_requirements: list[str] = Field(default_factory=list)
    internship_eligible: bool | None = None
    fresher_eligible: bool | None = None
    sponsorship_required: bool | None = None
    job_type: str | None = None
    remote_policy: str | None = None
    application_url: str | None = None
    posting_status: str = "active"
    uncertainties: list[str] = Field(default_factory=list)
    prompt_injection_detected: bool = False


class ApplyPilotState(TypedDict, total=False):
    """Shared LangGraph workflow state storing record references rather than large binaries."""

    # Execution
    run_id: str
    user_id: str
    workflow_status: str

    # Discovery
    search_preferences: dict
    discovered_job_ids: list[str]
    discovery_summary: dict
    current_job_id: str

    # Analysis
    job_requirements: dict
    eligibility_result: dict
    match_score: float

    # Resume
    master_profile_id: str
    resume_version_id: str
    resume_validation: dict

    # Application
    application_id: str
    application_mode: Literal["official_api", "human_approved_browser", "manual"]
    application_answers: dict
    approval_status: str
    submission_result: dict

    # Interview
    interview_kit_id: str

    # Errors
    retry_count: int
    errors: list[dict]
