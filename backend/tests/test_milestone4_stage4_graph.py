from datetime import date, datetime, timezone
import json
from pathlib import Path
import uuid
import fitz
import httpx
import pytest
from langgraph.checkpoint.memory import MemorySaver
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.agents.resume_intelligence.evidence_retriever import EvidenceRetriever
from app.agents.resume_intelligence.resume_tailor import (
    ResumeTailoringAgent,
    _LLMTailoringPayload,
    deterministic_tailor_resume,
)
from app.core.config import Settings
from app.database.base import Base
from app.database.models import (
    AuditLog,
    AutoTailorDailyQuota,
    CandidateProfile,
    CandidateSkill,
    Certification,
    Experience,
    Job,
    JobMatch,
    JobRequirement,
    JobSource,
    Project,
    Resume,
    ResumeGenerationJob,
    ResumeJobMapping,
    User,
    WorkflowRun,
)
from app.graphs.discovery_graph import run_discovery_workflow
from app.graphs.intelligence_graph import run_job_intelligence_workflow
from app.graphs.resume_intelligence_graph import (
    build_resume_intelligence_graph,
    run_resume_intelligence_workflow,
)
from app.llm.claude import ClaudeProvider
from app.llm.openai import OpenAICompatibleProvider
from app.llm.router import ModelRouter
from app.schemas.resume_intelligence import TailoredSkillGroup
from app.services.auto_tailor_trigger import (
    AutoTailorDispatchResult,
    trigger_auto_tailor_candidates,
)
from app.services.resume_vault_service import compute_sha256, store_resume_bytes


def _make_pdf_bytes(text: str) -> bytes:
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), text)
    out = doc.tobytes()
    doc.close()
    return out


@pytest.fixture()
def db_session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    with SessionLocal() as db:
        yield db
    engine.dispose()


def _seed_stage4_world(db: Session, storage_root: Path, *, with_master: bool = True):
    user = User(email=f"s4-{uuid.uuid4().hex[:6]}@example.com", hashed_password="h")
    db.add(user)
    db.flush()

    profile = CandidateProfile(
        user_id=user.id,
        full_name="Chethan Gowda",
        email="chethan@example.com",
        phone="+91-9000000001",
        location="Bengaluru, India",
        github_url="https://github.com/chethan",
        linkedin_url="https://linkedin.com/in/chethan",
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
            CandidateSkill(profile_id=profile.id, skill_name="PostgreSQL", category="Databases", verified=True),
            CandidateSkill(profile_id=profile.id, skill_name="Rust", category="Languages", verified=False),
        ]
    )
    db.add(
        Project(
            profile_id=profile.id,
            name="ORBIT",
            summary="Multi-agent workflow orchestrator built with Python and LangGraph",
            technologies=["Python", "LangGraph", "FastAPI"],
            implemented_features=[
                "Built checkpointed state graph for deterministic workflows",
                "Implemented FastAPI REST endpoints for workflow dispatch",
            ],
            planned_features=["Deploy Kubernetes autoscaler cluster"],
            verified_metrics=["Processed 500 workflows with 99% deterministic replay"],
            repo_url="https://github.com/chethan/orbit",
        )
    )
    db.add(
        Experience(
            profile_id=profile.id,
            company="Acme AI",
            role="Backend Intern",
            location="Bengaluru",
            start_date="2025-05",
            end_date="2025-08",
            verified_responsibilities=["Implemented FastAPI services and PostgreSQL migrations"],
        )
    )
    db.add(
        Certification(
            profile_id=profile.id,
            name="AWS Certified Cloud Practitioner",
            issuer="AWS",
            issued_date="2025-09",
            verified=True,
        )
    )

    master: Resume | None = None
    if with_master:
        m_bytes = _make_pdf_bytes("SUMMARY\nPython FastAPI\nPROJECTS\nORBIT\n")
        m_id = uuid.uuid4()
        m_path = store_resume_bytes(
            storage_root=storage_root,
            user_id=user.id,
            resume_id=m_id,
            safe_filename="master.pdf",
            is_master=True,
            data=m_bytes,
        )
        master = Resume(
            id=m_id,
            profile_id=profile.id,
            is_master=True,
            original_filename="master.pdf",
            content_type="application/pdf",
            file_size_bytes=len(m_bytes),
            version_label="master-v1",
            pdf_path=str(m_path),
            sha256_checksum=compute_sha256(m_bytes),
        )
        db.add(master)

    src = JobSource(source_name=f"gh-{uuid.uuid4().hex[:6]}", connector_type="greenhouse")
    db.add(src)
    db.flush()

    job = Job(
        source_id=src.id,
        source_job_id="404",
        canonical_url=f"https://boards.greenhouse.io/s4/jobs/{uuid.uuid4().hex[:8]}",
        dedup_key="e" * 64,
        content_checksum="f" * 64,
        company="DiscoverAI",
        role_title="Applied AI Engineer",
        location="Bengaluru, India",
        remote_policy="hybrid",
        employment_type="full_time",
        raw_jd="Required: Python, LangGraph, FastAPI. Preferred: PostgreSQL. 0-1 yrs.",
    )
    db.add(job)
    db.flush()

    req = JobRequirement(
        job_id=job.id,
        role_category="Applied AI Engineer",
        required_skills=["Python", "LangGraph", "FastAPI"],
        preferred_skills=["PostgreSQL"],
        technical_stack=["Python", "LangGraph", "FastAPI", "PostgreSQL"],
        responsibilities=["Build deterministic multi-agent workflows"],
        experience_min=0.0,
        experience_max=1.0,
        fresher_eligible=True,
    )
    db.add(req)
    db.commit()
    return user, profile, master, src, job, req


@pytest.mark.asyncio
async def test_resume_subgraph_end_to_end_idempotency_and_never_auto_approved(db_session, tmp_path):
    user, profile, master, _src, job, _req = _seed_stage4_world(db_session, tmp_path, with_master=True)

    state = await run_resume_intelligence_workflow(
        db_session,
        user_id=user.id,
        job_id=job.id,
        trigger_mode="manual",
        use_llm=False,
        storage_dir=tmp_path,
    )
    assert state["status"] == "completed"
    assert state["approval_status"] == "pending_review"
    assert state["validation_passed"] is True
    assert state["pdf_generated"] is True
    assert state["version_number"] == 1

    resume_row = db_session.get(Resume, uuid.UUID(state["resume_id"]))
    mapping_row = db_session.get(ResumeJobMapping, uuid.UUID(state["mapping_id"]))
    assert resume_row is not None and mapping_row is not None
    assert resume_row.is_master is False
    assert resume_row.master_resume_id == master.id
    assert resume_row.generation_status == "completed"
    assert resume_row.validation_passed is True
    assert resume_row.pdf_path and Path(resume_row.pdf_path).is_file()
    with fitz.open(resume_row.pdf_path) as doc:
        text = doc[0].get_text("text")
        assert "ORBIT" in text
        assert "LangGraph" in text

    # Approval strictly remains pending_review (no duplication on Resume, canonical on mapping)
    assert mapping_row.is_active is True
    assert mapping_row.approval_status == "pending_review"
    assert mapping_row.approved_at is None
    assert "required_skill_coverage" in resume_row.evaluation_report
    assert "disclaimer" in resume_row.evaluation_report

    wf = db_session.scalar(select(WorkflowRun).where(WorkflowRun.thread_id == state["run_id"]))
    assert wf is not None and wf.status == "completed" and wf.workflow_type == "resume_intelligence"
    audit = db_session.scalar(
        select(AuditLog).where(
            AuditLog.resource_id == str(mapping_row.id),
            AuditLog.action == "tailored_resume_generated",
        )
    )
    assert audit is not None

    # Idempotent second non-forced call reuses existing completed active mapping
    state2 = await run_resume_intelligence_workflow(
        db_session,
        user_id=user.id,
        job_id=job.id,
        trigger_mode="auto",
        use_llm=False,
        force_regenerate=False,
        storage_dir=tmp_path,
    )
    assert state2["idempotency_hit"] is True
    assert state2["mapping_id"] == state["mapping_id"]
    assert state2["resume_id"] == state["resume_id"]


@pytest.mark.asyncio
async def test_missing_master_and_claude_unavailable_deterministic_fallback(db_session, tmp_path):
    user, _profile, _master, _src, job, _req = _seed_stage4_world(db_session, tmp_path, with_master=False)

    def claude_503(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "claude unavailable"})

    router = ModelRouter(
        providers={
            "claude": ClaudeProvider(
                api_key="sk-ant-down",
                client=httpx.AsyncClient(transport=httpx.MockTransport(claude_503)),
            ),
            "openai": OpenAICompatibleProvider(api_key=""),  # unconfigured
        },
        max_retries_per_provider=0,
    )

    state = await run_resume_intelligence_workflow(
        db_session,
        user_id=user.id,
        job_id=job.id,
        use_llm=True,
        storage_dir=tmp_path,
        router=router,
    )
    assert state["status"] == "completed"
    assert state["master_analysis"]["has_master_file"] is False
    assert state["tailoring_mode"] == "deterministic_fallback"
    assert state["pdf_generated"] is True
    assert state["approval_status"] == "pending_review"


@pytest.mark.asyncio
async def test_validation_failure_bounded_retry_recovers_then_persistent_failure_blocks_pdf_and_preserves_active(
    db_session, tmp_path
):
    user, profile, _master, _src, job, req = _seed_stage4_world(db_session, tmp_path, with_master=True)
    ev = EvidenceRetriever().retrieve(db_session, profile=profile, job=job, req=req)
    clean_base = deterministic_tailor_resume(evidence=ev, job=job, req=req)

    # Bad payload introduces fabricated skill Kubernetes
    bad_content = clean_base.content.model_copy(
        update={
            "skill_groups": [
                TailoredSkillGroup(category="Cloud", skills=["Python", "Kubernetes"], evidence_ids=[])
            ]
        }
    )
    bad_payload = _LLMTailoringPayload(content=bad_content, diff=clean_base.diff)
    clean_payload = _LLMTailoringPayload(content=clean_base.content, diff=clean_base.diff)

    # Case A: Attempt 0 fails validation, bounded revision retry (Attempt 1) fixes it -> v1 succeeds
    calls_a = {"n": 0}

    def claude_fix_on_retry(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        if "_LLMCrossCheckOutput" in body.get("system", ""):
            return httpx.Response(
                200,
                json={
                    "content": [{"text": json.dumps({"passed": True, "flagged_claim_ids": [], "notes": []})}],
                    "usage": {"input_tokens": 50, "output_tokens": 20},
                },
            )
        calls_a["n"] += 1
        payload = bad_payload if calls_a["n"] == 1 else clean_payload
        return httpx.Response(
            200,
            json={
                "content": [{"text": payload.model_dump_json()}],
                "usage": {"input_tokens": 300, "output_tokens": 150},
            },
        )

    router_a = ModelRouter(
        providers={
            "claude": ClaudeProvider(
                api_key="sk-ant-a",
                client=httpx.AsyncClient(transport=httpx.MockTransport(claude_fix_on_retry)),
            )
        }
    )
    res_v1 = await run_resume_intelligence_workflow(
        db_session,
        user_id=user.id,
        job_id=job.id,
        use_llm=True,
        max_revisions=1,
        storage_dir=tmp_path,
        router=router_a,
    )
    assert calls_a["n"] == 2  # initial + 1 bounded retry
    assert res_v1["revision_attempt"] == 1
    assert res_v1["status"] == "completed"
    assert res_v1["pdf_generated"] is True
    assert res_v1["approval_status"] == "pending_review"
    v1_mapping_id = uuid.UUID(res_v1["mapping_id"])

    # Case B: Persistent hallucination on forced v2 -> bounded stops after max_revisions=1 (2 LLM calls),
    # blocks PDF generation, and leaves v1 active!
    calls_b = {"n": 0}

    def claude_always_hallucinates(_request: httpx.Request) -> httpx.Response:
        calls_b["n"] += 1
        return httpx.Response(
            200,
            json={
                "content": [{"text": bad_payload.model_dump_json()}],
                "usage": {"input_tokens": 200, "output_tokens": 100},
            },
        )

    router_b = ModelRouter(
        providers={
            "claude": ClaudeProvider(
                api_key="sk-ant-b",
                client=httpx.AsyncClient(transport=httpx.MockTransport(claude_always_hallucinates)),
            )
        }
    )
    res_v2_fail = await run_resume_intelligence_workflow(
        db_session,
        user_id=user.id,
        job_id=job.id,
        use_llm=True,
        force_regenerate=True,
        max_revisions=1,
        storage_dir=tmp_path,
        router=router_b,
    )
    assert calls_b["n"] == 2  # strictly bounded, zero infinite loop
    assert res_v2_fail["status"] == "validation_failed"
    assert res_v2_fail["validation_passed"] is False
    assert res_v2_fail["pdf_generated"] is False
    assert res_v2_fail["approval_status"] == "pending_review"

    failed_resume = db_session.get(Resume, uuid.UUID(res_v2_fail["resume_id"]))
    assert failed_resume is not None
    assert failed_resume.pdf_path is None  # NO PDF generated
    assert failed_resume.generation_status == "validation_failed"

    # Previous active v1 MUST remain active and unapproved
    db_session.expire_all()
    v1_mapping = db_session.get(ResumeJobMapping, v1_mapping_id)
    v2_mapping = db_session.get(ResumeJobMapping, uuid.UUID(res_v2_fail["mapping_id"]))
    assert v1_mapping is not None and v1_mapping.is_active is True and v1_mapping.approval_status == "pending_review"
    assert v2_mapping is not None and v2_mapping.is_active is False and v2_mapping.status == "validation_failed"


@pytest.mark.asyncio
async def test_previous_active_resume_preserved_if_replacement_persistence_fails(
    db_session, tmp_path, monkeypatch
):
    user, _profile, _master, _src, job, _req = _seed_stage4_world(db_session, tmp_path, with_master=True)
    v1 = await run_resume_intelligence_workflow(
        db_session, user_id=user.id, job_id=job.id, use_llm=False, storage_dir=tmp_path
    )
    v1_mapping_id = uuid.UUID(v1["mapping_id"])

    # Force a storage write crash during replacement persistence
    def _boom(**_kwargs):
        raise OSError("disk full during replacement")

    monkeypatch.setattr("app.graphs.resume_intelligence_graph.store_resume_bytes", _boom)

    with pytest.raises(OSError, match="disk full"):
        await run_resume_intelligence_workflow(
            db_session,
            user_id=user.id,
            job_id=job.id,
            use_llm=False,
            force_regenerate=True,
            storage_dir=tmp_path,
        )

    db_session.expire_all()
    v1_after = db_session.get(ResumeJobMapping, v1_mapping_id)
    assert v1_after is not None
    assert v1_after.is_active is True
    assert v1_after.approval_status == "pending_review"


@pytest.mark.asyncio
async def test_checkpoint_persistence_and_workflow_resumption(db_session, tmp_path):
    user, _profile, _master, _src, job, _req = _seed_stage4_world(db_session, tmp_path, with_master=True)

    tailor_calls = {"n": 0}
    orig_tailor = ResumeTailoringAgent.tailor

    async def _counting_tailor(self, *args, **kwargs):
        tailor_calls["n"] += 1
        return await orig_tailor(self, *args, **kwargs)

    # Compile with MemorySaver and interrupt right before PDF rendering
    saver = MemorySaver()
    agent = ResumeTailoringAgent()
    agent.tailor = _counting_tailor.__get__(agent, ResumeTailoringAgent)  # type: ignore[method-assign]

    graph = build_resume_intelligence_graph(
        db_session,
        checkpointer=saver,
        tailor_agent=agent,
        storage_dir=tmp_path,
    )
    # Recompile subgraph with interrupt_before on evaluate_and_render_pdf? We can check compiled snapshot/nodes,
    # or test checkpointer state directly via update_state / get_state and re-invoke on graph compiled with interrupt_before:
    thread_id = "resume-cp-test-1"
    config = {"configurable": {"thread_id": thread_id}}

    # Inject interrupt_before on graph's builder isn't needed if we invoke with interrupt_before via stream/compile;
    # let's compile from builder with interrupt_before cleanly? Wait: graph is already compiled, let's test get_state + stream/interrupt via langgraph config!
    # In LangGraph, ainvoke(..., interrupt_before=["evaluate_and_render_pdf"]) works dynamically OR checkpoint saved!
    paused_state = await graph.ainvoke(
        {
            "run_id": thread_id,
            "user_id": str(user.id),
            "job_id": str(job.id),
            "trigger_mode": "manual",
            "use_llm": False,
            "force_regenerate": False,
            "max_revisions": 1,
            "storage_dir": str(tmp_path),
        },
        config=config,
        interrupt_before=["evaluate_and_render_pdf"],
    )
    assert tailor_calls["n"] == 1
    assert paused_state["validation_passed"] is True
    assert paused_state["pdf_generated"] is False
    snap = await graph.aget_state(config)
    assert "evaluate_and_render_pdf" in snap.next

    # Resume from persisted checkpoint (None input) -> finishes PDF & DB persistence WITHOUT re-running tailor_resume
    resumed_state = await graph.ainvoke(None, config=config)
    assert tailor_calls["n"] == 1  # NOT re-run!
    assert resumed_state["status"] == "completed"
    assert resumed_state["pdf_generated"] is True
    assert resumed_state["approval_status"] == "pending_review"


@pytest.mark.asyncio
async def test_discovery_and_intelligence_succeed_when_auto_tailoring_fails(db_session, tmp_path):
    user, _profile, _master, src, job, _req = _seed_stage4_world(db_session, tmp_path, with_master=True)

    # Greenhouse mock for discovery
    def gh_handler(request: httpx.Request) -> httpx.Response:
        if "boards-api.greenhouse.io" in str(request.url):
            return httpx.Response(
                200,
                json={
                    "jobs": [
                        {
                            "id": 901,
                            "title": "Applied AI Engineer (0-1 yrs)",
                            "location": {"name": "Bengaluru, India"},
                            "first_published": datetime.now(timezone.utc).isoformat(),
                            "absolute_url": "https://boards.greenhouse.io/acme/jobs/901",
                            "content": "Python, LangGraph, FastAPI, PostgreSQL. 0-1 yrs experience.",
                        }
                    ],
                    "meta": {"total_pages": 1},
                },
            )
        return httpx.Response(200, json={})

    class FailingDispatcher:
        async def dispatch_auto_tailoring(self, *_args, **_kwargs):
            raise RuntimeError("Celery broker unavailable")

    src.base_url = "https://boards-api.greenhouse.io/v1/boards/acme/jobs"
    src.config = {"board_token": "acme"}
    db_session.commit()

    # Discovery MUST still succeed and return scored jobs even when dispatcher raises!
    disc_report = await run_discovery_workflow(
        db_session,
        user_id=user.id,
        source_ids=[src.id],
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(gh_handler)),
        auto_tailor_dispatcher=FailingDispatcher(),
        auto_tailor_settings=Settings(_env_file=None, auto_tailor_enabled=True, auto_tailor_min_score=50),
    )
    assert disc_report["discovered_count"] >= 1
    assert disc_report["auto_tailoring"]["failed_count"] >= 1
    assert disc_report["auto_tailoring"]["results"][0]["status"] == "failed"

    # Intelligence workflow MUST also succeed even when dispatcher raises!
    intel_report = await run_job_intelligence_workflow(
        db_session,
        user_id=user.id,
        job_id=job.id,
        use_llm=False,
        auto_tailor_dispatcher=FailingDispatcher(),
        auto_tailor_settings=Settings(_env_file=None, auto_tailor_enabled=True, auto_tailor_min_score=50),
    )
    assert intel_report["overall_score"] > 0
    assert intel_report["auto_tailoring"]["failed_count"] == 1
    assert db_session.scalar(select(JobMatch).where(JobMatch.job_id == job.id)) is not None


@pytest.mark.asyncio
async def test_auto_tailor_disabled_below_threshold_and_quota_exhausted_safeguards(db_session, tmp_path):
    user, profile, _master, _src, job, _req = _seed_stage4_world(db_session, tmp_path, with_master=True)

    # Create a second high-scoring job for priority ordering test
    job2 = Job(
        source_id=job.source_id,
        source_job_id="405",
        canonical_url="https://boards.greenhouse.io/s4/jobs/405",
        dedup_key="g" * 64,
        content_checksum="h" * 64,
        company="BetaAI",
        role_title="AI Engineer",
        raw_jd="Python, LangGraph, FastAPI",
    )
    db_session.add(job2)
    db_session.commit()

    recorded_dispatches: list[uuid.UUID] = []

    class SpyDispatcher:
        async def dispatch_auto_tailoring(
            self,
            _db,
            *,
            job_id: uuid.UUID,
            quota_date: date,
            **_kwargs,
        ) -> AutoTailorDispatchResult:
            recorded_dispatches.append(job_id)
            return AutoTailorDispatchResult(
                job_id=job_id,
                dispatched=True,
                status="queued",
                quota_date=quota_date,
                generation_job_id=uuid.uuid4(),
            )

    candidates = [
        {"job_id": str(job.id), "score": 78.0, "eligible": True},
        {"job_id": str(job2.id), "score": 92.0, "eligible": True},
    ]

    # 1. Disabled via AUTO_TAILOR_ENABLED=False
    disabled_res = await trigger_auto_tailor_candidates(
        db_session,
        user_id=user.id,
        profile_id=profile.id,
        scored_candidates=candidates,
        dispatcher=SpyDispatcher(),
        settings=Settings(_env_file=None, auto_tailor_enabled=False, auto_tailor_min_score=70),
    )
    assert disabled_res.dispatched_count == 0
    assert all(r.reason == "auto_tailor_disabled" for r in disabled_res.results)

    # 2. Below AUTO_TAILOR_MIN_SCORE (separate from MIN_JOB_MATCH_SCORE=60!)
    threshold_res = await trigger_auto_tailor_candidates(
        db_session,
        user_id=user.id,
        profile_id=profile.id,
        scored_candidates=[{"job_id": str(job.id), "score": 72.0, "eligible": True}],
        dispatcher=SpyDispatcher(),
        settings=Settings(
            _env_file=None,
            auto_tailor_enabled=True,
            min_job_match_score=60,
            auto_tailor_min_score=80,
        ),
    )
    assert threshold_res.dispatched_count == 0
    assert threshold_res.results[0].reason == "below_auto_tailor_min_score"

    # 3. Clean boundary when dispatcher is None (must NOT pretend jobs are queued)
    deferred_res = await trigger_auto_tailor_candidates(
        db_session,
        user_id=user.id,
        profile_id=profile.id,
        scored_candidates=candidates,
        dispatcher=None,
        settings=Settings(_env_file=None, auto_tailor_enabled=True, auto_tailor_min_score=75),
    )
    assert deferred_res.dispatched_count == 0
    assert all(r.status == "deferred_no_dispatcher" for r in deferred_res.results)
    assert db_session.scalar(select(ResumeGenerationJob)) is None

    # 4. Priority ordering + quota cap (daily_limit=1 dispatches highest score job2=92.0 first, skips job=78.0)
    recorded_dispatches.clear()
    quota_res = await trigger_auto_tailor_candidates(
        db_session,
        user_id=user.id,
        profile_id=profile.id,
        scored_candidates=candidates,
        dispatcher=SpyDispatcher(),
        settings=Settings(
            _env_file=None,
            auto_tailor_enabled=True,
            auto_tailor_min_score=75,
            auto_tailor_daily_limit=1,
        ),
    )
    assert quota_res.dispatched_count == 1
    assert recorded_dispatches == [job2.id]
    assert quota_res.results[1].reason == "daily_quota_exhausted"

    # 5. Persisted AutoTailorDailyQuota already at daily_limit -> 0 dispatched
    from app.services.auto_tailor_trigger import resolve_user_quota_date

    cfg = Settings(_env_file=None, auto_tailor_enabled=True, auto_tailor_daily_limit=2, auto_tailor_min_score=75)
    q_date = resolve_user_quota_date(db_session, user.id, cfg)
    db_session.add(AutoTailorDailyQuota(user_id=user.id, quota_date=q_date, reserved_count=2))
    db_session.commit()

    exhausted_res = await trigger_auto_tailor_candidates(
        db_session,
        user_id=user.id,
        profile_id=profile.id,
        scored_candidates=candidates,
        dispatcher=SpyDispatcher(),
        settings=cfg,
    )
    assert exhausted_res.dispatched_count == 0
    assert all(r.reason == "daily_quota_exhausted" for r in exhausted_res.results)
