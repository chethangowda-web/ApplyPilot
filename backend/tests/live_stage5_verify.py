"""Live PostgreSQL (pgvector) + Redis + Celery worker Stage 5 verification.

Tests against real PostgreSQL (port 5434) and Redis (port 6381):
1. Multi-threaded concurrent atomic quota enforcement (`AUTO_TAILOR_DAILY_LIMIT=5`).
2. Multi-threaded concurrent auto + manual logical idempotency (`pg_advisory_xact_lock` + unique index).
3. Multi-threaded `SELECT ... FOR UPDATE SKIP LOCKED` claiming across concurrent worker threads.
4. Live Celery worker process consuming from Redis broker -> LangGraph PostgresSaver checkpoint -> PDF on disk -> PostgreSQL persistence.
5. Crash & outbox recovery against live PostgreSQL + live restarted Celery worker (post-outage queued job + expired worker lease).

Usage (from backend directory):
    $env:DATABASE_URL="postgresql+psycopg://applypilot:applypilot@localhost:5434/applypilot"
    $env:REDIS_URL="redis://localhost:6381/0"
    python tests/live_stage5_verify.py
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import uuid
import fitz
from fastapi.testclient import TestClient
import psycopg
import redis
from sqlalchemy import select

from app.core.config import Settings, get_settings
from app.core.security import create_access_token
from app.database.models import (
    AutoTailorDailyQuota,
    CandidateProfile,
    CandidateSkill,
    Experience,
    Job,
    JobRequirement,
    JobSource,
    Project,
    Resume,
    ResumeGenerationJob,
    ResumeJobMapping,
    User,
    utc_now,
)
from app.database.session import get_engine, get_session_factory
from app.main import app
from app.services.outbox_service import (
    claim_generation_job,
    enqueue_and_commit_resume_generation,
    recover_outbox_jobs,
)
from app.services.quota_service import resolve_kolkata_quota_date
from app.services.resume_vault_service import compute_sha256


def _reset_and_seed_base(session_factory):
    with session_factory() as db:
        # Clean test users cleanly via cascade
        old_users = db.scalars(select(User).where(User.email.like("live_s5_%"))).all()
        for u in old_users:
            db.delete(u)
        db.commit()

        user = User(email=f"live_s5_{uuid.uuid4().hex[:8]}@example.com", hashed_password="h")
        db.add(user)
        db.flush()

        profile = CandidateProfile(
            user_id=user.id,
            full_name="Live Stage5 Candidate",
            email=user.email,
            phone="+91-9000000009",
            location="Bengaluru, India",
            summary="Backend & AI Engineer experienced in Python, FastAPI, and LangGraph.",
            education=[{"institution": "VTU", "degree": "B.Tech", "field_of_study": "CS", "end_year": "2026"}],
            achievements=["SIH Finalist 2025"],
        )
        db.add(profile)
        db.flush()

        db.add_all(
            [
                CandidateSkill(profile_id=profile.id, skill_name="Python", category="Languages", verified=True),
                CandidateSkill(profile_id=profile.id, skill_name="LangGraph", category="AI", verified=True),
                CandidateSkill(profile_id=profile.id, skill_name="FastAPI", category="Backend", verified=True),
            ]
        )
        db.add(
            Project(
                profile_id=profile.id,
                name="ORBIT",
                summary="Multi-agent workflow orchestrator built with Python and LangGraph",
                technologies=["Python", "LangGraph", "FastAPI"],
                implemented_features=["Built checkpointed state graph for deterministic workflows"],
                verified_metrics=["Processed 500 workflows with 99% replay"],
            )
        )
        db.add(
            Experience(
                profile_id=profile.id,
                company="Acme AI",
                role="Backend Intern",
                start_date="2025-05",
                end_date="2025-08",
                verified_responsibilities=["Implemented FastAPI services and PostgreSQL migrations"],
            )
        )

        src = JobSource(
            source_name=f"live-s5-src-{uuid.uuid4().hex[:8]}",
            connector_type="greenhouse",
        )
        db.add(src)
        db.flush()

        jobs: list[Job] = []
        for i in range(17):
            j = Job(
                source_id=src.id,
                source_job_id=f"live-{uuid.uuid4().hex[:8]}",
                canonical_url=f"https://boards.greenhouse.io/live/jobs/{uuid.uuid4().hex}",
                dedup_key=uuid.uuid4().hex * 2,
                content_checksum=uuid.uuid4().hex * 2,
                company=f"LiveCo{i}",
                role_title="Applied AI Engineer",
                location="Bengaluru, India",
                raw_jd="Required: Python, LangGraph, FastAPI. 0-1 yrs.",
            )
            db.add(j)
            db.flush()
            db.add(
                JobRequirement(
                    job_id=j.id,
                    role_category="Applied AI Engineer",
                    required_skills=["Python", "LangGraph", "FastAPI"],
                    technical_stack=["Python", "LangGraph", "FastAPI"],
                )
            )
            jobs.append(j)
        db.commit()
        return user.id, profile.id, [j.id for j in jobs], src.id


def _cleanup_test_entities(session_factory, user_id: uuid.UUID, source_id: uuid.UUID):
    with session_factory() as db:
        u = db.get(User, user_id)
        if u is not None:
            db.delete(u)
        db.commit()
        src = db.get(JobSource, source_id)
        if src is not None:
            # Jobs cascade-delete or delete manually first
            for j in db.scalars(select(Job).where(Job.source_id == src.id)).all():
                db.delete(j)
            db.flush()
            db.delete(src)
        db.commit()


def _wait_for_job_status(session_factory, gen_id: uuid.UUID, targets: tuple[str, ...], timeout_s: float = 35.0):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        with session_factory() as db:
            row = db.get(ResumeGenerationJob, gen_id)
            if row is not None and row.status in targets:
                return row
        time.sleep(0.5)
    raise TimeoutError(f"Generation job {gen_id} did not reach {targets} within {timeout_s}s")


def main() -> int:
    db_url = os.environ["DATABASE_URL"]
    redis_url = os.environ["REDIS_URL"]
    psycopg_dsn = db_url.replace("postgresql+psycopg://", "postgresql://")

    # Verify PostgreSQL + Redis connections
    r = redis.from_url(redis_url, socket_timeout=5)
    assert r.ping() is True
    r.flushdb()  # Start with clean Redis queue for deterministic worker test
    with psycopg.connect(psycopg_dsn, connect_timeout=5) as conn:
        assert conn.execute("SELECT 1").fetchone()[0] == 1

    with tempfile.TemporaryDirectory() as tmp_storage:
        os.environ["STORAGE_DIR"] = tmp_storage
        get_settings.cache_clear()
        get_engine.cache_clear()
        get_session_factory.cache_clear()
        factory = get_session_factory()

        user_id, profile_id, job_ids, source_id = _reset_and_seed_base(factory)
        try:
            cfg = Settings(
                _env_file=None,
                auto_tailor_enabled=True,
                auto_tailor_daily_limit=5,
                auto_tailor_min_score=75,
            )

            # 1. Real concurrent quota test on PostgreSQL (12 concurrent threads competing for limit=5)
            def _race_quota(jid: uuid.UUID):
                with factory() as t_db:
                    out, _ = enqueue_and_commit_resume_generation(
                        t_db,
                        user_id=user_id,
                        profile_id=profile_id,
                        job_id=jid,
                        trigger_mode="auto",
                        match_score=90.0,
                        settings=cfg,
                        dispatch=False,
                    )
                    return out.created, out.status

            with ThreadPoolExecutor(max_workers=12) as pool:
                outcomes = list(pool.map(_race_quota, job_ids[:12]))

            created_count = sum(1 for created, _ in outcomes if created)
            exhausted_count = sum(1 for _, st in outcomes if st == "quota_exhausted")
            assert created_count == 5, f"Expected strictly 5 created, got {created_count}: {outcomes}"
            assert exhausted_count == 7, f"Expected strictly 7 exhausted, got {exhausted_count}: {outcomes}"

            q_date = resolve_kolkata_quota_date()
            with factory() as db:
                quota_row = db.get(AutoTailorDailyQuota, (user_id, q_date))
                assert quota_row is not None and quota_row.reserved_count == 5
            print("PASS 1: Live PostgreSQL concurrent atomic daily quota (12 threads, limit=5) PASSED")

            # 2. Real concurrent logical idempotency race across auto + manual on same job_ids[12]
            target_idem_job = job_ids[12]

            def _race_idem(mode: str):
                with factory() as t_db:
                    out, _ = enqueue_and_commit_resume_generation(
                        t_db,
                        user_id=user_id,
                        profile_id=profile_id,
                        job_id=target_idem_job,
                        trigger_mode=mode,
                        match_score=95.0,
                        settings=Settings(_env_file=None, auto_tailor_enabled=True, auto_tailor_daily_limit=20, auto_tailor_min_score=75),
                        dispatch=False,
                    )
                    return out.created, out.generation_job_id

            with ThreadPoolExecutor(max_workers=8) as pool:
                idem_outcomes = list(pool.map(_race_idem, ["auto", "manual"] * 4))

            created_idem = [gid for created, gid in idem_outcomes if created]
            all_gids = {gid for _, gid in idem_outcomes}
            assert len(created_idem) == 1, f"Expected strictly 1 logical job created, got {idem_outcomes}"
            assert len(all_gids) == 1
            print("PASS 2: Live PostgreSQL concurrent auto/manual logical idempotency PASSED")

            # 3. Concurrent FOR UPDATE SKIP LOCKED claim test (4 workers racing same job_ids[13])
            with factory() as db:
                claim_out, _ = enqueue_and_commit_resume_generation(
                    db,
                    user_id=user_id,
                    profile_id=profile_id,
                    job_id=job_ids[13],
                    trigger_mode="manual",
                    dispatch=False,
                )
                claim_gid = claim_out.generation_job_id
                assert claim_gid is not None

            def _race_claim(idx: int):
                with factory() as t_db:
                    return claim_generation_job(t_db, claim_gid, worker_id=f"pg-race-w{idx}").claimed

            with ThreadPoolExecutor(max_workers=4) as pool:
                claim_wins = list(pool.map(_race_claim, range(4)))
            assert sum(1 for w in claim_wins if w) == 1, f"Expected 1 winner, got {claim_wins}"
            # Release claim cleanly back to queued for worker execution step
            with factory() as db:
                row = db.get(ResumeGenerationJob, claim_gid)
                assert row is not None
                row.status = "queued"
                row.attempt_count = 0
                row.worker_id = None
                row.lease_expires_at = None
                db.commit()
            print("PASS 3: Live PostgreSQL FOR UPDATE SKIP LOCKED concurrent claim PASSED")

            # Clear PASS 1-3 synthetic rows BEFORE starting Celery worker so _on_worker_ready sweeps only live jobs
            with factory() as db:
                for stale in db.scalars(
                    select(ResumeGenerationJob).where(ResumeGenerationJob.status.in_(("queued", "running")))
                ).all():
                    stale.status = "failed"
                    stale.completed_at = utc_now()
                    stale.worker_id = None
                    stale.lease_expires_at = None
                db.commit()

            # 4 & 5. Real Celery Worker + Redis + PostgresSaver + Outbox/Lease Crash Recovery
            backend_dir = Path(__file__).resolve().parents[1]
            worker_env = os.environ.copy()
            worker_env.update(
                {
                    "DATABASE_URL": db_url,
                    "REDIS_URL": redis_url,
                    "STORAGE_DIR": tmp_storage,
                    "GEMINI_API_KEY": "",
                    "ANTHROPIC_API_KEY": "",
                    "OPENAI_API_KEY": "",
                }
            )
            worker_proc = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "celery",
                    "-A",
                    "app.workers.celery_app",
                    "worker",
                    "--pool=solo",
                    "--loglevel=INFO",
                    "-n",
                    f"liveverify-{uuid.uuid4().hex[:6]}@%h",
                ],
                cwd=str(backend_dir),
                env=worker_env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            try:
                # Wait briefly for worker boot ready
                time.sleep(3.5)

                # Test 4: Dispatch live job_ids[14] through Celery/Redis -> real worker executes -> PDF + DB + Checkpoint
                with factory() as db:
                    live_out, broker_ok = enqueue_and_commit_resume_generation(
                        db,
                        user_id=user_id,
                        profile_id=profile_id,
                        job_id=job_ids[14],
                        trigger_mode="manual",
                        dispatch=True,
                    )
                    assert live_out.created and broker_ok
                    live_gid = live_out.generation_job_id
                    assert live_gid is not None

                done_row = _wait_for_job_status(factory, live_gid, ("completed", "failed", "validation_failed"), timeout_s=35.0)
                assert done_row.status == "completed", f"Live Celery job failed: {done_row.error_message}"
                assert done_row.resume_id is not None

                with factory() as db:
                    res_row = db.get(Resume, done_row.resume_id)
                    map_row = db.scalar(
                        select(ResumeJobMapping).where(ResumeJobMapping.generation_job_id == live_gid)
                    )
                    assert res_row is not None and res_row.pdf_path and Path(res_row.pdf_path).is_file()
                    assert map_row is not None and map_row.is_active is True
                    assert map_row.approval_status == "pending_review"
                    with fitz.open(res_row.pdf_path) as doc:
                        assert "ORBIT" in doc[0].get_text("text")

                with psycopg.connect(psycopg_dsn) as conn:
                    cp_count = conn.execute(
                        "SELECT COUNT(*) FROM checkpoints WHERE thread_id = %s",
                        (done_row.thread_id,),
                    ).fetchone()[0]
                    assert cp_count >= 1
                print("PASS 4: Live Celery/Redis worker end-to-end execution + PostgresSaver + PDF PASSED")

                # Test 5: Simulate crashed worker (expired lease `running` job) on job_ids[15] -> worker restart outbox recovery completes it
                with factory() as db:
                    crash_out, _ = enqueue_and_commit_resume_generation(
                        db,
                        user_id=user_id,
                        profile_id=profile_id,
                        job_id=job_ids[15],
                        trigger_mode="manual",
                        dispatch=False,  # Simulates worker crash / Redis outage right after claim
                    )
                    crash_gid = crash_out.generation_job_id
                    assert crash_gid is not None
                    row = db.get(ResumeGenerationJob, crash_gid)
                    assert row is not None
                    row.status = "running"
                    row.attempt_count = 1
                    row.worker_id = "dead-worker-99"
                    row.lease_expires_at = utc_now() - timedelta(minutes=10)
                    db.commit()

                    rec_stats = recover_outbox_jobs(db)
                    assert rec_stats["expired_requeued"] >= 1, f"Unexpected recovery stats: {rec_stats}"
                    assert rec_stats["queued_dispatched"] >= 1, f"Unexpected dispatch stats: {rec_stats}"

                rec_done = _wait_for_job_status(factory, crash_gid, ("completed", "failed", "validation_failed"), timeout_s=35.0)
                assert rec_done.status == "completed", f"Recovered live job failed: {rec_done.error_message}"
                print("PASS 5: Live expired-lease crash recovery -> live Celery worker completion PASSED")

                # Test 6: Stage 6 Live FastAPI API -> Celery/Redis -> Postgres -> Download/Checksum -> Concurrent Approve/Reject
                with factory() as db:
                    other_user = User(email=f"live_s6_other_{uuid.uuid4().hex[:6]}@example.com", hashed_password="h")
                    db.add(other_user)
                    db.flush()
                    db.add(CandidateProfile(user_id=other_user.id, full_name="Other", email=other_user.email))
                    db.commit()
                    other_uid = other_user.id

                owner_tok, _ = create_access_token(str(user_id))
                other_tok, _ = create_access_token(str(other_uid))
                owner_hdrs = {"Authorization": f"Bearer {owner_tok}"}
                other_hdrs = {"Authorization": f"Bearer {other_tok}"}
                api_job_id = job_ids[16]

                with TestClient(app) as client:
                    # Trigger via API POST /api/jobs/{job_id}/tailor -> 202 Accepted
                    t_resp = client.post(
                        f"/api/jobs/{api_job_id}/tailor",
                        headers=owner_hdrs,
                        json={"use_llm": False, "force_regenerate": False},
                    )
                    assert t_resp.status_code == 202, t_resp.text
                    api_gen_id = uuid.UUID(t_resp.json()["generation_job_id"])

                    # Non-forced duplicate returns 200 existing
                    dup_resp = client.post(
                        f"/api/jobs/{api_job_id}/tailor",
                        headers=owner_hdrs,
                        json={"use_llm": False, "force_regenerate": False},
                    )
                    assert dup_resp.status_code == 200 and dup_resp.json()["idempotency_hit"] is True

                    # Live Celery worker processes API request
                    api_done = _wait_for_job_status(factory, api_gen_id, ("completed", "failed", "validation_failed"), timeout_s=35.0)
                    assert api_done.status == "completed", f"Live API->Celery failed: {api_done.error_message}"
                    api_res_id = api_done.resume_id
                    assert api_res_id is not None

                    # API Retrieval endpoints + Cross-user 404 checks
                    det = client.get(f"/api/resumes/{api_res_id}/tailored", headers=owner_hdrs)
                    assert det.status_code == 200 and det.json()["approval_status"] == "pending_review"
                    assert client.get(f"/api/resumes/{api_res_id}/tailored", headers=other_hdrs).status_code == 404
                    assert client.get(f"/api/resumes/{api_res_id}/diff", headers=owner_hdrs).status_code == 200
                    assert client.get(f"/api/resumes/{api_res_id}/validation", headers=owner_hdrs).status_code == 200
                    assert client.get(f"/api/jobs/{api_job_id}/resumes/history", headers=owner_hdrs).status_code == 200

                    # Download & SHA-256 verification
                    assert client.get(f"/api/resumes/{api_res_id}/download?approved_only=true", headers=owner_hdrs).status_code == 403
                    dl = client.get(f"/api/resumes/{api_res_id}/download", headers=owner_hdrs)
                    assert dl.status_code == 200
                    assert compute_sha256(dl.content) == det.json()["sha256_checksum"]
                    assert client.get(f"/api/resumes/{api_res_id}/download", headers=other_hdrs).status_code == 404

                    # Concurrent live PostgreSQL approve vs reject race
                    def _race_decision(action: str) -> int:
                        with TestClient(app) as c:
                            resp = c.post(f"/api/resumes/{api_res_id}/{action}", headers=owner_hdrs, json={"notes": action})
                            return resp.status_code

                    with ThreadPoolExecutor(max_workers=6) as pool:
                        codes = list(pool.map(_race_decision, ["approve", "reject", "approve", "reject", "approve", "reject"]))
                    assert 200 in codes
                    with factory() as db:
                        mp = db.scalar(select(ResumeJobMapping).where(ResumeJobMapping.resume_id == api_res_id))
                        assert mp is not None and mp.approval_status in ("approved", "rejected")
                        if mp.approval_status == "approved":
                            assert mp.approved_at is not None and mp.rejected_at is None
                        else:
                            assert mp.rejected_at is not None and mp.approved_at is None

                    # Clean up extra other_user
                    with factory() as db:
                        ou = db.get(User, other_uid)
                        if ou is not None:
                            db.delete(ou)
                            db.commit()

                print("PASS 6: Live PostgreSQL + Celery Stage 6 API + Download + Concurrent Approve/Reject PASSED")
            except Exception:
                # Print DB row status + worker logs on failure for instant diagnosis
                with factory() as dbg_db:
                    for jrow in dbg_db.scalars(select(ResumeGenerationJob).where(ResumeGenerationJob.user_id == user_id)).all():
                        if jrow.job_id in (job_ids[14], job_ids[15]):
                            print(f"DEBUG job={jrow.id} status={jrow.status} attempts={jrow.attempt_count} err={jrow.error_message}")
                worker_proc.terminate()
                out_text, _ = worker_proc.communicate(timeout=5)
                print("WORKER LOGS:\n", out_text[-3000:])
                raise
            finally:
                if worker_proc.poll() is None:
                    worker_proc.terminate()
                    try:
                        worker_proc.wait(timeout=8)
                    except subprocess.TimeoutExpired:
                        worker_proc.kill()
                r.flushdb()
        finally:
            _cleanup_test_entities(factory, user_id, source_id)
            get_engine().dispose()

    print("ALL LIVE STAGE 5 POSTGRES + REDIS + CELERY WORKER VERIFICATIONS PASSED!")
    return 0


if __name__ == "__main__":
    sys.exit(main())
