from datetime import date
from pathlib import Path
import uuid
from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.database.models import (
    AutoTailorDailyQuota,
    CandidateProfile,
    Job,
    JobSource,
    Resume,
    ResumeGenerationJob,
    ResumeJobMapping,
    User,
)
from app.schemas.resume_intelligence import (
    AttributedTextItem,
    FactualValidationReport,
    ResumeEvaluationReport,
    TailoredResumeContent,
    TailoringDiff,
    ValidatedClaim,
)

BACKEND_DIR = Path(__file__).resolve().parents[1]


def _seed_user_profile_and_job(session: Session) -> tuple[User, CandidateProfile, Job]:
    user = User(email="stage1@example.com", hashed_password="hashed-password")
    session.add(user)
    session.flush()

    profile = CandidateProfile(user_id=user.id, full_name="Stage One", email=user.email)
    session.add(profile)
    session.flush()

    source = JobSource(source_name="gh-stage1", connector_type="greenhouse")
    session.add(source)
    session.flush()

    job = Job(
        source_id=source.id,
        source_job_id="101",
        canonical_url="https://boards.greenhouse.io/stage1/jobs/101",
        dedup_key="d" * 64,
        content_checksum="c" * 64,
        company="StageOne Co",
        role_title="AI Engineer Intern",
        raw_jd="Python, FastAPI, LangGraph",
    )
    session.add(job)
    session.flush()
    return user, profile, job


def test_stage1_settings_and_threshold_separation(monkeypatch):
    for key in (
        "MIN_JOB_MATCH_SCORE",
        "AUTO_TAILOR_ENABLED",
        "AUTO_TAILOR_DAILY_LIMIT",
        "AUTO_TAILOR_MIN_SCORE",
    ):
        monkeypatch.delenv(key, raising=False)

    defaults = Settings(_env_file=None)
    assert defaults.auto_tailor_enabled is True
    assert defaults.auto_tailor_daily_limit == 5
    assert defaults.auto_tailor_min_score == 75
    assert defaults.min_job_match_score == 75

    monkeypatch.setenv("MIN_JOB_MATCH_SCORE", "65")
    monkeypatch.setenv("AUTO_TAILOR_ENABLED", "false")
    monkeypatch.setenv("AUTO_TAILOR_DAILY_LIMIT", "8")
    monkeypatch.setenv("AUTO_TAILOR_MIN_SCORE", "82")

    cfg = Settings(_env_file=None)
    assert cfg.min_job_match_score == 65
    assert cfg.auto_tailor_enabled is False
    assert cfg.auto_tailor_daily_limit == 8
    assert cfg.auto_tailor_min_score == 82


def test_migration_0004_upgrade_downgrade_and_constraints_sqlite(tmp_path):
    db_file = tmp_path / "m4_stage1.db"
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_file}")

    command.upgrade(cfg, "head")
    engine = create_engine(f"sqlite:///{db_file}")
    insp = inspect(engine)
    tables = set(insp.get_table_names())
    assert {"auto_tailor_daily_quotas", "resume_generation_jobs", "resume_job_mappings"}.issubset(tables)

    resume_cols = {c["name"] for c in insp.get_columns("resumes")}
    assert {
        "master_resume_id",
        "generation_status",
        "validation_report",
        "evaluation_report",
        "structured_content",
    }.issubset(resume_cols)

    with Session(engine) as session:
        user, profile, job = _seed_user_profile_and_job(session)
        master = Resume(
            profile_id=profile.id,
            is_master=True,
            original_filename="master.pdf",
            content_type="application/pdf",
            file_size_bytes=512,
            version_label="master-v1",
            sha256_checksum="m" * 64,
        )
        session.add(master)
        session.flush()

        quota = AutoTailorDailyQuota(user_id=user.id, quota_date=date(2026, 11, 26), reserved_count=1)
        session.add(quota)
        session.flush()

        # Logical idempotency shared across auto & manual non-forced requests
        idem = "i" * 64
        gen1 = ResumeGenerationJob(
            user_id=user.id,
            profile_id=profile.id,
            job_id=job.id,
            master_resume_id=master.id,
            trigger_mode="auto",
            evidence_fingerprint="f" * 64,
            quota_date=date(2026, 11, 26),
            idempotency_key=idem,
            status="queued",
            thread_id="tailor-thread-1",
        )
        session.add(gen1)
        session.commit()

        dup_gen = ResumeGenerationJob(
            user_id=user.id,
            profile_id=profile.id,
            job_id=job.id,
            master_resume_id=master.id,
            trigger_mode="manual",
            evidence_fingerprint="f" * 64,
            idempotency_key=idem,
            status="queued",
            thread_id="tailor-thread-2",
        )
        session.add(dup_gen)
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()

        # Canonical ResumeJobMapping integrity checks
        r1 = Resume(
            profile_id=profile.id,
            master_resume_id=master.id,
            is_master=False,
            original_filename="tailored_v1.pdf",
            content_type="application/pdf",
            file_size_bytes=800,
            version_label="tailored-v1",
            pdf_path="/tmp/t1.pdf",
            sha256_checksum="1" * 64,
            template_version="ats-classic-v1",
            generation_status="completed",
            validation_passed=True,
        )
        r2 = Resume(
            profile_id=profile.id,
            master_resume_id=master.id,
            is_master=False,
            original_filename="tailored_v2.pdf",
            content_type="application/pdf",
            file_size_bytes=820,
            version_label="tailored-v2",
            pdf_path="/tmp/t2.pdf",
            sha256_checksum="2" * 64,
            template_version="ats-classic-v1",
            generation_status="completed",
            validation_passed=True,
        )
        session.add_all([r1, r2])
        session.flush()

        m1 = ResumeJobMapping(
            resume_id=r1.id,
            job_id=job.id,
            profile_id=profile.id,
            master_resume_id=master.id,
            run_id="run-1",
            version_number=1,
            is_active=True,
            evidence_fingerprint="f" * 64,
            status="completed",
            approval_status="pending_review",
        )
        session.add(m1)
        session.commit()

        # Cannot have two active completed mappings for the same (job_id, profile_id)
        m2_conflict = ResumeJobMapping(
            resume_id=r2.id,
            job_id=job.id,
            profile_id=profile.id,
            master_resume_id=master.id,
            run_id="run-2",
            version_number=2,
            is_active=True,
            evidence_fingerprint="f" * 64,
            status="completed",
            approval_status="pending_review",
        )
        session.add(m2_conflict)
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()

        # Cannot mark failed mapping as approved
        m_bad_approval = ResumeJobMapping(
            resume_id=None,
            job_id=job.id,
            profile_id=profile.id,
            master_resume_id=master.id,
            run_id="run-3",
            version_number=3,
            is_active=False,
            evidence_fingerprint="f" * 64,
            status="validation_failed",
            approval_status="approved",
        )
        session.add(m_bad_approval)
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()

    engine.dispose()
    command.downgrade(cfg, "0003_milestone_3")
    engine_down = create_engine(f"sqlite:///{db_file}")
    insp_down = inspect(engine_down)
    assert "resume_job_mappings" not in set(insp_down.get_table_names())
    assert "resume_generation_jobs" not in set(insp_down.get_table_names())
    assert "auto_tailor_daily_quotas" not in set(insp_down.get_table_names())
    assert "generation_status" not in {c["name"] for c in insp_down.get_columns("resumes")}
    engine_down.dispose()


def test_migration_0004_offline_postgres_sql(capsys):
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    cfg.set_main_option("sqlalchemy.url", "postgresql+psycopg://user:pass@localhost:5432/applypilot")

    command.upgrade(cfg, "0004_milestone_4", sql=True)
    sql_out = capsys.readouterr().out
    assert "CREATE TABLE auto_tailor_daily_quotas" in sql_out
    assert "CREATE TABLE resume_generation_jobs" in sql_out
    assert "CREATE TABLE resume_job_mappings" in sql_out
    assert "uq_resume_gen_jobs_active_idem" in sql_out
    assert "uq_resume_job_mapping_active_completed" in sql_out
    assert "uq_resume_job_mapping_version" in sql_out


def test_stage1_pydantic_schemas():
    content = TailoredResumeContent(
        full_name="Chethan Gowda",
        email="chethan@example.com",
        summary=AttributedTextItem(text="Python & LangGraph engineer.", evidence_ids=[f"skill:{uuid.uuid4()}"]),
    )
    assert content.section_order[0] == "summary"

    diff = TailoringDiff()
    assert diff.skills_reordered == []

    report = FactualValidationReport(
        claims=[
            ValidatedClaim(
                claim_id="c1",
                category="skill",
                claim_text="Python",
                classification="supported_fact",
                matched_evidence_ids=["skill:1"],
            )
        ],
        supported_count=1,
        deterministic_passed=True,
        validation_passed=True,
    )
    assert report.validation_passed is True

    eval_report = ResumeEvaluationReport(
        required_skill_coverage=1.0,
        preferred_skill_coverage=0.5,
        relevant_evidence_coverage=0.9,
        keyword_coverage=0.85,
        factual_consistency_score=1.0,
        readability_score=0.9,
        pdf_parsing_quality_score=1.0,
    )
    assert "not a guaranteed ATS score" in eval_report.disclaimer
