from typing import Any
from pydantic import BaseModel, Field


class EvidenceItem(BaseModel):
    """Extracted JD claim paired with supporting verbatim or normalized evidence from the JD."""

    value: str
    evidence_quote: str = ""
    verified_in_jd: bool = True


class JDIntelligenceExtraction(BaseModel):
    """Structured JD intelligence output produced by JDAnalyzerAgent."""

    job_title: str
    company: str
    location: str | None = None
    remote_policy: str = "unknown"  # remote | hybrid | onsite | unknown
    employment_type: str = "unknown"  # internship | full_time | contract | unknown
    role_category: str = "Other Engineering"

    required_skills: list[EvidenceItem] = Field(default_factory=list)
    preferred_skills: list[EvidenceItem] = Field(default_factory=list)
    trainable_skills: list[EvidenceItem] = Field(default_factory=list)
    responsibilities: list[EvidenceItem] = Field(default_factory=list)

    experience_min: float | None = None
    experience_max: float | None = None
    experience_evidence: str | None = None

    education_requirements: list[EvidenceItem] = Field(default_factory=list)
    graduation_criteria: list[EvidenceItem] = Field(default_factory=list)

    mandatory_qualifications: list[EvidenceItem] = Field(default_factory=list)
    preferred_qualifications: list[EvidenceItem] = Field(default_factory=list)
    application_instructions: list[EvidenceItem] = Field(default_factory=list)

    uncertainties: list[str] = Field(default_factory=list)
    prompt_injection_detected: bool = False
    prompt_injection_flags: list[str] = Field(default_factory=list)
    extraction_mode: str = "deterministic"  # llm_verified | deterministic | deterministic_fallback
    llm_usage: dict[str, Any] = Field(default_factory=dict)
