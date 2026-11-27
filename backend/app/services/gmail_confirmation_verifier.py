from datetime import timezone
from typing import Any
import uuid

from sqlalchemy import select
from sqlalchemy.orm import object_session

from app.database.models import Application, EmailConfirmationEvent, OAuthConnection
from app.services import application_policy_service as policy_service
from app.services.application_policy_service import (
    ConfirmationProofIssuer,
    VerifiedConfirmationProof,
)


class GmailConfirmationVerifier:
    """Trusted server-side verifier backed by persisted Gmail sync EmailConfirmationEvent records."""

    verifier_id: str = "gmail_oauth_confirmation_v1"
    supported_modes: frozenset[str] = frozenset({"official_api", "human_approved_browser", "manual"})

    def verify(
        self,
        *,
        application: Application,
        evidence: dict[str, Any],
        issuer: ConfirmationProofIssuer,
    ) -> VerifiedConfirmationProof:
        db = object_session(application)
        if db is None:
            raise ValueError("GmailConfirmationVerifier requires an active database session")

        raw_event_id = evidence.get("email_confirmation_event_id")
        if not raw_event_id:
            raise ValueError("Gmail confirmation requires a server-persisted email_confirmation_event_id")

        try:
            event_id = uuid.UUID(str(raw_event_id))
        except Exception as exc:
            raise ValueError("Invalid email_confirmation_event_id") from exc

        event = db.scalar(
            select(EmailConfirmationEvent).where(
                EmailConfirmationEvent.id == event_id,
                EmailConfirmationEvent.user_id == application.user_id,
                EmailConfirmationEvent.matched_application_id == application.id,
            )
        )
        if event is None:
            raise ValueError("No matching server-verified EmailConfirmationEvent found for this application")

        conn = db.scalar(
            select(OAuthConnection).where(
                OAuthConnection.id == event.oauth_connection_id,
                OAuthConnection.user_id == application.user_id,
                OAuthConnection.provider == "google_gmail",
            )
        )
        if conn is None:
            raise ValueError("EmailConfirmationEvent is not bound to an owned Gmail OAuthConnection")

        if event.match_status not in {"matched", "confirmed"}:
            raise ValueError(f"EmailConfirmationEvent status '{event.match_status}' cannot establish CONFIRMED")

        if not (event.gmail_message_id or "").strip():
            raise ValueError("EmailConfirmationEvent is missing internal Gmail message identifier")

        signals = event.match_signals if isinstance(event.match_signals, dict) else {}
        if not signals.get("confirmation_phrase_match"):
            raise ValueError("EmailConfirmationEvent lacks deterministic confirmation language signal")

        received_at = event.received_at
        if received_at.tzinfo is None:
            received_at = received_at.replace(tzinfo=timezone.utc)

        # Sanitized public confirmation metadata only (never expose gmail_message_id / gmail_thread_id)
        sanitized_evidence = {
            "email_confirmation_event_id": str(event.id),
            "sender_domain": event.sender_domain,
            "received_at": received_at.isoformat(),
            "match_signals": signals,
        }
        return issuer.issue(
            application=application,
            verification_source="verified_email_confirmation",
            external_ref_id=f"gmail_event:{event.id}",
            confirmed_at=received_at,
            raw_evidence=sanitized_evidence,
        )


def register_gmail_confirmation_verifier() -> GmailConfirmationVerifier:
    """Deterministically register GmailConfirmationVerifier in the server-owned registry."""
    existing = policy_service._CONFIRMATION_VERIFIER_REGISTRY.get(GmailConfirmationVerifier.verifier_id)
    if isinstance(existing, GmailConfirmationVerifier):
        return existing
    verifier = GmailConfirmationVerifier()
    policy_service._CONFIRMATION_VERIFIER_REGISTRY[verifier.verifier_id] = verifier
    return verifier
