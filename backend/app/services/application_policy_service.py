from dataclasses import dataclass, field
from datetime import datetime, time, timezone
import hashlib
import hmac
import re
import secrets
import uuid
from typing import Any, Literal, Protocol
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.agents.application.form_understanding import FormUnderstandingAgent
from app.core.config import Settings, get_settings
from app.core.security import encrypt_sensitive_answer
from app.database.models import (
    Application,
    ApplicationAnswer,
    CandidateProfile,
    Job,
    JobMatch,
    Resume,
    ResumeJobMapping,
    User,
)
from app.schemas.applications import (
    ApplicationMode,
    ApplicationPreparationResult,
    PolicyDecision,
    validate_application_transition,
)
from app.services.audit_service import record_audit_event
from app.services.deduplication_service import compute_dedup_key, normalize_canonical_url


class DuplicateApplicationError(ValueError):
    """Raised when an active application already exists for (user_id, canonical_job_key)."""


class PolicyViolationError(ValueError):
    """Raised when an application fails ApplicationPolicyRouter checks."""


_LOC_PUNCT_RE = re.compile(r"[^a-z0-9\s]+")
_TRAILING_COUNTRY_RE = re.compile(r"\b(?:india|usa|united states|uk|united kingdom)\b$")

TRUSTED_CONFIRMATION_SOURCES: frozenset[str] = frozenset(
    {
        "official_api_receipt",
        "verified_email_confirmation",
        "ats_webhook_receipt",
    }
)


def _normalize_token(value: str | None) -> str:
    cleaned = _LOC_PUNCT_RE.sub(" ", (value or "").lower())
    return " ".join(cleaned.split())


def normalize_job_location(
    location: str | None,
    *,
    is_remote: bool | None = None,
    remote_policy: str | None = None,
) -> tuple[str, str]:
    """Normalize location text and remote scope without merging distinct cities or arrangements."""
    norm_loc = _normalize_token(location)
    # Strip trailing country suffix only when a city/region token precedes it.
    stripped = _TRAILING_COUNTRY_RE.sub("", norm_loc).strip()
    if stripped:
        norm_loc = stripped

    policy = (remote_policy or "unknown").strip().lower()
    if is_remote is True or policy == "remote" or norm_loc == "remote":
        remote_scope = "remote"
        if norm_loc == "remote":
            norm_loc = ""
    elif policy in {"hybrid", "onsite", "on_site", "on-site"}:
        remote_scope = "onsite" if policy in {"onsite", "on_site", "on-site"} else "hybrid"
    else:
        remote_scope = "unknown"

    return norm_loc, remote_scope


def compute_canonical_job_key(job: Job) -> str:
    """Compute a deterministic cross-source canonical job identity.

    1. Prefers an employer-scoped external requisition identifier when present in
       `job.source_provenance` (`requisition_id`, `req_id`, or `employer_requisition_id`).
    2. Otherwise uses normalized employer, role, location, remote scope, and optional
       posting/team discriminator from `source_provenance`.
    """
    norm_company = _normalize_token(job.company)
    provenance = job.source_provenance if isinstance(job.source_provenance, dict) else {}
    req_id = str(
        provenance.get("requisition_id")
        or provenance.get("req_id")
        or provenance.get("employer_requisition_id")
        or ""
    ).strip().lower()

    if req_id:
        base = f"canon-v2|req|{norm_company}|{req_id}"
        return hashlib.sha256(base.encode("utf-8")).hexdigest()

    dedup = (job.dedup_key or "").strip()
    if not dedup:
        dedup = compute_dedup_key(job.company, job.role_title)
    norm_loc, remote_scope = normalize_job_location(
        job.location,
        is_remote=job.is_remote,
        remote_policy=job.remote_policy,
    )
    team_disc = _normalize_token(str(provenance.get("team") or ""))
    base = f"canon-v2|role|{dedup}|{norm_loc}|{remote_scope}|{team_disc}"
    return hashlib.sha256(base.encode("utf-8")).hexdigest()


def compute_application_idempotency_key(
    *,
    user_id: uuid.UUID,
    canonical_job_key: str,
    resume_id: uuid.UUID,
) -> str:
    """Deterministic SHA-256 idempotency key for preparing an application."""
    payload = f"{user_id}:{canonical_job_key}:{resume_id}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _today_utc_bounds(tz_name: str) -> tuple[datetime, datetime]:
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = timezone.utc
    now_local = datetime.now(tz)
    start_local = datetime.combine(now_local.date(), time.min, tzinfo=tz)
    end_local = datetime.combine(now_local.date(), time.max, tzinfo=tz)
    return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)


_PROOF_SEAL_KEY: bytes = secrets.token_bytes(32)
_SERVER_PROOF_CAPABILITY: object = object()


def _compute_proof_mac(
    *,
    application_id: uuid.UUID,
    verification_source: str,
    external_ref_id: str,
    confirmed_at: datetime,
    verifier_id: str,
) -> str:
    payload = (
        f"{application_id}|{verification_source}|{external_ref_id}|"
        f"{confirmed_at.isoformat()}|{verifier_id}"
    ).encode("utf-8")
    return hmac.new(_PROOF_SEAL_KEY, payload, hashlib.sha256).hexdigest()


@dataclass(frozen=True)
class VerifiedConfirmationProof:
    """HMAC-sealed proof that can only be minted via the server-owned ConfirmationProofIssuer."""

    application_id: uuid.UUID
    verification_source: str
    external_ref_id: str
    confirmed_at: datetime
    verifier_id: str
    raw_evidence: dict[str, Any]
    _seal_mac: str = field(default="", repr=False, compare=False)

    def __post_init__(self) -> None:
        if not self._seal_mac or self.confirmed_at.tzinfo is None:
            raise ValueError("VerifiedConfirmationProof cannot be constructed without server proof issuance")
        expected = _compute_proof_mac(
            application_id=self.application_id,
            verification_source=self.verification_source,
            external_ref_id=self.external_ref_id,
            confirmed_at=self.confirmed_at,
            verifier_id=self.verifier_id,
        )
        if not hmac.compare_digest(self._seal_mac, expected):
            raise ValueError("Invalid VerifiedConfirmationProof seal")


class ConfirmationProofIssuer:
    """Server-side capability granted only by the server verifier registry."""

    def __init__(self, verifier_id: str, *, _capability: object) -> None:
        if _capability is not _SERVER_PROOF_CAPABILITY:
            raise PermissionError("ConfirmationProofIssuer cannot be instantiated outside server registry")
        self._verifier_id = verifier_id

    def issue(
        self,
        *,
        application: Application,
        verification_source: str,
        external_ref_id: str,
        confirmed_at: datetime,
        raw_evidence: dict[str, Any],
    ) -> VerifiedConfirmationProof:
        if verification_source not in TRUSTED_CONFIRMATION_SOURCES:
            raise ValueError(f"Untrusted confirmation verification_source: {verification_source}")
        if not external_ref_id.strip():
            raise ValueError("Confirmation proof requires a non-empty external_ref_id")
        if confirmed_at.tzinfo is None:
            raise ValueError("Confirmation proof requires a timezone-aware confirmed_at timestamp")

        mac = _compute_proof_mac(
            application_id=application.id,
            verification_source=verification_source,
            external_ref_id=external_ref_id,
            confirmed_at=confirmed_at,
            verifier_id=self._verifier_id,
        )
        return VerifiedConfirmationProof(
            application_id=application.id,
            verification_source=verification_source,
            external_ref_id=external_ref_id,
            confirmed_at=confirmed_at,
            verifier_id=self._verifier_id,
            raw_evidence=dict(raw_evidence),
            _seal_mac=mac,
        )


class ConfirmationVerifier(Protocol):
    """Server-side verifier contract implemented by authorized Stage 3+ integrations."""

    verifier_id: str
    supported_modes: frozenset[str]

    def verify(
        self,
        *,
        application: Application,
        evidence: dict[str, Any],
        issuer: ConfirmationProofIssuer,
    ) -> VerifiedConfirmationProof: ...


# Server-owned verifier registry. Stage 2 intentionally registers zero production verifiers.
_CONFIRMATION_VERIFIER_REGISTRY: dict[str, ConfirmationVerifier] = {}


def _select_server_verifiers(application: Application) -> list[ConfirmationVerifier]:
    """Resolve registered server-side verifiers without trusting caller selection."""
    if not _CONFIRMATION_VERIFIER_REGISTRY:
        raise ValueError(
            "No trusted confirmation verifier configured; CONFIRMED transition requires a verified server-side integration"
        )
    matching: list[ConfirmationVerifier] = []
    for verifier in _CONFIRMATION_VERIFIER_REGISTRY.values():
        supported = getattr(verifier, "supported_modes", frozenset())
        if not supported or application.application_mode in supported:
            matching.append(verifier)
    if not matching:
        raise ValueError(
            f"No registered confirmation verifier supports application_mode={application.application_mode}"
        )
    return matching


def _validate_confirmation_proof(
    *,
    application: Application,
    evidence: dict[str, Any] | None,
) -> VerifiedConfirmationProof:
    """Fail-closed validation via server-owned verifier registry and HMAC-sealed proof."""
    verifiers = _select_server_verifiers(application)
    if not isinstance(evidence, dict) or not evidence:
        raise ValueError("Confirmation evidence must be a non-empty dictionary")

    last_exc: ValueError | None = None
    for verifier in verifiers:
        registered_id = str(getattr(verifier, "verifier_id", "")).strip()
        if not registered_id or _CONFIRMATION_VERIFIER_REGISTRY.get(registered_id) is not verifier:
            raise ValueError(f"Unregistered or mismatched confirmation verifier: {registered_id!r}")

        issuer = ConfirmationProofIssuer(registered_id, _capability=_SERVER_PROOF_CAPABILITY)
        try:
            proof = verifier.verify(application=application, evidence=evidence, issuer=issuer)
        except ValueError as exc:
            last_exc = exc
            continue
        if not isinstance(proof, VerifiedConfirmationProof):
            raise ValueError("ConfirmationVerifier must return a VerifiedConfirmationProof")
        if proof.verifier_id != registered_id:
            raise ValueError("Confirmation proof verifier_id does not match registered server verifier")
        if proof.application_id != application.id:
            raise ValueError("Confirmation proof application_id does not match target application")
        if proof.verification_source not in TRUSTED_CONFIRMATION_SOURCES:
            raise ValueError(f"Untrusted confirmation verification_source: {proof.verification_source}")
        if not proof.external_ref_id.strip():
            raise ValueError("Confirmation proof requires a non-empty external_ref_id")
        if proof.confirmed_at.tzinfo is None:
            raise ValueError("Confirmation proof requires a timezone-aware confirmed_at timestamp")

        expected_mac = _compute_proof_mac(
            application_id=proof.application_id,
            verification_source=proof.verification_source,
            external_ref_id=proof.external_ref_id,
            confirmed_at=proof.confirmed_at,
            verifier_id=proof.verifier_id,
        )
        if not hmac.compare_digest(proof._seal_mac, expected_mac):
            raise ValueError("Confirmation proof seal verification failed")
        return proof

    assert last_exc is not None
    raise last_exc


class ApplicationPolicyRouter:
    """Server-side policy enforcement and mode routing for applications."""

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        implemented_api_adapters: set[str] | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        # Stage 2 has no submission adapters implemented yet; callers/Stage 3 inject registered adapters.
        self.implemented_api_adapters = {a.strip().lower() for a in (implemented_api_adapters or set())}

    def count_daily_applications(self, db: Session, *, user_id: uuid.UUID, tz_name: str) -> int:
        start_utc, end_utc = _today_utc_bounds(tz_name)
        stmt = select(func.count(Application.id)).where(
            Application.user_id == user_id,
            Application.deleted_at.is_(None),
            Application.status.notin_(("FAILED", "EXPIRED", "REJECTED", "DUPLICATE")),
            Application.created_at >= start_utc,
            Application.created_at <= end_utc,
        )
        return int(db.scalar(stmt) or 0)

    def evaluate(
        self,
        db: Session,
        *,
        user: User,
        profile: CandidateProfile,
        job: Job,
        resume: Resume,
        preferred_mode: ApplicationMode | None = None,
        stage: Literal["prepare", "submit"] = "prepare",
    ) -> PolicyDecision:
        """Evaluate eligibility, approved-resume, daily cap, pause, and mode authorization."""
        blockers: list[str] = []
        warnings: list[str] = []

        canonical_job_key = compute_canonical_job_key(job)
        idempotency_key = compute_application_idempotency_key(
            user_id=user.id,
            canonical_job_key=canonical_job_key,
            resume_id=resume.id,
        )

        submission_paused = bool(self.settings.pause_all_submissions)
        if submission_paused:
            if stage == "submit":
                blockers.append("Global submission pause (PAUSE_ALL_SUBMISSIONS) is active")
            else:
                warnings.append("Global submission pause is active; preparation allowed, submission blocked")

        if job.status != "active" or job.expired_at is not None:
            blockers.append(f"Job is not active (status={job.status})")

        match = db.scalar(
            select(JobMatch).where(JobMatch.job_id == job.id, JobMatch.profile_id == profile.id)
        )
        if match is None:
            blockers.append("No JobMatch record found for job and candidate profile")
        else:
            if not match.eligible or not match.hard_constraints_passed:
                blockers.append("Candidate does not satisfy job eligibility/hard constraints")
            if match.score < self.settings.min_job_match_score:
                blockers.append(
                    f"JobMatch score {match.score:.1f} is below MIN_JOB_MATCH_SCORE ({self.settings.min_job_match_score})"
                )

        if resume.profile_id != profile.id:
            blockers.append("Resume does not belong to candidate profile")
        else:
            approved_mapping = db.scalar(
                select(ResumeJobMapping).where(
                    ResumeJobMapping.job_id == job.id,
                    ResumeJobMapping.profile_id == profile.id,
                    ResumeJobMapping.resume_id == resume.id,
                    ResumeJobMapping.status == "completed",
                    ResumeJobMapping.approval_status == "approved",
                )
            )
            if approved_mapping is None:
                blockers.append("Resume is not an approved tailored resume for this job")

        daily_used = self.count_daily_applications(db, user_id=user.id, tz_name=user.timezone)
        daily_limit = self.settings.max_applications_per_day
        if daily_used >= daily_limit:
            blockers.append(f"Daily application limit reached ({daily_used}/{daily_limit})")

        selected_mode = self._select_mode(job=job, preferred_mode=preferred_mode, blockers=blockers)

        return PolicyDecision(
            allowed=len(blockers) == 0,
            selected_mode=selected_mode if len(blockers) == 0 else None,
            submission_paused=submission_paused,
            blocker_reasons=blockers,
            warnings=warnings,
            canonical_job_key=canonical_job_key,
            idempotency_key=idempotency_key,
            daily_used=daily_used,
            daily_limit=daily_limit,
        )

    def _select_mode(
        self,
        *,
        job: Job,
        preferred_mode: ApplicationMode | None,
        blockers: list[str],
    ) -> ApplicationMode:
        connector = (job.source.connector_type if job.source else "").strip().lower()
        source_cfg = job.source.config if (job.source and isinstance(job.source.config, dict)) else {}
        api_authorized = bool(source_cfg.get("submission_api_authorized", False))
        adapter_implemented = connector in self.implemented_api_adapters
        api_eligible = bool(self.settings.auto_submit_supported_apis and api_authorized and adapter_implemented)

        destination = normalize_canonical_url(job.application_url or job.canonical_url or "")

        if preferred_mode == "official_api":
            if not api_eligible:
                blockers.append(
                    "official_api mode is not authorized or no submission adapter is implemented for this source"
                )
                return "manual"
            return "official_api"

        if preferred_mode == "manual":
            return "manual"

        if api_eligible and preferred_mode is None:
            return "official_api"

        if destination and self.settings.browser_submission_requires_approval:
            return "human_approved_browser"

        return "manual"

    def prepare_application(
        self,
        db: Session,
        *,
        user: User,
        profile: CandidateProfile,
        job: Job,
        resume: Resume,
        form_schema: list[dict[str, Any]] | None = None,
        llm_drafts: dict[str, str] | None = None,
        preferred_mode: ApplicationMode | None = None,
        form_agent: FormUnderstandingAgent | None = None,
    ) -> ApplicationPreparationResult:
        """Prepare an application, map verified answers, encrypt sensitive values, and persist."""
        policy = self.evaluate(
            db,
            user=user,
            profile=profile,
            job=job,
            resume=resume,
            preferred_mode=preferred_mode,
            stage="prepare",
        )
        if not policy.allowed or policy.selected_mode is None:
            raise PolicyViolationError("; ".join(policy.blocker_reasons))

        existing = db.scalar(
            select(Application).where(
                Application.user_id == user.id,
                Application.canonical_job_key == policy.canonical_job_key,
                Application.deleted_at.is_(None),
                Application.status.notin_(("FAILED", "EXPIRED", "REJECTED")),
            )
        )
        if existing is not None:
            raise DuplicateApplicationError(
                f"Active application {existing.id} already exists for canonical_job_key={policy.canonical_job_key}"
            )

        agent = form_agent or FormUnderstandingAgent()
        mapping = agent.analyze_and_map(
            job=job,
            profile=profile,
            resume=resume,
            form_schema=form_schema,
            llm_drafts=llm_drafts,
        )

        destination_url = normalize_canonical_url(job.application_url or job.canonical_url)
        app_id = uuid.uuid4()
        app = Application(
            id=app_id,
            user_id=user.id,
            job_id=job.id,
            resume_id=resume.id,
            canonical_job_key=policy.canonical_job_key,
            idempotency_key=policy.idempotency_key,
            application_mode=policy.selected_mode,
            status="RESUME_READY",
            destination_url=destination_url,
            thread_id=f"app-{app_id}",
        )
        db.add(app)
        try:
            db.flush()
        except IntegrityError as exc:
            db.rollback()
            raise DuplicateApplicationError(
                f"Concurrent duplicate application blocked for canonical_job_key={policy.canonical_job_key}"
            ) from exc

        target_status = "AWAITING_APPROVAL" if mapping.ready_for_approval else "NEEDS_USER_INPUT"
        validate_application_transition(app.status, target_status)
        app.status = target_status

        for item in mapping.mapped_answers:
            if item.is_sensitive:
                if item.proposed_answer is None:
                    continue
                ct, ver = encrypt_sensitive_answer(
                    item.proposed_answer,
                    application_id=str(app.id),
                    question_key=item.question_key,
                    settings=self.settings,
                )
                db.add(
                    ApplicationAnswer(
                        application_id=app.id,
                        question_key=item.question_key,
                        question=item.question,
                        is_sensitive=True,
                        answer_encrypted=ct,
                        answer_plaintext=None,
                        key_version=ver,
                        evidence_source=item.evidence_source,
                        user_verified=False,
                    )
                )
            else:
                if item.proposed_answer is None:
                    continue
                db.add(
                    ApplicationAnswer(
                        application_id=app.id,
                        question_key=item.question_key,
                        question=item.question,
                        is_sensitive=False,
                        answer_encrypted=None,
                        answer_plaintext=item.proposed_answer,
                        key_version=None,
                        evidence_source=item.evidence_source,
                        user_verified=(item.verification_state == "verified"),
                    )
                )

        record_audit_event(
            db,
            user_id=user.id,
            actor=f"user:{user.id}",
            action="application.prepared",
            resource_type="application",
            resource_id=str(app.id),
            metadata={
                "status": app.status,
                "application_mode": app.application_mode,
                "canonical_job_key": app.canonical_job_key,
                "missing_required_count": len(mapping.missing_required_keys),
                "unverified_count": len(mapping.unverified_keys),
                "sensitive_confirm_count": len(mapping.sensitive_keys_requiring_confirmation),
            },
        )
        db.flush()

        return ApplicationPreparationResult(
            application_id=app.id,
            status=app.status,  # type: ignore[arg-type]
            policy=policy,
            form_mapping=mapping,
        )


def transition_application_status(
    db: Session,
    *,
    application: Application,
    next_status: str,
    actor: str,
    confirmation_evidence: dict[str, Any] | None = None,
    error_message: str | None = None,
) -> Application:
    """Server-side transition guard enforcing state machine and confirmation-evidence rules."""
    validate_application_transition(application.status, next_status)

    if next_status == "CONFIRMED":
        merged_evidence = confirmation_evidence if confirmation_evidence is not None else application.confirmation_evidence
        proof = _validate_confirmation_proof(
            application=application,
            evidence=merged_evidence,
        )
        application.confirmation_evidence = {
            **proof.raw_evidence,
            "verification_source": proof.verification_source,
            "external_ref_id": proof.external_ref_id,
            "verifier_id": proof.verifier_id,
            "application_id": str(proof.application_id),
        }
        application.external_ref_id = proof.external_ref_id
        application.confirmed_at = proof.confirmed_at

    if error_message is not None:
        application.error_message = error_message

    previous_status = application.status
    application.status = next_status

    record_audit_event(
        db,
        user_id=application.user_id,
        actor=actor,
        action="application.status_transition",
        resource_type="application",
        resource_id=str(application.id),
        metadata={"from_status": previous_status, "to_status": next_status},
    )
    db.flush()
    return application
