"""Live PostgreSQL 16 cross-process advisory-lock verification for orphan PDF reconciliation.

Uses independent OS processes (`subprocess.Popen`) with separate Python interpreters and
PostgreSQL connections so Python `threading.Lock` cannot mask cross-process locking.

Usage (from backend directory):
    $env:DATABASE_URL="postgresql+psycopg://applypilot:applypilot@localhost:5434/applypilot"
    python tests/live_orphan_lock_verify.py
"""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import uuid
import psycopg
from sqlalchemy import select

from app.core.config import get_settings
from app.database.models import (
    CandidateProfile,
    Job,
    JobSource,
    Resume,
    ResumeGenerationJob,
    User,
    utc_now,
)
from app.database.session import get_engine, get_session_factory
from app.services.resume_vault_service import (
    compute_sha256,
    reconcile_orphaned_tailored_pdfs,
    store_resume_bytes,
)

_WORKER_CHILD_CODE = """
import os, sys, time, uuid
from pathlib import Path
from app.core.config import get_settings
from app.database.models import Resume
from app.database.session import get_engine, get_session_factory
from app.services.resume_vault_service import artifact_persistence_lock, compute_sha256, store_resume_bytes

storage_root = Path(sys.argv[1])
user_id = uuid.UUID(sys.argv[2])
profile_id = uuid.UUID(sys.argv[3])
resume_id = uuid.UUID(sys.argv[4])
paused_flag = Path(sys.argv[5])
action_flag = Path(sys.argv[6])

get_settings.cache_clear()
get_engine.cache_clear()
get_session_factory.cache_clear()
factory = get_session_factory()

pdf_bytes = b"%PDF-1.4\\n1 0 obj<<>>endobj\\ntrailer<<>>\\n%%EOF"

with factory() as db:
    with artifact_persistence_lock(db, resume_id, release_xact_on_exit=False):
        stored = store_resume_bytes(
            storage_root=storage_root,
            user_id=user_id,
            resume_id=resume_id,
            safe_filename="crossproc.pdf",
            is_master=False,
            data=pdf_bytes,
        )
        db.add(
            Resume(
                id=resume_id,
                profile_id=profile_id,
                is_master=False,
                original_filename="crossproc.pdf",
                content_type="application/pdf",
                file_size_bytes=len(pdf_bytes),
                version_label="tailored-v1",
                pdf_path=str(stored),
                sha256_checksum=compute_sha256(pdf_bytes),
                generation_status="completed",
                validation_passed=True,
            )
        )
        db.flush()
        paused_flag.write_text("paused", encoding="utf-8")
        deadline = time.monotonic() + 20.0
        while not action_flag.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        action = action_flag.read_text(encoding="utf-8").strip() if action_flag.exists() else "rollback"
        if action == "commit":
            db.commit()
        else:
            db.rollback()
            stored.unlink(missing_ok=True)
"""

_CLEANER_CHILD_CODE = """
import json, sys, time
from pathlib import Path
from app.core.config import get_settings
from app.database.session import get_engine, get_session_factory
from app.services.resume_vault_service import reconcile_orphaned_tailored_pdfs

storage_root = Path(sys.argv[1])
out_file = Path(sys.argv[2])
hold_after_flag = Path(sys.argv[3]) if len(sys.argv) > 3 else None

get_settings.cache_clear()
get_engine.cache_clear()
get_session_factory.cache_clear()
factory = get_session_factory()

t0 = time.monotonic()
with factory() as db:
    stats = reconcile_orphaned_tailored_pdfs(db, storage_root=storage_root, min_age_seconds=0)
    in_tx_after = db.in_transaction()
elapsed = round(time.monotonic() - t0, 4)

out_file.write_text(json.dumps({"stats": stats, "elapsed_s": elapsed, "in_tx_after": in_tx_after}), encoding="utf-8")
"""


def _wait_for_file(path: Path, timeout_s: float = 10.0) -> None:
    deadline = time.monotonic() + timeout_s
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    if not path.exists():
        raise TimeoutError(f"Timed out waiting for {path}")


def main() -> int:
    db_url = os.environ["DATABASE_URL"]
    psycopg_dsn = db_url.replace("postgresql+psycopg://", "postgresql://")
    backend_dir = Path(__file__).resolve().parents[1]

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        storage_root = tmp_path / "storage"
        storage_root.mkdir(parents=True, exist_ok=True)

        os.environ["STORAGE_DIR"] = str(storage_root)
        get_settings.cache_clear()
        get_engine.cache_clear()
        get_session_factory.cache_clear()
        factory = get_session_factory()

        with factory() as db:
            u = User(email=f"live_lock_{uuid.uuid4().hex[:8]}@example.com", hashed_password="h")
            db.add(u)
            db.flush()
            p = CandidateProfile(user_id=u.id, full_name="Lock Probe", email=u.email)
            db.add(p)
            db.flush()
            src = JobSource(source_name=f"lock-src-{uuid.uuid4().hex[:8]}", connector_type="greenhouse")
            db.add(src)
            db.flush()
            job = Job(
                source_id=src.id,
                source_job_id=uuid.uuid4().hex[:8],
                canonical_url=f"https://boards.greenhouse.io/lock/jobs/{uuid.uuid4().hex}",
                dedup_key=uuid.uuid4().hex * 2,
                content_checksum=uuid.uuid4().hex * 2,
                company="LockCo",
                role_title="AI Engineer",
                raw_jd="Python",
            )
            db.add(job)
            db.commit()
            uid, pid, jid, src_id = u.id, p.id, job.id, src.id

        child_env = os.environ.copy()
        child_env["DATABASE_URL"] = db_url
        child_env["STORAGE_DIR"] = str(storage_root)

        try:
            # 1. Seed a master resume (must remain untouched throughout)
            master_id = uuid.uuid4()
            master_bytes = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF"
            master_path = store_resume_bytes(
                storage_root=storage_root,
                user_id=uid,
                resume_id=master_id,
                safe_filename="master.pdf",
                is_master=True,
                data=master_bytes,
            )
            with factory() as db:
                db.add(
                    Resume(
                        id=master_id,
                        profile_id=pid,
                        is_master=True,
                        original_filename="master.pdf",
                        content_type="application/pdf",
                        file_size_bytes=len(master_bytes),
                        version_label="master-v1",
                        pdf_path=str(master_path),
                        sha256_checksum=compute_sha256(master_bytes),
                    )
                )
                db.commit()

            # Scenario 1: Process A holds pg_advisory_xact_lock -> Process B blocks in pg_locks -> Process A commits -> Process B preserves PDF
            res_id_1 = uuid.uuid4()
            paused_1 = tmp_path / "paused_1"
            action_1 = tmp_path / "action_1"
            cleaner_out_1 = tmp_path / "cleaner_1.json"

            proc_a = subprocess.Popen(
                [sys.executable, "-c", _WORKER_CHILD_CODE, str(storage_root), str(uid), str(pid), str(res_id_1), str(paused_1), str(action_1)],
                cwd=str(backend_dir),
                env=child_env,
                stdin=subprocess.DEVNULL,
            )
            _wait_for_file(paused_1)

            proc_b = subprocess.Popen(
                [sys.executable, "-c", _CLEANER_CHILD_CODE, str(storage_root), str(cleaner_out_1)],
                cwd=str(backend_dir),
                env=child_env,
                stdin=subprocess.DEVNULL,
            )
            try:
                # Poll pg_locks deterministically until Process B is visibly waiting on PostgreSQL advisory lock
                wait_deadline = time.monotonic() + 10.0
                lock_map: dict[bool, int] = {}
                while time.monotonic() < wait_deadline:
                    with psycopg.connect(psycopg_dsn) as conn:
                        locks = conn.execute(
                            "SELECT granted, COUNT(*) FROM pg_locks WHERE locktype = 'advisory' GROUP BY granted"
                        ).fetchall()
                        lock_map = {bool(granted): int(cnt) for granted, cnt in locks}
                    if lock_map.get(True, 0) >= 1 and lock_map.get(False, 0) >= 1:
                        break
                    time.sleep(0.1)

                assert proc_b.poll() is None, "Cleaner Process B should be blocked waiting for Process A's advisory lock"
                assert lock_map.get(True, 0) >= 1, f"Expected granted advisory lock from Process A: {lock_map}"
                assert lock_map.get(False, 0) >= 1, f"Expected waiting advisory lock from Process B: {lock_map}"

                # Allow Process A to commit -> Process B unblocks, finds committed Resume, preserves PDF
                action_1.write_text("commit", encoding="utf-8")
                assert proc_a.wait(timeout=10) == 0
                assert proc_b.wait(timeout=10) == 0
            finally:
                for p in (proc_a, proc_b):
                    if p.poll() is None:
                        p.terminate()
                        p.wait(timeout=5)

            res_b1 = json.loads(cleaner_out_1.read_text(encoding="utf-8"))
            assert res_b1["stats"]["removed"] == 0, f"Cleaner deleted committed PDF! {res_b1}"
            assert res_b1["in_tx_after"] is False, "Cleaner must not leave an open transaction"

            tailored_pdf_1 = storage_root / "tailored_resumes" / str(uid) / f"{res_id_1}_crossproc.pdf"
            assert tailored_pdf_1.is_file()
            assert master_path.is_file()

            # Verify zero lingering advisory locks after both processes complete
            with psycopg.connect(psycopg_dsn) as conn:
                leftover = conn.execute("SELECT COUNT(*) FROM pg_locks WHERE locktype = 'advisory'").fetchone()[0]
                assert leftover == 0, f"Advisory locks leaked after commit: {leftover}"
            print("PASS LOCK 1: Cross-process Postgres pg_advisory_xact_lock commit coordination PASSED")

            # Scenario 2: Process A holds lock, rolls back -> Process B unblocks cleanly & advisory lock released
            res_id_2 = uuid.uuid4()
            paused_2 = tmp_path / "paused_2"
            action_2 = tmp_path / "action_2"
            cleaner_out_2 = tmp_path / "cleaner_2.json"

            proc_a2 = subprocess.Popen(
                [sys.executable, "-c", _WORKER_CHILD_CODE, str(storage_root), str(uid), str(pid), str(res_id_2), str(paused_2), str(action_2)],
                cwd=str(backend_dir),
                env=child_env,
                stdin=subprocess.DEVNULL,
            )
            _wait_for_file(paused_2)

            proc_b2 = subprocess.Popen(
                [sys.executable, "-c", _CLEANER_CHILD_CODE, str(storage_root), str(cleaner_out_2)],
                cwd=str(backend_dir),
                env=child_env,
                stdin=subprocess.DEVNULL,
            )
            try:
                wait_deadline2 = time.monotonic() + 10.0
                lock_map2: dict[bool, int] = {}
                while time.monotonic() < wait_deadline2:
                    with psycopg.connect(psycopg_dsn) as conn:
                        locks2 = conn.execute(
                            "SELECT granted, COUNT(*) FROM pg_locks WHERE locktype = 'advisory' GROUP BY granted"
                        ).fetchall()
                        lock_map2 = {bool(granted): int(cnt) for granted, cnt in locks2}
                    if lock_map2.get(True, 0) >= 1 and lock_map2.get(False, 0) >= 1:
                        break
                    time.sleep(0.1)
                assert proc_b2.poll() is None
                assert lock_map2.get(False, 0) >= 1

                action_2.write_text("rollback", encoding="utf-8")
                assert proc_a2.wait(timeout=10) == 0
                assert proc_b2.wait(timeout=10) == 0
            finally:
                for p in (proc_a2, proc_b2):
                    if p.poll() is None:
                        p.terminate()
                        p.wait(timeout=5)
            res_b2 = json.loads(cleaner_out_2.read_text(encoding="utf-8"))
            assert res_b2["stats"]["removed"] == 0
            assert tailored_pdf_1.is_file() and master_path.is_file()

            with psycopg.connect(psycopg_dsn) as conn:
                leftover2 = conn.execute("SELECT COUNT(*) FROM pg_locks WHERE locktype = 'advisory'").fetchone()[0]
                assert leftover2 == 0
            print("PASS LOCK 2: Cross-process Postgres advisory lock release on rollback PASSED")

            # Scenario 3: Reverse ordering (cleaner runs BEFORE worker persists) + active valid worker lease protection
            res_id_3 = uuid.uuid4()
            inflight_pdf = store_resume_bytes(
                storage_root=storage_root,
                user_id=uid,
                resume_id=res_id_3,
                safe_filename="inflight_pg.pdf",
                is_master=False,
                data=master_bytes,
            )
            with factory() as db:
                gen = ResumeGenerationJob(
                    user_id=uid,
                    profile_id=pid,
                    job_id=jid,
                    trigger_mode="manual",
                    evidence_fingerprint="b" * 64,
                    status="running",
                    worker_id="pg-live-worker",
                    lease_expires_at=utc_now() + (utc_now() - utc_now()),  # will replace cleanly below
                    thread_id="res-pg-lease-probe",
                )
                from datetime import timedelta

                gen.lease_expires_at = utc_now() + timedelta(minutes=5)
                db.add(gen)
                db.commit()

                # Independent cleaner run must skip active valid-lease job AND preserve master & committed PDFs
                stats_3 = reconcile_orphaned_tailored_pdfs(db, storage_root=storage_root, min_age_seconds=0)
                assert stats_3["removed"] == 0
                assert stats_3["skipped_active_job"] >= 1
                assert db.in_transaction() is False
                assert inflight_pdf.is_file() and tailored_pdf_1.is_file() and master_path.is_file()
            print("PASS LOCK 3: Reverse ordering, open-tx check & valid Postgres worker lease protection PASSED")
        finally:
            with factory() as db:
                u_del = db.get(User, uid)
                if u_del is not None:
                    db.delete(u_del)
                for j_del in db.scalars(select(Job).where(Job.source_id == src_id)).all():
                    db.delete(j_del)
                db.flush()
                s_del = db.get(JobSource, src_id)
                if s_del is not None:
                    db.delete(s_del)
                db.commit()
            get_engine().dispose()

    print("ALL LIVE POSTGRESQL CROSS-PROCESS ADVISORY LOCK TESTS PASSED!")
    return 0


if __name__ == "__main__":
    sys.exit(main())
