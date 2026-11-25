import uuid
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload
from app.api.dependencies.auth import get_current_user
from app.database.models import (
    CandidateProfile,
    CandidateSkill,
    Certification,
    Experience,
    Project,
    User,
)
from app.database.session import get_db
from app.schemas.profile import (
    CandidateProfileResponse,
    CandidateProfileUpsertRequest,
    CandidateSkillIn,
    CandidateSkillOut,
    CertificationIn,
    CertificationOut,
    ExperienceIn,
    ExperienceOut,
    ProjectIn,
    ProjectOut,
)
from app.services.audit_service import record_audit_event

router = APIRouter(prefix="/api/profile", tags=["profile"])


def _load_profile_for_user(db: Session, user_id: uuid.UUID) -> CandidateProfile | None:
    stmt = (
        select(CandidateProfile)
        .where(CandidateProfile.user_id == user_id)
        .options(
            selectinload(CandidateProfile.skills),
            selectinload(CandidateProfile.projects),
            selectinload(CandidateProfile.experiences),
            selectinload(CandidateProfile.certifications),
        )
    )
    return db.scalar(stmt)


def _require_profile(db: Session, user: User) -> CandidateProfile:
    profile = _load_profile_for_user(db, user.id)
    if profile is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Candidate profile not found; create one via POST /api/profile first",
        )
    return profile


def _delete_owned_item(
    db: Session,
    *,
    model_cls: type,
    item_id: uuid.UUID,
    profile: CandidateProfile,
    user: User,
    resource_type: str,
    action: str,
) -> dict[str, str]:
    item = db.scalar(
        select(model_cls).where(model_cls.id == item_id, model_cls.profile_id == profile.id)
    )
    if item is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"{resource_type} not found",
        )
    db.delete(item)
    record_audit_event(
        db,
        user_id=user.id,
        actor="user",
        action=action,
        resource_type=resource_type,
        resource_id=str(item_id),
    )
    db.commit()
    return {"deleted": str(item_id)}


@router.post("", response_model=CandidateProfileResponse)
def upsert_profile(
    payload: CandidateProfileUpsertRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> CandidateProfile:
    profile = _load_profile_for_user(db, current_user.id)
    action = "profile_updated"
    if profile is None:
        profile = CandidateProfile(user_id=current_user.id, full_name=payload.full_name, email=payload.email)
        db.add(profile)
        action = "profile_created"

    profile.full_name = payload.full_name
    profile.phone = payload.phone
    profile.email = payload.email
    profile.location = payload.location
    profile.github_url = payload.github_url
    profile.linkedin_url = payload.linkedin_url
    profile.portfolio_url = payload.portfolio_url
    profile.summary = payload.summary
    profile.education = [e.model_dump() for e in payload.education]
    profile.achievements = payload.achievements
    profile.confirmed_preferences = payload.confirmed_preferences

    db.flush()
    record_audit_event(
        db,
        user_id=current_user.id,
        actor="user",
        action=action,
        resource_type="candidate_profile",
        resource_id=str(profile.id),
    )
    db.commit()
    return _require_profile(db, current_user)


@router.get("", response_model=CandidateProfileResponse)
def get_profile(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> CandidateProfile:
    return _require_profile(db, current_user)


@router.post("/skills", response_model=CandidateSkillOut, status_code=status.HTTP_201_CREATED)
def add_skill(
    payload: CandidateSkillIn,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> CandidateSkill:
    profile = _require_profile(db, current_user)
    skill = CandidateSkill(profile_id=profile.id, **payload.model_dump())
    db.add(skill)
    db.flush()
    record_audit_event(
        db,
        user_id=current_user.id,
        actor="user",
        action="skill_added",
        resource_type="candidate_skill",
        resource_id=str(skill.id),
        metadata={"category": skill.category},
    )
    db.commit()
    db.refresh(skill)
    return skill


@router.delete("/skills/{skill_id}")
def delete_skill(
    skill_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, str]:
    profile = _require_profile(db, current_user)
    return _delete_owned_item(
        db,
        model_cls=CandidateSkill,
        item_id=skill_id,
        profile=profile,
        user=current_user,
        resource_type="candidate_skill",
        action="skill_deleted",
    )


@router.post("/projects", response_model=ProjectOut, status_code=status.HTTP_201_CREATED)
def add_project(
    payload: ProjectIn,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Project:
    profile = _require_profile(db, current_user)
    project = Project(profile_id=profile.id, **payload.model_dump())
    db.add(project)
    db.flush()
    record_audit_event(
        db,
        user_id=current_user.id,
        actor="user",
        action="project_added",
        resource_type="project",
        resource_id=str(project.id),
    )
    db.commit()
    db.refresh(project)
    return project


@router.delete("/projects/{project_id}")
def delete_project(
    project_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, str]:
    profile = _require_profile(db, current_user)
    return _delete_owned_item(
        db,
        model_cls=Project,
        item_id=project_id,
        profile=profile,
        user=current_user,
        resource_type="project",
        action="project_deleted",
    )


@router.post("/experiences", response_model=ExperienceOut, status_code=status.HTTP_201_CREATED)
def add_experience(
    payload: ExperienceIn,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Experience:
    profile = _require_profile(db, current_user)
    exp = Experience(profile_id=profile.id, **payload.model_dump())
    db.add(exp)
    db.flush()
    record_audit_event(
        db,
        user_id=current_user.id,
        actor="user",
        action="experience_added",
        resource_type="experience",
        resource_id=str(exp.id),
    )
    db.commit()
    db.refresh(exp)
    return exp


@router.delete("/experiences/{experience_id}")
def delete_experience(
    experience_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, str]:
    profile = _require_profile(db, current_user)
    return _delete_owned_item(
        db,
        model_cls=Experience,
        item_id=experience_id,
        profile=profile,
        user=current_user,
        resource_type="experience",
        action="experience_deleted",
    )


@router.post("/certifications", response_model=CertificationOut, status_code=status.HTTP_201_CREATED)
def add_certification(
    payload: CertificationIn,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Certification:
    profile = _require_profile(db, current_user)
    cert = Certification(profile_id=profile.id, **payload.model_dump())
    db.add(cert)
    db.flush()
    record_audit_event(
        db,
        user_id=current_user.id,
        actor="user",
        action="certification_added",
        resource_type="certification",
        resource_id=str(cert.id),
    )
    db.commit()
    db.refresh(cert)
    return cert


@router.delete("/certifications/{cert_id}")
def delete_certification(
    cert_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, str]:
    profile = _require_profile(db, current_user)
    return _delete_owned_item(
        db,
        model_cls=Certification,
        item_id=cert_id,
        profile=profile,
        user=current_user,
        resource_type="certification",
        action="certification_deleted",
    )
