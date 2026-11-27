from pathlib import Path
from typing import Any, TypedDict

from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.graph import END, START, StateGraph
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.database.models import (
    Application,
    ApplicationAnswer,
    ApprovalRecord,
    CandidateProfile,
    Job,
    Resume,
    ResumeJobMapping,
    User,
)
from app.graphs.checkpointer import _to_psycopg_dsn, ensure_postgres_checkpointer_setup
from app.schemas.applications import ExecutionOptions, ExecutionResult
from app.services.application_policy_service import ApplicationPolicyRouter
from app.services.browser_executor import (
    PlaywrightBrowserExecutor,
    load_verified_resume_snapshot,
    validate_approval_and_answers,
)
from app.services.execution_adapters import (
    ApprovalValidationError,
    ExecutionContext,
    UnsupportedPlatformError,
    get_authorized_official_api_adapter,
)


class ApplicationExecutionState(TypedDict, total=False):
    application_id: str
    user_id: str
    thread_id: str
    mode: str
    policy_verified: bool
    answers_hash: str
    verified_resume_path: str
    execution_result: dict[str, Any]
    error_type: str | None
    error_detail: str | None


def build_application_execution_graph(
    db: Session,
    *,
    user: User,
    profile: CandidateProfile,
    application: Application,
    job: Job,
    resume: Resume,
    mapping: ResumeJobMapping | None,
    answers: list[ApplicationAnswer],
    approval_record: ApprovalRecord,
    options: ExecutionOptions,
    browser_executor: PlaywrightBrowserExecutor,
    settings: Settings | None = None,
    checkpointer: Any | None = None,
):
    """Build stateful Checkpoint D application execution graph reusing Stage 2 policy & Stage 3 browser safety."""
    cfg = settings or get_settings()
    policy_router = ApplicationPolicyRouter(settings=cfg)

    verified_snapshot: dict[str, Any] = {}

    def validate_policy_and_approval(state: ApplicationExecutionState) -> dict[str, Any]:
        if application.user_id != user.id or profile.user_id != user.id:
            raise ApprovalValidationError("Application ownership validation failed")
        if application.status != "SUBMISSION_PENDING":
            raise ApprovalValidationError(f"Unexpected execution state: {application.status}")

        decision = policy_router.evaluate(
            db,
            user=user,
            profile=profile,
            job=job,
            resume=resume,
            preferred_mode=application.application_mode,  # type: ignore[arg-type]
            stage="submit",
        )
        if not decision.allowed:
            raise ApprovalValidationError("; ".join(decision.blocker_reasons))

        pdf_path, _pdf_bytes, pdf_sha256 = load_verified_resume_snapshot(
            user_id=user.id,
            application=application,
            profile=profile,
            resume=resume,
            mapping=mapping,
            storage_root=Path(cfg.storage_dir),
        )
        verified_answers, _vk, _uk, _suk, answers_hash = validate_approval_and_answers(
            application=application,
            user_id=user.id,
            resume=resume,
            verified_resume_checksum=pdf_sha256,
            approval_record=approval_record,
            answers=answers,
            settings=cfg,
        )
        verified_snapshot["pdf_path"] = pdf_path
        verified_snapshot["verified_answers"] = verified_answers
        verified_snapshot["answers_hash"] = answers_hash
        return {
            "policy_verified": True,
            "mode": application.application_mode,
            "answers_hash": answers_hash,
            "verified_resume_path": pdf_path.name,
        }

    def route_execution_mode(state: ApplicationExecutionState) -> str:
        mode = state.get("mode", application.application_mode)
        if mode == "official_api":
            return "official_api_node"
        if mode == "manual":
            return "manual_handoff_node"
        return "browser_prefill_node"

    def official_api_node(_state: ApplicationExecutionState) -> dict[str, Any]:
        adapter = get_authorized_official_api_adapter(job.source)
        if adapter is None:
            raise UnsupportedPlatformError(
                "Official API submission adapter is not authorized or implemented for this source"
            )
        exec_ctx = ExecutionContext(
            application=application,
            job=job,
            resume=resume,
            verified_resume_path=verified_snapshot["pdf_path"],
            verified_answers=verified_snapshot["verified_answers"],
            answers_hash=verified_snapshot["answers_hash"],
            approval_record=approval_record,
            options=options,
        )
        result = adapter.execute(exec_ctx)
        return {"execution_result": result.model_dump(mode="json")}

    def manual_handoff_node(_state: ApplicationExecutionState) -> dict[str, Any]:
        result = ExecutionResult(
            application_id=application.id,
            status="MANUAL_ACTION_REQUIRED",
            adapter_name="manual_handoff",
            mode="manual",
            stopped_before_submit=True,
        )
        return {"execution_result": result.model_dump(mode="json")}

    def browser_prefill_node(_state: ApplicationExecutionState) -> dict[str, Any]:
        exec_ctx = ExecutionContext(
            application=application,
            job=job,
            resume=resume,
            verified_resume_path=verified_snapshot["pdf_path"],
            verified_answers=verified_snapshot["verified_answers"],
            answers_hash=verified_snapshot["answers_hash"],
            approval_record=approval_record,
            options=options,
        )
        result = browser_executor.execute_with_validated_context(
            ctx=exec_ctx,
            user_id=user.id,
            profile=profile,
            mapping=mapping,
            answers=answers,
        )
        return {"execution_result": result.model_dump(mode="json")}

    builder = StateGraph(ApplicationExecutionState)
    builder.add_node("validate_policy_and_approval", validate_policy_and_approval)
    builder.add_node("official_api_node", official_api_node)
    builder.add_node("manual_handoff_node", manual_handoff_node)
    builder.add_node("browser_prefill_node", browser_prefill_node)

    builder.add_edge(START, "validate_policy_and_approval")
    builder.add_conditional_edges(
        "validate_policy_and_approval",
        route_execution_mode,
        {
            "official_api_node": "official_api_node",
            "manual_handoff_node": "manual_handoff_node",
            "browser_prefill_node": "browser_prefill_node",
        },
    )
    builder.add_edge("official_api_node", END)
    builder.add_edge("manual_handoff_node", END)
    builder.add_edge("browser_prefill_node", END)
    return builder.compile(checkpointer=checkpointer)


def run_application_execution_graph(
    db: Session,
    *,
    user: User,
    profile: CandidateProfile,
    application: Application,
    job: Job,
    resume: Resume,
    mapping: ResumeJobMapping | None,
    answers: list[ApplicationAnswer],
    approval_record: ApprovalRecord,
    options: ExecutionOptions,
    browser_executor: PlaywrightBrowserExecutor,
    settings: Settings | None = None,
    checkpointer: Any | None = None,
) -> ExecutionResult:
    """Invoke the checkpointed application execution workflow and return validated ExecutionResult."""
    cfg = settings or get_settings()
    config = {"configurable": {"thread_id": application.thread_id, "checkpoint_ns": "application_exec"}}
    initial_state: ApplicationExecutionState = {
        "application_id": str(application.id),
        "user_id": str(user.id),
        "thread_id": application.thread_id,
        "mode": application.application_mode,
    }
    if checkpointer is not None:
        graph = build_application_execution_graph(
            db,
            user=user,
            profile=profile,
            application=application,
            job=job,
            resume=resume,
            mapping=mapping,
            answers=answers,
            approval_record=approval_record,
            options=options,
            browser_executor=browser_executor,
            settings=cfg,
            checkpointer=checkpointer,
        )
        out = graph.invoke(initial_state, config=config)
    else:
        db_url = cfg.database_url
        if db.bind is not None and db.bind.dialect.name == "postgresql":
            db_url = db.bind.url.render_as_string(hide_password=False)
            ensure_postgres_checkpointer_setup(db_url)
            dsn = _to_psycopg_dsn(db_url)
            with PostgresSaver.from_conn_string(dsn) as sync_saver:
                graph = build_application_execution_graph(
                    db,
                    user=user,
                    profile=profile,
                    application=application,
                    job=job,
                    resume=resume,
                    mapping=mapping,
                    answers=answers,
                    approval_record=approval_record,
                    options=options,
                    browser_executor=browser_executor,
                    settings=cfg,
                    checkpointer=sync_saver,
                )
                out = graph.invoke(initial_state, config=config)
        else:
            graph = build_application_execution_graph(
                db,
                user=user,
                profile=profile,
                application=application,
                job=job,
                resume=resume,
                mapping=mapping,
                answers=answers,
                approval_record=approval_record,
                options=options,
                browser_executor=browser_executor,
                settings=cfg,
                checkpointer=MemorySaver(),
            )
            out = graph.invoke(initial_state, config=config)

    return ExecutionResult.model_validate(out["execution_result"])
