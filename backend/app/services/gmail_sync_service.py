from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import re
from typing import Any
import uuid

from fastapi import HTTPException, status
import httpx
import redis
from sqlalchemy import select
from sqlalchemy.orm import Session
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.core.security import decrypt_oauth_token, encrypt_oauth_token
from app.database.models import Application, EmailConfirmationEvent, Job, OAuthConnection, User
from app.schemas.integrations import GmailSyncTriggerResponse, SanitizedEmailConfirmationMetadata
from app.services.application_policy_service import transition_application_status
from app.services.audit_service import record_audit_event
from app.services.email_confirmation_matcher import ParsedEmailCandidate, match_confirmation_email
from app.services.gmail_confirmation_verifier import register_gmail_confirmation_verifier
from app.services.gmail_oauth_service import GOOGLE_TOKEN_URL

logger = get_logger("applypilot.gmail_sync")

GMAIL_MESSAGES_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages"
GMAIL_HISTORY_URL = "https://gmail.googleapis.com/gmail/v1/users/me/history"
GMAIL_HTTP_TIMEOUT_SECONDS = 15.0
SYNC_LOCK_PREFIX = "applypilot:oauth:gmail:sync_lock:"
SYNC_LOCK_TTL_SECONDS = 300
_FROM_EMAIL_RE = re.compile(r"<([^>]+)>|([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})")


class _GmailRateLimitError(RuntimeError):
    """Transient 429/5xx error eligible for bounded exponential backoff."""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _extract_sender_domain(from_header: str) -> str:
    m = _FROM_EMAIL_RE.search(from_header or "")
    email_addr = (m.group(1) or m.group(2) or "").strip().lower() if m else ""
    if "@" in email_addr:
        return email_addr.split("@", 1)[1]
    return "unknown"


def _parse_message_date(date_header: str, internal_date_ms: str | None) -> datetime:
    if date_header:
        try:
            dt = parsedate_to_datetime(date_header)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except Exception:
            pass
    if internal_date_ms and str(internal_date_ms).isdigit():
        return datetime.fromtimestamp(int(internal_date_ms) / 1000.0, tz=timezone.utc)
    return _utc_now()


class GmailSyncService:
    """Read-only Gmail synchronization and deterministic application confirmation service."""

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

    def _acquire_lock(self, connection_id: uuid.UUID) -> bool:
        try:
            r = self._get_redis()
            return bool(r.set(f"{SYNC_LOCK_PREFIX}{connection_id}", "1", nx=True, ex=SYNC_LOCK_TTL_SECONDS))
        except Exception:
            # If Redis is unavailable during manual/test sync, allow DB-level uniqueness to guard idempotency
            return True

    def _release_lock(self, connection_id: uuid.UUID) -> None:
        try:
            self._get_redis().delete(f"{SYNC_LOCK_PREFIX}{connection_id}")
        except Exception:
            pass

    def _make_client(self) -> httpx.Client:
        kwargs: dict[str, Any] = {"timeout": GMAIL_HTTP_TIMEOUT_SECONDS}
        if self._http_transport is not None:
            kwargs["transport"] = self._http_transport
        return httpx.Client(**kwargs)

    @retry(
        retry=retry_if_exception_type(_GmailRateLimitError),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=4),
        reraise=True,
    )
    def _gmail_get(self, client: httpx.Client, url: str, *, access_token: str, params: dict[str, Any] | None = None) -> httpx.Response:
        resp = client.get(
            url,
            headers={"Authorization": f"Bearer {access_token}"},
            params=params,
        )
        if resp.status_code in {429, 500, 502, 503, 504}:
            raise _GmailRateLimitError(f"Transient Gmail API status {resp.status_code}")
        return resp

    def _refresh_access_token(self, db: Session, conn: OAuthConnection) -> str | None:
        if not conn.encrypted_refresh_token or not conn.encryption_key_version:
            return None
        refresh_token = decrypt_oauth_token(
            conn.encrypted_refresh_token,
            key_version=conn.encryption_key_version,
            user_id=str(conn.user_id),
            provider=conn.provider,
            token_kind="refresh_token",
            settings=self.settings,
        )
        with self._make_client() as client:
            resp = client.post(
                GOOGLE_TOKEN_URL,
                data={
                    "client_id": self.settings.google_client_id.strip(),
                    "client_secret": self.settings.google_client_secret.strip(),
                    "refresh_token": refresh_token,
                    "grant_type": "refresh_token",
                },
            )
        if resp.status_code != 200:
            err_code = ""
            try:
                err_code = str(resp.json().get("error") or "")
            except Exception:
                pass
            if resp.status_code in {400, 401} and err_code in {"invalid_grant", "unauthorized_client", ""}:
                conn.status = "revoked"
                conn.revoked_at = _utc_now()
                conn.encrypted_refresh_token = None
                conn.encryption_key_version = None
                conn.last_sync_status = "failed"
                conn.last_sync_error = "Gmail authorization was revoked or expired; please reconnect."
                record_audit_event(
                    db,
                    user_id=conn.user_id,
                    actor="system:gmail_sync",
                    action="oauth.revoked_detected",
                    resource_type="oauth_connection",
                    resource_id=str(conn.id),
                    metadata={"provider": conn.provider},
                )
                db.commit()
                return None
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Failed to refresh Gmail access token",
            )

        data = resp.json()
        access_token = str(data.get("access_token") or "").strip()
        rotated_rt = str(data.get("refresh_token") or "").strip()
        if rotated_rt:
            enc_rt, key_ver = encrypt_oauth_token(
                rotated_rt,
                user_id=str(conn.user_id),
                provider=conn.provider,
                token_kind="refresh_token",
                settings=self.settings,
            )
            conn.encrypted_refresh_token = enc_rt
            conn.encryption_key_version = key_ver
        return access_token or None

    def _collect_message_ids(
        self,
        client: httpx.Client,
        *,
        access_token: str,
        conn: OAuthConnection,
    ) -> tuple[list[str], str | None]:
        max_msgs = self.settings.gmail_sync_max_messages
        if conn.last_history_id:
            msg_ids: list[str] = []
            page_token: str | None = None
            latest_history_id: str | None = conn.last_history_id
            history_expired = False
            while len(msg_ids) < max_msgs:
                params: dict[str, Any] = {
                    "startHistoryId": conn.last_history_id,
                    "historyTypes": "messageAdded",
                }
                if page_token:
                    params["pageToken"] = page_token
                resp = self._gmail_get(client, GMAIL_HISTORY_URL, access_token=access_token, params=params)
                if resp.status_code == 404:
                    history_expired = True
                    break
                if resp.status_code != 200:
                    raise HTTPException(status_code=502, detail="Gmail history sync failed")
                payload = resp.json()
                if payload.get("historyId"):
                    latest_history_id = str(payload["historyId"])
                for h in payload.get("history") or []:
                    for added in h.get("messagesAdded") or []:
                        mid = str((added.get("message") or {}).get("id") or "").strip()
                        if mid and mid not in msg_ids:
                            msg_ids.append(mid)
                page_token = payload.get("nextPageToken")
                if not page_token:
                    break
            if not history_expired:
                return msg_ids[:max_msgs], latest_history_id

        # Bounded initial or fallback discovery
        msg_ids = []
        page_token = None
        query = f"newer_than:{self.settings.gmail_confirmation_window_days}d"
        while len(msg_ids) < max_msgs:
            params = {"q": query, "maxResults": min(50, max_msgs - len(msg_ids))}
            if page_token:
                params["pageToken"] = page_token
            resp = self._gmail_get(client, GMAIL_MESSAGES_URL, access_token=access_token, params=params)
            if resp.status_code != 200:
                raise HTTPException(status_code=502, detail="Gmail message discovery failed")
            payload = resp.json()
            for item in payload.get("messages") or []:
                mid = str(item.get("id") or "").strip()
                if mid and mid not in msg_ids:
                    msg_ids.append(mid)
            page_token = payload.get("nextPageToken")
            if not page_token:
                break
        return msg_ids[:max_msgs], None

    def sync_connection(self, db: Session, *, conn: OAuthConnection) -> dict[str, int]:
        """Synchronize one connected Gmail mailbox and confirm matched applications."""
        register_gmail_confirmation_verifier()
        if conn.status != "connected":
            return {"processed": 0, "confirmed": 0, "ambiguous": 0}

        # Only synchronize mailboxes that have applications awaiting confirmation
        candidate_rows = list(
            db.execute(
                select(Application, Job)
                .join(Job, Job.id == Application.job_id)
                .where(
                    Application.user_id == conn.user_id,
                    Application.deleted_at.is_(None),
                    Application.status.in_(["USER_REPORTED_SUBMITTED", "UNVERIFIED", "SUBMITTED"]),
                )
            ).all()
        )
        if not candidate_rows:
            conn.last_synced_at = _utc_now()
            conn.last_sync_status = "succeeded"
            conn.last_sync_error = None
            db.commit()
            return {"processed": 0, "confirmed": 0, "ambiguous": 0}

        if not self._acquire_lock(conn.id):
            return {"processed": 0, "confirmed": 0, "ambiguous": 0}

        processed = 0
        confirmed = 0
        ambiguous = 0
        try:
            conn.last_sync_status = "running"
            db.commit()

            access_token = self._refresh_access_token(db, conn)
            if not access_token:
                return {"processed": 0, "confirmed": 0, "ambiguous": 0}

            with self._make_client() as client:
                msg_ids, next_history_id = self._collect_message_ids(
                    client, access_token=access_token, conn=conn
                )
                max_history_seen = next_history_id or conn.last_history_id

                for mid in msg_ids:
                    existing = db.scalar(
                        select(EmailConfirmationEvent).where(
                            EmailConfirmationEvent.oauth_connection_id == conn.id,
                            EmailConfirmationEvent.gmail_message_id == mid,
                        )
                    )
                    if existing is not None:
                        continue

                    msg_resp = self._gmail_get(
                        client,
                        f"{GMAIL_MESSAGES_URL}/{mid}",
                        access_token=access_token,
                        params={
                            "format": "metadata",
                            "metadataHeaders": ["From", "Subject", "Date", "Authentication-Results"],
                        },
                    )
                    if msg_resp.status_code != 200:
                        continue
                    msg_json = msg_resp.json()
                    if msg_json.get("historyId"):
                        hid = str(msg_json["historyId"])
                        if not max_history_seen or (hid.isdigit() and max_history_seen.isdigit() and int(hid) > int(max_history_seen)):
                            max_history_seen = hid

                    headers_list = (msg_json.get("payload") or {}).get("headers") or []
                    hdr_map = {
                        str(h.get("name") or "").lower(): str(h.get("value") or "")
                        for h in headers_list
                        if isinstance(h, dict)
                    }
                    candidate = ParsedEmailCandidate(
                        gmail_message_id=mid,
                        gmail_thread_id=str(msg_json.get("threadId") or "") or None,
                        sender_domain=_extract_sender_domain(hdr_map.get("from", "")),
                        subject=hdr_map.get("subject", ""),
                        snippet=str(msg_json.get("snippet") or ""),
                        received_at=_parse_message_date(hdr_map.get("date", ""), msg_json.get("internalDate")),
                        auth_results_header=hdr_map.get("authentication-results", ""),
                    )

                    decision = match_confirmation_email(
                        candidate,
                        candidates=candidate_rows,
                        confirmation_window_days=self.settings.gmail_confirmation_window_days,
                    )
                    ev = EmailConfirmationEvent(
                        oauth_connection_id=conn.id,
                        user_id=conn.user_id,
                        gmail_message_id=candidate.gmail_message_id,
                        gmail_thread_id=candidate.gmail_thread_id,
                        sender_domain=candidate.sender_domain[:255],
                        received_at=candidate.received_at,
                        match_status=decision.match_status,
                        matched_application_id=decision.matched_application_id,
                        match_signals=decision.match_signals,
                    )
                    db.add(ev)
                    db.flush()
                    processed += 1

                    if decision.match_status == "ambiguous":
                        ambiguous += 1
                    elif decision.match_status == "matched" and decision.matched_application_id is not None:
                        target_app = db.scalar(
                            select(Application).where(
                                Application.id == decision.matched_application_id,
                                Application.user_id == conn.user_id,
                            )
                        )
                        if target_app is not None and target_app.status != "CONFIRMED":
                            if target_app.status == "USER_REPORTED_SUBMITTED":
                                transition_application_status(
                                    db,
                                    application=target_app,
                                    next_status="UNVERIFIED",
                                    actor="system:gmail_sync",
                                )
                            transition_application_status(
                                db,
                                application=target_app,
                                next_status="CONFIRMED",
                                actor="system:gmail_sync",
                                confirmation_evidence={"email_confirmation_event_id": str(ev.id)},
                            )
                            ev.match_status = "confirmed"
                            confirmed += 1

            conn.last_history_id = max_history_seen
            conn.last_synced_at = _utc_now()
            conn.last_sync_status = "succeeded"
            conn.last_sync_error = None
            record_audit_event(
                db,
                user_id=conn.user_id,
                actor="system:gmail_sync",
                action="oauth.gmail_synced",
                resource_type="oauth_connection",
                resource_id=str(conn.id),
                metadata={"processed": processed, "confirmed": confirmed, "ambiguous": ambiguous},
            )
            db.commit()
            return {"processed": processed, "confirmed": confirmed, "ambiguous": ambiguous}
        except Exception as exc:
            conn.last_sync_status = "failed"
            conn.last_sync_error = type(exc).__name__
            db.commit()
            raise
        finally:
            self._release_lock(conn.id)

    def sync_user_connections(self, db: Session, *, user: User) -> GmailSyncTriggerResponse:
        conns = list(
            db.scalars(
                select(OAuthConnection).where(
                    OAuthConnection.user_id == user.id,
                    OAuthConnection.provider == "google_gmail",
                    OAuthConnection.status == "connected",
                )
            )
        )
        if not conns:
            return GmailSyncTriggerResponse(
                status="no_active_connection",
                connections_queued=0,
                message="No connected Gmail mailbox found.",
            )
        total_confirmed = 0
        for conn in conns:
            res = self.sync_connection(db, conn=conn)
            total_confirmed += res["confirmed"]
        return GmailSyncTriggerResponse(
            status="completed",
            connections_queued=len(conns),
            message=f"Checked {len(conns)} mailbox(es); confirmed {total_confirmed} application(s).",
        )

    def list_user_confirmation_events(
        self,
        db: Session,
        *,
        user: User,
        application_id: uuid.UUID | None = None,
    ) -> list[SanitizedEmailConfirmationMetadata]:
        query = select(EmailConfirmationEvent).where(EmailConfirmationEvent.user_id == user.id)
        if application_id is not None:
            query = query.where(EmailConfirmationEvent.matched_application_id == application_id)
        rows = list(db.scalars(query.order_by(EmailConfirmationEvent.received_at.desc()).limit(50)))
        return [SanitizedEmailConfirmationMetadata.model_validate(r) for r in rows]
