from datetime import datetime, timedelta, timezone
from pathlib import Path
import uuid

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.security import (
    decrypt_sensitive_answer,
    encrypt_sensitive_answer,
    rotate_sensitive_answer_ciphertext,
)
from app.database.models import (
    Application,
    ApplicationAnswer,
    ApprovalRecord,
    AuditLog,
    CandidateProfile,
    Job,
    JobSource,
    Resume,
    User,
)
from app.schemas.applications import (
    ALLOWED_APPLICATION_TRANSITIONS,
    compute_answers_hash,
    compute_approval_snapshot_hash,
    validate_application_transition,
)

BACKEND_DIR = Path(__file__).resolve().parents[1]


def _seed_user_job_resume(session: Session, *, suffix: str = "1") -> tuple[User, Job, Resume]:
    user = User(email=f"m5-stage1-{suffix}@example.com", hashed_password="hashed-password")
    session.add(user)
    session.flush()

    profile = CandidateProfile(user_id=user.id, full_name=f"M5 User {suffix}", email=user.email)
    session.add(profile)
    session.flush()

    source = JobSource(source_name=f"gh-m5-{suffix}", connector_type="greenhouse")
    session.add(source)
    session.flush()

    job = Job(
        source_id=source.id,
        source_job_id=f"job-{suffix}",
        canonical_url=f"https://boards.greenhouse.io/m5/jobs/{suffix}",
        dedup_key=(suffix * 64)[:64],
        content_checksum=(suffix * 64)[:64],
        company="M5 Co",
        role_title="AI Engineer",
        raw_jd="Python, FastAPI, PostgreSQL",
    )
    session.add(job)
    session.flush()

    resume = Resume(
        profile_id=profile.id,
        is_master=False,
        original_filename=f"tailored-{suffix}.pdf",
        content_type="application/pdf",
        file_size_bytes=512,
        version_label=f"tailored-{suffix}",
        sha256_checksum=("a" * 63 + suffix)[:64],
    )
    session.add(resume)
    session.flush()
    return user, job, resume


def test_stage1_settings_encryption_and_retention_defaults(monkeypatch):
    for key in (
        "ENVIRONMENT",
        "SECRET_KEY",
        "ENCRYPTION_KEY",
        "ENCRYPTION_KEY_VERSION",
        "ENCRYPTION_PREVIOUS_KEYS",
        "APPROVAL_TTL_HOURS",
        "APPLICATION_ARTIFACT_RETENTION_DAYS",
    ):
        monkeypatch.delenv(key, raising=False)

    defaults = Settings(_env_file=None, encryption_key="")
    assert defaults.encryption_key_version == "v1"
    assert defaults.encryption_previous_keys == ""
    assert defaults.approval_ttl_hours == 24
    assert defaults.application_artifact_retention_days == 14

    with pytest.raises(ValueError, match="ENCRYPTION_KEY"):
        defaults.validate_encryption_configured()


def test_sensitive_answer_encryption_aad_and_key_rotation():
    app_id = str(uuid.uuid4())
    question_key = "work_authorization_ssn_last4"
    cfg_v1 = Settings(
        _env_file=None,
        encryption_key="primary-secret-key-v1",
        encryption_key_version="v1",
    )

    ciphertext, key_ver = encrypt_sensitive_answer(
        "1234",
        application_id=app_id,
        question_key=question_key,
        settings=cfg_v1,
    )
    assert key_ver == "v1"
    assert "1234" not in ciphertext

    plaintext = decrypt_sensitive_answer(
        ciphertext,
        key_version=key_ver,
        application_id=app_id,
        question_key=question_key,
        settings=cfg_v1,
    )
    assert plaintext == "1234"

    # AAD tampering must fail authentication
    with pytest.raises(Exception):
        decrypt_sensitive_answer(
            ciphertext,
            key_version=key_ver,
            application_id=app_id,
            question_key="different_question",
            settings=cfg_v1,
        )

    # Unknown key version must be rejected
    with pytest.raises(ValueError, match="v99"):
        decrypt_sensitive_answer(
            ciphertext,
            key_version="v99",
            application_id=app_id,
            question_key=question_key,
            settings=cfg_v1,
        )

    # Ciphertext / AES-GCM authentication-tag tampering must fail with no plaintext fallback
    import base64
    from cryptography.exceptions import InvalidTag

    raw_payload = bytearray(base64.urlsafe_b64decode(ciphertext.encode("ascii")))
    raw_payload[-1] ^= 0xFF
    tampered_ciphertext = base64.urlsafe_b64encode(bytes(raw_payload)).decode("ascii")
    with pytest.raises(InvalidTag):
        decrypt_sensitive_answer(
            tampered_ciphertext,
            key_version=key_ver,
            application_id=app_id,
            question_key=question_key,
            settings=cfg_v1,
        )

    # Fail-closed when ENCRYPTION_KEY is missing
    cfg_empty = Settings(_env_file=None, encryption_key="")
    with pytest.raises(ValueError, match="ENCRYPTION_KEY"):
        encrypt_sensitive_answer(
            "1234",
            application_id=app_id,
            question_key=question_key,
            settings=cfg_empty,
        )

    # Key rotation: v1 -> v2 using ENCRYPTION_PREVIOUS_KEYS
    cfg_v2 = Settings(
        _env_file=None,
        encryption_key="rotated-secret-key-v2",
        encryption_key_version="v2",
        encryption_previous_keys="v1:primary-secret-key-v1",
    )
    rotated_ct, rotated_ver = rotate_sensitive_answer_ciphertext(
        ciphertext,
        old_key_version="v1",
        application_id=app_id,
        question_key=question_key,
        settings=cfg_v2,
    )
    assert rotated_ver == "v2"
    assert rotated_ct != ciphertext
    assert (
        decrypt_sensitive_answer(
            rotated_ct,
            key_version="v2",
            application_id=app_id,
            question_key=question_key,
            settings=cfg_v2,
        )
        == "1234"
    )


def test_state_machine_and_snapshot_hash_invalidation():
    assert "CONFIRMED" not in ALLOWED_APPLICATION_TRANSITIONS["USER_REPORTED_SUBMITTED"]
    assert ALLOWED_APPLICATION_TRANSITIONS["USER_REPORTED_SUBMITTED"] == frozenset({"UNVERIFIED", "FAILED"})
    assert "INTERVIEW" not in ALLOWED_APPLICATION_TRANSITIONS["USER_REPORTED_SUBMITTED"]
    assert "REJECTED" not in ALLOWED_APPLICATION_TRANSITIONS["USER_REPORTED_SUBMITTED"]
    assert "INTERVIEW" in ALLOWED_APPLICATION_TRANSITIONS["CONFIRMED"]

    with pytest.raises(ValueError, match="USER_REPORTED_SUBMITTED -> CONFIRMED"):
        validate_application_transition("USER_REPORTED_SUBMITTED", "CONFIRMED")
    validate_application_transition("USER_REPORTED_SUBMITTED", "UNVERIFIED")
    validate_application_transition("UNVERIFIED", "CONFIRMED")

    base_answers_hash = compute_answers_hash([("q2", "yes"), ("q1", "no")])
    assert base_answers_hash == compute_answers_hash([("q1", " no "), ("q2", "yes")])

    base_kwargs = {
        "application_id": "app-1",
        "user_id": "user-1",
        "resume_id": "res-1",
        "resume_checksum": "chk-1",
        "canonical_job_key": "canon-1",
        "destination_url": "https://boards.greenhouse.io/acme/jobs/1",
        "answers_hash": base_answers_hash,
        "application_mode": "human_approved_browser",
    }
    base_hash = compute_approval_snapshot_hash(**base_kwargs)

    assert base_hash != compute_approval_snapshot_hash(**{**base_kwargs, "resume_id": "res-2"})
    assert base_hash != compute_approval_snapshot_hash(
        **{**base_kwargs, "answers_hash": compute_answers_hash([("q1", "yes"), ("q2", "yes")])}
    )
    assert base_hash != compute_approval_snapshot_hash(
        **{**base_kwargs, "destination_url": "https://boards.greenhouse.io/acme/jobs/2"}
    )
    assert base_hash != compute_approval_snapshot_hash(
        **{**base_kwargs, "application_mode": "official_api"}
    )


def test_migration_0005_upgrade_downgrade_and_constraints_sqlite(tmp_path):
    db_file = tmp_path / "m5_stage1.db"
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_file}")

    command.upgrade(cfg, "head")
    engine = create_engine(f"sqlite:///{db_file}")
    insp = inspect(engine)
    tables = set(insp.get_table_names())
    assert {"applications", "application_answers", "approval_records"}.issubset(tables)

    with Session(engine) as session:
        session.execute(text("PRAGMA foreign_keys=ON"))
        user, job, resume = _seed_user_job_resume(session, suffix="1")

        app = Application(
            user_id=user.id,
            job_id=job.id,
            resume_id=resume.id,
            canonical_job_key="canon-key-1",
            idempotency_key="idem-key-1",
            application_mode="human_approved_browser",
            status="AWAITING_APPROVAL",
            destination_url="https://boards.greenhouse.io/m5/jobs/1",
            thread_id="thread-m5-1",
        )
        session.add(app)
        session.flush()

        # Duplicate active application for same (user_id, canonical_job_key) must fail
        dup_app = Application(
            user_id=user.id,
            job_id=job.id,
            resume_id=resume.id,
            canonical_job_key="canon-key-1",
            idempotency_key="idem-key-dup",
            application_mode="human_approved_browser",
            status="RESUME_READY",
            destination_url="https://boards.greenhouse.io/m5/jobs/1",
            thread_id="thread-m5-dup",
        )
        session.add(dup_app)
        with pytest.raises(IntegrityError):
            session.flush()
        session.rollback()

        # Re-create active app, then test sensitive answer check constraint
        session.execute(text("PRAGMA foreign_keys=ON"))
        user, job, resume = _seed_user_job_resume(session, suffix="2")
        app2 = Application(
            user_id=user.id,
            job_id=job.id,
            resume_id=resume.id,
            canonical_job_key="canon-key-2",
            idempotency_key="idem-key-2",
            application_mode="human_approved_browser",
            status="AWAITING_APPROVAL",
            destination_url="https://boards.greenhouse.io/m5/jobs/2",
            thread_id="thread-m5-2",
        )
        session.add(app2)
        session.flush()

        # Sensitive answer with plaintext must be rejected by ck_app_answers_sensitive_encrypted
        bad_sensitive = ApplicationAnswer(
            application_id=app2.id,
            question_key="ssn",
            question="SSN last 4",
            is_sensitive=True,
            answer_plaintext="1234",
            answer_encrypted=None,
            key_version=None,
        )
        session.add(bad_sensitive)
        with pytest.raises(IntegrityError):
            session.flush()
        session.rollback()

        # Valid encrypted sensitive answer + immutable ApprovalRecord + User deletion SET NULL
        session.execute(text("PRAGMA foreign_keys=ON"))
        user, job, resume = _seed_user_job_resume(session, suffix="3")
        app3 = Application(
            user_id=user.id,
            job_id=job.id,
            resume_id=resume.id,
            canonical_job_key="canon-key-3",
            idempotency_key="idem-key-3",
            application_mode="human_approved_browser",
            status="APPROVED",
            destination_url="https://boards.greenhouse.io/m5/jobs/3",
            thread_id="thread-m5-3",
        )
        session.add(app3)
        session.flush()

        enc_cfg = Settings(_env_file=None, encryption_key="test-key", encryption_key_version="v1")
        ct, ver = encrypt_sensitive_answer(
            "1234",
            application_id=str(app3.id),
            question_key="ssn",
            settings=enc_cfg,
        )
        ans = ApplicationAnswer(
            application_id=app3.id,
            question_key="ssn",
            question="SSN last 4",
            is_sensitive=True,
            answer_encrypted=ct,
            answer_plaintext=None,
            key_version=ver,
        )
        session.add(ans)

        now = datetime.now(timezone.utc)
        rec = ApprovalRecord(
            application_id=app3.id,
            decision="approved",
            actor=f"user:{user.id}",
            resume_id=resume.id,
            resume_checksum=resume.sha256_checksum,
            destination_url=app3.destination_url,
            application_mode=app3.application_mode,
            answers_hash="h" * 64,
            approval_snapshot_hash="s" * 64,
            decided_at=now,
            expires_at=now + timedelta(hours=24),
        )
        session.add(rec)
        audit = AuditLog(
            user_id=user.id,
            actor=f"user:{user.id}",
            action="application.approved",
            resource_type="application",
            resource_id=str(app3.id),
        )
        session.add(audit)
        session.commit()

        # ORM immutability listeners must reject update/delete on ApprovalRecord & AuditLog
        rec.notes = "tamper"
        with pytest.raises(ValueError, match="append-only"):
            session.flush()
        session.rollback()

        with pytest.raises(ValueError, match="append-only"):
            session.delete(rec)
            session.flush()
        session.rollback()

        with pytest.raises(ValueError, match="append-only"):
            session.delete(audit)
            session.flush()
        session.rollback()

        # Deleting Application is blocked by approval_records ON DELETE RESTRICT
        with pytest.raises(IntegrityError):
            session.execute(text("DELETE FROM applications WHERE id = :aid"), {"aid": app3.id.hex})
            session.flush()
        session.rollback()

        # Soft-deletion & anonymization clears user_id while preserving immutable approval_records
        refreshed = session.get(Application, app3.id)
        assert refreshed is not None
        refreshed.user_id = None
        refreshed.deleted_at = now
        refreshed.anonymized_at = now
        session.commit()

        refreshed = session.get(Application, app3.id)
        assert refreshed is not None
        assert refreshed.user_id is None
        assert refreshed.deleted_at is not None
        assert refreshed.anonymized_at is not None
        assert session.get(ApprovalRecord, rec.id) is not None

        # Retry allowed after FAILED status on new active app
        user4, job4, resume4 = _seed_user_job_resume(session, suffix="4")
        failed_app = Application(
            user_id=user4.id,
            job_id=job4.id,
            resume_id=resume4.id,
            canonical_job_key="canon-key-retry",
            idempotency_key="idem-key-retry-1",
            application_mode="human_approved_browser",
            status="FAILED",
            destination_url="https://boards.greenhouse.io/m5/jobs/4",
            thread_id="thread-m5-retry-1",
        )
        session.add(failed_app)
        session.flush()

        retry_app = Application(
            user_id=user4.id,
            job_id=job4.id,
            resume_id=resume4.id,
            canonical_job_key="canon-key-retry",
            idempotency_key="idem-key-retry-2",
            application_mode="human_approved_browser",
            status="RESUME_READY",
            destination_url="https://boards.greenhouse.io/m5/jobs/4",
            thread_id="thread-m5-retry-2",
        )
        session.add(retry_app)
        session.commit()

    engine.dispose()

    command.downgrade(cfg, "0004_milestone_4")
    engine2 = create_engine(f"sqlite:///{db_file}")
    tables_after_downgrade = set(inspect(engine2).get_table_names())
    assert "applications" not in tables_after_downgrade
    assert "application_answers" not in tables_after_downgrade
    assert "approval_records" not in tables_after_downgrade
    engine2.dispose()

    command.upgrade(cfg, "head")
    engine3 = create_engine(f"sqlite:///{db_file}")
    tables_after_reapply = set(inspect(engine3).get_table_names())
    assert {"applications", "application_answers", "approval_records"}.issubset(tables_after_reapply)
    engine3.dispose()


def test_migration_0005_offline_postgresql_ddl(capsys):
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    cfg.set_main_option("sqlalchemy.url", "postgresql+psycopg://user:pass@localhost:5432/applypilot")

    command.upgrade(cfg, "0004_milestone_4:0005_milestone_5", sql=True)
    captured = capsys.readouterr().out
    assert "CREATE TABLE applications" in captured
    assert "CREATE TABLE application_answers" in captured
    assert "CREATE TABLE approval_records" in captured
    assert "uq_applications_active_user_canonical_job" in captured
    assert "trg_approval_records_immutable" in captured
    assert "trg_audit_logs_immutable" in captured
