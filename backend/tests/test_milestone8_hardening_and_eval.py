from datetime import datetime, timedelta, timezone
import uuid

from fastapi.testclient import TestClient
import pytest

from app.core.resilience import CircuitBreaker, CircuitOpenError, DeadLetterBuffer
from app.database.models import Application, Job
from app.main import app
from app.services.email_confirmation_matcher import ParsedEmailCandidate, match_confirmation_email
from app.services.observability_service import CostBudgetExceededError, ObservabilityRegistry


def test_circuit_breaker_and_dead_letter_buffer():
    cb = CircuitBreaker(name="gemini", failure_threshold=2, recovery_timeout_seconds=0.05)
    assert cb.state == "closed"
    cb.ensure_closed()
    cb.record_failure()
    assert cb.state == "closed"
    cb.record_failure()
    assert cb.state == "open"
    with pytest.raises(CircuitOpenError):
        cb.ensure_closed()

    import time

    time.sleep(0.06)
    assert cb.state == "half_open"
    cb.record_success()
    assert cb.state == "closed"

    dlq = DeadLetterBuffer(max_entries=2)
    dlq.push(task_name="t1", task_id="1", error_type="TimeoutError")
    dlq.push(task_name="t2", task_id="2", error_type="HTTPError")
    dlq.push(task_name="t3", task_id="3", error_type="ValueError")
    recent = dlq.list_recent()
    assert len(recent) == 2
    assert recent[0]["task_name"] == "t3"


def test_observability_cost_budget_and_ops_endpoints():
    reg = ObservabilityRegistry(default_workflow_budget_usd=0.05)
    reg.check_budget("wf-1")
    reg.record_call(
        provider="gemini",
        model="gemini-2.5-flash",
        input_tokens=1000,
        output_tokens=500,
        latency_ms=120.5,
        estimated_cost_usd=0.06,
        workflow_id="wf-1",
    )
    with pytest.raises(CostBudgetExceededError):
        reg.check_budget("wf-1")

    summary = reg.summary()
    assert summary["total_calls"] == 1
    assert summary["total_tokens"] == 1500

    from app.api.dependencies.auth import get_current_user
    from app.database.models import User

    with TestClient(app) as client:
        assert client.get("/api/ops/observability").status_code == 401
        app.dependency_overrides[get_current_user] = lambda: User(
            id=uuid.uuid4(), email="ops@example.com", hashed_password="x"
        )
        try:
            resp = client.get("/api/ops/observability")
            assert resp.status_code == 200
            assert "ai" in resp.json() and "dead_letters" in resp.json()
        finally:
            app.dependency_overrides.clear()


def test_email_confirmation_precision_recall_zero_false_positives():
    now = datetime.now(timezone.utc)
    job = Job(
        id=uuid.uuid4(),
        source_id=uuid.uuid4(),
        source_job_id="REQ-101",
        canonical_url="https://boards.greenhouse.io/acme/jobs/101",
        dedup_key="a" * 64,
        content_checksum="b" * 64,
        company="Acme Corp",
        role_title="Backend Engineer",
        raw_jd="Python",
    )
    application = Application(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        job_id=job.id,
        resume_id=uuid.uuid4(),
        canonical_job_key="c" * 64,
        idempotency_key="d" * 64,
        status="USER_REPORTED_SUBMITTED",
        destination_url="https://boards.greenhouse.io/acme/jobs/101",
        thread_id="t1",
        submitted_at=now - timedelta(hours=1),
    )

    positives = [
        ParsedEmailCandidate(
            gmail_message_id="p1",
            gmail_thread_id="t1",
            sender_domain="greenhouse.io",
            subject="Application received for Backend Engineer",
            snippet="Thank you for applying to Acme Corp (REQ-101).",
            received_at=now,
            auth_results_header="dkim=pass",
        )
    ]
    negatives = [
        ParsedEmailCandidate(
            gmail_message_id="n1",
            gmail_thread_id="t2",
            sender_domain="greenhouse.io",
            subject="Update from Acme Corp",
            snippet="Thank you for applying. Unfortunately we are not moving forward.",
            received_at=now,
        ),
        ParsedEmailCandidate(
            gmail_message_id="n2",
            gmail_thread_id="t3",
            sender_domain="googlemail.com",
            subject="Delivery Status Notification (Failure)",
            snippet="Undeliverable: Thank you for applying",
            received_at=now,
        ),
        ParsedEmailCandidate(
            gmail_message_id="n3",
            gmail_thread_id="t4",
            sender_domain="jobs-digest.example.com",
            subject="Weekly Job Digest",
            snippet="Unsubscribe from this list. Thank you for applying.",
            received_at=now,
        ),
    ]

    tp = sum(
        1
        for e in positives
        if match_confirmation_email(e, candidates=[(application, job)], now=now).match_status == "matched"
    )
    fp = sum(
        1
        for e in negatives
        if match_confirmation_email(e, candidates=[(application, job)], now=now).match_status == "matched"
    )
    assert tp == len(positives)
    assert fp == 0


def test_recv_ctrl_eof_raises_clean_error_on_closed_socket():
    import socket as _socket
    from app.services.browser_executor import _recv_ctrl

    s1, s2 = _socket.socketpair()
    try:
        s2.close()
        with pytest.raises(EOFError):
            _recv_ctrl(s1, bytearray())
    finally:
        s1.close()
