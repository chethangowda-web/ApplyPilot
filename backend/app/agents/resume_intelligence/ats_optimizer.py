import re
from typing import Any
from pydantic import BaseModel, Field

from app.database.models import JobRequirement
from app.schemas.resume_intelligence import TailoredResumeContent
from app.services.resume_parser import parse_resume_bytes
from app.services.semantic_matching_service import _normalize_skill

_STRONG_ACTION_VERBS: frozenset[str] = frozenset(
    {
        "accelerated",
        "architected",
        "automated",
        "built",
        "configured",
        "created",
        "delivered",
        "deployed",
        "designed",
        "developed",
        "engineered",
        "enhanced",
        "implemented",
        "improved",
        "integrated",
        "launched",
        "led",
        "maintained",
        "migrated",
        "optimized",
        "orchestrated",
        "processed",
        "reduced",
        "refactored",
        "scaled",
        "shipped",
        "streamlined",
        "tested",
        "trained",
    }
)

_ALLOWED_ATS_SECTIONS: frozenset[str] = frozenset(
    {"summary", "skills", "experience", "projects", "education", "certifications", "achievements"}
)


class ATSOptimizationReport(BaseModel):
    keyword_coverage: float = Field(..., ge=0.0, le=1.0)
    matched_keywords: list[str] = Field(default_factory=list)
    missing_keywords: list[str] = Field(default_factory=list)
    has_standard_headings: bool = True
    standard_sections_present: list[str] = Field(default_factory=list)
    total_bullets: int = 0
    concise_bullets: int = 0
    action_verb_bullets: int = 0
    avg_bullet_words: float = 0.0
    pdf_parse_passed: bool | None = None
    pdf_ats_report: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)


def flatten_tailored_resume_text(content: TailoredResumeContent) -> str:
    """Collect all rendered resume text for deterministic ATS keyword and readability inspection."""
    parts: list[str] = [
        content.full_name,
        content.summary.text,
    ]
    for grp in content.skill_groups:
        parts.append(f"{grp.category}: {', '.join(grp.skills)}")
    for exp in content.experiences:
        parts.append(f"{exp.role} {exp.company} {exp.start_date} {exp.end_date or ''}")
        parts.extend(b.text for b in exp.bullets)
    for proj in content.projects:
        parts.append(f"{proj.name} {proj.summary} {' '.join(proj.technologies)}")
        parts.extend(b.text for b in proj.bullets)
    for edu in content.education:
        parts.append(f"{edu.institution} {edu.degree} {edu.field_of_study or ''}")
    for cert in content.certifications:
        parts.append(f"{cert.name} {cert.issuer}")
    for ach in content.achievements:
        parts.append(ach.text)
    return "\n".join(p for p in parts if p)


def _text_contains_term(text_lower: str, term: str) -> bool:
    norm = _normalize_skill(term)
    if not norm:
        return False
    # Check both original term and normalized alias using token boundaries
    candidates = {term.strip().lower(), norm}
    for cand in candidates:
        if re.search(rf"(?<![a-z0-9]){re.escape(cand)}(?![a-z0-9])", text_lower):
            return True
    return False


class ATSOptimizationAgent:
    """Deterministic ATS keyword alignment, bullet hygiene, heading, and PDF-parsing checker."""

    def analyze(
        self,
        *,
        content: TailoredResumeContent,
        req: JobRequirement | None = None,
        pdf_bytes: bytes | None = None,
    ) -> ATSOptimizationReport:
        full_text = flatten_tailored_resume_text(content)
        text_lower = full_text.lower()

        target_keywords: list[str] = []
        if req is not None:
            raw_terms = [
                *(req.required_skills or []),
                *(req.preferred_skills or []),
                *(req.technical_stack or []),
            ]
            seen_norm: set[str] = set()
            for t in raw_terms:
                n = _normalize_skill(str(t))
                if n and n not in seen_norm:
                    seen_norm.add(n)
                    target_keywords.append(str(t).strip())

        matched: list[str] = []
        missing: list[str] = []
        for kw in target_keywords:
            if _text_contains_term(text_lower, kw):
                matched.append(kw)
            else:
                missing.append(kw)

        keyword_coverage = round(len(matched) / len(target_keywords), 4) if target_keywords else 1.0

        present_sections = [
            sec.strip().lower()
            for sec in content.section_order
            if sec.strip().lower() in _ALLOWED_ATS_SECTIONS
        ]
        has_standard_headings = bool(present_sections) and (
            "skills" in present_sections or "projects" in present_sections or "experience" in present_sections
        )

        bullets: list[str] = []
        for exp in content.experiences:
            bullets.extend(b.text.strip() for b in exp.bullets if b.text.strip())
        for proj in content.projects:
            bullets.extend(b.text.strip() for b in proj.bullets if b.text.strip())

        concise = 0
        action_verb = 0
        total_words = 0
        warnings: list[str] = []
        for b in bullets:
            words = b.split()
            total_words += len(words)
            if len(b) <= 220 and 4 <= len(words) <= 32:
                concise += 1
            first_word = re.sub(r"[^a-z]", "", words[0].lower()) if words else ""
            if first_word in _STRONG_ACTION_VERBS:
                action_verb += 1
        avg_words = round(total_words / len(bullets), 2) if bullets else 0.0

        if bullets and concise < len(bullets):
            warnings.append("Some resume bullets exceed concise ATS length (<=32 words / 220 chars).")
        if missing:
            warnings.append(
                f"{len(missing)} JD keywords absent because candidate lacks verified evidence: {', '.join(missing[:5])}."
            )

        pdf_parse_passed: bool | None = None
        pdf_ats_report: dict[str, Any] = {}
        if pdf_bytes is not None:
            parsed = parse_resume_bytes(pdf_bytes, ".pdf")
            pdf_parse_passed = bool(parsed.get("validation_passed"))
            pdf_ats_report = dict(parsed.get("ats_report") or {})
            if not pdf_parse_passed:
                warnings.append("Rendered PDF failed round-trip ATS text extraction validation.")

        return ATSOptimizationReport(
            keyword_coverage=keyword_coverage,
            matched_keywords=matched,
            missing_keywords=missing,
            has_standard_headings=has_standard_headings,
            standard_sections_present=present_sections,
            total_bullets=len(bullets),
            concise_bullets=concise,
            action_verb_bullets=action_verb,
            avg_bullet_words=avg_words,
            pdf_parse_passed=pdf_parse_passed,
            pdf_ats_report=pdf_ats_report,
            warnings=warnings,
        )
