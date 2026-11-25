import uuid
from typing import Any
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.database.models import CandidateProfile, Job, JobMatch, JobRequirement, Resume
from app.services.fingerprint_service import (
    TEMPLATE_VERSION,
    compute_achievement_evidence_id,
    compute_education_evidence_id,
    compute_evidence_fingerprint,
)
from app.services.semantic_matching_service import (
    cosine_similarity,
    deterministic_local_embedding,
    map_requirements_to_evidence,
)


def _norm(value: str) -> str:
    return " ".join(value.strip().lower().split())


class SkillEvidenceRecord(BaseModel):
    evidence_id: str
    skill_name: str
    category: str
    proficiency: str | None = None
    evidence_ref: str | None = None
    jd_relevance: str = "other"  # required | preferred | technical | other
    relevance_score: float = 0.0


class ProjectEvidenceRecord(BaseModel):
    evidence_id: str
    project_id: str
    name: str
    summary: str
    technologies: list[str] = Field(default_factory=list)
    implemented_features: list[str] = Field(default_factory=list)
    verified_metrics: list[str] = Field(default_factory=list)
    repo_url: str | None = None
    live_url: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    matched_jd_skills: list[str] = Field(default_factory=list)
    semantic_similarity: float = 0.0
    relevance_score: float = 0.0


class ExperienceEvidenceRecord(BaseModel):
    evidence_id: str
    experience_id: str
    company: str
    role: str
    location: str | None = None
    start_date: str
    end_date: str | None = None
    is_current: bool = False
    verified_responsibilities: list[str] = Field(default_factory=list)
    matched_jd_skills: list[str] = Field(default_factory=list)
    semantic_similarity: float = 0.0
    relevance_score: float = 0.0


class EducationEvidenceRecord(BaseModel):
    evidence_id: str
    institution: str
    degree: str
    field_of_study: str | None = None
    start_year: str | None = None
    end_year: str | None = None
    grade_or_cgpa: str | None = None


class CertificationEvidenceRecord(BaseModel):
    evidence_id: str
    certification_id: str
    name: str
    issuer: str
    issued_date: str | None = None
    credential_url: str | None = None


class AchievementEvidenceRecord(BaseModel):
    evidence_id: str
    text: str


class RetrievedCandidateEvidence(BaseModel):
    profile_id: uuid.UUID
    job_id: uuid.UUID
    master_resume_id: uuid.UUID | None = None
    evidence_fingerprint: str
    template_version: str = TEMPLATE_VERSION

    full_name: str
    email: str
    phone: str | None = None
    location: str | None = None
    github_url: str | None = None
    linkedin_url: str | None = None
    portfolio_url: str | None = None
    summary: str | None = None
    identity_evidence_id: str

    skills: list[SkillEvidenceRecord] = Field(default_factory=list)
    projects: list[ProjectEvidenceRecord] = Field(default_factory=list)
    experiences: list[ExperienceEvidenceRecord] = Field(default_factory=list)
    education: list[EducationEvidenceRecord] = Field(default_factory=list)
    certifications: list[CertificationEvidenceRecord] = Field(default_factory=list)
    achievements: list[AchievementEvidenceRecord] = Field(default_factory=list)

    matched_required_skills: list[str] = Field(default_factory=list)
    missing_required_skills: list[str] = Field(default_factory=list)
    missing_evidence: list[dict[str, Any]] = Field(default_factory=list)
    # Audit-only sets kept strictly out of resume rendering so FactualValidator can detect leaks
    excluded_unverified_skills: list[str] = Field(default_factory=list)
    excluded_planned_features_by_project: dict[str, list[str]] = Field(default_factory=dict)


class EvidenceRetriever:
    """Retrieves verified, entity-scoped candidate evidence and computes canonical fingerprints."""

    def retrieve(
        self,
        db: Session,
        *,
        profile: CandidateProfile,
        job: Job,
        req: JobRequirement | None,
        match: JobMatch | None = None,
        master_resume: Resume | None = None,
        template_version: str = TEMPLATE_VERSION,
    ) -> RetrievedCandidateEvidence:
        req_skills = [_norm(s) for s in (req.required_skills or [])] if req else []
        pref_skills = [_norm(s) for s in (req.preferred_skills or [])] if req else []
        tech_stack = [_norm(s) for s in (req.technical_stack or [])] if req else []
        job_vec = list(job.jd_embedding or []) or deterministic_local_embedding(
            f"{job.role_title}\n{job.raw_jd}"
        )

        verified_skills: list[SkillEvidenceRecord] = []
        excluded_unverified_skills: list[str] = []
        for sk in profile.skills or []:
            if not sk.verified:
                excluded_unverified_skills.append(sk.skill_name)
                continue
            sk_norm = _norm(sk.skill_name)
            if sk_norm in req_skills:
                jd_rel, score = "required", 3.0
            elif sk_norm in pref_skills:
                jd_rel, score = "preferred", 2.0
            elif sk_norm in tech_stack:
                jd_rel, score = "technical", 1.5
            else:
                jd_rel, score = "other", 0.5
            verified_skills.append(
                SkillEvidenceRecord(
                    evidence_id=f"skill:{sk.id}",
                    skill_name=sk.skill_name.strip(),
                    category=sk.category.strip() or "General",
                    proficiency=sk.proficiency,
                    evidence_ref=sk.evidence_ref,
                    jd_relevance=jd_rel,
                    relevance_score=score,
                )
            )
        verified_skills.sort(key=lambda s: (-s.relevance_score, s.skill_name.lower(), s.evidence_id))

        all_target_skills = list(dict.fromkeys([*req_skills, *pref_skills, *tech_stack]))

        projects: list[ProjectEvidenceRecord] = []
        excluded_planned: dict[str, list[str]] = {}
        for proj in profile.projects or []:
            if proj.planned_features:
                excluded_planned[str(proj.id)] = [str(x).strip() for x in proj.planned_features if str(x).strip()]
            pv = list(proj.embedding or []) or deterministic_local_embedding(
                f"{proj.name} {proj.summary} {' '.join(proj.technologies or [])}"
            )
            sim = cosine_similarity(job_vec, pv)
            tech_norms = {_norm(t) for t in (proj.technologies or [])}
            impl_texts = [str(f).strip() for f in (proj.implemented_features or []) if str(f).strip()]
            impl_blob = f"{proj.summary} {' '.join(impl_texts)}".lower()
            matched = [
                t for t in all_target_skills if t in tech_norms or t in impl_blob
            ]
            req_hits = sum(1 for t in req_skills if t in tech_norms or t in impl_blob)
            relevance = round(sim * 0.5 + req_hits * 0.35 + len(matched) * 0.15, 4)
            projects.append(
                ProjectEvidenceRecord(
                    evidence_id=f"project:{proj.id}",
                    project_id=str(proj.id),
                    name=proj.name.strip(),
                    summary=proj.summary.strip(),
                    technologies=[t.strip() for t in (proj.technologies or []) if t.strip()],
                    implemented_features=impl_texts,
                    verified_metrics=[str(m).strip() for m in (proj.verified_metrics or []) if str(m).strip()],
                    repo_url=proj.repo_url,
                    live_url=proj.live_url,
                    start_date=proj.start_date,
                    end_date=proj.end_date,
                    matched_jd_skills=matched,
                    semantic_similarity=round(sim, 4),
                    relevance_score=relevance,
                )
            )
        projects.sort(key=lambda p: (-p.relevance_score, -p.semantic_similarity, p.project_id))

        experiences: list[ExperienceEvidenceRecord] = []
        for exp in profile.experiences or []:
            resp_list = [str(r).strip() for r in (exp.verified_responsibilities or []) if str(r).strip()]
            ev = list(exp.embedding or []) or deterministic_local_embedding(
                f"{exp.role} {exp.company} {' '.join(resp_list)}"
            )
            sim = cosine_similarity(job_vec, ev)
            resp_blob = f"{exp.role} {' '.join(resp_list)}".lower()
            matched = [t for t in all_target_skills if t in resp_blob]
            relevance = round(sim * 0.6 + len(matched) * 0.25, 4)
            experiences.append(
                ExperienceEvidenceRecord(
                    evidence_id=f"exp:{exp.id}",
                    experience_id=str(exp.id),
                    company=exp.company.strip(),
                    role=exp.role.strip(),
                    location=exp.location,
                    start_date=exp.start_date,
                    end_date=exp.end_date,
                    is_current=bool(exp.is_current),
                    verified_responsibilities=resp_list,
                    matched_jd_skills=matched,
                    semantic_similarity=round(sim, 4),
                    relevance_score=relevance,
                )
            )
        experiences.sort(key=lambda e: (-e.relevance_score, -e.semantic_similarity, e.experience_id))

        education: list[EducationEvidenceRecord] = []
        for item in profile.education or []:
            if isinstance(item, dict):
                edu_eid = compute_education_evidence_id(item)
                education.append(
                    EducationEvidenceRecord(
                        evidence_id=edu_eid,
                        institution=str(item.get("institution") or "").strip(),
                        degree=str(item.get("degree") or "").strip(),
                        field_of_study=item.get("field_of_study"),
                        start_year=str(item["start_year"]) if item.get("start_year") is not None else None,
                        end_year=str(item["end_year"]) if item.get("end_year") is not None else None,
                        grade_or_cgpa=str(item["grade_or_cgpa"]) if item.get("grade_or_cgpa") is not None else None,
                    )
                )

        certifications: list[CertificationEvidenceRecord] = []
        for cert in profile.certifications or []:
            if not cert.verified:
                continue
            certifications.append(
                CertificationEvidenceRecord(
                    evidence_id=f"cert:{cert.id}",
                    certification_id=str(cert.id),
                    name=cert.name.strip(),
                    issuer=cert.issuer.strip(),
                    issued_date=cert.issued_date,
                    credential_url=cert.credential_url,
                )
            )
        certifications.sort(key=lambda c: c.certification_id)

        achievements: list[AchievementEvidenceRecord] = []
        for raw_ach in profile.achievements or []:
            txt = str(raw_ach).strip() if not isinstance(raw_ach, dict) else str(raw_ach.get("title") or raw_ach.get("description") or "").strip()
            if txt:
                ach_eid = compute_achievement_evidence_id(raw_ach)
                achievements.append(AchievementEvidenceRecord(evidence_id=ach_eid, text=txt))

        req_map = map_requirements_to_evidence(req=req, profile=profile)
        fingerprint = compute_evidence_fingerprint(
            profile=profile,
            master_resume=master_resume,
            job=job,
            req=req,
            template_version=template_version,
        )

        return RetrievedCandidateEvidence(
            profile_id=profile.id,
            job_id=job.id,
            master_resume_id=master_resume.id if master_resume else None,
            evidence_fingerprint=fingerprint,
            template_version=template_version,
            full_name=profile.full_name,
            email=profile.email,
            phone=profile.phone,
            location=profile.location,
            github_url=profile.github_url,
            linkedin_url=profile.linkedin_url,
            portfolio_url=profile.portfolio_url,
            summary=profile.summary,
            identity_evidence_id=f"profile:{profile.id}",
            skills=verified_skills,
            projects=projects,
            experiences=experiences,
            education=education,
            certifications=certifications,
            achievements=achievements,
            matched_required_skills=list(req_map["matched_required"]),
            missing_required_skills=list(req_map["confirmed_missing_required"]),
            missing_evidence=list(req_map["missing_evidence"]),
            excluded_unverified_skills=excluded_unverified_skills,
            excluded_planned_features_by_project=excluded_planned,
        )
