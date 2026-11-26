from contextlib import contextmanager
from datetime import datetime, timezone
import os
from pathlib import Path
import uuid

from alembic import command
from alembic.config import Config
import psycopg
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.agents.application import FormUnderstandingAgent
from app.core.config import Settings
from app.core.security import decrypt_sensitive_answer
from app.database.base import Base
from app.database.models import (
    Application,
    ApplicationAnswer,
    AuditLog,
    CandidateProfile,
    CandidateSkill,
    Job,
    JobMatch,
    JobRequirement,
    JobSource,
    Resume,
    ResumeJobMapping,
    User,
)
import app.services.application_policy_service as policy_service_mod
from app.services.application_policy_service import (
    ApplicationPolicyRouter,
    ConfirmationProofIssuer,
    DuplicateApplicationError,
    PolicyViolationError,
    VerifiedConfirmationProof,
    compute_application_idempotency_key,
    compute_canonical_job_key,
    transition_application_status,
)
from app.services.deduplication_service import compute_dedup_key, normalize_canonical_url

BACKEND_DIR = Path(__file__).resolve().parents[1]


def _make_settings(**overrides) -> Settings:
    defaults = {
        "_env_file": None,
        "encryption_key": "stage2-test-encryption-key-32bytes!",
        "encryption_key_version": "v1",
        "min_job_match_score": 75,
        "max_applications_per_day": 5,
        "auto_submit_supported_apis": True,
        "browser_submission_requires_approval": True,
        "pause_all_submissions": False,
    }
    defaults.update(overrides)
    return Settings(**defaults)


def _seed_ready_entities(
    session: Session,
    *,
    suffix: str = "1",
    approval_status: str = "approved",
    eligible: bool = True,
    hard_constraints_passed: bool = True,
    match_score: float = 88.0,
    connector_type: str = "greenhouse",
    source_config: dict | None = None,
    job_status: str = "active",
    company: str = "Acme AI",
    role_title: str = "Senior AI Engineer",
) -> tuple[User, CandidateProfile, Job, Resume]:
    user = User(email=f"m5-s2-{suffix}@example.com", hashed_password="hashed-password")
    session.add(user)
    session.flush()

    profile = CandidateProfile(
        user_id=user.id,
        full_name="Chethan Gowda",
        email=user.email,
        phone="+91-9999999999",
        location="Bengaluru",
        linkedin_url="https://linkedin.com/in/chethan",
        github_url="https://github.com/chethan",
        confirmed_preferences={"work_authorization": "Authorized to work in India"},
    )
    session.add(profile)
    session.flush()

    session.add(
        CandidateSkill(
            profile_id=profile.id,
            skill_name="Python",
            category="Language",
            verified=True,
        )
    )
    session.add(
        CandidateSkill(
            profile_id=profile.id,
            skill_name="UnverifiedRust",
            category="Language",
            verified=False,
        )
    )

    source = JobSource(
        source_name=f"src-{suffix}-{uuid.uuid4().hex[:6]}",
        connector_type=connector_type,
        config=source_config or {},
    )
    session.add(source)
    session.flush()

    dedup = compute_dedup_key(company, role_title)
    job = Job(
        source_id=source.id,
        source_job_id=f"ext-{suffix}",
        canonical_url=f"https://boards.greenhouse.io/{suffix}/jobs/{uuid.uuid4().hex[:8]}",
        dedup_key=dedup,
        content_checksum=uuid.uuid4().hex * 2,
        company=company,
        role_title=role_title,
        location="Remote",
        raw_jd="Build agentic workflows with Python and FastAPI.",
        application_url=f"https://boards.greenhouse.io/{suffix}/jobs/apply-{uuid.uuid4().hex[:6]}",
        status=job_status,
    )
    session.add(job)
    session.flush()

    session.add(
        JobMatch(
            job_id=job.id,
            profile_id=profile.id,
            score=match_score,
            eligible=eligible,
            hard_constraints_passed=hard_constraints_passed,
            recommendation="apply" if eligible else "skip",
            explanation="Matched skills and constraints.",
        )
    )

    resume = Resume(
        profile_id=profile.id,
        is_master=False,
        original_filename=f"tailored-{suffix}.pdf",
        content_type="application/pdf",
        file_size_bytes=1024,
        version_label=f"v-{suffix}",
        sha256_checksum=uuid.uuid4().hex * 2,
    )
    session.add(resume)
    session.flush()

    session.add(
        ResumeJobMapping(
            resume_id=resume.id,
            job_id=job.id,
            profile_id=profile.id,
            run_id=f"run-{suffix}",
            version_number=1,
            is_active=(approval_status == "approved"),
            evidence_fingerprint="e" * 64,
            status="completed",
            approval_status=approval_status,
        )
    )
    session.flush()
    return user, profile, job, resume


def _sqlite_session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Session(engine)


def test_form_understanding_verified_unverified_missing_and_sensitive():
    with _sqlite_session() as session:
        _, profile, job, resume = _seed_ready_entities(session, suffix="form1")
        session.refresh(profile)

        agent = FormUnderstandingAgent()
        # 1. Verified fields + optional missing field -> ready_for_approval=True
        result_ok = agent.analyze_and_map(
            job=job,
            profile=profile,
            resume=resume,
            form_schema=[
                {"question_key": "full_name", "label": "Full Name", "field_type": "text", "required": True},
                {"question_key": "email", "label": "Email", "field_type": "email", "required": True},
                {"question_key": "resume", "label": "Resume", "field_type": "file", "required": True},
                {"question_key": "cover_note", "label": "Optional Cover Note", "field_type": "textarea", "required": False},
            ],
        )
        assert result_ok.ready_for_approval is True
        assert result_ok.missing_required_keys == []
        assert result_ok.missing_optional_keys == ["cover_note"]
        by_key = {m.question_key: m for m in result_ok.mapped_answers}
        assert by_key["full_name"].verification_state == "verified"
        assert by_key["full_name"].proposed_answer == "Chethan Gowda"
        assert by_key["cover_note"].verification_state == "missing"
        assert by_key["cover_note"].proposed_answer is None

        # 2. Required missing, LLM draft (unverified), and sensitive questions -> ready_for_approval=False
        result_blocked = agent.analyze_and_map(
            job=job,
            profile=profile,
            resume=resume,
            form_schema=[
                {"question_key": "why_company", "label": "Why do you want to join?", "required": True},
                {"question_key": "required_unknown", "label": "Custom Required Essay", "required": True},
                {"question_key": "work_auth", "label": "Are you legally authorized to work?", "required": True},
                {"question_key": "visa_sponsorship", "label": "Will you require visa sponsorship?", "required": True},
                {"question_key": "salary_expectation", "label": "Desired salary expectation?", "required": False},
            ],
            llm_drafts={"why_company": "I admire your engineering culture."},
        )
        assert result_blocked.ready_for_approval is False
        assert "required_unknown" in result_blocked.missing_required_keys
        assert "why_company" in result_blocked.unverified_keys
        assert set(result_blocked.sensitive_keys_requiring_confirmation) == {
            "work_auth",
            "visa_sponsorship",
            "salary_expectation",
        }
        blocked_map = {m.question_key: m for m in result_blocked.mapped_answers}
        assert blocked_map["why_company"].verification_state == "unverified"
        assert blocked_map["why_company"].requires_user_confirmation is True
        assert blocked_map["visa_sponsorship"].verification_state == "missing"
        assert blocked_map["visa_sponsorship"].proposed_answer is None
        # Even with confirmed_preferences hint, sensitive answer requires per-application confirmation
        assert blocked_map["work_auth"].verification_state == "unverified"
        assert blocked_map["work_auth"].requires_user_confirmation is True


def test_form_understanding_extracts_jd_instructions_fallback():
    with _sqlite_session() as session:
        _, profile, job, resume = _seed_ready_entities(session, suffix="jd")
        session.add(
            JobRequirement(
                job_id=job.id,
                role_category="AI/ML Engineer",
                application_instructions=["Provide your expected CTC and notice period"],
            )
        )
        session.flush()
        session.refresh(job)

        agent = FormUnderstandingAgent()
        result = agent.analyze_and_map(job=job, profile=profile, resume=resume)
        assert len(result.questions) == 1
        assert result.questions[0].is_sensitive is True
        assert result.questions[0].sensitive_category == "salary"
        assert result.ready_for_approval is False


def test_policy_router_gates_pause_limits_and_official_api_auth():
    with _sqlite_session() as session:
        user, profile, job, resume = _seed_ready_entities(
            session,
            suffix="pol1",
            connector_type="greenhouse",
            source_config={"submission_api_authorized": False},
        )

        # PAUSE_ALL_SUBMISSIONS=True must NOT block preparation, but MUST block submit stage
        paused_router = ApplicationPolicyRouter(settings=_make_settings(pause_all_submissions=True))
        prep_decision = paused_router.evaluate(
            session, user=user, profile=profile, job=job, resume=resume, stage="prepare"
        )
        assert prep_decision.allowed is True
        assert prep_decision.submission_paused is True
        assert prep_decision.selected_mode == "human_approved_browser"

        submit_decision = paused_router.evaluate(
            session, user=user, profile=profile, job=job, resume=resume, stage="submit"
        )
        assert submit_decision.allowed is False
        assert any("PAUSE_ALL_SUBMISSIONS" in r for r in submit_decision.blocker_reasons)

        # official_api must NOT be selected without BOTH submission_api_authorized=True AND implemented adapter
        active_router = ApplicationPolicyRouter(settings=_make_settings())
        api_req_denied = active_router.evaluate(
            session,
            user=user,
            profile=profile,
            job=job,
            resume=resume,
            preferred_mode="official_api",
        )
        assert api_req_denied.allowed is False

        # Even if source config authorizes API, unimplemented adapter still blocks official_api
        job.source.config = {"submission_api_authorized": True}
        session.flush()
        no_adapter_denied = active_router.evaluate(
            session,
            user=user,
            profile=profile,
            job=job,
            resume=resume,
            preferred_mode="official_api",
        )
        assert no_adapter_denied.allowed is False

        # When both authorized AND adapter implemented, official_api is allowed
        adapter_router = ApplicationPolicyRouter(
            settings=_make_settings(),
            implemented_api_adapters={"greenhouse"},
        )
        api_allowed = adapter_router.evaluate(
            session,
            user=user,
            profile=profile,
            job=job,
            resume=resume,
            preferred_mode="official_api",
        )
        assert api_allowed.allowed is True
        assert api_allowed.selected_mode == "official_api"

        # Unapproved resume must be rejected
        _, profile2, job2, resume2 = _seed_ready_entities(
            session, suffix="unapp", approval_status="pending_review"
        )
        unapp_decision = active_router.evaluate(
            session, user=profile2.user, profile=profile2, job=job2, resume=resume2
        )
        assert unapp_decision.allowed is False
        assert any("approved tailored resume" in r for r in unapp_decision.blocker_reasons)

        # Ineligible JobMatch must be rejected
        _, profile3, job3, resume3 = _seed_ready_entities(
            session, suffix="inelig", eligible=False, match_score=50.0
        )
        inelig_decision = active_router.evaluate(
            session, user=profile3.user, profile=profile3, job=job3, resume=resume3
        )
        assert inelig_decision.allowed is False
        assert len(inelig_decision.blocker_reasons) >= 2

        # Daily limit reached must be rejected
        zero_limit_router = ApplicationPolicyRouter(
            settings=_make_settings(max_applications_per_day=1)
        )
        # Prepare 1 application to hit daily limit
        zero_limit_router.prepare_application(
            session,
            user=user,
            profile=profile,
            job=job,
            resume=resume,
            form_schema=[{"question_key": "email", "label": "Email", "field_type": "email", "required": True}],
        )
        # Second distinct job for same user hits daily limit
        job_extra = Job(
            source_id=job.source_id,
            source_job_id="ext-extra",
            canonical_url="https://boards.greenhouse.io/extra/jobs/999",
            dedup_key=compute_dedup_key("OtherCo", "Staff AI Engineer"),
            content_checksum="z" * 64,
            company="OtherCo",
            role_title="Staff AI Engineer",
            raw_jd="Python",
            status="active",
        )
        session.add(job_extra)
        session.flush()
        session.add(
            JobMatch(
                job_id=job_extra.id,
                profile_id=profile.id,
                score=90.0,
                eligible=True,
                hard_constraints_passed=True,
                recommendation="apply",
                explanation="Strong match",
            )
        )
        session.add(
            ResumeJobMapping(
                resume_id=resume.id,
                job_id=job_extra.id,
                profile_id=profile.id,
                run_id="run-extra",
                version_number=1,
                is_active=True,
                evidence_fingerprint="f" * 64,
                status="completed",
                approval_status="approved",
            )
        )
        session.flush()
        with pytest.raises(PolicyViolationError, match="Daily application limit"):
            zero_limit_router.prepare_application(
                session, user=user, profile=profile, job=job_extra, resume=resume
            )


def test_cross_source_idempotency_encryption_and_state_transitions():
    with _sqlite_session() as session:
        settings = _make_settings()
        router = ApplicationPolicyRouter(settings=settings)
        user, profile, job1, resume1 = _seed_ready_entities(
            session, suffix="dup1", company="Linear", role_title="AI Engineer"
        )

        # Second source listing the same company + role
        src2 = JobSource(source_name="lever-mirror", connector_type="lever")
        session.add(src2)
        session.flush()
        job2 = Job(
            source_id=src2.id,
            source_job_id="lever-999",
            canonical_url="https://jobs.lever.co/linear/ai-engineer",
            dedup_key=compute_dedup_key("Linear", "AI Engineer"),
            content_checksum="y" * 64,
            company="Linear",
            role_title="AI Engineer",
            location="Remote",
            raw_jd="Python",
            status="active",
        )
        session.add(job2)
        session.flush()
        assert compute_canonical_job_key(job1) == compute_canonical_job_key(job2)

        session.add(
            JobMatch(
                job_id=job2.id,
                profile_id=profile.id,
                score=92.0,
                eligible=True,
                hard_constraints_passed=True,
                recommendation="apply",
                explanation="Strong match",
            )
        )
        session.add(
            ResumeJobMapping(
                resume_id=resume1.id,
                job_id=job2.id,
                profile_id=profile.id,
                run_id="run-dup2",
                version_number=1,
                is_active=True,
                evidence_fingerprint="d" * 64,
                status="completed",
                approval_status="approved",
            )
        )
        session.flush()

        prep = router.prepare_application(
            session,
            user=user,
            profile=profile,
            job=job1,
            resume=resume1,
            form_schema=[
                {"question_key": "email", "label": "Email", "field_type": "email", "required": True},
                {"question_key": "work_authorization", "label": "Work authorization", "required": True},
            ],
        )
        assert prep.status == "NEEDS_USER_INPUT"

        # Sensitive answer persisted encrypted with no plaintext fallback
        sens_ans = session.scalar(
            select(ApplicationAnswer).where(
                ApplicationAnswer.application_id == prep.application_id,
                ApplicationAnswer.question_key == "work_authorization",
            )
        )
        assert sens_ans is not None
        assert sens_ans.is_sensitive is True
        assert sens_ans.answer_plaintext is None
        assert sens_ans.answer_encrypted is not None
        assert sens_ans.key_version == "v1"
        assert sens_ans.user_verified is False
        assert (
            decrypt_sensitive_answer(
                sens_ans.answer_encrypted,
                key_version="v1",
                application_id=str(prep.application_id),
                question_key="work_authorization",
                settings=settings,
            )
            == "Authorized to work in India"
        )

        # Cross-source duplicate for same user + canonical_job_key must be blocked
        with pytest.raises(DuplicateApplicationError):
            router.prepare_application(session, user=user, profile=profile, job=job2, resume=resume1)

        # Server-side transition enforcement
        app = session.get(Application, prep.application_id)
        assert app is not None
        transition_application_status(session, application=app, next_status="AWAITING_APPROVAL", actor=f"user:{user.id}")
        transition_application_status(session, application=app, next_status="APPROVED", actor=f"user:{user.id}")
        transition_application_status(session, application=app, next_status="SUBMISSION_PENDING", actor="system")
        transition_application_status(session, application=app, next_status="MANUAL_ACTION_REQUIRED", actor="system")
        transition_application_status(session, application=app, next_status="USER_REPORTED_SUBMITTED", actor=f"user:{user.id}")

        # Direct USER_REPORTED_SUBMITTED -> CONFIRMED must be rejected
        with pytest.raises(ValueError, match="Invalid application status transition"):
            transition_application_status(
                session,
                application=app,
                next_status="CONFIRMED",
                actor=f"user:{user.id}",
                confirmation_evidence={"verification_source": "email_confirmation", "email_message_id": "m-1"},
            )

        transition_application_status(session, application=app, next_status="UNVERIFIED", actor="system")

        class _TrustedTestVerifier:
            verifier_id = "test_email_verifier_v1"
            supported_modes = frozenset({"manual", "human_approved_browser", "official_api"})

            def __init__(
                self,
                *,
                override_app: Application | None = None,
                source: str = "verified_email_confirmation",
                ext_ref: str = "msg-999",
                confirmed_at: datetime | None = None,
                forged_proof: bool = False,
            ) -> None:
                self.override_app = override_app
                self.source = source
                self.ext_ref = ext_ref
                self.confirmed_at = confirmed_at or datetime(2026, 11, 26, 3, 0, tzinfo=timezone.utc)
                self.forged_proof = forged_proof

            def verify(
                self,
                *,
                application: Application,
                evidence: dict,
                issuer: ConfirmationProofIssuer,
            ) -> VerifiedConfirmationProof:
                if evidence.get("secret_token") != "trusted-server-hmac":
                    raise ValueError("Untrusted or unverified evidence payload")
                if self.forged_proof:
                    return VerifiedConfirmationProof(
                        application_id=application.id,
                        verification_source=self.source,
                        external_ref_id=self.ext_ref,
                        confirmed_at=self.confirmed_at,
                        verifier_id=self.verifier_id,
                        raw_evidence={"provider": "fake"},
                        _seal_mac="forged-mac",
                    )
                return issuer.issue(
                    application=self.override_app or application,
                    verification_source=self.source,
                    external_ref_id=self.ext_ref,
                    confirmed_at=self.confirmed_at,
                    raw_evidence={"provider": "gmail_api"},
                )

        @contextmanager
        def _test_registry(verifiers: dict):
            prev = dict(policy_service_mod._CONFIRMATION_VERIFIER_REGISTRY)
            policy_service_mod._CONFIRMATION_VERIFIER_REGISTRY.clear()
            policy_service_mod._CONFIRMATION_VERIFIER_REGISTRY.update(verifiers)
            try:
                yield
            finally:
                policy_service_mod._CONFIRMATION_VERIFIER_REGISTRY.clear()
                policy_service_mod._CONFIRMATION_VERIFIER_REGISTRY.update(prev)

        # 0. Production module must not expose any env-var-gated test override helper
        assert not hasattr(policy_service_mod, "_override_confirmation_verifiers_for_test")
        assert policy_service_mod._CONFIRMATION_VERIFIER_REGISTRY == {}

        # 1. Public transition service cannot accept caller-injected confirmation_verifier
        with pytest.raises(TypeError):
            transition_application_status(
                session,
                application=app,
                next_status="CONFIRMED",
                actor=f"user:{user.id}",
                confirmation_evidence={"secret_token": "trusted-server-hmac"},
                confirmation_verifier=_TrustedTestVerifier(),  # type: ignore[call-arg]
            )

        # 2. Caller cannot instantiate VerifiedConfirmationProof with fabricated evidence
        with pytest.raises(ValueError, match="server proof issuance|Invalid VerifiedConfirmationProof seal"):
            VerifiedConfirmationProof(
                application_id=app.id,
                verification_source="official_api_receipt",
                external_ref_id="fake-ref",
                confirmed_at=datetime(2026, 11, 26, 3, 0, tzinfo=timezone.utc),
                verifier_id="fake-verifier",
                raw_evidence={"fake": True},
                _seal_mac="0" * 64,
            )

        # 3. Caller cannot instantiate ConfirmationProofIssuer without server capability
        with pytest.raises(PermissionError, match="outside server registry"):
            ConfirmationProofIssuer("fake-verifier", _capability=object())

        # 4. Stage 2 default (empty production registry) keeps CONFIRMED inaccessible
        with pytest.raises(ValueError, match="No trusted confirmation verifier configured"):
            transition_application_status(
                session,
                application=app,
                next_status="CONFIRMED",
                actor=f"user:{user.id}",
                confirmation_evidence={"verification_source": "official_api_receipt", "external_ref_id": "fake"},
            )

        # 5. Unregistered / mismatched registry key fails closed
        spoofed = _TrustedTestVerifier()
        with _test_registry({"different_registered_key": spoofed}):
            with pytest.raises(ValueError, match="Unregistered or mismatched confirmation verifier"):
                transition_application_status(
                    session,
                    application=app,
                    next_status="CONFIRMED",
                    actor="tracker:email_verifier",
                    confirmation_evidence={"secret_token": "trusted-server-hmac"},
                )

        # 5b. Fake duck-typed verifier returning non-proof dict or trying to bypass seal fails
        trusted = _TrustedTestVerifier()

        class _FakeDuckTypedVerifier:
            verifier_id = "fake_duck"
            supported_modes = frozenset({"human_approved_browser"})

            def verify(self, *, application: Application, evidence: dict, issuer: ConfirmationProofIssuer):
                return {"fake": True}

        with _test_registry({"fake_duck": _FakeDuckTypedVerifier()}):
            with pytest.raises(ValueError, match="VerifiedConfirmationProof"):
                transition_application_status(
                    session,
                    application=app,
                    next_status="CONFIRMED",
                    actor="tracker:email_verifier",
                    confirmation_evidence={"secret_token": "trusted-server-hmac"},
                )

        # 6. Arbitrary caller-supplied dict without server verification is rejected
        with _test_registry({trusted.verifier_id: trusted}):
            with pytest.raises(ValueError, match="Untrusted or unverified"):
                transition_application_status(
                    session,
                    application=app,
                    next_status="CONFIRMED",
                    actor=f"user:{user.id}",
                    confirmation_evidence={"verification_source": "user_self_report", "external_ref_id": "fake"},
                )

        # 7. Mismatched application_id is rejected
        other_app = Application(
            id=uuid.uuid4(),
            user_id=user.id,
            job_id=app.job_id,
            resume_id=app.resume_id,
            canonical_job_key="other",
            idempotency_key="other",
            application_mode="human_approved_browser",
            status="UNVERIFIED",
            destination_url=app.destination_url,
            thread_id="other",
        )
        mismatched = _TrustedTestVerifier(override_app=other_app)
        with _test_registry({mismatched.verifier_id: mismatched}):
            with pytest.raises(ValueError, match="application_id does not match"):
                transition_application_status(
                    session,
                    application=app,
                    next_status="CONFIRMED",
                    actor="tracker:email_verifier",
                    confirmation_evidence={"secret_token": "trusted-server-hmac"},
                )

        # 8. Untrusted source is rejected even from registered verifier
        untrusted_src = _TrustedTestVerifier(source="llm_inferred")
        with _test_registry({untrusted_src.verifier_id: untrusted_src}):
            with pytest.raises(ValueError, match="Untrusted confirmation verification_source"):
                transition_application_status(
                    session,
                    application=app,
                    next_status="CONFIRMED",
                    actor="tracker:email_verifier",
                    confirmation_evidence={"secret_token": "trusted-server-hmac"},
                )

        # 9. Naive timestamp is rejected
        naive_ts = _TrustedTestVerifier(confirmed_at=datetime(2026, 11, 26, 3, 0))
        with _test_registry({naive_ts.verifier_id: naive_ts}):
            with pytest.raises(ValueError, match="timezone-aware confirmed_at"):
                transition_application_status(
                    session,
                    application=app,
                    next_status="CONFIRMED",
                    actor="tracker:email_verifier",
                    confirmation_evidence={"secret_token": "trusted-server-hmac"},
                )

        # 10. Controlled trusted-verifier fixture in pytest registry succeeds cleanly
        with _test_registry({trusted.verifier_id: trusted}):
            confirmed_app = transition_application_status(
                session,
                application=app,
                next_status="CONFIRMED",
                actor="tracker:email_verifier",
                confirmation_evidence={"secret_token": "trusted-server-hmac"},
            )
            assert confirmed_app.status == "CONFIRMED"
            assert confirmed_app.confirmed_at == datetime(2026, 11, 26, 3, 0, tzinfo=timezone.utc)
            assert confirmed_app.confirmation_evidence["verifier_id"] == "test_email_verifier_v1"

        # 11. Registry automatically restored to empty after context exit -> CONFIRMED inaccessible again
        confirmed_app_2_app = Application(
            user_id=user.id,
            job_id=job2.id,
            resume_id=resume1.id,
            canonical_job_key="post-exit-canon",
            idempotency_key="post-exit-idem",
            application_mode="human_approved_browser",
            status="UNVERIFIED",
            destination_url=app.destination_url,
            thread_id="post-exit-thread",
        )
        session.add(confirmed_app_2_app)
        session.flush()
        with pytest.raises(ValueError, match="No trusted confirmation verifier configured"):
            transition_application_status(
                session,
                application=confirmed_app_2_app,
                next_status="CONFIRMED",
                actor="tracker:email_verifier",
                confirmation_evidence={"secret_token": "trusted-server-hmac"},
            )


def test_canonical_job_identity_and_idempotency():
    s1, s2 = uuid.uuid4(), uuid.uuid4()
    dedup = compute_dedup_key("Acme AI", "Senior AI Engineer")

    # 1. Same requisition across two different sources -> identical canonical key
    job_req_gh = Job(
        source_id=s1,
        source_job_id="gh-101",
        canonical_url="https://boards.greenhouse.io/acme/jobs/101?utm_source=linkedin",
        dedup_key=dedup,
        content_checksum="a" * 64,
        company="Acme AI",
        role_title="Senior AI Engineer",
        location="Bengaluru, India",
        raw_jd="Python",
        source_provenance={"requisition_id": "REQ-2026-01"},
    )
    job_req_lv = Job(
        source_id=s2,
        source_job_id="lever-abc",
        canonical_url="https://jobs.lever.co/acme/abc/",
        dedup_key=dedup,
        content_checksum="b" * 64,
        company="Acme AI",
        role_title="Senior AI Engineer",
        location="Bengaluru",
        raw_jd="Python",
        source_provenance={"requisition_id": "req-2026-01"},
    )
    assert compute_canonical_job_key(job_req_gh) == compute_canonical_job_key(job_req_lv)
    assert normalize_canonical_url(job_req_gh.canonical_url) == "https://boards.greenhouse.io/acme/jobs/101"

    # 2. Same company/title/location but different requisition IDs -> distinct canonical keys
    job_req_other = Job(
        source_id=s1,
        source_job_id="gh-102",
        canonical_url="https://boards.greenhouse.io/acme/jobs/102",
        dedup_key=dedup,
        content_checksum="c" * 64,
        company="Acme AI",
        role_title="Senior AI Engineer",
        location="Bengaluru",
        raw_jd="Python",
        source_provenance={"requisition_id": "REQ-2026-02"},
    )
    assert compute_canonical_job_key(job_req_gh) != compute_canonical_job_key(job_req_other)

    # 3. Missing external requisition IDs + harmless location formatting differences -> identical key
    job_no_req_a = Job(
        source_id=s1,
        canonical_url="https://boards.greenhouse.io/acme/jobs/201",
        dedup_key=dedup,
        content_checksum="d" * 64,
        company="Acme AI",
        role_title="Senior AI Engineer",
        location="San Francisco, CA",
        remote_policy="onsite",
        raw_jd="Python",
    )
    job_no_req_b = Job(
        source_id=s2,
        canonical_url="https://jobs.lever.co/acme/201",
        dedup_key=dedup,
        content_checksum="e" * 64,
        company="Acme AI",
        role_title="Senior AI Engineer",
        location="  san francisco   ca ",
        remote_policy="onsite",
        raw_jd="Python",
    )
    assert compute_canonical_job_key(job_no_req_a) == compute_canonical_job_key(job_no_req_b)

    # 4. Different cities -> distinct keys
    job_city_hyd = Job(
        source_id=s1,
        canonical_url="https://boards.greenhouse.io/acme/jobs/202",
        dedup_key=dedup,
        content_checksum="f" * 64,
        company="Acme AI",
        role_title="Senior AI Engineer",
        location="Hyderabad, India",
        remote_policy="onsite",
        raw_jd="Python",
    )
    job_city_blr = Job(
        source_id=s2,
        canonical_url="https://jobs.lever.co/acme/202",
        dedup_key=dedup,
        content_checksum="1" * 64,
        company="Acme AI",
        role_title="Senior AI Engineer",
        location="Bengaluru, India",
        remote_policy="onsite",
        raw_jd="Python",
    )
    assert compute_canonical_job_key(job_city_hyd) != compute_canonical_job_key(job_city_blr)

    # 5. Remote vs location-specific jobs -> distinct keys
    job_remote = Job(
        source_id=s1,
        canonical_url="https://boards.greenhouse.io/acme/jobs/203",
        dedup_key=dedup,
        content_checksum="2" * 64,
        company="Acme AI",
        role_title="Senior AI Engineer",
        location="Bengaluru, India",
        is_remote=True,
        remote_policy="remote",
        raw_jd="Python",
    )
    assert compute_canonical_job_key(job_remote) != compute_canonical_job_key(job_city_blr)

    # 6. Repeated idempotency-key generation is deterministic
    uid, rid = uuid.uuid4(), uuid.uuid4()
    ck = compute_canonical_job_key(job_req_gh)
    k1 = compute_application_idempotency_key(user_id=uid, canonical_job_key=ck, resume_id=rid)
    k2 = compute_application_idempotency_key(user_id=uid, canonical_job_key=ck, resume_id=rid)
    assert k1 == k2


def test_live_postgres_stage2_duplicate_prevention_and_encryption():
    admin_url = "postgresql://applypilot:applypilot@localhost:5434/postgres"
    db_name = f"applypilot_m5_s2_{uuid.uuid4().hex[:8]}"
    target_url = f"postgresql+psycopg://applypilot:applypilot@localhost:5434/{db_name}"

    with psycopg.connect(admin_url, autocommit=True) as conn:
        conn.execute(f"CREATE DATABASE {db_name}")

    try:
        cfg = Config(str(BACKEND_DIR / "alembic.ini"))
        cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
        cfg.set_main_option("sqlalchemy.url", target_url)
        prev_db = os.environ.get("DATABASE_URL")
        os.environ["DATABASE_URL"] = target_url
        try:
            command.upgrade(cfg, "head")
        finally:
            if prev_db is None:
                os.environ.pop("DATABASE_URL", None)
            else:
                os.environ["DATABASE_URL"] = prev_db

        engine = create_engine(target_url)
        settings = _make_settings()
        router = ApplicationPolicyRouter(settings=settings)

        with Session(engine) as session:
            user, profile, job1, resume1 = _seed_ready_entities(
                session, suffix="pg1", company="Anthropic", role_title="Applied AI Engineer"
            )
            src2 = JobSource(source_name="pg-ashby", connector_type="ashby")
            session.add(src2)
            session.flush()
            job2 = Job(
                source_id=src2.id,
                source_job_id="ashby-1",
                canonical_url="https://jobs.ashbyhq.com/anthropic/applied-ai",
                dedup_key=compute_dedup_key("Anthropic", "Applied AI Engineer"),
                content_checksum="x" * 64,
                company="Anthropic",
                role_title="Applied AI Engineer",
                location="Remote",
                raw_jd="Python, Agents",
                status="active",
            )
            session.add(job2)
            session.flush()
            session.add(
                JobMatch(
                    job_id=job2.id,
                    profile_id=profile.id,
                    score=95.0,
                    eligible=True,
                    hard_constraints_passed=True,
                    recommendation="apply",
                    explanation="Strong match",
                )
            )
            session.add(
                ResumeJobMapping(
                    resume_id=resume1.id,
                    job_id=job2.id,
                    profile_id=profile.id,
                    run_id="run-pg-2",
                    version_number=1,
                    is_active=True,
                    evidence_fingerprint="g" * 64,
                    status="completed",
                    approval_status="approved",
                )
            )
            session.commit()

            prep = router.prepare_application(
                session,
                user=user,
                profile=profile,
                job=job1,
                resume=resume1,
                form_schema=[
                    {"question_key": "email", "label": "Email", "field_type": "email", "required": True},
                    {"question_key": "work_authorization", "label": "Work authorization", "required": True},
                ],
            )
            session.commit()

            ans = session.scalar(
                select(ApplicationAnswer).where(
                    ApplicationAnswer.application_id == prep.application_id,
                    ApplicationAnswer.question_key == "work_authorization",
                )
            )
            assert ans is not None
            assert ans.answer_plaintext is None
            assert ans.answer_encrypted is not None
            assert (
                decrypt_sensitive_answer(
                    ans.answer_encrypted,
                    key_version=ans.key_version or "",
                    application_id=str(prep.application_id),
                    question_key="work_authorization",
                    settings=settings,
                )
                == "Authorized to work in India"
            )

            audit = session.scalar(
                select(AuditLog).where(
                    AuditLog.resource_id == str(prep.application_id),
                    AuditLog.action == "application.prepared",
                )
            )
            assert audit is not None

            with pytest.raises(DuplicateApplicationError):
                router.prepare_application(session, user=user, profile=profile, job=job2, resume=resume1)

        engine.dispose()
    finally:
        with psycopg.connect(admin_url, autocommit=True) as conn:
            conn.execute(f"DROP DATABASE IF EXISTS {db_name} WITH (FORCE)")
