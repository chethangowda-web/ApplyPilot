import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.api.dependencies.auth import get_current_user
from app.database.models import User
from app.database.session import get_db
from app.schemas.integrations import (
    GmailConnectionSummary,
    GmailDisconnectRequest,
    GmailDisconnectResponse,
    GmailOAuthCallbackRequest,
    GmailOAuthStartResponse,
    GmailStatusResponse,
    GmailSyncTriggerResponse,
    SanitizedEmailConfirmationMetadata,
)
from app.services.gmail_oauth_service import GmailOAuthService, compute_session_binding_hash
from app.services.gmail_sync_service import GmailSyncService

router = APIRouter(prefix="/api/integrations/gmail", tags=["integrations"])
_bearer_scheme = HTTPBearer(auto_error=False)


def get_gmail_oauth_service() -> GmailOAuthService:
    return GmailOAuthService()


def get_gmail_sync_service() -> GmailSyncService:
    return GmailSyncService()


def _require_session_binding_hash(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> str:
    if credentials is None or not credentials.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
        )
    return compute_session_binding_hash(credentials.credentials)


@router.post("/connect/start", response_model=GmailOAuthStartResponse)
def start_gmail_connect_endpoint(
    current_user: User = Depends(get_current_user),
    session_binding_hash: str = Depends(_require_session_binding_hash),
    db: Session = Depends(get_db),
    service: GmailOAuthService = Depends(get_gmail_oauth_service),
) -> GmailOAuthStartResponse:
    """Start Google OAuth2 PKCE authorization flow for read-only Gmail access."""
    return service.start_connect(db, user=current_user, session_binding_hash=session_binding_hash)


@router.post("/callback", response_model=GmailConnectionSummary)
def complete_gmail_callback_endpoint(
    payload: GmailOAuthCallbackRequest,
    current_user: User = Depends(get_current_user),
    session_binding_hash: str = Depends(_require_session_binding_hash),
    db: Session = Depends(get_db),
    service: GmailOAuthService = Depends(get_gmail_oauth_service),
) -> GmailConnectionSummary:
    """Finalize Google OAuth2 callback forwarded by the authenticated Next.js BFF."""
    return service.complete_callback(
        db,
        user=current_user,
        session_binding_hash=session_binding_hash,
        code=payload.code,
        state=payload.state,
    )


@router.get("/status", response_model=GmailStatusResponse)
def get_gmail_status_endpoint(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    service: GmailOAuthService = Depends(get_gmail_oauth_service),
) -> GmailStatusResponse:
    """Return sanitized Gmail connection status for the authenticated user."""
    return service.get_status(db, user=current_user)


@router.post("/disconnect", response_model=GmailDisconnectResponse)
def disconnect_gmail_endpoint(
    payload: GmailDisconnectRequest | None = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    service: GmailOAuthService = Depends(get_gmail_oauth_service),
) -> GmailDisconnectResponse:
    """Revoke (best-effort) and disconnect Gmail OAuth credentials for the authenticated user."""
    connection_id = payload.connection_id if payload is not None else None
    return service.disconnect(db, user=current_user, connection_id=connection_id)


@router.post("/sync", response_model=GmailSyncTriggerResponse)
def sync_gmail_confirmations_endpoint(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    service: GmailSyncService = Depends(get_gmail_sync_service),
) -> GmailSyncTriggerResponse:
    """Trigger bounded read-only Gmail synchronization to verify pending applications."""
    return service.sync_user_connections(db, user=current_user)


@router.get("/events", response_model=list[SanitizedEmailConfirmationMetadata])
def list_gmail_confirmation_events_endpoint(
    application_id: uuid.UUID | None = Query(default=None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    service: GmailSyncService = Depends(get_gmail_sync_service),
) -> list[SanitizedEmailConfirmationMetadata]:
    """Return sanitized confirmation metadata for the authenticated user (no raw Gmail IDs)."""
    return service.list_user_confirmation_events(db, user=current_user, application_id=application_id)
