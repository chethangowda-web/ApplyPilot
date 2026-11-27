import base64
from datetime import datetime, timezone
import hashlib
import hmac
import json
import secrets
from typing import Any
from urllib.parse import urlencode
import uuid

from fastapi import HTTPException, status
import httpx
import redis
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.core.security import decrypt_oauth_token, encrypt_oauth_token
from app.database.models import OAuthConnection, User
from app.schemas.integrations import (
    GmailConnectionSummary,
    GmailDisconnectResponse,
    GmailOAuthStartResponse,
    GmailStatusResponse,
)
from app.services.audit_service import record_audit_event

logger = get_logger("applypilot.gmail_oauth")

GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
GOOGLE_OAUTH_SCOPES = (GMAIL_READONLY_SCOPE,)
ALLOWED_OAUTH_SCOPES = frozenset({GMAIL_READONLY_SCOPE})

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_REVOKE_URL = "https://oauth2.googleapis.com/revoke"
GMAIL_PROFILE_URL = "https://gmail.googleapis.com/gmail/v1/users/me/profile"

OAUTH_STATE_PREFIX = "applypilot:oauth:gmail:state:"
OAUTH_STATE_TTL_SECONDS = 600
OAUTH_HTTP_TIMEOUT_SECONDS = 10.0


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def compute_session_binding_hash(raw_token: str) -> str:
    """Hash the authenticated bearer token so Redis state binds to the initiating session without storing the JWT."""
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def compute_pkce_s256_challenge(code_verifier: str) -> str:
    """Compute RFC 7636 S256 code_challenge from a PKCE code_verifier."""
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


class GmailOAuthService:
    """Server-side Google OAuth2 PKCE + encrypted refresh-token persistence service."""

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        redis_client: Any | None = None,
        http_transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self._redis_client = redis_client
        self._http_transport = http_transport

    def _get_redis(self) -> Any:
        if self._redis_client is not None:
            return self._redis_client
        return redis.Redis.from_url(
            self.settings.redis_url,
            decode_responses=True,
            socket_connect_timeout=2,
            socket_timeout=2,
        )

    def _require_oauth_configured(self) -> None:
        if (
            not self.settings.google_client_id.strip()
            or not self.settings.google_client_secret.strip()
            or not self.settings.google_oauth_redirect_uri.strip()
        ):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Gmail OAuth2 client configuration is incomplete",
            )
        try:
            self.settings.validate_encryption_configured()
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Encryption key is not configured for OAuth credential storage",
            ) from exc

    def _consume_state_atomic(self, state: str) -> dict[str, Any]:
        key = f"{OAUTH_STATE_PREFIX}{state}"
        try:
            r = self._get_redis()
            if hasattr(r, "getdel"):
                raw = r.getdel(key)
            else:
                pipe = r.pipeline()
                pipe.get(key)
                pipe.delete(key)
                raw, _ = pipe.execute()
        except Exception as exc:
            logger.warning("gmail_oauth_redis_unavailable", extra={"extra_fields": {"error_type": type(exc).__name__}})
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="OAuth state store is temporarily unavailable",
            ) from exc

        if not raw:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid, expired, or already-used OAuth state",
            )
        try:
            parsed = json.loads(raw)
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Malformed OAuth state payload",
            ) from exc
        if not isinstance(parsed, dict):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Malformed OAuth state payload",
            )
        return parsed

    def start_connect(
        self,
        db: Session,
        *,
        user: User,
        session_binding_hash: str,
    ) -> GmailOAuthStartResponse:
        self._require_oauth_configured()

        state = secrets.token_urlsafe(32)
        code_verifier = secrets.token_urlsafe(48)
        code_challenge = compute_pkce_s256_challenge(code_verifier)

        state_payload = json.dumps(
            {
                "user_id": str(user.id),
                "session_binding_hash": session_binding_hash,
                "code_verifier": code_verifier,
                "redirect_uri": self.settings.google_oauth_redirect_uri.strip(),
                "created_at": utc_now().isoformat(),
            }
        )
        try:
            r = self._get_redis()
            r.set(f"{OAUTH_STATE_PREFIX}{state}", state_payload, ex=OAUTH_STATE_TTL_SECONDS)
        except Exception as exc:
            logger.warning("gmail_oauth_state_write_failed", extra={"extra_fields": {"error_type": type(exc).__name__}})
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="OAuth state store is temporarily unavailable",
            ) from exc

        query = urlencode(
            {
                "client_id": self.settings.google_client_id.strip(),
                "redirect_uri": self.settings.google_oauth_redirect_uri.strip(),
                "response_type": "code",
                "scope": " ".join(GOOGLE_OAUTH_SCOPES),
                "access_type": "offline",
                "prompt": "consent",
                "include_granted_scopes": "false",
                "state": state,
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
            }
        )
        record_audit_event(
            db,
            user_id=user.id,
            actor=f"user:{user.id}",
            action="oauth.connect_started",
            resource_type="oauth_connection",
            resource_id="google_gmail",
            metadata={"provider": "google_gmail", "scopes": list(GOOGLE_OAUTH_SCOPES)},
        )
        db.commit()
        return GmailOAuthStartResponse(
            authorization_url=f"{GOOGLE_AUTH_URL}?{query}",
            provider="google_gmail",
            scopes=list(GOOGLE_OAUTH_SCOPES),
        )

    def complete_callback(
        self,
        db: Session,
        *,
        user: User,
        session_binding_hash: str,
        code: str,
        state: str,
    ) -> GmailConnectionSummary:
        self._require_oauth_configured()

        state_data = self._consume_state_atomic(state)
        bound_user_id = str(state_data.get("user_id") or "")
        bound_session_hash = str(state_data.get("session_binding_hash") or "")
        code_verifier = str(state_data.get("code_verifier") or "")
        redirect_uri = str(state_data.get("redirect_uri") or "")

        if (
            bound_user_id != str(user.id)
            or not bound_session_hash
            or not hmac.compare_digest(bound_session_hash, session_binding_hash)
        ):
            record_audit_event(
                db,
                user_id=user.id,
                actor=f"user:{user.id}",
                action="oauth.callback_rejected",
                resource_type="oauth_connection",
                resource_id="google_gmail",
                metadata={"reason": "session_or_user_mismatch"},
            )
            db.commit()
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="OAuth callback does not match the initiating authenticated session",
            )

        if not code_verifier or redirect_uri != self.settings.google_oauth_redirect_uri.strip():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid OAuth state parameters",
            )

        client_kwargs: dict[str, Any] = {"timeout": OAUTH_HTTP_TIMEOUT_SECONDS}
        if self._http_transport is not None:
            client_kwargs["transport"] = self._http_transport

        try:
            with httpx.Client(**client_kwargs) as client:
                token_resp = client.post(
                    GOOGLE_TOKEN_URL,
                    data={
                        "client_id": self.settings.google_client_id.strip(),
                        "client_secret": self.settings.google_client_secret.strip(),
                        "code": code,
                        "code_verifier": code_verifier,
                        "grant_type": "authorization_code",
                        "redirect_uri": self.settings.google_oauth_redirect_uri.strip(),
                    },
                )
        except httpx.TimeoutException as exc:
            raise HTTPException(
                status_code=status.HTTP_504_GATEWAY_TIMEOUT,
                detail="Google OAuth token exchange timed out",
            ) from exc
        except httpx.HTTPError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Google OAuth token exchange failed",
            ) from exc

        if token_resp.status_code != 200:
            error_code = "token_exchange_failed"
            try:
                err_json = token_resp.json()
                if isinstance(err_json, dict) and isinstance(err_json.get("error"), str):
                    error_code = err_json["error"]
            except Exception:
                pass
            record_audit_event(
                db,
                user_id=user.id,
                actor=f"user:{user.id}",
                action="oauth.callback_rejected",
                resource_type="oauth_connection",
                resource_id="google_gmail",
                metadata={"reason": error_code},
            )
            db.commit()
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Google OAuth authorization rejected ({error_code})",
            )

        try:
            token_data = token_resp.json()
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Invalid token response from Google OAuth",
            ) from exc

        access_token = str(token_data.get("access_token") or "").strip()
        if not access_token:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Google OAuth response missing access token",
            )

        raw_scope = str(token_data.get("scope") or "").strip()
        granted_scopes = sorted({s.strip() for s in raw_scope.split() if s.strip()})
        if GMAIL_READONLY_SCOPE not in granted_scopes:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Required gmail.readonly scope was not granted",
            )
        unexpected_scopes = set(granted_scopes) - ALLOWED_OAUTH_SCOPES
        if unexpected_scopes:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Unexpected broader OAuth scopes were granted",
            )

        try:
            with httpx.Client(**client_kwargs) as client:
                profile_resp = client.get(
                    GMAIL_PROFILE_URL,
                    headers={"Authorization": f"Bearer {access_token}"},
                )
        except httpx.TimeoutException as exc:
            raise HTTPException(
                status_code=status.HTTP_504_GATEWAY_TIMEOUT,
                detail="Gmail profile lookup timed out",
            ) from exc
        except httpx.HTTPError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Gmail profile lookup failed",
            ) from exc

        if profile_resp.status_code != 200:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Unable to verify Gmail mailbox identity",
            )

        try:
            profile_data = profile_resp.json()
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Invalid Gmail profile payload",
            ) from exc

        mailbox_email = str(profile_data.get("emailAddress") or "").strip().lower()
        if not mailbox_email or "@" not in mailbox_email:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Gmail profile did not return a valid mailbox address",
            )

        existing = db.scalar(
            select(OAuthConnection).where(
                OAuthConnection.user_id == user.id,
                OAuthConnection.provider == "google_gmail",
                OAuthConnection.mailbox_email == mailbox_email,
            )
        )

        new_refresh_token = str(token_data.get("refresh_token") or "").strip()
        if new_refresh_token:
            encrypted_rt, key_ver = encrypt_oauth_token(
                new_refresh_token,
                user_id=str(user.id),
                provider="google_gmail",
                token_kind="refresh_token",
                settings=self.settings,
            )
        elif existing and existing.encrypted_refresh_token and existing.encryption_key_version:
            # Preserve existing encrypted refresh token when Google omits refresh_token on reconnect
            encrypted_rt = existing.encrypted_refresh_token
            key_ver = existing.encryption_key_version
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Google did not return a refresh token; revoke app access and reconnect with consent",
            )

        if existing is None:
            conn = OAuthConnection(
                user_id=user.id,
                provider="google_gmail",
                mailbox_email=mailbox_email,
                status="connected",
                encrypted_refresh_token=encrypted_rt,
                encryption_key_version=key_ver,
                granted_scopes=granted_scopes,
            )
            db.add(conn)
            db.flush()
        else:
            conn = existing
            conn.status = "connected"
            conn.encrypted_refresh_token = encrypted_rt
            conn.encryption_key_version = key_ver
            conn.granted_scopes = granted_scopes
            conn.revoked_at = None
            conn.disconnected_at = None
            conn.last_sync_error = None
            db.flush()

        record_audit_event(
            db,
            user_id=user.id,
            actor=f"user:{user.id}",
            action="oauth.connected",
            resource_type="oauth_connection",
            resource_id=str(conn.id),
            metadata={"provider": "google_gmail", "scopes": granted_scopes},
        )
        db.commit()
        db.refresh(conn)
        return GmailConnectionSummary.model_validate(conn)

    def get_status(self, db: Session, *, user: User) -> GmailStatusResponse:
        rows = list(
            db.scalars(
                select(OAuthConnection)
                .where(
                    OAuthConnection.user_id == user.id,
                    OAuthConnection.provider == "google_gmail",
                )
                .order_by(OAuthConnection.created_at.desc())
            )
        )
        return GmailStatusResponse(
            connected=any(row.status == "connected" for row in rows),
            connections=[GmailConnectionSummary.model_validate(row) for row in rows],
        )

    def disconnect(
        self,
        db: Session,
        *,
        user: User,
        connection_id: uuid.UUID | None = None,
    ) -> GmailDisconnectResponse:
        query = select(OAuthConnection).where(
            OAuthConnection.user_id == user.id,
            OAuthConnection.provider == "google_gmail",
        )
        if connection_id is not None:
            query = query.where(OAuthConnection.id == connection_id)
        else:
            query = query.order_by(OAuthConnection.created_at.desc())

        rows = list(db.scalars(query))
        if not rows:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Gmail OAuth connection not found",
            )

        target = rows[0]
        now = utc_now()
        if target.status == "disconnected" and target.encrypted_refresh_token is None:
            return GmailDisconnectResponse(
                connection_id=target.id,
                status="disconnected",
                disconnected_at=target.disconnected_at or now,
            )

        for conn in rows:
            if conn.encrypted_refresh_token and conn.encryption_key_version:
                try:
                    plaintext_rt = decrypt_oauth_token(
                        conn.encrypted_refresh_token,
                        key_version=conn.encryption_key_version,
                        user_id=str(user.id),
                        provider="google_gmail",
                        token_kind="refresh_token",
                        settings=self.settings,
                    )
                    client_kwargs: dict[str, Any] = {"timeout": OAUTH_HTTP_TIMEOUT_SECONDS}
                    if self._http_transport is not None:
                        client_kwargs["transport"] = self._http_transport
                    with httpx.Client(**client_kwargs) as client:
                        client.post(GOOGLE_REVOKE_URL, data={"token": plaintext_rt})
                except Exception as exc:
                    logger.warning(
                        "gmail_oauth_remote_revoke_skipped",
                        extra={"extra_fields": {"connection_id": str(conn.id), "error_type": type(exc).__name__}},
                    )

            conn.encrypted_refresh_token = None
            conn.encryption_key_version = None
            conn.last_history_id = None
            conn.status = "disconnected"
            conn.disconnected_at = now
            conn.last_sync_error = None

            record_audit_event(
                db,
                user_id=user.id,
                actor=f"user:{user.id}",
                action="oauth.disconnected",
                resource_type="oauth_connection",
                resource_id=str(conn.id),
                metadata={"provider": "google_gmail"},
            )

        db.commit()
        db.refresh(target)
        return GmailDisconnectResponse(
            connection_id=target.id,
            status="disconnected",
            disconnected_at=target.disconnected_at or now,
        )
