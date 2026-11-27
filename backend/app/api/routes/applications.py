import uuid
from fastapi import APIRouter, Body, Depends, Response, status
from sqlalchemy.orm import Session

from app.api.dependencies.auth import get_current_user
from app.database.models import User
from app.database.session import get_db
from app.schemas.applications import (
    ApplicationAnswersSubmitRequest,
    ApplicationDecisionRequest,
    ApplicationDetailResponse,
    ApplicationExecuteRequest,
    ApplicationPreparationResult,
    ApplicationPrepareRequest,
    ApplicationReconcileRequest,
    ApplicationResponse,
    ApplicationSubmitOutcomeRequest,
    ApprovalRecordResponse,
    ExecutionResult,
    HandoffSessionInfo,
    ManualSubmitAuthorizationRequest,
    ManualSubmitAuthorizationResponse,
)
from app.services.application_execution_service import ApplicationExecutionService

router = APIRouter(prefix="/api/applications", tags=["applications"])


def get_application_execution_service() -> ApplicationExecutionService:
    """FastAPI dependency provider for ApplicationExecutionService."""
    return ApplicationExecutionService()


@router.post("/prepare", response_model=ApplicationPreparationResult, status_code=status.HTTP_201_CREATED)
def prepare_application_endpoint(
    payload: ApplicationPrepareRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    service: ApplicationExecutionService = Depends(get_application_execution_service),
) -> ApplicationPreparationResult:
    """Evaluate policy, extract/map form questions, encrypt sensitive fields, and prepare application."""
    return service.prepare_application(
        db,
        user=current_user,
        job_id=payload.job_id,
        resume_id=payload.resume_id,
        preferred_mode=payload.preferred_mode,
    )


@router.get("", response_model=list[ApplicationResponse])
def list_applications_endpoint(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    service: ApplicationExecutionService = Depends(get_application_execution_service),
) -> list[ApplicationResponse]:
    """List applications owned strictly by the authenticated user."""
    return service.list_applications(db, user=current_user)


@router.get("/{application_id}", response_model=ApplicationDetailResponse)
def get_application_detail_endpoint(
    application_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    service: ApplicationExecutionService = Depends(get_application_execution_service),
) -> ApplicationDetailResponse:
    """Fetch owner-only application details with redacted sensitive answer previews."""
    return service.get_application_detail(db, user=current_user, application_id=application_id)


@router.post("/{application_id}/answers", response_model=ApplicationDetailResponse)
def submit_application_answers_endpoint(
    application_id: uuid.UUID,
    payload: ApplicationAnswersSubmitRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    service: ApplicationExecutionService = Depends(get_application_execution_service),
) -> ApplicationDetailResponse:
    """Save and verify user-provided application answers (sensitive fields AES-256-GCM encrypted)."""
    return service.submit_user_answers(
        db,
        user=current_user,
        application_id=application_id,
        items=payload.answers,
    )


@router.post("/{application_id}/approve", response_model=ApprovalRecordResponse)
def approve_application_endpoint(
    application_id: uuid.UUID,
    payload: ApplicationDecisionRequest | None = Body(default=None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    service: ApplicationExecutionService = Depends(get_application_execution_service),
) -> ApprovalRecordResponse:
    """Create an immutable snapshot-bound ApprovalRecord for an authenticated owner."""
    return service.approve_application(
        db,
        user=current_user,
        application_id=application_id,
        notes=payload.notes if payload else None,
    )


@router.post("/{application_id}/reject", response_model=ApprovalRecordResponse)
def reject_application_endpoint(
    application_id: uuid.UUID,
    payload: ApplicationDecisionRequest | None = Body(default=None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    service: ApplicationExecutionService = Depends(get_application_execution_service),
) -> ApprovalRecordResponse:
    """Reject a prepared application with append-only auditability."""
    return service.reject_application(
        db,
        user=current_user,
        application_id=application_id,
        notes=payload.notes if payload else None,
    )


@router.post("/{application_id}/execute", response_model=ExecutionResult)
def execute_application_endpoint(
    application_id: uuid.UUID,
    payload: ApplicationExecuteRequest | None = Body(default=None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    service: ApplicationExecutionService = Depends(get_application_execution_service),
) -> ExecutionResult:
    """Execute prefill and controlled handoff for an approved application (never auto-submits)."""
    return service.execute_application(
        db,
        user=current_user,
        application_id=application_id,
        options=payload.options if payload else None,
    )


@router.get("/{application_id}/handoff/{handoff_session_id}", response_model=HandoffSessionInfo)
def get_handoff_session_endpoint(
    application_id: uuid.UUID,
    handoff_session_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    service: ApplicationExecutionService = Depends(get_application_execution_service),
) -> HandoffSessionInfo:
    """Retrieve owner-scoped status of an active browser handoff session."""
    return service.get_handoff_session_status(
        db,
        user=current_user,
        application_id=application_id,
        handoff_session_id=handoff_session_id,
    )


@router.post("/{application_id}/authorize-manual-submit", response_model=ManualSubmitAuthorizationResponse)
def authorize_manual_submit_endpoint(
    application_id: uuid.UUID,
    payload: ManualSubmitAuthorizationRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    service: ApplicationExecutionService = Depends(get_application_execution_service),
) -> ManualSubmitAuthorizationResponse:
    """Explicit authenticated user confirmation granting one-time manual browser submit authorization."""
    return service.authorize_manual_submit(
        db,
        user=current_user,
        application_id=application_id,
        request=payload,
    )


@router.post("/{application_id}/handoff/{handoff_session_id}/close", response_model=HandoffSessionInfo)
def close_handoff_session_endpoint(
    application_id: uuid.UUID,
    handoff_session_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    service: ApplicationExecutionService = Depends(get_application_execution_service),
) -> HandoffSessionInfo:
    """Close and terminate an owner's active browser handoff session."""
    return service.close_handoff_session(
        db,
        user=current_user,
        application_id=application_id,
        handoff_session_id=handoff_session_id,
    )


@router.post("/{application_id}/submit-outcome", response_model=ApplicationResponse)
def record_submit_outcome_endpoint(
    application_id: uuid.UUID,
    payload: ApplicationSubmitOutcomeRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    service: ApplicationExecutionService = Depends(get_application_execution_service),
) -> ApplicationResponse:
    """Record manual submit report, interruption, or pre-submit failure (never sets CONFIRMED)."""
    return service.record_submission_outcome(
        db,
        user=current_user,
        application_id=application_id,
        outcome=payload.outcome,
        handoff_session_id=payload.handoff_session_id,
        notes=payload.notes,
    )


@router.post("/{application_id}/reconcile", response_model=ApplicationResponse)
def reconcile_application_confirmation_endpoint(
    application_id: uuid.UUID,
    payload: ApplicationReconcileRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    service: ApplicationExecutionService = Depends(get_application_execution_service),
) -> ApplicationResponse:
    """Reconcile application to CONFIRMED strictly through server-side ConfirmationVerifier validation."""
    return service.reconcile_confirmation(
        db,
        user=current_user,
        application_id=application_id,
        confirmation_evidence=payload.confirmation_evidence,
    )


@router.get("/{application_id}/artifacts/{artifact_id}")
def get_application_artifact_endpoint(
    application_id: uuid.UUID,
    artifact_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    service: ApplicationExecutionService = Depends(get_application_execution_service),
) -> Response:
    """Return masked PNG artifact bytes after owner verification (never exposes server paths)."""
    png_bytes = service.get_artifact_png_bytes(
        db,
        user=current_user,
        application_id=application_id,
        artifact_id=artifact_id,
    )
    return Response(content=png_bytes, media_type="image/png")
