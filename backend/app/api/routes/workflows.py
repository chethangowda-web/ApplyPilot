from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.api.dependencies.auth import get_current_user
from app.database.models import User, WorkflowRun
from app.database.session import get_db
from app.schemas.jobs import WorkflowRunResponse

router = APIRouter(prefix="/api/workflows", tags=["workflows"])


@router.get("/{thread_id}", response_model=WorkflowRunResponse)
def get_workflow_status(
    thread_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> WorkflowRun:
    run = db.scalar(
        select(WorkflowRun).where(
            WorkflowRun.thread_id == thread_id,
            WorkflowRun.user_id == current_user.id,
        )
    )
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workflow run not found")
    return run
