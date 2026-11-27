from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import re
from typing import Any
from urllib.parse import urlparse
import uuid

from app.database.models import Application, Job

RECOGNIZED_ATS_DOMAINS: frozenset[str] = frozenset(
    {
        "greenhouse.io",
        "lever.co",
        "ashbyhq.com",
        "myworkdayjobs.com",
        "workday.com",
        "smartrecruiters.com",
        "icims.com",
        "jobvite.com",
        "bamboohr.com",
        "taleo.net",
        "rippling.com",
    }
)

_BOUNCE_RE = re.compile(
    r"\b(mailer-daemon|delivery status notification|undeliverable|mail delivery failed|returned mail)\b",
    re.IGNORECASE,
)
_REJECTION_RE = re.compile(
    r"\b(unfortunately|not moving forward|other candidates|we regret to inform|position has been filled|not selected)\b",
    re.IGNORECASE,
)
_NEWSLETTER_RE = re.compile(
    r"\b(unsubscribe from this list|weekly job digest|recommended jobs for you|job alert)\b",
    re.IGNORECASE,
)
_CONFIRMATION_PHRASE_RE = re.compile(
    r"\b("
    r"thank you for applying|thanks for applying|thanks for your application|"
    r"application received|we(?:'ve| have) received your application|"
    r"your application (?:for|to) .+ (?:has been )?(?:received|submitted)|"
    r"application confirmation|successfully submitted your application"
    r")\b",
    re.IGNORECASE,
)
_AUTH_PASS_RE = re.compile(r"\b(dkim|spf)=pass\b", re.IGNORECASE)
_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _registrable_domain(host_or_domain: str) -> str:
    host = (host_or_domain or "").strip().lower().split(":")[0]
    parts = [p for p in host.split(".") if p]
    if len(parts) >= 2:
        return ".".join(parts[-2:])
    return host


def _extract_domain_from_url(url: str) -> str:
    try:
        return _registrable_domain(urlparse(url).hostname or "")
    except Exception:
        return ""


def _normalize_tokens(text: str) -> list[str]:
    return [t for t in _TOKEN_RE.findall((text or "").lower()) if len(t) >= 2]


@dataclass(frozen=True)
class ParsedEmailCandidate:
    gmail_message_id: str
    gmail_thread_id: str | None
    sender_domain: str
    subject: str
    snippet: str
    received_at: datetime
    auth_results_header: str = ""


@dataclass(frozen=True)
class EmailMatchDecision:
    match_status: str  # matched | ambiguous | rejected | unmatched
    matched_application_id: uuid.UUID | None = None
    score: int = 0
    match_signals: dict[str, Any] = field(default_factory=dict)


def _ensure_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def match_confirmation_email(
    email: ParsedEmailCandidate,
    *,
    candidates: list[tuple[Application, Job]],
    confirmation_window_days: int = 14,
    now: datetime | None = None,
) -> EmailMatchDecision:
    """Deterministically match an incoming email against candidate applications awaiting confirmation."""
    current_time = _ensure_utc(now or datetime.now(timezone.utc))
    received_at = _ensure_utc(email.received_at)
    combined_text = f"{email.subject}\n{email.snippet}".strip()
    sender_reg_domain = _registrable_domain(email.sender_domain)

    if _BOUNCE_RE.search(combined_text) or "postmaster" in email.sender_domain.lower():
        return EmailMatchDecision(
            match_status="rejected",
            match_signals={"disqualifier": "bounce_or_delivery_failure"},
        )
    if _REJECTION_RE.search(combined_text):
        return EmailMatchDecision(
            match_status="rejected",
            match_signals={"disqualifier": "rejection_language"},
        )
    if _NEWSLETTER_RE.search(combined_text):
        return EmailMatchDecision(
            match_status="unmatched",
            match_signals={"disqualifier": "newsletter_or_alert"},
        )

    phrase_match = bool(_CONFIRMATION_PHRASE_RE.search(combined_text))
    if not phrase_match:
        return EmailMatchDecision(
            match_status="unmatched",
            match_signals={"confirmation_phrase_match": False},
        )

    if (current_time - received_at) > timedelta(days=confirmation_window_days):
        return EmailMatchDecision(
            match_status="unmatched",
            match_signals={"confirmation_phrase_match": True, "within_window": False},
        )

    auth_pass = bool(_AUTH_PASS_RE.search(email.auth_results_header or ""))
    is_ats_domain = sender_reg_domain in RECOGNIZED_ATS_DOMAINS
    text_tokens = set(_normalize_tokens(combined_text))
    combined_lower = combined_text.lower()

    scored: list[tuple[int, Application, dict[str, Any]]] = []
    for app, job in candidates:
        if app.status not in {"USER_REPORTED_SUBMITTED", "UNVERIFIED", "SUBMITTED"}:
            continue
        ref_time = _ensure_utc(app.submitted_at or app.updated_at or app.created_at)
        if received_at < (ref_time - timedelta(minutes=5)) or received_at > (current_time + timedelta(minutes=5)):
            continue

        dest_domain = _extract_domain_from_url(app.destination_url)
        domain_match = bool(dest_domain and sender_reg_domain and dest_domain == sender_reg_domain)

        company_tokens = [t for t in _normalize_tokens(job.company) if t not in {"inc", "llc", "ltd", "corp"}]
        company_match = bool(company_tokens) and all(t in text_tokens for t in company_tokens)

        role_tokens = [
            t
            for t in _normalize_tokens(job.role_title)
            if t not in {"engineer", "developer", "intern", "junior", "senior", "and", "the", "for"}
        ]
        if not role_tokens:
            role_tokens = _normalize_tokens(job.role_title)
        role_hits = sum(1 for t in role_tokens if t in text_tokens)
        role_match = bool(role_tokens) and role_hits >= min(2, len(role_tokens))

        provenance = job.source_provenance if isinstance(job.source_provenance, dict) else {}
        req_id = str(
            provenance.get("requisition_id")
            or provenance.get("req_id")
            or provenance.get("employer_requisition_id")
            or job.source_job_id
            or ""
        ).strip()
        req_id_match = bool(len(req_id) >= 4 and req_id.lower() in combined_lower)

        # Require a strong employer/ATS/requisition anchor alongside confirmation language
        if not (domain_match or (is_ats_domain and company_match) or (company_match and req_id_match)):
            continue

        score = 30  # confirmation phrase + valid time window
        if domain_match:
            score += 30
        elif is_ats_domain:
            score += 20
        if company_match:
            score += 20
        if role_match:
            score += 15
        if req_id_match:
            score += 25
        if auth_pass:
            score += 10

        signals = {
            "confirmation_phrase_match": True,
            "within_window": True,
            "domain_match": domain_match,
            "ats_domain_match": is_ats_domain,
            "company_match": company_match,
            "role_match": role_match,
            "req_id_match": req_id_match,
            "auth_pass": auth_pass,
            "score": score,
        }
        scored.append((score, app, signals))

    if not scored:
        return EmailMatchDecision(
            match_status="unmatched",
            match_signals={"confirmation_phrase_match": True, "candidate_matched": False},
        )

    scored.sort(key=lambda item: item[0], reverse=True)
    if len(scored) >= 2 and (scored[0][0] - scored[1][0]) < 15:
        return EmailMatchDecision(
            match_status="ambiguous",
            matched_application_id=None,
            score=scored[0][0],
            match_signals={**scored[0][2], "ambiguous_candidate_count": len(scored)},
        )

    top_score, top_app, top_signals = scored[0]
    return EmailMatchDecision(
        match_status="matched",
        matched_application_id=top_app.id,
        score=top_score,
        match_signals=top_signals,
    )
