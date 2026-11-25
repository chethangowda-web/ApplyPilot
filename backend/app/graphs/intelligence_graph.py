import uuid
from typing import Any, TypedDict
from langgraph.graph import END, START, StateGraph
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.agents.job_intelligence.jd_analyzer import JDAnalyzerAgent
from app.core.config import Settings
from app.core.logging import get_logger
from app.database.models import (
    CandidateProfile,
    Job,
    JobAnalysisHistory,
    JobMatch,
    JobRequirement,
    WorkflowRun,
)
from app.graphs.checkpointer import managed_checkpointer
from app.llm.router import ModelRouter
from app.services.audit_service import record_audit_event
from app.services.auto_tailor_trigger import (
    AutoTailorDispatcher,
    trigger_auto_tailor_candidates,
)
from app.services.outbox_service import TransactionalOutboxDispatcher
from app.services.scoring_service import upsert_job_match
from app.services.semantic_matching_service import sync_job_and_profile_embeddings

logger = get_logger("applypilot.intelligence_graph")


class IntelligenceGraphState(TypedDict, total=False):
    run_id: str
    user_id: str
    job_id: str
    profile_id: str
    use_llm: bool
    allow_reanalysis: bool
    already_completed: bool
    extraction_mode: str
    model_provider: str | None
    embedding_source: str
    match_id: str
    overall_score: float
    eligible: bool
    confidence: float
    analysis_result: dict[str, Any]
    errors: list[dict[str, Any]]


def _persist_jd_extraction(db: Session, job: Job, ext: Any) -> JobRequirement:
    req = db.scalar(select(JobRequirement).where(JobRequirement.job_id == job.id))
    if req is None:
        req = JobRequirement(job_id=job.id, role_category=ext.role_category)
        db.add(req)

    req.role_category = ext.role_category
    req.location = ext.location or job.location
    req.remote_policy = ext.remote_policy
    req.employment_type = ext.employment_type
    req.required_skills = [i.value for i in ext.required_skills]
    req.preferred_skills = [i.value for i in ext.preferred_skills]
    req.trainable_skills = [i.value for i in ext.trainable_skills]
    req.responsibilities = [i.value for i in ext.responsibilities]
    req.technical_stack = list(dict.fromkeys([*req.required_skills, *req.preferred_skills]))
    req.experience_min = ext.experience_min
    req.experience_max = ext.experience_max
    req.education_requirements = [i.value for i in ext.education_requirements]
    req.graduation_criteria = [i.model_dump() for i in ext.graduation_criteria]
    req.mandatory_qualifications = [i.model_dump() for i in ext.mandatory_qualifications]
    req.preferred_qualifications = [i.model_dump() for i in ext.preferred_qualifications]
    req.application_instructions = [i.model_dump() for i in ext.application_instructions]
    req.evidence_items = {
        "required_skills": [i.model_dump() for i in ext.required_skills],
        "preferred_skills": [i.model_dump() for i in ext.preferred_skills],
        "responsibilities": [i.model_dump() for i in ext.responsibilities],
        "education_requirements": [i.model_dump() for i in ext.education_requirements],
        "experience_evidence": ext.experience_evidence,
    }
    req.extraction_mode = ext.extraction_mode
    req.uncertainties = list(ext.uncertainties)
    req.prompt_injection_detected = bool(ext.prompt_injection_detected)
    db.flush()
    return req


def build_intelligence_graph(
    db: Session,
    *,
    router: ModelRouter | None = None,
    checkpointer: Any,
):
    """Build and compile the Job Intelligence LangGraph subgraph."""
    analyzer = JDAnalyzerAgent(router=router)

    def _update_workflow_run(
        thread_id: str,
        *,
        user_id: uuid.UUID,
        job_id: uuid.UUID,
        stage: str,
        status: str,
        metadata: dict[str, Any] | None = None,
    ) -> WorkflowRun:
        run = db.scalar(select(WorkflowRun).where(WorkflowRun.thread_id == thread_id))
        if run is None:
            run = WorkflowRun(
                thread_id=thread_id,
                user_id=user_id,
                job_id=job_id,
                workflow_type="job_intelligence",
                current_stage=stage,
                status=status,
                checkpoint_metadata=metadata or {},
            )
            db.add(run)
        else:
            run.current_stage = stage
            run.status = status
            if metadata:
                merged = dict(run.checkpoint_metadata or {})
                merged.update(metadata)
                run.checkpoint_metadata = merged
        db.flush()
        return run

    def load_context(state: IntelligenceGraphState) -> dict[str, Any]:
        uid = uuid.UUID(state["user_id"])
        jid = uuid.UUID(state["job_id"])
        existing_run = db.scalar(select(WorkflowRun).where(WorkflowRun.thread_id == state["run_id"]))
        if (
            existing_run is not None
            and existing_run.status == "completed"
            and not state.get("allow_reanalysis", False)
        ):
            return {
                "already_completed": True,
                "analysis_result": dict(existing_run.checkpoint_metadata or {}),
            }

        job = db.get(Job, jid)
        if job is None:
            raise ValueError(f"Job {jid} not found")
        profile = db.scalar(
            select(CandidateProfile)
            .where(CandidateProfile.user_id == uid)
            .options(
                selectinload(CandidateProfile.skills),
                selectinload(CandidateProfile.projects),
                selectinload(CandidateProfile.experiences),
                selectinload(CandidateProfile.certifications),
            )
        )
        if profile is None:
            raise ValueError("Candidate profile not found")

        _update_workflow_run(
            state["run_id"],
            user_id=uid,
            job_id=jid,
            stage="load_context",
            status="running",
        )
        return {
            "profile_id": str(profile.id),
            "already_completed": False,
            "errors": list(state.get("errors") or []),
        }

    async def extract_jd_requirements(state: IntelligenceGraphState) -> dict[str, Any]:
        if state.get("already_completed"):
            return {}
        jid = uuid.UUID(state["job_id"])
        uid = uuid.UUID(state["user_id"])
        job = db.get(Job, jid)
        assert job is not None
        ext = await analyzer.analyze(
            job_title=job.role_title,
            company=job.company,
            raw_jd=job.raw_jd,
            location=job.location,
            workplace_type=job.remote_policy,
            commitment=job.employment_type,
            use_llm=bool(state.get("use_llm", True)),
        )
        _persist_jd_extraction(db, job, ext)
        _update_workflow_run(
            state["run_id"],
            user_id=uid,
            job_id=jid,
            stage="extract_jd_requirements",
            status="running",
            metadata={"extraction_mode": ext.extraction_mode},
        )
        return {
            "extraction_mode": ext.extraction_mode,
            "model_provider": ext.llm_usage.get("provider") if ext.llm_usage else None,
        }

    async def sync_semantic_embeddings(state: IntelligenceGraphState) -> dict[str, Any]:
        if state.get("already_completed"):
            return {}
        jid = uuid.UUID(state["job_id"])
        pid = uuid.UUID(state["profile_id"])
        uid = uuid.UUID(state["user_id"])
        job = db.get(Job, jid)
        profile = db.scalar(
            select(CandidateProfile)
            .where(CandidateProfile.id == pid)
            .options(
                selectinload(CandidateProfile.skills),
                selectinload(CandidateProfile.projects),
                selectinload(CandidateProfile.experiences),
                selectinload(CandidateProfile.certifications),
            )
        )
        assert job is not None and profile is not None
        emb_src = await sync_job_and_profile_embeddings(db, job=job, profile=profile, router=router)
        _update_workflow_run(
            state["run_id"],
            user_id=uid,
            job_id=jid,
            stage="sync_semantic_embeddings",
            status="running",
            metadata={"embedding_source": emb_src},
        )
        return {"embedding_source": emb_src}

    def score_and_explain(state: IntelligenceGraphState) -> dict[str, Any]:
        if state.get("already_completed"):
            return {}
        jid = uuid.UUID(state["job_id"])
        pid = uuid.UUID(state["profile_id"])
        uid = uuid.UUID(state["user_id"])
        job = db.get(Job, jid)
        req = db.scalar(select(JobRequirement).where(JobRequirement.job_id == jid))
        profile = db.scalar(
            select(CandidateProfile)
            .where(CandidateProfile.id == pid)
            .options(
                selectinload(CandidateProfile.skills),
                selectinload(CandidateProfile.projects),
                selectinload(CandidateProfile.experiences),
                selectinload(CandidateProfile.certifications),
            )
        )
        assert job is not None and profile is not None
        match = upsert_job_match(db, job=job, req=req, profile=profile)
        _update_workflow_run(
            state["run_id"],
            user_id=uid,
            job_id=jid,
            stage="score_and_explain",
            status="running",
        )
        return {
            "match_id": str(match.id),
            "overall_score": match.score,
            "eligible": match.eligible,
            "confidence": match.confidence,
        }

    def record_history_and_finish(state: IntelligenceGraphState) -> dict[str, Any]:
        if state.get("already_completed"):
            return {}
        jid = uuid.UUID(state["job_id"])
        pid = uuid.UUID(state["profile_id"])
        uid = uuid.UUID(state["user_id"])
        job = db.get(Job, jid)
        req = db.scalar(select(JobRequirement).where(JobRequirement.job_id == jid))
        match = db.get(JobMatch, uuid.UUID(state["match_id"]))
        assert job is not None and match is not None

        snapshot = {
            "run_id": state["run_id"],
            "job_id": str(job.id),
            "profile_id": str(pid),
            "extraction_mode": state.get("extraction_mode", "deterministic"),
            "model_provider": state.get("model_provider"),
            "embedding_source": state.get("embedding_source", "local_lexical_hash"),
            "overall_score": match.score,
            "eligible": match.eligible,
            "hard_constraints_passed": match.hard_constraints_passed,
            "required_skill_coverage": match.required_skill_coverage,
            "preferred_skill_coverage": match.preferred_skill_coverage,
            "semantic_similarity": match.semantic_similarity,
            "recommendation": match.recommendation,
            "confidence": match.confidence,
            "eligibility_blockers": match.eligibility_blockers,
            "uncertainties": match.uncertainties,
            "prompt_injection_detected": bool(req.prompt_injection_detected) if req else False,
        }
        hist = JobAnalysisHistory(
            job_id=job.id,
            profile_id=pid,
            run_id=state["run_id"],
            extraction_mode=snapshot["extraction_mode"],
            model_provider=snapshot["model_provider"],
            overall_score=match.score,
            eligible=match.eligible,
            confidence=match.confidence,
            snapshot=snapshot,
        )
        db.add(hist)
        _update_workflow_run(
            state["run_id"],
            user_id=uid,
            job_id=jid,
            stage="completed",
            status="completed",
            metadata=snapshot,
        )
        record_audit_event(
            db,
            user_id=uid,
            actor="intelligence_subgraph",
            action="job_intelligence_completed",
            resource_type="job",
            resource_id=str(job.id),
            metadata={
                "run_id": state["run_id"],
                "extraction_mode": snapshot["extraction_mode"],
                "overall_score": match.score,
                "eligible": match.eligible,
            },
        )
        db.commit()
        return {"analysis_result": snapshot}

    graph = StateGraph(IntelligenceGraphState)
    graph.add_node("load_context", load_context)
    graph.add_node("extract_jd_requirements", extract_jd_requirements)
    graph.add_node("sync_semantic_embeddings", sync_semantic_embeddings)
    graph.add_node("score_and_explain", score_and_explain)
    graph.add_node("record_history_and_finish", record_history_and_finish)

    graph.add_edge(START, "load_context")
    graph.add_edge("load_context", "extract_jd_requirements")
    graph.add_edge("extract_jd_requirements", "sync_semantic_embeddings")
    graph.add_edge("sync_semantic_embeddings", "score_and_explain")
    graph.add_edge("score_and_explain", "record_history_and_finish")
    graph.add_edge("record_history_and_finish", END)

    return graph.compile(checkpointer=checkpointer)


async def run_job_intelligence_workflow(
    db: Session,
    *,
    user_id: uuid.UUID,
    job_id: uuid.UUID,
    run_id: str | None = None,
    use_llm: bool = True,
    allow_reanalysis: bool = True,
    router: ModelRouter | None = None,
    checkpointer: Any | None = None,
    auto_tailor_dispatcher: AutoTailorDispatcher | None = None,
    auto_tailor_settings: Settings | None = None,
) -> dict[str, Any]:
    """Execute the Job Intelligence subgraph with PostgreSQL/Memory checkpointing and failure capture."""
    rid = run_id or f"intel-{uuid.uuid4().hex[:12]}"
    try:
        async with managed_checkpointer(db, override_checkpointer=checkpointer) as cp:
            compiled = build_intelligence_graph(db, router=router, checkpointer=cp)
            final_state = await compiled.ainvoke(
                {
                    "run_id": rid,
                    "user_id": str(user_id),
                    "job_id": str(job_id),
                    "use_llm": use_llm,
                    "allow_reanalysis": allow_reanalysis,
                },
                config={"configurable": {"thread_id": rid}},
            )
            snapshot = dict(final_state["analysis_result"])
    except Exception as exc:
        logger.warning(
            "intelligence_workflow_failed",
            extra={"extra_fields": {"run_id": rid, "error_type": type(exc).__name__}},
        )
        run = db.scalar(select(WorkflowRun).where(WorkflowRun.thread_id == rid))
        if run is not None:
            run.status = "failed"
            run.checkpoint_metadata = {
                **(run.checkpoint_metadata or {}),
                "error_type": type(exc).__name__,
                "error": str(exc),
            }
            db.commit()
        raise

    # Non-blocking auto-tailoring trigger post-commit
    profile_id_str = snapshot.get("profile_id") or final_state.get("profile_id")
    if profile_id_str:
        try:
            active_dispatcher = auto_tailor_dispatcher or TransactionalOutboxDispatcher(
                settings=auto_tailor_settings
            )
            batch = await trigger_auto_tailor_candidates(
                db,
                user_id=user_id,
                profile_id=uuid.UUID(str(profile_id_str)),
                scored_candidates=[
                    {
                        "job_id": str(job_id),
                        "score": snapshot.get("overall_score", 0.0),
                        "eligible": snapshot.get("eligible", False),
                    }
                ],
                dispatcher=active_dispatcher,
                settings=auto_tailor_settings,
            )
            snapshot["auto_tailoring"] = batch.model_dump(mode="json")
        except Exception as exc:
            logger.warning(
                "intelligence_auto_tailor_non_blocking_error",
                extra={"extra_fields": {"run_id": rid, "error_type": type(exc).__name__}},
            )
            snapshot["auto_tailoring"] = {"error": type(exc).__name__}

    return snapshot
