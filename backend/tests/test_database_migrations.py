from pathlib import Path
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import Session

from app.core.logging import redact_value
from app.database.models import CandidateProfile, CandidateSkill, Project, Resume, User

BACKEND_DIR = Path(__file__).resolve().parents[1]


def test_alembic_upgrade_and_downgrade_sqlite(tmp_path):
    db_file = tmp_path / "test_mig.db"
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_file}")

    command.upgrade(cfg, "head")
    engine = create_engine(f"sqlite:///{db_file}")
    tables = set(inspect(engine).get_table_names())
    expected = {
        "users",
        "candidate_profiles",
        "candidate_skills",
        "projects",
        "experiences",
        "certifications",
        "resumes",
        "audit_logs",
    }
    assert expected.issubset(tables)

    # Verify ORM CRUD round-trip on migrated schema
    with Session(engine) as session:
        user = User(email="test@example.com", hashed_password="hashed-value")
        session.add(user)
        session.flush()
        profile = CandidateProfile(
            user_id=user.id,
            full_name="Test Candidate",
            email="test@example.com",
        )
        session.add(profile)
        session.flush()
        session.add(CandidateSkill(profile_id=profile.id, skill_name="Python", category="Languages"))
        session.add(Project(profile_id=profile.id, name="ORBIT", summary="Multi-agent app", technologies=["Python"]))
        session.add(
            Resume(
                profile_id=profile.id,
                is_master=True,
                original_filename="master.pdf",
                content_type="application/pdf",
                file_size_bytes=1024,
                version_label="master-v1",
                sha256_checksum="a" * 64,
            )
        )
        session.commit()
        assert session.query(User).count() == 1
        assert session.query(Resume).count() == 1

    engine.dispose()
    command.downgrade(cfg, "base")
    engine2 = create_engine(f"sqlite:///{db_file}")
    assert expected.isdisjoint(set(inspect(engine2).get_table_names()))
    engine2.dispose()


def test_alembic_offline_postgresql_sql(capsys):
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    cfg.set_main_option("sqlalchemy.url", "postgresql+psycopg://user:pass@localhost:5432/applypilot")

    command.upgrade(cfg, "head", sql=True)
    sql_out = capsys.readouterr().out
    assert "CREATE EXTENSION IF NOT EXISTS vector;" in sql_out
    assert "CREATE TABLE users" in sql_out
    assert "VECTOR(768)" in sql_out
    assert "JSONB" in sql_out


def test_logging_redacts_secrets_and_pii():
    redacted = redact_value(
        {
            "password": "my-secret-password",
            "access_token": "jwt-token",
            "note": "Contact user@example.com with Bearer abc.def.ghi",
        }
    )
    assert redacted["password"] == "[REDACTED]"
    assert redacted["access_token"] == "[REDACTED]"
    assert "user@example.com" not in redacted["note"]
    assert "abc.def.ghi" not in redacted["note"]
