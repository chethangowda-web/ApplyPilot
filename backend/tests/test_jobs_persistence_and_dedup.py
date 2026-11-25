from datetime import datetime, timedelta, timezone
from pathlib import Path
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import Session

from app.database.models import Job, JobMatch, JobRequirement, JobSource
from app.schemas.workflow import NormalizedJob
from app.services.deduplication_service import mark_job_expired, upsert_discovered_job

BACKEND_DIR = Path(__file__).resolve().parents[1]


def test_migration_0002_upgrade_and_downgrade_sqlite(tmp_path):
    db_file = tmp_path / "m2_mig.db"
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_file}")

    command.upgrade(cfg, "head")
    engine = create_engine(f"sqlite:///{db_file}")
    tables = set(inspect(engine).get_table_names())
    assert {"job_sources", "jobs", "job_requirements", "job_matches"}.issubset(tables)
    engine.dispose()

    command.downgrade(cfg, "0001_milestone_1")
    engine2 = create_engine(f"sqlite:///{db_file}")
    tables_after = set(inspect(engine2).get_table_names())
    assert {"job_sources", "jobs", "job_requirements", "job_matches"}.isdisjoint(tables_after)
    assert "users" in tables_after
    engine2.dispose()


def test_deduplication_reposts_and_expiry_preserves_history(tmp_path):
    db_file = tmp_path / "dedup.db"
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_file}")
    command.upgrade(cfg, "head")

    engine = create_engine(f"sqlite:///{db_file}")
    t0 = datetime(2026, 11, 20, 10, 0, tzinfo=timezone.utc)
    t1 = t0 + timedelta(days=2)
    t2 = t0 + timedelta(days=5)

    with Session(engine) as db:
        src_gh = JobSource(source_name="greenhouse:acme", connector_type="greenhouse")
        src_cs = JobSource(source_name="career_site:acme", connector_type="career_site")
        db.add_all([src_gh, src_cs])
        db.flush()

        n1 = NormalizedJob(
            source="greenhouse",
            external_job_id="1001",
            canonical_url="https://boards.greenhouse.io/acme/jobs/1001?utm_source=test",
            company="Acme AI",
            job_title="AI Engineer Intern",
            location="Bengaluru, India",
            employment_type="internship",
            required_skills=["Python", "LangGraph"],
            description="Build LangGraph agents.",
            posted_at=t0,
            discovered_at=t0,
        )
        job1, out1 = upsert_discovered_job(db, source=src_gh, normalized=n1, seen_at=t0)
        assert out1 == "created"
        assert job1.first_seen_at.replace(tzinfo=None) == t0.replace(tzinfo=None)

        # Cross-source duplicate via normalized URL tracking stripping & similarity
        n_dup = NormalizedJob(
            source="career_site",
            external_job_id="cs-9",
            canonical_url="https://acme.ai/careers/ai-engineer-intern",
            company="Acme AI",
            job_title="AI Engineer Intern",
            location="Bengaluru, India",
            description="Build LangGraph agents.",
            posted_at=t0,
            discovered_at=t1,
        )
        job_dup, out_dup = upsert_discovered_job(db, source=src_cs, normalized=n_dup, seen_at=t1)
        assert out_dup == "duplicate"
        assert job_dup.id == job1.id
        assert len(job_dup.source_provenance["alternates"]) == 1

        # Expire job -> preserves record & JobRequirement
        mark_job_expired(db, job1)
        assert job1.status == "expired"
        assert db.query(JobRequirement).count() == 1

        # Reposted with newer date & updated JD
        n_repost = NormalizedJob(
            source="greenhouse",
            external_job_id="1001",
            canonical_url="https://boards.greenhouse.io/acme/jobs/1001",
            company="Acme AI",
            job_title="AI Engineer Intern",
            location="Bengaluru, India",
            description="Build LangGraph agents with FastAPI.",
            posted_at=t2,
            discovered_at=t2,
        )
        job_rep, out_rep = upsert_discovered_job(db, source=src_gh, normalized=n_repost, seen_at=t2)
        assert out_rep == "reposted"
        assert job_rep.id == job1.id
        assert job_rep.repost_count == 1
        assert job_rep.status == "active"
        assert db.query(Job).count() == 1
        assert JobMatch.__tablename__ == "job_matches"
        db.commit()

    engine.dispose()
