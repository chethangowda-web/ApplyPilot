from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
import re
import threading
import uuid
from typing import Any, Iterator

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.application.form_understanding import classify_sensitive_question
from app.core.config import Settings, get_settings
from app.core.logging import redact_value
from app.core.security import decrypt_sensitive_answer, encrypt_sensitive_answer
from app.database.models import (
    Application,
    ApplicationAnswer,
    ApprovalRecord,
    AuditLog,
    CandidateProfile,
    Job,
    Resume,
    ResumeJobMapping,
    User,
    utc_now,
)
from app.graphs.application_graph import run_application_execution_graph
from app.schemas.applications import (
    ApplicationAnswerInput,
    ApplicationAnswerResponse,
    ApplicationDetailResponse,
    ApplicationMode,
    ApplicationPreparationResult,
    ApplicationResponse,
    ApprovalRecordResponse,
    ExecutionOptions,
    ExecutionResult,
    HandoffSessionInfo,
    ManualSubmitAuthorizationRequest,
    ManualSubmitAuthorizationResponse,
    compute_answers_hash,
    compute_approval_snapshot_hash,
)
from app.services.application_policy_service import (
    ApplicationPolicyRouter,
    DuplicateApplicationError,
    PolicyViolationError,
    transition_application_status,
)
from app.services.audit_service import record_audit_event
from app.services.browser_executor import (
    BrowserHandoffManager,
    PlaywrightBrowserExecutor,
    get_handoff_manager,
    load_verified_resume_snapshot,
    resolve_owned_artifact_bytes,
    validate_approval_and_answers,
)
from app.services.execution_adapters import (
    ApprovalValidationError,
    SSRFBlockedError,
    SubmitBlockedViolationError,
    UnsupportedPlatformError,
    get_authorized_official_api_adapter,
)

_WINDOWS_PATH_RE = re.compile(r"[A-Za-z]:\\[^\s'\"]+")
_UNIX_PATH_RE = re.compile(r"/(?:var|tmp|home|Users|app|storage)[^\s'\"]+")

_APP_LOCKS_GUARD = threading.Lock()
_APP_LOCKS: dict[uuid.UUID, threading.Lock] = {}


@contextmanager
def _application_lock(application_id: uuid.UUID) -> Iterator[None]:
    with _APP_LOCKS_GUARD:
        lock = _APP_LOCKS.get(application_id)
        if lock is None:
            lock = threading.Lock()
            _APP_LOCKS[application_id] = lock
    with lock:
        yield


def sanitize_safe_error_message(raw_message: str) -> str:
    """Scrub PII, bearer tokens, and server filesystem paths from error messages."""
    redacted = str(redact_value(raw_message or "Execution failed"))
    redacted = _WINDOWS_PATH_RE.sub("[REDACTED_PATH]", redacted)
    redacted = _UNIX_PATH_RE.sub("[REDACTED_PATH]", redacted)
    return redacted[:500]


def _serialize_answer_safe(ans: ApplicationAnswer) -> ApplicationAnswerResponse:
    """Return answer metadata with sensitive answers permanently masked."""
    if ans.is_sensitive:
        preview = "[REDACTED — VERIFIED]" if ans.user_verified else "[REDACTED — UNVERIFIED]"
    else:
        text = (ans.answer_plaintext or "").strip()
        preview = text[:200] if text else None
    return ApplicationAnswerResponse(
        id=ans.id,
        question_key=ans.question_key,
        question=ans.question,
        is_sensitive=ans.is_sensitive,
        answer_preview=preview,
        evidence_source=ans.evidence_source,
        user_verified=ans.user_verified,
        created_at=ans.created_at,
    )


class ApplicationExecutionService:
    """Authenticated Checkpoint D service orchestrating preparation, approval, execution, and reconciliation."""

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        handoff_manager: BrowserHandoffManager | None = None,
        browser_executor: PlaywrightBrowserExecutor | None = None,
        _allowed_test_origins: frozenset[tuple[str, int]] = frozenset(),
    ) -> None:
        self.settings = settings or get_settings()
        storage_root = Path(self.settings.storage_dir)
        self.handoff_manager = handoff_manager or get_handoff_manager(storage_root)
        self.browser_executor = browser_executor or PlaywrightBrowserExecutor(
            settings=self.settings,
            handoff_manager=self.handoff_manager,
            _allowed_test_origins=_allowed_test_origins,
        )
        self.policy_router = ApplicationPolicyRouter(settings=self.settings)

    def _get_owned_application(
        self,
        db: Session,
        *,
        user: User,
        application_id: uuid.UUID,
        lock_row: bool = False,
    ) -> Application:
        stmt = select(Application).where(
            Application.id == application_id,
            Application.user_id == user.id,
            Application.deleted_at.is_(None),
        )
        if lock_row and db.bind is not None and db.bind.dialect.name == "postgresql":
            stmt = stmt.with_for_update()
        app = db.scalar(stmt)
        if app is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Application not found")
        return app

    def _get_owned_execution_entities(
        self,
        db: Session,
        *,
        user: User,
        app: Application,
    ) -> tuple[CandidateProfile, Job, Resume, ResumeJobMapping | None, list[ApplicationAnswer], ApprovalRecord | None]:
        profile = db.scalar(select(CandidateProfile).where(CandidateProfile.user_id == user.id))
        if profile is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Candidate profile not found")
        job = db.get(Job, app.job_id)
        resume = db.get(Resume, app.resume_id)
        if job is None or resume is None or resume.profile_id != profile.id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Application job or resume not found")
        mapping = db.scalar(
            select(ResumeJobMapping)
            .where(
                ResumeJobMapping.resume_id == resume.id,
                ResumeJobMapping.job_id == job.id,
                ResumeJobMapping.profile_id == profile.id,
            )
            .order_by(ResumeJobMapping.created_at.desc())
        )
        answers = list(
            db.scalars(
                select(ApplicationAnswer)
                .where(ApplicationAnswer.application_id == app.id)
                .order_by(ApplicationAnswer.question_key.asc())
            )
        )
        approval = db.scalar(
            select(ApprovalRecord)
            .where(ApprovalRecord.application_id == app.id)
            .order_by(ApprovalRecord.decided_at.desc())
        )
        return profile, job, resume, mapping, answers, approval

    def _compute_live_answers_hash(
        self,
        *,
        app: Application,
        answers: list[ApplicationAnswer],
    ) -> tuple[str, list[str], list[str]]:
        pairs: list[tuple[str, str]] = []
        unverified: list[str] = []
        sensitive_unconfirmed: list[str] = []
        for ans in answers:
            if ans.is_sensitive:
                if not ans.answer_encrypted or not ans.key_version:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail=f"Sensitive answer {ans.question_key!r} is missing encrypted payload",
                    )
                plaintext = decrypt_sensitive_answer(
                    ans.answer_encrypted,
                    key_version=ans.key_version,
                    application_id=str(app.id),
                    question_key=ans.question_key,
                    settings=self.settings,
                )
                pairs.append((ans.question_key, plaintext))
                if not ans.user_verified:
                    sensitive_unconfirmed.append(ans.question_key)
                    unverified.append(ans.question_key)
            else:
                plain = (ans.answer_plaintext or "").strip()
                pairs.append((ans.question_key, plain))
                if not ans.user_verified or not plain:
                    unverified.append(ans.question_key)
        return compute_answers_hash(pairs), unverified, sensitive_unconfirmed

    def prepare_application(
        self,
        db: Session,
        *,
        user: User,
        job_id: uuid.UUID,
        resume_id: uuid.UUID,
        preferred_mode: ApplicationMode | None = None,
    ) -> ApplicationPreparationResult:
        """Prepare an owner-scoped application via Milestone 5 Stage 2 policy & form agent."""
        profile = db.scalar(select(CandidateProfile).where(CandidateProfile.user_id == user.id))
        if profile is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Candidate profile not found")
        job = db.get(Job, job_id)
        resume = db.get(Resume, resume_id)
        if job is None or resume is None or resume.profile_id != profile.id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job or resume not found")

        try:
            result = self.policy_router.prepare_application(
                db,
                user=user,
                profile=profile,
                job=job,
                resume=resume,
                preferred_mode=preferred_mode,
            )
        except DuplicateApplicationError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        except PolicyViolationError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        db.commit()
        return result

    def list_applications(self, db: Session, *, user: User) -> list[ApplicationResponse]:
        apps = list(
            db.scalars(
                select(Application)
                .where(Application.user_id == user.id, Application.deleted_at.is_(None))
                .order_by(Application.created_at.desc())
            )
        )
        return [ApplicationResponse.model_validate(a) for a in apps]

    def get_application_detail(
        self,
        db: Session,
        *,
        user: User,
        application_id: uuid.UUID,
    ) -> ApplicationDetailResponse:
        app = self._get_owned_application(db, user=user, application_id=application_id)
        answers = list(
            db.scalars(
                select(ApplicationAnswer)
                .where(ApplicationAnswer.application_id == app.id)
                .order_by(ApplicationAnswer.question_key.asc())
            )
        )
        approvals = list(
            db.scalars(
                select(ApprovalRecord)
                .where(ApprovalRecord.application_id == app.id)
                .order_by(ApprovalRecord.decided_at.desc())
            )
        )
        audit_rows = list(
            db.scalars(
                select(AuditLog)
                .where(AuditLog.resource_type == "application", AuditLog.resource_id == str(app.id))
                .order_by(AuditLog.created_at.desc())
                .limit(20)
            )
        )
        history = [
            {
                "action": row.action,
                "actor": row.actor,
                "created_at": row.created_at.isoformat(),
                "metadata": redact_value(row.event_metadata or {}),
            }
            for row in audit_rows
        ]
        sessions = self.handoff_manager.list_user_sessions(user_id=user.id, application_id=app.id)
        return ApplicationDetailResponse(
            application=ApplicationResponse.model_validate(app),
            answers=[_serialize_answer_safe(a) for a in answers],
            approval_records=[ApprovalRecordResponse.model_validate(r) for r in approvals],
            active_handoff_sessions=sessions,
            execution_history=history,
        )

    def submit_user_answers(
        self,
        db: Session,
        *,
        user: User,
        application_id: uuid.UUID,
        items: list[ApplicationAnswerInput],
    ) -> ApplicationDetailResponse:
        """Persist user-verified answers (AES-256-GCM encrypting all sensitive items) without granting approval."""
        with _application_lock(application_id):
            app = self._get_owned_application(db, user=user, application_id=application_id, lock_row=True)
            if app.status not in {"NEEDS_USER_INPUT", "AWAITING_APPROVAL"}:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"Answers cannot be modified while status={app.status}",
                )

            existing = {
                row.question_key: row
                for row in db.scalars(
                    select(ApplicationAnswer).where(ApplicationAnswer.application_id == app.id)
                )
            }
            sensitive_count = 0
            for item in items:
                key = item.question_key.strip()
                existing_row = existing.get(key)
                classified_sens = classify_sensitive_question(item.question, key) is not None
                must_encrypt = bool(
                    item.is_sensitive
                    or classified_sens
                    or (existing_row is not None and existing_row.is_sensitive)
                )
                if must_encrypt:
                    sensitive_count += 1
                    ct, ver = encrypt_sensitive_answer(
                        item.answer.strip(),
                        application_id=str(app.id),
                        question_key=key,
                        settings=self.settings,
                    )
                    if existing_row is None:
                        db.add(
                            ApplicationAnswer(
                                application_id=app.id,
                                question_key=key,
                                question=item.question.strip(),
                                is_sensitive=True,
                                answer_encrypted=ct,
                                answer_plaintext=None,
                                key_version=ver,
                                evidence_source=item.evidence_source or "user_input",
                                user_verified=True,
                            )
                        )
                    else:
                        existing_row.question = item.question.strip()
                        existing_row.is_sensitive = True
                        existing_row.answer_encrypted = ct
                        existing_row.answer_plaintext = None
                        existing_row.key_version = ver
                        existing_row.evidence_source = item.evidence_source or existing_row.evidence_source or "user_input"
                        existing_row.user_verified = True
                else:
                    plain = item.answer.strip()
                    if existing_row is None:
                        db.add(
                            ApplicationAnswer(
                                application_id=app.id,
                                question_key=key,
                                question=item.question.strip(),
                                is_sensitive=False,
                                answer_encrypted=None,
                                answer_plaintext=plain,
                                key_version=None,
                                evidence_source=item.evidence_source or "user_input",
                                user_verified=True,
                            )
                        )
                    else:
                        existing_row.question = item.question.strip()
                        existing_row.is_sensitive = False
                        existing_row.answer_encrypted = None
                        existing_row.answer_plaintext = plain
                        existing_row.key_version = None
                        existing_row.evidence_source = item.evidence_source or existing_row.evidence_source or "user_input"
                        existing_row.user_verified = True

            db.flush()
            all_answers = list(
                db.scalars(select(ApplicationAnswer).where(ApplicationAnswer.application_id == app.id))
            )
            _ahash, unverified, sens_unconf = self._compute_live_answers_hash(app=app, answers=all_answers)
            if app.status == "NEEDS_USER_INPUT" and not unverified and not sens_unconf:
                transition_application_status(
                    db,
                    application=app,
                    next_status="AWAITING_APPROVAL",
                    actor=f"user:{user.id}",
                )

            record_audit_event(
                db,
                user_id=user.id,
                actor=f"user:{user.id}",
                action="application.answers_updated",
                resource_type="application",
                resource_id=str(app.id),
                metadata={
                    "updated_count": len(items),
                    "sensitive_count": sensitive_count,
                    "status": app.status,
                },
            )
            db.commit()
        return self.get_application_detail(db, user=user, application_id=application_id)

    def approve_application(
        self,
        db: Session,
        *,
        user: User,
        application_id: uuid.UUID,
        notes: str | None = None,
    ) -> ApprovalRecordResponse:
        """Create an immutable snapshot-bound ApprovalRecord for an authenticated owner."""
        with _application_lock(application_id):
            app = self._get_owned_application(db, user=user, application_id=application_id, lock_row=True)
            if app.status == "FAILED":
                transition_application_status(
                    db,
                    application=app,
                    next_status="AWAITING_APPROVAL",
                    actor=f"user:{user.id}",
                )
            elif app.status != "AWAITING_APPROVAL":
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"Application cannot be approved from status={app.status}",
                )

            profile, job, resume, mapping, answers, _prev_rec = self._get_owned_execution_entities(
                db, user=user, app=app
            )
            decision = self.policy_router.evaluate(
                db,
                user=user,
                profile=profile,
                job=job,
                resume=resume,
                preferred_mode=app.application_mode,  # type: ignore[arg-type]
                stage="submit",
            )
            if not decision.allowed:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="; ".join(decision.blocker_reasons),
                )

            try:
                _path, _pdf_bytes, verified_checksum = load_verified_resume_snapshot(
                    user_id=user.id,
                    application=app,
                    profile=profile,
                    resume=resume,
                    mapping=mapping,
                    storage_root=Path(self.settings.storage_dir),
                )
            except ApprovalValidationError as exc:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

            answers_hash, unverified, sens_unconf = self._compute_live_answers_hash(app=app, answers=answers)
            if unverified or sens_unconf:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="All answers (including sensitive questions) must be verified before approval",
                )

            snap_hash = compute_approval_snapshot_hash(
                application_id=app.id,
                user_id=user.id,
                resume_id=resume.id,
                resume_checksum=verified_checksum,
                canonical_job_key=app.canonical_job_key,
                destination_url=app.destination_url,
                answers_hash=answers_hash,
                application_mode=app.application_mode,
            )
            now = utc_now()
            record = ApprovalRecord(
                application_id=app.id,
                decision="approved",
                actor=f"user:{user.id}",
                resume_id=resume.id,
                resume_checksum=verified_checksum,
                destination_url=app.destination_url,
                application_mode=app.application_mode,
                answers_hash=answers_hash,
                approval_snapshot_hash=snap_hash,
                notes=notes,
                decided_at=now,
                expires_at=now + timedelta(hours=self.settings.approval_ttl_hours),
            )
            db.add(record)
            transition_application_status(
                db,
                application=app,
                next_status="APPROVED",
                actor=f"user:{user.id}",
            )
            record_audit_event(
                db,
                user_id=user.id,
                actor=f"user:{user.id}",
                action="application.approved",
                resource_type="application",
                resource_id=str(app.id),
                metadata={
                    "approval_snapshot_hash": snap_hash,
                    "resume_checksum": verified_checksum,
                    "application_mode": app.application_mode,
                },
            )
            db.commit()
            db.refresh(record)
            return ApprovalRecordResponse.model_validate(record)

    def reject_application(
        self,
        db: Session,
        *,
        user: User,
        application_id: uuid.UUID,
        notes: str | None = None,
    ) -> ApprovalRecordResponse:
        with _application_lock(application_id):
            app = self._get_owned_application(db, user=user, application_id=application_id, lock_row=True)
            if app.status not in {"NEEDS_USER_INPUT", "AWAITING_APPROVAL"}:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"Application cannot be rejected from status={app.status}",
                )
            _profile, _job, resume, _mapping, answers, _prev = self._get_owned_execution_entities(
                db, user=user, app=app
            )
            answers_hash, _, _ = self._compute_live_answers_hash(app=app, answers=answers)
            snap_hash = compute_approval_snapshot_hash(
                application_id=app.id,
                user_id=user.id,
                resume_id=resume.id,
                resume_checksum=resume.sha256_checksum or "missing",
                canonical_job_key=app.canonical_job_key,
                destination_url=app.destination_url,
                answers_hash=answers_hash,
                application_mode=app.application_mode,
            )
            now = utc_now()
            record = ApprovalRecord(
                application_id=app.id,
                decision="rejected",
                actor=f"user:{user.id}",
                resume_id=resume.id,
                resume_checksum=resume.sha256_checksum or "missing",
                destination_url=app.destination_url,
                application_mode=app.application_mode,
                answers_hash=answers_hash,
                approval_snapshot_hash=snap_hash,
                notes=notes,
                decided_at=now,
                expires_at=now,
            )
            db.add(record)
            transition_application_status(
                db,
                application=app,
                next_status="REJECTED",
                actor=f"user:{user.id}",
            )
            db.commit()
            db.refresh(record)
            return ApprovalRecordResponse.model_validate(record)

    def execute_application(
        self,
        db: Session,
        *,
        user: User,
        application_id: uuid.UUID,
        options: ExecutionOptions | None = None,
    ) -> ExecutionResult:
        """Execute an approved application up to safe prefill + controlled handoff (never auto-submits)."""
        opts = options or ExecutionOptions()
        with _application_lock(application_id):
            app = self._get_owned_application(db, user=user, application_id=application_id, lock_row=True)
            if app.status != "APPROVED":
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"Application execution requires APPROVED status (current={app.status})",
                )
            profile, job, resume, mapping, answers, approval = self._get_owned_execution_entities(
                db, user=user, app=app
            )

            # Official API adapters remain disabled unless explicitly authorized AND implemented.
            if app.application_mode == "official_api" and get_authorized_official_api_adapter(job.source) is None:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Official API submission adapter is not authorized or implemented for this source",
                )

            # Pre-execution policy, resume snapshot, and approval snapshot verification before entering SUBMISSION_PENDING
            decision = self.policy_router.evaluate(
                db,
                user=user,
                profile=profile,
                job=job,
                resume=resume,
                preferred_mode=app.application_mode,  # type: ignore[arg-type]
                stage="submit",
            )
            if not decision.allowed:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="; ".join(decision.blocker_reasons))

            try:
                _path, _bytes, verified_sha256 = load_verified_resume_snapshot(
                    user_id=user.id,
                    application=app,
                    profile=profile,
                    resume=resume,
                    mapping=mapping,
                    storage_root=Path(self.settings.storage_dir),
                )
                validate_approval_and_answers(
                    application=app,
                    user_id=user.id,
                    resume=resume,
                    verified_resume_checksum=verified_sha256,
                    approval_record=approval,
                    answers=answers,
                    settings=self.settings,
                )
            except ApprovalValidationError as exc:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

            assert approval is not None
            transition_application_status(
                db,
                application=app,
                next_status="SUBMISSION_PENDING",
                actor=f"user:{user.id}",
            )
            db.commit()

            try:
                result = run_application_execution_graph(
                    db,
                    user=user,
                    profile=profile,
                    application=app,
                    job=job,
                    resume=resume,
                    mapping=mapping,
                    answers=answers,
                    approval_record=approval,
                    options=opts,
                    browser_executor=self.browser_executor,
                    settings=self.settings,
                )
            except ApprovalValidationError as exc:
                safe_msg = sanitize_safe_error_message(str(exc))
                transition_application_status(
                    db,
                    application=app,
                    next_status="FAILED",
                    actor="system:execution_guard",
                    error_message=safe_msg,
                )
                db.commit()
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=safe_msg) from exc
            except (SSRFBlockedError, UnsupportedPlatformError) as exc:
                safe_msg = sanitize_safe_error_message(str(exc))
                transition_application_status(
                    db,
                    application=app,
                    next_status="FAILED",
                    actor="system:execution_guard",
                    error_message=safe_msg,
                )
                db.commit()
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=safe_msg) from exc
            except SubmitBlockedViolationError as exc:
                safe_msg = sanitize_safe_error_message(str(exc))
                transition_application_status(
                    db,
                    application=app,
                    next_status="UNVERIFIED",
                    actor="system:execution_guard",
                    error_message=safe_msg,
                )
                db.commit()
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=safe_msg) from exc
            except Exception as exc:
                safe_msg = sanitize_safe_error_message(f"Unexpected execution failure: {type(exc).__name__}")
                transition_application_status(
                    db,
                    application=app,
                    next_status="UNVERIFIED",
                    actor="system:execution_guard",
                    error_message=safe_msg,
                )
                db.commit()
                raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=safe_msg) from exc

            safe_res_err = sanitize_safe_error_message(result.error_message) if result.error_message else None
            transition_application_status(
                db,
                application=app,
                next_status="MANUAL_ACTION_REQUIRED",
                actor="system:browser_handoff" if app.application_mode == "human_approved_browser" else "system:manual_handoff",
                error_message=safe_res_err,
            )
            record_audit_event(
                db,
                user_id=user.id,
                actor=f"user:{user.id}",
                action="application.execution_handoff_ready",
                resource_type="application",
                resource_id=str(app.id),
                metadata={
                    "status": app.status,
                    "browser_mode": result.browser_mode,
                    "stopped_before_submit": True,
                    "resume_attached": result.resume_attached,
                    "blocked_submit_attempts": result.blocked_submit_attempts,
                    "filled_count": sum(1 for f in result.fields_filled if f.status == "filled"),
                },
            )
            db.commit()
            return result

    def authorize_manual_submit(
        self,
        db: Session,
        *,
        user: User,
        application_id: uuid.UUID,
        request: ManualSubmitAuthorizationRequest,
    ) -> ManualSubmitAuthorizationResponse:
        """Issue an explicit, authenticated, single-use manual submit authorization for a visible handoff."""
        with _application_lock(application_id):
            app = self._get_owned_application(db, user=user, application_id=application_id, lock_row=True)
            if app.status not in {"MANUAL_ACTION_REQUIRED", "SUBMISSION_PENDING"}:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"Manual submission cannot be authorized in status={app.status}",
                )
            profile, job, resume, mapping, answers, approval = self._get_owned_execution_entities(
                db, user=user, app=app
            )
            decision = self.policy_router.evaluate(
                db,
                user=user,
                profile=profile,
                job=job,
                resume=resume,
                preferred_mode=app.application_mode,  # type: ignore[arg-type]
                stage="submit",
            )
            if not decision.allowed:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="; ".join(decision.blocker_reasons),
                )

            try:
                _path, _bytes, verified_sha256 = load_verified_resume_snapshot(
                    user_id=user.id,
                    application=app,
                    profile=profile,
                    resume=resume,
                    mapping=mapping,
                    storage_root=Path(self.settings.storage_dir),
                )
                _va, _vk, _uk, _suk, answers_hash = validate_approval_and_answers(
                    application=app,
                    user_id=user.id,
                    resume=resume,
                    verified_resume_checksum=verified_sha256,
                    approval_record=approval,
                    answers=answers,
                    settings=self.settings,
                )
            except ApprovalValidationError as exc:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

            assert approval is not None
            try:
                auth = self.handoff_manager.authorize_manual_submit(
                    user_id=user.id,
                    application_id=app.id,
                    request=request,
                    live_answers_hash=answers_hash,
                    live_resume_checksum=verified_sha256,
                    live_approval_snapshot_hash=approval.approval_snapshot_hash,
                )
            except ApprovalValidationError as exc:
                msg = str(exc)
                code = status.HTTP_404_NOT_FOUND if "ownership" in msg.lower() else status.HTTP_409_CONFLICT
                raise HTTPException(status_code=code, detail=msg) from exc
            except SubmitBlockedViolationError as exc:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
            except UnsupportedPlatformError as exc:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

            record_audit_event(
                db,
                user_id=user.id,
                actor=f"user:{user.id}",
                action="application.manual_submit_authorized",
                resource_type="application",
                resource_id=str(app.id),
                metadata={
                    "authorization_id": str(auth.authorization_id),
                    "handoff_session_id": str(auth.handoff_session_id),
                    "http_method": auth.http_method,
                    "expires_at": auth.expires_at.isoformat(),
                },
            )
            db.commit()
            return auth.to_safe_response()

    def get_handoff_session_status(
        self,
        db: Session,
        *,
        user: User,
        application_id: uuid.UUID,
        handoff_session_id: uuid.UUID,
    ) -> HandoffSessionInfo:
        self._get_owned_application(db, user=user, application_id=application_id)
        try:
            info = self.handoff_manager.get_session_info(handoff_session_id=handoff_session_id, user_id=user.id)
        except ApprovalValidationError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        if info.application_id != application_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Handoff session does not match application")
        return info

    def close_handoff_session(
        self,
        db: Session,
        *,
        user: User,
        application_id: uuid.UUID,
        handoff_session_id: uuid.UUID,
    ) -> HandoffSessionInfo:
        self._get_owned_application(db, user=user, application_id=application_id)
        try:
            info = self.handoff_manager.get_session_info(handoff_session_id=handoff_session_id, user_id=user.id)
            if info.application_id != application_id:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Handoff session does not match application")
            closed = self.handoff_manager.close_session(handoff_session_id=handoff_session_id, user_id=user.id)
        except ApprovalValidationError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        record_audit_event(
            db,
            user_id=user.id,
            actor=f"user:{user.id}",
            action="application.handoff_closed",
            resource_type="application",
            resource_id=str(application_id),
            metadata={"handoff_session_id": str(handoff_session_id)},
        )
        db.commit()
        return closed

    def record_submission_outcome(
        self,
        db: Session,
        *,
        user: User,
        application_id: uuid.UUID,
        outcome: str,
        handoff_session_id: uuid.UUID | None = None,
        notes: str | None = None,
    ) -> ApplicationResponse:
        """Record manual, interrupted, or failed submission outcome cleanly without granting CONFIRMED."""
        with _application_lock(application_id):
            app = self._get_owned_application(db, user=user, application_id=application_id, lock_row=True)
            authorized_fired = 0
            if handoff_session_id is not None:
                try:
                    info = self.handoff_manager.get_session_info(
                        handoff_session_id=handoff_session_id,
                        user_id=user.id,
                    )
                    if info.application_id != application_id:
                        raise HTTPException(
                            status_code=status.HTTP_404_NOT_FOUND,
                            detail="Handoff session does not belong to this application",
                        )
                    live = self.handoff_manager.get_session_live_status(
                        handoff_session_id=handoff_session_id,
                        user_id=user.id,
                    )
                    authorized_fired = int(live.get("authorized_submitted", 0))
                    self.handoff_manager.close_session(handoff_session_id=handoff_session_id, user_id=user.id)
                except ApprovalValidationError:
                    pass

            safe_notes = sanitize_safe_error_message(notes) if notes else None
            if outcome == "submitted_in_browser":
                if app.status not in {"MANUAL_ACTION_REQUIRED"}:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail=f"Cannot record browser submission outcome from status={app.status}",
                    )
                if app.submitted_at is None:
                    app.submitted_at = utc_now()
                transition_application_status(
                    db,
                    application=app,
                    next_status="USER_REPORTED_SUBMITTED",
                    actor=f"user:{user.id}",
                    error_message=safe_notes,
                )
            elif outcome == "interrupted":
                if app.status not in {"SUBMISSION_PENDING", "MANUAL_ACTION_REQUIRED", "USER_REPORTED_SUBMITTED"}:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail=f"Cannot mark interrupted from status={app.status}",
                    )
                transition_application_status(
                    db,
                    application=app,
                    next_status="UNVERIFIED",
                    actor=f"user:{user.id}",
                    error_message=safe_notes or "Submission outcome uncertain/interrupted; reconciliation required",
                )
            elif outcome == "failed":
                if app.status not in {"SUBMISSION_PENDING", "MANUAL_ACTION_REQUIRED", "USER_REPORTED_SUBMITTED"}:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail=f"Cannot mark failed from status={app.status}",
                    )
                if authorized_fired > 0 or app.submitted_at is not None:
                    # Submission may already have reached the employer; prevent blind retry via FAILED.
                    transition_application_status(
                        db,
                        application=app,
                        next_status="UNVERIFIED",
                        actor="system:reconciliation_guard",
                        error_message="Authorized submission request already fired; marked UNVERIFIED to prevent duplicate retry",
                    )
                else:
                    transition_application_status(
                        db,
                        application=app,
                        next_status="FAILED",
                        actor=f"user:{user.id}",
                        error_message=safe_notes or "Manual submission aborted prior to submit",
                    )
            else:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Unsupported outcome: {outcome}")

            record_audit_event(
                db,
                user_id=user.id,
                actor=f"user:{user.id}",
                action="application.submission_outcome_recorded",
                resource_type="application",
                resource_id=str(app.id),
                metadata={
                    "outcome": outcome,
                    "resulting_status": app.status,
                    "authorized_fired": authorized_fired,
                },
            )
            db.commit()
            db.refresh(app)
            return ApplicationResponse.model_validate(app)

    def reconcile_confirmation(
        self,
        db: Session,
        *,
        user: User,
        application_id: uuid.UUID,
        confirmation_evidence: dict[str, Any],
    ) -> ApplicationResponse:
        """Verify confirmation strictly through trusted server-side verifier registry (never caller self-assertions)."""
        with _application_lock(application_id):
            app = self._get_owned_application(db, user=user, application_id=application_id, lock_row=True)
            if app.status == "USER_REPORTED_SUBMITTED":
                transition_application_status(
                    db,
                    application=app,
                    next_status="UNVERIFIED",
                    actor="system:reconciliation",
                )
                db.commit()
            elif app.status not in {"UNVERIFIED", "SUBMITTED"}:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"Application cannot be reconciled to CONFIRMED from status={app.status}",
                )

            try:
                transition_application_status(
                    db,
                    application=app,
                    next_status="CONFIRMED",
                    actor=f"user:{user.id}",
                    confirmation_evidence=confirmation_evidence,
                )
            except ValueError as exc:
                record_audit_event(
                    db,
                    user_id=user.id,
                    actor=f"user:{user.id}",
                    action="application.confirmation_rejected",
                    resource_type="application",
                    resource_id=str(app.id),
                    metadata={"reason": str(exc)},
                )
                db.commit()
                raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

            db.commit()
            db.refresh(app)
            return ApplicationResponse.model_validate(app)

    def get_artifact_png_bytes(
        self,
        db: Session,
        *,
        user: User,
        application_id: uuid.UUID,
        artifact_id: str,
    ) -> bytes:
        self._get_owned_application(db, user=user, application_id=application_id)
        try:
            return resolve_owned_artifact_bytes(
                storage_root=Path(self.settings.storage_dir),
                user_id=user.id,
                application_id=application_id,
                artifact_id=artifact_id,
            )
        except (FileNotFoundError, ValueError) as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Artifact not found") from exc
