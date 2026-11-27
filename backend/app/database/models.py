import uuid
from datetime import date, datetime, timezone
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    event,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.database.base import Base
from app.database.types import EmbeddingVector, JSONVariant, StringArrayVariant


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True, nullable=False)
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Kolkata", nullable=False)
    account_preferences: Mapped[dict] = mapped_column(JSONVariant, default=dict, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )

    profile: Mapped["CandidateProfile"] = relationship(
        back_populates="user", uselist=False, cascade="all, delete-orphan"
    )


class CandidateProfile(Base):
    __tablename__ = "candidate_profiles"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    full_name: Mapped[str] = mapped_column(String(200), nullable=False)
    phone: Mapped[str] = mapped_column(String(50), nullable=True)
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    location: Mapped[str] = mapped_column(String(200), nullable=True)
    github_url: Mapped[str] = mapped_column(String(500), nullable=True)
    linkedin_url: Mapped[str] = mapped_column(String(500), nullable=True)
    portfolio_url: Mapped[str] = mapped_column(String(500), nullable=True)
    summary: Mapped[str] = mapped_column(Text, nullable=True)
    education: Mapped[list] = mapped_column(JSONVariant, default=list, nullable=False)
    achievements: Mapped[list] = mapped_column(JSONVariant, default=list, nullable=False)
    confirmed_preferences: Mapped[dict] = mapped_column(JSONVariant, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )

    user: Mapped["User"] = relationship(back_populates="profile")
    skills: Mapped[list["CandidateSkill"]] = relationship(
        back_populates="profile", cascade="all, delete-orphan"
    )
    projects: Mapped[list["Project"]] = relationship(
        back_populates="profile", cascade="all, delete-orphan"
    )
    experiences: Mapped[list["Experience"]] = relationship(
        back_populates="profile", cascade="all, delete-orphan"
    )
    certifications: Mapped[list["Certification"]] = relationship(
        back_populates="profile", cascade="all, delete-orphan"
    )
    resumes: Mapped[list["Resume"]] = relationship(
        back_populates="profile", cascade="all, delete-orphan"
    )


class CandidateSkill(Base):
    __tablename__ = "candidate_skills"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    profile_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("candidate_profiles.id", ondelete="CASCADE"), index=True, nullable=False
    )
    skill_name: Mapped[str] = mapped_column(String(120), nullable=False)
    category: Mapped[str] = mapped_column(String(80), default="General", nullable=False)
    proficiency: Mapped[str] = mapped_column(String(64), nullable=True)
    evidence_ref: Mapped[str] = mapped_column(String(500), nullable=True)
    verified: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    embedding: Mapped[list] = mapped_column(EmbeddingVector(768), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)

    profile: Mapped["CandidateProfile"] = relationship(back_populates="skills")


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    profile_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("candidate_profiles.id", ondelete="CASCADE"), index=True, nullable=False
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    technologies: Mapped[list[str]] = mapped_column(StringArrayVariant, default=list, nullable=False)
    implemented_features: Mapped[list] = mapped_column(JSONVariant, default=list, nullable=False)
    planned_features: Mapped[list] = mapped_column(JSONVariant, default=list, nullable=False)
    verified_metrics: Mapped[list] = mapped_column(JSONVariant, default=list, nullable=False)
    repo_url: Mapped[str] = mapped_column(String(500), nullable=True)
    live_url: Mapped[str] = mapped_column(String(500), nullable=True)
    start_date: Mapped[str] = mapped_column(String(40), nullable=True)
    end_date: Mapped[str] = mapped_column(String(40), nullable=True)
    embedding: Mapped[list] = mapped_column(EmbeddingVector(768), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)

    profile: Mapped["CandidateProfile"] = relationship(back_populates="projects")


class Experience(Base):
    __tablename__ = "experiences"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    profile_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("candidate_profiles.id", ondelete="CASCADE"), index=True, nullable=False
    )
    company: Mapped[str] = mapped_column(String(200), nullable=False)
    role: Mapped[str] = mapped_column(String(200), nullable=False)
    location: Mapped[str] = mapped_column(String(200), nullable=True)
    start_date: Mapped[str] = mapped_column(String(40), nullable=False)
    end_date: Mapped[str] = mapped_column(String(40), nullable=True)
    is_current: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    verified_responsibilities: Mapped[list] = mapped_column(JSONVariant, default=list, nullable=False)
    embedding: Mapped[list] = mapped_column(EmbeddingVector(768), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)

    profile: Mapped["CandidateProfile"] = relationship(back_populates="experiences")


class Certification(Base):
    __tablename__ = "certifications"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    profile_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("candidate_profiles.id", ondelete="CASCADE"), index=True, nullable=False
    )
    name: Mapped[str] = mapped_column(String(250), nullable=False)
    issuer: Mapped[str] = mapped_column(String(200), nullable=False)
    issued_date: Mapped[str] = mapped_column(String(40), nullable=True)
    credential_url: Mapped[str] = mapped_column(String(500), nullable=True)
    verified: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    embedding: Mapped[list] = mapped_column(EmbeddingVector(768), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)

    profile: Mapped["CandidateProfile"] = relationship(back_populates="certifications")


class Resume(Base):
    __tablename__ = "resumes"
    __table_args__ = (
        CheckConstraint(
            "generation_status IN ('uploaded', 'completed', 'validation_failed', 'failed')",
            name="ck_resumes_generation_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    profile_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("candidate_profiles.id", ondelete="CASCADE"), index=True, nullable=False
    )
    master_resume_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("resumes.id", ondelete="SET NULL"), index=True, nullable=True
    )
    is_master: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str] = mapped_column(String(120), nullable=False)
    file_size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    version_label: Mapped[str] = mapped_column(String(120), nullable=False)
    docx_path: Mapped[str] = mapped_column(String(500), nullable=True)
    pdf_path: Mapped[str] = mapped_column(String(500), nullable=True)
    sha256_checksum: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    template_version: Mapped[str] = mapped_column(String(64), default="master-upload-v1", nullable=False)
    generation_status: Mapped[str] = mapped_column(String(32), default="uploaded", nullable=False)
    ats_report: Mapped[dict] = mapped_column(JSONVariant, default=dict, nullable=False)
    parsed_metadata: Mapped[dict] = mapped_column(JSONVariant, default=dict, nullable=False)
    validation_report: Mapped[dict] = mapped_column(JSONVariant, default=dict, nullable=False)
    evaluation_report: Mapped[dict] = mapped_column(JSONVariant, default=dict, nullable=False)
    structured_content: Mapped[dict] = mapped_column(JSONVariant, default=dict, nullable=False)
    validation_passed: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)

    profile: Mapped["CandidateProfile"] = relationship(back_populates="resumes")
    job_mappings: Mapped[list["ResumeJobMapping"]] = relationship(
        back_populates="resume",
        foreign_keys="ResumeJobMapping.resume_id",
    )


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), index=True, nullable=True
    )
    actor: Mapped[str] = mapped_column(String(120), nullable=False)
    action: Mapped[str] = mapped_column(String(120), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(80), nullable=False)
    resource_id: Mapped[str] = mapped_column(String(120), nullable=False)
    event_metadata: Mapped[dict] = mapped_column(JSONVariant, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class JobSource(Base):
    __tablename__ = "job_sources"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source_name: Mapped[str] = mapped_column(String(160), unique=True, nullable=False)
    connector_type: Mapped[str] = mapped_column(String(64), nullable=False)
    base_url: Mapped[str] = mapped_column(String(500), nullable=True)
    config: Mapped[dict] = mapped_column(JSONVariant, default=dict, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    rate_limit_config: Mapped[dict] = mapped_column(JSONVariant, default=dict, nullable=False)
    last_run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    health_status: Mapped[str] = mapped_column(String(32), default="healthy", nullable=False)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error: Mapped[str] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )

    jobs: Mapped[list["Job"]] = relationship(back_populates="source")


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        UniqueConstraint("source_id", "source_job_id", name="uq_jobs_source_external_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("job_sources.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    source_job_id: Mapped[str] = mapped_column(String(255), nullable=True)
    canonical_url: Mapped[str] = mapped_column(String(1000), unique=True, nullable=False)
    dedup_key: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    content_checksum: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    company: Mapped[str] = mapped_column(String(250), index=True, nullable=False)
    role_title: Mapped[str] = mapped_column(String(300), index=True, nullable=False)
    location: Mapped[str] = mapped_column(String(300), nullable=True)
    remote_policy: Mapped[str] = mapped_column(String(32), default="unknown", nullable=False)
    is_remote: Mapped[bool] = mapped_column(Boolean, nullable=True)
    employment_type: Mapped[str] = mapped_column(String(64), default="unknown", nullable=False)
    salary_min: Mapped[float] = mapped_column(Float, nullable=True)
    salary_max: Mapped[float] = mapped_column(Float, nullable=True)
    salary_currency: Mapped[str] = mapped_column(String(16), nullable=True)
    salary_text: Mapped[str] = mapped_column(String(160), nullable=True)
    raw_jd: Mapped[str] = mapped_column(Text, nullable=False)
    application_url: Mapped[str] = mapped_column(String(1000), nullable=True)
    posted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at_source: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    repost_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="active", index=True, nullable=False)
    expired_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    source_provenance: Mapped[dict] = mapped_column(JSONVariant, default=dict, nullable=False)
    jd_embedding: Mapped[list] = mapped_column(EmbeddingVector(768), nullable=True)

    source: Mapped["JobSource"] = relationship(back_populates="jobs")
    requirements: Mapped["JobRequirement"] = relationship(
        back_populates="job", uselist=False, cascade="all, delete-orphan"
    )
    matches: Mapped[list["JobMatch"]] = relationship(back_populates="job", cascade="all, delete-orphan")


class JobRequirement(Base):
    __tablename__ = "job_requirements"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    job_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("jobs.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    role_category: Mapped[str] = mapped_column(String(120), nullable=False)
    location: Mapped[str] = mapped_column(String(300), nullable=True)
    remote_policy: Mapped[str] = mapped_column(String(32), default="unknown", nullable=False)
    employment_type: Mapped[str] = mapped_column(String(64), default="unknown", nullable=False)
    required_skills: Mapped[list] = mapped_column(JSONVariant, default=list, nullable=False)
    preferred_skills: Mapped[list] = mapped_column(JSONVariant, default=list, nullable=False)
    trainable_skills: Mapped[list] = mapped_column(JSONVariant, default=list, nullable=False)
    responsibilities: Mapped[list] = mapped_column(JSONVariant, default=list, nullable=False)
    technical_stack: Mapped[list] = mapped_column(JSONVariant, default=list, nullable=False)
    experience_min: Mapped[float] = mapped_column(Float, nullable=True)
    experience_max: Mapped[float] = mapped_column(Float, nullable=True)
    education_requirements: Mapped[list] = mapped_column(JSONVariant, default=list, nullable=False)
    graduation_criteria: Mapped[list] = mapped_column(JSONVariant, default=list, nullable=False)
    mandatory_qualifications: Mapped[list] = mapped_column(JSONVariant, default=list, nullable=False)
    preferred_qualifications: Mapped[list] = mapped_column(JSONVariant, default=list, nullable=False)
    application_instructions: Mapped[list] = mapped_column(JSONVariant, default=list, nullable=False)
    evidence_items: Mapped[dict] = mapped_column(JSONVariant, default=dict, nullable=False)
    extraction_mode: Mapped[str] = mapped_column(String(48), default="deterministic", nullable=False)
    internship_eligible: Mapped[bool] = mapped_column(Boolean, nullable=True)
    fresher_eligible: Mapped[bool] = mapped_column(Boolean, nullable=True)
    sponsorship_required: Mapped[bool] = mapped_column(Boolean, nullable=True)
    uncertainties: Mapped[list] = mapped_column(JSONVariant, default=list, nullable=False)
    prompt_injection_detected: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    extracted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)

    job: Mapped["Job"] = relationship(back_populates="requirements")


class JobMatch(Base):
    __tablename__ = "job_matches"
    __table_args__ = (
        UniqueConstraint("job_id", "profile_id", name="uq_job_matches_job_profile"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    job_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("jobs.id", ondelete="CASCADE"), index=True, nullable=False
    )
    profile_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("candidate_profiles.id", ondelete="CASCADE"), index=True, nullable=False
    )
    score: Mapped[float] = mapped_column(Float, nullable=False)
    eligible: Mapped[bool] = mapped_column(Boolean, nullable=False)
    hard_constraints_passed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    required_skill_coverage: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    preferred_skill_coverage: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    semantic_similarity: Mapped[float] = mapped_column(Float, nullable=True)
    matched_skills: Mapped[list] = mapped_column(JSONVariant, default=list, nullable=False)
    missing_required_skills: Mapped[list] = mapped_column(JSONVariant, default=list, nullable=False)
    preferred_gaps: Mapped[list] = mapped_column(JSONVariant, default=list, nullable=False)
    evidence_mapping: Mapped[list] = mapped_column(JSONVariant, default=list, nullable=False)
    missing_evidence: Mapped[list] = mapped_column(JSONVariant, default=list, nullable=False)
    eligibility_blockers: Mapped[list] = mapped_column(JSONVariant, default=list, nullable=False)
    uncertainties: Mapped[list] = mapped_column(JSONVariant, default=list, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)
    score_breakdown: Mapped[dict] = mapped_column(JSONVariant, default=dict, nullable=False)
    recommendation: Mapped[str] = mapped_column(String(64), nullable=False)
    explanation: Mapped[str] = mapped_column(Text, nullable=False)
    scored_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)

    job: Mapped["Job"] = relationship(back_populates="matches")


class WorkflowRun(Base):
    __tablename__ = "workflow_runs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    thread_id: Mapped[str] = mapped_column(String(160), unique=True, nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    job_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("jobs.id", ondelete="SET NULL"), index=True, nullable=True
    )
    workflow_type: Mapped[str] = mapped_column(String(64), default="discovery", nullable=False)
    current_stage: Mapped[str] = mapped_column(String(80), default="started", nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="running", nullable=False)
    checkpoint_metadata: Mapped[dict] = mapped_column(JSONVariant, default=dict, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


class JobAnalysisHistory(Base):
    __tablename__ = "job_analysis_history"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    job_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("jobs.id", ondelete="CASCADE"), index=True, nullable=False
    )
    profile_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("candidate_profiles.id", ondelete="CASCADE"), index=True, nullable=False
    )
    run_id: Mapped[str] = mapped_column(String(160), nullable=True)
    extraction_mode: Mapped[str] = mapped_column(String(48), default="deterministic", nullable=False)
    model_provider: Mapped[str] = mapped_column(String(64), nullable=True)
    overall_score: Mapped[float] = mapped_column(Float, nullable=False)
    eligible: Mapped[bool] = mapped_column(Boolean, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)
    snapshot: Mapped[dict] = mapped_column(JSONVariant, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class AutoTailorDailyQuota(Base):
    __tablename__ = "auto_tailor_daily_quotas"
    __table_args__ = (
        CheckConstraint("reserved_count >= 0", name="ck_auto_tailor_quota_nonneg"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    quota_date: Mapped[date] = mapped_column(Date, primary_key=True)
    reserved_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


class ResumeGenerationJob(Base):
    __tablename__ = "resume_generation_jobs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued', 'running', 'completed', 'validation_failed', 'failed')",
            name="ck_resume_gen_jobs_status",
        ),
        CheckConstraint(
            "trigger_mode IN ('auto', 'manual', 'regenerate')",
            name="ck_resume_gen_jobs_trigger_mode",
        ),
        CheckConstraint(
            "attempt_count >= 0 AND max_attempts >= 1",
            name="ck_resume_gen_jobs_attempts",
        ),
        Index(
            "uq_resume_gen_jobs_active_idem",
            "idempotency_key",
            unique=True,
            postgresql_where=text(
                "idempotency_key IS NOT NULL AND status IN ('queued', 'running', 'completed')"
            ),
            sqlite_where=text(
                "idempotency_key IS NOT NULL AND status IN ('queued', 'running', 'completed')"
            ),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    profile_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("candidate_profiles.id", ondelete="CASCADE"), index=True, nullable=False
    )
    job_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("jobs.id", ondelete="CASCADE"), index=True, nullable=False
    )
    master_resume_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("resumes.id", ondelete="SET NULL"), nullable=True
    )
    trigger_mode: Mapped[str] = mapped_column(String(24), default="auto", nullable=False)
    evidence_fingerprint: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    quota_date: Mapped[date] = mapped_column(Date, nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True, nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    worker_id: Mapped[str] = mapped_column(String(120), nullable=True)
    lease_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True, nullable=True)
    last_heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    thread_id: Mapped[str] = mapped_column(String(160), nullable=False)
    resume_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("resumes.id", ondelete="SET NULL"), nullable=True
    )
    error_message: Mapped[str] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)


class ResumeJobMapping(Base):
    __tablename__ = "resume_job_mappings"
    __table_args__ = (
        UniqueConstraint("job_id", "profile_id", "version_number", name="uq_resume_job_mapping_version"),
        CheckConstraint(
            "status IN ('completed', 'validation_failed', 'failed')",
            name="ck_resume_job_mapping_status",
        ),
        CheckConstraint(
            "approval_status IN ('pending_review', 'approved', 'rejected')",
            name="ck_resume_job_mapping_approval_status",
        ),
        CheckConstraint(
            "approval_status != 'approved' OR status = 'completed'",
            name="ck_resume_job_mapping_approved_requires_completed",
        ),
        Index(
            "uq_resume_job_mapping_active_completed",
            "job_id",
            "profile_id",
            unique=True,
            postgresql_where=text("is_active = TRUE AND status = 'completed'"),
            sqlite_where=text("is_active = 1 AND status = 'completed'"),
        ),
        Index(
            "ix_resume_job_mapping_fp_lookup",
            "job_id",
            "profile_id",
            "evidence_fingerprint",
            "status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    resume_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("resumes.id", ondelete="SET NULL"), index=True, nullable=True
    )
    job_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("jobs.id", ondelete="CASCADE"), index=True, nullable=False
    )
    profile_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("candidate_profiles.id", ondelete="CASCADE"), index=True, nullable=False
    )
    master_resume_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("resumes.id", ondelete="SET NULL"), nullable=True
    )
    generation_job_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("resume_generation_jobs.id", ondelete="SET NULL"), unique=True, nullable=True
    )
    run_id: Mapped[str] = mapped_column(String(160), index=True, nullable=False)
    trigger_mode: Mapped[str] = mapped_column(String(24), default="manual", nullable=False)
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    evidence_fingerprint: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    approval_status: Mapped[str] = mapped_column(String(32), default="pending_review", nullable=False)
    approval_notes: Mapped[str] = mapped_column(Text, nullable=True)
    approved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    rejected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    tailoring_diff: Mapped[dict] = mapped_column(JSONVariant, default=dict, nullable=False)
    model_provider: Mapped[str] = mapped_column(String(64), nullable=True)
    token_usage: Mapped[dict] = mapped_column(JSONVariant, default=dict, nullable=False)
    error_message: Mapped[str] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)

    resume: Mapped["Resume"] = relationship(
        back_populates="job_mappings",
        foreign_keys=[resume_id],
    )


class Application(Base):
    __tablename__ = "applications"
    __table_args__ = (
        CheckConstraint(
            "application_mode IN ('official_api', 'human_approved_browser', 'manual')",
            name="ck_applications_mode",
        ),
        CheckConstraint(
            "status IN ("
            "'ELIGIBLE', 'MATCHED', 'RESUME_READY', 'NEEDS_USER_INPUT', 'AWAITING_APPROVAL', "
            "'APPROVED', 'SUBMISSION_PENDING', 'SUBMITTED', 'USER_REPORTED_SUBMITTED', "
            "'UNVERIFIED', 'CONFIRMED', 'INTERVIEW', 'REJECTED', 'WITHDRAWN', 'EXPIRED', "
            "'FAILED', 'DUPLICATE', 'MANUAL_ACTION_REQUIRED'"
            ")",
            name="ck_applications_status",
        ),
        Index(
            "uq_applications_active_user_canonical_job",
            "user_id",
            "canonical_job_key",
            unique=True,
            postgresql_where=text(
                "user_id IS NOT NULL AND deleted_at IS NULL AND status NOT IN ('FAILED', 'EXPIRED', 'REJECTED')"
            ),
            sqlite_where=text(
                "user_id IS NOT NULL AND deleted_at IS NULL AND status NOT IN ('FAILED', 'EXPIRED', 'REJECTED')"
            ),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), index=True, nullable=True
    )
    job_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("jobs.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    resume_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("resumes.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    canonical_job_key: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    application_mode: Mapped[str] = mapped_column(String(32), default="human_approved_browser", nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="RESUME_READY", index=True, nullable=False)
    destination_url: Mapped[str] = mapped_column(String(1000), nullable=False)
    thread_id: Mapped[str] = mapped_column(String(160), unique=True, nullable=False)
    external_ref_id: Mapped[str] = mapped_column(String(255), nullable=True)
    confirmation_evidence: Mapped[dict] = mapped_column(JSONVariant, default=dict, nullable=False)
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    confirmed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    next_followup_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    anonymized_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    error_message: Mapped[str] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )

    answers: Mapped[list["ApplicationAnswer"]] = relationship(
        back_populates="application", cascade="all, delete-orphan"
    )
    approval_records: Mapped[list["ApprovalRecord"]] = relationship(
        back_populates="application", order_by="ApprovalRecord.decided_at.desc()"
    )


class ApplicationAnswer(Base):
    __tablename__ = "application_answers"
    __table_args__ = (
        CheckConstraint(
            "(NOT is_sensitive) OR ("
            "answer_encrypted IS NOT NULL AND length(answer_encrypted) > 0 "
            "AND answer_plaintext IS NULL AND key_version IS NOT NULL"
            ")",
            name="ck_app_answers_sensitive_encrypted",
        ),
        CheckConstraint(
            "is_sensitive OR (answer_plaintext IS NOT NULL AND answer_encrypted IS NULL)",
            name="ck_app_answers_nonsensitive_plain",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    application_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("applications.id", ondelete="CASCADE"), index=True, nullable=False
    )
    question_key: Mapped[str] = mapped_column(String(160), nullable=False)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    is_sensitive: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    answer_encrypted: Mapped[str] = mapped_column(Text, nullable=True)
    answer_plaintext: Mapped[str] = mapped_column(Text, nullable=True)
    key_version: Mapped[str] = mapped_column(String(32), nullable=True)
    evidence_source: Mapped[str] = mapped_column(String(160), nullable=True)
    user_verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )

    application: Mapped["Application"] = relationship(back_populates="answers")


class ApprovalRecord(Base):
    __tablename__ = "approval_records"
    __table_args__ = (
        CheckConstraint("decision IN ('approved', 'rejected')", name="ck_approval_records_decision"),
        CheckConstraint(
            "application_mode IN ('official_api', 'human_approved_browser', 'manual')",
            name="ck_approval_records_mode",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    application_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("applications.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    decision: Mapped[str] = mapped_column(String(24), nullable=False)
    actor: Mapped[str] = mapped_column(String(120), nullable=False)
    resume_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    resume_checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    destination_url: Mapped[str] = mapped_column(String(1000), nullable=False)
    application_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    answers_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    approval_snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    notes: Mapped[str] = mapped_column(Text, nullable=True)
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    application: Mapped["Application"] = relationship(back_populates="approval_records")


class OAuthConnection(Base):
    __tablename__ = "oauth_connections"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "provider",
            "mailbox_email",
            name="uq_oauth_connections_user_provider_mailbox",
        ),
        CheckConstraint(
            "status IN ('connected', 'revoked', 'expired', 'disconnected', 'error')",
            name="ck_oauth_connections_status",
        ),
        CheckConstraint(
            "last_sync_status IS NULL OR last_sync_status IN ('idle', 'running', 'succeeded', 'failed')",
            name="ck_oauth_connections_sync_status",
        ),
        CheckConstraint(
            "status != 'connected' OR ("
            "encrypted_refresh_token IS NOT NULL AND length(encrypted_refresh_token) > 0 "
            "AND encryption_key_version IS NOT NULL"
            ")",
            name="ck_oauth_connections_connected_token_required",
        ),
        Index("ix_oauth_connections_user_provider_status", "user_id", "provider", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    provider: Mapped[str] = mapped_column(String(64), default="google_gmail", nullable=False)
    mailbox_email: Mapped[str] = mapped_column(String(320), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="connected", nullable=False)
    encrypted_refresh_token: Mapped[str] = mapped_column(Text, nullable=True)
    encryption_key_version: Mapped[str] = mapped_column(String(32), nullable=True)
    granted_scopes: Mapped[list] = mapped_column(JSONVariant, default=list, nullable=False)
    last_history_id: Mapped[str] = mapped_column(String(128), nullable=True)
    last_synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    last_sync_status: Mapped[str] = mapped_column(String(32), nullable=True)
    last_sync_error: Mapped[str] = mapped_column(Text, nullable=True)
    revoked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    disconnected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )

    confirmation_events: Mapped[list["EmailConfirmationEvent"]] = relationship(
        back_populates="oauth_connection", cascade="all, delete-orphan"
    )


class EmailConfirmationEvent(Base):
    __tablename__ = "email_confirmation_events"
    __table_args__ = (
        UniqueConstraint(
            "oauth_connection_id",
            "gmail_message_id",
            name="uq_email_conf_events_conn_msg",
        ),
        CheckConstraint(
            "match_status IN ('matched', 'confirmed', 'ambiguous', 'rejected', 'unmatched')",
            name="ck_email_conf_events_match_status",
        ),
        Index("ix_email_conf_events_user_status", "user_id", "match_status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    oauth_connection_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("oauth_connections.id", ondelete="CASCADE"), index=True, nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    gmail_message_id: Mapped[str] = mapped_column(String(128), nullable=False)
    gmail_thread_id: Mapped[str] = mapped_column(String(128), nullable=True)
    sender_domain: Mapped[str] = mapped_column(String(255), nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    match_status: Mapped[str] = mapped_column(String(32), default="unmatched", nullable=False)
    matched_application_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("applications.id", ondelete="SET NULL"), index=True, nullable=True
    )
    match_signals: Mapped[dict] = mapped_column(JSONVariant, default=dict, nullable=False)
    processed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)

    oauth_connection: Mapped["OAuthConnection"] = relationship(back_populates="confirmation_events")


class ApplicationFollowup(Base):
    __tablename__ = "application_followups"
    __table_args__ = (
        CheckConstraint(
            "reminder_type IN ('initial_followup', 'second_followup', 'interview_thank_you', 'custom')",
            name="ck_app_followups_type",
        ),
        CheckConstraint(
            "status IN ('pending', 'completed', 'snoozed', 'cancelled')",
            name="ck_app_followups_status",
        ),
        Index("ix_app_followups_user_status_due", "user_id", "status", "due_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    application_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("applications.id", ondelete="CASCADE"), index=True, nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    reminder_type: Mapped[str] = mapped_column(String(32), default="initial_followup", nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="pending", nullable=False)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    snoozed_until: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    interval_days: Mapped[int] = mapped_column(Integer, default=7, nullable=False)
    draft_subject: Mapped[str] = mapped_column(String(300), nullable=False)
    draft_body: Mapped[str] = mapped_column(Text, nullable=False)
    recruiter_context: Mapped[dict] = mapped_column(JSONVariant, default=dict, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


class InterviewKit(Base):
    __tablename__ = "interview_kits"
    __table_args__ = (
        Index("ix_interview_kits_user_app_active", "user_id", "application_id", "is_active"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    application_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("applications.id", ondelete="CASCADE"), index=True, nullable=False
    )
    job_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("jobs.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    resume_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("resumes.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    resume_checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    modules_json: Mapped[dict] = mapped_column(JSONVariant, default=dict, nullable=False)
    progress_json: Mapped[dict] = mapped_column(JSONVariant, default=dict, nullable=False)
    user_feedback: Mapped[str] = mapped_column(Text, nullable=True)
    readiness_estimate: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


class InterviewSession(Base):
    __tablename__ = "interview_sessions"
    __table_args__ = (
        CheckConstraint(
            "round_type IN ('technical', 'dsa', 'backend_swe', 'ai_ml', 'resume_deep_dive', 'behavioral', 'mixed')",
            name="ck_interview_sessions_round",
        ),
        CheckConstraint(
            "difficulty IN ('easy', 'medium', 'hard')",
            name="ck_interview_sessions_difficulty",
        ),
        CheckConstraint(
            "status IN ('active', 'completed')",
            name="ck_interview_sessions_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    kit_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("interview_kits.id", ondelete="CASCADE"), index=True, nullable=False
    )
    round_type: Mapped[str] = mapped_column(String(32), default="mixed", nullable=False)
    difficulty: Mapped[str] = mapped_column(String(16), default="medium", nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="active", nullable=False)
    current_question_index: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    current_question_json: Mapped[dict] = mapped_column(JSONVariant, default=dict, nullable=False)
    report_json: Mapped[dict] = mapped_column(JSONVariant, default=dict, nullable=False)
    overall_score: Mapped[float] = mapped_column(Float, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


class InterviewAttempt(Base):
    __tablename__ = "interview_attempts"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    session_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("interview_sessions.id", ondelete="CASCADE"), index=True, nullable=False
    )
    kit_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("interview_kits.id", ondelete="CASCADE"), index=True, nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    question_index: Mapped[int] = mapped_column(Integer, nullable=False)
    module_key: Mapped[str] = mapped_column(String(64), nullable=False)
    difficulty: Mapped[str] = mapped_column(String(24), nullable=False)
    question_text: Mapped[str] = mapped_column(Text, nullable=False)
    expected_structure: Mapped[list] = mapped_column(JSONVariant, default=list, nullable=False)
    hints: Mapped[list] = mapped_column(JSONVariant, default=list, nullable=False)
    candidate_answer: Mapped[str] = mapped_column(Text, nullable=False)
    feedback_json: Mapped[dict] = mapped_column(JSONVariant, default=dict, nullable=False)
    score: Mapped[float] = mapped_column(Float, nullable=False)
    weak_topics: Mapped[list] = mapped_column(JSONVariant, default=list, nullable=False)
    strong_topics: Mapped[list] = mapped_column(JSONVariant, default=list, nullable=False)
    attempted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class InterviewMemory(Base):
    __tablename__ = "interview_memories"
    __table_args__ = (
        CheckConstraint(
            "memory_type IN ('weak_topic', 'recurring_mistake', 'strong_area', 'behavioral_example', 'asked_question', 'feedback_note')",
            name="ck_interview_memories_type",
        ),
        Index("ix_interview_memories_user_type", "user_id", "memory_type"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    memory_type: Mapped[str] = mapped_column(String(32), nullable=False)
    topic: Mapped[str] = mapped_column(String(160), index=True, nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(String(120), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, default=0.7, nullable=False)
    evidence_refs: Mapped[list] = mapped_column(JSONVariant, default=list, nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    last_score: Mapped[float] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


def _reject_append_only_mutation(_mapper, _connection, target) -> None:
    raise ValueError(f"{type(target).__name__} is append-only and immutable")


event.listen(ApprovalRecord, "before_update", _reject_append_only_mutation)
event.listen(ApprovalRecord, "before_delete", _reject_append_only_mutation)
event.listen(AuditLog, "before_update", _reject_append_only_mutation)
event.listen(AuditLog, "before_delete", _reject_append_only_mutation)
