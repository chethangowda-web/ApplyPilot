import re
from typing import Any

from app.database.models import CandidateProfile, Job, Resume
from app.schemas.applications import (
    ExtractedFormQuestion,
    FormFieldType,
    FormMappingResult,
    MappedApplicationAnswer,
    SensitiveCategory,
)

_KEY_CLEAN_RE = re.compile(r"[^a-z0-9]+")

_SENSITIVE_RULES: list[tuple[SensitiveCategory, tuple[str, ...]]] = [
    (
        "work_authorization",
        (
            "work authorization",
            "authorized to work",
            "legally authorized",
            "right to work",
            "citizenship",
            "citizen",
            "permanent resident",
            "green card",
            "work permit",
        ),
    ),
    (
        "sponsorship",
        (
            "sponsorship",
            "visa",
            "h-1b",
            "h1b",
            "opt",
            "cpt",
            "immigration",
            "require sponsorship",
        ),
    ),
    (
        "demographic",
        (
            "gender",
            "race",
            "ethnicity",
            "veteran",
            "disability",
            "eeo",
            "equal employment",
            "pronouns",
            "hispanic",
            "latino",
        ),
    ),
    (
        "legal",
        (
            "felony",
            "criminal",
            "conviction",
            "background check",
            "non-compete",
            "noncompete",
            "litigation",
            "security clearance",
            "export control",
        ),
    ),
    (
        "salary",
        (
            "salary",
            "compensation",
            "pay expectation",
            "desired pay",
            "expected ctc",
            "current ctc",
            "hourly rate",
        ),
    ),
    (
        "experience",
        (
            "years of experience",
            "how many years",
            "years experience",
        ),
    ),
    (
        "identity",
        (
            "ssn",
            "social security",
            "passport",
            "national id",
            "tax id",
            "date of birth",
            "dob",
            "driver's license",
        ),
    ),
]

_VALID_FIELD_TYPES: frozenset[str] = frozenset(
    {"text", "textarea", "select", "radio", "checkbox", "number", "date", "file", "url", "email", "phone"}
)


def _normalize_question_key(raw: str) -> str:
    cleaned = _KEY_CLEAN_RE.sub("_", raw.strip().lower()).strip("_")
    return (cleaned or "question")[:160]


def classify_sensitive_question(question_text: str, question_key: str = "") -> SensitiveCategory | None:
    """Deterministically classify whether an application question is sensitive."""
    combined = f"{question_key} {question_text}".lower().replace("_", " ")
    for category, keywords in _SENSITIVE_RULES:
        for kw in keywords:
            if re.search(rf"\b{re.escape(kw)}\b", combined):
                return category
    return None


def _coerce_field_type(raw: str | None) -> FormFieldType:
    candidate = (raw or "text").strip().lower()
    if candidate in _VALID_FIELD_TYPES:
        return candidate  # type: ignore[return-value]
    return "text"


class FormUnderstandingAgent:
    """Extracts application form questions and maps strictly verified candidate evidence."""

    def extract_questions(
        self,
        *,
        job: Job,
        form_schema: list[dict[str, Any]] | None = None,
    ) -> list[ExtractedFormQuestion]:
        """Extract normalized questions from structured form schema or JD application instructions."""
        questions: list[ExtractedFormQuestion] = []
        seen_keys: set[str] = set()

        for item in form_schema or []:
            label = str(item.get("question") or item.get("label") or item.get("name") or "").strip()
            if not label:
                continue
            raw_key = str(item.get("question_key") or item.get("key") or item.get("name") or label)
            qkey = _normalize_question_key(raw_key)
            if qkey in seen_keys:
                continue
            seen_keys.add(qkey)

            explicit_sensitive = bool(item.get("is_sensitive", False))
            category = classify_sensitive_question(label, qkey)
            is_sensitive = explicit_sensitive or (category is not None)
            if explicit_sensitive and category is None:
                category = "identity"

            options = [str(o).strip() for o in (item.get("options") or []) if str(o).strip()]
            questions.append(
                ExtractedFormQuestion(
                    question_key=qkey,
                    question=label,
                    field_type=_coerce_field_type(item.get("field_type") or item.get("type")),
                    required=bool(item.get("required", True)),
                    is_sensitive=is_sensitive,
                    sensitive_category=category,
                    options=options,
                )
            )

        if not questions and job.requirements and job.requirements.application_instructions:
            for idx, instr in enumerate(job.requirements.application_instructions, start=1):
                text_val = str(instr).strip()
                if not text_val:
                    continue
                qkey = _normalize_question_key(f"instruction_{idx}_{text_val[:40]}")
                if qkey in seen_keys:
                    continue
                seen_keys.add(qkey)
                category = classify_sensitive_question(text_val, qkey)
                questions.append(
                    ExtractedFormQuestion(
                        question_key=qkey,
                        question=text_val,
                        field_type="textarea",
                        required=True,
                        is_sensitive=category is not None,
                        sensitive_category=category,
                        options=[],
                    )
                )

        return questions

    def map_candidate_answers(
        self,
        *,
        questions: list[ExtractedFormQuestion],
        profile: CandidateProfile,
        resume: Resume,
        llm_drafts: dict[str, str] | None = None,
    ) -> FormMappingResult:
        """Map questions strictly from verified profile/resume evidence without fabrication."""
        mapped: list[MappedApplicationAnswer] = []
        missing_required: list[str] = []
        missing_optional: list[str] = []
        unverified_keys: list[str] = []
        sensitive_confirm_keys: list[str] = []

        prefs = profile.confirmed_preferences if isinstance(profile.confirmed_preferences, dict) else {}
        verified_skills = sorted({s.skill_name.strip() for s in (profile.skills or []) if s.verified and s.skill_name})
        drafts = llm_drafts or {}

        for q in questions:
            answer_val: str | None = None
            evidence_src: str | None = None
            state = "missing"
            requires_confirm = False
            reason: str | None = None

            if q.is_sensitive:
                # Never fabricate sensitive answers; even confirmed_preferences hints require per-application confirmation.
                pref_val = self._lookup_sensitive_preference(q, prefs)
                if pref_val is not None:
                    answer_val = pref_val
                    evidence_src = "candidate_profile.confirmed_preferences"
                    state = "unverified"
                    requires_confirm = True
                    reason = "Sensitive answer requires explicit per-application user confirmation"
                else:
                    answer_val = None
                    evidence_src = None
                    state = "missing"
                    requires_confirm = True
                    reason = "Sensitive question missing; explicit user input required"
            else:
                verified_val, verified_src = self._lookup_verified_non_sensitive(
                    q=q,
                    profile=profile,
                    resume=resume,
                    verified_skills=verified_skills,
                )
                if verified_val is not None:
                    answer_val = verified_val
                    evidence_src = verified_src
                    state = "verified"
                    requires_confirm = False
                elif q.question_key in drafts and str(drafts[q.question_key]).strip():
                    # LLM drafts are NEVER treated as verified evidence.
                    answer_val = str(drafts[q.question_key]).strip()
                    evidence_src = "llm_draft_unverified"
                    state = "unverified"
                    requires_confirm = True
                    reason = "LLM-generated draft is unverified and requires user confirmation"
                else:
                    answer_val = None
                    evidence_src = None
                    state = "missing"
                    requires_confirm = q.required
                    reason = "No verified profile or resume evidence found"

            if state == "missing":
                if q.required:
                    missing_required.append(q.question_key)
                else:
                    missing_optional.append(q.question_key)
            elif state == "unverified":
                unverified_keys.append(q.question_key)

            if q.is_sensitive and requires_confirm:
                sensitive_confirm_keys.append(q.question_key)

            mapped.append(
                MappedApplicationAnswer(
                    question_key=q.question_key,
                    question=q.question,
                    field_type=q.field_type,
                    required=q.required,
                    is_sensitive=q.is_sensitive,
                    sensitive_category=q.sensitive_category,
                    verification_state=state,  # type: ignore[arg-type]
                    proposed_answer=answer_val,
                    evidence_source=evidence_src,
                    requires_user_confirmation=requires_confirm,
                    reason=reason,
                )
            )

        ready_for_approval = (
            not missing_required
            and not unverified_keys
            and not sensitive_confirm_keys
        )

        return FormMappingResult(
            questions=questions,
            mapped_answers=mapped,
            missing_required_keys=missing_required,
            missing_optional_keys=missing_optional,
            unverified_keys=unverified_keys,
            sensitive_keys_requiring_confirmation=sensitive_confirm_keys,
            ready_for_approval=ready_for_approval,
        )

    def analyze_and_map(
        self,
        *,
        job: Job,
        profile: CandidateProfile,
        resume: Resume,
        form_schema: list[dict[str, Any]] | None = None,
        llm_drafts: dict[str, str] | None = None,
    ) -> FormMappingResult:
        """Extract form questions and map verified candidate evidence."""
        questions = self.extract_questions(job=job, form_schema=form_schema)
        return self.map_candidate_answers(
            questions=questions,
            profile=profile,
            resume=resume,
            llm_drafts=llm_drafts,
        )

    @staticmethod
    def _lookup_sensitive_preference(q: ExtractedFormQuestion, prefs: dict[str, Any]) -> str | None:
        if q.question_key in prefs and str(prefs[q.question_key]).strip():
            return str(prefs[q.question_key]).strip()
        if q.sensitive_category and q.sensitive_category in prefs and str(prefs[q.sensitive_category]).strip():
            return str(prefs[q.sensitive_category]).strip()
        return None

    @staticmethod
    def _lookup_verified_non_sensitive(
        *,
        q: ExtractedFormQuestion,
        profile: CandidateProfile,
        resume: Resume,
        verified_skills: list[str],
    ) -> tuple[str | None, str | None]:
        key = q.question_key
        text = q.question.lower()

        if q.field_type == "file" or "resume" in key or "cv" in key or "resume" in text:
            return resume.original_filename, "resumes.original_filename"

        if q.field_type == "email" or key in {"email", "email_address"} or "email" in text:
            if profile.email and profile.email.strip():
                return profile.email.strip(), "candidate_profiles.email"

        if q.field_type == "phone" or key in {"phone", "phone_number", "mobile"} or "phone" in text:
            if profile.phone and profile.phone.strip():
                return profile.phone.strip(), "candidate_profiles.phone"

        if key in {"full_name", "name", "candidate_name"} or "full name" in text:
            if profile.full_name and profile.full_name.strip():
                return profile.full_name.strip(), "candidate_profiles.full_name"

        if key in {"first_name"} or "first name" in text:
            parts = (profile.full_name or "").strip().split()
            if parts:
                return parts[0], "candidate_profiles.full_name"

        if key in {"last_name"} or "last name" in text:
            parts = (profile.full_name or "").strip().split()
            if len(parts) >= 2:
                return parts[-1], "candidate_profiles.full_name"

        if "linkedin" in key or "linkedin" in text:
            if profile.linkedin_url and profile.linkedin_url.strip():
                return profile.linkedin_url.strip(), "candidate_profiles.linkedin_url"

        if "github" in key or "github" in text:
            if profile.github_url and profile.github_url.strip():
                return profile.github_url.strip(), "candidate_profiles.github_url"

        if "portfolio" in key or "website" in key or "portfolio" in text:
            if profile.portfolio_url and profile.portfolio_url.strip():
                return profile.portfolio_url.strip(), "candidate_profiles.portfolio_url"

        if key in {"location", "city", "current_location"} or "location" in text:
            if profile.location and profile.location.strip():
                return profile.location.strip(), "candidate_profiles.location"

        if ("skills" in key or "technical skills" in text) and verified_skills:
            return ", ".join(verified_skills), "candidate_skills.verified"

        return None, None
