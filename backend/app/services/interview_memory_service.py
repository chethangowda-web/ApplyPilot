import uuid

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database.models import InterviewMemory, User
from app.schemas.career import (
    InterviewMemoryCreateRequest,
    InterviewMemoryResponse,
    InterviewMemoryUpdateRequest,
)


class InterviewMemoryService:
    """Owner-scoped CRUD for user-controlled interview memory."""

    def list_memories(self, db: Session, *, user: User) -> list[InterviewMemoryResponse]:
        rows = list(
            db.scalars(
                select(InterviewMemory)
                .where(InterviewMemory.user_id == user.id)
                .order_by(InterviewMemory.updated_at.desc())
            )
        )
        return [InterviewMemoryResponse.model_validate(r) for r in rows]

    def create_memory(
        self,
        db: Session,
        *,
        user: User,
        payload: InterviewMemoryCreateRequest,
    ) -> InterviewMemoryResponse:
        mem = InterviewMemory(
            user_id=user.id,
            memory_type=payload.memory_type,
            topic=payload.topic.strip(),
            summary=payload.summary.strip(),
            source="user_created",
            confidence=payload.confidence,
            evidence_refs=[],
        )
        db.add(mem)
        db.commit()
        db.refresh(mem)
        return InterviewMemoryResponse.model_validate(mem)

    def update_memory(
        self,
        db: Session,
        *,
        user: User,
        memory_id: uuid.UUID,
        payload: InterviewMemoryUpdateRequest,
    ) -> InterviewMemoryResponse:
        mem = db.scalar(
            select(InterviewMemory).where(
                InterviewMemory.id == memory_id,
                InterviewMemory.user_id == user.id,
            )
        )
        if mem is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Interview memory not found")

        if payload.topic is not None:
            mem.topic = payload.topic.strip()
        if payload.summary is not None:
            mem.summary = payload.summary.strip()
        if payload.confidence is not None:
            mem.confidence = payload.confidence
        mem.source = "user_edited"

        db.commit()
        db.refresh(mem)
        return InterviewMemoryResponse.model_validate(mem)

    def delete_memory(self, db: Session, *, user: User, memory_id: uuid.UUID) -> None:
        mem = db.scalar(
            select(InterviewMemory).where(
                InterviewMemory.id == memory_id,
                InterviewMemory.user_id == user.id,
            )
        )
        if mem is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Interview memory not found")
        db.delete(mem)
        db.commit()
