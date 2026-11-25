import hashlib
import re
from datetime import datetime, timezone
from difflib import SequenceMatcher
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.database.models import Job, JobRequirement, JobSource
from app.schemas.workflow import NormalizedJob
from app.services.jd_sanitizer import detect_prompt_injection

_TRACKING_PARAMS = frozenset({"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "gh_src", "ref", "source"})
_PUNCT_RE = re.compile(r"[^a-z0-9\s]+")


def normalize_canonical_url(url: str) -> str:
    """Normalize job URLs by stripping tracking parameters and trailing slashes."""
    parsed = urlparse(url.strip())
    clean_query = [
        (k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=False) if k.lower() not in _TRACKING_PARAMS
    ]
    path = parsed.path.rstrip("/") or "/"
    return urlunparse(
        (
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            path,
            "",
            urlencode(sorted(clean_query)),
            "",
        )
    )


def _norm_text(value: str | None) -> str:
    cleaned = _PUNCT_RE.sub(" ", (value or "").lower())
    return " ".join(cleaned.split())


def compute_dedup_key(company: str, title: str) -> str:
    base = f"{_norm_text(company)}|{_norm_text(title)}"
    return hashlib.sha256(base.encode("utf-8")).hexdigest()


def compute_content_checksum(title: str, description: str) -> str:
    base = f"{_norm_text(title)}|{_norm_text(description)}"
    return hashlib.sha256(base.encode("utf-8")).hexdigest()


def _title_similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, _norm_text(a), _norm_text(b)).ratio()


def _locations_compatible(loc_a: str | None, remote_a: bool | None, loc_b: str | None, remote_b: bool | None) -> bool:
    if remote_a and remote_b:
        return True
    na, nb = _norm_text(loc_a), _norm_text(loc_b)
    if not na or not nb:
        return True
    return na == nb or na in nb or nb in na


def classify_role_category(title: str) -> str:
    t = title.lower()
    if any(k in t for k in ("applied ai", "agentic", "llm", "generative ai", "ai engineer", "ai/ml", "ml engineer")):
        return "AI/ML Engineer"
    if any(k in t for k in ("backend", "back-end", "api engineer", "platform engineer")):
        return "Backend Engineer"
    if any(k in t for k in ("sde", "software development engineer", "software engineer", "swe")):
        return "Software Engineer"
    return "Other Engineering"


def upsert_discovered_job(
    db: Session,
    *,
    source: JobSource,
    normalized: NormalizedJob,
    seen_at: datetime | None = None,
) -> tuple[Job, str]:
    """Insert or deduplicate/update a discovered job while preserving first_seen_at and history.

    Returns (job_record, outcome) where outcome is 'created' | 'duplicate' | 'reposted'.
    """
    now = seen_at or datetime.now(timezone.utc)
    canonical = normalize_canonical_url(normalized.canonical_url)
    ext_id = normalized.external_job_id or normalized.source_job_id
    dedup_key = compute_dedup_key(normalized.company, normalized.job_title)
    content_checksum = compute_content_checksum(normalized.job_title, normalized.description)

    existing: Job | None = None
    # 1. Exact source_id + external_job_id
    if ext_id:
        existing = db.scalar(
            select(Job).where(Job.source_id == source.id, Job.source_job_id == ext_id)
        )
    # 2. Exact canonical URL
    if existing is None:
        existing = db.scalar(select(Job).where(Job.canonical_url == canonical))
    # 3. Exact dedup_key or high company+title similarity + compatible location
    if existing is None:
        existing = db.scalar(select(Job).where(Job.dedup_key == dedup_key))
    if existing is None:
        norm_company = _norm_text(normalized.company)
        candidates = db.scalars(select(Job).where(Job.company.ilike(f"%{normalized.company.strip()}%"))).all()
        for cand in candidates:
            if _norm_text(cand.company) == norm_company and _title_similarity(cand.role_title, normalized.job_title) >= 0.88:
                if _locations_compatible(cand.location, cand.is_remote, normalized.location, normalized.is_remote):
                    existing = cand
                    break

    if existing is not None:
        was_expired = existing.status in {"expired", "unavailable"}
        newer_post = bool(
            normalized.posted_at
            and existing.posted_at
            and normalized.posted_at.replace(tzinfo=timezone.utc) > existing.posted_at.replace(tzinfo=timezone.utc)
        )
        changed_body = existing.content_checksum != content_checksum

        existing.last_seen_at = now
        if normalized.updated_at:
            existing.updated_at_source = normalized.updated_at

        provenance = dict(existing.source_provenance or {})
        alternates: list[dict[str, Any]] = list(provenance.get("alternates") or [])
        alt_entry = {"source": normalized.source, "external_job_id": ext_id, "canonical_url": canonical}
        if alt_entry not in alternates and canonical != existing.canonical_url:
            alternates.append(alt_entry)
        provenance["alternates"] = alternates
        existing.source_provenance = provenance

        outcome = "duplicate"
        if normalized.status == "expired":
            existing.status = "expired"
            existing.expired_at = existing.expired_at or now
        elif was_expired or newer_post:
            existing.repost_count += 1
            existing.status = "active"
            existing.expired_at = None
            if normalized.posted_at:
                existing.posted_at = normalized.posted_at
            if changed_body and normalized.description:
                existing.raw_jd = normalized.description
                existing.content_checksum = content_checksum
            outcome = "reposted"

        db.flush()
        return existing, outcome

    provenance_doc = {
        "primary_source": normalized.source,
        "external_job_id": ext_id,
        "raw_metadata": normalized.raw_metadata,
        "alternates": [],
    }
    job = Job(
        source_id=source.id,
        source_job_id=ext_id,
        canonical_url=canonical,
        dedup_key=dedup_key,
        content_checksum=content_checksum,
        company=normalized.company.strip(),
        role_title=normalized.job_title.strip(),
        location=normalized.location,
        remote_policy=normalized.remote_policy or "unknown",
        is_remote=normalized.is_remote,
        employment_type=normalized.employment_type or "unknown",
        salary_min=normalized.salary_min,
        salary_max=normalized.salary_max,
        salary_currency=normalized.salary_currency,
        salary_text=normalized.salary_text,
        raw_jd=normalized.description,
        application_url=normalized.application_url or canonical,
        posted_at=normalized.posted_at,
        updated_at_source=normalized.updated_at,
        first_seen_at=normalized.discovered_at or now,
        last_seen_at=now,
        repost_count=0,
        status=normalized.status,
        expired_at=now if normalized.status == "expired" else None,
        source_provenance=provenance_doc,
    )
    db.add(job)
    db.flush()

    injections = detect_prompt_injection(normalized.description)
    uncertainties: list[str] = []
    if not normalized.description.strip():
        uncertainties.append("missing_description")
    if normalized.experience_min is None and normalized.experience_max is None:
        uncertainties.append("experience_not_specified")
    if injections:
        uncertainties.append("prompt_injection_flagged")

    req = JobRequirement(
        job_id=job.id,
        role_category=classify_role_category(normalized.job_title),
        required_skills=normalized.required_skills,
        preferred_skills=normalized.preferred_skills,
        trainable_skills=[],
        responsibilities=[],
        technical_stack=list(dict.fromkeys([*normalized.required_skills, *normalized.preferred_skills])),
        experience_min=normalized.experience_min,
        experience_max=normalized.experience_max,
        education_requirements=normalized.education_requirements,
        internship_eligible=("intern" in normalized.job_title.lower() or normalized.employment_type == "internship"),
        fresher_eligible=(
            True
            if (normalized.experience_min == 0 or "fresher" in normalized.description.lower())
            else None
        ),
        sponsorship_required=None,
        uncertainties=uncertainties,
        prompt_injection_detected=bool(injections),
    )
    db.add(req)
    db.flush()
    return job, "created"


def mark_job_expired(db: Session, job: Job, reason: str = "upstream_expired") -> Job:
    """Mark a job expired without ever deleting the job or any downstream match/application history."""
    now = datetime.now(timezone.utc)
    job.status = "expired"
    job.expired_at = job.expired_at or now
    provenance = dict(job.source_provenance or {})
    provenance["expiry_reason"] = reason
    job.source_provenance = provenance
    db.flush()
    return job
