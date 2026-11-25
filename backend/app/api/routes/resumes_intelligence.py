import uuid
from fastapi import APIRouter, Body, Depends, Query, status
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.api.dependencies.auth import get_current_user
from app.database.models import User
from app.database.session import get_db
from app.schemas.resume_intelligence import (
    ApprovalDecisionRequest,
    ApprovalDecisionResponse,
    FactualValidationReportResponse,
    RegenerateResumeRequest,
    TailoredResumeDetailResponse,
    TailoredResumeSummaryResponse,
    TailorEnqueueResponse,
    TailoringDiffResponse,
)
from app.services.resume_intelligence_service import (
    approve_tailored_resume,
    get_resume_validation_report,
    get_tailored_resume_detail,
    get_tailored_resume_diff,
    list_tailored_resumes,
    regenerate_tailored_resume,
    reject_tailored_resume,
    resolve_verified_tailored_download,
)

router = APIRouter(prefix="/api/resumes", tags=["resume_intelligence"])


@router.get("/tailored", response_model=list[TailoredResumeSummaryResponse])
def list_user_tailored_resumes(
    job_id: uuid.UUID | None = Query(default=None),
    approval_status: str | None = Query(default=None),
    active_only: bool = Query(default=False),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[TailoredResumeSummaryResponse]:
    """List owner-scoped tailored resumes across jobs with version and approval state."""
    return list_tailored_resumes(
        db,
        user=current_user,
        job_id=job_id,
        approval_status=approval_status,
        active_only=active_only,
    )


@router.post("/{resume_id}/regenerate", response_model=TailorEnqueueResponse, status_code=status.HTTP_202_ACCEPTED)
def regenerate_resume_endpoint(
    resume_id: uuid.UUID,
    _payload: RegenerateResumeRequest | None = Body(default=None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> TailorEnqueueResponse:
    """Enqueue an intentional new tailored resume generation request for an existing tailored resume."""
    return regenerate_tailored_resume(db, user=current_user, resume_id=resume_id)


@router.get("/{resume_id}/tailored", response_model=TailoredResumeDetailResponse)
def get_tailored_resume_detail_endpoint(
    resume_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> TailoredResumeDetailResponse:
    """Retrieve owner-only tailored resume details, validation/evaluation reports, and approval state."""
    return get_tailored_resume_detail(db, user=current_user, resume_id=resume_id)


@router.get("/{resume_id}/diff", response_model=TailoringDiffResponse)
def get_tailored_resume_diff_endpoint(
    resume_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> TailoringDiffResponse:
    """Retrieve structured tailoring diff with evidence references for an owned tailored resume."""
    return get_tailored_resume_diff(db, user=current_user, resume_id=resume_id)


@router.get("/{resume_id}/validation", response_model=FactualValidationReportResponse)
def get_tailored_resume_validation_endpoint(
    resume_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> FactualValidationReportResponse:
    """Retrieve factual validation and ATS parsing reports for an owned resume."""
    return get_resume_validation_report(db, user=current_user, resume_id=resume_id)


@router.get("/{resume_id}/download")
def download_tailored_resume_endpoint(
    resume_id: uuid.UUID,
    approved_only: bool = Query(default=False),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> FileResponse:
    """Download an immutable, validated tailored PDF after verifying ownership and SHA-256 integrity."""
    resume, resolved_path = resolve_verified_tailored_download(
        db,
        user=current_user,
        resume_id=resume_id,
        approved_only=approved_only,
    )
    return FileResponse(
        path=resolved_path,
        media_type=resume.content_type,
        filename=resume.original_filename,
    )


@router.post("/{resume_id}/approve", response_model=ApprovalDecisionResponse)
def approve_tailored_resume_endpoint(
    resume_id: uuid.UUID,
    payload: ApprovalDecisionRequest | None = Body(default=None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ApprovalDecisionResponse:
    """Owner-only idempotent approval of a completed, factually validated tailored resume."""
    return approve_tailored_resume(
        db,
        user=current_user,
        resume_id=resume_id,
        notes=payload.notes if payload else None,
    )


@router.post("/{resume_id}/reject", response_model=ApprovalDecisionResponse)
def reject_tailored_resume_endpoint(
    resume_id: uuid.UUID,
    payload: ApprovalDecisionRequest | None = Body(default=None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ApprovalDecisionResponse:
    """Owner-only rejection of a tailored resume without mutating the immutable PDF artifact."""
    return reject_tailored_resume(
        db,
        user=current_user,
        resume_id=resume_id,
        notes=payload.notes if payload else None,
    )
