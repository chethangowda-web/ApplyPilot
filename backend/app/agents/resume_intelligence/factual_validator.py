import re
import secrets
from typing import Literal
from pydantic import BaseModel, Field

from app.agents.resume_intelligence.evidence_retriever import (
    CertificationEvidenceRecord,
    ExperienceEvidenceRecord,
    ProjectEvidenceRecord,
    RetrievedCandidateEvidence,
)
from app.llm.base import LLMMessage, ProviderError
from app.llm.router import ModelRouter
from app.schemas.resume_intelligence import (
    ClaimClassification,
    FactualValidationReport,
    TailoredResumeContent,
    ValidatedClaim,
)
from app.services.jd_sanitizer import _KNOWN_SKILLS
from app.services.semantic_matching_service import _normalize_skill

_TOKEN_RE = re.compile(r"[a-z0-9+#.]+")
_METRIC_TOKEN_RE = re.compile(
    r"(?:\b\d+(?:\.\d+)?%|\b\d+(?:\.\d+)?x\b|\b\d+(?:,\d{3})*(?:\.\d+)?\s*(?:k|m|b|ms|s|users?|workflows?|requests?|rps|qps|hrs?|hours?|days?)\b|\b\d+(?:\.\d+)?\+?\b)",
    re.IGNORECASE,
)

# Stopwords + safe resume action/connector vocabulary permitted when rewriting verified facts.
_REWRITE_SAFE_VOCAB: frozenset[str] = frozenset(
    {
        "a",
        "an",
        "and",
        "as",
        "at",
        "by",
        "for",
        "from",
        "in",
        "into",
        "of",
        "on",
        "or",
        "the",
        "to",
        "using",
        "via",
        "with",
        "across",
        "against",
        "based",
        "between",
        "including",
        "is",
        "are",
        "was",
        "were",
        "that",
        "this",
        "candidate",
        "engineer",
        "developer",
        "targeting",
        "experienced",
        "verified",
        "experience",
        "skill",
        "skills",
        "built",
        "build",
        "building",
        "designed",
        "design",
        "developed",
        "develop",
        "implemented",
        "implement",
        "engineered",
        "created",
        "create",
        "delivered",
        "deliver",
        "orchestrated",
        "processed",
        "improved",
        "optimized",
        "integrated",
        "deployed",
        "maintained",
        "tested",
        "automated",
        "achieved",
        "applied",
        "focused",
        "systems",
        "system",
        "service",
        "services",
        "application",
        "applications",
        "solution",
        "solutions",
        "project",
        "projects",
        "work",
        "workflow",
        "workflows",
        "role",
        "end",
        "production",
        "deterministic",
        "reliable",
        "scalable",
    }
)


class _LLMCrossCheckOutput(BaseModel):
    passed: bool
    flagged_claim_ids: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


def _norm_ws(text: str | None) -> str:
    return " ".join((text or "").strip().lower().split())


def _content_tokens(text: str) -> list[str]:
    tokens = [t.strip(".") for t in _TOKEN_RE.findall((text or "").lower())]
    return [t for t in tokens if len(t) >= 2]


def _extract_metric_numbers(text: str) -> list[str]:
    """Extract normalized numeric tokens and metric expressions that must be evidence-backed."""
    found: list[str] = []
    for match in _METRIC_TOKEN_RE.finditer(text or ""):
        raw = " ".join(match.group(0).lower().split())
        nums = re.findall(r"\d+(?:\.\d+)?", raw)
        for n in nums:
            if n not in found:
                found.append(n)
    return found


def _collect_known_tech_catalog(evidence: RetrievedCandidateEvidence) -> set[str]:
    """Catalog of unambiguous technology names used to catch cross-project or external leakage."""
    catalog: set[str] = {_normalize_skill(sk) for sk in _KNOWN_SKILLS}
    for s in evidence.skills:
        catalog.add(_normalize_skill(s.skill_name))
    for unv in evidence.excluded_unverified_skills:
        catalog.add(_normalize_skill(unv))
    for p in evidence.projects:
        for t in p.technologies:
            catalog.add(_normalize_skill(t))
    # Exclude overly short tokens that collide with English prose unless explicit tech
    return {t for t in catalog if len(t) >= 2}


_CASE_SENSITIVE_TECHS: frozenset[str] = frozenset({"go", "c", "r"})


def _find_mentioned_techs(text: str, catalog: set[str]) -> list[str]:
    raw_text = text or ""
    lowered = raw_text.lower()
    found: list[str] = []
    for tech in sorted(catalog):
        if tech in _CASE_SENSITIVE_TECHS:
            # Avoid false positives on ordinary English words like "to go"
            if re.search(rf"(?<![A-Za-z0-9]){re.escape(tech.capitalize())}(?![A-Za-z0-9])", raw_text):
                found.append(tech)
        elif re.search(rf"(?<![a-z0-9]){re.escape(tech)}(?![a-z0-9])", lowered):
            found.append(tech)
    return found


def _matches_planned_feature(claim_text: str, planned_items: list[str], implemented_blob: str) -> str | None:
    """Return matching planned feature string if claim appears to promote planned work as implemented."""
    claim_norm = _norm_ws(claim_text)
    claim_tokens = {t for t in _content_tokens(claim_text) if t not in _REWRITE_SAFE_VOCAB and len(t) >= 3}
    if not claim_tokens:
        return None
    impl_tokens = {t for t in _content_tokens(implemented_blob) if len(t) >= 3}

    for pf in planned_items:
        pf_norm = _norm_ws(pf)
        if not pf_norm:
            continue
        if pf_norm in claim_norm and pf_norm not in _norm_ws(implemented_blob):
            return pf
        pf_tokens = {t for t in _content_tokens(pf) if t not in _REWRITE_SAFE_VOCAB and len(t) >= 3}
        if not pf_tokens:
            continue
        # Only focus on tokens distinguishing the planned feature from already-implemented work
        unique_pf_tokens = pf_tokens - impl_tokens
        check_tokens = unique_pf_tokens if unique_pf_tokens else pf_tokens
        overlap = check_tokens & claim_tokens
        ratio = len(overlap) / len(check_tokens)
        if (len(overlap) >= 2 and ratio >= 0.6) or (len(check_tokens) == 1 and len(overlap) == 1 and pf_norm not in _norm_ws(implemented_blob)):
            return pf
    return None


def _classify_text_against_evidence(
    *,
    claim_text: str,
    verbatim_sources: list[str],
    scoped_tokens: set[str],
    scoped_numbers: set[str],
    allowed_evidence_ids: set[str],
    claimed_evidence_ids: list[str],
) -> tuple[ClaimClassification, list[str], str | None]:
    """Classify a single textual claim strictly as supported_fact, rewritten_fact, or unsupported_claim."""
    norm_claim = _norm_ws(claim_text)
    if not norm_claim:
        return "unsupported_claim", [], "empty_claim_text"

    # 1. Check metric / number grounding first (never silently pass invented numbers)
    for num in _extract_metric_numbers(claim_text):
        if num not in scoped_numbers:
            return "unsupported_claim", [], f"fabricated_or_unverified_metric:{num}"

    # 2. Check evidence ID validity if provided
    valid_eids = [eid for eid in claimed_evidence_ids if eid in allowed_evidence_ids]
    invalid_eids = [eid for eid in claimed_evidence_ids if eid not in allowed_evidence_ids]
    if invalid_eids:
        return "unsupported_claim", valid_eids, f"invalid_or_out_of_scope_evidence_ids:{','.join(invalid_eids)}"

    # 3. Verbatim or substring match -> supported_fact
    norm_sources = [_norm_ws(s) for s in verbatim_sources if _norm_ws(s)]
    for src in norm_sources:
        if norm_claim == src or (len(norm_claim) >= 8 and norm_claim in src):
            matched_ids = valid_eids or sorted(allowed_evidence_ids)[:1]
            return "supported_fact", matched_ids, None

    # 4. Rewritten fact verification: every meaningful claim token must trace to scoped evidence
    #    or standard rewrite glue vocabulary, with concrete overlap against scoped evidence.
    tokens = _content_tokens(claim_text)
    if not tokens:
        return "unsupported_claim", valid_eids, "unclassifiable_empty_tokens"

    evidence_hits = sum(1 for t in tokens if t in scoped_tokens)
    grounded_or_safe = sum(1 for t in tokens if t in scoped_tokens or t in _REWRITE_SAFE_VOCAB)
    ungrounded_tokens = [t for t in tokens if t not in scoped_tokens and t not in _REWRITE_SAFE_VOCAB]

    coverage = grounded_or_safe / len(tokens)
    if evidence_hits >= 2 and coverage >= 0.85 and not ungrounded_tokens and (valid_eids or allowed_evidence_ids):
        return "rewritten_fact", valid_eids or sorted(allowed_evidence_ids)[:1], None

    if evidence_hits == 0:
        return "unsupported_claim", valid_eids, "no_overlapping_verified_evidence"

    return (
        "unsupported_claim",
        valid_eids,
        f"ungrounded_terms_in_claim:{','.join(ungrounded_tokens[:5])}" if ungrounded_tokens else "insufficient_evidence_grounding",
    )


class FactualValidatorAgent:
    """Authoritative per-claim deterministic validator with optional non-overriding LLM cross-check."""

    def __init__(self, router: ModelRouter | None = None) -> None:
        self.router = router or ModelRouter()

    def _project_lookup(
        self, evidence: RetrievedCandidateEvidence
    ) -> dict[str, ProjectEvidenceRecord]:
        lookup: dict[str, ProjectEvidenceRecord] = {}
        for p in evidence.projects:
            lookup[p.evidence_id] = p
            lookup[p.project_id] = p
        return lookup

    def _experience_lookup(
        self, evidence: RetrievedCandidateEvidence
    ) -> dict[str, ExperienceEvidenceRecord]:
        lookup: dict[str, ExperienceEvidenceRecord] = {}
        for e in evidence.experiences:
            lookup[e.evidence_id] = e
            lookup[e.experience_id] = e
        return lookup

    def validate_deterministic(
        self,
        *,
        content: TailoredResumeContent,
        evidence: RetrievedCandidateEvidence,
    ) -> FactualValidationReport:
        claims: list[ValidatedClaim] = []
        claim_seq = 0

        def _next_id(prefix: str) -> str:
            nonlocal claim_seq
            claim_seq += 1
            return f"{prefix}-{claim_seq}"

        tech_catalog = _collect_known_tech_catalog(evidence)
        verified_skill_by_norm = {
            _normalize_skill(s.skill_name): s for s in evidence.skills
        }
        unverified_skill_norms = {
            _normalize_skill(s) for s in evidence.excluded_unverified_skills
        }
        all_planned_features = [
            pf
            for pfs in evidence.excluded_planned_features_by_project.values()
            for pf in pfs
        ]

        # Build union corpus of verified evidence (explicitly EXCLUDES planned_features & unverified skills)
        global_verified_sources: list[str] = []
        if evidence.summary:
            global_verified_sources.append(evidence.summary)
        for s in evidence.skills:
            global_verified_sources.append(s.skill_name)
        for p in evidence.projects:
            global_verified_sources.extend(
                [p.name, p.summary, *p.technologies, *p.implemented_features, *p.verified_metrics]
            )
            if p.start_date:
                global_verified_sources.append(p.start_date)
            if p.end_date:
                global_verified_sources.append(p.end_date)
        for e in evidence.experiences:
            global_verified_sources.extend(
                [e.company, e.role, e.location or "", e.start_date, e.end_date or "", *e.verified_responsibilities]
            )
        for ed in evidence.education:
            global_verified_sources.extend(
                [
                    ed.institution,
                    ed.degree,
                    ed.field_of_study or "",
                    ed.start_year or "",
                    ed.end_year or "",
                    ed.grade_or_cgpa or "",
                ]
            )
        for c in evidence.certifications:
            global_verified_sources.extend([c.name, c.issuer, c.issued_date or ""])
        for a in evidence.achievements:
            global_verified_sources.append(a.text)

        global_tokens = {t for src in global_verified_sources for t in _content_tokens(src)}
        global_numbers = {n for src in global_verified_sources for n in _extract_metric_numbers(src)}
        global_allowed_eids: set[str] = {
            evidence.identity_evidence_id,
            *(s.evidence_id for s in evidence.skills),
            *(p.evidence_id for p in evidence.projects),
            *(e.evidence_id for e in evidence.experiences),
            *(ed.evidence_id for ed in evidence.education),
            *(c.evidence_id for c in evidence.certifications),
            *(a.evidence_id for a in evidence.achievements),
        }

        # 1. Validate Summary
        sum_text = content.summary.text
        sum_planned = _matches_planned_feature(
            sum_text, all_planned_features, " ".join(global_verified_sources)
        )
        allowed_summary_techs = (
            set(verified_skill_by_norm)
            | {_normalize_skill(pt) for p in evidence.projects for pt in p.technologies}
            | set(_find_mentioned_techs(" ".join(global_verified_sources), tech_catalog))
        ) - unverified_skill_norms
        sum_techs = _find_mentioned_techs(sum_text, tech_catalog)
        bad_sum_techs = [
            t for t in sum_techs if (t in unverified_skill_norms) or (t not in allowed_summary_techs)
        ]
        if sum_planned:
            claims.append(
                ValidatedClaim(
                    claim_id=_next_id("summary"),
                    category="summary",
                    entity_id=evidence.identity_evidence_id,
                    claim_text=sum_text,
                    classification="unsupported_claim",
                    violation_reason=f"planned_feature_promoted_as_implemented:{sum_planned}",
                )
            )
        elif bad_sum_techs:
            claims.append(
                ValidatedClaim(
                    claim_id=_next_id("summary"),
                    category="summary",
                    entity_id=evidence.identity_evidence_id,
                    claim_text=sum_text,
                    classification="unsupported_claim",
                    violation_reason=f"unverified_skill_in_summary:{','.join(bad_sum_techs)}",
                )
            )
        else:
            cls, matched_ids, reason = _classify_text_against_evidence(
                claim_text=sum_text,
                verbatim_sources=[evidence.summary or ""],
                scoped_tokens=global_tokens,
                scoped_numbers=global_numbers,
                allowed_evidence_ids=global_allowed_eids,
                claimed_evidence_ids=content.summary.evidence_ids,
            )
            claims.append(
                ValidatedClaim(
                    claim_id=_next_id("summary"),
                    category="summary",
                    entity_id=evidence.identity_evidence_id,
                    claim_text=sum_text,
                    classification=cls,
                    matched_evidence_ids=matched_ids,
                    violation_reason=reason,
                )
            )

        # 2. Validate Skills (each skill item checked individually)
        for grp in content.skill_groups:
            for sk in grp.skills:
                sk_norm = _normalize_skill(sk)
                if sk_norm in unverified_skill_norms:
                    claims.append(
                        ValidatedClaim(
                            claim_id=_next_id("skill"),
                            category="skill",
                            claim_text=sk,
                            classification="unsupported_claim",
                            violation_reason="candidate_skill_marked_unverified",
                        )
                    )
                elif sk_norm in verified_skill_by_norm:
                    rec = verified_skill_by_norm[sk_norm]
                    cls: ClaimClassification = (
                        "supported_fact" if _norm_ws(sk) == _norm_ws(rec.skill_name) else "rewritten_fact"
                    )
                    claims.append(
                        ValidatedClaim(
                            claim_id=_next_id("skill"),
                            category="skill",
                            entity_id=rec.evidence_id,
                            claim_text=sk,
                            classification=cls,
                            matched_evidence_ids=[rec.evidence_id],
                        )
                    )
                else:
                    claims.append(
                        ValidatedClaim(
                            claim_id=_next_id("skill"),
                            category="skill",
                            claim_text=sk,
                            classification="unsupported_claim",
                            violation_reason="fabricated_or_unverified_skill",
                        )
                    )

        # 3. Validate Projects (STRICT per-project scoping!)
        proj_lookup = self._project_lookup(evidence)
        for proj in content.projects:
            rec = proj_lookup.get(proj.project_id)
            if rec is None:
                claims.append(
                    ValidatedClaim(
                        claim_id=_next_id("proj"),
                        category="project_outcome",
                        entity_id=proj.project_id,
                        claim_text=f"{proj.name}: {proj.summary}",
                        classification="unsupported_claim",
                        violation_reason="unknown_project_record",
                    )
                )
                continue

            proj_eids = {rec.evidence_id, rec.project_id}
            # Verify project identity/links/dates haven't been fabricated or altered
            metadata_violations: list[str] = []
            if _norm_ws(proj.name) != _norm_ws(rec.name):
                metadata_violations.append("project_name_mismatch")
            if proj.repo_url and _norm_ws(proj.repo_url) != _norm_ws(rec.repo_url):
                metadata_violations.append("project_repo_url_mismatch")
            if proj.live_url and _norm_ws(proj.live_url) != _norm_ws(rec.live_url):
                metadata_violations.append("project_live_url_mismatch")
            if proj.start_date and _norm_ws(proj.start_date) != _norm_ws(rec.start_date):
                metadata_violations.append("project_start_date_mismatch")
            if proj.end_date and _norm_ws(proj.end_date) != _norm_ws(rec.end_date):
                metadata_violations.append("project_end_date_mismatch")

            rec_tech_norms = {_normalize_skill(t) for t in rec.technologies}
            # Validate per-project technologies (prevent cross-project leakage!)
            other_project_techs = {
                _normalize_skill(t)
                for other in evidence.projects
                if other.project_id != rec.project_id
                for t in other.technologies
            }
            for tech in proj.technologies:
                t_norm = _normalize_skill(tech)
                if t_norm in rec_tech_norms:
                    claims.append(
                        ValidatedClaim(
                            claim_id=_next_id("proj-tech"),
                            category="project_tech",
                            entity_id=rec.evidence_id,
                            claim_text=f"{rec.name}: {tech}",
                            classification="supported_fact",
                            matched_evidence_ids=[rec.evidence_id],
                        )
                    )
                elif t_norm in other_project_techs:
                    claims.append(
                        ValidatedClaim(
                            claim_id=_next_id("proj-tech"),
                            category="project_tech",
                            entity_id=rec.evidence_id,
                            claim_text=f"{rec.name}: {tech}",
                            classification="unsupported_claim",
                            violation_reason="cross_project_technology_leakage",
                        )
                    )
                else:
                    claims.append(
                        ValidatedClaim(
                            claim_id=_next_id("proj-tech"),
                            category="project_tech",
                            entity_id=rec.evidence_id,
                            claim_text=f"{rec.name}: {tech}",
                            classification="unsupported_claim",
                            violation_reason="unverified_project_technology",
                        )
                    )

            # Scoped project corpus (ONLY this project's verified data)
            rec_sources = [rec.name, rec.summary, *rec.technologies, *rec.implemented_features, *rec.verified_metrics]
            if rec.start_date:
                rec_sources.append(rec.start_date)
            if rec.end_date:
                rec_sources.append(rec.end_date)
            rec_tokens = {t for s in rec_sources for t in _content_tokens(s)}
            rec_numbers = {n for s in rec_sources for n in _extract_metric_numbers(s)}
            rec_impl_blob = " ".join(rec_sources)
            rec_planned = evidence.excluded_planned_features_by_project.get(rec.project_id, [])

            # Validate project summary
            if metadata_violations:
                claims.append(
                    ValidatedClaim(
                        claim_id=_next_id("proj-meta"),
                        category="project_outcome",
                        entity_id=rec.evidence_id,
                        claim_text=f"{proj.name} ({proj.start_date or ''}-{proj.end_date or ''})",
                        classification="unsupported_claim",
                        violation_reason=",".join(metadata_violations),
                    )
                )
            else:
                sum_pf = _matches_planned_feature(proj.summary, rec_planned, rec_impl_blob)
                if sum_pf:
                    claims.append(
                        ValidatedClaim(
                            claim_id=_next_id("proj-sum"),
                            category="project_outcome",
                            entity_id=rec.evidence_id,
                            claim_text=proj.summary,
                            classification="unsupported_claim",
                            violation_reason=f"planned_feature_promoted_as_implemented:{sum_pf}",
                        )
                    )
                else:
                    mentioned = _find_mentioned_techs(proj.summary, tech_catalog)
                    allowed_proj_techs = rec_tech_norms | {
                        m for m in mentioned if re.search(rf"(?<![a-z0-9]){re.escape(m)}(?![a-z0-9])", rec_impl_blob.lower())
                    }
                    leaked = [m for m in mentioned if m not in allowed_proj_techs]
                    if leaked:
                        reason = (
                            f"cross_project_technology_leakage:{','.join(leaked)}"
                            if any(m in other_project_techs for m in leaked)
                            else f"unverified_project_technology:{','.join(leaked)}"
                        )
                        claims.append(
                            ValidatedClaim(
                                claim_id=_next_id("proj-sum"),
                                category="project_outcome",
                                entity_id=rec.evidence_id,
                                claim_text=proj.summary,
                                classification="unsupported_claim",
                                violation_reason=reason,
                            )
                        )
                    else:
                        cls, mids, reason = _classify_text_against_evidence(
                            claim_text=proj.summary,
                            verbatim_sources=[rec.summary, *rec.implemented_features],
                            scoped_tokens=rec_tokens,
                            scoped_numbers=rec_numbers,
                            allowed_evidence_ids=proj_eids,
                            claimed_evidence_ids=proj.evidence_ids or [rec.evidence_id],
                        )
                        claims.append(
                            ValidatedClaim(
                                claim_id=_next_id("proj-sum"),
                                category="project_outcome",
                                entity_id=rec.evidence_id,
                                claim_text=proj.summary,
                                classification=cls,
                                matched_evidence_ids=mids,
                                violation_reason=reason,
                            )
                        )

            # Validate each project bullet
            for bullet in proj.bullets:
                has_numbers = bool(_extract_metric_numbers(bullet.text))
                cat: Literal["metric", "project_outcome"] = "metric" if has_numbers else "project_outcome"

                pf_hit = _matches_planned_feature(bullet.text, rec_planned, rec_impl_blob)
                if pf_hit:
                    claims.append(
                        ValidatedClaim(
                            claim_id=_next_id("proj-bullet"),
                            category=cat,
                            entity_id=rec.evidence_id,
                            claim_text=bullet.text,
                            classification="unsupported_claim",
                            violation_reason=f"planned_feature_promoted_as_implemented:{pf_hit}",
                        )
                    )
                    continue

                mentioned = _find_mentioned_techs(bullet.text, tech_catalog)
                allowed_proj_techs = rec_tech_norms | {
                    m for m in mentioned if re.search(rf"(?<![a-z0-9]){re.escape(m)}(?![a-z0-9])", rec_impl_blob.lower())
                }
                leaked = [m for m in mentioned if m not in allowed_proj_techs]
                if leaked:
                    reason = (
                        f"cross_project_technology_leakage:{','.join(leaked)}"
                        if any(m in other_project_techs for m in leaked)
                        else f"unverified_project_technology:{','.join(leaked)}"
                    )
                    claims.append(
                        ValidatedClaim(
                            claim_id=_next_id("proj-bullet"),
                            category=cat,
                            entity_id=rec.evidence_id,
                            claim_text=bullet.text,
                            classification="unsupported_claim",
                            violation_reason=reason,
                        )
                    )
                    continue

                cls, mids, reason = _classify_text_against_evidence(
                    claim_text=bullet.text,
                    verbatim_sources=[rec.summary, *rec.implemented_features, *rec.verified_metrics],
                    scoped_tokens=rec_tokens,
                    scoped_numbers=rec_numbers,
                    allowed_evidence_ids=proj_eids,
                    claimed_evidence_ids=bullet.evidence_ids,
                )
                claims.append(
                    ValidatedClaim(
                        claim_id=_next_id("proj-bullet"),
                        category=cat,
                        entity_id=rec.evidence_id,
                        claim_text=bullet.text,
                        classification=cls,
                        matched_evidence_ids=mids,
                        violation_reason=reason,
                    )
                )

        # 4. Validate Experiences (STRICT per-experience scoping + dates/company/role)
        exp_lookup = self._experience_lookup(evidence)
        for exp in content.experiences:
            rec = exp_lookup.get(exp.experience_id)
            if rec is None:
                claims.append(
                    ValidatedClaim(
                        claim_id=_next_id("exp"),
                        category="experience",
                        entity_id=exp.experience_id,
                        claim_text=f"{exp.role} at {exp.company}",
                        classification="unsupported_claim",
                        violation_reason="fabricated_or_unknown_experience_record",
                    )
                )
                continue

            exp_eids = {rec.evidence_id, rec.experience_id}
            # Check dates/company/role match verbatim
            header_violations: list[str] = []
            if _norm_ws(exp.company) != _norm_ws(rec.company):
                header_violations.append("experience_company_mismatch")
            if _norm_ws(exp.role) != _norm_ws(rec.role):
                header_violations.append("experience_role_mismatch")
            if _norm_ws(exp.start_date) != _norm_ws(rec.start_date):
                header_violations.append("experience_start_date_mismatch")
            expected_end = "present" if rec.is_current and not rec.end_date else _norm_ws(rec.end_date)
            actual_end = "present" if exp.is_current and not exp.end_date else _norm_ws(exp.end_date)
            if actual_end != expected_end:
                header_violations.append("experience_end_date_mismatch")
            if exp.location and rec.location and _norm_ws(exp.location) != _norm_ws(rec.location):
                header_violations.append("experience_location_mismatch")

            claims.append(
                ValidatedClaim(
                    claim_id=_next_id("exp-header"),
                    category="experience",
                    entity_id=rec.evidence_id,
                    claim_text=f"{exp.role} — {exp.company} ({exp.start_date} - {exp.end_date or 'Present'})",
                    classification="unsupported_claim" if header_violations else "supported_fact",
                    matched_evidence_ids=[] if header_violations else [rec.evidence_id],
                    violation_reason=",".join(header_violations) if header_violations else None,
                )
            )

            rec_sources = [
                rec.company,
                rec.role,
                rec.location or "",
                rec.start_date,
                rec.end_date or "",
                *rec.verified_responsibilities,
            ]
            rec_tokens = {t for s in rec_sources for t in _content_tokens(s)}
            rec_numbers = {n for s in rec_sources for n in _extract_metric_numbers(s)}
            rec_blob = " ".join(rec_sources).lower()

            for bullet in exp.bullets:
                mentioned = _find_mentioned_techs(bullet.text, tech_catalog)
                ungrounded_tech = [
                    m for m in mentioned
                    if not re.search(rf"(?<![a-z0-9]){re.escape(m)}(?![a-z0-9])", rec_blob)
                ]
                if ungrounded_tech:
                    claims.append(
                        ValidatedClaim(
                            claim_id=_next_id("exp-bullet"),
                            category="experience",
                            entity_id=rec.evidence_id,
                            claim_text=bullet.text,
                            classification="unsupported_claim",
                            violation_reason=f"unverified_technology_in_experience:{','.join(ungrounded_tech)}",
                        )
                    )
                    continue

                cls, mids, reason = _classify_text_against_evidence(
                    claim_text=bullet.text,
                    verbatim_sources=rec.verified_responsibilities,
                    scoped_tokens=rec_tokens,
                    scoped_numbers=rec_numbers,
                    allowed_evidence_ids=exp_eids,
                    claimed_evidence_ids=bullet.evidence_ids,
                )
                claims.append(
                    ValidatedClaim(
                        claim_id=_next_id("exp-bullet"),
                        category="experience",
                        entity_id=rec.evidence_id,
                        claim_text=bullet.text,
                        classification=cls,
                        matched_evidence_ids=mids,
                        violation_reason=reason,
                    )
                )

        # 5. Validate Education (exact verbatim verification)
        edu_by_id = {ed.evidence_id: ed for ed in evidence.education}
        for edu in content.education:
            rec = edu_by_id.get(edu.evidence_id)
            if rec is None:
                # Try matching by institution + degree
                rec = next(
                    (
                        e
                        for e in evidence.education
                        if _norm_ws(e.institution) == _norm_ws(edu.institution)
                        and _norm_ws(e.degree) == _norm_ws(edu.degree)
                    ),
                    None,
                )
            if rec is None:
                claims.append(
                    ValidatedClaim(
                        claim_id=_next_id("edu"),
                        category="education",
                        entity_id=edu.evidence_id,
                        claim_text=f"{edu.degree} — {edu.institution}",
                        classification="unsupported_claim",
                        violation_reason="fabricated_or_unknown_education_record",
                    )
                )
                continue

            edu_violations: list[str] = []
            if _norm_ws(edu.institution) != _norm_ws(rec.institution):
                edu_violations.append("institution_mismatch")
            if _norm_ws(edu.degree) != _norm_ws(rec.degree):
                edu_violations.append("degree_mismatch")
            if _norm_ws(edu.field_of_study) != _norm_ws(rec.field_of_study):
                edu_violations.append("field_of_study_mismatch")
            if edu.start_year and _norm_ws(edu.start_year) != _norm_ws(rec.start_year):
                edu_violations.append("start_year_mismatch")
            if edu.end_year and _norm_ws(edu.end_year) != _norm_ws(rec.end_year):
                edu_violations.append("end_year_mismatch")
            if edu.grade_or_cgpa and _norm_ws(edu.grade_or_cgpa) != _norm_ws(rec.grade_or_cgpa):
                edu_violations.append("grade_mismatch")

            claims.append(
                ValidatedClaim(
                    claim_id=_next_id("edu"),
                    category="education",
                    entity_id=rec.evidence_id,
                    claim_text=f"{edu.degree} in {edu.field_of_study or ''} at {edu.institution}".strip(),
                    classification="unsupported_claim" if edu_violations else "supported_fact",
                    matched_evidence_ids=[] if edu_violations else [rec.evidence_id],
                    violation_reason=",".join(edu_violations) if edu_violations else None,
                )
            )

        # 6. Validate Certifications
        cert_lookup: dict[str, CertificationEvidenceRecord] = {}
        for c in evidence.certifications:
            cert_lookup[c.evidence_id] = c
            cert_lookup[c.certification_id] = c

        for cert in content.certifications:
            rec = cert_lookup.get(cert.certification_id)
            if rec is None:
                claims.append(
                    ValidatedClaim(
                        claim_id=_next_id("cert"),
                        category="certification",
                        entity_id=cert.certification_id,
                        claim_text=f"{cert.name} — {cert.issuer}",
                        classification="unsupported_claim",
                        violation_reason="unverified_or_fabricated_certification",
                    )
                )
                continue

            cert_violations: list[str] = []
            if _norm_ws(cert.name) != _norm_ws(rec.name):
                cert_violations.append("certification_name_mismatch")
            if _norm_ws(cert.issuer) != _norm_ws(rec.issuer):
                cert_violations.append("certification_issuer_mismatch")
            if cert.issued_date and _norm_ws(cert.issued_date) != _norm_ws(rec.issued_date):
                cert_violations.append("certification_date_mismatch")
            if cert.credential_url and _norm_ws(cert.credential_url) != _norm_ws(rec.credential_url):
                cert_violations.append("certification_url_mismatch")

            claims.append(
                ValidatedClaim(
                    claim_id=_next_id("cert"),
                    category="certification",
                    entity_id=rec.evidence_id,
                    claim_text=f"{cert.name} — {cert.issuer}",
                    classification="unsupported_claim" if cert_violations else "supported_fact",
                    matched_evidence_ids=[] if cert_violations else [rec.evidence_id],
                    violation_reason=",".join(cert_violations) if cert_violations else None,
                )
            )

        # 7. Validate Achievements
        ach_by_id = {a.evidence_id: a for a in evidence.achievements}
        ach_sources = [a.text for a in evidence.achievements]
        ach_tokens = {t for s in ach_sources for t in _content_tokens(s)}
        ach_numbers = {n for s in ach_sources for n in _extract_metric_numbers(s)}
        ach_eids = set(ach_by_id)
        for ach in content.achievements:
            cls, mids, reason = _classify_text_against_evidence(
                claim_text=ach.text,
                verbatim_sources=ach_sources,
                scoped_tokens=ach_tokens,
                scoped_numbers=ach_numbers,
                allowed_evidence_ids=ach_eids,
                claimed_evidence_ids=ach.evidence_ids,
            )
            claims.append(
                ValidatedClaim(
                    claim_id=_next_id("ach"),
                    category="achievement",
                    entity_id=mids[0] if mids else None,
                    claim_text=ach.text,
                    classification=cls,
                    matched_evidence_ids=mids,
                    violation_reason=reason,
                )
            )

        supported_count = sum(1 for c in claims if c.classification == "supported_fact")
        rewritten_count = sum(1 for c in claims if c.classification == "rewritten_fact")
        unsupported_claims = [c for c in claims if c.classification == "unsupported_claim"]
        unsupported_count = len(unsupported_claims)
        deterministic_passed = len(claims) > 0 and unsupported_count == 0

        return FactualValidationReport(
            claims=claims,
            supported_count=supported_count,
            rewritten_count=rewritten_count,
            unsupported_count=unsupported_count,
            unsupported_claims=unsupported_claims,
            deterministic_passed=deterministic_passed,
            llm_crosscheck_passed=None,
            llm_crosscheck_notes=[],
            validation_passed=deterministic_passed,
        )

    async def validate(
        self,
        *,
        content: TailoredResumeContent,
        evidence: RetrievedCandidateEvidence,
        use_llm_crosscheck: bool = True,
        skip_llm_on_deterministic_fail: bool = False,
    ) -> FactualValidationReport:
        """Run authoritative deterministic validation, plus optional OpenAI cross-check (add-rejections only)."""
        report = self.validate_deterministic(content=content, evidence=evidence)

        if (skip_llm_on_deterministic_fail and not report.deterministic_passed) or not use_llm_crosscheck or not self.router.select_provider_chain("evaluation"):
            return report

        boundary = secrets.token_hex(8)
        claim_payload = [
            {"claim_id": c.claim_id, "category": c.category, "entity_id": c.entity_id, "text": c.claim_text}
            for c in report.claims
        ]
        ev_payload = evidence.model_dump(
            mode="json",
            exclude={"evidence_fingerprint"},
        )
        messages = [
            LLMMessage(
                role="system",
                content=(
                    "You are a strict factual consistency auditor. Check each resume claim against the verified "
                    "candidate evidence. Flag any claim_id that invents skills, metrics, dates, certifications, "
                    "or promotes planned features / leaks cross-project technologies."
                ),
            ),
            LLMMessage(
                role="user",
                content=(
                    f"VERIFIED_EVIDENCE:\n{ev_payload}\n\n"
                    f"<CLAIMS_{boundary}>\n{claim_payload}\n</CLAIMS_{boundary}>\n\n"
                    "Return JSON with passed (bool), flagged_claim_ids (list[str]), notes (list[str])."
                ),
            ),
        ]
        try:
            resp = await self.router.generate(
                task="evaluation",
                messages=messages,
                schema=_LLMCrossCheckOutput,
            )
            if isinstance(resp.parsed, _LLMCrossCheckOutput):
                check = resp.parsed
                report.llm_crosscheck_passed = bool(check.passed and not check.flagged_claim_ids)
                report.llm_crosscheck_notes = list(check.notes)

                # LLM cross-check can ONLY ADD rejections, NEVER clear deterministic failures
                flagged = set(check.flagged_claim_ids)
                if flagged:
                    for c in report.claims:
                        if c.claim_id in flagged and c.classification != "unsupported_claim":
                            c.classification = "unsupported_claim"
                            c.violation_reason = "flagged_by_llm_crosscheck"
                    report.supported_count = sum(1 for c in report.claims if c.classification == "supported_fact")
                    report.rewritten_count = sum(1 for c in report.claims if c.classification == "rewritten_fact")
                    report.unsupported_claims = [c for c in report.claims if c.classification == "unsupported_claim"]
                    report.unsupported_count = len(report.unsupported_claims)

                report.validation_passed = bool(
                    report.deterministic_passed and report.llm_crosscheck_passed and report.unsupported_count == 0
                )
        except ProviderError:
            # Failure of optional LLM cross-check leaves deterministic verdict intact
            report.llm_crosscheck_notes.append("llm_crosscheck_unavailable_deterministic_authoritative")

        return report
