import uuid
from datetime import datetime, timezone
from pathlib import Path
from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import create_engine, inspect

from app.database.models import (
    CandidateProfile,
    CandidateSkill,
    Certification,
    Experience,
    Job,
    JobRequirement,
    Project,
)
from app.services.scoring_service import score_job_for_profile
from app.services.semantic_matching_service import deterministic_local_embedding, map_requirements_to_evidence

BACKEND_DIR = Path(__file__).resolve().parents[1]


def test_migration_0003_upgrade_and_downgrade_sqlite(tmp_path):
    db_file = tmp_path / "m3_mig.db"
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_file}")

    command.upgrade(cfg, "head")
    engine = create_engine(f"sqlite:///{db_file}")
    insp = inspect(engine)
    tables = set(insp.get_table_names())
    assert {"workflow_runs", "job_analysis_history"}.issubset(tables)
    req_cols = {c["name"] for c in insp.get_columns("job_requirements")}
    assert {"graduation_criteria", "mandatory_qualifications", "evidence_items", "extraction_mode"}.issubset(req_cols)
    match_cols = {c["name"] for c in insp.get_columns("job_matches")}
    assert {"evidence_mapping", "missing_evidence", "eligibility_blockers", "confidence"}.issubset(match_cols)
    engine.dispose()

    command.downgrade(cfg, "0002_milestone_2")
    engine2 = create_engine(f"sqlite:///{db_file}")
    tables_after = set(inspect(engine2).get_table_names())
    assert {"workflow_runs", "job_analysis_history"}.isdisjoint(tables_after)
    engine2.dispose()


def test_evidence_mapping_separates_missing_evidence_from_confirmed_missing():
    profile = CandidateProfile(
        id=uuid.uuid4(),
        full_name="Chethan",
        email="chethan@example.com",
        education=[{"institution": "Presidency", "degree": "B.Tech", "field_of_study": "CSE"}],
    )
    profile.skills = [
        CandidateSkill(id=uuid.uuid4(), skill_name="Python", category="Lang", evidence_ref="ORBIT", verified=True),
        CandidateSkill(id=uuid.uuid4(), skill_name="Redis", category="DB", evidence_ref=None, verified=True),
        CandidateSkill(id=uuid.uuid4(), skill_name="Docker", category="Infra", verified=False),
    ]
    profile.projects = [
        Project(
            id=uuid.uuid4(),
            name="ORBIT",
            summary="Multi-agent FastAPI workflow",
            technologies=["Python", "FastAPI"],
            implemented_features=["LangGraph routing"],
            planned_features=["MCP integration"],
        )
    ]
    profile.experiences = [
        Experience(
            id=uuid.uuid4(),
            company="Codec",
            role="Intern",
            start_date="Feb 2026",
            verified_responsibilities=["Built REST APIs with Git"],
        )
    ]
    profile.certifications = [
        Certification(id=uuid.uuid4(), name="AWS Cloud Foundations", issuer="AWS", verified=True)
    ]

    req = JobRequirement(
        role_category="AI/ML Engineer",
        required_skills=["Python", "Redis", "Docker", "MCP", "Kubernetes"],
        preferred_skills=["REST APIs", "AWS Cloud Foundations"],
    )

    ev = map_requirements_to_evidence(req=req, profile=profile)
    # Python matched with concrete skill + project backing
    assert "Python" in ev["matched_required"]
    # Redis matched as verified skill, but flagged in missing_evidence because it lacks project/exp/evidence_ref backing
    assert "Redis" in ev["matched_required"]
    assert any(m["item"] == "Redis" for m in ev["missing_evidence"])
    # Docker (verified=False) and MCP (planned_only) go to missing_evidence, NOT matched_required
    assert "Docker" not in ev["matched_required"]
    assert any(m["item"] == "Docker" and "unverified" in m["reason"] for m in ev["missing_evidence"])
    assert any(m["item"] == "MCP" and "planned" in m["reason"] for m in ev["missing_evidence"])
    # Kubernetes is completely absent -> confirmed_missing_required (and not in missing_evidence)
    assert "Kubernetes" in ev["confirmed_missing_required"]
    assert not any(m["item"] == "Kubernetes" for m in ev["missing_evidence"])
    # Preferred matches trace to experience & certification
    assert "REST APIs" in ev["matched_preferred"]
    assert "AWS Cloud Foundations" in ev["matched_preferred"]


@pytest.mark.asyncio
async def test_explainable_score_blocks_senior_role_even_with_high_semantic_similarity():
    now = datetime.now(timezone.utc)
    profile = CandidateProfile(
        id=uuid.uuid4(),
        full_name="Chethan",
        email="chethan@example.com",
        education=[{"institution": "Presidency", "degree": "B.Tech", "field_of_study": "CSE"}],
    )
    profile.skills = [
        CandidateSkill(id=uuid.uuid4(), skill_name="Python", category="Lang", evidence_ref="ORBIT", verified=True),
        CandidateSkill(id=uuid.uuid4(), skill_name="LangGraph", category="AI", evidence_ref="ORBIT", verified=True),
    ]
    shared_text = "Python LangGraph multi-agent FastAPI RAG"
    profile.projects = [
        Project(
            id=uuid.uuid4(),
            name="ORBIT",
            summary=shared_text,
            technologies=["Python", "LangGraph", "FastAPI"],
            implemented_features=["Multi-agent RAG"],
            embedding=deterministic_local_embedding(shared_text),
        )
    ]

    job = Job(
        id=uuid.uuid4(),
        canonical_url="https://example.com/senior-ai",
        dedup_key="k",
        content_checksum="c",
        company="Acme",
        role_title="Senior AI Engineer",
        location="Bengaluru, India",
        remote_policy="hybrid",
        employment_type="full_time",
        raw_jd=f"{shared_text}. Minimum 5 years experience.",
        jd_embedding=deterministic_local_embedding(shared_text),
        first_seen_at=now,
        last_seen_at=now,
        status="active",
    )
    req = JobRequirement(
        role_category="AI/ML Engineer",
        required_skills=["Python", "LangGraph"],
        experience_min=5.0,
    )
    res = score_job_for_profile(job=job, req=req, profile=profile, now=now)
    assert res["semantic_similarity"] > 0.8
    assert res["eligible"] is False
    assert res["hard_constraints_passed"] is False
    assert res["recommendation"] == "ineligible"
    assert "senior_or_mid_level_role_excluded" in res["eligibility_blockers"]
    assert "not a hiring probability" in res["explanation"]
