from datetime import datetime
import uuid
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field

OAuthProvider = Literal["google_gmail"]
OAuthConnectionStatus = Literal["connected", "revoked", "expired", "disconnected", "error"]
OAuthSyncStatus = Literal["idle", "running", "succeeded", "failed"]
EmailMatchStatus = Literal["matched", "confirmed", "ambiguous", "rejected", "unmatched"]


class GmailOAuthStartResponse(BaseModel):
    """Server-generated Google OAuth2 authorization URL (never exposes client secret or state store)."""

    model_config = ConfigDict(extra="forbid")

    authorization_url: str
    provider: OAuthProvider = "google_gmail"
    scopes: list[str] = Field(default_factory=list)


class GmailOAuthCallbackRequest(BaseModel):
    """Server-to-server OAuth callback payload forwarded by the authenticated BFF."""

    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=1, max_length=2048)
    state: str = Field(min_length=16, max_length=512)


class GmailDisconnectRequest(BaseModel):
    """Optional target connection ID for disconnecting Gmail."""

    model_config = ConfigDict(extra="forbid")

    connection_id: uuid.UUID | None = None


class GmailConnectionSummary(BaseModel):
    """Sanitized OAuth connection status for frontend display (never exposes tokens or history cursors)."""

    model_config = ConfigDict(from_attributes=True, extra="ignore")

    id: uuid.UUID
    provider: str
    mailbox_email: str
    status: OAuthConnectionStatus
    granted_scopes: list[str] = Field(default_factory=list)
    last_synced_at: datetime | None = None
    last_sync_status: OAuthSyncStatus | None = None
    last_sync_error: str | None = None
    revoked_at: datetime | None = None
    disconnected_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class GmailStatusResponse(BaseModel):
    """Connected Gmail mailbox summary for the authenticated user."""

    model_config = ConfigDict(extra="forbid")

    connected: bool
    connections: list[GmailConnectionSummary] = Field(default_factory=list)


class GmailDisconnectResponse(BaseModel):
    """Response returned after disconnecting a Gmail OAuth connection."""

    model_config = ConfigDict(extra="forbid")

    connection_id: uuid.UUID
    status: OAuthConnectionStatus
    disconnected_at: datetime


class GmailSyncTriggerResponse(BaseModel):
    """Response returned when triggering a Gmail confirmation sync."""

    model_config = ConfigDict(extra="forbid")

    status: str
    connections_queued: int = Field(ge=0)
    message: str


class SanitizedEmailConfirmationMetadata(BaseModel):
    """Minimal auditable confirmation metadata safe for frontend display (omits raw Gmail IDs and bodies)."""

    model_config = ConfigDict(from_attributes=True, extra="ignore")

    id: uuid.UUID
    sender_domain: str
    received_at: datetime
    match_status: EmailMatchStatus
    matched_application_id: uuid.UUID | None = None
    match_signals: dict[str, Any] = Field(default_factory=dict)
    processed_at: datetime
