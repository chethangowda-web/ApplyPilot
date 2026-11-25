from pathlib import Path
import uuid
from typing import Any, TypedDict
from langgraph.graph import END, START, StateGraph
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.agents.resume_intelligence.ats_optimizer import (
    ATSOptimizationAgent,
    ATSOptimizationReport,
)
from app.agents.resume_intelligence.evidence_retriever import (
    EvidenceRetriever,
    RetrievedCandidateEvidence,
)
from app.agents.resume_intelligence.factual_validator import FactualValidatorAgent
from app.agents.resume_intelligence.resume_analyzer import (
    MasterResumeAnalysis,
    ResumeAnalyzerAgent,
)
from app.agents.resume_intelligence.resume_evaluator import ResumeEvaluatorAgent
from app.agents.resume_intelligence.resume_tailor import (
    ResumeTailoringAgent,
    TailoringResult,
)
from app.core.config import get_settings
from app.core.logging import get_logger
from app.database.models import (
    CandidateProfile,
    Job,
    JobMatch,
    JobRequirement,
    Resume,
    ResumeGenerationJob,
    ResumeJobMapping,
    WorkflowRun,
    utc_now,
)
from app.graphs.checkpointer import managed_checkpointer
from app.llm.router import ModelRouter
from app.schemas.resume_intelligence import (
    FactualValidationReport,
    TailoredResumeContent,
    TailoringDiff,
)
from app.services.audit_service import record_audit_event
from app.services.fingerprint_service import TEMPLATE_VERSION
from app.services.pdf_resume_service import render_tailored_resume_pdf
from app.services.resume_vault_service import (
    artifact_persistence_lock,
    compute_sha256,
    store_resume_bytes,
)

logger = get_logger("applypilot.resume_intelligence_graph")


class ResumeIntelligenceGraphState(TypedDict, total=False):
    run_id: str
    generation_job_id: str | None
    user_id: str
    profile_id: str
    job_id: str
    master_resume_id: str | None
    trigger_mode: str
    use_llm: bool
    force_regenerate: bool
    max_revisions: int
    template_version: str
    storage_dir: str | None

    evidence_fingerprint: str
    idempotency_hit: bool
    existing_mapping_id: str | None
    existing_resume_id: str | None

    master_analysis: dict[str, Any]
    evidence_snapshot: dict[str, Any]

    revision_attempt: int
    revision_feedback: list[str]
    tailored_content: dict[str, Any]
    tailoring_diff: dict[str, Any]
    tailoring_mode: str
    model_provider: str | None
    token_usage: dict[str, Any]

    ats_report: dict[str, Any]
    validation_report: dict[str, Any]
    validation_passed: bool
    evaluation_report: dict[str, Any]

    pdf_generated: bool
    resume_id: str | None
    mapping_id: str | None
    version_number: int | None
    status: str  # completed | validation_failed | failed
    approval_status: str  # always pending_review
    error_message: str | None
    errors: list[dict[str, Any]]


def _accumulate_usage(prev: dict[str, Any] | None, new_usage: dict[str, Any] | None) -> dict[str, Any]:
    if not new_usage:
        return dict(prev or {})
    if not prev:
        return dict(new_usage)
    return {
        "provider": new_usage.get("provider") or prev.get("provider"),
        "model": new_usage.get("model") or prev.get("model"),
        "prompt_tokens": int(prev.get("prompt_tokens") or 0) + int(new_usage.get("prompt_tokens") or 0),
        "completion_tokens": int(prev.get("completion_tokens") or 0) + int(new_usage.get("completion_tokens") or 0),
        "total_tokens": int(prev.get("total_tokens") or 0) + int(new_usage.get("total_tokens") or 0),
        "estimated_cost_usd": round(
            float(prev.get("estimated_cost_usd") or 0.0) + float(new_usage.get("estimated_cost_usd") or 0.0),
            8,
        ),
    }


def _next_version_number(db: Session, job_id: uuid.UUID, profile_id: uuid.UUID) -> int:
    current_max = db.scalar(
        select(func.max(ResumeJobMapping.version_number)).where(
            ResumeJobMapping.job_id == job_id,
            ResumeJobMapping.profile_id == profile_id,
        )
    )
    return int(current_max or 0) + 1


def build_resume_intelligence_graph(
    db: Session,
    *,
    router: ModelRouter | None = None,
    checkpointer: Any,
    tailor_agent: ResumeTailoringAgent | None = None,
    validator_agent: FactualValidatorAgent | None = None,
    storage_dir: Path | None = None,
):
    """Build and compile the LangGraph Resume Intelligence subgraph."""
    shared_router = router or ModelRouter()
    analyzer = ResumeAnalyzerAgent()
    retriever = EvidenceRetriever()
    tailorer = tailor_agent or ResumeTailoringAgent(router=shared_router)
    ats_optimizer = ATSOptimizationAgent()
    validator = validator_agent or FactualValidatorAgent(router=shared_router)
    evaluator = ResumeEvaluatorAgent()

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
                workflow_type="resume_intelligence",
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

    def _load_profile_and_job(state: ResumeIntelligenceGraphState) -> tuple[CandidateProfile, Job, JobRequirement | None, JobMatch | None, Resume | None]:
        uid = uuid.UUID(state["user_id"])
        jid = uuid.UUID(state["job_id"])
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
        req = db.scalar(select(JobRequirement).where(JobRequirement.job_id == jid))
        match = db.scalar(
            select(JobMatch).where(JobMatch.job_id == jid, JobMatch.profile_id == profile.id)
        )
        req_master_id = uuid.UUID(state["master_resume_id"]) if state.get("master_resume_id") else None
        master = analyzer.select_active_master_resume(db, profile.id, master_resume_id=req_master_id)
        return profile, job, req, match, master

    def analyze_master_resume(state: ResumeIntelligenceGraphState) -> dict[str, Any]:
        uid = uuid.UUID(state["user_id"])
        jid = uuid.UUID(state["job_id"])
        profile, _job, _req, _match, master = _load_profile_and_job(state)
        resolved_storage = (
            Path(state["storage_dir"])
            if state.get("storage_dir")
            else (storage_dir if storage_dir is not None else Path(get_settings().storage_dir))
        )
        analysis = analyzer.analyze(
            db,
            profile=profile,
            master_resume=master,
            storage_dir=resolved_storage,
        )
        _update_workflow_run(
            state["run_id"],
            user_id=uid,
            job_id=jid,
            stage="analyze_master_resume",
            status="running",
            metadata={"has_master_file": analysis.has_master_file},
        )
        return {
            "profile_id": str(profile.id),
            "master_resume_id": str(master.id) if master else None,
            "master_analysis": analysis.model_dump(mode="json"),
            "revision_attempt": int(state.get("revision_attempt") or 0),
            "errors": list(state.get("errors") or []),
            "pdf_generated": False,
            "approval_status": "pending_review",
        }

    def retrieve_evidence(state: ResumeIntelligenceGraphState) -> dict[str, Any]:
        uid = uuid.UUID(state["user_id"])
        jid = uuid.UUID(state["job_id"])
        profile, job, req, match, master = _load_profile_and_job(state)
        tpl = state.get("template_version") or TEMPLATE_VERSION

        ev = retriever.retrieve(
            db,
            profile=profile,
            job=job,
            req=req,
            match=match,
            master_resume=master,
            template_version=tpl,
        )

        # Crash-after-mapping-commit recovery: if generation_job_id already persisted a mapping, reuse it
        raw_gen_id = state.get("generation_job_id")
        if raw_gen_id:
            gen_uuid = uuid.UUID(raw_gen_id)
            by_gen = db.scalar(
                select(ResumeJobMapping).where(ResumeJobMapping.generation_job_id == gen_uuid)
            )
            if by_gen is not None:
                res_row = db.get(Resume, by_gen.resume_id) if by_gen.resume_id else None
                gen_row = db.get(ResumeGenerationJob, gen_uuid)
                if gen_row is not None and gen_row.status not in ("completed", "validation_failed"):
                    gen_row.status = by_gen.status if by_gen.status in ("completed", "validation_failed") else "completed"
                    gen_row.resume_id = by_gen.resume_id
                    gen_row.completed_at = gen_row.completed_at or utc_now()
                    gen_row.worker_id = None
                    gen_row.lease_expires_at = None
                _update_workflow_run(
                    state["run_id"],
                    user_id=uid,
                    job_id=jid,
                    stage="completed_idempotent",
                    status=by_gen.status,
                    metadata={
                        "idempotency_hit": True,
                        "mapping_id": str(by_gen.id),
                        "resume_id": str(by_gen.resume_id) if by_gen.resume_id else None,
                    },
                )
                db.commit()
                return {
                    "evidence_fingerprint": by_gen.evidence_fingerprint,
                    "evidence_snapshot": ev.model_dump(mode="json"),
                    "idempotency_hit": True,
                    "existing_mapping_id": str(by_gen.id),
                    "existing_resume_id": str(by_gen.resume_id) if by_gen.resume_id else None,
                    "mapping_id": str(by_gen.id),
                    "resume_id": str(by_gen.resume_id) if by_gen.resume_id else None,
                    "version_number": by_gen.version_number,
                    "status": by_gen.status,
                    "approval_status": by_gen.approval_status,
                    "validation_passed": bool(res_row.validation_passed) if res_row else (by_gen.status == "completed"),
                    "pdf_generated": False,
                }

        # Logical idempotency reuse for same fingerprint when force_regenerate is False
        if not bool(state.get("force_regenerate", False)):
            existing = db.scalar(
                select(ResumeJobMapping).where(
                    ResumeJobMapping.job_id == job.id,
                    ResumeJobMapping.profile_id == profile.id,
                    ResumeJobMapping.evidence_fingerprint == ev.evidence_fingerprint,
                    ResumeJobMapping.status == "completed",
                    ResumeJobMapping.is_active.is_(True),
                )
            )
            if existing is not None and existing.resume_id is not None:
                res_row = db.get(Resume, existing.resume_id)
                if raw_gen_id:
                    gen_row = db.get(ResumeGenerationJob, uuid.UUID(raw_gen_id))
                    if gen_row is not None:
                        gen_row.status = "completed"
                        gen_row.resume_id = existing.resume_id
                        gen_row.completed_at = gen_row.completed_at or utc_now()
                        gen_row.worker_id = None
                        gen_row.lease_expires_at = None
                _update_workflow_run(
                    state["run_id"],
                    user_id=uid,
                    job_id=jid,
                    stage="completed_idempotent",
                    status="completed",
                    metadata={
                        "idempotency_hit": True,
                        "mapping_id": str(existing.id),
                        "resume_id": str(existing.resume_id),
                    },
                )
                db.commit()
                return {
                    "evidence_fingerprint": ev.evidence_fingerprint,
                    "evidence_snapshot": ev.model_dump(mode="json"),
                    "idempotency_hit": True,
                    "existing_mapping_id": str(existing.id),
                    "existing_resume_id": str(existing.resume_id),
                    "mapping_id": str(existing.id),
                    "resume_id": str(existing.resume_id),
                    "version_number": existing.version_number,
                    "status": "completed",
                    "approval_status": existing.approval_status,
                    "validation_passed": bool(res_row.validation_passed) if res_row else True,
                    "pdf_generated": False,
                }

        _update_workflow_run(
            state["run_id"],
            user_id=uid,
            job_id=jid,
            stage="retrieve_evidence",
            status="running",
            metadata={"evidence_fingerprint": ev.evidence_fingerprint},
        )
        return {
            "idempotency_hit": False,
            "evidence_fingerprint": ev.evidence_fingerprint,
            "evidence_snapshot": ev.model_dump(mode="json"),
        }

    def _route_after_evidence(state: ResumeIntelligenceGraphState) -> str:
        if state.get("idempotency_hit"):
            return END
        return "tailor_resume"

    async def tailor_resume(state: ResumeIntelligenceGraphState) -> dict[str, Any]:
        uid = uuid.UUID(state["user_id"])
        jid = uuid.UUID(state["job_id"])
        _profile, job, req, _match, _master = _load_profile_and_job(state)
        ev = RetrievedCandidateEvidence.model_validate(state["evidence_snapshot"])
        master_analysis = MasterResumeAnalysis.model_validate(state["master_analysis"])

        result: TailoringResult = await tailorer.tailor(
            evidence=ev,
            job=job,
            req=req,
            master_analysis=master_analysis,
            use_llm=bool(state.get("use_llm", True)),
            revision_feedback=list(state.get("revision_feedback") or []) or None,
        )
        merged_usage = _accumulate_usage(state.get("token_usage"), result.token_usage)
        attempt = int(state.get("revision_attempt") or 0)

        _update_workflow_run(
            state["run_id"],
            user_id=uid,
            job_id=jid,
            stage=f"tailor_resume_attempt_{attempt}",
            status="running",
            metadata={"tailoring_mode": result.mode, "attempt": attempt},
        )
        return {
            "tailored_content": result.content.model_dump(mode="json"),
            "tailoring_diff": result.diff.model_dump(mode="json"),
            "tailoring_mode": result.mode,
            "model_provider": result.model_provider,
            "token_usage": merged_usage,
        }

    async def ats_and_validate(state: ResumeIntelligenceGraphState) -> dict[str, Any]:
        uid = uuid.UUID(state["user_id"])
        jid = uuid.UUID(state["job_id"])
        _profile, _job, req, _match, _master = _load_profile_and_job(state)
        ev = RetrievedCandidateEvidence.model_validate(state["evidence_snapshot"])
        content = TailoredResumeContent.model_validate(state["tailored_content"])

        ats_pre = ats_optimizer.analyze(content=content, req=req, pdf_bytes=None)
        val_report = await validator.validate(
            content=content,
            evidence=ev,
            use_llm_crosscheck=bool(state.get("use_llm", True)),
            skip_llm_on_deterministic_fail=True,
        )
        _update_workflow_run(
            state["run_id"],
            user_id=uid,
            job_id=jid,
            stage="ats_and_validate",
            status="running",
            metadata={
                "validation_passed": val_report.validation_passed,
                "unsupported_count": val_report.unsupported_count,
            },
        )
        return {
            "ats_report": ats_pre.model_dump(mode="json"),
            "validation_report": val_report.model_dump(mode="json"),
            "validation_passed": bool(val_report.validation_passed),
        }

    def _route_after_validation(state: ResumeIntelligenceGraphState) -> str:
        if state.get("validation_passed"):
            return "evaluate_and_render_pdf"
        attempt = int(state.get("revision_attempt") or 0)
        max_rev = max(0, min(2, int(state.get("max_revisions", 1))))
        if attempt < max_rev:
            return "prepare_revision"
        return "persist_validation_failure"

    def prepare_revision(state: ResumeIntelligenceGraphState) -> dict[str, Any]:
        val_raw = state.get("validation_report") or {}
        unsupported = val_raw.get("unsupported_claims") or []
        feedback = [
            f"{c.get('category', 'claim')} failed ({c.get('violation_reason', 'unverified')}): {c.get('claim_text', '')}"
            for c in unsupported[:8]
        ]
        next_attempt = int(state.get("revision_attempt") or 0) + 1
        return {
            "revision_attempt": next_attempt,
            "revision_feedback": feedback,
        }

    def evaluate_and_render_pdf(state: ResumeIntelligenceGraphState) -> dict[str, Any]:
        # Hard guard: PDF generation is strictly forbidden unless validation passed
        if not state.get("validation_passed"):
            raise RuntimeError("Cannot generate tailored PDF when factual validation has not passed.")

        uid = uuid.UUID(state["user_id"])
        pid = uuid.UUID(state["profile_id"])
        jid = uuid.UUID(state["job_id"])
        _profile, job, req, _match, master = _load_profile_and_job(state)

        ev = RetrievedCandidateEvidence.model_validate(state["evidence_snapshot"])
        content = TailoredResumeContent.model_validate(state["tailored_content"])
        diff = TailoringDiff.model_validate(state["tailoring_diff"])
        val_report = FactualValidationReport.model_validate(state["validation_report"])
        tpl = state.get("template_version") or TEMPLATE_VERSION

        # 1. Render PDF in memory first (no DB mutation yet)
        pdf_res = render_tailored_resume_pdf(content, template_version=tpl)
        if not pdf_res.quality_passed:
            raise ValueError("Rendered PDF failed post-render ATS quality inspection.")

        # 2. Post-PDF ATS analysis + 7-metric evaluation
        final_ats: ATSOptimizationReport = ats_optimizer.analyze(
            content=content,
            req=req,
            pdf_bytes=pdf_res.pdf_bytes,
        )
        merged_ats = {**pdf_res.ats_report, **final_ats.model_dump(mode="json")}
        eval_report = evaluator.evaluate(
            content=content,
            evidence=ev,
            req=req,
            ats_report=final_ats,
            validation_report=val_report,
        )

        # 3. Persist file + DB records atomically so prior active mapping survives any failure
        resolved_storage = (
            Path(state["storage_dir"])
            if state.get("storage_dir")
            else (storage_dir if storage_dir is not None else Path(get_settings().storage_dir))
        )
        next_ver = _next_version_number(db, jid, pid)
        new_resume_id = uuid.uuid4()
        safe_company = "".join(ch if ch.isalnum() else "_" for ch in job.company.lower()).strip("_") or "company"
        pdf_filename = f"tailored_{safe_company}_v{next_ver}.pdf"
        with artifact_persistence_lock(db, new_resume_id, release_xact_on_exit=False):
            stored_path = store_resume_bytes(
                storage_root=resolved_storage,
                user_id=uid,
                resume_id=new_resume_id,
                safe_filename=pdf_filename,
                is_master=False,
                data=pdf_res.pdf_bytes,
            )
            try:
                new_resume = Resume(
                    id=new_resume_id,
                    profile_id=pid,
                    master_resume_id=master.id if master else None,
                    is_master=False,
                    original_filename=pdf_filename,
                    content_type="application/pdf",
                    file_size_bytes=pdf_res.file_size_bytes,
                    version_label=f"tailored-v{next_ver}",
                    docx_path=None,
                    pdf_path=str(stored_path),
                    sha256_checksum=pdf_res.sha256_checksum,
                    template_version=tpl,
                    generation_status="completed",
                    ats_report=merged_ats,
                    parsed_metadata=pdf_res.parsed_metadata,
                    validation_report=val_report.model_dump(mode="json"),
                    evaluation_report=eval_report.model_dump(mode="json"),
                    structured_content=content.model_dump(mode="json"),
                    validation_passed=True,
                )
                db.add(new_resume)
                db.flush()

                # Deactivate prior active completed mapping ONLY after replacement Resume persisted
                prior_active = db.scalars(
                    select(ResumeJobMapping).where(
                        ResumeJobMapping.job_id == jid,
                        ResumeJobMapping.profile_id == pid,
                        ResumeJobMapping.is_active.is_(True),
                        ResumeJobMapping.status == "completed",
                    )
                ).all()
                for prev in prior_active:
                    prev.is_active = False
                db.flush()

                gen_job_uuid = uuid.UUID(state["generation_job_id"]) if state.get("generation_job_id") else None
                new_mapping = ResumeJobMapping(
                    resume_id=new_resume.id,
                    job_id=jid,
                    profile_id=pid,
                    master_resume_id=master.id if master else None,
                    generation_job_id=gen_job_uuid,
                    run_id=state["run_id"],
                    trigger_mode=state.get("trigger_mode") or "manual",
                    version_number=next_ver,
                    is_active=True,
                    evidence_fingerprint=ev.evidence_fingerprint,
                    status="completed",
                    approval_status="pending_review",  # NEVER auto-approve
                    tailoring_diff=diff.model_dump(mode="json"),
                    model_provider=state.get("model_provider"),
                    token_usage=dict(state.get("token_usage") or {}),
                )
                db.add(new_mapping)
                db.flush()

                if gen_job_uuid is not None:
                    gen_row = db.get(ResumeGenerationJob, gen_job_uuid)
                    if gen_row is not None:
                        gen_row.status = "completed"
                        gen_row.resume_id = new_resume.id
                        gen_row.completed_at = utc_now()
                        gen_row.worker_id = None
                        gen_row.lease_expires_at = None
                        gen_row.error_message = None

                eval_dict = eval_report.model_dump(mode="json")
                _update_workflow_run(
                    state["run_id"],
                    user_id=uid,
                    job_id=jid,
                    stage="completed",
                    status="completed",
                    metadata={
                        "resume_id": str(new_resume.id),
                        "mapping_id": str(new_mapping.id),
                        "version_number": next_ver,
                        "approval_status": "pending_review",
                        "validation_passed": True,
                    },
                )
                record_audit_event(
                    db,
                    user_id=uid,
                    actor="resume_intelligence_subgraph",
                    action="tailored_resume_generated",
                    resource_type="resume_job_mapping",
                    resource_id=str(new_mapping.id),
                    metadata={
                        "job_id": str(jid),
                        "resume_id": str(new_resume.id),
                        "version_number": next_ver,
                        "trigger_mode": state.get("trigger_mode") or "manual",
                        "approval_status": "pending_review",
                        "validation_passed": True,
                        "model_provider": state.get("model_provider"),
                    },
                )
                db.commit()
            except Exception:
                db.rollback()
                stored_path.unlink(missing_ok=True)
                raise

        return {
            "ats_report": merged_ats,
            "evaluation_report": eval_dict,
            "pdf_generated": True,
            "resume_id": str(new_resume.id),
            "mapping_id": str(new_mapping.id),
            "version_number": next_ver,
            "status": "completed",
            "approval_status": "pending_review",
        }

    def persist_validation_failure(state: ResumeIntelligenceGraphState) -> dict[str, Any]:
        """Persist failed validation metadata WITHOUT generating any PDF or altering previous active resume."""
        uid = uuid.UUID(state["user_id"])
        pid = uuid.UUID(state["profile_id"])
        jid = uuid.UUID(state["job_id"])
        _profile, job, req, _match, master = _load_profile_and_job(state)

        ev = RetrievedCandidateEvidence.model_validate(state["evidence_snapshot"])
        content = TailoredResumeContent.model_validate(state["tailored_content"])
        diff = TailoringDiff.model_validate(state["tailoring_diff"])
        val_report = FactualValidationReport.model_validate(state["validation_report"])
        ats_pre = ATSOptimizationReport.model_validate(state["ats_report"])
        eval_report = evaluator.evaluate(
            content=content,
            evidence=ev,
            req=req,
            ats_report=ats_pre,
            validation_report=val_report,
        )

        next_ver = _next_version_number(db, jid, pid)
        err_summary = (
            f"Factual validation failed with {val_report.unsupported_count} unsupported claim(s)."
        )
        failed_resume = Resume(
            profile_id=pid,
            master_resume_id=master.id if master else None,
            is_master=False,
            original_filename=f"validation_failed_v{next_ver}.pdf",
            content_type="application/pdf",
            file_size_bytes=0,
            version_label=f"tailored-v{next_ver}-failed",
            docx_path=None,
            pdf_path=None,  # Strictly NO PDF generated on validation failure
            sha256_checksum=compute_sha256(val_report.model_dump_json().encode("utf-8")),
            template_version=state.get("template_version") or TEMPLATE_VERSION,
            generation_status="validation_failed",
            ats_report=ats_pre.model_dump(mode="json"),
            parsed_metadata={},
            validation_report=val_report.model_dump(mode="json"),
            evaluation_report=eval_report.model_dump(mode="json"),
            structured_content=content.model_dump(mode="json"),
            validation_passed=False,
        )
        db.add(failed_resume)
        db.flush()

        gen_job_uuid = uuid.UUID(state["generation_job_id"]) if state.get("generation_job_id") else None
        # Note: prior active completed mapping is NOT touched (remains is_active=True)
        failed_mapping = ResumeJobMapping(
            resume_id=failed_resume.id,
            job_id=jid,
            profile_id=pid,
            master_resume_id=master.id if master else None,
            generation_job_id=gen_job_uuid,
            run_id=state["run_id"],
            trigger_mode=state.get("trigger_mode") or "manual",
            version_number=next_ver,
            is_active=False,
            evidence_fingerprint=ev.evidence_fingerprint,
            status="validation_failed",
            approval_status="pending_review",
            tailoring_diff=diff.model_dump(mode="json"),
            model_provider=state.get("model_provider"),
            token_usage=dict(state.get("token_usage") or {}),
            error_message=err_summary,
        )
        db.add(failed_mapping)
        db.flush()

        if gen_job_uuid is not None:
            gen_row = db.get(ResumeGenerationJob, gen_job_uuid)
            if gen_row is not None:
                gen_row.status = "validation_failed"
                gen_row.resume_id = failed_resume.id
                gen_row.completed_at = utc_now()
                gen_row.worker_id = None
                gen_row.lease_expires_at = None
                gen_row.error_message = err_summary

        _update_workflow_run(
            state["run_id"],
            user_id=uid,
            job_id=jid,
            stage="validation_failed",
            status="validation_failed",
            metadata={
                "resume_id": str(failed_resume.id),
                "mapping_id": str(failed_mapping.id),
                "version_number": next_ver,
                "unsupported_count": val_report.unsupported_count,
                "pdf_generated": False,
            },
        )
        record_audit_event(
            db,
            user_id=uid,
            actor="resume_intelligence_subgraph",
            action="tailored_resume_validation_failed",
            resource_type="resume_job_mapping",
            resource_id=str(failed_mapping.id),
            metadata={
                "job_id": str(jid),
                "resume_id": str(failed_resume.id),
                "version_number": next_ver,
                "unsupported_count": val_report.unsupported_count,
            },
        )
        db.commit()

        return {
            "evaluation_report": eval_report.model_dump(mode="json"),
            "pdf_generated": False,
            "resume_id": str(failed_resume.id),
            "mapping_id": str(failed_mapping.id),
            "version_number": next_ver,
            "status": "validation_failed",
            "approval_status": "pending_review",
            "error_message": err_summary,
        }

    graph = StateGraph(ResumeIntelligenceGraphState)
    graph.add_node("analyze_master_resume", analyze_master_resume)
    graph.add_node("retrieve_evidence", retrieve_evidence)
    graph.add_node("tailor_resume", tailor_resume)
    graph.add_node("ats_and_validate", ats_and_validate)
    graph.add_node("prepare_revision", prepare_revision)
    graph.add_node("evaluate_and_render_pdf", evaluate_and_render_pdf)
    graph.add_node("persist_validation_failure", persist_validation_failure)

    graph.add_edge(START, "analyze_master_resume")
    graph.add_edge("analyze_master_resume", "retrieve_evidence")
    graph.add_conditional_edges(
        "retrieve_evidence",
        _route_after_evidence,
        {END: END, "tailor_resume": "tailor_resume"},
    )
    graph.add_edge("tailor_resume", "ats_and_validate")
    graph.add_conditional_edges(
        "ats_and_validate",
        _route_after_validation,
        {
            "evaluate_and_render_pdf": "evaluate_and_render_pdf",
            "prepare_revision": "prepare_revision",
            "persist_validation_failure": "persist_validation_failure",
        },
    )
    graph.add_edge("prepare_revision", "tailor_resume")
    graph.add_edge("evaluate_and_render_pdf", END)
    graph.add_edge("persist_validation_failure", END)

    return graph.compile(checkpointer=checkpointer)


async def run_resume_intelligence_workflow(
    db: Session,
    *,
    user_id: uuid.UUID,
    job_id: uuid.UUID,
    master_resume_id: uuid.UUID | None = None,
    generation_job_id: uuid.UUID | None = None,
    trigger_mode: str = "manual",
    run_id: str | None = None,
    use_llm: bool = True,
    force_regenerate: bool = False,
    max_revisions: int = 1,
    template_version: str = TEMPLATE_VERSION,
    storage_dir: Path | None = None,
    router: ModelRouter | None = None,
    checkpointer: Any | None = None,
    tailor_agent: ResumeTailoringAgent | None = None,
    validator_agent: FactualValidatorAgent | None = None,
) -> dict[str, Any]:
    """Execute the LangGraph Resume Intelligence subgraph with persistent checkpointing."""
    rid = run_id or f"res-{uuid.uuid4().hex[:12]}"
    try:
        async with managed_checkpointer(db, override_checkpointer=checkpointer) as cp:
            compiled = build_resume_intelligence_graph(
                db,
                router=router,
                checkpointer=cp,
                tailor_agent=tailor_agent,
                validator_agent=validator_agent,
                storage_dir=storage_dir,
            )
            initial_state: ResumeIntelligenceGraphState = {
                "run_id": rid,
                "generation_job_id": str(generation_job_id) if generation_job_id else None,
                "user_id": str(user_id),
                "job_id": str(job_id),
                "master_resume_id": str(master_resume_id) if master_resume_id else None,
                "trigger_mode": trigger_mode,
                "use_llm": use_llm,
                "force_regenerate": force_regenerate,
                "max_revisions": max_revisions,
                "template_version": template_version,
                "storage_dir": str(storage_dir) if storage_dir else None,
            }
            final_state = await compiled.ainvoke(
                initial_state,
                config={"configurable": {"thread_id": rid}},
            )
            return dict(final_state)
    except Exception as exc:
        db.rollback()
        logger.warning(
            "resume_intelligence_workflow_failed",
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
