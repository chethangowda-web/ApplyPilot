from datetime import datetime, timedelta, timezone
import hashlib
import uuid

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database.models import Application, ApplicationFollowup, Job, User
from app.schemas.career import FollowupCreateRequest, FollowupResponse, FollowupUpdateRequest
from app.services.audit_service import record_audit_event


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _ensure_utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def _build_draft(app: Application, job: Job, reminder_type: str) -> tuple[str, str]:
    role = job.role_title
    company = job.company
    if app.status == "CONFIRMED":
        status_line = "following up on my confirmed application"
    elif app.status == "INTERVIEW":
        status_line = "following up regarding our recent interview"
    else:
        status_line = "checking in on my application status"

    if reminder_type == "interview_thank_you":
        subject = f"Thank you — {role} Interview at {company}"
        body = (
            f"Hi {company} Recruiting Team,\n\n"
            f"Thank you for speaking with me about the {role} position. "
            f"I remain very excited about the opportunity and look forward to next steps.\n\n"
            f"Best regards,"
        )
    else:
        subject = f"Follow-up: {role} Application at {company}"
        body = (
            f"Hi {company} Recruiting Team,\n\n"
            f"I am {status_line} for the {role} role. "
            f"Please let me know if any additional details would be helpful.\n\n"
            f"Best regards,"
        )
    return subject, body


class FollowupService:
    """Owner-scoped application follow-up reminders and editable draft templates (never auto-sends)."""

    def _to_response(self, row: ApplicationFollowup) -> FollowupResponse:
        now = _utc_now()
        due = _ensure_utc(row.snoozed_until or row.due_at)
        is_overdue = row.status in {"pending", "snoozed"} and due < now
        resp = FollowupResponse.model_validate(row)
        resp.is_overdue = is_overdue
        return resp

    def create_followup(
        self,
        db: Session,
        *,
        user: User,
        payload: FollowupCreateRequest,
    ) -> FollowupResponse:
        row_data = db.execute(
            select(Application, Job)
            .join(Job, Job.id == Application.job_id)
            .where(
                Application.id == payload.application_id,
                Application.user_id == user.id,
                Application.deleted_at.is_(None),
            )
        ).first()
        if row_data is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Application not found")
        app, job = row_data

        if app.status in {"REJECTED", "WITHDRAWN", "EXPIRED"}:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Follow-up reminders cannot be created for status={app.status}",
            )

        now = _utc_now()
        due_at = _ensure_utc(payload.due_at or (now + timedelta(days=payload.interval_days)))
        idem = hashlib.sha256(
            f"{user.id}:{app.id}:{payload.reminder_type}:{due_at.date().isoformat()}".encode("utf-8")
        ).hexdigest()

        existing = db.scalar(
            select(ApplicationFollowup).where(ApplicationFollowup.idempotency_key == idem)
        )
        if existing is not None:
            return self._to_response(existing)

        default_subject, default_body = _build_draft(app, job, payload.reminder_type)
        recruiter_ctx = {
            "company": job.company,
            "role_title": job.role_title,
            "application_status": app.status,
            "destination_url": app.destination_url,
        }
        fu = ApplicationFollowup(
            application_id=app.id,
            user_id=user.id,
            reminder_type=payload.reminder_type,
            status="pending",
            due_at=due_at,
            interval_days=payload.interval_days,
            draft_subject=(payload.draft_subject or default_subject).strip(),
            draft_body=(payload.draft_body or default_body).strip(),
            recruiter_context=recruiter_ctx,
            idempotency_key=idem,
        )
        db.add(fu)
        app.next_followup_at = due_at
        record_audit_event(
            db,
            user_id=user.id,
            actor=f"user:{user.id}",
            action="followup.created",
            resource_type="application_followup",
            resource_id=str(fu.id),
            metadata={"application_id": str(app.id), "reminder_type": payload.reminder_type},
        )
        db.commit()
        db.refresh(fu)
        return self._to_response(fu)

    def list_followups(
        self,
        db: Session,
        *,
        user: User,
        application_id: uuid.UUID | None = None,
    ) -> list[FollowupResponse]:
        query = select(ApplicationFollowup).where(ApplicationFollowup.user_id == user.id)
        if application_id is not None:
            query = query.where(ApplicationFollowup.application_id == application_id)
        rows = list(db.scalars(query.order_by(ApplicationFollowup.due_at.asc())))
        return [self._to_response(r) for r in rows]

    def update_followup(
        self,
        db: Session,
        *,
        user: User,
        followup_id: uuid.UUID,
        payload: FollowupUpdateRequest,
    ) -> FollowupResponse:
        fu = db.scalar(
            select(ApplicationFollowup).where(
                ApplicationFollowup.id == followup_id,
                ApplicationFollowup.user_id == user.id,
            )
        )
        if fu is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Follow-up not found")

        if payload.draft_subject is not None:
            fu.draft_subject = payload.draft_subject.strip()
        if payload.draft_body is not None:
            fu.draft_body = payload.draft_body.strip()
        if payload.snooze_days is not None:
            snoozed = _utc_now() + timedelta(days=payload.snooze_days)
            fu.snoozed_until = snoozed
            fu.due_at = snoozed
            fu.status = "snoozed"
        elif payload.due_at is not None:
            fu.due_at = _ensure_utc(payload.due_at)
            fu.snoozed_until = None
            fu.status = "pending"

        db.commit()
        db.refresh(fu)
        return self._to_response(fu)

    def complete_followup(
        self,
        db: Session,
        *,
        user: User,
        followup_id: uuid.UUID,
    ) -> FollowupResponse:
        fu = db.scalar(
            select(ApplicationFollowup).where(
                ApplicationFollowup.id == followup_id,
                ApplicationFollowup.user_id == user.id,
            )
        )
        if fu is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Follow-up not found")

        if fu.status != "completed":
            fu.status = "completed"
            fu.completed_at = _utc_now()
            record_audit_event(
                db,
                user_id=user.id,
                actor=f"user:{user.id}",
                action="followup.completed",
                resource_type="application_followup",
                resource_id=str(fu.id),
                metadata={"application_id": str(fu.application_id)},
            )
            db.commit()
            db.refresh(fu)
        return self._to_response(fu)

    def generate_due_followups(self, db: Session, *, user: User | None = None, interval_days: int = 7) -> int:
        """Idempotently schedule initial follow-ups for confirmed/submitted applications without pending reminders."""
        cutoff = _utc_now() - timedelta(days=interval_days)
        query = (
            select(Application, Job)
            .join(Job, Job.id == Application.job_id)
            .where(
                Application.deleted_at.is_(None),
                Application.status.in_(["CONFIRMED", "SUBMITTED", "USER_REPORTED_SUBMITTED"]),
                Application.updated_at <= cutoff,
            )
        )
        if user is not None:
            query = query.where(Application.user_id == user.id)

        created = 0
        for app, job in db.execute(query).all():
            if app.user_id is None:
                continue
            existing = db.scalar(
                select(ApplicationFollowup).where(
                    ApplicationFollowup.application_id == app.id,
                    ApplicationFollowup.user_id == app.user_id,
                )
            )
            if existing is not None:
                continue
            due = _utc_now()
            idem = hashlib.sha256(f"{app.user_id}:{app.id}:initial_followup:auto".encode("utf-8")).hexdigest()
            if db.scalar(select(ApplicationFollowup).where(ApplicationFollowup.idempotency_key == idem)):
                continue
            subj, body = _build_draft(app, job, "initial_followup")
            db.add(
                ApplicationFollowup(
                    application_id=app.id,
                    user_id=app.user_id,
                    reminder_type="initial_followup",
                    status="pending",
                    due_at=due,
                    interval_days=interval_days,
                    draft_subject=subj,
                    draft_body=body,
                    recruiter_context={
                        "company": job.company,
                        "role_title": job.role_title,
                        "application_status": app.status,
                    },
                    idempotency_key=idem,
                )
            )
            created += 1
        if created:
            db.commit()
        return created
