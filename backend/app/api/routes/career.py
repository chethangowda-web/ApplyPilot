import uuid

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy.orm import Session

from app.api.dependencies.auth import get_current_user
from app.database.models import User
from app.database.session import get_db
from app.schemas.career import (
    FollowupCreateRequest,
    FollowupResponse,
    FollowupUpdateRequest,
    InterviewKitGenerateRequest,
    InterviewKitProgressRequest,
    InterviewKitResponse,
    InterviewMemoryCreateRequest,
    InterviewMemoryResponse,
    InterviewMemoryUpdateRequest,
    InterviewSessionResponse,
    MockAnswerSubmitRequest,
    MockSessionStartRequest,
)
from app.services.followup_service import FollowupService
from app.services.interview_kit_service import InterviewKitService
from app.services.interview_memory_service import InterviewMemoryService
from app.services.mock_interview_service import MockInterviewService

router = APIRouter(tags=["career-intelligence"])

_followup_service = FollowupService()
_kit_service = InterviewKitService()
_mock_service = MockInterviewService()
_memory_service = InterviewMemoryService()


@router.get("/api/followups", response_model=list[FollowupResponse])
def list_followups_endpoint(
    application_id: uuid.UUID | None = Query(default=None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[FollowupResponse]:
    return _followup_service.list_followups(db, user=current_user, application_id=application_id)


@router.post("/api/followups", response_model=FollowupResponse, status_code=status.HTTP_201_CREATED)
def create_followup_endpoint(
    payload: FollowupCreateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> FollowupResponse:
    return _followup_service.create_followup(db, user=current_user, payload=payload)


@router.patch("/api/followups/{followup_id}", response_model=FollowupResponse)
def update_followup_endpoint(
    followup_id: uuid.UUID,
    payload: FollowupUpdateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> FollowupResponse:
    return _followup_service.update_followup(db, user=current_user, followup_id=followup_id, payload=payload)


@router.post("/api/followups/{followup_id}/complete", response_model=FollowupResponse)
def complete_followup_endpoint(
    followup_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> FollowupResponse:
    return _followup_service.complete_followup(db, user=current_user, followup_id=followup_id)


@router.post("/api/followups/generate-due")
def generate_due_followups_endpoint(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, int]:
    created = _followup_service.generate_due_followups(db, user=current_user)
    return {"created": created}


@router.get("/api/interviews/kits", response_model=list[InterviewKitResponse])
def list_interview_kits_endpoint(
    application_id: uuid.UUID | None = Query(default=None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[InterviewKitResponse]:
    return _kit_service.list_kits(db, user=current_user, application_id=application_id)


@router.post("/api/interviews/kits/generate", response_model=InterviewKitResponse, status_code=status.HTTP_201_CREATED)
def generate_interview_kit_endpoint(
    payload: InterviewKitGenerateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> InterviewKitResponse:
    return _kit_service.generate_kit(
        db,
        user=current_user,
        application_id=payload.application_id,
        module_key=payload.module_key,
    )


@router.get("/api/interviews/kits/{kit_id}", response_model=InterviewKitResponse)
def get_interview_kit_endpoint(
    kit_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> InterviewKitResponse:
    return _kit_service.get_kit(db, user=current_user, kit_id=kit_id)


@router.patch("/api/interviews/kits/{kit_id}/progress", response_model=InterviewKitResponse)
def update_interview_kit_progress_endpoint(
    kit_id: uuid.UUID,
    payload: InterviewKitProgressRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> InterviewKitResponse:
    return _kit_service.update_progress(db, user=current_user, kit_id=kit_id, payload=payload)


@router.get("/api/interviews/kits/{kit_id}/export")
def export_interview_kit_endpoint(
    kit_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Response:
    md = _kit_service.export_kit_markdown(db, user=current_user, kit_id=kit_id)
    return Response(content=md, media_type="text/markdown")


@router.post("/api/interviews/sessions/start", response_model=InterviewSessionResponse, status_code=status.HTTP_201_CREATED)
def start_mock_session_endpoint(
    payload: MockSessionStartRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> InterviewSessionResponse:
    return _mock_service.start_session(db, user=current_user, payload=payload)


@router.get("/api/interviews/sessions/{session_id}", response_model=InterviewSessionResponse)
def get_mock_session_endpoint(
    session_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> InterviewSessionResponse:
    return _mock_service.get_session(db, user=current_user, session_id=session_id)


@router.post("/api/interviews/sessions/{session_id}/answer", response_model=InterviewSessionResponse)
def submit_mock_answer_endpoint(
    session_id: uuid.UUID,
    payload: MockAnswerSubmitRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> InterviewSessionResponse:
    return _mock_service.submit_answer(db, user=current_user, session_id=session_id, answer=payload.answer)


@router.post("/api/interviews/sessions/{session_id}/complete", response_model=InterviewSessionResponse)
def complete_mock_session_endpoint(
    session_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> InterviewSessionResponse:
    return _mock_service.complete_session(db, user=current_user, session_id=session_id)


@router.get("/api/interviews/memory", response_model=list[InterviewMemoryResponse])
def list_interview_memory_endpoint(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[InterviewMemoryResponse]:
    return _memory_service.list_memories(db, user=current_user)


@router.post("/api/interviews/memory", response_model=InterviewMemoryResponse, status_code=status.HTTP_201_CREATED)
def create_interview_memory_endpoint(
    payload: InterviewMemoryCreateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> InterviewMemoryResponse:
    return _memory_service.create_memory(db, user=current_user, payload=payload)


@router.patch("/api/interviews/memory/{memory_id}", response_model=InterviewMemoryResponse)
def update_interview_memory_endpoint(
    memory_id: uuid.UUID,
    payload: InterviewMemoryUpdateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> InterviewMemoryResponse:
    return _memory_service.update_memory(db, user=current_user, memory_id=memory_id, payload=payload)


@router.delete("/api/interviews/memory/{memory_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_interview_memory_endpoint(
    memory_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Response:
    _memory_service.delete_memory(db, user=current_user, memory_id=memory_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
