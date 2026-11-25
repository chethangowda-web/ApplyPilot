from app.agents.resume_intelligence.ats_optimizer import (
    ATSOptimizationReport,
    _text_contains_term,
    flatten_tailored_resume_text,
)
from app.agents.resume_intelligence.evidence_retriever import RetrievedCandidateEvidence
from app.database.models import JobRequirement
from app.schemas.resume_intelligence import (
    FactualValidationReport,
    ResumeEvaluationReport,
    TailoredResumeContent,
)


class ResumeEvaluatorAgent:
    """Computes the seven approved deterministic quality & alignment metrics for tailored resumes."""

    def evaluate(
        self,
        *,
        content: TailoredResumeContent,
        evidence: RetrievedCandidateEvidence,
        req: JobRequirement | None,
        ats_report: ATSOptimizationReport,
        validation_report: FactualValidationReport,
    ) -> ResumeEvaluationReport:
        full_text = flatten_tailored_resume_text(content).lower()

        # 1. Required skill coverage
        req_skills = [str(s).strip() for s in (req.required_skills or [])] if req else []
        if req_skills:
            req_hits = sum(1 for s in req_skills if _text_contains_term(full_text, s))
            required_skill_coverage = round(req_hits / len(req_skills), 4)
        else:
            required_skill_coverage = 1.0

        # 2. Preferred skill coverage
        pref_skills = [str(s).strip() for s in (req.preferred_skills or [])] if req else []
        if pref_skills:
            pref_hits = sum(1 for s in pref_skills if _text_contains_term(full_text, s))
            preferred_skill_coverage = round(pref_hits / len(pref_skills), 4)
        else:
            preferred_skill_coverage = 1.0

        # 3. Relevant evidence coverage (projects + experiences included relative to available evidence)
        avail_proj_ids = {p.evidence_id for p in evidence.projects[:4]}
        avail_exp_ids = {e.evidence_id for e in evidence.experiences[:4]}
        used_proj_ids = {p.project_id for p in content.projects}
        used_exp_ids = {e.experience_id for e in content.experiences}
        total_avail = len(avail_proj_ids) + len(avail_exp_ids)
        if total_avail > 0:
            covered = len(avail_proj_ids & used_proj_ids) + len(avail_exp_ids & used_exp_ids)
            relevant_evidence_coverage = round(min(1.0, covered / total_avail), 4)
        else:
            relevant_evidence_coverage = 1.0

        # 4. Keyword coverage (from ATSOptimizationAgent)
        keyword_coverage = float(ats_report.keyword_coverage)

        # 5. Factual consistency score
        total_claims = len(validation_report.claims)
        if total_claims == 0:
            factual_consistency_score = 0.0
        else:
            consistent = total_claims - validation_report.unsupported_count
            factual_consistency_score = round(max(0.0, consistent / total_claims), 4)

        # 6. Readability score (summary + section structure + concise bullets + action verbs)
        readability_points = 0.0
        max_points = 4.0
        if 20 <= len(content.summary.text.strip()) <= 500:
            readability_points += 1.0
        if ats_report.has_standard_headings:
            readability_points += 1.0
        if ats_report.total_bullets > 0:
            concise_ratio = ats_report.concise_bullets / ats_report.total_bullets
            action_ratio = ats_report.action_verb_bullets / ats_report.total_bullets
            readability_points += concise_ratio
            readability_points += min(1.0, action_ratio + 0.25)
        else:
            readability_points += 1.0
        readability_score = round(min(1.0, readability_points / max_points), 4)

        # 7. PDF parsing quality score
        if ats_report.pdf_parse_passed is None:
            pdf_parsing_quality_score = 1.0 if ats_report.has_standard_headings else 0.7
        else:
            checks = 0.0
            total_pdf_checks = 4.0
            if ats_report.pdf_ats_report.get("text_extractable"):
                checks += 1.0
            if ats_report.pdf_ats_report.get("has_standard_headings"):
                checks += 1.0
            if ats_report.pdf_ats_report.get("within_page_budget", True):
                checks += 1.0
            if ats_report.pdf_parse_passed:
                checks += 1.0
            pdf_parsing_quality_score = round(checks / total_pdf_checks, 4)

        return ResumeEvaluationReport(
            required_skill_coverage=required_skill_coverage,
            preferred_skill_coverage=preferred_skill_coverage,
            relevant_evidence_coverage=relevant_evidence_coverage,
            keyword_coverage=keyword_coverage,
            factual_consistency_score=factual_consistency_score,
            readability_score=readability_score,
            pdf_parsing_quality_score=pdf_parsing_quality_score,
        )
