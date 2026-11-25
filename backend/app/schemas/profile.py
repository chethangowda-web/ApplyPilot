import uuid
from datetime import datetime
from typing import Any
from pydantic import BaseModel, ConfigDict, Field


class EducationEntry(BaseModel):
    institution: str = Field(..., min_length=1, max_length=250)
    degree: str = Field(..., min_length=1, max_length=160)
    field_of_study: str | None = Field(default=None, max_length=160)
    cgpa: str | None = Field(default=None, max_length=32)
    start_date: str | None = Field(default=None, max_length=40)
    end_date: str | None = Field(default=None, max_length=40)


class CandidateSkillIn(BaseModel):
    skill_name: str = Field(..., min_length=1, max_length=120)
    category: str = Field(default="General", min_length=1, max_length=80)
    proficiency: str | None = Field(default=None, max_length=64)
    evidence_ref: str | None = Field(default=None, max_length=500)
    verified: bool = True


class CandidateSkillOut(CandidateSkillIn):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    created_at: datetime


class ProjectIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    summary: str = Field(..., min_length=1, max_length=4000)
    technologies: list[str] = Field(default_factory=list)
    implemented_features: list[str] = Field(default_factory=list)
    planned_features: list[str] = Field(default_factory=list)
    verified_metrics: list[str] = Field(default_factory=list)
    repo_url: str | None = Field(default=None, max_length=500)
    live_url: str | None = Field(default=None, max_length=500)
    start_date: str | None = Field(default=None, max_length=40)
    end_date: str | None = Field(default=None, max_length=40)


class ProjectOut(ProjectIn):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    created_at: datetime


class ExperienceIn(BaseModel):
    company: str = Field(..., min_length=1, max_length=200)
    role: str = Field(..., min_length=1, max_length=200)
    location: str | None = Field(default=None, max_length=200)
    start_date: str = Field(..., min_length=1, max_length=40)
    end_date: str | None = Field(default=None, max_length=40)
    is_current: bool = False
    verified_responsibilities: list[str] = Field(default_factory=list)


class ExperienceOut(ExperienceIn):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    created_at: datetime


class CertificationIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=250)
    issuer: str = Field(..., min_length=1, max_length=200)
    issued_date: str | None = Field(default=None, max_length=40)
    credential_url: str | None = Field(default=None, max_length=500)
    verified: bool = True


class CertificationOut(CertificationIn):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    created_at: datetime


class CandidateProfileUpsertRequest(BaseModel):
    full_name: str = Field(..., min_length=1, max_length=200)
    phone: str | None = Field(default=None, max_length=50)
    email: str = Field(..., min_length=5, max_length=320)
    location: str | None = Field(default=None, max_length=200)
    github_url: str | None = Field(default=None, max_length=500)
    linkedin_url: str | None = Field(default=None, max_length=500)
    portfolio_url: str | None = Field(default=None, max_length=500)
    summary: str | None = Field(default=None, max_length=4000)
    education: list[EducationEntry] = Field(default_factory=list)
    achievements: list[str] = Field(default_factory=list)
    confirmed_preferences: dict[str, Any] = Field(default_factory=dict)


class CandidateProfileResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    user_id: uuid.UUID
    full_name: str
    phone: str | None
    email: str
    location: str | None
    github_url: str | None
    linkedin_url: str | None
    portfolio_url: str | None
    summary: str | None
    education: list[dict[str, Any]]
    achievements: list[str]
    confirmed_preferences: dict[str, Any]
    skills: list[CandidateSkillOut] = Field(default_factory=list)
    projects: list[ProjectOut] = Field(default_factory=list)
    experiences: list[ExperienceOut] = Field(default_factory=list)
    certifications: list[CertificationOut] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime
