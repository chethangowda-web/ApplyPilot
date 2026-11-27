import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, TypedDict
import httpx
from langgraph.graph import END, START, StateGraph
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.agents.job_intelligence.jd_analyzer import JDAnalyzerAgent
from app.connectors.base import BlockedSourceError, ConnectorError, RateLimitError
from app.connectors.registry import build_connector
from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.database.models import CandidateProfile, Job, JobAnalysisHistory, JobSource, WorkflowRun
from app.graphs.checkpointer import managed_checkpointer
from app.graphs.intelligence_graph import _persist_jd_extraction
from app.llm.router import ModelRouter
from app.schemas.workflow import NormalizedJob
from app.services.audit_service import record_audit_event
from app.services.auto_tailor_trigger import (
    AutoTailorDispatcher,
    trigger_auto_tailor_candidates,
)
from app.services.outbox_service import TransactionalOutboxDispatcher
from app.services.deduplication_service import mark_job_expired, upsert_discovered_job
from app.services.scoring_service import upsert_job_match
from app.services.semantic_matching_service import sync_job_and_profile_embeddings

logger = get_logger("applypilot.discovery_graph")


class DiscoveryGraphState(TypedDict, total=False):
    run_id: str
    user_id: str
    profile_id: str
    query: str
    location: str | None
    max_pages: int
    source_ids: list[str]
    search_queries: list[str]
    fetched_records: list[dict[str, Any]]
    discovered_job_ids: list[str]
    created_count: int
    duplicate_count: int
    reposted_count: int
    expired_count: int
    scored_matches: list[dict[str, Any]]
    source_metrics: list[dict[str, Any]]
    ai_metrics: dict[str, Any]
    started_monotonic: float
    discovery_report: dict[str, Any]
    errors: list[dict[str, Any]]


def build_discovery_graph(
    db: Session,
    *,
    connector_factory: Callable[[JobSource], Any] | None = None,
    http_client: httpx.AsyncClient | None = None,
    checkpointer: Any | None = None,
    router: ModelRouter | None = None,
):
    """Build and compile the stateful LangGraph Job Discovery + Intelligence subgraph."""
    settings = get_settings()
    analyzer = JDAnalyzerAgent(router=router)

    def load_search_preferences(state: DiscoveryGraphState) -> dict[str, Any]:
        uid = uuid.UUID(state["user_id"])
        profile = db.scalar(select(CandidateProfile).where(CandidateProfile.user_id == uid))
        return {
            "profile_id": str(profile.id) if profile else "",
            "errors": list(state.get("errors") or []),
            "created_count": 0,
            "duplicate_count": 0,
            "reposted_count": 0,
            "expired_count": 0,
        }

    def generate_search_queries(state: DiscoveryGraphState) -> dict[str, Any]:
        base_q = (state.get("query") or "").strip()
        queries = [base_q] if base_q else [""]
        return {"search_queries": queries}

    async def run_source_connectors(state: DiscoveryGraphState) -> dict[str, Any]:
        stmt = select(JobSource).where(JobSource.enabled.is_(True))
        requested_ids = state.get("source_ids") or []
        if requested_ids:
            uuid_ids = [uuid.UUID(s) for s in requested_ids]
            stmt = stmt.where(JobSource.id.in_(uuid_ids))
        sources = list(db.scalars(stmt).all())

        fetched: list[dict[str, Any]] = []
        errors = list(state.get("errors") or [])
        source_metrics: list[dict[str, Any]] = []
        queries = state.get("search_queries") or [""]
        max_pages = int(state.get("max_pages") or 3)
        smart_filter = bool(getattr(settings, "discovery_smart_filter_enabled", True))
        max_analyzed = int(getattr(settings, "discovery_max_analyzed_jobs_per_run", 40))

        for src in sources:
            src.last_run_at = datetime.now(timezone.utc)
            try:
                connector = (
                    connector_factory(src)
                    if connector_factory is not None
                    else build_connector(src, client=http_client)
                )
                src_cfg = dict(src.config or {})
                per_board_cap = max(5, max_analyzed // max(1, len(sources)) + 5)
                src_smart_filter = bool(src_cfg.get("smart_filter", smart_filter))
                for q in queries:
                    items: list[NormalizedJob] = await connector.search(
                        query=q,
                        location=state.get("location"),
                        filters={
                            "max_pages": max_pages,
                            "smart_filter": src_smart_filter,
                            "max_results": per_board_cap,
                            "cursor": src_cfg.get("continuation_cursor"),
                        },
                    )
                    for item in items:
                        fetched.append({"source_id": str(src.id), "job": item.model_dump(mode="json")})

                scan_m = getattr(connector, "last_scan_metrics", None)
                if scan_m is not None:
                    m_dict = scan_m.to_dict() if hasattr(scan_m, "to_dict") else dict(scan_m)
                    m_dict["source_name"] = src.source_name
                    source_metrics.append(m_dict)
                    if m_dict.get("next_cursor") is not None:
                        src_cfg["continuation_cursor"] = m_dict["next_cursor"]
                    else:
                        src_cfg.pop("continuation_cursor", None)
                    src.config = src_cfg

                health = connector.get_health()
                src.health_status = health.status
                src.consecutive_failures = health.consecutive_failures
                src.last_error = health.last_error
            except (BlockedSourceError, RateLimitError, ConnectorError) as exc:
                src.consecutive_failures += 1
                src.health_status = "unhealthy" if src.consecutive_failures >= 3 else "degraded"
                src.last_error = str(exc)
                err_entry = {
                    "source_id": str(src.id),
                    "source_name": src.source_name,
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                }
                errors.append(err_entry)
                logger.warning("connector_error", extra={"extra_fields": err_entry})

        db.flush()
        return {"fetched_records": fetched, "errors": errors, "source_metrics": source_metrics}

    def normalize_and_deduplicate(state: DiscoveryGraphState) -> dict[str, Any]:
        fetched = state.get("fetched_records") or []
        discovered_ids: list[str] = []
        created = 0
        duplicates = 0
        reposted = 0
        expired = 0
        seen_source_objs: dict[str, JobSource] = {}

        for entry in fetched:
            src_id_str = entry["source_id"]
            src = seen_source_objs.get(src_id_str)
            if src is None:
                src = db.get(JobSource, uuid.UUID(src_id_str))
                if src is None:
                    continue
                seen_source_objs[src_id_str] = src

            norm = NormalizedJob.model_validate(entry["job"])
            job_row, outcome = upsert_discovered_job(db, source=src, normalized=norm)
            jid = str(job_row.id)
            if jid not in discovered_ids:
                discovered_ids.append(jid)
            if outcome == "created":
                created += 1
            elif outcome == "duplicate":
                duplicates += 1
            elif outcome == "reposted":
                reposted += 1
            if job_row.status == "expired":
                expired += 1

        db.flush()
        return {
            "discovered_job_ids": discovered_ids,
            "created_count": created,
            "duplicate_count": duplicates,
            "reposted_count": reposted,
            "expired_count": expired,
        }

    async def check_posting_status(state: DiscoveryGraphState) -> dict[str, Any]:
        expired = int(state.get("expired_count") or 0)
        errors = list(state.get("errors") or [])
        seen_ids = {uuid.UUID(jid) for jid in (state.get("discovered_job_ids") or [])}

        # Check previously active jobs from queried sources that were absent from this scan
        queried_source_ids = [uuid.UUID(s) for s in (state.get("source_ids") or [])]
        if not queried_source_ids:
            queried_source_ids = [
                s.id for s in db.scalars(select(JobSource).where(JobSource.enabled.is_(True))).all()
            ]

        if queried_source_ids:
            stale_candidates = db.scalars(
                select(Job).where(
                    Job.source_id.in_(queried_source_ids),
                    Job.status == "active",
                )
            ).all()
            for job in stale_candidates:
                if job.id in seen_ids or not job.source_job_id:
                    continue
                src = db.get(JobSource, job.source_id)
                if src is None:
                    continue
                try:
                    connector = (
                        connector_factory(src)
                        if connector_factory is not None
                        else build_connector(src, client=http_client)
                    )
                    live_status = await connector.check_status(job.source_job_id)
                    if live_status == "expired":
                        mark_job_expired(db, job, reason="posting_absent_and_expired")
                        expired += 1
                except ConnectorError as exc:
                    errors.append(
                        {"job_id": str(job.id), "error_type": type(exc).__name__, "message": str(exc)}
                    )
        db.flush()
        return {"expired_count": expired, "errors": errors}

    async def score_and_rank_jobs(state: DiscoveryGraphState) -> dict[str, Any]:
        profile_id_str = state.get("profile_id") or ""
        if not profile_id_str:
            return {"scored_matches": []}

        profile = db.scalar(
            select(CandidateProfile)
            .where(CandidateProfile.id == uuid.UUID(profile_id_str))
            .options(
                selectinload(CandidateProfile.skills),
                selectinload(CandidateProfile.projects),
                selectinload(CandidateProfile.experiences),
                selectinload(CandidateProfile.certifications),
            )
        )
        if profile is None:
            return {"scored_matches": []}

        max_analyzed = int(getattr(settings, "discovery_max_analyzed_jobs_per_run", 40))
        candidate_ids = (state.get("discovered_job_ids") or [])[:max_analyzed]
        scored: list[dict[str, Any]] = []
        est_tokens = 0
        eligible_cnt = 0
        ineligible_cnt = 0
        for jid_str in candidate_ids:
            job = db.get(Job, uuid.UUID(jid_str))
            if job is None:
                continue
            ext = await analyzer.analyze(
                job_title=job.role_title,
                company=job.company,
                raw_jd=job.raw_jd,
                location=job.location,
                workplace_type=job.remote_policy,
                commitment=job.employment_type,
                use_llm=True,
            )
            if ext.llm_usage:
                est_tokens += int(ext.llm_usage.get("input_tokens") or 0) + int(
                    ext.llm_usage.get("output_tokens") or 0
                )
            req = _persist_jd_extraction(db, job, ext)
            await sync_job_and_profile_embeddings(db, job=job, profile=profile, router=router)
            match = upsert_job_match(db, job=job, req=req, profile=profile)
            if match.eligible:
                eligible_cnt += 1
            else:
                ineligible_cnt += 1
            db.add(
                JobAnalysisHistory(
                    job_id=job.id,
                    profile_id=profile.id,
                    run_id=state["run_id"],
                    extraction_mode=ext.extraction_mode,
                    model_provider=ext.llm_usage.get("provider") if ext.llm_usage else None,
                    overall_score=match.score,
                    eligible=match.eligible,
                    confidence=match.confidence,
                    snapshot={
                        "run_id": state["run_id"],
                        "overall_score": match.score,
                        "eligible": match.eligible,
                        "recommendation": match.recommendation,
                        "confidence": match.confidence,
                    },
                )
            )
            scored.append(
                {
                    "job_id": str(job.id),
                    "score": match.score,
                    "eligible": match.eligible,
                    "recommendation": match.recommendation,
                }
            )
        scored.sort(key=lambda x: x["score"], reverse=True)
        db.flush()
        return {
            "scored_matches": scored,
            "ai_metrics": {
                "jobs_submitted_for_ai": len(scored),
                "eligible_count": eligible_cnt,
                "ineligible_count": ineligible_cnt,
                "estimated_llm_tokens": est_tokens,
            },
        }

    def build_discovery_report(state: DiscoveryGraphState) -> dict[str, Any]:
        scored = state.get("scored_matches") or []
        min_score = float(settings.min_job_match_score)
        daily_target = int(settings.daily_application_target)
        # Quality-filtered shortlist capped by daily target, never padded with ineligible jobs
        recommended = [
            item["job_id"]
            for item in scored
            if item["eligible"] and item["score"] >= min_score
        ][:daily_target]

        sources_count = len({r["source_id"] for r in (state.get("fetched_records") or [])}) or len(
            state.get("errors") or []
        )
        src_metrics = list(state.get("source_metrics") or [])
        ai_m = dict(state.get("ai_metrics") or {})
        started_m = float(state.get("started_monotonic") or time.monotonic())
        elapsed_ms = round(max(0.0, (time.monotonic() - started_m) * 1000.0), 2)

        report = {
            "run_id": state["run_id"],
            "sources_queried": sources_count,
            "discovered_count": len(state.get("discovered_job_ids") or []),
            "created_count": int(state.get("created_count") or 0),
            "duplicate_count": int(state.get("duplicate_count") or 0),
            "reposted_count": int(state.get("reposted_count") or 0),
            "expired_count": int(state.get("expired_count") or 0),
            "quality_matches_count": len(recommended),
            "recommended_job_ids": recommended,
            "errors": list(state.get("errors") or []),
            "metrics": {
                "raw_jobs_fetched": sum(int(m.get("raw_fetched") or 0) for m in src_metrics),
                "pages_scanned": sum(int(m.get("pages_scanned") or 0) for m in src_metrics),
                "removed_by_role": sum(int(m.get("removed_by_role") or 0) for m in src_metrics),
                "removed_by_location": sum(int(m.get("removed_by_location") or 0) for m in src_metrics),
                "removed_by_experience": sum(int(m.get("removed_by_experience") or 0) for m in src_metrics),
                "deduplicated_count": int(state.get("duplicate_count") or 0),
                "jobs_submitted_for_ai": int(ai_m.get("jobs_submitted_for_ai") or 0),
                "eligible_count": int(ai_m.get("eligible_count") or 0),
                "ineligible_count": int(ai_m.get("ineligible_count") or 0),
                "api_failures": len(state.get("errors") or []),
                "processing_time_ms": elapsed_ms,
                "estimated_llm_tokens": int(ai_m.get("estimated_llm_tokens") or 0),
                "per_source": src_metrics,
            },
        }
        record_audit_event(
            db,
            user_id=uuid.UUID(state["user_id"]),
            actor="discovery_subgraph",
            action="job_discovery_completed",
            resource_type="discovery_run",
            resource_id=state["run_id"],
            metadata={
                "discovered_count": report["discovered_count"],
                "created_count": report["created_count"],
                "duplicate_count": report["duplicate_count"],
                "quality_matches_count": report["quality_matches_count"],
                "error_count": len(report["errors"]),
            },
        )
        existing_wf = db.scalar(select(WorkflowRun).where(WorkflowRun.thread_id == state["run_id"]))
        if existing_wf is None:
            db.add(
                WorkflowRun(
                    thread_id=state["run_id"],
                    user_id=uuid.UUID(state["user_id"]),
                    workflow_type="discovery",
                    current_stage="completed",
                    status="completed",
                    checkpoint_metadata=report,
                )
            )
        else:
            existing_wf.current_stage = "completed"
            existing_wf.status = "completed"
            existing_wf.checkpoint_metadata = report
        db.commit()
        return {"discovery_report": report}

    graph = StateGraph(DiscoveryGraphState)
    graph.add_node("load_search_preferences", load_search_preferences)
    graph.add_node("generate_search_queries", generate_search_queries)
    graph.add_node("run_source_connectors", run_source_connectors)
    graph.add_node("normalize_and_deduplicate", normalize_and_deduplicate)
    graph.add_node("check_posting_status", check_posting_status)
    graph.add_node("score_and_rank_jobs", score_and_rank_jobs)
    graph.add_node("build_discovery_report", build_discovery_report)

    graph.add_edge(START, "load_search_preferences")
    graph.add_edge("load_search_preferences", "generate_search_queries")
    graph.add_edge("generate_search_queries", "run_source_connectors")
    graph.add_edge("run_source_connectors", "normalize_and_deduplicate")
    graph.add_edge("normalize_and_deduplicate", "check_posting_status")
    graph.add_edge("check_posting_status", "score_and_rank_jobs")
    graph.add_edge("score_and_rank_jobs", "build_discovery_report")
    graph.add_edge("build_discovery_report", END)

    return graph.compile(checkpointer=checkpointer)


async def run_discovery_workflow(
    db: Session,
    *,
    user_id: uuid.UUID,
    query: str = "",
    location: str | None = None,
    source_ids: list[uuid.UUID] | None = None,
    max_pages: int = 3,
    run_id: str | None = None,
    connector_factory: Callable[[JobSource], Any] | None = None,
    http_client: httpx.AsyncClient | None = None,
    checkpointer: Any | None = None,
    router: ModelRouter | None = None,
    auto_tailor_dispatcher: AutoTailorDispatcher | None = None,
    auto_tailor_settings: Settings | None = None,
) -> dict[str, Any]:
    """Execute the Job Discovery + Intelligence LangGraph subgraph and return its discovery report."""
    rid = run_id or f"disc-{uuid.uuid4().hex[:12]}"
    async with managed_checkpointer(db, override_checkpointer=checkpointer) as cp:
        compiled = build_discovery_graph(
            db,
            connector_factory=connector_factory,
            http_client=http_client,
            checkpointer=cp,
            router=router,
        )
        initial_state: DiscoveryGraphState = {
            "run_id": rid,
            "user_id": str(user_id),
            "query": query,
            "location": location,
            "max_pages": max_pages,
            "source_ids": [str(s) for s in source_ids] if source_ids else [],
            "started_monotonic": time.monotonic(),
        }
        final_state = await compiled.ainvoke(
            initial_state,
            config={"configurable": {"thread_id": rid}},
        )
        report = dict(final_state["discovery_report"])

    # Non-blocking post-commit auto-tailoring trigger: never fails discovery
    profile_id_str = final_state.get("profile_id") or ""
    scored = list(final_state.get("scored_matches") or [])
    if profile_id_str and scored:
        try:
            active_dispatcher = auto_tailor_dispatcher or TransactionalOutboxDispatcher(
                settings=auto_tailor_settings
            )
            batch = await trigger_auto_tailor_candidates(
                db,
                user_id=user_id,
                profile_id=uuid.UUID(profile_id_str),
                scored_candidates=scored,
                dispatcher=active_dispatcher,
                settings=auto_tailor_settings,
            )
            report["auto_tailoring"] = batch.model_dump(mode="json")
        except Exception as exc:
            logger.warning(
                "discovery_auto_tailor_non_blocking_error",
                extra={"extra_fields": {"run_id": rid, "error_type": type(exc).__name__}},
            )
            report["auto_tailoring"] = {"error": type(exc).__name__}

    return report
