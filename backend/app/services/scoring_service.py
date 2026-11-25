from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.database.models import CandidateProfile, Job, JobMatch, JobRequirement
from app.services.semantic_matching_service import (
    compute_pgvector_or_cosine_similarities,
    map_requirements_to_evidence,
)

INDIA_MARKERS = (
    "india",
    "bengaluru",
    "bangalore",
    "hyderabad",
    "pune",
    "chennai",
    "mumbai",
    "gurugram",
    "gurgaon",
    "noida",
    "delhi",
    "kolkata",
    "mangaluru",
    "kochi",
    "ahmedabad",
)

TARGET_ROLE_KEYWORDS = (
    "software development engineer",
    "sde",
    "software engineer",
    "swe",
    "ai engineer",
    "applied ai",
    "ai/ml",
    "ml engineer",
    "machine learning engineer",
    "generative ai",
    "agentic ai",
    "llm engineer",
    "backend engineer",
    "backend developer",
    "python developer",
)

SENIOR_BLOCKERS = (
    "senior",
    "sr.",
    "staff ",
    "principal",
    "lead ",
    "tech lead",
    "engineering manager",
    "director",
    "vp ",
    "architect",
    "sde iii",
    "sde-3",
    "sde 3",
    "sde ii",
    "sde-2",
    "sde 2",
)


@dataclass(frozen=True)
class ScoringWeights:
    technical_skills: float = 40.0
    project_relevance: float = 20.0
    experience_eligibility: float = 15.0
    education_eligibility: float = 10.0
    location_work_mode: float = 5.0
    job_freshness: float = 5.0
    role_alignment: float = 5.0


def _normalize_skill(s: str) -> str:
    val = s.strip().lower()
    aliases = {
        "llms": "llm",
        "rest api": "rest apis",
        "postgres": "postgresql",
        "reactjs": "react",
        "react.js": "react",
        "nextjs": "next.js",
        "nodejs": "node.js",
        "expressjs": "express.js",
    }
    return aliases.get(val, val)


def _check_location_india_or_remote(job: Job) -> tuple[bool | None, str]:
    if job.is_remote is True or job.remote_policy == "remote":
        return True, "confirmed_remote"
    loc = (job.location or "").lower().strip()
    if not loc:
        return None, "location_and_remote_unspecified"
    if "remote" in loc:
        return True, "confirmed_remote"
    if any(marker in loc for marker in INDIA_MARKERS):
        return True, "confirmed_india"
    return False, f"outside_target_geography ({job.location})"


def _check_target_role(title: str) -> tuple[bool, str]:
    lower = title.lower()
    if any(b in lower for b in SENIOR_BLOCKERS):
        return False, "senior_or_mid_level_role_excluded"
    if any(k in lower for k in TARGET_ROLE_KEYWORDS):
        return True, "target_role_matched"
    return False, "non_target_role"


def _check_experience_eligibility(job: Job, req: JobRequirement | None) -> tuple[bool | None, str]:
    exp_min = req.experience_min if req else None
    exp_max = req.experience_max if req else None
    title_lower = job.role_title.lower()
    desc_lower = (job.raw_jd or "").lower()

    if exp_min is not None and exp_min > 2.0:
        return False, f"requires_min_{exp_min}_years_gt_2"
    if exp_min is not None and exp_min <= 2.0:
        return True, f"confirmed_exp_range_{exp_min}_to_{exp_max or 'open'}_years"

    if (
        job.employment_type == "internship"
        or "intern" in title_lower
        or "fresher" in title_lower
        or "fresher" in desc_lower
        or "0-1" in desc_lower
        or "0–1" in desc_lower
        or "0-2" in desc_lower
        or "0–2" in desc_lower
        or "entry level" in desc_lower
    ):
        return True, "confirmed_internship_or_entry_level"

    return None, "experience_requirement_unspecified"


def _check_education_eligibility(
    profile: CandidateProfile,
    req: JobRequirement | None,
    job: Job,
) -> tuple[bool | None, str]:
    edu_reqs = list(req.education_requirements or []) if req else []
    desc_lower = (job.raw_jd or "").lower()
    edu_entries = list(profile.education or [])

    if not edu_reqs:
        if not edu_entries:
            return None, "candidate_education_missing"
        return True, "no_restrictive_degree_constraint"

    if not edu_entries:
        return None, "candidate_education_missing_for_explicit_requirement"

    edu_blob = " ".join(
        f"{e.get('degree', '')} {e.get('field_of_study', '')} {e.get('institution', '')}".lower()
        for e in edu_entries
        if isinstance(e, dict)
    )

    if "final semester" in desc_lower or "recently completed" in desc_lower or "graduated" in desc_lower:
        end_dates = " ".join(str(e.get("end_date", "")).lower() for e in edu_entries if isinstance(e, dict))
        if "2028" in end_dates or "2029" in end_dates:
            return False, "graduation_timeline_mismatch_final_semester_required"

    has_tech_degree = any(
        k in edu_blob for k in ("b.tech", "b.e", "bachelor", "m.tech", "mca", "computer science", "ai", "it")
    )
    if has_tech_degree:
        return True, "verified_degree_matches_cs_ai_requirement"
    return False, "degree_background_mismatch"


def score_job_for_profile(
    *,
    job: Job,
    req: JobRequirement | None,
    profile: CandidateProfile,
    weights: ScoringWeights | None = None,
    now: datetime | None = None,
    db: Session | None = None,
) -> dict[str, Any]:
    """Deterministically evaluate hard eligibility blockers and explainable heuristic alignment (0-100)."""
    w = weights or ScoringWeights()
    ts_now = now or datetime.now(timezone.utc)
    uncertainties: list[str] = list(req.uncertainties or []) if req else []
    blockers: list[str] = []

    if job.status != "active":
        blockers.append(f"posting_status_{job.status}")

    has_description = bool((job.raw_jd or "").strip())
    if not has_description and "missing_job_description" not in uncertainties:
        uncertainties.append("missing_job_description")

    role_ok, role_reason = _check_target_role(job.role_title)
    if not role_ok:
        blockers.append(role_reason)

    loc_ok, loc_reason = _check_location_india_or_remote(job)
    if loc_ok is False:
        blockers.append(loc_reason)
    elif loc_ok is None and loc_reason not in uncertainties:
        uncertainties.append(loc_reason)

    exp_ok, exp_reason = _check_experience_eligibility(job, req)
    if exp_ok is False:
        blockers.append(exp_reason)
    elif exp_ok is None and exp_reason not in uncertainties:
        uncertainties.append(exp_reason)

    edu_ok, edu_reason = _check_education_eligibility(profile, req, job)
    if edu_ok is False:
        blockers.append(edu_reason)
    elif edu_ok is None and edu_reason not in uncertainties:
        uncertainties.append(edu_reason)

    hard_constraints_passed = len(blockers) == 0

    # Stage 3 evidence mapping (strict separation of missing skills vs missing evidence)
    ev = map_requirements_to_evidence(req=req, profile=profile)
    req_skills = list(req.required_skills or []) if req else []
    pref_skills = list(req.preferred_skills or []) if req else []

    req_cov = round(len(ev["matched_required"]) / len(req_skills), 4) if req_skills else 0.0
    pref_cov = round(len(ev["matched_preferred"]) / len(pref_skills), 4) if pref_skills else 0.0

    # Semantic similarity (pgvector on PostgreSQL, cosine on SQLite/in-memory)
    if db is not None:
        sem = compute_pgvector_or_cosine_similarities(db, job=job, profile=profile)
    else:
        from app.services.semantic_matching_service import cosine_similarity, deterministic_local_embedding

        j_vec = list(job.jd_embedding or []) or deterministic_local_embedding(f"{job.role_title}\n{job.raw_jd}")
        sims = []
        for p in profile.projects or []:
            pv = list(p.embedding or []) or deterministic_local_embedding(
                f"{p.name} {p.summary} {' '.join(p.technologies or [])}"
            )
            sims.append(cosine_similarity(j_vec, pv))
        sem = {"overall_similarity": round(max(sims), 4) if sims else 0.0, "top_semantic_matches": []}

    # 1. Technical skills (40)
    if not req_skills and not pref_skills:
        skills_pts = 0.0
        if has_description and "no_structured_skills_extracted" not in uncertainties:
            uncertainties.append("no_structured_skills_extracted")
    else:
        r_ratio = req_cov if req_skills else 1.0
        p_ratio = pref_cov if pref_skills else 1.0
        skills_pts = round(w.technical_skills * (0.8 * r_ratio + 0.2 * p_ratio), 2)

    # 2. Project relevance (20) — strictly implemented features/technologies, never planned_features
    implemented_tech: set[str] = set()
    implemented_text_parts: list[str] = []
    for proj in profile.projects or []:
        if proj.implemented_features or proj.summary:
            for t in proj.technologies or []:
                implemented_tech.add(_normalize_skill(t))
            implemented_text_parts.append(
                f"{proj.name} {proj.summary} {' '.join(str(f) for f in (proj.implemented_features or []))}".lower()
            )
    if not req_skills or not implemented_tech:
        proj_pts = 0.0
    else:
        proj_overlap = sum(1 for s in req_skills if _normalize_skill(s) in implemented_tech)
        domain_bonus = (
            0.25
            if any(k in " ".join(implemented_text_parts) for k in ("agent", "rag", "fastapi", "api", "backend"))
            else 0.0
        )
        proj_pts = round(w.project_relevance * min(1.0, (proj_overlap / len(req_skills)) + domain_bonus), 2)

    exp_pts = w.experience_eligibility if exp_ok is True else 0.0
    edu_pts = w.education_eligibility if edu_ok is True else 0.0
    loc_pts = w.location_work_mode if loc_ok is True else 0.0

    ref_dt = job.posted_at or job.first_seen_at
    if ref_dt is not None:
        if ref_dt.tzinfo is None:
            ref_dt = ref_dt.replace(tzinfo=timezone.utc)
        age_days = max(0.0, (ts_now - ref_dt).total_seconds() / 86400.0)
        if job.posted_at is None:
            freshness_pts = round(w.job_freshness * 0.3, 2)
        elif age_days <= 3:
            freshness_pts = w.job_freshness
        elif age_days <= 7:
            freshness_pts = round(w.job_freshness * 0.8, 2)
        elif age_days <= 14:
            freshness_pts = round(w.job_freshness * 0.5, 2)
        else:
            freshness_pts = round(w.job_freshness * 0.2, 2)
    else:
        freshness_pts = 0.0

    role_pts = w.role_alignment if role_ok else 0.0
    total_score = round(
        skills_pts + proj_pts + exp_pts + edu_pts + loc_pts + freshness_pts + role_pts,
        2,
    )

    # Hard eligibility blockers ALWAYS override semantic similarity & keyword overlap
    if not hard_constraints_passed:
        eligible = False
        recommendation = "ineligible"
    elif any(
        u in uncertainties
        for u in (
            "missing_job_description",
            "location_and_remote_unspecified",
            "experience_requirement_unspecified",
        )
    ):
        eligible = False
        recommendation = "manual_review"
    else:
        eligible = True
        if total_score >= 85:
            recommendation = "priority_application"
        elif total_score >= 75:
            recommendation = "strong_candidate"
        elif total_score >= 65:
            recommendation = "manual_review"
        else:
            recommendation = "do_not_apply"

    confidence = round(max(0.2, 1.0 - 0.15 * len(uncertainties)), 2)

    breakdown = {
        "technical_skills": skills_pts,
        "project_relevance": proj_pts,
        "experience_eligibility": exp_pts,
        "education_eligibility": edu_pts,
        "location_work_mode": loc_pts,
        "job_freshness": freshness_pts,
        "role_alignment": role_pts,
    }
    explanation_parts = [
        f"Estimated rubric alignment score {total_score}/100 ({recommendation}; not a hiring probability).",
        f"Required-skill coverage: {round(req_cov * 100, 1)}% ({len(ev['matched_required'])}/{len(req_skills)}).",
        f"Preferred-skill coverage: {round(pref_cov * 100, 1)}%.",
        f"Role: {role_reason}. Geography: {loc_reason}. Experience: {exp_reason}. Education: {edu_reason}.",
    ]
    if blockers:
        explanation_parts.append(f"Eligibility blockers: {', '.join(blockers)}.")
    if uncertainties:
        explanation_parts.append(f"Confidence {confidence}; uncertainties: {', '.join(uncertainties)}.")

    return {
        "score": total_score,
        "eligible": eligible,
        "hard_constraints_passed": hard_constraints_passed,
        "required_skill_coverage": req_cov,
        "preferred_skill_coverage": pref_cov,
        "semantic_similarity": sem["overall_similarity"],
        "top_semantic_matches": sem["top_semantic_matches"],
        "matched_skills": ev["matched_required"],
        "missing_required_skills": ev["unmatched_required"],
        "confirmed_missing_skills": ev["confirmed_missing_required"],
        "preferred_gaps": ev["preferred_gaps"],
        "evidence_mapping": ev["evidence_mapping"],
        "missing_evidence": ev["missing_evidence"],
        "eligibility_blockers": blockers,
        "uncertainties": uncertainties,
        "confidence": confidence,
        "score_breakdown": breakdown,
        "failed_gates": blockers,
        "recommendation": recommendation,
        "explanation": " ".join(explanation_parts),
    }


def upsert_job_match(
    db: Session,
    *,
    job: Job,
    req: JobRequirement | None,
    profile: CandidateProfile,
    weights: ScoringWeights | None = None,
) -> JobMatch:
    """Compute and persist a full explainable JobMatch record for (job, profile)."""
    result = score_job_for_profile(job=job, req=req, profile=profile, weights=weights, db=db)
    existing = db.scalar(
        select(JobMatch).where(JobMatch.job_id == job.id, JobMatch.profile_id == profile.id)
    )
    if existing is None:
        existing = JobMatch(
            job_id=job.id,
            profile_id=profile.id,
            score=0.0,
            eligible=False,
            hard_constraints_passed=False,
            recommendation="manual_review",
            explanation="",
        )
        db.add(existing)

    existing.score = result["score"]
    existing.eligible = result["eligible"]
    existing.hard_constraints_passed = result["hard_constraints_passed"]
    existing.required_skill_coverage = result["required_skill_coverage"]
    existing.preferred_skill_coverage = result["preferred_skill_coverage"]
    existing.semantic_similarity = result["semantic_similarity"]
    existing.matched_skills = result["matched_skills"]
    existing.missing_required_skills = result["missing_required_skills"]
    existing.preferred_gaps = result["preferred_gaps"]
    existing.evidence_mapping = result["evidence_mapping"]
    existing.missing_evidence = result["missing_evidence"]
    existing.eligibility_blockers = result["eligibility_blockers"]
    existing.uncertainties = result["uncertainties"]
    existing.confidence = result["confidence"]
    existing.score_breakdown = result["score_breakdown"]
    existing.recommendation = result["recommendation"]
    existing.explanation = result["explanation"]
    existing.scored_at = datetime.now(timezone.utc)
    db.flush()
    return existing
