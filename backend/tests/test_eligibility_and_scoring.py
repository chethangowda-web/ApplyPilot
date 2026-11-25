from datetime import datetime, timezone
from app.database.models import CandidateProfile, CandidateSkill, Job, JobRequirement, Project
from app.services.scoring_service import score_job_for_profile


def _make_profile() -> CandidateProfile:
    p = CandidateProfile(
        full_name="Chethan Gowda S N",
        email="chethan@example.com",
        location="Bengaluru, India",
        education=[
            {
                "institution": "Presidency University",
                "degree": "B.Tech",
                "field_of_study": "Computer Science and Engineering",
                "cgpa": "8.0",
                "start_date": "Sep 2024",
                "end_date": "Aug 2028 (Expected)",
            }
        ],
    )
    p.skills = [
        CandidateSkill(skill_name="Python", category="Languages", verified=True),
        CandidateSkill(skill_name="FastAPI", category="Backend", verified=True),
        CandidateSkill(skill_name="LangGraph", category="AI", verified=True),
        CandidateSkill(skill_name="PostgreSQL", category="Databases", verified=True),
    ]
    p.projects = [
        Project(
            name="ORBIT",
            summary="Multi-agent task system using LangGraph and FastAPI.",
            technologies=["Python", "FastAPI", "LangGraph", "PostgreSQL"],
            implemented_features=["LangGraph supervisor", "PostgreSQL tools"],
            planned_features=["Kubernetes auto-scaling"],
        )
    ]
    return p


def test_strong_eligible_internship_scores_high():
    profile = _make_profile()
    now = datetime.now(timezone.utc)
    job = Job(
        canonical_url="https://boards.greenhouse.io/acme/jobs/1",
        dedup_key="k1",
        content_checksum="c1",
        company="Acme AI",
        role_title="AI Engineer Intern",
        location="Bengaluru, India",
        remote_policy="hybrid",
        is_remote=False,
        employment_type="internship",
        raw_jd="Build Python, FastAPI, and LangGraph agents with PostgreSQL. B.Tech in CS/AI. 0-1 years.",
        posted_at=now,
        first_seen_at=now,
        last_seen_at=now,
        status="active",
    )
    req = JobRequirement(
        role_category="AI/ML Engineer",
        required_skills=["Python", "FastAPI", "LangGraph", "PostgreSQL"],
        preferred_skills=["Docker"],
        experience_min=0.0,
        experience_max=1.0,
        education_requirements=["B.E./B.Tech/Bachelor's"],
        internship_eligible=True,
    )
    res = score_job_for_profile(job=job, req=req, profile=profile, now=now)
    assert res["eligible"] is True
    assert res["hard_constraints_passed"] is True
    assert res["score"] >= 85.0
    assert res["recommendation"] == "priority_application"
    assert res["missing_required_skills"] == []
    assert res["preferred_gaps"] == ["Docker"]


def test_hard_experience_and_geography_blocks_override_keywords():
    profile = _make_profile()
    now = datetime.now(timezone.utc)
    # 5+ years required + US onsite -> ineligible despite 100% skill match
    job = Job(
        canonical_url="https://boards.greenhouse.io/acme/jobs/2",
        dedup_key="k2",
        content_checksum="c2",
        company="Acme AI",
        role_title="AI Engineer",
        location="San Francisco, CA",
        remote_policy="onsite",
        is_remote=False,
        employment_type="full_time",
        raw_jd="Python, FastAPI, LangGraph. Minimum 5 years experience.",
        posted_at=now,
        first_seen_at=now,
        last_seen_at=now,
        status="active",
    )
    req = JobRequirement(
        role_category="AI/ML Engineer",
        required_skills=["Python", "FastAPI", "LangGraph"],
        experience_min=5.0,
    )
    res = score_job_for_profile(job=job, req=req, profile=profile, now=now)
    assert res["eligible"] is False
    assert res["hard_constraints_passed"] is False
    assert res["recommendation"] == "ineligible"


def test_missing_information_never_treated_as_confirmed_eligible():
    profile = _make_profile()
    now = datetime.now(timezone.utc)
    job = Job(
        canonical_url="https://example.com/jobs/3",
        dedup_key="k3",
        content_checksum="c3",
        company="Unknown Co",
        role_title="Software Engineer",
        location=None,
        remote_policy="unknown",
        is_remote=None,
        employment_type="unknown",
        raw_jd="",
        first_seen_at=now,
        last_seen_at=now,
        status="active",
    )
    req = JobRequirement(role_category="Software Engineer")
    res = score_job_for_profile(job=job, req=req, profile=profile, now=now)
    assert res["eligible"] is False
    assert res["recommendation"] == "manual_review"
    assert "missing_job_description" in res["uncertainties"]
    assert "location_and_remote_unspecified" in res["uncertainties"]


def test_planned_project_features_not_credited_and_final_semester_checked():
    profile = _make_profile()
    now = datetime.now(timezone.utc)
    job = Job(
        canonical_url="https://example.com/jobs/4",
        dedup_key="k4",
        content_checksum="c4",
        company="Grad Only Co",
        role_title="AI Engineer Intern",
        location="Remote",
        remote_policy="remote",
        is_remote=True,
        employment_type="internship",
        raw_jd="Final semester B.Tech CS students only. Kubernetes required.",
        posted_at=now,
        first_seen_at=now,
        last_seen_at=now,
        status="active",
    )
    req = JobRequirement(
        role_category="AI/ML Engineer",
        required_skills=["Kubernetes"],
        education_requirements=["B.E./B.Tech/Bachelor's"],
        experience_min=0.0,
    )
    res = score_job_for_profile(job=job, req=req, profile=profile, now=now)
    # Kubernetes is ONLY in planned_features, NOT implemented_features/skills
    assert "Kubernetes" in res["missing_required_skills"]
    # 2028 graduation fails explicit final-semester check
    assert res["eligible"] is False
    assert res["recommendation"] == "ineligible"
