"""Run the 60-JD Job Intelligence evaluation benchmark and report measured metrics."""

import asyncio
import json
import uuid
from datetime import datetime, timezone
from typing import Any

from app.agents.job_intelligence.jd_analyzer import JDAnalyzerAgent
from app.database.models import CandidateProfile, CandidateSkill, Job, JobRequirement, Project
from app.services.benchmark_dataset import build_benchmark_dataset
from app.services.scoring_service import score_job_for_profile


def _reference_profile() -> CandidateProfile:
    profile = CandidateProfile(
        id=uuid.uuid4(),
        full_name="Chethan Gowda S N",
        email="chethan@example.com",
        location="Bengaluru, India",
        education=[
            {
                "institution": "Presidency University, Bengaluru",
                "degree": "B.Tech",
                "field_of_study": "Computer Science and Engineering",
                "cgpa": "8.0",
                "start_date": "Sep 2024",
                "end_date": "Aug 2028 (Expected)",
            }
        ],
    )
    profile.skills = [
        CandidateSkill(id=uuid.uuid4(), skill_name="Python", category="Languages", evidence_ref="ORBIT", verified=True),
        CandidateSkill(id=uuid.uuid4(), skill_name="TypeScript", category="Languages", evidence_ref="ORBIT", verified=True),
        CandidateSkill(id=uuid.uuid4(), skill_name="SQL", category="Languages", evidence_ref="ORBIT", verified=True),
        CandidateSkill(id=uuid.uuid4(), skill_name="FastAPI", category="Backend", evidence_ref="ORBIT", verified=True),
        CandidateSkill(id=uuid.uuid4(), skill_name="LangGraph", category="AI", evidence_ref="ORBIT", verified=True),
        CandidateSkill(id=uuid.uuid4(), skill_name="RAG", category="AI", evidence_ref="Citewise", verified=True),
        CandidateSkill(id=uuid.uuid4(), skill_name="PostgreSQL", category="DB", evidence_ref="ORBIT", verified=True),
        CandidateSkill(id=uuid.uuid4(), skill_name="pgvector", category="DB", evidence_ref="Citewise", verified=True),
        CandidateSkill(id=uuid.uuid4(), skill_name="Redis", category="DB", evidence_ref="ORBIT", verified=True),
        CandidateSkill(id=uuid.uuid4(), skill_name="Docker", category="Infra", evidence_ref="Citewise", verified=True),
        CandidateSkill(id=uuid.uuid4(), skill_name="Git", category="Tools", evidence_ref="Codec", verified=True),
        CandidateSkill(id=uuid.uuid4(), skill_name="REST APIs", category="Backend", evidence_ref="ORBIT", verified=True),
    ]
    profile.projects = [
        Project(
            id=uuid.uuid4(),
            name="ORBIT",
            summary="Multi-agent task system using LangGraph, FastAPI, PostgreSQL, Redis, Next.js.",
            technologies=["Python", "FastAPI", "LangGraph", "PostgreSQL", "Redis", "Next.js"],
            implemented_features=["LangGraph supervisor", "5 specialist agents", "REST APIs"],
            planned_features=[],
        ),
        Project(
            id=uuid.uuid4(),
            name="Citewise",
            summary="Secure Enterprise RAG Chatbot with FastAPI and PostgreSQL/pgvector.",
            technologies=["Python", "FastAPI", "PostgreSQL", "pgvector", "React", "Docker"],
            implemented_features=["Hybrid search RAG", "Golden-set eval suite"],
            planned_features=[],
        ),
    ]
    return profile


async def run_benchmark() -> dict[str, Any]:
    dataset = build_benchmark_dataset()
    analyzer = JDAnalyzerAgent()
    profile = _reference_profile()
    now = datetime.now(timezone.utc)

    req_tp = req_fp = req_fn = 0
    pref_tp = pref_fp = pref_fn = 0
    exp_exact = exp_total = 0
    edu_exact = edu_total = 0
    elig_exact = 0
    inj_tp = inj_fp = inj_fn = 0
    grounded_items = total_evidence_items = 0
    consistent_scores = 0

    for item in dataset:
        ext = await analyzer.analyze(
            job_title=item.job_title,
            company=item.company,
            raw_jd=item.raw_jd,
            location=item.location,
            workplace_type=item.workplace_type,
            commitment=item.commitment,
            use_llm=False,
        )

        # Required & preferred skill classification metrics
        pred_req = {s.value.lower() for s in ext.required_skills}
        gold_req = {s.lower() for s in item.expected_required_skills}
        req_tp += len(pred_req & gold_req)
        req_fp += len(pred_req - gold_req)
        req_fn += len(gold_req - pred_req)

        pred_pref = {s.value.lower() for s in ext.preferred_skills}
        gold_pref = {s.lower() for s in item.expected_preferred_skills}
        pref_tp += len(pred_pref & gold_pref)
        pref_fp += len(pred_pref - gold_pref)
        pref_fn += len(gold_pref - pred_pref)

        # Experience extraction exactness when expected
        if item.expected_exp_min is not None:
            exp_total += 1
            if ext.experience_min == item.expected_exp_min:
                exp_exact += 1

        # Education extraction recall when expected
        if item.expected_education:
            edu_total += 1
            pred_edu = {e.value for e in ext.education_requirements}
            if set(item.expected_education).issubset(pred_edu):
                edu_exact += 1

        # Evidence grounding
        for ev_item in [*ext.required_skills, *ext.preferred_skills]:
            total_evidence_items += 1
            if ev_item.verified_in_jd and ev_item.evidence_quote:
                grounded_items += 1

        # Prompt injection accuracy
        if ext.prompt_injection_detected and item.expected_prompt_injection:
            inj_tp += 1
        elif ext.prompt_injection_detected and not item.expected_prompt_injection:
            inj_fp += 1
        elif not ext.prompt_injection_detected and item.expected_prompt_injection:
            inj_fn += 1

        # Eligibility & deterministic scoring consistency
        job = Job(
            id=uuid.uuid4(),
            canonical_url=f"https://benchmark.local/{item.jd_id}",
            dedup_key=item.jd_id,
            content_checksum=item.jd_id,
            company=item.company,
            role_title=item.job_title,
            location=item.location,
            remote_policy=ext.remote_policy,
            is_remote=(ext.remote_policy == "remote"),
            employment_type=ext.employment_type,
            raw_jd=item.raw_jd,
            posted_at=now,
            first_seen_at=now,
            last_seen_at=now,
            status="active",
        )
        req = JobRequirement(
            job_id=job.id,
            role_category=ext.role_category,
            required_skills=[s.value for s in ext.required_skills],
            preferred_skills=[s.value for s in ext.preferred_skills],
            experience_min=ext.experience_min,
            experience_max=ext.experience_max,
            education_requirements=[e.value for e in ext.education_requirements],
            uncertainties=ext.uncertainties,
            prompt_injection_detected=ext.prompt_injection_detected,
        )
        s1 = score_job_for_profile(job=job, req=req, profile=profile, now=now)
        s2 = score_job_for_profile(job=job, req=req, profile=profile, now=now)
        if s1["score"] == s2["score"] and s1["eligible"] == s2["eligible"]:
            consistent_scores += 1
        if s1["eligible"] == item.expected_eligible:
            elig_exact += 1

    def _prf(tp: int, fp: int, fn: int) -> dict[str, float]:
        p = tp / (tp + fp) if (tp + fp) else 1.0
        r = tp / (tp + fn) if (tp + fn) else 1.0
        f1 = (2 * p * r / (p + r)) if (p + r) else 0.0
        return {"precision": round(p, 4), "recall": round(r, 4), "f1": round(f1, 4)}

    return {
        "dataset_size": len(dataset),
        "required_skills_metrics": _prf(req_tp, req_fp, req_fn),
        "preferred_skills_metrics": _prf(pref_tp, pref_fp, pref_fn),
        "experience_extraction_accuracy": round(exp_exact / exp_total, 4) if exp_total else 0.0,
        "education_extraction_accuracy": round(edu_exact / edu_total, 4) if edu_total else 0.0,
        "evidence_grounding_rate": round(grounded_items / total_evidence_items, 4) if total_evidence_items else 0.0,
        "eligibility_accuracy": round(elig_exact / len(dataset), 4),
        "scoring_consistency_rate": round(consistent_scores / len(dataset), 4),
        "prompt_injection_detection": _prf(inj_tp, inj_fp, inj_fn),
    }


if __name__ == "__main__":
    print(json.dumps(asyncio.run(run_benchmark()), indent=2))
