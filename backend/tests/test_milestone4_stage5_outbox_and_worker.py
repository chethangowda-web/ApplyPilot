from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import uuid
import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool, StaticPool

from app.core.config import Settings
from app.database.base import Base
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
from app.graphs.discovery_graph import run_discovery_workflow
from app.services.fingerprint_service import compute_generation_idempotency_key
from app.services.outbox_service import (
    TransactionalOutboxDispatcher,
    claim_generation_job,
    dispatch_outbox_job_to_broker,
    enqueue_and_commit_resume_generation,
    handle_generation_job_error,
    recover_outbox_jobs,
    release_worker_lease_on_shutdown,
    renew_worker_lease,
)
from app.services.quota_service import resolve_kolkata_quota_date
from app.services.resume_vault_service import (
    artifact_persistence_lock,
    compute_sha256,
    reconcile_orphaned_tailored_pdfs,
    store_resume_bytes,
)
from app.workers import resume_tasks
from app.workers.celery_app import celery_app
from app.workers.resume_tasks import execute_resume_generation_job


@pytest.fixture()
def sqlite_file_factory(tmp_path):
    """File-backed SQLite session factory so multiple sessions/workers share committed state."""
    db_file = tmp_path / "stage5_test.db"
    engine = create_engine(
        f"sqlite:///{db_file}",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    yield factory, tmp_path
    engine.dispose()


def _seed_user_profile_job(db: Session, *, suffix: str = "1") -> tuple[User, CandidateProfile, Job]:
    user = User(email=f"s5-{suffix}-{uuid.uuid4().hex[:6]}@example.com", hashed_password="h")
    db.add(user)
    db.flush()

    profile = CandidateProfile(
        user_id=user.id,
        full_name="Chethan Gowda",
        email="chethan@example.com",
        phone="+91-9000000001",
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
            verified_metrics=["Processed 500 workflows cleanly"],
        )
    )
    db.add(
        Experience(
            profile_id=profile.id,
            company="Acme AI",
            role="Backend Intern",
            start_date="2025-05",
            end_date="2025-08",
            verified_responsibilities=["Implemented FastAPI services"],
        )
    )

    src = JobSource(
        source_name=f"gh-{suffix}-{uuid.uuid4().hex[:6]}",
        connector_type="greenhouse",
        base_url="https://boards-api.greenhouse.io/v1/boards/acme/jobs",
        config={"board_token": "acme"},
    )
    db.add(src)
    db.flush()

    job = Job(
        source_id=src.id,
        source_job_id=f"{suffix}-{uuid.uuid4().hex[:6]}",
        canonical_url=f"https://boards.greenhouse.io/s5/jobs/{uuid.uuid4().hex[:8]}",
        dedup_key=uuid.uuid4().hex * 2,
        content_checksum=uuid.uuid4().hex * 2,
        company="OutboxLabs",
        role_title="Applied AI Engineer",
        location="Bengaluru, India",
        raw_jd="Required: Python, LangGraph, FastAPI. 0-1 yrs.",
    )
    db.add(job)
    db.flush()

    db.add(
        JobRequirement(
            job_id=job.id,
            role_category="Applied AI Engineer",
            required_skills=["Python", "LangGraph", "FastAPI"],
            technical_stack=["Python", "LangGraph", "FastAPI"],
        )
    )
    db.commit()
    return user, profile, job


def test_kolkata_quota_date_calculation():
    # 2026-11-25 19:00 UTC == 2026-11-26 00:30 IST (Asia/Kolkata crosses midnight ahead of UTC)
    utc_late = datetime(2026, 11, 25, 19, 0, 0, tzinfo=timezone.utc)
    assert resolve_kolkata_quota_date(utc_late).isoformat() == "2026-11-26"


def test_celery_app_durable_configuration():
    assert celery_app.conf.task_acks_late is True
    assert celery_app.conf.task_reject_on_worker_lost is True
    assert celery_app.conf.worker_prefetch_multiplier == 1
    assert celery_app.conf.timezone == "Asia/Kolkata"


def test_auto_manual_logical_idempotency_explicit_regenerate_and_quota_isolation(sqlite_file_factory):
    factory, tmp_path = sqlite_file_factory
    with factory() as db:
        user, profile, job = _seed_user_profile_job(db, suffix="idem")
        cfg = Settings(_env_file=None, auto_tailor_enabled=True, auto_tailor_daily_limit=3, auto_tailor_min_score=70)
        dispatched_ids: list[uuid.UUID] = []

        # 1. Manual non-forced enqueue MUST NOT consume auto quota
        out_manual, sent1 = enqueue_and_commit_resume_generation(
            db,
            user_id=user.id,
            profile_id=profile.id,
            job_id=job.id,
            trigger_mode="manual",
            settings=cfg,
            broker_dispatch_fn=lambda gid: (dispatched_ids.append(gid), True)[1],
        )
        assert out_manual.created is True and sent1 is True
        assert db.scalar(select(AutoTailorDailyQuota)) is None

        # 2. Automatic request for same job + fingerprint reuses logical manual request & consumes 0 quota
        out_auto_dup, sent2 = enqueue_and_commit_resume_generation(
            db,
            user_id=user.id,
            profile_id=profile.id,
            job_id=job.id,
            trigger_mode="auto",
            match_score=85.0,
            settings=cfg,
            broker_dispatch_fn=lambda gid: (dispatched_ids.append(gid), True)[1],
        )
        assert out_auto_dup.created is False
        assert out_auto_dup.accepted is True
        assert out_auto_dup.generation_job_id == out_manual.generation_job_id
        assert sent2 is False
        assert db.scalar(select(AutoTailorDailyQuota)) is None

        # 3. Execute v1 via worker -> completes cleanly
        res1 = execute_resume_generation_job(
            out_manual.generation_job_id,
            worker_id="w-idem-1",
            session_factory=factory,
            use_llm=False,
            storage_dir=tmp_path,
        )
        assert res1["status"] == "completed"

        # 4. Another non-forced request for completed fingerprint reuses existing v1
        out_dup_completed, _ = enqueue_and_commit_resume_generation(
            db,
            user_id=user.id,
            profile_id=profile.id,
            job_id=job.id,
            trigger_mode="auto",
            match_score=90.0,
            settings=cfg,
            broker_dispatch_fn=lambda _gid: True,
        )
        assert out_dup_completed.created is False
        assert out_dup_completed.status == "existing"

        # 5. Explicit regenerate creates new generation request & v2 WITHOUT consuming auto quota
        out_regen, sent_regen = enqueue_and_commit_resume_generation(
            db,
            user_id=user.id,
            profile_id=profile.id,
            job_id=job.id,
            trigger_mode="regenerate",
            force_regenerate=True,
            settings=cfg,
            broker_dispatch_fn=lambda gid: (dispatched_ids.append(gid), True)[1],
        )
        assert out_regen.created is True and sent_regen is True
        assert out_regen.generation_job_id != out_manual.generation_job_id
        assert db.scalar(select(AutoTailorDailyQuota)) is None

        res2 = execute_resume_generation_job(
            out_regen.generation_job_id,
            worker_id="w-idem-2",
            session_factory=factory,
            use_llm=False,
            storage_dir=tmp_path,
        )
        assert res2["status"] == "completed"

        mappings = list(
            db.scalars(
                select(ResumeJobMapping)
                .where(ResumeJobMapping.job_id == job.id, ResumeJobMapping.profile_id == profile.id)
                .order_by(ResumeJobMapping.version_number.asc())
            ).all()
        )
        assert len(mappings) == 2
        assert mappings[0].version_number == 1 and mappings[0].is_active is False
        assert mappings[1].version_number == 2 and mappings[1].is_active is True
        assert all(m.approval_status == "pending_review" for m in mappings)


def test_transient_retries_reuse_quota_and_terminal_failure_behavior(sqlite_file_factory):
    factory, _tmp_path = sqlite_file_factory
    with factory() as db:
        user, profile, job = _seed_user_profile_job(db, suffix="retry")
        cfg = Settings(_env_file=None, auto_tailor_enabled=True, auto_tailor_daily_limit=2, auto_tailor_min_score=70)
        out, _ = enqueue_and_commit_resume_generation(
            db,
            user_id=user.id,
            profile_id=profile.id,
            job_id=job.id,
            trigger_mode="auto",
            match_score=88.0,
            max_attempts=3,
            settings=cfg,
            broker_dispatch_fn=lambda _gid: True,
        )
        assert out.created is True and out.reserved_count_after == 1
        gen_id = out.generation_job_id
        assert gen_id is not None

        async def _transient_boom(*_args, **_kwargs):
            raise TimeoutError("LLM socket timeout")

        # Attempts 1 and 2 fail transiently -> return to queued with SAME single quota reservation
        for expected_attempt in (1, 2):
            res = execute_resume_generation_job(
                gen_id,
                worker_id=f"w-retry-{expected_attempt}",
                session_factory=factory,
                workflow_runner=_transient_boom,
            )
            assert res["claimed"] is True
            assert res["status"] == "queued"
            assert res["retry_scheduled"] is True
            db.expire_all()
            row = db.get(ResumeGenerationJob, gen_id)
            assert row is not None and row.attempt_count == expected_attempt and row.status == "queued"
            quota = db.scalar(select(AutoTailorDailyQuota).where(AutoTailorDailyQuota.user_id == user.id))
            assert quota is not None and quota.reserved_count == 1  # NEVER consumes a 2nd quota slot

        # Attempt 3 hits max_attempts=3 -> terminal `failed`, still 1 quota slot used
        res3 = execute_resume_generation_job(
            gen_id,
            worker_id="w-retry-3",
            session_factory=factory,
            workflow_runner=_transient_boom,
        )
        assert res3["status"] == "failed"
        assert res3["retry_scheduled"] is False
        db.expire_all()
        row3 = db.get(ResumeGenerationJob, gen_id)
        assert row3 is not None and row3.status == "failed" and row3.completed_at is not None
        quota_after = db.scalar(select(AutoTailorDailyQuota).where(AutoTailorDailyQuota.user_id == user.id))
        assert quota_after is not None and quota_after.reserved_count == 1

        # Claiming exhausted terminal failed job returns claimed=False
        claim_after = claim_generation_job(db, gen_id, worker_id="w-late")
        assert claim_after.claimed is False and claim_after.reason == "max_attempts_exhausted"

        # Non-forced manual retry after terminal failure creates a fresh queued outbox job cleanly
        out_retry_manual, sent_retry_manual = enqueue_and_commit_resume_generation(
            db,
            user_id=user.id,
            profile_id=profile.id,
            job_id=job.id,
            trigger_mode="manual",
            force_regenerate=False,
            settings=cfg,
            broker_dispatch_fn=lambda _gid: True,
        )
        assert out_retry_manual.created is True and sent_retry_manual is True
        assert out_retry_manual.generation_job_id != gen_id
        quota_final = db.scalar(select(AutoTailorDailyQuota).where(AutoTailorDailyQuota.user_id == user.id))
        assert quota_final is not None and quota_final.reserved_count == 1


def test_duplicate_celery_delivery_lease_renewal_expiration_and_graceful_shutdown(sqlite_file_factory):
    factory, tmp_path = sqlite_file_factory
    with factory() as db:
        user, profile, job = _seed_user_profile_job(db, suffix="lease")
        out, _ = enqueue_and_commit_resume_generation(
            db,
            user_id=user.id,
            profile_id=profile.id,
            job_id=job.id,
            trigger_mode="manual",
            broker_dispatch_fn=lambda _gid: True,
        )
        gen_id = out.generation_job_id
        assert gen_id is not None

        # 1. Worker A claims lease
        c1 = claim_generation_job(db, gen_id, worker_id="worker-A", lease_seconds=60)
        assert c1.claimed is True

        # Duplicate Celery delivery while Worker A holds active valid lease -> refused
        c_dup = claim_generation_job(db, gen_id, worker_id="worker-B", lease_seconds=60)
        assert c_dup.claimed is False and c_dup.reason == "already_running_valid_lease"

        # Lease renewal succeeds ONLY for owner worker-A
        assert renew_worker_lease(db, gen_id, worker_id="worker-B", lease_seconds=90) is False
        assert renew_worker_lease(db, gen_id, worker_id="worker-A", lease_seconds=90) is True

        # Graceful shutdown releases lease cleanly back to `queued`
        assert release_worker_lease_on_shutdown(db, gen_id, worker_id="worker-A") is True
        db.expire_all()
        row = db.get(ResumeGenerationJob, gen_id)
        assert row is not None and row.status == "queued" and row.worker_id is None

        # Simulate Worker C claiming and crashing with expired lease
        c2 = claim_generation_job(db, gen_id, worker_id="worker-C", lease_seconds=10)
        assert c2.claimed is True
        row = db.get(ResumeGenerationJob, gen_id)
        assert row is not None
        row.lease_expires_at = utc_now() - timedelta(minutes=5)
        db.commit()

        # Stale-running job recovery puts it back to queued and dispatches
        dispatched: list[uuid.UUID] = []
        stats = recover_outbox_jobs(db, dispatch_fn=lambda gid: (dispatched.append(gid), True)[1])
        assert stats["expired_requeued"] == 1
        assert gen_id in dispatched

        # Execute now completes v1
        final_res = execute_resume_generation_job(
            gen_id,
            worker_id="worker-D",
            session_factory=factory,
            use_llm=False,
            storage_dir=tmp_path,
        )
        assert final_res["status"] == "completed"

        # Post-completion duplicate Celery delivery -> idempotent no-op
        dup_after = execute_resume_generation_job(
            gen_id,
            worker_id="worker-E",
            session_factory=factory,
            use_llm=False,
            storage_dir=tmp_path,
        )
        assert dup_after["claimed"] is False
        assert dup_after["reason"] == "already_terminal_completed"


def test_crash_after_pdf_write_before_commit_and_crash_after_mapping_commit_preserve_active(
    sqlite_file_factory, monkeypatch
):
    factory, tmp_path = sqlite_file_factory
    with factory() as db:
        user, profile, job = _seed_user_profile_job(db, suffix="crash")

        # Complete v1 first so we have an active successful resume to protect
        out1, _ = enqueue_and_commit_resume_generation(
            db, user_id=user.id, profile_id=profile.id, job_id=job.id, trigger_mode="manual", broker_dispatch_fn=lambda _: True
        )
        r1 = execute_resume_generation_job(
            out1.generation_job_id, worker_id="w1", session_factory=factory, use_llm=False, storage_dir=tmp_path
        )
        assert r1["status"] == "completed"
        v1_mapping_id = uuid.UUID(r1["mapping_id"])

        # Trigger explicit v2 regeneration
        out2, _ = enqueue_and_commit_resume_generation(
            db,
            user_id=user.id,
            profile_id=profile.id,
            job_id=job.id,
            trigger_mode="regenerate",
            force_regenerate=True,
            max_attempts=3,
            broker_dispatch_fn=lambda _: True,
        )
        gen2_id = out2.generation_job_id
        assert gen2_id is not None

        # Crash AFTER PDF bytes stored on disk, BEFORE ResumeJobMapping DB commit!
        from app.services.resume_vault_service import store_resume_bytes as real_store

        wrote_orphan: list[Path] = []

        def _crash_after_pdf_store(**kwargs):
            p = real_store(**kwargs)
            wrote_orphan.append(p)
            return p

        monkeypatch.setattr("app.graphs.resume_intelligence_graph.store_resume_bytes", _crash_after_pdf_store)
        orig_commit = Session.commit
        commit_calls = {"n": 0}

        def _fail_final_mapping_commit(self):
            # Fail the transaction commit right after PDF bytes were written & flushed
            if len(wrote_orphan) == 1 and commit_calls["n"] == 0:
                commit_calls["n"] += 1
                raise OSError("Simulated power loss right after PDF creation before DB commit")
            return orig_commit(self)

        monkeypatch.setattr(Session, "commit", _fail_final_mapping_commit)
        crash_res = execute_resume_generation_job(
            gen2_id, worker_id="w2-crash", session_factory=factory, use_llm=False, storage_dir=tmp_path
        )
        # Verify orphaned PDF was cleaned up on commit rollback
        assert len(wrote_orphan) == 1 and not wrote_orphan[0].exists()
        assert crash_res["status"] == "queued" and crash_res["retry_scheduled"] is True

        # Verify v1 remained active throughout crash!
        db.expire_all()
        v1_mid = db.get(ResumeJobMapping, v1_mapping_id)
        assert v1_mid is not None and v1_mid.is_active is True

        # Recovery run succeeds cleanly and produces strictly ONE v2
        rec_res = execute_resume_generation_job(
            gen2_id, worker_id="w2-recover", session_factory=factory, use_llm=False, storage_dir=tmp_path
        )
        assert rec_res["status"] == "completed"
        db.expire_all()
        mappings = list(
            db.scalars(
                select(ResumeJobMapping)
                .where(ResumeJobMapping.job_id == job.id)
                .order_by(ResumeJobMapping.version_number.asc())
            ).all()
        )
        assert [m.version_number for m in mappings] == [1, 2]
        assert mappings[0].is_active is False and mappings[1].is_active is True

        # Crash Point D: Simulate lost Celery ACK when mapping with generation_job_id ALREADY committed
        # Create an interrupted job row linked to an existing mapping, simulate recovery run
        gen2_row = db.get(ResumeGenerationJob, gen2_id)
        assert gen2_row is not None
        gen2_row.status = "running"
        gen2_row.lease_expires_at = utc_now() - timedelta(minutes=2)
        db.commit()
        recover_outbox_jobs(db, dispatch_fn=lambda _: True)
        idemp_res = execute_resume_generation_job(
            gen2_id, worker_id="w2-dup-recovery", session_factory=factory, use_llm=False, storage_dir=tmp_path
        )
        assert idemp_res["status"] == "completed"
        # Still only versions [1, 2] — zero duplicate version created!
        mappings_after = list(db.scalars(select(ResumeJobMapping).where(ResumeJobMapping.job_id == job.id)).all())
        assert len(mappings_after) == 2


@pytest.mark.asyncio
async def test_redis_unavailable_outbox_recovery_and_discovery_resilience(sqlite_file_factory, monkeypatch):
    factory, _tmp_path = sqlite_file_factory
    # Point Celery broker to an unreachable Redis port so real Celery connection fails cleanly
    monkeypatch.setattr(
        "app.workers.celery_app.celery_app.send_task",
        lambda *_a, **_kw: (_ for _ in ()).throw(ConnectionError("Redis connection refused")),
    )

    with factory() as db:
        user, profile, job = _seed_user_profile_job(db, suffix="redisdown")

        def gh_handler(request: httpx.Request) -> httpx.Response:
            if "boards-api.greenhouse.io" in str(request.url):
                return httpx.Response(
                    200,
                    json={
                        "jobs": [
                            {
                                "id": 777,
                                "title": "Applied AI Engineer",
                                "location": {"name": "Bengaluru, India"},
                                "first_published": datetime.now(timezone.utc).isoformat(),
                                "absolute_url": "https://boards.greenhouse.io/acme/jobs/777",
                                "content": "Python, LangGraph, FastAPI. 0-1 yrs.",
                            }
                        ],
                        "meta": {"total_pages": 1},
                    },
                )
            return httpx.Response(200, json={})

        # Discovery commits discovered job + persists committed `queued` outbox row even though Redis is down!
        report = await run_discovery_workflow(
            db,
            user_id=user.id,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(gh_handler)),
            auto_tailor_dispatcher=TransactionalOutboxDispatcher(dispatch_to_broker=True),
            auto_tailor_settings=Settings(_env_file=None, auto_tailor_enabled=True, auto_tailor_min_score=60),
        )
        assert report["discovered_count"] >= 1
        assert report["auto_tailoring"]["dispatched_count"] == 1
        assert report["auto_tailoring"]["results"][0]["reason"] == "outbox_committed_broker_deferred"

        # Committed queued row survives Redis outage & backend restart
        queued_jobs = list(db.scalars(select(ResumeGenerationJob).where(ResumeGenerationJob.status == "queued")).all())
        assert len(queued_jobs) == 1

        # Later when Redis comes back (or application/worker restarts), outbox sweeper dispatches pending row
        recovered_ids: list[uuid.UUID] = []
        stats = recover_outbox_jobs(db, dispatch_fn=lambda gid: (recovered_ids.append(gid), True)[1])
        assert stats["queued_dispatched"] == 1
        assert recovered_ids == [queued_jobs[0].id]


def test_dispatch_outbox_job_to_broker_unreachable_returns_false(monkeypatch):
    monkeypatch.setattr(
        "app.workers.celery_app.celery_app.send_task",
        lambda *_a, **_kw: (_ for _ in ()).throw(OSError("Broker unreachable")),
    )
    assert dispatch_outbox_job_to_broker(uuid.uuid4()) is False


def test_celery_task_retry_delegation(sqlite_file_factory, monkeypatch):
    factory, _tmp_path = sqlite_file_factory
    with factory() as db:
        user, profile, job = _seed_user_profile_job(db, suffix="celeryretry")
        out, _ = enqueue_and_commit_resume_generation(
            db, user_id=user.id, profile_id=profile.id, job_id=job.id, trigger_mode="manual", broker_dispatch_fn=lambda _: True
        )
        gen_id = out.generation_job_id

    monkeypatch.setattr(resume_tasks, "get_session_factory", lambda: factory)
    monkeypatch.setattr(
        resume_tasks,
        "execute_resume_generation_job",
        lambda *_a, **_kw: {"status": "queued", "retry_scheduled": True, "error": "transient network glitch"},
    )

    class RetrySentinel(Exception):
        pass

    monkeypatch.setattr(
        resume_tasks.process_resume_generation,
        "retry",
        lambda *args, **kwargs: (_ for _ in ()).throw(RetrySentinel(f"countdown={kwargs.get('countdown')}")),
    )

    with pytest.raises(RetrySentinel, match="countdown=1"):
        resume_tasks.process_resume_generation.run(str(gen_id))


def test_manual_request_reuses_auto_request_without_extra_quota(sqlite_file_factory):
    factory, _tmp_path = sqlite_file_factory
    with factory() as db:
        user, profile, job = _seed_user_profile_job(db, suffix="automanual")
        cfg = Settings(_env_file=None, auto_tailor_enabled=True, auto_tailor_daily_limit=3, auto_tailor_min_score=70)

        out_auto, _ = enqueue_and_commit_resume_generation(
            db,
            user_id=user.id,
            profile_id=profile.id,
            job_id=job.id,
            trigger_mode="auto",
            match_score=85.0,
            settings=cfg,
            broker_dispatch_fn=lambda _: True,
        )
        assert out_auto.created is True and out_auto.reserved_count_after == 1

        out_manual, _ = enqueue_and_commit_resume_generation(
            db,
            user_id=user.id,
            profile_id=profile.id,
            job_id=job.id,
            trigger_mode="manual",
            force_regenerate=False,
            settings=cfg,
            broker_dispatch_fn=lambda _: True,
        )
        assert out_manual.created is False
        assert out_manual.generation_job_id == out_auto.generation_job_id
        # Verify single row & identical idempotency key
        rows = list(db.scalars(select(ResumeGenerationJob).where(ResumeGenerationJob.job_id == job.id)).all())
        assert len(rows) == 1
        assert rows[0].idempotency_key == compute_generation_idempotency_key(
            profile_id=profile.id, job_id=job.id, evidence_fingerprint=rows[0].evidence_fingerprint
        )
        assert handle_generation_job_error(db, uuid.uuid4(), worker_id="w-none", error_message="missing")[0] == "missing"
        # Unused import check for Resume sanity
        assert Resume.__tablename__ == "resumes"


def test_sigkill_process_crash_orphan_pdf_reconciliation_and_recovery(sqlite_file_factory):
    """Simulate hard worker process termination (os._exit) right after PDF creation before DB commit."""
    factory, tmp_path = sqlite_file_factory
    db_file = tmp_path / "stage5_test.db"

    with factory() as db:
        user, profile, job = _seed_user_profile_job(db, suffix="sigkill")

        # Seed a real master resume on disk (must NEVER be deleted by orphan cleanup)
        master_bytes = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF"
        master_id = uuid.uuid4()
        master_path = store_resume_bytes(
            storage_root=tmp_path,
            user_id=user.id,
            resume_id=master_id,
            safe_filename="master.pdf",
            is_master=True,
            data=master_bytes,
        )
        db.add(
            Resume(
                id=master_id,
                profile_id=profile.id,
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

        # Complete v1 cleanly
        out1, _ = enqueue_and_commit_resume_generation(
            db, user_id=user.id, profile_id=profile.id, job_id=job.id, trigger_mode="manual", broker_dispatch_fn=lambda _: True
        )
        r1 = execute_resume_generation_job(
            out1.generation_job_id, worker_id="w-v1", session_factory=factory, use_llm=False, storage_dir=tmp_path
        )
        assert r1["status"] == "completed"
        v1_resume_id = uuid.UUID(r1["resume_id"])
        v1_mapping_id = uuid.UUID(r1["mapping_id"])
        v1_pdf = Path(db.get(Resume, v1_resume_id).pdf_path)
        assert v1_pdf.is_file()

        # Enqueue v2 regeneration
        out2, _ = enqueue_and_commit_resume_generation(
            db,
            user_id=user.id,
            profile_id=profile.id,
            job_id=job.id,
            trigger_mode="regenerate",
            force_regenerate=True,
            max_attempts=3,
            broker_dispatch_fn=lambda _: True,
        )
        gen2_id = out2.generation_job_id
        assert gen2_id is not None

    # Spawn isolated subprocess that writes the v2 PDF and calls os._exit(9) BEFORE DB commit
    crash_script = (
        "import os, uuid\n"
        "from pathlib import Path\n"
        "from sqlalchemy import create_engine\n"
        "from sqlalchemy.orm import sessionmaker\n"
        "from app.services import resume_vault_service\n"
        "import app.graphs.resume_intelligence_graph as rig\n"
        "from app.workers.resume_tasks import execute_resume_generation_job\n"
        "real_store = resume_vault_service.store_resume_bytes\n"
        "def _store_and_sigkill(**kwargs):\n"
        "    real_store(**kwargs)\n"
        "    os._exit(9)\n"
        "rig.store_resume_bytes = _store_and_sigkill\n"
        f"engine = create_engine('sqlite:///{db_file.as_posix()}', connect_args={{'check_same_thread': False}})\n"
        "factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)\n"
        f"execute_resume_generation_job(uuid.UUID('{gen2_id}'), worker_id='w-sigkill', session_factory=factory, use_llm=False, storage_dir=Path(r'{str(tmp_path)}'))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", crash_script],
        cwd=str(Path(__file__).resolve().parents[1]),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    assert proc.returncode == 9

    tailored_user_dir = tmp_path / "tailored_resumes" / str(user.id)
    pdfs_after_crash = list(tailored_user_dir.glob("*.pdf"))
    orphan_pdfs = [p for p in pdfs_after_crash if p.resolve() != v1_pdf.resolve()]
    assert len(orphan_pdfs) == 1
    orphan_path = orphan_pdfs[0]

    with factory() as db:
        # 1. Previous active v1 & master resume remain intact
        v1_map = db.get(ResumeJobMapping, v1_mapping_id)
        assert v1_map is not None and v1_map.is_active is True
        assert master_path.is_file() and v1_pdf.is_file()

        # 2. Grace period protects fresh in-flight files
        fresh_stats = reconcile_orphaned_tailored_pdfs(db, storage_root=tmp_path, min_age_seconds=300)
        assert fresh_stats["removed"] == 0 and fresh_stats["skipped_grace"] >= 1
        assert orphan_path.is_file()

        # 3. Age orphan past grace period & expire dead worker's lease -> outbox recovery removes orphan cleanly
        past = time.time() - 600
        os.utime(orphan_path, (past, past))
        gen2_row = db.get(ResumeGenerationJob, gen2_id)
        assert gen2_row is not None and gen2_row.status == "running"
        gen2_row.lease_expires_at = utc_now() - timedelta(minutes=5)
        db.commit()

        rec_stats = recover_outbox_jobs(
            db,
            dispatch_fn=lambda _: True,
            storage_dir=tmp_path,
            orphan_min_age_seconds=300,
        )
        assert rec_stats["expired_requeued"] == 1
        assert rec_stats["orphans_removed"] == 1
        assert not orphan_path.exists()
        # Master and v1 PDFs MUST remain untouched
        assert master_path.is_file() and v1_pdf.is_file()

        # 4. Idempotent second cleanup run removes 0 and keeps valid files safe
        idem_stats = reconcile_orphaned_tailored_pdfs(db, storage_root=tmp_path, min_age_seconds=300)
        assert idem_stats["removed"] == 0

    # 5. Recovered worker completes strictly ONE v2
    r2 = execute_resume_generation_job(
        gen2_id, worker_id="w-recover-sigkill", session_factory=factory, use_llm=False, storage_dir=tmp_path
    )
    assert r2["status"] == "completed"
    with factory() as db:
        mappings = list(
            db.scalars(
                select(ResumeJobMapping)
                .where(ResumeJobMapping.job_id == job.id)
                .order_by(ResumeJobMapping.version_number.asc())
            ).all()
        )
        assert [m.version_number for m in mappings] == [1, 2]
        assert mappings[0].is_active is False and mappings[1].is_active is True


def test_orphan_cleanup_and_worker_commit_concurrent_overlap_never_deletes_committed_pdf(tmp_path):
    """Deterministic overlap: cleanup (min_age=0) blocks on shared lock until worker commits Resume, then preserves PDF."""
    db_file = tmp_path / "conc_orphan.db"
    engine = create_engine(
        f"sqlite:///{db_file}",
        connect_args={"check_same_thread": False, "timeout": 30},
        poolclass=NullPool,
    )
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)

    try:
        with factory() as db:
            user, profile, job = _seed_user_profile_job(db, suffix="conc_orphan")

        target_resume_id = uuid.uuid4()
        pdf_bytes = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF"
        pdf_written = threading.Event()
        cleanup_started = threading.Event()
        worker_err: list[BaseException] = []
        cleanup_stats: dict[str, int] = {}

        def _worker_persist() -> None:
            try:
                with factory() as w_db:
                    with artifact_persistence_lock(w_db, target_resume_id):
                        stored = store_resume_bytes(
                            storage_root=tmp_path,
                            user_id=user.id,
                            resume_id=target_resume_id,
                            safe_filename="concurrent_tailored.pdf",
                            is_master=False,
                            data=pdf_bytes,
                        )
                        pdf_written.set()
                        # Wait briefly while cleanup thread attempts to acquire lock before commit
                        cleanup_started.wait(timeout=2.0)
                        time.sleep(0.1)
                        w_db.add(
                            Resume(
                                id=target_resume_id,
                                profile_id=profile.id,
                                is_master=False,
                                original_filename="concurrent_tailored.pdf",
                                content_type="application/pdf",
                                file_size_bytes=len(pdf_bytes),
                                version_label="tailored-v1",
                                pdf_path=str(stored),
                                sha256_checksum=compute_sha256(pdf_bytes),
                                generation_status="completed",
                                validation_passed=True,
                            )
                        )
                        w_db.commit()
            except BaseException as exc:
                worker_err.append(exc)

        def _cleanup_scan() -> None:
            pdf_written.wait(timeout=5.0)
            cleanup_started.set()
            with factory() as c_db:
                res = reconcile_orphaned_tailored_pdfs(
                    c_db,
                    storage_root=tmp_path,
                    min_age_seconds=0,  # Disable grace period so ONLY shared lock + recheck protects it
                )
                cleanup_stats.update(res)

        t_worker = threading.Thread(target=_worker_persist)
        t_cleanup = threading.Thread(target=_cleanup_scan)
        t_worker.start()
        t_cleanup.start()
        t_worker.join(timeout=10)
        t_cleanup.join(timeout=10)

        assert not worker_err
        assert cleanup_stats["removed"] == 0

        with factory() as db:
            committed = db.get(Resume, target_resume_id)
            assert committed is not None
            assert committed.pdf_path and Path(committed.pdf_path).is_file()

            # Also verify active valid-lease running job skips deletion even for an uncommitted file at min_age=0
            in_flight_id = uuid.uuid4()
            in_flight_path = store_resume_bytes(
                storage_root=tmp_path,
                user_id=user.id,
                resume_id=in_flight_id,
                safe_filename="inflight.pdf",
                is_master=False,
                data=pdf_bytes,
            )
            db.add(
                ResumeGenerationJob(
                    user_id=user.id,
                    profile_id=profile.id,
                    job_id=job.id,
                    trigger_mode="manual",
                    evidence_fingerprint="a" * 64,
                    status="running",
                    worker_id="active-w1",
                    lease_expires_at=utc_now() + timedelta(minutes=5),
                    thread_id="res-active-lease",
                )
            )
            db.commit()
            lease_stats = reconcile_orphaned_tailored_pdfs(db, storage_root=tmp_path, min_age_seconds=0)
            assert lease_stats["removed"] == 0
            assert lease_stats["skipped_active_job"] >= 1
            assert in_flight_path.is_file()
    finally:
        engine.dispose()
