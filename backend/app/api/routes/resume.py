import uuid
from pathlib import Path
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from fastapi.responses import FileResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from app.api.dependencies.auth import get_current_user
from app.core.config import get_settings
from app.database.models import CandidateProfile, Resume, User
from app.database.session import get_db
from app.schemas.resume import ResumeMetadataResponse
from app.services.audit_service import record_audit_event
from app.services.resume_parser import parse_resume_bytes
from app.services.resume_vault_service import (
    ALLOWED_EXTENSIONS,
    compute_sha256,
    resolve_owned_resume_path,
    sanitize_filename,
    store_resume_bytes,
    validate_file_contents,
)

router = APIRouter(prefix="/api/resume", tags=["resume"])


def _get_or_create_profile(db: Session, user: User) -> CandidateProfile:
    profile = db.scalar(select(CandidateProfile).where(CandidateProfile.user_id == user.id))
    if profile is None:
        profile = CandidateProfile(
            user_id=user.id,
            full_name=user.email.split("@", 1)[0],
            email=user.email,
        )
        db.add(profile)
        db.flush()
    return profile


def _get_owned_resume(db: Session, user: User, resume_id: uuid.UUID) -> Resume:
    stmt = (
        select(Resume)
        .join(CandidateProfile, Resume.profile_id == CandidateProfile.id)
        .where(Resume.id == resume_id, CandidateProfile.user_id == user.id)
    )
    resume = db.scalar(stmt)
    if resume is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Resume not found")
    return resume


@router.post("/upload", response_model=ResumeMetadataResponse, status_code=status.HTTP_201_CREATED)
async def upload_resume(
    file: UploadFile = File(...),
    is_master: bool = Form(default=True),
    version_label: str | None = Form(default=None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Resume:
    settings = get_settings()
    safe_filename, ext = sanitize_filename(file.filename or "")
    data = await file.read()
    validate_file_contents(data, ext, settings.max_resume_size_bytes)

    parsed = parse_resume_bytes(data, ext)
    checksum = compute_sha256(data)
    profile = _get_or_create_profile(db, current_user)

    duplicate = db.scalar(
        select(Resume).where(Resume.profile_id == profile.id, Resume.sha256_checksum == checksum)
    )
    if duplicate is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Duplicate resume upload: an identical file with this SHA-256 checksum already exists",
        )

    existing_count = db.scalar(
        select(func.count()).select_from(Resume).where(Resume.profile_id == profile.id)
    ) or 0
    prefix = "master" if is_master else "tailored"
    resolved_label = (version_label or f"{prefix}-v{existing_count + 1}").strip()[:120]

    resume_id = uuid.uuid4()
    storage_root = Path(settings.storage_dir)
    stored_path = store_resume_bytes(
        storage_root=storage_root,
        user_id=current_user.id,
        resume_id=resume_id,
        safe_filename=safe_filename,
        is_master=is_master,
        data=data,
    )

    resume = Resume(
        id=resume_id,
        profile_id=profile.id,
        is_master=is_master,
        original_filename=safe_filename,
        content_type=ALLOWED_EXTENSIONS[ext],
        file_size_bytes=len(data),
        version_label=resolved_label,
        pdf_path=str(stored_path) if ext == ".pdf" else None,
        docx_path=str(stored_path) if ext == ".docx" else None,
        sha256_checksum=checksum,
        template_version="master-upload-v1" if is_master else "tailored-upload-v1",
        ats_report=parsed["ats_report"],
        parsed_metadata=parsed["parsed_metadata"],
        validation_passed=parsed["validation_passed"],
    )
    db.add(resume)
    db.flush()
    record_audit_event(
        db,
        user_id=current_user.id,
        actor="user",
        action="resume_uploaded",
        resource_type="resume",
        resource_id=str(resume.id),
        metadata={
            "is_master": is_master,
            "file_format": ext.lstrip("."),
            "file_size_bytes": len(data),
            "sha256_checksum": checksum,
            "detected_heading_count": parsed["ats_report"]["detected_heading_count"],
        },
    )
    db.commit()
    db.refresh(resume)
    return resume


@router.get("/versions", response_model=list[ResumeMetadataResponse])
def list_resume_versions(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[Resume]:
    profile = db.scalar(select(CandidateProfile).where(CandidateProfile.user_id == current_user.id))
    if profile is None:
        return []
    stmt = (
        select(Resume)
        .where(Resume.profile_id == profile.id)
        .order_by(Resume.created_at.desc())
    )
    return list(db.scalars(stmt).all())


@router.get("/versions/{resume_id}", response_model=ResumeMetadataResponse)
def get_resume_version(
    resume_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Resume:
    return _get_owned_resume(db, current_user, resume_id)


@router.get("/versions/{resume_id}/download")
def download_resume_version(
    resume_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> FileResponse:
    settings = get_settings()
    resume = _get_owned_resume(db, current_user, resume_id)
    stored_path = resume.pdf_path or resume.docx_path
    resolved = resolve_owned_resume_path(Path(settings.storage_dir), stored_path)
    return FileResponse(
        path=resolved,
        media_type=resume.content_type,
        filename=resume.original_filename,
    )
