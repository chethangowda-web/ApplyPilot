import hashlib
import json
import uuid
from typing import Any
from app.database.models import CandidateProfile, Job, JobRequirement, Resume

TEMPLATE_VERSION = "ats-classic-v1"


def _norm_text(value: str | None) -> str:
    return (value or "").strip()


def _content_derived_id(prefix: str, canonical_text: str) -> str:
    """Create a stable, position-independent evidence ID from canonicalized content."""
    digest = hashlib.sha256(canonical_text.encode("utf-8")).hexdigest()[:16]
    return f"{prefix}:{digest}"


def compute_education_evidence_id(item: dict[str, Any]) -> str:
    """Return the stable content-derived evidence_id for an education record."""
    parts = [
        _norm_text(str(item.get("institution") or "")),
        _norm_text(str(item.get("degree") or "")),
        _norm_text(str(item.get("field_of_study") or "")),
        _norm_text(str(item.get("start_year") or "")),
        _norm_text(str(item.get("end_year") or "")),
    ]
    return _content_derived_id("edu", "|".join(parts).lower())


def compute_achievement_evidence_id(item: Any) -> str:
    """Return the stable content-derived evidence_id for an achievement record."""
    if isinstance(item, dict):
        text = str(item.get("title") or item.get("description") or "")
    else:
        text = str(item)
    return _content_derived_id("ach", _norm_text(text).lower())


def _canonicalize_json_list(items: list[Any] | None, prefix: str) -> list[dict[str, Any]]:
    """Build evidence entries with content-derived IDs (position-independent)."""
    result: list[dict[str, Any]] = []
    for item in items or []:
        if prefix == "edu" and isinstance(item, dict):
            eid = compute_education_evidence_id(item)
        else:
            eid = compute_achievement_evidence_id(item)
        result.append({"evidence_id": eid, "value": item})
    # Sort by evidence_id for deterministic ordering
    result.sort(key=lambda x: x["evidence_id"])
    return result


def compute_evidence_fingerprint(
    *,
    profile: CandidateProfile,
    master_resume: Resume | None,
    job: Job,
    req: JobRequirement | None,
    template_version: str = TEMPLATE_VERSION,
) -> str:
    """Compute a deterministic SHA-256 fingerprint across all resume-rendered candidate fields & JD reqs."""
    verified_skills = sorted(
        [
            {
                "evidence_id": f"skill:{s.id}",
                "skill_name": _norm_text(s.skill_name),
                "category": _norm_text(s.category),
                "proficiency": _norm_text(s.proficiency),
                "evidence_ref": _norm_text(s.evidence_ref),
                "verified": bool(s.verified),
            }
            for s in (profile.skills or [])
            if s.verified
        ],
        key=lambda x: (x["evidence_id"], x["skill_name"].lower()),
    )

    projects = sorted(
        [
            {
                "evidence_id": f"project:{p.id}",
                "name": _norm_text(p.name),
                "summary": _norm_text(p.summary),
                "technologies": sorted(_norm_text(t) for t in (p.technologies or []) if _norm_text(t)),
                "implemented_features": list(p.implemented_features or []),
                "verified_metrics": list(p.verified_metrics or []),
                "repo_url": _norm_text(p.repo_url),
                "live_url": _norm_text(p.live_url),
                "start_date": _norm_text(p.start_date),
                "end_date": _norm_text(p.end_date),
            }
            for p in (profile.projects or [])
        ],
        key=lambda x: x["evidence_id"],
    )

    experiences = sorted(
        [
            {
                "evidence_id": f"exp:{e.id}",
                "company": _norm_text(e.company),
                "role": _norm_text(e.role),
                "location": _norm_text(e.location),
                "start_date": _norm_text(e.start_date),
                "end_date": _norm_text(e.end_date),
                "is_current": bool(e.is_current),
                "verified_responsibilities": list(e.verified_responsibilities or []),
            }
            for e in (profile.experiences or [])
        ],
        key=lambda x: x["evidence_id"],
    )

    certifications = sorted(
        [
            {
                "evidence_id": f"cert:{c.id}",
                "name": _norm_text(c.name),
                "issuer": _norm_text(c.issuer),
                "issued_date": _norm_text(c.issued_date),
                "credential_url": _norm_text(c.credential_url),
                "verified": bool(c.verified),
            }
            for c in (profile.certifications or [])
            if c.verified
        ],
        key=lambda x: x["evidence_id"],
    )

    req_payload: dict[str, Any] = {}
    if req is not None:
        req_payload = {
            "role_category": _norm_text(req.role_category),
            "required_skills": list(req.required_skills or []),
            "preferred_skills": list(req.preferred_skills or []),
            "trainable_skills": list(req.trainable_skills or []),
            "responsibilities": list(req.responsibilities or []),
            "technical_stack": list(req.technical_stack or []),
            "experience_min": req.experience_min,
            "experience_max": req.experience_max,
            "education_requirements": list(req.education_requirements or []),
        }

    payload = {
        "template_version": template_version,
        "master_resume": (
            {
                "id": str(master_resume.id),
                "sha256_checksum": master_resume.sha256_checksum,
            }
            if master_resume is not None
            else None
        ),
        "candidate_identity": {
            "evidence_id": f"profile:{profile.id}",
            "full_name": _norm_text(profile.full_name),
            "email": _norm_text(profile.email),
            "phone": _norm_text(profile.phone),
            "location": _norm_text(profile.location),
            "github_url": _norm_text(profile.github_url),
            "linkedin_url": _norm_text(profile.linkedin_url),
            "portfolio_url": _norm_text(profile.portfolio_url),
            "summary": _norm_text(profile.summary),
        },
        "education": _canonicalize_json_list(profile.education, "edu"),
        "achievements": _canonicalize_json_list(profile.achievements, "ach"),
        "skills": verified_skills,
        "projects": projects,
        "experiences": experiences,
        "certifications": certifications,
        "job": {
            "job_id": str(job.id),
            "company": _norm_text(job.company),
            "role_title": _norm_text(job.role_title),
            "requirements": req_payload,
        },
    }
    serialized = json.dumps(payload, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def compute_generation_idempotency_key(
    *,
    profile_id: uuid.UUID | str,
    job_id: uuid.UUID | str,
    evidence_fingerprint: str,
) -> str:
    """Shared logical idempotency key across automatic and non-forced manual requests."""
    raw = f"{profile_id}:{job_id}:{evidence_fingerprint}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
