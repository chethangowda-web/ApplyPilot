from datetime import datetime, timedelta, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import tomllib
import uuid

from playwright.sync_api import sync_playwright
from pydantic import BaseModel, ValidationError
import pytest

from app.core.config import Settings
from app.core.security import encrypt_sensitive_answer
from app.database.models import (
    Application,
    ApplicationAnswer,
    ApprovalRecord,
    CandidateProfile,
    Job,
    JobSource,
    Resume,
    ResumeJobMapping,
)
from app.schemas.applications import (
    DetectedFormField,
    ExecutionOptions,
    ExecutionResult,
    FieldFillOutcome,
    HandoffSessionInfo,
    ManualSubmitAuthorizationRequest,
    ManualSubmitAuthorizationResponse,
    compute_answers_hash,
    compute_approval_snapshot_hash,
)
import app.services.browser_executor as browser_mod
from app.services.browser_executor import (
    BrowserHandoffManager,
    BrowserProcessRegistry,
    LocalSSRFEgressProxy,
    PlaywrightBrowserExecutor,
    ProcessSessionManifest,
    SubmitGuardState,
    Win32JobSupervisor,
    build_protected_chromium_args,
    create_isolated_browser_context,
    get_process_creation_time,
    install_submit_protection,
    load_verified_resume_snapshot,
    purge_expired_application_artifacts,
    resolve_and_validate_host,
    resolve_owned_artifact_bytes,
    save_masked_application_artifact,
    terminate_verified_process,
    validate_approval_and_answers,
    validate_destination_url,
    validate_resume_for_execution,
    verify_windows_egress_firewall,
)
from app.services.execution_adapters import (
    ApprovalValidationError,
    ExecutionContext,
    ManualSubmitAuthorization,
    SSRFBlockedError,
    SubmitBlockedViolationError,
    UnsupportedPlatformError,
    get_authorized_official_api_adapter,
    list_registered_official_api_adapters,
)

BACKEND_DIR = Path(__file__).resolve().parents[1]


def test_stage3_checkpoint_a_config_and_no_local_bypass(monkeypatch):
    for key in (
        "BROWSER_EXECUTION_TIMEOUT_SECONDS",
        "BROWSER_HANDOFF_TTL_SECONDS",
        "CAPTURE_APPLICATION_ARTIFACTS",
        "REQUIRE_OS_EGRESS_FIREWALL",
        "OS_EGRESS_FIREWALL_RULE_PREFIX",
    ):
        monkeypatch.delenv(key, raising=False)

    cfg = Settings(_env_file=None)
    assert cfg.browser_execution_timeout_seconds == 60
    assert cfg.browser_handoff_ttl_seconds == 1800
    assert cfg.capture_application_artifacts is False
    assert cfg.require_os_egress_firewall is False
    assert cfg.os_egress_firewall_rule_prefix == "ApplyPilot-Browser-Egress"

    # Public configuration must never expose an option allowing arbitrary private/local browser destinations
    assert not hasattr(cfg, "allow_local_browser_destinations")
    assert not hasattr(ExecutionOptions(), "allow_local_destinations")


def test_stage3_checkpoint_a_schemas_and_pii_minimization():
    now = datetime.now(timezone.utc)
    field = DetectedFormField(
        selector="input[name='email']",
        question_key="email",
        label="Email Address",
        field_type="email",
        required=True,
    )
    assert field.question_key == "email"

    outcome = FieldFillOutcome(question_key="ssn", status="filled")
    assert "value" not in type(outcome).model_fields

    # 1. Request schema strictly rejects client-supplied authorization_token, consumed, or extra fields
    handoff_id = uuid.uuid4()
    pending_id = uuid.uuid4()
    valid_req = ManualSubmitAuthorizationRequest(
        handoff_session_id=handoff_id,
        pending_submission_id=pending_id,
        expected_form_action="https://boards.greenhouse.io/acme/jobs/123/applications",
        expected_http_method="POST",
        expected_payload_sha256="a" * 64,
    )
    assert valid_req.handoff_session_id == handoff_id

    for bad_payload in (
        {
            "handoff_session_id": str(handoff_id),
            "pending_submission_id": str(pending_id),
            "expected_form_action": "https://boards.greenhouse.io/acme/jobs/123/applications",
            "expected_http_method": "POST",
        },
        {
            "handoff_session_id": str(handoff_id),
            "pending_submission_id": str(pending_id),
            "expected_form_action": "https://boards.greenhouse.io/acme/jobs/123/applications",
            "expected_http_method": "POST",
            "expected_payload_sha256": "a" * 64,
            "authorization_token": "attacker-supplied-token-0123456789abcdef",
        },
        {
            "handoff_session_id": str(handoff_id),
            "pending_submission_id": str(pending_id),
            "expected_form_action": "https://boards.greenhouse.io/acme/jobs/123/applications",
            "expected_http_method": "POST",
            "expected_payload_sha256": "a" * 64,
            "consumed": True,
        },
        {
            "handoff_session_id": str(handoff_id),
            "pending_submission_id": str(pending_id),
            "expected_form_action": "https://boards.greenhouse.io/acme/jobs/123/applications",
            "expected_http_method": "POST",
            "expected_payload_sha256": "a" * 64,
            "user_id": str(uuid.uuid4()),
        },
    ):
        with pytest.raises(ValidationError):
            ManualSubmitAuthorizationRequest.model_validate(bad_payload)

    # 2. ExecutionOptions also forbids unexpected client keys
    with pytest.raises(ValidationError):
        ExecutionOptions.model_validate({"browser_mode": "headless", "allow_local_destinations": True})

    # 3. Internal ManualSubmitAuthorization is NOT a Pydantic request model, generates CSPRNG token
    #    independently of client input, and keeps consumed exclusively server-controlled.
    assert not issubclass(ManualSubmitAuthorization, BaseModel)
    with pytest.raises(TypeError):
        ManualSubmitAuthorization(  # type: ignore[call-arg]
            application_id=uuid.uuid4(),
            handoff_session_id=handoff_id,
            user_id=uuid.uuid4(),
            destination_url="https://boards.greenhouse.io/acme/jobs/123",
            form_action="https://boards.greenhouse.io/acme/jobs/123/applications",
            http_method="POST",
            answers_hash="a" * 64,
            resume_checksum="b" * 64,
            approval_snapshot_hash="c" * 64,
            issued_at=now,
            expires_at=now + timedelta(minutes=10),
            authorization_token="client-supplied",
        )
    with pytest.raises(TypeError):
        ManualSubmitAuthorization(  # type: ignore[call-arg]
            application_id=uuid.uuid4(),
            handoff_session_id=handoff_id,
            user_id=uuid.uuid4(),
            destination_url="https://boards.greenhouse.io/acme/jobs/123",
            form_action="https://boards.greenhouse.io/acme/jobs/123/applications",
            http_method="POST",
            answers_hash="a" * 64,
            resume_checksum="b" * 64,
            approval_snapshot_hash="c" * 64,
            issued_at=now,
            expires_at=now + timedelta(minutes=10),
            consumed=True,
        )

    auth1 = ManualSubmitAuthorization(
        application_id=uuid.uuid4(),
        handoff_session_id=handoff_id,
        user_id=uuid.uuid4(),
        destination_url="https://boards.greenhouse.io/acme/jobs/123",
        form_action="https://boards.greenhouse.io/acme/jobs/123/applications",
        http_method="POST",
        answers_hash="a" * 64,
        resume_checksum="b" * 64,
        approval_snapshot_hash="c" * 64,
        issued_at=now,
        expires_at=now + timedelta(minutes=10),
    )
    auth2 = ManualSubmitAuthorization(
        application_id=auth1.application_id,
        handoff_session_id=auth1.handoff_session_id,
        user_id=auth1.user_id,
        destination_url=auth1.destination_url,
        form_action=auth1.form_action,
        http_method=auth1.http_method,
        answers_hash=auth1.answers_hash,
        resume_checksum=auth1.resume_checksum,
        approval_snapshot_hash=auth1.approval_snapshot_hash,
        issued_at=now,
        expires_at=now + timedelta(minutes=10),
    )
    assert len(auth1.authorization_token) >= 43
    assert auth1.authorization_token != auth2.authorization_token
    assert auth1.consumed is False
    assert auth1.authorization_token not in repr(auth1)

    auth1.mark_consumed()
    assert auth1.consumed is True
    with pytest.raises(SubmitBlockedViolationError):
        auth1.mark_consumed()

    # 4. Safe public response schema does not expose internal token or hashes
    safe_resp = auth1.to_safe_response()
    assert isinstance(safe_resp, ManualSubmitAuthorizationResponse)
    dumped = safe_resp.model_dump()
    assert "authorization_token" not in dumped
    assert "answers_hash" not in dumped
    assert "approval_snapshot_hash" not in dumped
    assert dumped["consumed"] is True

    res = ExecutionResult(
        application_id=auth1.application_id,
        status="MANUAL_ACTION_REQUIRED",
        adapter_name="playwright_browser",
        mode="human_approved_browser",
        browser_mode="visible_handoff",
        handoff_session=HandoffSessionInfo(
            handoff_session_id=auth1.handoff_session_id,
            application_id=auth1.application_id,
            status="active",
            created_at=now,
            expires_at=now + timedelta(minutes=30),
        ),
    )
    assert res.stopped_before_submit is True
    assert res.os_firewall_enforced is False
    assert res.artifact_ids == []


def test_stage3_checkpoint_a_empty_official_api_registry():
    assert list_registered_official_api_adapters() == frozenset()

    authorized_src = JobSource(
        source_name="gh-prod",
        connector_type="greenhouse",
        config={"submission_api_authorized": True},
    )
    assert get_authorized_official_api_adapter(authorized_src) is None
    assert get_authorized_official_api_adapter(None) is None


def test_stage3_checkpoint_a_pyproject_and_firewall_script():
    pyproject = tomllib.loads((BACKEND_DIR / "pyproject.toml").read_text(encoding="utf-8"))
    deps = pyproject["project"]["dependencies"]
    assert any(d.startswith("playwright>=") for d in deps)

    ps_script = BACKEND_DIR / "scripts" / "provision_browser_firewall.ps1"
    assert ps_script.is_file()
    content = ps_script.read_text(encoding="utf-8")
    assert "0.0.0.0-127.0.0.0" in content
    assert "127.0.0.2-255.255.255.255" in content
    assert "127.0.0.1" in content


def test_stage3_checkpoint_b_ssrf_url_and_dns_validation(monkeypatch):
    # Public IPv4 and IPv6 addresses must be accepted
    def _mock_public_v4(host, port, type=0):  # noqa: A002
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]

    monkeypatch.setattr(socket, "getaddrinfo", _mock_public_v4)
    infos_v4 = resolve_and_validate_host("example.com", 443)
    assert infos_v4[0][4][0] == "93.184.216.34"

    def _mock_public_v6(host, port, type=0):  # noqa: A002
        return [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2606:2800:220:1:248:1893:25c8:1946", 443, 0, 0))]

    monkeypatch.setattr(socket, "getaddrinfo", _mock_public_v6)
    infos_v6 = resolve_and_validate_host("example.com", 443)
    assert "2606:2800:220:1:248:1893:25c8:1946" in infos_v6[0][4][0]

    # Unsupported schemes, credentials, ports, LinkedIn, and private/metadata hosts/IPs rejected
    for bad_url in (
        "file:///C:/Windows/win.ini",
        "ftp://example.com/resume.pdf",
        "javascript:alert(1)",
        "https://user:pass@example.com/jobs/1",
        "https://www.linkedin.com/jobs/view/123",
        "https://example.com:8443/jobs/1",
        "http://127.0.0.1:5434/",
        "http://localhost:8000/",
        "http://169.254.169.254/latest/meta-data/",
        "http://metadata.google.internal/computeMetadata/v1/",
        "http://10.0.0.1/",
        "http://172.16.0.1/",
        "http://192.168.1.1/",
        "http://100.64.0.10/",
        "http://[::1]/",
        "http://[::ffff:127.0.0.1]/",
        "http://[::1%eth0]/",
        "http://0.0.0.0/",
        "http://224.0.0.1/",
    ):
        with pytest.raises(SSRFBlockedError):
            validate_destination_url(bad_url)

    # Default proxy instance must have empty _allowed_test_origins and reject loopback CONNECT/HTTP
    monkeypatch.undo()
    with LocalSSRFEgressProxy() as default_proxy:
        assert default_proxy._allowed_test_origins == frozenset()
        with pytest.raises(SSRFBlockedError):
            default_proxy.resolve_and_validate("127.0.0.1", 443)
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(5)
        sock.connect(("127.0.0.1", default_proxy.port))
        try:
            sock.sendall(b"CONNECT 127.0.0.1:443 HTTP/1.1\r\nHost: 127.0.0.1:443\r\n\r\n")
            connect_resp = sock.recv(1024)
            assert b"403 Forbidden" in connect_resp
        finally:
            sock.close()

    # Mixed DNS result containing public AND private IP must fail closed
    def _mock_mixed_dns(host, port, type=0):  # noqa: A002
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", port)),
        ]

    monkeypatch.setattr(socket, "getaddrinfo", _mock_mixed_dns)
    with pytest.raises(SSRFBlockedError, match="disallowed IP"):
        resolve_and_validate_host("mixed-rebind.example.com", 443)


def test_stage3_checkpoint_b_dns_rebinding_single_resolution_proxy(monkeypatch):
    # Start a local fixture server that echoes back
    hits: list[str] = []

    class _FixtureHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            body = b"single-resolve-ok"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            return

    fixture = ThreadingHTTPServer(("127.0.0.1", 0), _FixtureHandler)
    threading.Thread(target=fixture.serve_forever, daemon=True).start()

    resolve_calls = {"count": 0}

    def _rebinding_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):  # noqa: A002
        if host == "rebind.test.example":
            resolve_calls["count"] += 1
            if resolve_calls["count"] == 1:
                # 1st lookup returns validated destination IP (public in real use; fixture here)
                return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]
            # Any hypothetical 2nd lookup would poison to metadata IP
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", port))]
        return orig_getaddrinfo(host, port, family, type, proto, flags)

    orig_getaddrinfo = socket.getaddrinfo
    monkeypatch.setattr(socket, "getaddrinfo", _rebinding_getaddrinfo)

    try:
        with LocalSSRFEgressProxy(
            _allowed_test_origins=frozenset({("rebind.test.example", fixture.server_port)})
        ) as proxy:
            sock = socket.create_connection(("127.0.0.1", proxy.port), timeout=5)
            try:
                sock.sendall(
                    f"GET http://rebind.test.example:{fixture.server_port}/apply HTTP/1.1\r\n"
                    f"Host: rebind.test.example:{fixture.server_port}\r\n"
                    f"Connection: close\r\n\r\n".encode("ascii")
                )
                resp = b""
                while True:
                    chunk = sock.recv(4096)
                    if not chunk:
                        break
                    resp += chunk
            finally:
                sock.close()

            assert b"200 OK" in resp
            assert b"single-resolve-ok" in resp
            # Proxy must resolve DNS strictly ONCE per outbound connection
            assert resolve_calls["count"] == 1
            assert hits == ["/apply"]
    finally:
        fixture.shutdown()
        fixture.server_close()



def test_stage3_checkpoint_b_live_chromium_proxy_fail_closed_sw_and_ws():
    fixture_hits: list[str] = []

    class _FixtureHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            fixture_hits.append(self.path)
            if self.path == "/page":
                html = (
                    b"<!doctype html><html><body>"
                    b"<script>"
                    b"window.__swResult = 'none';"
                    b"window.checkSW = async () => {"
                    b"  if (!('serviceWorker' in navigator)) return 'blocked';"
                    b"  try { await navigator.serviceWorker.register('/sw.js'); return 'registered'; }"
                    b"  catch (e) { return 'rejected:' + e.name; }"
                    b"};"
                    b"</script></body></html>"
                )
            else:
                html = b"console.log('sw');"
            self.send_response(200)
            self.send_header("Content-Type", "text/html" if self.path == "/page" else "application/javascript")
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            self.wfile.write(html)

        def log_message(self, *args):
            return

    fixture = ThreadingHTTPServer(("127.0.0.1", 0), _FixtureHandler)
    threading.Thread(target=fixture.serve_forever, daemon=True).start()

    args = build_protected_chromium_args()
    assert "--proxy-bypass-list=<-loopback>" in args
    assert "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1" in args
    assert "--disable-quic" in args
    assert "--webrtc-ip-handling-policy=disable_non_proxied_udp" in args

    proxy = LocalSSRFEgressProxy(
        _allowed_test_origins=frozenset(
            {
                ("127.0.0.1", fixture.server_port),
                ("localhost", fixture.server_port),
            }
        )
    )
    proxy.start()
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=args)
            ctx = create_isolated_browser_context(browser, egress_proxy=proxy)
            page = ctx.new_page()

            # Both 127.0.0.1 and localhost fixture URLs must travel through the proxy
            page.goto(f"http://127.0.0.1:{fixture.server_port}/page")
            page.goto(f"http://localhost:{fixture.server_port}/page")
            assert any(f"127.0.0.1:{fixture.server_port}/page" in t for t in proxy.allowed_targets)
            assert any(f"localhost:{fixture.server_port}/page" in t for t in proxy.allowed_targets)

            # Service worker registration must be blocked
            sw_status = page.evaluate("() => window.checkSW()")
            assert sw_status in {"blocked", "rejected:SecurityError", "rejected:DOMException"} or sw_status.startswith("rejected:")
            assert "/sw.js" not in fixture_hits

            # Prohibited metadata / private / unapproved-port / WebSocket requests must be blocked
            blocked_fetch = page.evaluate(
                """async () => {
                    try { await fetch('http://169.254.169.254/latest/meta-data/'); return 'leaked'; }
                    catch (e) { return 'blocked'; }
                }"""
            )
            assert blocked_fetch == "blocked"

            ws_outcome = page.evaluate(
                """() => new Promise((resolve) => {
                    try {
                        const ws = new WebSocket('ws://127.0.0.1:5434/ws');
                        ws.onopen = () => resolve('opened');
                        ws.onerror = () => resolve('blocked');
                        ws.onclose = () => resolve('blocked');
                        setTimeout(() => resolve('blocked'), 1500);
                    } catch (e) {
                        resolve('blocked');
                    }
                })"""
            )
            assert ws_outcome == "blocked"
            assert len(proxy.blocked_targets) >= 2

            # Shut down proxy mid-session -> browser must fail closed without bypassing directly to fixture!
            hits_before_stop = len(fixture_hits)
            proxy.stop()
            with pytest.raises(Exception):
                page.goto(f"http://127.0.0.1:{fixture.server_port}/after-stop", timeout=3000)
            assert len(fixture_hits) == hits_before_stop

            ctx.close()
            browser.close()
    finally:
        proxy.stop()
        fixture.shutdown()
        fixture.server_close()


def test_stage3_checkpoint_b_windows_firewall_verification(monkeypatch):
    def _make_rule(name: str, *, action: str, protocol: str, remote: str, enabled: str = "Yes", program: str = r"C:\chrome\chrome.exe"):
        return (
            f"Rule Name: {name}\n"
            f"Enabled: {enabled}\n"
            f"Direction: Out\n"
            f"Profiles: Domain,Private,Public\n"
            f"RemoteIP: {remote}\n"
            f"Protocol: {protocol}\n"
            f"Program: {program}\n"
            f"Action: {action}\n"
        )

    valid_outputs = {
        "allprofiles": "Domain Profile Settings:\nState ON\nPrivate Profile Settings:\nState ON\nPublic Profile Settings:\nState ON\n",
        "ApplyPilot-Browser-Egress-Allow-Loopback-TCP": _make_rule(
            "Allow", action="Allow", protocol="TCP", remote="127.0.0.1"
        ),
        "ApplyPilot-Browser-Egress-Block-External-IPv4-TCP": _make_rule(
            "BlockV4", action="Block", protocol="TCP", remote="0.0.0.0-127.0.0.0,127.0.0.2-255.255.255.255"
        ),
        "ApplyPilot-Browser-Egress-Block-External-IPv6-TCP": _make_rule(
            "BlockV6", action="Block", protocol="TCP", remote="::/0"
        ),
        "ApplyPilot-Browser-Egress-Block-All-UDP": _make_rule(
            "BlockUDP", action="Block", protocol="UDP", remote="Any"
        ),
    }

    def _run_factory(overrides=None, missing=()):
        ovr = overrides or {}

        def _fake_run(cmd, capture_output=True, text=True, check=False):
            joined = " ".join(cmd)
            if "allprofiles" in joined:
                out = ovr.get("allprofiles", valid_outputs["allprofiles"])
                return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")
            for name in valid_outputs:
                if f"name={name}" in joined:
                    if name in missing:
                        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="No rules match")
                    out = ovr.get(name, valid_outputs[name])
                    return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="")

        return _fake_run

    monkeypatch.setattr(browser_mod.sys, "platform", "win32")
    monkeypatch.setattr(subprocess, "run", _run_factory())
    ok = verify_windows_egress_firewall(chromium_path=r"C:\chrome\chrome.exe")
    assert ok.enforced is True

    # Missing rule fails
    monkeypatch.setattr(subprocess, "run", _run_factory(missing={"ApplyPilot-Browser-Egress-Block-All-UDP"}))
    assert verify_windows_egress_firewall(chromium_path=r"C:\chrome\chrome.exe").enforced is False

    # Rule name exists but Enabled=No fails
    monkeypatch.setattr(
        subprocess,
        "run",
        _run_factory(
            overrides={
                "ApplyPilot-Browser-Egress-Block-All-UDP": _make_rule(
                    "BlockUDP", action="Block", protocol="UDP", remote="Any", enabled="No"
                )
            }
        ),
    )
    assert verify_windows_egress_firewall(chromium_path=r"C:\chrome\chrome.exe").enforced is False

    # Mismatched Program path fails
    monkeypatch.setattr(subprocess, "run", _run_factory())
    assert verify_windows_egress_firewall(chromium_path=r"C:\other\chrome.exe").enforced is False

    # Over-broad RemoteIP on Allow rule fails (even if 127.0.0.1 is listed alongside 10.0.0.0/8)
    for bad_allow in ("Any", "127.0.0.1,10.0.0.0/8", "127.0.0.0/8"):
        monkeypatch.setattr(
            subprocess,
            "run",
            _run_factory(
                overrides={
                    "ApplyPilot-Browser-Egress-Allow-Loopback-TCP": _make_rule(
                        "Allow", action="Allow", protocol="TCP", remote=bad_allow
                    )
                }
            ),
        )
        assert verify_windows_egress_firewall(chromium_path=r"C:\chrome\chrome.exe").enforced is False

    # Gapped IPv4 block range fails
    monkeypatch.setattr(
        subprocess,
        "run",
        _run_factory(
            overrides={
                "ApplyPilot-Browser-Egress-Block-External-IPv4-TCP": _make_rule(
                    "BlockV4", action="Block", protocol="TCP", remote="0.0.0.0-127.0.0.0,255.255.255.255"
                )
            }
        ),
    )
    assert verify_windows_egress_firewall(chromium_path=r"C:\chrome\chrome.exe").enforced is False

    # Narrow ::/128 on IPv6 block fails (must be ::/0 or Any)
    monkeypatch.setattr(
        subprocess,
        "run",
        _run_factory(
            overrides={
                "ApplyPilot-Browser-Egress-Block-External-IPv6-TCP": _make_rule(
                    "BlockV6", action="Block", protocol="TCP", remote="::/128"
                )
            }
        ),
    )
    assert verify_windows_egress_firewall(chromium_path=r"C:\chrome\chrome.exe").enforced is False

    # Port-restricted UDP rule fails
    monkeypatch.setattr(
        subprocess,
        "run",
        _run_factory(
            overrides={
                "ApplyPilot-Browser-Egress-Block-All-UDP": (
                    "Rule Name: BlockUDP\nEnabled: Yes\nDirection: Out\nProfiles: Domain,Private,Public\n"
                    "RemoteIP: Any\nProtocol: UDP\nLocalPort: Any\nRemotePort: 53\n"
                    r"Program: C:\chrome\chrome.exe" "\nAction: Block\n"
                )
            }
        ),
    )
    assert verify_windows_egress_firewall(chromium_path=r"C:\chrome\chrome.exe").enforced is False

    # Program=Any fails even when chromium_path=None
    monkeypatch.setattr(
        subprocess,
        "run",
        _run_factory(
            overrides={
                "ApplyPilot-Browser-Egress-Block-All-UDP": _make_rule(
                    "BlockUDP", action="Block", protocol="UDP", remote="Any", program="Any"
                )
            }
        ),
    )
    assert verify_windows_egress_firewall().enforced is False


def test_stage3_checkpoint_b_suspended_launch_and_child_inheritance(tmp_path, monkeypatch):
    import ctypes

    marker = tmp_path / "started.txt"
    sup = Win32JobSupervisor()
    try:
        # 1. Parent launches suspended, is assigned BEFORE resuming, spawns grandchild, then both die on close()
        grandchild_pid_file = tmp_path / "grandchild.txt"
        child_script = (
            "import pathlib, subprocess, sys, time; "
            f"pathlib.Path({str(marker)!r}).write_text('ran'); "
            "gc = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
            f"pathlib.Path({str(grandchild_pid_file)!r}).write_text(str(gc.pid)); "
            "time.sleep(60)"
        )
        parent_pid, parent_ct = sup.spawn_suspended_in_job([sys.executable, "-c", child_script])
        assert parent_pid > 0
        assert parent_ct is not None

        # Wait briefly for grandchild PID file
        for _ in range(50):
            if grandchild_pid_file.is_file() and grandchild_pid_file.read_text().strip():
                break
            import time as _t
            _t.sleep(0.1)
        assert marker.is_file()
        grandchild_pid = int(grandchild_pid_file.read_text().strip())
        assert get_process_creation_time(grandchild_pid) is not None

        # Closing supervisor terminates BOTH parent and grandchild atomically via Job Object
        sup.close(terminate_processes=True)
        import time as _t
        for _ in range(30):
            if get_process_creation_time(parent_pid) is None and get_process_creation_time(grandchild_pid) is None:
                break
            _t.sleep(0.1)
        assert get_process_creation_time(parent_pid) is None
        assert get_process_creation_time(grandchild_pid) is None
    finally:
        sup.close(terminate_processes=True)

    # 2. Assignment failure MUST terminate the suspended process BEFORE any instruction executes
    never_ran = tmp_path / "never_ran.txt"
    fail_sup = Win32JobSupervisor()
    try:
        monkeypatch.setattr(ctypes.windll.kernel32, "AssignProcessToJobObject", lambda _j, _p: 0)
        with pytest.raises(OSError, match="AssignProcessToJobObject failed"):
            fail_sup.spawn_suspended_in_job(
                [sys.executable, "-c", f"import pathlib; pathlib.Path({str(never_ran)!r}).write_text('executed')"]
            )
        assert not never_ran.exists()
    finally:
        fail_sup.close(terminate_processes=True)


def test_stage3_checkpoint_b_job_object_pid_reuse_and_reaper(tmp_path, monkeypatch):
    # Live Windows Job Object + PID creation-time verification test
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        ct = get_process_creation_time(proc.pid)
        assert ct is not None

        # Wrong expected creation_time (PID reuse protection) must refuse to terminate
        assert terminate_verified_process(unrelated.pid, expected_creation_time=123456789) is False
        assert unrelated.poll() is None

        sup = Win32JobSupervisor()
        assert sup.uses_kernel_job_object is True
        assigned_ct = sup.assign_process(proc.pid)
        assert assigned_ct == ct

        # Manifest reaper skips mismatched-creation-time PIDs (never kills unrelated process)
        reg = BrowserProcessRegistry(tmp_path)
        bad_profile = tmp_path / "bad_prof"
        bad_profile.mkdir()
        expired = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        sid = str(uuid.uuid4())
        reg.write_manifest(
            ProcessSessionManifest(
                session_id=sid,
                application_id=str(uuid.uuid4()),
                user_id=str(uuid.uuid4()),
                pid=unrelated.pid,
                creation_time=999999999,
                pgid=None,
                temp_profile_dir=str(bad_profile),
                created_at=expired,
                expires_at=expired,
            )
        )
        reaped = reg.reap_abandoned_sessions()
        assert sid in reaped
        assert unrelated.poll() is None  # Unrelated process left untouched!
        assert not bad_profile.exists()

        # Closing Job Object supervisor immediately terminates assigned child process tree
        sup.close(terminate_processes=True)
        assert proc.wait(timeout=5) != 0
    finally:
        for p in (proc, unrelated):
            if p.poll() is None:
                p.kill()
                p.wait(timeout=5)

    # POSIX fallback path verification
    killed_pgids: list[tuple[int, int]] = []
    monkeypatch.setattr(browser_mod.sys, "platform", "linux")
    monkeypatch.setattr(browser_mod.os, "getpgid", lambda pid: pid + 1000, raising=False)
    monkeypatch.setattr(browser_mod.os, "killpg", lambda pgid, sig: killed_pgids.append((pgid, sig)), raising=False)
    posix_sup = Win32JobSupervisor()
    assert posix_sup.uses_kernel_job_object is False
    assert posix_sup.assign_process(42) is None
    posix_sup.close(terminate_processes=True)
    assert len(killed_pgids) == 1
    assert killed_pgids[0][0] == 1042


def _make_checkpoint_c_bundle(tmp_path: Path, destination_url: str, *, include_sensitive_unconf: bool = False):
    storage_root = tmp_path / "storage"
    uid = uuid.uuid4()
    pid = uuid.uuid4()
    jid = uuid.uuid4()
    rid = uuid.uuid4()
    aid = uuid.uuid4()

    pdf_dir = storage_root / "tailored_resumes" / str(uid)
    pdf_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = pdf_dir / f"{rid}_tailored.pdf"
    pdf_bytes = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF\n"
    pdf_path.write_bytes(pdf_bytes)
    checksum = hashlib.sha256(pdf_bytes).hexdigest()

    cfg = Settings(
        _env_file=None,
        encryption_key="checkpoint-c-secret-encryption-key!",
        encryption_key_version="v1",
        storage_dir=str(storage_root),
        browser_execution_timeout_seconds=45,
        browser_handoff_ttl_seconds=600,
    )

    profile = CandidateProfile(id=pid, user_id=uid, full_name="Chethan Gowda", email="chethan@example.com")
    source = JobSource(id=uuid.uuid4(), source_name="gh-c", connector_type="greenhouse")
    job = Job(
        id=jid,
        source_id=source.id,
        source_job_id="j-c",
        canonical_url=destination_url,
        dedup_key="d" * 64,
        content_checksum="e" * 64,
        company="Acme",
        role_title="AI Engineer",
        raw_jd="Python",
    )
    resume = Resume(
        id=rid,
        profile_id=pid,
        is_master=False,
        original_filename="tailored.pdf",
        content_type="application/pdf",
        file_size_bytes=len(pdf_bytes),
        pdf_path=str(pdf_path),
        version_label="v1",
        sha256_checksum=checksum,
        generation_status="completed",
        validation_passed=True,
    )
    mapping = ResumeJobMapping(
        id=uuid.uuid4(),
        resume_id=rid,
        job_id=jid,
        profile_id=pid,
        run_id="run-c",
        version_number=1,
        is_active=True,
        evidence_fingerprint="f" * 64,
        status="completed",
        approval_status="approved",
    )
    app = Application(
        id=aid,
        user_id=uid,
        job_id=jid,
        resume_id=rid,
        canonical_job_key="k" * 64,
        idempotency_key="m" * 64,
        application_mode="human_approved_browser",
        status="APPROVED",
        destination_url=destination_url,
        thread_id=f"app-{aid}",
    )

    enc_ssn, kver = encrypt_sensitive_answer(
        "9876-SECRET",
        application_id=str(aid),
        question_key="national_id",
        settings=cfg,
    )
    answers = [
        ApplicationAnswer(
            application_id=aid,
            question_key="full_name",
            question="Full Name",
            is_sensitive=False,
            answer_plaintext="Chethan Gowda",
            user_verified=True,
        ),
        ApplicationAnswer(
            application_id=aid,
            question_key="email",
            question="Email",
            is_sensitive=False,
            answer_plaintext="chethan@example.com",
            user_verified=True,
        ),
        ApplicationAnswer(
            application_id=aid,
            question_key="experience_tier",
            question="Experience Tier",
            is_sensitive=False,
            answer_plaintext="Senior",
            user_verified=True,
        ),
        ApplicationAnswer(
            application_id=aid,
            question_key="relocation",
            question="Open to relocate",
            is_sensitive=False,
            answer_plaintext="yes",
            user_verified=True,
        ),
        ApplicationAnswer(
            application_id=aid,
            question_key="unverified_draft",
            question="Unverified Draft",
            is_sensitive=False,
            answer_plaintext="Draft LLM text",
            user_verified=False,
        ),
        ApplicationAnswer(
            application_id=aid,
            question_key="national_id",
            question="National ID",
            is_sensitive=True,
            answer_encrypted=enc_ssn,
            key_version=kver,
            user_verified=not include_sensitive_unconf,
        ),
    ]
    pair_list = []
    for a in answers:
        val = "9876-SECRET" if a.is_sensitive else (a.answer_plaintext or "")
        pair_list.append((a.question_key, val))
    ahash = compute_answers_hash(pair_list)
    now = datetime.now(timezone.utc)
    snap = compute_approval_snapshot_hash(
        application_id=aid,
        user_id=uid,
        resume_id=rid,
        resume_checksum=checksum,
        canonical_job_key=app.canonical_job_key,
        destination_url=destination_url,
        answers_hash=ahash,
        application_mode="human_approved_browser",
    )
    rec = ApprovalRecord(
        id=uuid.uuid4(),
        application_id=aid,
        decision="approved",
        actor=f"user:{uid}",
        resume_id=rid,
        resume_checksum=checksum,
        destination_url=destination_url,
        application_mode="human_approved_browser",
        answers_hash=ahash,
        approval_snapshot_hash=snap,
        decided_at=now,
        expires_at=now + timedelta(hours=24),
    )
    return cfg, storage_root, pdf_path, uid, profile, job, resume, mapping, app, answers, rec, ahash, snap


def test_stage3_checkpoint_c_resume_approval_and_artifact_security(tmp_path):
    (
        cfg,
        storage_root,
        pdf_path,
        uid,
        profile,
        job,
        resume,
        mapping,
        app,
        answers,
        rec,
        ahash,
        _snap,
    ) = _make_checkpoint_c_bundle(tmp_path, "https://boards.greenhouse.io/acme/jobs/1")

    # Happy validation succeeds
    resolved = validate_resume_for_execution(
        user_id=uid,
        application=app,
        profile=profile,
        resume=resume,
        mapping=mapping,
        storage_root=storage_root,
    )
    assert resolved == pdf_path.resolve()

    # Wrong user fails
    with pytest.raises(ApprovalValidationError, match="ownership"):
        validate_resume_for_execution(
            user_id=uuid.uuid4(),
            application=app,
            profile=profile,
            resume=resume,
            mapping=mapping,
            storage_root=storage_root,
        )

    # Master resume rejected
    resume.is_master = True
    with pytest.raises(ApprovalValidationError, match="Master resume"):
        validate_resume_for_execution(
            user_id=uid, application=app, profile=profile, resume=resume, mapping=mapping, storage_root=storage_root
        )
    resume.is_master = False

    # Unapproved mapping rejected
    mapping.approval_status = "pending_review"
    with pytest.raises(ApprovalValidationError, match="ResumeJobMapping"):
        validate_resume_for_execution(
            user_id=uid, application=app, profile=profile, resume=resume, mapping=mapping, storage_root=storage_root
        )
    mapping.approval_status = "approved"

    # Path traversal rejected without leaking path details
    orig_path = resume.pdf_path
    resume.pdf_path = str(storage_root / ".." / "outside.pdf")
    with pytest.raises(ApprovalValidationError, match="Invalid resume artifact path"):
        validate_resume_for_execution(
            user_id=uid, application=app, profile=profile, resume=resume, mapping=mapping, storage_root=storage_root
        )
    resume.pdf_path = orig_path

    # Tampered PDF checksum rejected
    pdf_path.write_bytes(b"%PDF-1.4 tampered!")
    with pytest.raises(ApprovalValidationError, match="checksum"):
        validate_resume_for_execution(
            user_id=uid, application=app, profile=profile, resume=resume, mapping=mapping, storage_root=storage_root
        )
    # Restore PDF
    pdf_bytes = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF\n"
    pdf_path.write_bytes(pdf_bytes)

    # Approval expiry and snapshot tampering rejection
    rec.expires_at = datetime.now(timezone.utc) - timedelta(seconds=5)
    with pytest.raises(ApprovalValidationError, match="expired"):
        validate_approval_and_answers(
            application=app,
            user_id=uid,
            resume=resume,
            verified_resume_checksum=resume.sha256_checksum,
            approval_record=rec,
            answers=answers,
            settings=cfg,
        )
    rec.expires_at = datetime.now(timezone.utc) + timedelta(hours=24)

    # Changed answer breaks snapshot hash
    orig_plain = answers[0].answer_plaintext
    answers[0].answer_plaintext = "Changed Name"
    with pytest.raises(ApprovalValidationError, match="snapshot hash mismatch"):
        validate_approval_and_answers(
            application=app,
            user_id=uid,
            resume=resume,
            verified_resume_checksum=resume.sha256_checksum,
            approval_record=rec,
            answers=answers,
            settings=cfg,
        )
    answers[0].answer_plaintext = orig_plain

    # Artifact owner confinement & retention purge
    art_id = save_masked_application_artifact(
        storage_root=storage_root,
        user_id=uid,
        application_id=app.id,
        png_bytes=b"\x89PNG\r\n\x1a\nfake",
    )
    assert resolve_owned_artifact_bytes(
        storage_root=storage_root, user_id=uid, application_id=app.id, artifact_id=art_id
    ).startswith(b"\x89PNG")
    with pytest.raises(FileNotFoundError):
        resolve_owned_artifact_bytes(
            storage_root=storage_root, user_id=uuid.uuid4(), application_id=app.id, artifact_id=art_id
        )
    art_file = storage_root / "artifacts" / uid.hex / f"{app.id.hex}_{uuid.UUID(art_id).hex}.png"
    old_ts = (datetime.now(timezone.utc) - timedelta(days=20)).timestamp()
    os.utime(art_file, (old_ts, old_ts))
    assert purge_expired_application_artifacts(storage_root=storage_root, retention_days=14) == 1


def test_stage3_checkpoint_c_supervised_executor_prefill_barriers_and_pii_safety(tmp_path):
    submitted_payloads: list[str] = []

    class _FixtureHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/form":
                html = b"""<!doctype html><html><body>
                <form id="app-form" method="POST" action="/apply">
                  <label for="f_name">Full Name</label>
                  <input id="f_name" name="full_name" type="text" required />
                  <label for="f_email">Email</label>
                  <input id="f_email" name="email" type="email" required />
                  <label for="f_exp">Experience Tier</label>
                  <select id="f_exp" name="experience_tier"><option value="Junior">Junior</option><option value="Senior">Senior</option></select>
                  <label for="f_reloc">Open to relocate</label>
                  <input id="f_reloc" name="relocation" type="checkbox" />
                  <label for="f_unv">Unverified Draft</label>
                  <input id="f_unv" name="unverified_draft" type="text" />
                  <label for="f_nid">National ID</label>
                  <input id="f_nid" name="national_id" type="text" />
                  <label for="f_ambig">Unknown Ambiguous Question</label>
                  <input id="f_ambig" name="unknown_ambig" type="text" />
                  <label for="f_cv">Resume Upload</label>
                  <input id="f_cv" name="resume" type="file" required />
                  <button id="submit-btn" type="submit">Submit Application</button>
                </form>
                <script>
                  window.addEventListener('DOMContentLoaded', () => {
                    try { document.getElementById('app-form').submit(); } catch (_) {}
                    setTimeout(() => {
                      fetch('/apply', {method: 'POST', body: 'timer-auto'}).catch(() => {});
                    }, 20);
                  });
                </script>
                </body></html>"""
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(html)))
                self.end_headers()
                self.wfile.write(html)
            elif self.path == "/captcha":
                html = b"<!doctype html><html><body><div class='g-recaptcha'></div><form></form></body></html>"
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(html)))
                self.end_headers()
                self.wfile.write(html)

        def do_POST(self):
            ln = int(self.headers.get("Content-Length") or "0")
            submitted_payloads.append(self.rfile.read(ln).decode("utf-8", errors="replace"))
            b = b"ok"
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b)

        def log_message(self, *args):
            return

    fixture = ThreadingHTTPServer(("127.0.0.1", 0), _FixtureHandler)
    threading.Thread(target=fixture.serve_forever, daemon=True).start()
    try:
        dest = f"http://127.0.0.1:{fixture.server_port}/form"
        cfg, storage_root, _pdf, uid, profile, job, resume, mapping, app, answers, rec, ahash, _snap = (
            _make_checkpoint_c_bundle(tmp_path, dest, include_sensitive_unconf=True)
        )
        # Recompute snapshot matching unconfirmed national_id (which is present in bundle with user_verified=False)
        executor = PlaywrightBrowserExecutor(
            settings=cfg,
            _allowed_test_origins=frozenset({("127.0.0.1", fixture.server_port)}),
        )
        ctx = ExecutionContext(
            application=app,
            job=job,
            resume=resume,
            verified_resume_path=Path(resume.pdf_path),
            verified_answers={},
            answers_hash=ahash,
            approval_record=rec,
            options=ExecutionOptions(browser_mode="headless", capture_artifacts=False),
        )
        res = executor.execute_with_validated_context(
            ctx=ctx, user_id=uid, profile=profile, mapping=mapping, answers=answers
        )
        assert res.stopped_before_submit is True
        assert res.status == "MANUAL_ACTION_REQUIRED"
        assert res.resume_attached is True
        assert res.blocked_submit_attempts >= 1
        assert submitted_payloads == []  # ZERO submissions reached server!
        assert res.artifact_ids == []  # Artifacts disabled by default
        by_key = {f.question_key: f.status for f in res.fields_filled}
        assert by_key["full_name"] == "filled"
        assert by_key["email"] == "filled"
        assert by_key["experience_tier"] == "filled"
        assert by_key["relocation"] == "filled"
        assert by_key["unverified_draft"] == "skipped_unverified"
        assert by_key["national_id"] == "skipped_unverified"
        assert by_key["unknown_ambig"] == "skipped_missing"

        # No decrypted sensitive answer or raw filesystem path leaked in ExecutionResult
        dumped = json.dumps(res.model_dump(mode="json"))
        assert "9876-SECRET" not in dumped
        assert str(storage_root) not in dumped

        # CAPTCHA barrier page stops safely
        captcha_url = f"http://127.0.0.1:{fixture.server_port}/captcha"
        cfg2, _sr, _p2, uid2, prof2, job2, res2, map2, app2, ans2, rec2, ahash2, _s2 = _make_checkpoint_c_bundle(
            tmp_path / "cap", captcha_url
        )
        ex2 = PlaywrightBrowserExecutor(
            settings=cfg2,
            _allowed_test_origins=frozenset({("127.0.0.1", fixture.server_port)}),
        )
        res_cap = ex2.execute_with_validated_context(
            ctx=ExecutionContext(
                application=app2,
                job=job2,
                resume=res2,
                verified_resume_path=Path(res2.pdf_path),
                verified_answers={},
                answers_hash=ahash2,
                approval_record=rec2,
                options=ExecutionOptions(browser_mode="headless"),
            ),
            user_id=uid2,
            profile=prof2,
            mapping=map2,
            answers=ans2,
        )
        assert res_cap.restriction_detected is not None
        assert "CAPTCHA" in res_cap.restriction_detected
        assert res_cap.status == "MANUAL_ACTION_REQUIRED"
    finally:
        fixture.shutdown()
        fixture.server_close()



def test_stage3_checkpoint_c_adversarial_submit_and_manual_authorization(tmp_path):
    server_hits: list[tuple[str, str]] = []

    class _FixtureHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            server_hits.append(("GET", self.path))
            if self.path == "/apply-page":
                html = b"""<!doctype html><html><body>
                <form id="f" method="POST" action="/apply">
                  <input id="q" name="q" value="abc" />
                  <button id="submit-btn" type="submit">Submit</button>
                </form>
                <form id="f-get" method="GET" action="/apply-get">
                  <input name="g" value="1" />
                  <button id="get-submit" type="submit">GetSubmit</button>
                </form>
                <button id="spa-btn" type="button" onclick="fetch('/spa-submit',{method:'POST',body:'x'})">SPA</button>
                </body></html>"""
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(html)))
                self.end_headers()
                self.wfile.write(html)
            else:
                b = b"get-ok"
                self.send_response(200)
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

        def do_POST(self):
            ln = int(self.headers.get("Content-Length") or "0")
            _ = self.rfile.read(ln)
            server_hits.append(("POST", self.path))
            b = b"post-ok"
            self.send_response(200)
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def log_message(self, *args):
            return

    fixture = ThreadingHTTPServer(("127.0.0.1", 0), _FixtureHandler)
    threading.Thread(target=fixture.serve_forever, daemon=True).start()
    dest = f"http://127.0.0.1:{fixture.server_port}/apply-page"
    action = f"http://127.0.0.1:{fixture.server_port}/apply"

    proxy = LocalSSRFEgressProxy(_allowed_test_origins=frozenset({("127.0.0.1", fixture.server_port)}))
    proxy.start()
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, args=build_protected_chromium_args())
            ctx = create_isolated_browser_context(browser, egress_proxy=proxy)
            state = SubmitGuardState(expected_destination_url=dest)
            install_submit_protection(ctx, state=state)
            page = ctx.new_page()
            page.goto(dest)
            state.mark_prefill_complete(final_url=dest, detected_form_action=action, detected_http_method="POST")
            server_hits.clear()

            # 1. JS form.submit() and requestSubmit() throw SecurityError
            assert page.evaluate("() => { try { document.getElementById('f').submit(); return 'ran'; } catch(e) { return e.name; } }") == "SecurityError"
            assert page.evaluate("() => { try { document.getElementById('f').requestSubmit(); return 'ran'; } catch(e) { return e.name; } }") == "SecurityError"

            # 2. Synthetic SubmitEvent, delayed setTimeout, Promise, fetch, XHR, sendBeacon, SPA click, GET form all blocked & NOT held
            page.evaluate(
                """() => {
                    document.getElementById('f').dispatchEvent(new SubmitEvent('submit', {bubbles:true, cancelable:true}));
                    document.getElementById('submit-btn').click();
                    setTimeout(() => { try { document.getElementById('f').submit(); } catch(_){} fetch('/apply',{method:'POST'}); }, 10);
                    Promise.resolve().then(() => { fetch('/apply',{method:'POST'}).catch(()=>{}); });
                    const xhr = new XMLHttpRequest(); xhr.open('POST', '/apply'); xhr.send('xhr');
                    navigator.sendBeacon('/apply', 'beacon');
                }"""
            )
            page.locator("#spa-btn").click()
            page.wait_for_timeout(150)
            assert state.get_pending_submission_info() is None

            # 3. GET form mismatch is blocked & NOT held when primary form is POST /apply
            tab_g = ctx.new_page()
            tab_g.goto(dest)
            try:
                tab_g.locator("#get-submit").click(no_wait_after=True)
                tab_g.wait_for_timeout(100)
            finally:
                tab_g.close()
            assert state.get_pending_submission_info() is None
            assert [h for h in server_hits if h[1] != "/apply-page"] == []

            # 4. Real user click intercepts & HOLDS pending submission without forwarding to server!
            page.locator("#submit-btn").click(no_wait_after=True)
            for _ in range(20):
                page.wait_for_timeout(30)
                if state.get_pending_submission_info() is not None:
                    break
            pend = state.get_pending_submission_info()
            assert pend is not None
            assert pend.target_url == action
            assert pend.http_method == "POST"
            assert [h for h in server_hits if h[1] != "/apply-page"] == []

            # 5. Wrong/expired/tampered/unrelated confirmations MUST NOT release held submission!
            now = datetime.now(timezone.utc)
            base_kwargs = dict(
                application_id=uuid.uuid4(),
                handoff_session_id=uuid.uuid4(),
                user_id=uuid.uuid4(),
                destination_url=dest,
                form_action=action,
                http_method="POST",
                answers_hash="a" * 64,
                resume_checksum="b" * 64,
                approval_snapshot_hash="c" * 64,
                issued_at=now,
                expires_at=now + timedelta(minutes=5),
            )

            bad_auth = ManualSubmitAuthorization(**base_kwargs)
            with pytest.raises(SubmitBlockedViolationError, match="Answers hash"):
                state.release_held_submission(
                    auth=bad_auth,
                    pending_submission_id=pend.pending_submission_id,
                    live_answers_hash="z" * 64,
                    live_resume_checksum="b" * 64,
                    live_approval_snapshot_hash="c" * 64,
                )
            assert bad_auth.consumed is False

            exp_auth = ManualSubmitAuthorization(**{**base_kwargs, "expires_at": now - timedelta(seconds=1)})
            with pytest.raises(SubmitBlockedViolationError, match="expired"):
                state.release_held_submission(
                    auth=exp_auth,
                    pending_submission_id=pend.pending_submission_id,
                    live_answers_hash="a" * 64,
                    live_resume_checksum="b" * 64,
                    live_approval_snapshot_hash="c" * 64,
                )
            assert exp_auth.consumed is False

            wrong_action_auth = ManualSubmitAuthorization(
                **{**base_kwargs, "form_action": f"http://127.0.0.1:{fixture.server_port}/other-action"}
            )
            with pytest.raises(SubmitBlockedViolationError, match="action/method"):
                state.release_held_submission(
                    auth=wrong_action_auth,
                    pending_submission_id=pend.pending_submission_id,
                    live_answers_hash="a" * 64,
                    live_resume_checksum="b" * 64,
                    live_approval_snapshot_hash="c" * 64,
                )
            assert [h for h in server_hits if h[1] != "/apply-page"] == []

            # Unrelated POST /telemetry while pending is held is blocked & leaves pending intact
            good_auth = ManualSubmitAuthorization(**base_kwargs)
            tab_noise = ctx.new_page()
            tab_noise.goto(dest)
            try:
                tab_noise.evaluate("() => fetch('/telemetry', {method: 'POST', body: 'noise'}).catch(() => {})")
                tab_noise.wait_for_timeout(100)
            finally:
                tab_noise.close()
            assert good_auth.consumed is False
            assert ("POST", "/telemetry") not in server_hits

            # Exact matching confirmation consumes token atomically & releases ONLY that held Route!
            held_route = state.release_held_submission(
                auth=good_auth,
                pending_submission_id=pend.pending_submission_id,
                live_answers_hash="a" * 64,
                live_resume_checksum="b" * 64,
                live_approval_snapshot_hash="c" * 64,
            )
            held_route.fallback()
            page.wait_for_timeout(200)
            assert good_auth.consumed is True
            assert [h for h in server_hits if h[1] == "/apply"] == [("POST", "/apply")]

            # Replay click and replay release are both blocked
            with pytest.raises(SubmitBlockedViolationError, match="already consumed"):
                state.release_held_submission(
                    auth=good_auth,
                    pending_submission_id=pend.pending_submission_id,
                    live_answers_hash="a" * 64,
                    live_resume_checksum="b" * 64,
                    live_approval_snapshot_hash="c" * 64,
                )
            tab_replay = ctx.new_page()
            tab_replay.goto(dest)
            try:
                tab_replay.locator("#submit-btn").click(no_wait_after=True)
                tab_replay.wait_for_timeout(150)
            finally:
                tab_replay.close()
            assert [h for h in server_hits if h[1] == "/apply"] == [("POST", "/apply")]

            ctx.close()
            browser.close()
    finally:
        proxy.stop()
        fixture.shutdown()
        fixture.server_close()


def test_stage3_checkpoint_c_visible_handoff_ownership_ttl_and_celery_guard(tmp_path, monkeypatch):
    class _FixtureHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            html = b"<!doctype html><html><body><form method='POST' action='/apply'><input name='full_name' required /><button type='submit'>Submit</button></form></body></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            self.wfile.write(html)
        def log_message(self, *args):
            return

    fixture = ThreadingHTTPServer(("127.0.0.1", 0), _FixtureHandler)
    threading.Thread(target=fixture.serve_forever, daemon=True).start()
    try:
        dest = f"http://127.0.0.1:{fixture.server_port}/"
        cfg, _sr, _pdf, uid, profile, job, resume, mapping, app, answers, rec, ahash, snap = (
            _make_checkpoint_c_bundle(tmp_path, dest)
        )
        mgr = BrowserHandoffManager(storage_root=Path(cfg.storage_dir))
        executor = PlaywrightBrowserExecutor(
            settings=cfg,
            handoff_manager=mgr,
            _allowed_test_origins=frozenset({("127.0.0.1", fixture.server_port)}),
        )
        res = executor.execute_with_validated_context(
            ctx=ExecutionContext(
                application=app,
                job=job,
                resume=resume,
                verified_resume_path=Path(resume.pdf_path),
                verified_answers={},
                answers_hash=ahash,
                approval_record=rec,
                options=ExecutionOptions(browser_mode="visible_handoff"),
            ),
            user_id=uid,
            profile=profile,
            mapping=mapping,
            answers=answers,
        )
        assert res.handoff_session is not None
        sid = res.handoff_session.handoff_session_id

        # Wrong user rejected for inspection, authorization, and close
        with pytest.raises(ApprovalValidationError):
            mgr.get_session_info(handoff_session_id=sid, user_id=uuid.uuid4())
        with pytest.raises(ApprovalValidationError):
            mgr.close_session(handoff_session_id=sid, user_id=uuid.uuid4())

        # Wrong form action / method rejected
        with pytest.raises(ApprovalValidationError, match="form action"):
            mgr.authorize_manual_submit(
                user_id=uid,
                application_id=app.id,
                request=ManualSubmitAuthorizationRequest(
                    handoff_session_id=sid,
                    pending_submission_id=uuid.uuid4(),
                    expected_form_action=f"http://127.0.0.1:{fixture.server_port}/wrong",
                    expected_http_method="POST",
                    expected_payload_sha256="a" * 64,
                ),
                live_answers_hash=ahash,
                live_resume_checksum=resume.sha256_checksum,
                live_approval_snapshot_hash=snap,
            )

        # Premature authorization BEFORE user clicks Submit in the visible browser is rejected fail-closed!
        with pytest.raises(SubmitBlockedViolationError, match="No intercepted pending user submission"):
            mgr.authorize_manual_submit(
                user_id=uid,
                application_id=app.id,
                request=ManualSubmitAuthorizationRequest(
                    handoff_session_id=sid,
                    pending_submission_id=uuid.uuid4(),
                    expected_form_action=f"http://127.0.0.1:{fixture.server_port}/apply",
                    expected_http_method="POST",
                    expected_payload_sha256="a" * 64,
                ),
                live_answers_hash=ahash,
                live_resume_checksum=resume.sha256_checksum,
                live_approval_snapshot_hash=snap,
            )
        assert mgr.get_session_info(handoff_session_id=sid, user_id=uid).manual_submit_armed is False

        # TTL expiration sweeps and terminates supervised browser session & cleans manifest
        expired = mgr.sweep_expired_sessions(now=datetime.now(timezone.utc) + timedelta(hours=1))
        assert sid in expired
        with pytest.raises(ApprovalValidationError):
            mgr.get_session_info(handoff_session_id=sid, user_id=uid)

        # Visible handoff must fail closed inside a background Celery worker task
        class _FakeReq:
            id = "celery-task-1"
        class _FakeTask:
            request = _FakeReq()
        import celery
        monkeypatch.setattr(celery, "current_task", _FakeTask())
        with pytest.raises(UnsupportedPlatformError, match="Celery"):
            executor.execute_with_validated_context(
                ctx=ExecutionContext(
                    application=app,
                    job=job,
                    resume=resume,
                    verified_resume_path=Path(resume.pdf_path),
                    verified_answers={},
                    answers_hash=ahash,
                    approval_record=rec,
                    options=ExecutionOptions(browser_mode="visible_handoff"),
                ),
                user_id=uid,
                profile=profile,
                mapping=mapping,
                answers=answers,
            )
    finally:
        fixture.shutdown()
        fixture.server_close()


def test_stage3_audit_get_submit_and_variant_blocking():
    server_hits: list[tuple[str, str]] = []

    class _FixtureHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            server_hits.append(("GET", self.path))
            if self.path.startswith("/apply-page"):
                html = (
                    b"<!doctype html><html><body>"
                    b"<form id='gf-nq' method='GET' action='/apply-get'>"
                    b"<button id='gf-nq-btn' type='submit'>GetNoQuery</button></form>"
                    b"<a id='alt-link' href='/submit-alt'>Alt</a>"
                    b"</body></html>"
                )
            else:
                html = b"<!doctype html><html><body>ok</body></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            self.wfile.write(html)

        def log_message(self, *args):
            return

    fixture = ThreadingHTTPServer(("127.0.0.1", 0), _FixtureHandler)
    threading.Thread(target=fixture.serve_forever, daemon=True).start()
    dest = f"http://127.0.0.1:{fixture.server_port}/apply-page"
    get_action = f"http://127.0.0.1:{fixture.server_port}/apply-get"

    proxy = LocalSSRFEgressProxy(_allowed_test_origins=frozenset({("127.0.0.1", fixture.server_port)}))
    proxy.start()
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, args=build_protected_chromium_args())
            ctx = create_isolated_browser_context(browser, egress_proxy=proxy)
            state = SubmitGuardState(expected_destination_url=dest)
            install_submit_protection(ctx, state=state)

            page = ctx.new_page()
            page.goto(dest)
            state.mark_prefill_complete(
                final_url=dest,
                detected_form_action=get_action,
                detected_http_method="GET",
            )
            server_hits.clear()

            # 1. fetch GET, XHR GET, path-traversal GET, and anchor nav MUST fail closed & NOT become pending
            tab2 = ctx.new_page()
            tab2.goto(dest)
            try:
                tab2.evaluate(
                    """() => {
                        fetch('/apply-get').catch(() => {});
                        fetch('/./apply/../apply-get').catch(() => {});
                        const xhr = new XMLHttpRequest();
                        xhr.open('GET', '/apply-get');
                        xhr.send();
                    }"""
                )
                tab2.locator("#alt-link").click(no_wait_after=True)
                tab2.wait_for_timeout(150)
            finally:
                tab2.close()

            assert [h for h in server_hits if h[1] != "/apply-page"] == []
            assert state.get_pending_submission_info() is None

            # 2. User click on queryless GET form holds submission (still 0 hits); subdomain auth cannot release it
            page.locator("#gf-nq-btn").click(no_wait_after=True)
            for _ in range(20):
                page.wait_for_timeout(30)
                if state.get_pending_submission_info() is not None:
                    break
            pend = state.get_pending_submission_info()
            assert pend is not None and pend.http_method == "GET"
            assert [h for h in server_hits if h[1] != "/apply-page"] == []

            now = datetime.now(timezone.utc)
            bad_sub_auth = ManualSubmitAuthorization(
                application_id=uuid.uuid4(),
                handoff_session_id=uuid.uuid4(),
                user_id=uuid.uuid4(),
                destination_url=dest,
                form_action=f"http://sub.127.0.0.1:{fixture.server_port}/apply-get",
                http_method="GET",
                answers_hash="a" * 64,
                resume_checksum="b" * 64,
                approval_snapshot_hash="c" * 64,
                issued_at=now,
                expires_at=now + timedelta(minutes=5),
            )
            with pytest.raises(SubmitBlockedViolationError, match="action/method"):
                state.release_held_submission(
                    auth=bad_sub_auth,
                    pending_submission_id=pend.pending_submission_id,
                    live_answers_hash="a" * 64,
                    live_resume_checksum="b" * 64,
                    live_approval_snapshot_hash="c" * 64,
                )
            assert [h for h in server_hits if h[1] != "/apply-page"] == []
            assert bad_sub_auth.consumed is False

            # 3. Exact matching GET ManualSubmitAuthorization releases ONE held submission and consumes atomically
            good_get_auth = ManualSubmitAuthorization(
                application_id=uuid.uuid4(),
                handoff_session_id=uuid.uuid4(),
                user_id=uuid.uuid4(),
                destination_url=dest,
                form_action=get_action,
                http_method="GET",
                answers_hash="a" * 64,
                resume_checksum="b" * 64,
                approval_snapshot_hash="c" * 64,
                issued_at=now,
                expires_at=now + timedelta(minutes=5),
            )
            held_route = state.release_held_submission(
                auth=good_get_auth,
                pending_submission_id=pend.pending_submission_id,
                live_answers_hash="a" * 64,
                live_resume_checksum="b" * 64,
                live_approval_snapshot_hash="c" * 64,
            )
            held_route.fallback()
            page.wait_for_timeout(150)
            assert good_get_auth.consumed is True
            get_hits = [h for h in server_hits if h[1].startswith("/apply-get")]
            assert len(get_hits) == 1
            assert get_hits[0][0] == "GET" and get_hits[0][1].rstrip("?") == "/apply-get"

            ctx.close()
            browser.close()
    finally:
        proxy.stop()
        fixture.shutdown()
        fixture.server_close()


def test_stage3_audit_concurrent_manual_auth_single_use_and_no_rearm(tmp_path):
    dest = "https://boards.greenhouse.io/acme/jobs/1"
    action = "https://boards.greenhouse.io/acme/jobs/1/apply"
    now = datetime.now(timezone.utc)
    auth = ManualSubmitAuthorization(
        application_id=uuid.uuid4(),
        handoff_session_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        destination_url=dest,
        form_action=action,
        http_method="POST",
        answers_hash="a" * 64,
        resume_checksum="b" * 64,
        approval_snapshot_hash="c" * 64,
        issued_at=now,
        expires_at=now + timedelta(minutes=5),
    )

    class _DummyRoute:
        pass

    state = SubmitGuardState(expected_destination_url=dest)
    state.mark_prefill_complete(final_url=dest, detected_form_action=action, detected_http_method="POST")
    held = state.hold_pending_submission(
        route=_DummyRoute(),  # type: ignore[arg-type]
        url=action,
        method="POST",
        page_url=dest,
        is_navigation=True,
        resource_type="document",
    )
    assert held is not None

    barrier = threading.Barrier(8)
    outcomes: list[bool] = []
    lock = threading.Lock()

    def _race():
        barrier.wait(timeout=5)
        try:
            state.release_held_submission(
                auth=auth,
                pending_submission_id=held.pending_submission_id,
                live_answers_hash="a" * 64,
                live_resume_checksum="b" * 64,
                live_approval_snapshot_hash="c" * 64,
            )
            ok = True
        except SubmitBlockedViolationError:
            ok = False
        with lock:
            outcomes.append(ok)

    threads = [threading.Thread(target=_race) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert outcomes.count(True) == 1
    assert outcomes.count(False) == 7
    assert auth.consumed is True
    assert state.authorized_submissions == 1
    assert state.blocked_attempts == 7

    # Holding another submission or releasing after consumption MUST fail closed
    assert (
        state.hold_pending_submission(
            route=_DummyRoute(),  # type: ignore[arg-type]
            url=action,
            method="POST",
            page_url=dest,
            is_navigation=True,
            resource_type="document",
        )
        is None
    )
    with pytest.raises(SubmitBlockedViolationError, match="already consumed"):
        state.release_held_submission(
            auth=ManualSubmitAuthorization(
                application_id=auth.application_id,
                handoff_session_id=auth.handoff_session_id,
                user_id=auth.user_id,
                destination_url=dest,
                form_action=action,
                http_method="POST",
                answers_hash="a" * 64,
                resume_checksum="b" * 64,
                approval_snapshot_hash="c" * 64,
                issued_at=now,
                expires_at=now + timedelta(minutes=5),
            ),
            pending_submission_id=held.pending_submission_id,
            live_answers_hash="a" * 64,
            live_resume_checksum="b" * 64,
            live_approval_snapshot_hash="c" * 64,
        )


def test_stage3_audit_resume_toctou_snapshot_protection(tmp_path, monkeypatch):
    from app.services._browser_worker import prefill_page_safely

    class _FixtureHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            html = (
                b"<!doctype html><html><body>"
                b"<form method='POST' action='/apply' enctype='multipart/form-data'>"
                b"<input type='file' name='resume' accept='.pdf' />"
                b"<input name='full_name' required />"
                b"<button type='submit'>Submit</button>"
                b"</form></body></html>"
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            self.wfile.write(html)

        def log_message(self, *args):
            return

    fixture = ThreadingHTTPServer(("127.0.0.1", 0), _FixtureHandler)
    threading.Thread(target=fixture.serve_forever, daemon=True).start()
    dest = f"http://127.0.0.1:{fixture.server_port}/"
    proxy = LocalSSRFEgressProxy(_allowed_test_origins=frozenset({("127.0.0.1", fixture.server_port)}))
    proxy.start()
    try:
        cfg, storage_root, pdf_path, uid, profile, job, resume, mapping, app, answers, rec, ahash, _snap = (
            _make_checkpoint_c_bundle(tmp_path, dest)
        )
        orig_bytes = pdf_path.read_bytes()

        # 1. Capture immutable snapshot cleanly, then tamper file on disk (simulating TOCTOU race)
        _path, snap_bytes, snap_sha = load_verified_resume_snapshot(
            user_id=uid,
            application=app,
            profile=profile,
            resume=resume,
            mapping=mapping,
            storage_root=storage_root,
        )
        assert snap_bytes == orig_bytes
        pdf_path.write_bytes(b"%PDF-1.4\n%ATTACKER-TAMPERED-DISK-BYTES")

        # 2. Browser upload via verified snapshot bytes delivers original verified bytes, NOT tampered disk bytes
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, args=build_protected_chromium_args())
            ctx = create_isolated_browser_context(browser, egress_proxy=proxy)
            state = SubmitGuardState(expected_destination_url=dest)
            install_submit_protection(ctx, state=state)
            page = ctx.new_page()
            out = prefill_page_safely(
                page=page,
                destination_url=dest,
                verified_resume_bytes=snap_bytes,
                verified_resume_filename=pdf_path.name,
                expected_resume_checksum=snap_sha,
                verified_answers={"full_name": "Jane Candidate"},
                verified_keys={"full_name"},
                unverified_keys=set(),
                sensitive_unconfirmed_keys=set(),
                submit_state=state,
                egress_proxy=proxy,
                egress_timeout_pause_ms=0,
            )
            assert out["resume_attached"] is True
            uploaded_text = page.evaluate(
                "async () => await document.querySelector(\"input[type='file']\").files[0].text()"
            )
            assert uploaded_text.encode("utf-8") == orig_bytes
            assert "ATTACKER-TAMPERED" not in uploaded_text
            ctx.close()
            browser.close()

        # 3. Restore & verify full PlaywrightBrowserExecutor fails closed if tampered BEFORE snapshot
        with pytest.raises(ApprovalValidationError, match="checksum"):
            load_verified_resume_snapshot(
                user_id=uid,
                application=app,
                profile=profile,
                resume=resume,
                mapping=mapping,
                storage_root=storage_root,
            )

        # 4. Simulate race inside PlaywrightBrowserExecutor RIGHT after snapshot capture
        pdf_path.write_bytes(orig_bytes)
        real_snapshot = browser_mod.load_verified_resume_snapshot

        def _racy_snapshot(**kwargs):
            p, b, s = real_snapshot(**kwargs)
            p.write_bytes(b"%PDF-1.4\n%POST-SNAPSHOT-RACE-TAMPER")
            return p, b, s

        monkeypatch.setattr(browser_mod, "load_verified_resume_snapshot", _racy_snapshot)
        executor = PlaywrightBrowserExecutor(
            settings=cfg,
            _allowed_test_origins=frozenset({("127.0.0.1", fixture.server_port)}),
        )
        res = executor.execute_with_validated_context(
            ctx=ExecutionContext(
                application=app,
                job=job,
                resume=resume,
                verified_resume_path=pdf_path,
                verified_answers={},
                answers_hash=ahash,
                approval_record=rec,
                options=ExecutionOptions(browser_mode="headless", capture_artifacts=False),
            ),
            user_id=uid,
            profile=profile,
            mapping=mapping,
            answers=answers,
        )
        assert res.resume_attached is True
        assert res.resume_checksum == snap_sha
    finally:
        proxy.stop()
        fixture.shutdown()
        fixture.server_close()


def test_stage3_audit_zero_pii_in_logs_results_errors_and_masked_artifacts(tmp_path, caplog):
    synthetic_ssn = "SYNTHETIC-PII-SSN-999112222"
    synthetic_name = "SYNTHETIC-CANDIDATE-Z9"

    class _FixtureHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            html = (
                b"<!doctype html><html><body>"
                b"<form method='POST' action='/apply'>"
                b"<label>Full Name <input name='full_name' required /></label>"
                b"<label>National ID <input name='national_id' /></label>"
                b"<input type='password' name='portal_pin' value='SECRET-PIN-VAL' />"
                b"<button type='submit'>Submit</button>"
                b"</form></body></html>"
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            self.wfile.write(html)

        def log_message(self, *args):
            return

    fixture = ThreadingHTTPServer(("127.0.0.1", 0), _FixtureHandler)
    threading.Thread(target=fixture.serve_forever, daemon=True).start()
    dest = f"http://127.0.0.1:{fixture.server_port}/"
    try:
        storage_root = tmp_path / "sr"
        storage_root.mkdir()
        pdf_path = storage_root / "r.pdf"
        pdf_bytes = b"%PDF-1.4\n%pii-audit"
        pdf_path.write_bytes(pdf_bytes)
        checksum = hashlib.sha256(pdf_bytes).hexdigest()
        cfg = Settings(
            _env_file=None,
            encryption_key="checkpoint-c-secret-encryption-key!",
            encryption_key_version="v1",
            storage_dir=str(storage_root),
            browser_execution_timeout_seconds=45,
            browser_handoff_ttl_seconds=600,
        )
        uid, pid, jid, rid, aid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        profile = CandidateProfile(id=pid, user_id=uid, full_name=synthetic_name, email="synthetic@example.com")
        source = JobSource(id=uuid.uuid4(), source_name="gh-pii", connector_type="greenhouse")
        job = Job(
            id=jid,
            source_id=source.id,
            source_job_id="pii-1",
            canonical_url=dest,
            dedup_key="d" * 64,
            content_checksum="e" * 64,
            company="Acme",
            role_title="Engineer",
            raw_jd="Python",
        )
        resume = Resume(
            id=rid,
            profile_id=pid,
            is_master=False,
            original_filename="tailored.pdf",
            content_type="application/pdf",
            file_size_bytes=len(pdf_bytes),
            pdf_path=str(pdf_path),
            version_label="v1",
            sha256_checksum=checksum,
            generation_status="completed",
            validation_passed=True,
        )
        mapping = ResumeJobMapping(
            id=uuid.uuid4(),
            resume_id=rid,
            job_id=jid,
            profile_id=pid,
            run_id="run-pii",
            version_number=1,
            is_active=True,
            evidence_fingerprint="f" * 64,
            status="completed",
            approval_status="approved",
        )
        app = Application(
            id=aid,
            user_id=uid,
            job_id=jid,
            resume_id=rid,
            canonical_job_key="greenhouse:pii-1",
            idempotency_key="i" * 64,
            application_mode="human_approved_browser",
            status="APPROVED",
            destination_url=dest,
            thread_id=f"app-{aid}",
        )
        enc_ssn, kver = encrypt_sensitive_answer(
            synthetic_ssn,
            application_id=str(aid),
            question_key="national_id",
            settings=cfg,
        )
        answers = [
            ApplicationAnswer(
                application_id=aid,
                question_key="full_name",
                question="Full Name",
                is_sensitive=False,
                answer_plaintext=synthetic_name,
                user_verified=True,
            ),
            ApplicationAnswer(
                application_id=aid,
                question_key="national_id",
                question="National ID",
                is_sensitive=True,
                answer_encrypted=enc_ssn,
                key_version=kver,
                user_verified=True,
            ),
        ]
        ahash = compute_answers_hash([("full_name", synthetic_name), ("national_id", synthetic_ssn)])
        now = datetime.now(timezone.utc)
        snap = compute_approval_snapshot_hash(
            application_id=aid,
            user_id=uid,
            resume_id=rid,
            resume_checksum=checksum,
            canonical_job_key=app.canonical_job_key,
            destination_url=dest,
            answers_hash=ahash,
            application_mode="human_approved_browser",
        )
        rec = ApprovalRecord(
            id=uuid.uuid4(),
            application_id=aid,
            decision="approved",
            actor=f"user:{uid}",
            resume_id=rid,
            resume_checksum=checksum,
            destination_url=dest,
            application_mode="human_approved_browser",
            answers_hash=ahash,
            approval_snapshot_hash=snap,
            decided_at=now,
            expires_at=now + timedelta(hours=24),
        )

        caplog.clear()
        executor = PlaywrightBrowserExecutor(
            settings=cfg,
            _allowed_test_origins=frozenset({("127.0.0.1", fixture.server_port)}),
        )
        res = executor.execute_with_validated_context(
            ctx=ExecutionContext(
                application=app,
                job=job,
                resume=resume,
                verified_resume_path=pdf_path,
                verified_answers={},
                answers_hash=ahash,
                approval_record=rec,
                options=ExecutionOptions(browser_mode="headless", capture_artifacts=True),
            ),
            user_id=uid,
            profile=profile,
            mapping=mapping,
            answers=answers,
        )

        serialized = res.model_dump_json()
        assert synthetic_ssn not in serialized
        assert synthetic_name not in serialized
        assert enc_ssn not in serialized
        assert str(storage_root) not in serialized
        assert str(pdf_path) not in serialized
        assert synthetic_ssn not in caplog.text
        assert enc_ssn not in caplog.text
        assert str(pdf_path) not in caplog.text
        assert len(res.artifact_ids) == 1
        png_bytes = resolve_owned_artifact_bytes(
            storage_root=storage_root,
            user_id=uid,
            application_id=aid,
            artifact_id=res.artifact_ids[0],
        )
        assert png_bytes.startswith(b"\x89PNG\r\n\x1a\n")
    finally:
        fixture.shutdown()
        fixture.server_close()


def test_pending_submission_captured_fields_preview_masks_sensitive_and_binds_wire_payload() -> None:
    from urllib.parse import urlencode
    from app.services.browser_executor import SubmitGuardState

    guard = SubmitGuardState(expected_destination_url="https://boards.greenhouse.io/acme/jobs/101")
    guard.mark_prefill_complete(
        final_url="https://boards.greenhouse.io/acme/jobs/101",
        detected_form_action="https://boards.greenhouse.io/acme/jobs/101",
        detected_http_method="POST",
    )
    sensitive_vals = {
        "field_nino": "AB123456C",
        "field_dob": "1999-04-12",
        "field_passport": "P99887766",
        "field_visa": "Requires H1B sponsorship",
        "field_phone": "+91-9876543210",
        "field_email": "aarav@example.com",
        "field_addr": "42 MG Road, Bengaluru",
        "field_comp": "2800000 INR",
        "opaque_custom": "ambiguous-secret-val",
    }
    expected = {"full_name": ["Aarav Mehta"], **{k: [v] for k, v in sensitive_vals.items()}}
    guard.set_expected_payload_policy(
        expected_fields=expected,
        expected_file_fields={"resume": "a" * 64},
        allowed_csrf_tokens={"csrf_token": "csrf-secret-xyz"},
        field_metadata={
            "full_name": {"label": "Full Name", "question_key": "full_name", "field_type": "text", "input_type": "text", "autocomplete": "name", "is_sensitive": False},
            "field_nino": {"label": "National Insurance Number", "question_key": "q_1", "field_type": "text", "input_type": "text", "autocomplete": "", "is_sensitive": False},
            "field_dob": {"label": "Birth Info", "question_key": "q_2", "field_type": "text", "input_type": "date", "autocomplete": "bday", "is_sensitive": False},
            "field_passport": {"label": "Passport Number", "question_key": "q_3", "field_type": "text", "input_type": "password", "autocomplete": "", "is_sensitive": False},
            "field_visa": {"label": "Work Authorization & Visa Sponsorship", "question_key": "q_4", "field_type": "text", "input_type": "text", "autocomplete": "", "is_sensitive": False},
            "field_phone": {"label": "Contact Number", "question_key": "q_5", "field_type": "phone", "input_type": "tel", "autocomplete": "tel", "is_sensitive": False},
            "field_email": {"label": "Contact", "question_key": "q_6", "field_type": "email", "input_type": "email", "autocomplete": "email", "is_sensitive": False},
            "field_addr": {"label": "Home", "question_key": "q_7", "field_type": "text", "input_type": "text", "autocomplete": "street-address", "is_sensitive": False},
            "field_comp": {"label": "Expected Salary & Compensation", "question_key": "q_8", "field_type": "text", "input_type": "text", "autocomplete": "", "is_sensitive": False},
            # opaque_custom intentionally omitted from field_metadata -> must fail closed!
        },
    )

    body = urlencode({**{k: v for k, v in sensitive_vals.items()}, "full_name": "Aarav Mehta", "csrf_token": "csrf-secret-xyz"}).encode("utf-8")
    # Also verify _build_captured_field_previews directly with file_checksums + metadata
    from app.services.browser_executor import _build_captured_field_previews, parse_submitted_request_payload

    text_fields, _files, wire_bytes = parse_submitted_request_payload(
        url="https://boards.greenhouse.io/acme/jobs/101",
        method="POST",
        headers={"content-type": "application/x-www-form-urlencoded"},
        post_data_buffer=body,
    )
    previews = _build_captured_field_previews(
        text_fields=text_fields,
        file_checksums={"resume": "a" * 64},
        field_metadata=guard._field_metadata,
    )
    by_name = {p.field_name: p for p in previews}
    assert by_name["full_name"].is_sensitive is False
    assert by_name["full_name"].value_preview == "Aarav Mehta"
    for k, raw_val in sensitive_vals.items():
        assert by_name[k].is_sensitive is True, f"{k} must be masked"
        assert raw_val not in (by_name[k].value_preview or ""), f"{k} leaked raw value!"
    assert by_name["csrf_token"].field_kind == "csrf_token"
    assert by_name["csrf_token"].is_sensitive is True
    assert "csrf-secret-xyz" not in (by_name["csrf_token"].value_preview or "")
    assert by_name["resume"].field_kind == "file"
    assert by_name["resume"].file_sha256 == "a" * 64
    assert by_name["resume"].value_preview is None
    assert len(wire_bytes) > 0
