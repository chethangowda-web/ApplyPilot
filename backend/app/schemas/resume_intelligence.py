import uuid
from datetime import date, datetime
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field


class AttributedTextItem(BaseModel):
    text: str = Field(..., min_length=1, max_length=600)
    evidence_ids: list[str] = Field(default_factory=list)
    claim_status: Literal["supported_fact", "rewritten_fact"] = "rewritten_fact"


class TailoredSkillGroup(BaseModel):
    category: str = Field(..., min_length=1, max_length=80)
    skills: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)


class TailoredExperienceItem(BaseModel):
    experience_id: str
    company: str
    role: str
    location: str | None = None
    start_date: str
    end_date: str | None = None
    is_current: bool = False
    bullets: list[AttributedTextItem] = Field(default_factory=list)


class TailoredProjectItem(BaseModel):
    project_id: str
    name: str
    summary: str
    technologies: list[str] = Field(default_factory=list)
    bullets: list[AttributedTextItem] = Field(default_factory=list)
    repo_url: str | None = None
    live_url: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)


class TailoredEducationItem(BaseModel):
    evidence_id: str
    institution: str
    degree: str
    field_of_study: str | None = None
    start_year: str | None = None
    end_year: str | None = None
    grade_or_cgpa: str | None = None


class TailoredCertificationItem(BaseModel):
    certification_id: str
    name: str
    issuer: str
    issued_date: str | None = None
    credential_url: str | None = None


class TailoredResumeContent(BaseModel):
    full_name: str
    email: str
    phone: str | None = None
    location: str | None = None
    github_url: str | None = None
    linkedin_url: str | None = None
    portfolio_url: str | None = None
    summary: AttributedTextItem
    section_order: list[str] = Field(
        default_factory=lambda: [
            "summary",
            "skills",
            "experience",
            "projects",
            "education",
            "certifications",
            "achievements",
        ]
    )
    skill_groups: list[TailoredSkillGroup] = Field(default_factory=list)
    experiences: list[TailoredExperienceItem] = Field(default_factory=list)
    projects: list[TailoredProjectItem] = Field(default_factory=list)
    education: list[TailoredEducationItem] = Field(default_factory=list)
    certifications: list[TailoredCertificationItem] = Field(default_factory=list)
    achievements: list[AttributedTextItem] = Field(default_factory=list)


class SkillReorderEntry(BaseModel):
    skill_name: str
    original_rank: int
    tailored_rank: int
    reason: str
    evidence_id: str


class ProjectRewriteEntry(BaseModel):
    project_id: str
    name: str
    before_summary: str
    after_summary: str
    before_bullets: list[str] = Field(default_factory=list)
    after_bullets: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)


class ExperienceRewriteEntry(BaseModel):
    experience_id: str
    company: str
    role: str
    before_bullets: list[str] = Field(default_factory=list)
    after_bullets: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)


class KeywordEmphasisEntry(BaseModel):
    keyword: str
    section: str
    evidence_id: str


class SectionChangeEntry(BaseModel):
    section: str
    action: Literal["reordered", "deprioritized", "prioritized", "omitted"]
    reason: str
    evidence_ids: list[str] = Field(default_factory=list)


class TailoringDiff(BaseModel):
    skills_reordered: list[SkillReorderEntry] = Field(default_factory=list)
    project_rewrites: list[ProjectRewriteEntry] = Field(default_factory=list)
    experience_rewrites: list[ExperienceRewriteEntry] = Field(default_factory=list)
    keywords_emphasized: list[KeywordEmphasisEntry] = Field(default_factory=list)
    sections_reordered_or_deprioritized: list[SectionChangeEntry] = Field(default_factory=list)


ClaimClassification = Literal["supported_fact", "rewritten_fact", "unsupported_claim"]


class ValidatedClaim(BaseModel):
    claim_id: str
    category: Literal[
        "summary",
        "skill",
        "project_tech",
        "project_outcome",
        "metric",
        "experience",
        "education",
        "certification",
        "achievement",
    ]
    entity_id: str | None = None
    claim_text: str
    classification: ClaimClassification
    matched_evidence_ids: list[str] = Field(default_factory=list)
    violation_reason: str | None = None


class FactualValidationReport(BaseModel):
    claims: list[ValidatedClaim] = Field(default_factory=list)
    supported_count: int = 0
    rewritten_count: int = 0
    unsupported_count: int = 0
    unsupported_claims: list[ValidatedClaim] = Field(default_factory=list)
    deterministic_passed: bool = False
    llm_crosscheck_passed: bool | None = None
    llm_crosscheck_notes: list[str] = Field(default_factory=list)
    validation_passed: bool = False


class ResumeEvaluationReport(BaseModel):
    required_skill_coverage: float = Field(..., ge=0.0, le=1.0)
    preferred_skill_coverage: float = Field(..., ge=0.0, le=1.0)
    relevant_evidence_coverage: float = Field(..., ge=0.0, le=1.0)
    keyword_coverage: float = Field(..., ge=0.0, le=1.0)
    factual_consistency_score: float = Field(..., ge=0.0, le=1.0)
    readability_score: float = Field(..., ge=0.0, le=1.0)
    pdf_parsing_quality_score: float = Field(..., ge=0.0, le=1.0)
    disclaimer: str = (
        "Alignment and PDF quality metrics are deterministic/heuristic estimates, "
        "not a guaranteed ATS score or hiring probability."
    )


class TailorJobRequest(BaseModel):
    use_llm: bool = True
    force_regenerate: bool = False


class RegenerateResumeRequest(BaseModel):
    use_llm: bool = True


class ApprovalDecisionRequest(BaseModel):
    notes: str | None = Field(default=None, max_length=1000)


class OutboxJobStatusResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    job_id: uuid.UUID
    profile_id: uuid.UUID
    trigger_mode: str
    evidence_fingerprint: str
    quota_date: date | None
    status: str
    attempt_count: int
    max_attempts: int
    thread_id: str
    resume_id: uuid.UUID | None
    error_message: str | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None


class TailorEnqueueResponse(BaseModel):
    accepted: bool
    created: bool
    idempotency_hit: bool = False
    status: str
    reason: str
    job_id: uuid.UUID
    generation_job_id: uuid.UUID | None = None
    existing_resume_id: uuid.UUID | None = None
    evidence_fingerprint: str | None = None
    dispatched_to_broker: bool = False
    outbox_job: OutboxJobStatusResponse | None = None


class ApprovalDecisionResponse(BaseModel):
    mapping_id: uuid.UUID
    resume_id: uuid.UUID
    job_id: uuid.UUID
    version_number: int
    status: str
    approval_status: str
    approval_notes: str | None = None
    approved_at: datetime | None = None
    rejected_at: datetime | None = None
    idempotent: bool = False


class JobResumeHistoryResponse(BaseModel):
    job_id: uuid.UUID
    versions: list["ResumeGenerationHistoryResponse"] = Field(default_factory=list)
    outbox_jobs: list[OutboxJobStatusResponse] = Field(default_factory=list)


class TailoredResumeSummaryResponse(BaseModel):
    resume_id: uuid.UUID
    mapping_id: uuid.UUID
    job_id: uuid.UUID
    master_resume_id: uuid.UUID | None
    version_number: int
    version_label: str
    is_active: bool
    status: str
    approval_status: str
    template_version: str
    evidence_fingerprint: str
    validation_passed: bool
    evaluation_report: dict[str, Any]
    created_at: datetime


class TailoredResumeDetailResponse(TailoredResumeSummaryResponse):
    run_id: str
    trigger_mode: str
    sha256_checksum: str | None
    file_size_bytes: int | None
    ats_report: dict[str, Any]
    parsed_metadata: dict[str, Any]
    validation_report: dict[str, Any]
    tailoring_diff: dict[str, Any]
    token_usage: dict[str, Any]
    model_provider: str | None
    approval_notes: str | None
    approved_at: datetime | None
    rejected_at: datetime | None


class TailoringDiffResponse(BaseModel):
    resume_id: uuid.UUID
    mapping_id: uuid.UUID
    job_id: uuid.UUID
    version_number: int
    evidence_fingerprint: str
    tailoring_diff: dict[str, Any]


class FactualValidationReportResponse(BaseModel):
    resume_id: uuid.UUID
    mapping_id: uuid.UUID | None = None
    job_id: uuid.UUID | None = None
    validation_passed: bool
    validation_report: dict[str, Any]
    ats_report: dict[str, Any]


class ResumeGenerationHistoryResponse(BaseModel):
    mapping_id: uuid.UUID
    resume_id: uuid.UUID | None
    job_id: uuid.UUID
    master_resume_id: uuid.UUID | None
    generation_job_id: uuid.UUID | None
    run_id: str
    trigger_mode: str
    version_number: int
    is_active: bool
    evidence_fingerprint: str
    status: str
    approval_status: str
    approval_notes: str | None
    approved_at: datetime | None
    rejected_at: datetime | None
    model_provider: str | None
    token_usage: dict[str, Any]
    error_message: str | None
    created_at: datetime
