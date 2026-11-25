import hashlib
import math
import re
from typing import Any
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.database.models import (
    CandidateProfile,
    CandidateSkill,
    Experience,
    Job,
    JobRequirement,
    Project,
)
from app.llm.base import ProviderError
from app.llm.router import ModelRouter

_TOKEN_RE = re.compile(r"[a-z0-9+#.]+")


def deterministic_local_embedding(text: str, dimensions: int = 768) -> list[float]:
    """Generate a deterministic, L2-normalized 768-dim token-projection vector when offline."""
    vec = [0.0] * dimensions
    tokens = _TOKEN_RE.findall((text or "").lower())
    if not tokens:
        return vec
    bigrams = [f"{tokens[i]}_{tokens[i + 1]}" for i in range(len(tokens) - 1)]
    for tok in [*tokens, *bigrams]:
        digest = hashlib.sha256(tok.encode("utf-8")).digest()
        idx = int.from_bytes(digest[:4], "little") % dimensions
        sign = 1.0 if (digest[4] & 1) == 0 else -1.0
        vec[idx] += sign
    norm = math.sqrt(sum(v * v for v in vec))
    if norm > 0:
        vec = [round(v / norm, 6) for v in vec]
    return vec


def cosine_similarity(vec_a: list[float] | None, vec_b: list[float] | None) -> float:
    if not vec_a or not vec_b or len(vec_a) != len(vec_b):
        return 0.0
    dot = sum(a * b for a, b in zip(vec_a, vec_b))
    na = math.sqrt(sum(a * a for a in vec_a))
    nb = math.sqrt(sum(b * b for b in vec_b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return max(0.0, min(1.0, round(dot / (na * nb), 4)))


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


async def embed_text(
    text: str,
    *,
    router: ModelRouter | None = None,
    dimensions: int = 768,
) -> tuple[list[float], str]:
    """Embed text via configured ModelRouter provider, or deterministic local projection if offline."""
    if router is not None:
        try:
            vecs = await router.embed([text], dimensions=dimensions)
            if vecs and len(vecs[0]) == dimensions:
                return vecs[0], "provider_api"
        except ProviderError:
            pass
    return deterministic_local_embedding(text, dimensions=dimensions), "local_lexical_hash"


async def sync_job_and_profile_embeddings(
    db: Session,
    *,
    job: Job,
    profile: CandidateProfile,
    router: ModelRouter | None = None,
) -> str:
    """Populate missing 768-dim vectors on Job and verified CandidateProfile evidence records."""
    source_used = "local_lexical_hash"
    if job.jd_embedding is None:
        jd_vec, source_used = await embed_text(
            f"{job.role_title}\n{job.raw_jd}",
            router=router,
        )
        job.jd_embedding = jd_vec

    for sk in profile.skills or []:
        if sk.verified and sk.embedding is None:
            sk_vec, _ = await embed_text(
                f"{sk.skill_name} {sk.category} {sk.evidence_ref or ''}",
                router=router,
            )
            sk.embedding = sk_vec

    for proj in profile.projects or []:
        if proj.embedding is None:
            proj_text = (
                f"{proj.name} {proj.summary} {' '.join(proj.technologies or [])} "
                f"{' '.join(str(f) for f in (proj.implemented_features or []))}"
            )
            proj.embedding, _ = await embed_text(proj_text, router=router)

    for exp in profile.experiences or []:
        if exp.embedding is None:
            exp_text = (
                f"{exp.role} {exp.company} "
                f"{' '.join(str(r) for r in (exp.verified_responsibilities or []))}"
            )
            exp.embedding, _ = await embed_text(exp_text, router=router)

    for cert in profile.certifications or []:
        if cert.verified and cert.embedding is None:
            cert.embedding, _ = await embed_text(f"{cert.name} {cert.issuer}", router=router)

    db.flush()
    return source_used


def compute_pgvector_or_cosine_similarities(
    db: Session,
    *,
    job: Job,
    profile: CandidateProfile,
) -> dict[str, Any]:
    """Query pgvector cosine similarity on PostgreSQL (or exact Python cosine on SQLite)."""
    job_vec = list(job.jd_embedding or []) or deterministic_local_embedding(f"{job.role_title}\n{job.raw_jd}")
    dialect = db.bind.dialect.name if db.bind is not None else "sqlite"

    evidence_hits: list[dict[str, Any]] = []

    if dialect == "postgresql" and job.jd_embedding is not None:
        # Native pgvector cosine distance query across skills, projects, experiences, certifications
        sk_rows = db.execute(
            select(
                CandidateSkill.id,
                CandidateSkill.skill_name,
                (1 - CandidateSkill.embedding.cosine_distance(job_vec)).label("sim"),
            )
            .where(
                CandidateSkill.profile_id == profile.id,
                CandidateSkill.verified.is_(True),
                CandidateSkill.embedding.is_not(None),
            )
            .order_by(CandidateSkill.embedding.cosine_distance(job_vec))
            .limit(8)
        ).all()
        for row in sk_rows:
            evidence_hits.append(
                {"type": "skill", "id": str(row.id), "label": row.skill_name, "similarity": round(float(row.sim), 4)}
            )

        pr_rows = db.execute(
            select(
                Project.id,
                Project.name,
                (1 - Project.embedding.cosine_distance(job_vec)).label("sim"),
            )
            .where(Project.profile_id == profile.id, Project.embedding.is_not(None))
            .order_by(Project.embedding.cosine_distance(job_vec))
            .limit(5)
        ).all()
        for row in pr_rows:
            evidence_hits.append(
                {"type": "project", "id": str(row.id), "label": row.name, "similarity": round(float(row.sim), 4)}
            )

        ex_rows = db.execute(
            select(
                Experience.id,
                Experience.role,
                Experience.company,
                (1 - Experience.embedding.cosine_distance(job_vec)).label("sim"),
            )
            .where(Experience.profile_id == profile.id, Experience.embedding.is_not(None))
            .order_by(Experience.embedding.cosine_distance(job_vec))
            .limit(5)
        ).all()
        for row in ex_rows:
            evidence_hits.append(
                {
                    "type": "experience",
                    "id": str(row.id),
                    "label": f"{row.role} @ {row.company}",
                    "similarity": round(float(row.sim), 4),
                }
            )
    else:
        for sk in profile.skills or []:
            if sk.verified:
                sv = list(sk.embedding or []) or deterministic_local_embedding(f"{sk.skill_name} {sk.category}")
                evidence_hits.append(
                    {"type": "skill", "id": str(sk.id), "label": sk.skill_name, "similarity": cosine_similarity(job_vec, sv)}
                )
        for proj in profile.projects or []:
            pv = list(proj.embedding or []) or deterministic_local_embedding(
                f"{proj.name} {proj.summary} {' '.join(proj.technologies or [])}"
            )
            evidence_hits.append(
                {"type": "project", "id": str(proj.id), "label": proj.name, "similarity": cosine_similarity(job_vec, pv)}
            )
        for exp in profile.experiences or []:
            ev = list(exp.embedding or []) or deterministic_local_embedding(
                f"{exp.role} {exp.company} {' '.join(str(r) for r in (exp.verified_responsibilities or []))}"
            )
            evidence_hits.append(
                {
                    "type": "experience",
                    "id": str(exp.id),
                    "label": f"{exp.role} @ {exp.company}",
                    "similarity": cosine_similarity(job_vec, ev),
                }
            )
        for cert in profile.certifications or []:
            if cert.verified:
                cv = list(cert.embedding or []) or deterministic_local_embedding(f"{cert.name} {cert.issuer}")
                evidence_hits.append(
                    {"type": "certification", "id": str(cert.id), "label": cert.name, "similarity": cosine_similarity(job_vec, cv)}
                )

    evidence_hits.sort(key=lambda x: x["similarity"], reverse=True)
    top_sims = [h["similarity"] for h in evidence_hits[:3]]
    overall_sim = round(sum(top_sims) / len(top_sims), 4) if top_sims else 0.0
    return {
        "overall_similarity": overall_sim,
        "top_semantic_matches": evidence_hits[:8],
    }


def map_requirements_to_evidence(
    *,
    req: JobRequirement | None,
    profile: CandidateProfile,
) -> dict[str, Any]:
    """Trace JD requirements against verified skills, projects, experiences, and certifications.

    Strictly separates:
      - matched_skills (backed by verified evidence)
      - confirmed_missing_skills (not found anywhere in candidate profile)
      - missing_evidence (unverified claims, planned-only project features, or missing supporting artifacts)
    """
    req_skills = list(req.required_skills or []) if req else []
    pref_skills = list(req.preferred_skills or []) if req else []

    verified_skills = [s for s in (profile.skills or []) if s.verified]
    unverified_skill_names = {_normalize_skill(s.skill_name) for s in (profile.skills or []) if not s.verified}
    planned_only_text = " ".join(
        " ".join(str(f) for f in (p.planned_features or [])) for p in (profile.projects or [])
    ).lower()

    evidence_mapping: list[dict[str, Any]] = []
    matched_required: list[str] = []
    confirmed_missing_required: list[str] = []
    missing_evidence: list[dict[str, str]] = []

    def _find_evidence_for_skill(skill: str) -> list[dict[str, Any]]:
        norm = _normalize_skill(skill)
        sources: list[dict[str, Any]] = []

        for sk in verified_skills:
            if _normalize_skill(sk.skill_name) == norm:
                sources.append(
                    {
                        "type": "skill",
                        "id": str(sk.id),
                        "label": sk.skill_name,
                        "evidence_ref": sk.evidence_ref or "",
                    }
                )
        for proj in profile.projects or []:
            tech_norms = {_normalize_skill(t) for t in (proj.technologies or [])}
            impl_blob = f"{proj.summary} {' '.join(str(f) for f in (proj.implemented_features or []))}".lower()
            if norm in tech_norms or re.search(rf"(?<![a-z0-9]){re.escape(norm)}(?![a-z0-9])", impl_blob):
                sources.append(
                    {
                        "type": "project",
                        "id": str(proj.id),
                        "label": proj.name,
                        "detail": proj.summary[:160],
                    }
                )
        for exp in profile.experiences or []:
            resp_blob = f"{exp.role} {' '.join(str(r) for r in (exp.verified_responsibilities or []))}".lower()
            if re.search(rf"(?<![a-z0-9]){re.escape(norm)}(?![a-z0-9])", resp_blob):
                sources.append(
                    {
                        "type": "experience",
                        "id": str(exp.id),
                        "label": f"{exp.role} @ {exp.company}",
                    }
                )
        for cert in profile.certifications or []:
            if cert.verified and re.search(rf"(?<![a-z0-9]){re.escape(norm)}(?![a-z0-9])", cert.name.lower()):
                sources.append(
                    {
                        "type": "certification",
                        "id": str(cert.id),
                        "label": cert.name,
                    }
                )
        return sources

    for sk in req_skills:
        norm = _normalize_skill(sk)
        srcs = _find_evidence_for_skill(sk)
        if srcs:
            matched_required.append(sk)
            # Check if matched ONLY by a bare skill without project/experience/evidence_ref backing
            has_concrete_backing = any(
                s["type"] in ("project", "experience", "certification") or bool(s.get("evidence_ref"))
                for s in srcs
            )
            if not has_concrete_backing:
                missing_evidence.append(
                    {
                        "item": sk,
                        "reason": "listed_skill_lacks_project_or_experience_evidence",
                    }
                )
            evidence_mapping.append(
                {
                    "requirement": sk,
                    "kind": "required_skill",
                    "status": "matched",
                    "evidence_sources": srcs,
                }
            )
        elif norm in unverified_skill_names:
            missing_evidence.append(
                {"item": sk, "reason": "candidate_skill_marked_unverified"}
            )
            evidence_mapping.append(
                {
                    "requirement": sk,
                    "kind": "required_skill",
                    "status": "unverified_evidence",
                    "evidence_sources": [],
                }
            )
        elif re.search(rf"(?<![a-z0-9]){re.escape(norm)}(?![a-z0-9])", planned_only_text):
            missing_evidence.append(
                {"item": sk, "reason": "only_in_planned_project_features_not_implemented"}
            )
            evidence_mapping.append(
                {
                    "requirement": sk,
                    "kind": "required_skill",
                    "status": "planned_only_not_verified",
                    "evidence_sources": [],
                }
            )
        else:
            confirmed_missing_required.append(sk)
            evidence_mapping.append(
                {
                    "requirement": sk,
                    "kind": "required_skill",
                    "status": "confirmed_missing",
                    "evidence_sources": [],
                }
            )

    matched_preferred: list[str] = []
    preferred_gaps: list[str] = []
    for sk in pref_skills:
        srcs = _find_evidence_for_skill(sk)
        if srcs:
            matched_preferred.append(sk)
            evidence_mapping.append(
                {
                    "requirement": sk,
                    "kind": "preferred_skill",
                    "status": "matched",
                    "evidence_sources": srcs,
                }
            )
        else:
            preferred_gaps.append(sk)

    # Profile-level missing evidence checks (education / prior experience when needed)
    if not (profile.education or []):
        missing_evidence.append({"item": "education", "reason": "candidate_education_missing_in_profile"})
    if req and req.experience_min is not None and req.experience_min > 0 and not (profile.experiences or []):
        missing_evidence.append({"item": "experience", "reason": "no_prior_experience_records_in_profile"})

    return {
        "matched_required": matched_required,
        "confirmed_missing_required": confirmed_missing_required,
        "unmatched_required": [sk for sk in req_skills if sk not in matched_required],
        "matched_preferred": matched_preferred,
        "preferred_gaps": preferred_gaps,
        "evidence_mapping": evidence_mapping,
        "missing_evidence": missing_evidence,
    }
