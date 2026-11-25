import uuid
from datetime import datetime
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field


class JobSourceCreateRequest(BaseModel):
    source_name: str = Field(..., min_length=2, max_length=160)
    connector_type: Literal["greenhouse", "lever", "career_site"]
    base_url: str | None = Field(default=None, max_length=500)
    config: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True
    rate_limit_config: dict[str, Any] = Field(default_factory=dict)


class JobSourceHealthResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    source_name: str
    connector_type: str
    enabled: bool
    health_status: str
    consecutive_failures: int
    last_error: str | None
    last_run_at: datetime | None


class DiscoveryTriggerRequest(BaseModel):
    query: str = Field(default="", max_length=200)
    location: str | None = Field(default=None, max_length=120)
    source_ids: list[uuid.UUID] | None = None
    max_pages: int = Field(default=3, ge=1, le=20)


class DiscoveryRunResponse(BaseModel):
    run_id: str
    sources_queried: int
    discovered_count: int
    created_count: int
    duplicate_count: int
    reposted_count: int
    expired_count: int
    quality_matches_count: int
    recommended_job_ids: list[str]
    errors: list[dict[str, Any]] = Field(default_factory=list)


class JobRequirementResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    role_category: str
    location: str | None = None
    remote_policy: str = "unknown"
    employment_type: str = "unknown"
    required_skills: list[str]
    preferred_skills: list[str]
    trainable_skills: list[str]
    responsibilities: list[str] = Field(default_factory=list)
    technical_stack: list[str]
    experience_min: float | None
    experience_max: float | None
    education_requirements: list[str]
    graduation_criteria: list[dict[str, Any]] = Field(default_factory=list)
    mandatory_qualifications: list[dict[str, Any]] = Field(default_factory=list)
    preferred_qualifications: list[dict[str, Any]] = Field(default_factory=list)
    application_instructions: list[dict[str, Any]] = Field(default_factory=list)
    evidence_items: dict[str, Any] = Field(default_factory=dict)
    extraction_mode: str = "deterministic"
    internship_eligible: bool | None
    fresher_eligible: bool | None
    uncertainties: list[str]
    prompt_injection_detected: bool
    extracted_at: datetime


class JobMatchResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    job_id: uuid.UUID
    profile_id: uuid.UUID
    score: float
    eligible: bool
    hard_constraints_passed: bool
    required_skill_coverage: float = 0.0
    preferred_skill_coverage: float = 0.0
    semantic_similarity: float | None = None
    matched_skills: list[str]
    missing_required_skills: list[str]
    preferred_gaps: list[str]
    evidence_mapping: list[dict[str, Any]] = Field(default_factory=list)
    missing_evidence: list[dict[str, Any]] = Field(default_factory=list)
    eligibility_blockers: list[str] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)
    confidence: float = 1.0
    score_breakdown: dict[str, Any]
    recommendation: str
    explanation: str
    scored_at: datetime


class JobIntelligenceResponse(BaseModel):
    job_id: uuid.UUID
    company: str
    role_title: str
    status: str
    requirements: JobRequirementResponse | None
    match: JobMatchResponse
    skill_coverage: dict[str, Any]
    score_disclaimer: str = "Estimated rubric alignment score; not a hiring probability."


class JobEvidenceResponse(BaseModel):
    job_id: uuid.UUID
    profile_id: uuid.UUID
    evidence_mapping: list[dict[str, Any]]
    missing_evidence: list[dict[str, Any]]
    confirmed_missing_skills: list[str]
    jd_evidence_items: dict[str, Any]


class JobEligibilityResponse(BaseModel):
    job_id: uuid.UUID
    eligible: bool
    hard_constraints_passed: bool
    eligibility_blockers: list[str]
    uncertainties: list[str]
    confidence: float
    recommendation: str
    explanation: str


class JobAnalysisHistoryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    job_id: uuid.UUID
    profile_id: uuid.UUID
    run_id: str | None
    extraction_mode: str
    model_provider: str | None
    overall_score: float
    eligible: bool
    confidence: float
    snapshot: dict[str, Any]
    created_at: datetime


class WorkflowRunResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    thread_id: str
    workflow_type: str
    current_stage: str
    status: str
    checkpoint_metadata: dict[str, Any]
    started_at: datetime
    updated_at: datetime


class JobSummaryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    source_id: uuid.UUID
    source_job_id: str | None
    canonical_url: str
    company: str
    role_title: str
    location: str | None
    remote_policy: str
    is_remote: bool | None
    employment_type: str
    salary_min: float | None
    salary_max: float | None
    salary_currency: str | None
    salary_text: str | None
    application_url: str | None
    posted_at: datetime | None
    first_seen_at: datetime
    last_seen_at: datetime
    repost_count: int
    status: str
    requirements: JobRequirementResponse | None = None
    match: JobMatchResponse | None = None


class JobDetailResponse(JobSummaryResponse):
    raw_jd: str
    updated_at_source: datetime | None
    expired_at: datetime | None
    source_provenance: dict[str, Any]


class JobStatusUpdateRequest(BaseModel):
    status: Literal["active", "expired", "unavailable"]
    reason: str | None = Field(default=None, max_length=250)
