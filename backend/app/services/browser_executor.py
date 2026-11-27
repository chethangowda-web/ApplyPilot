import base64
import ctypes
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from email.parser import BytesParser
from email.policy import default as _EMAIL_DEFAULT_POLICY
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import os
from pathlib import Path
import posixpath
import secrets
import select
import shutil
import signal
import socket
import subprocess
import sys
import threading
import uuid
from typing import Any
from urllib.parse import parse_qsl, urljoin, urlparse

from playwright.sync_api import Browser, BrowserContext, Route, WebSocketRoute

from app.agents.application.form_understanding import classify_sensitive_question
from app.connectors.base import BlockedSourceError, assert_permitted_url
from app.core.config import Settings, get_settings
from app.core.security import decrypt_sensitive_answer
from app.database.models import (
    Application,
    ApplicationAnswer,
    ApprovalRecord,
    CandidateProfile,
    Resume,
    ResumeJobMapping,
)
from app.schemas.applications import (
    ExecutionResult,
    FieldFillOutcome,
    HandoffSessionInfo,
    ManualSubmitAuthorizationRequest,
    PendingSubmissionFieldPreview,
    PendingSubmissionInfo,
    compute_answers_hash,
    compute_approval_snapshot_hash,
)
from app.services.execution_adapters import (
    ApplicationExecutionAdapter,
    ApprovalValidationError,
    ExecutionContext,
    ManualSubmitAuthorization,
    SSRFBlockedError,
    SubmitBlockedViolationError,
    UnsupportedPlatformError,
)

_ALLOWED_EXTERNAL_PORTS = frozenset({80, 443})
_CGNAT_NETWORK = ipaddress.IPv4Network("100.64.0.0/10")
_BLOCKED_HOSTNAMES = frozenset(
    {
        "localhost",
        "localhost.localdomain",
        "metadata",
        "metadata.google.internal",
        "metadata.goog",
        "instance-data",
    }
)
_BLOCKED_HOST_SUFFIXES = (".localhost", ".local", ".internal", ".home.arpa")
_LINKEDIN_DOMAINS = ("linkedin.com", "lnkd.in")

_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_PROCESS_SET_QUOTA = 0x0100
_PROCESS_TERMINATE = 0x0001
_SYNCHRONIZE = 0x00100000


def _is_disallowed_ip(ip_obj: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip_obj, ipaddress.IPv6Address) and ip_obj.ipv4_mapped is not None:
        return _is_disallowed_ip(ip_obj.ipv4_mapped)
    if (
        ip_obj.is_private
        or ip_obj.is_loopback
        or ip_obj.is_link_local
        or ip_obj.is_reserved
        or ip_obj.is_multicast
        or ip_obj.is_unspecified
    ):
        return True
    if isinstance(ip_obj, ipaddress.IPv4Address) and ip_obj in _CGNAT_NETWORK:
        return True
    return False


def _validate_hostname_and_port(
    host: str,
    port: int,
    *,
    allowed_test_origins: frozenset[tuple[str, int]] = frozenset(),
) -> str:
    norm_host = host.strip().rstrip(".").lower()
    if not norm_host or "%" in norm_host:
        raise SSRFBlockedError("Empty or zone-scoped target hostname is forbidden")
    if any(norm_host == dom or norm_host.endswith(f".{dom}") for dom in _LINKEDIN_DOMAINS):
        raise SSRFBlockedError("LinkedIn automation/scraping is prohibited")

    if (norm_host, port) in allowed_test_origins:
        return norm_host

    if port not in _ALLOWED_EXTERNAL_PORTS:
        raise SSRFBlockedError(f"Unsupported outbound port: {port}")
    if norm_host in _BLOCKED_HOSTNAMES or norm_host.endswith(_BLOCKED_HOST_SUFFIXES):
        raise SSRFBlockedError(f"Blocked internal/metadata hostname: {norm_host}")

    try:
        ip_literal = ipaddress.ip_address(norm_host)
    except ValueError:
        ip_literal = None
    if ip_literal is not None and _is_disallowed_ip(ip_literal):
        raise SSRFBlockedError(f"Blocked non-public IP literal: {ip_literal}")
    return norm_host


def validate_destination_url(
    url: str,
    *,
    allow_websocket: bool = False,
    allowed_test_origins: frozenset[tuple[str, int]] = frozenset(),
) -> tuple[str, str, int]:
    """Validate scheme, host, credentials, and port for browser/WebSocket egress."""
    parsed = urlparse((url or "").strip())
    scheme = parsed.scheme.lower()
    if parsed.username or parsed.password:
        raise SSRFBlockedError("URLs containing embedded credentials are forbidden")

    if scheme in {"http", "https"}:
        try:
            assert_permitted_url(url)
        except BlockedSourceError as exc:
            raise SSRFBlockedError(str(exc)) from exc
    elif allow_websocket and scheme in {"ws", "wss"}:
        pass
    else:
        raise SSRFBlockedError(f"Unsupported URL scheme: {parsed.scheme}")

    host = parsed.hostname or ""
    if parsed.port is not None:
        port = parsed.port
    elif scheme in {"https", "wss"}:
        port = 443
    else:
        port = 80

    norm_host = _validate_hostname_and_port(
        host,
        port,
        allowed_test_origins=allowed_test_origins,
    )
    return scheme, norm_host, port


def resolve_and_validate_host(
    host: str,
    port: int,
    *,
    allowed_test_origins: frozenset[tuple[str, int]] = frozenset(),
) -> list[tuple[Any, ...]]:
    """Resolve DNS once and validate EVERY returned IP before opening any outbound socket."""
    norm_host = _validate_hostname_and_port(
        host,
        port,
        allowed_test_origins=allowed_test_origins,
    )
    try:
        infos = socket.getaddrinfo(norm_host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise SSRFBlockedError(f"DNS resolution failed for {norm_host}: {exc}") from exc
    if not infos:
        raise SSRFBlockedError(f"DNS resolution returned no addresses for {norm_host}")

    is_explicit_test = (norm_host, port) in allowed_test_origins
    validated: list[tuple[Any, ...]] = []
    for info in infos:
        sockaddr = info[4]
        raw_ip = str(sockaddr[0]).split("%", 1)[0]
        try:
            ip_obj = ipaddress.ip_address(raw_ip)
        except ValueError as exc:
            raise SSRFBlockedError(f"Unparseable resolved IP for {norm_host}: {raw_ip}") from exc

        if not is_explicit_test and _is_disallowed_ip(ip_obj):
            raise SSRFBlockedError(
                f"DNS response for {norm_host} resolved to disallowed IP {ip_obj}"
            )
        validated.append(info)
    return validated


def _connect_validated_infos(
    infos: list[tuple[Any, ...]],
    *,
    timeout: float = 10.0,
) -> socket.socket:
    """Connect strictly using already-validated socket address tuples (no second DNS lookup)."""
    last_err: OSError | None = None
    for family, socktype, proto, _canonname, sockaddr in infos:
        sock: socket.socket | None = None
        try:
            sock = socket.socket(family, socktype, proto)
            sock.settimeout(timeout)
            sock.connect(sockaddr)
            return sock
        except OSError as exc:
            last_err = exc
            if sock is not None:
                sock.close()
    raise SSRFBlockedError(f"Outbound connection failed: {last_err}")


def _relay_sockets(client_sock: socket.socket, upstream_sock: socket.socket) -> None:
    try:
        sockets = [client_sock, upstream_sock]
        while True:
            readable, _, exceptional = select.select(sockets, [], sockets, 30.0)
            if exceptional or not readable:
                break
            for src in readable:
                dst = upstream_sock if src is client_sock else client_sock
                data = src.recv(16384)
                if not data:
                    return
                dst.sendall(data)
    except OSError:
        return
    finally:
        try:
            upstream_sock.close()
        except OSError:
            pass


class _ProxyServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, server_address, handler_cls, owner: "LocalSSRFEgressProxy") -> None:
        self.owner = owner
        super().__init__(server_address, handler_cls)


class _SSRFEgressProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return

    def _reject(self, code: int, reason: str) -> None:
        self.server.owner.record_blocked(self.path, reason)
        body = reason.encode("utf-8", errors="replace")
        self.send_response(code, "Forbidden")
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def do_CONNECT(self) -> None:
        target = (self.path or "").strip()
        if ":" not in target:
            self._reject(403, "CONNECT target missing port")
            return
        host, raw_port = target.rsplit(":", 1)
        host = host.strip("[]")
        try:
            port = int(raw_port)
        except ValueError:
            self._reject(403, "Invalid CONNECT port")
            return

        owner: LocalSSRFEgressProxy = self.server.owner
        try:
            infos = owner.resolve_and_validate(host, port)
            upstream = _connect_validated_infos(infos, timeout=owner.timeout_seconds)
        except SSRFBlockedError as exc:
            self._reject(403, str(exc))
            return

        owner.record_allowed(f"CONNECT {host}:{port}")
        self.send_response(200, "Connection Established")
        self.end_headers()
        self.close_connection = True
        _relay_sockets(self.connection, upstream)

    def _handle_http_proxy(self) -> None:
        owner: LocalSSRFEgressProxy = self.server.owner
        try:
            scheme, norm_host, port = validate_destination_url(
                self.path,
                allow_websocket=False,
                allowed_test_origins=owner._allowed_test_origins,
            )
            if scheme not in {"http", "https"}:
                raise SSRFBlockedError(f"Unsupported HTTP proxy scheme: {scheme}")
            infos = owner.resolve_and_validate(norm_host, port)
            upstream = _connect_validated_infos(infos, timeout=owner.timeout_seconds)
        except SSRFBlockedError as exc:
            self._reject(403, str(exc))
            return

        owner.record_allowed(self.path)
        parsed = urlparse(self.path)
        rel_path = parsed.path or "/"
        if parsed.query:
            rel_path = f"{rel_path}?{parsed.query}"

        content_len_raw = (self.headers.get("Content-Length") or "0").strip()
        try:
            content_len = int(content_len_raw)
        except ValueError:
            self._reject(400, "Invalid Content-Length header")
            return
        if content_len < 0:
            self._reject(400, "Negative Content-Length header")
            return
        body = self.rfile.read(content_len) if content_len > 0 else b""

        fwd_headers = []
        for hk, hv in self.headers.items():
            if hk.lower() in {"proxy-connection", "connection", "keep-alive"}:
                continue
            fwd_headers.append(f"{hk}: {hv}")
        if "host" not in {k.lower() for k in self.headers.keys()}:
            fwd_headers.append(f"Host: {norm_host}:{port}")
        fwd_headers.append("Connection: close")

        raw_req = (
            f"{self.command} {rel_path} HTTP/1.1\r\n"
            + "\r\n".join(fwd_headers)
            + "\r\n\r\n"
        ).encode("latin-1") + body

        try:
            upstream.sendall(raw_req)
            upstream.settimeout(owner.timeout_seconds)
            chunks: list[bytes] = []
            while True:
                buf = upstream.recv(16384)
                if not buf:
                    break
                chunks.append(buf)
            response_bytes = b"".join(chunks)
            if response_bytes:
                self.connection.sendall(response_bytes)
        finally:
            try:
                upstream.close()
            except OSError:
                pass
            self.close_connection = True

    def do_GET(self) -> None:
        self._handle_http_proxy()

    def do_HEAD(self) -> None:
        self._handle_http_proxy()

    def do_OPTIONS(self) -> None:
        self._handle_http_proxy()

    def do_POST(self) -> None:
        self._handle_http_proxy()

    def do_PUT(self) -> None:
        self._handle_http_proxy()

    def do_PATCH(self) -> None:
        self._handle_http_proxy()

    def do_DELETE(self) -> None:
        self._handle_http_proxy()


class LocalSSRFEgressProxy:
    """Enforced local loopback proxy resolving DNS once and connecting strictly by validated IP."""

    def __init__(
        self,
        *,
        timeout_seconds: float = 10.0,
        _allowed_test_origins: frozenset[tuple[str, int]] = frozenset(),
    ) -> None:
        self.timeout_seconds = timeout_seconds
        # Private test-harness-only origin exemption; never exposed in Settings or ExecutionOptions.
        self._allowed_test_origins = _allowed_test_origins
        self._lock = threading.Lock()
        self._server: _ProxyServer | None = None
        self._thread: threading.Thread | None = None
        self.allowed_targets: list[str] = []
        self.blocked_targets: list[tuple[str, str]] = []
        self._running = False

    @property
    def port(self) -> int:
        if self._server is None:
            raise RuntimeError("LocalSSRFEgressProxy has not been started")
        return int(self._server.server_address[1])

    @property
    def is_running(self) -> bool:
        return self._running

    def start(self) -> int:
        if self._running:
            return self.port
        self._server = _ProxyServer(("127.0.0.1", 0), _SSRFEgressProxyHandler, self)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        self._running = True
        return self.port

    def stop(self) -> None:
        self._running = False
        if self._server is not None:
            try:
                self._server.shutdown()
            finally:
                self._server.server_close()
                self._server = None
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=2.0)
            self._thread = None

    def record_allowed(self, target: str) -> None:
        with self._lock:
            self.allowed_targets.append(target)

    def record_blocked(self, target: str, reason: str) -> None:
        with self._lock:
            self.blocked_targets.append((target, reason))

    def validate_url(self, url: str, *, allow_websocket: bool = False) -> tuple[str, str, int]:
        return validate_destination_url(
            url,
            allow_websocket=allow_websocket,
            allowed_test_origins=self._allowed_test_origins,
        )

    def resolve_and_validate(self, host: str, port: int) -> list[tuple[Any, ...]]:
        return resolve_and_validate_host(
            host,
            port,
            allowed_test_origins=self._allowed_test_origins,
        )

    def __enter__(self) -> "LocalSSRFEgressProxy":
        self.start()
        return self

    def __exit__(self, *args: Any) -> None:
        self.stop()


def build_protected_chromium_args() -> list[str]:
    """Return Chromium launch flags disabling loopback proxy bypass, direct DNS, QUIC, and WebRTC UDP."""
    return [
        "--proxy-bypass-list=<-loopback>",
        "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1",
        "--disable-quic",
        "--webrtc-ip-handling-policy=disable_non_proxied_udp",
        "--force-webrtc-ip-handling-policy",
        "--enforce-webrtc-ip-permission-check",
        "--disable-background-networking",
        "--disable-features=AsyncDns",
    ]


def create_isolated_browser_context(
    browser: Browser,
    *,
    egress_proxy: LocalSSRFEgressProxy,
) -> BrowserContext:
    """Create an isolated ephemeral BrowserContext with proxy routing, SW block, and WS/HTTP filters."""
    if not egress_proxy.is_running:
        raise SSRFBlockedError("Egress proxy is not running; refusing browser context creation")

    ctx = browser.new_context(
        proxy={"server": f"http://127.0.0.1:{egress_proxy.port}"},
        service_workers="block",
        ignore_https_errors=False,
    )
    ctx.add_init_script(
        "try { Object.defineProperty(Navigator.prototype, 'serviceWorker', "
        "{ get: () => undefined, configurable: false }); } catch (_) {}"
    )

    def _route_http(route: Route) -> None:
        if not egress_proxy.is_running:
            egress_proxy.record_blocked(route.request.url, "Egress proxy shut down")
            route.abort("blockedbyclient")
            return
        if getattr(route.request, "resource_type", "") == "serviceworker":
            egress_proxy.record_blocked(route.request.url, "Service worker script blocked")
            route.abort("blockedbyclient")
            return
        url = route.request.url
        if url == "about:blank" or url.startswith("data:") or url.startswith("blob:"):
            route.continue_()
            return
        try:
            _scheme, host, port = egress_proxy.validate_url(url, allow_websocket=False)
            egress_proxy.resolve_and_validate(host, port)
        except SSRFBlockedError as exc:
            egress_proxy.record_blocked(url, str(exc))
            route.abort("blockedbyclient")
            return
        route.continue_()

    def _route_ws(ws: WebSocketRoute) -> None:
        if not egress_proxy.is_running:
            egress_proxy.record_blocked(ws.url, "Egress proxy shut down")
            ws.close(code=1008, reason="Egress proxy unavailable")
            return
        try:
            _scheme, host, port = egress_proxy.validate_url(ws.url, allow_websocket=True)
            egress_proxy.resolve_and_validate(host, port)
        except SSRFBlockedError as exc:
            egress_proxy.record_blocked(ws.url, str(exc))
            ws.close(code=1008, reason="WebSocket blocked by SSRF egress policy")
            return
        ws.connect_to_server()

    ctx.route("**/*", _route_http)
    ctx.route_web_socket("**/*", _route_ws)
    # Expose SSRF checker cleanly so layered handlers (e.g., submit protection) preserve SSRF checks.
    ctx._applypilot_ssrf_check = _route_http  # type: ignore[attr-defined]
    return ctx


@dataclass(frozen=True)
class FirewallVerificationStatus:
    enforced: bool
    reason: str
    rules_checked: dict[str, dict[str, str]] = field(default_factory=dict)


def _parse_netsh_rule(stdout: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for line in stdout.splitlines():
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        key = k.strip()
        val = v.strip()
        if key:
            parsed[key] = val
    return parsed


def _profiles_cover_all(profiles_str: str) -> bool:
    tokens = {p.strip().lower() for p in profiles_str.split(",") if p.strip()}
    return {"domain", "private", "public"}.issubset(tokens)


def verify_windows_egress_firewall(
    *,
    rule_prefix: str = "ApplyPilot-Browser-Egress",
    chromium_path: str | None = None,
) -> FirewallVerificationStatus:
    """Verify actual active Windows Defender Firewall rules (never checks rule names alone)."""
    if sys.platform != "win32":
        return FirewallVerificationStatus(
            enforced=False,
            reason=f"Windows Defender Firewall unavailable on platform={sys.platform}",
        )

    prof_proc = subprocess.run(
        ["netsh", "advfirewall", "show", "allprofiles", "state"],
        capture_output=True,
        text=True,
        check=False,
    )
    if prof_proc.returncode != 0:
        return FirewallVerificationStatus(
            enforced=False,
            reason=f"Failed to query Windows Firewall profile states (rc={prof_proc.returncode})",
        )

    on_count = sum(1 for ln in prof_proc.stdout.splitlines() if ln.strip().upper().startswith("STATE") and "ON" in ln.upper())
    if on_count < 3:
        return FirewallVerificationStatus(
            enforced=False,
            reason="One or more Windows Defender Firewall profiles (Domain/Private/Public) are OFF",
        )

    expected_specs = {
        f"{rule_prefix}-Allow-Loopback-TCP": ("Allow", "Out", "TCP", "127.0.0.1"),
        f"{rule_prefix}-Block-External-IPv4-TCP": ("Block", "Out", "TCP", "0.0.0.0"),
        f"{rule_prefix}-Block-External-IPv6-TCP": ("Block", "Out", "TCP", "::"),
        f"{rule_prefix}-Block-All-UDP": ("Block", "Out", "UDP", "Any"),
    }

    norm_chrome = os.path.normcase(os.path.normpath(chromium_path)) if chromium_path else None
    checked: dict[str, dict[str, str]] = {}

    for rule_name, (exp_action, exp_dir, exp_proto, exp_remote_marker) in expected_specs.items():
        proc = subprocess.run(
            ["netsh", "advfirewall", "firewall", "show", "rule", f"name={rule_name}", "verbose"],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            return FirewallVerificationStatus(
                enforced=False,
                reason=f"Required firewall rule missing: {rule_name}",
                rules_checked=checked,
            )

        attrs = _parse_netsh_rule(proc.stdout)
        checked[rule_name] = attrs

        if attrs.get("Enabled", "").lower() != "yes":
            return FirewallVerificationStatus(enforced=False, reason=f"Rule {rule_name} is not Enabled", rules_checked=checked)
        if attrs.get("Direction", "").lower() != exp_dir.lower():
            return FirewallVerificationStatus(enforced=False, reason=f"Rule {rule_name} has wrong Direction", rules_checked=checked)
        if attrs.get("Action", "").lower() != exp_action.lower():
            return FirewallVerificationStatus(enforced=False, reason=f"Rule {rule_name} has wrong Action", rules_checked=checked)
        if attrs.get("Protocol", "").lower() != exp_proto.lower():
            return FirewallVerificationStatus(enforced=False, reason=f"Rule {rule_name} has wrong Protocol", rules_checked=checked)
        if not _profiles_cover_all(attrs.get("Profiles", "")):
            return FirewallVerificationStatus(enforced=False, reason=f"Rule {rule_name} does not cover all profiles", rules_checked=checked)

        local_port = attrs.get("LocalPort", "Any").strip().lower()
        remote_port = attrs.get("RemotePort", "Any").strip().lower()
        if local_port != "any" or remote_port != "any":
            return FirewallVerificationStatus(
                enforced=False,
                reason=f"Rule {rule_name} must apply to All ports (got LocalPort={local_port}, RemotePort={remote_port})",
                rules_checked=checked,
            )

        remote_ip = attrs.get("RemoteIP", "")
        remote_tokens = {tok.strip().lower() for tok in remote_ip.split(",") if tok.strip()}
        if exp_remote_marker == "127.0.0.1":
            if not remote_tokens or not remote_tokens.issubset({"127.0.0.1", "127.0.0.1/32", "127.0.0.1-127.0.0.1"}):
                return FirewallVerificationStatus(
                    enforced=False,
                    reason=f"Rule {rule_name} RemoteIP must strictly equal 127.0.0.1 (got {remote_ip!r})",
                    rules_checked=checked,
                )
        elif exp_remote_marker == "0.0.0.0":
            has_low = any(t in remote_tokens for t in ("0.0.0.0-127.0.0.0", "0.0.0.0-126.255.255.255"))
            has_high = any(t in remote_tokens for t in ("127.0.0.2-255.255.255.255", "128.0.0.0-255.255.255.255"))
            if not (has_low and has_high):
                return FirewallVerificationStatus(
                    enforced=False,
                    reason=f"Rule {rule_name} missing complete non-loopback IPv4 ranges (got {remote_ip!r})",
                    rules_checked=checked,
                )
        elif exp_remote_marker == "::":
            if not any(t in remote_tokens for t in ("::/0", "any")):
                return FirewallVerificationStatus(
                    enforced=False,
                    reason=f"Rule {rule_name} must cover all IPv6 addresses (::/0 or Any, got {remote_ip!r})",
                    rules_checked=checked,
                )
        elif exp_remote_marker == "Any":
            if "any" not in remote_tokens:
                return FirewallVerificationStatus(enforced=False, reason=f"Rule {rule_name} must block Any remote UDP", rules_checked=checked)

        prog_raw = attrs.get("Program", "").strip()
        prog = os.path.normcase(os.path.normpath(prog_raw)) if prog_raw else ""
        if not prog or prog == "any":
            return FirewallVerificationStatus(
                enforced=False,
                reason=f"Rule {rule_name} must be scoped to a specific Chromium executable (got {prog_raw!r})",
                rules_checked=checked,
            )
        if norm_chrome is not None and prog != norm_chrome:
            return FirewallVerificationStatus(
                enforced=False,
                reason=f"Rule {rule_name} Program ({prog}) does not match Chromium ({norm_chrome})",
                rules_checked=checked,
            )
        if norm_chrome is None and not prog.endswith("chrome.exe"):
            return FirewallVerificationStatus(
                enforced=False,
                reason=f"Rule {rule_name} Program ({prog}) does not target chrome.exe",
                rules_checked=checked,
            )

    return FirewallVerificationStatus(
        enforced=True,
        reason="Active Windows Defender Firewall browser egress rules verified",
        rules_checked=checked,
    )


def get_process_creation_time(pid: int) -> int | None:
    """Return Windows FILETIME creation timestamp for PID, or None if unavailable/exited."""
    if sys.platform != "win32" or pid <= 0:
        return None
    import ctypes.wintypes as w

    k32 = ctypes.windll.kernel32
    k32.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
    k32.OpenProcess.restype = w.HANDLE
    k32.GetProcessTimes.argtypes = [
        w.HANDLE,
        ctypes.POINTER(w.FILETIME),
        ctypes.POINTER(w.FILETIME),
        ctypes.POINTER(w.FILETIME),
        ctypes.POINTER(w.FILETIME),
    ]
    k32.GetProcessTimes.restype = w.BOOL
    k32.CloseHandle.argtypes = [w.HANDLE]
    k32.CloseHandle.restype = w.BOOL

    handle = k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        c, e, k, u = w.FILETIME(), w.FILETIME(), w.FILETIME(), w.FILETIME()
        if not k32.GetProcessTimes(handle, ctypes.byref(c), ctypes.byref(e), ctypes.byref(k), ctypes.byref(u)):
            return None
        return (int(c.dwHighDateTime) << 32) | int(c.dwLowDateTime)
    finally:
        k32.CloseHandle(handle)


def terminate_verified_process(pid: int, *, expected_creation_time: int | None = None, pgid: int | None = None) -> bool:
    """Terminate a tracked browser process only after verifying PID + creation-time identity."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        if expected_creation_time is None:
            return False
        actual_ct = get_process_creation_time(pid)
        if actual_ct is None or actual_ct != expected_creation_time:
            return False
        import ctypes.wintypes as w

        k32 = ctypes.windll.kernel32
        k32.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
        k32.OpenProcess.restype = w.HANDLE
        k32.TerminateProcess.argtypes = [w.HANDLE, w.UINT]
        k32.TerminateProcess.restype = w.BOOL
        k32.CloseHandle.argtypes = [w.HANDLE]
        k32.CloseHandle.restype = w.BOOL

        handle = k32.OpenProcess(_PROCESS_TERMINATE | _SYNCHRONIZE, False, pid)
        if not handle:
            return False
        try:
            return bool(k32.TerminateProcess(handle, 1))
        finally:
            k32.CloseHandle(handle)

    # POSIX best-effort process-group cleanup (does NOT auto-terminate on parent SIGKILL like Windows Job Objects)
    sig_kill = getattr(signal, "SIGKILL", signal.SIGTERM)
    try:
        if pgid and hasattr(os, "killpg"):
            os.killpg(pgid, sig_kill)
        else:
            os.kill(pid, sig_kill)
        return True
    except ProcessLookupError:
        return False


class Win32JobSupervisor:
    """OS-level Windows Job Object supervisor with KILL_ON_JOB_CLOSE and POSIX fallback."""

    def __init__(self) -> None:
        self._job_handle: Any = None
        self._assigned_pids: dict[int, int] = {}
        self._pgids: dict[int, int] = {}
        self.uses_kernel_job_object = sys.platform == "win32"

        if self.uses_kernel_job_object:
            import ctypes.wintypes as w

            class _IO_COUNTERS(ctypes.Structure):
                _fields_ = [
                    ("ReadOperationCount", ctypes.c_uint64),
                    ("WriteOperationCount", ctypes.c_uint64),
                    ("OtherOperationCount", ctypes.c_uint64),
                    ("ReadTransferCount", ctypes.c_uint64),
                    ("WriteTransferCount", ctypes.c_uint64),
                    ("OtherTransferCount", ctypes.c_uint64),
                ]

            class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
                _fields_ = [
                    ("PerProcessUserTimeLimit", ctypes.c_int64),
                    ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", w.DWORD),
                    ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t),
                    ("ActiveProcessLimit", w.DWORD),
                    ("Affinity", ctypes.c_size_t),
                    ("PriorityClass", w.DWORD),
                    ("SchedulingClass", w.DWORD),
                ]

            class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
                _fields_ = [
                    ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
                    ("IoInfo", _IO_COUNTERS),
                    ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t),
                ]

            k32 = ctypes.windll.kernel32
            k32.CreateJobObjectW.argtypes = [w.LPVOID, w.LPCWSTR]
            k32.CreateJobObjectW.restype = w.HANDLE
            k32.SetInformationJobObject.argtypes = [w.HANDLE, ctypes.c_int, w.LPVOID, w.DWORD]
            k32.SetInformationJobObject.restype = w.BOOL

            job = k32.CreateJobObjectW(None, None)
            if not job:
                raise OSError("Failed to create Windows Job Object")

            info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
            info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            ok = k32.SetInformationJobObject(
                job,
                _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
                ctypes.byref(info),
                ctypes.sizeof(info),
            )
            if not ok:
                k32.CloseHandle.argtypes = [w.HANDLE]
                k32.CloseHandle.restype = w.BOOL
                k32.CloseHandle(job)
                raise OSError("Failed to configure KILL_ON_JOB_CLOSE on Windows Job Object")
            self._job_handle = job

    def assign_process(self, pid: int) -> int | None:
        """Assign PID to Job Object (Windows) or track pgid (POSIX); returns Windows creation time if available."""
        if self.uses_kernel_job_object and self._job_handle is not None:
            import ctypes.wintypes as w

            creation_time = get_process_creation_time(pid)
            if creation_time is None:
                raise ProcessLookupError(f"Cannot inspect creation time for PID {pid}")

            k32 = ctypes.windll.kernel32
            k32.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
            k32.OpenProcess.restype = w.HANDLE
            k32.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
            k32.AssignProcessToJobObject.restype = w.BOOL
            k32.CloseHandle.argtypes = [w.HANDLE]
            k32.CloseHandle.restype = w.BOOL

            proc_handle = k32.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, pid)
            if not proc_handle:
                raise OSError(f"Failed to open PID {pid} for Job Object assignment")
            try:
                if not k32.AssignProcessToJobObject(self._job_handle, proc_handle):
                    raise OSError(f"AssignProcessToJobObject failed for PID {pid}")
            finally:
                k32.CloseHandle(proc_handle)

            self._assigned_pids[pid] = creation_time
            return creation_time

        if hasattr(os, "getpgid"):
            self._pgids[pid] = os.getpgid(pid)
        return None

    def spawn_suspended_in_job(
        self,
        cmd: list[str],
        *,
        cwd: str | Path | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> tuple[int, int | None]:
        """Spawn a process in CREATE_SUSPENDED state, assign to Job Object, then resume.

        Prevents pre-assignment execution races and inherits Job containment to child processes.
        """
        if self.uses_kernel_job_object and self._job_handle is not None:
            import ctypes.wintypes as w

            class _STARTUPINFOW(ctypes.Structure):
                _fields_ = [
                    ("cb", w.DWORD),
                    ("lpReserved", w.LPWSTR),
                    ("lpDesktop", w.LPWSTR),
                    ("lpTitle", w.LPWSTR),
                    ("dwX", w.DWORD),
                    ("dwY", w.DWORD),
                    ("dwXSize", w.DWORD),
                    ("dwYSize", w.DWORD),
                    ("dwXCountChars", w.DWORD),
                    ("dwYCountChars", w.DWORD),
                    ("dwFillAttribute", w.DWORD),
                    ("dwFlags", w.DWORD),
                    ("wShowWindow", w.WORD),
                    ("cbReserved2", w.WORD),
                    ("lpReserved2", ctypes.POINTER(w.BYTE)),
                    ("hStdInput", w.HANDLE),
                    ("hStdOutput", w.HANDLE),
                    ("hStdError", w.HANDLE),
                ]

            class _PROCESS_INFORMATION(ctypes.Structure):
                _fields_ = [
                    ("hProcess", w.HANDLE),
                    ("hThread", w.HANDLE),
                    ("dwProcessId", w.DWORD),
                    ("dwThreadId", w.DWORD),
                ]

            create_suspended = 0x00000004
            create_new_process_group = 0x00000200
            create_unicode_environment = 0x00000400
            k32 = ctypes.windll.kernel32
            k32.CreateProcessW.argtypes = [
                w.LPCWSTR,
                w.LPWSTR,
                w.LPVOID,
                w.LPVOID,
                w.BOOL,
                w.DWORD,
                w.LPVOID,
                w.LPCWSTR,
                ctypes.POINTER(_STARTUPINFOW),
                ctypes.POINTER(_PROCESS_INFORMATION),
            ]
            k32.CreateProcessW.restype = w.BOOL
            k32.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
            k32.AssignProcessToJobObject.restype = w.BOOL
            k32.ResumeThread.argtypes = [w.HANDLE]
            k32.ResumeThread.restype = w.DWORD
            k32.TerminateProcess.argtypes = [w.HANDLE, w.UINT]
            k32.TerminateProcess.restype = w.BOOL
            k32.CloseHandle.argtypes = [w.HANDLE]
            k32.CloseHandle.restype = w.BOOL
            k32.GetProcessTimes.argtypes = [
                w.HANDLE,
                ctypes.POINTER(w.FILETIME),
                ctypes.POINTER(w.FILETIME),
                ctypes.POINTER(w.FILETIME),
                ctypes.POINTER(w.FILETIME),
            ]
            k32.GetProcessTimes.restype = w.BOOL

            si = _STARTUPINFOW()
            si.cb = ctypes.sizeof(_STARTUPINFOW)
            pi = _PROCESS_INFORMATION()
            cmd_buf = ctypes.create_unicode_buffer(subprocess.list2cmdline(cmd))
            cwd_str = str(cwd) if cwd is not None else None
            env_buf = None
            flags = create_suspended | create_new_process_group
            if extra_env:
                merged_env = dict(os.environ)
                merged_env.update(extra_env)
                env_entries = [
                    f"{k}={v}"
                    for k, v in sorted(merged_env.items(), key=lambda kv: kv[0].upper())
                    if k and "\0" not in k and "\0" not in str(v)
                ]
                env_block = "\0".join(env_entries) + "\0\0"
                env_buf = ctypes.create_unicode_buffer(env_block)
                flags |= create_unicode_environment

            created = k32.CreateProcessW(
                None,
                cmd_buf,
                None,
                None,
                False,
                flags,
                env_buf,
                cwd_str,
                ctypes.byref(si),
                ctypes.byref(pi),
            )
            if not created:
                raise OSError(f"CreateProcessW failed for command: {cmd[0]!r}")

            try:
                if not k32.AssignProcessToJobObject(self._job_handle, pi.hProcess):
                    k32.TerminateProcess(pi.hProcess, 1)
                    raise OSError(f"AssignProcessToJobObject failed for suspended PID {pi.dwProcessId}")

                c, e, k, u = w.FILETIME(), w.FILETIME(), w.FILETIME(), w.FILETIME()
                creation_time: int | None = None
                if k32.GetProcessTimes(pi.hProcess, ctypes.byref(c), ctypes.byref(e), ctypes.byref(k), ctypes.byref(u)):
                    creation_time = (int(c.dwHighDateTime) << 32) | int(c.dwLowDateTime)
                if creation_time is None:
                    k32.TerminateProcess(pi.hProcess, 1)
                    raise OSError(f"GetProcessTimes failed for suspended PID {pi.dwProcessId}")

                prev_suspend = k32.ResumeThread(pi.hThread)
                if prev_suspend == 0xFFFFFFFF:
                    k32.TerminateProcess(pi.hProcess, 1)
                    raise OSError(f"ResumeThread failed for PID {pi.dwProcessId}")

                pid = int(pi.dwProcessId)
                self._assigned_pids[pid] = creation_time
                return pid, creation_time
            finally:
                k32.CloseHandle(pi.hThread)
                k32.CloseHandle(pi.hProcess)

        posix_env = None
        if extra_env:
            posix_env = dict(os.environ)
            posix_env.update(extra_env)
        proc = subprocess.Popen(cmd, cwd=cwd, env=posix_env, start_new_session=True)
        self._pgids[proc.pid] = proc.pid
        return proc.pid, None

    def close(self, *, terminate_processes: bool = True) -> None:
        if self.uses_kernel_job_object and self._job_handle is not None:
            import ctypes.wintypes as w

            k32 = ctypes.windll.kernel32
            k32.TerminateJobObject.argtypes = [w.HANDLE, w.UINT]
            k32.TerminateJobObject.restype = w.BOOL
            k32.CloseHandle.argtypes = [w.HANDLE]
            k32.CloseHandle.restype = w.BOOL
            try:
                if terminate_processes:
                    k32.TerminateJobObject(self._job_handle, 1)
            finally:
                k32.CloseHandle(self._job_handle)
                self._job_handle = None
                self._assigned_pids.clear()
            return

        if terminate_processes:
            for pid, pgid in list(self._pgids.items()):
                terminate_verified_process(pid, pgid=pgid)
        self._pgids.clear()


@dataclass(frozen=True)
class ProcessSessionManifest:
    session_id: str
    application_id: str
    user_id: str
    pid: int
    creation_time: int | None
    pgid: int | None
    temp_profile_dir: str
    created_at: str
    expires_at: str


class BrowserProcessRegistry:
    """Persistent crash-recovery manifest ledger for supervised browser processes."""

    def __init__(self, storage_root: Path) -> None:
        self.sessions_dir = (storage_root.resolve() / "runtime" / "browser_sessions").resolve()
        self.sessions_dir.mkdir(parents=True, exist_ok=True)

    def _manifest_path(self, session_id: str) -> Path:
        safe_id = str(uuid.UUID(session_id))
        return self.sessions_dir / f"{safe_id}.json"

    def write_manifest(self, manifest: ProcessSessionManifest) -> Path:
        path = self._manifest_path(manifest.session_id)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(asdict(manifest), ensure_ascii=True), encoding="utf-8")
        tmp.replace(path)
        return path

    def remove_manifest(self, session_id: str, *, cleanup_profile_dir: bool = True) -> None:
        path = self._manifest_path(session_id)
        if not path.is_file():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            data = {}
        if cleanup_profile_dir and data.get("temp_profile_dir"):
            prof = Path(str(data["temp_profile_dir"]))
            if prof.is_dir():
                shutil.rmtree(prof, ignore_errors=True)
        path.unlink(missing_ok=True)

    def reap_abandoned_sessions(
        self,
        *,
        now: datetime | None = None,
        force_reap_unexpired: bool = False,
    ) -> list[str]:
        """Reap expired or orphaned manifests, verifying PID + creation_time before terminating."""
        current = now or datetime.now(timezone.utc)
        reaped: list[str] = []

        for path in sorted(self.sessions_dir.glob("*.json")):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                expires_at = datetime.fromisoformat(str(raw["expires_at"]))
                pid = int(raw["pid"])
                creation_time = raw.get("creation_time")
                pgid = raw.get("pgid")
                sid = str(raw["session_id"])
            except Exception:
                path.unlink(missing_ok=True)
                continue

            expired = expires_at <= current
            if sys.platform == "win32":
                actual_ct = get_process_creation_time(pid)
                process_gone_or_reused = actual_ct is None or (creation_time is not None and actual_ct != int(creation_time))
            else:
                process_gone_or_reused = False
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    process_gone_or_reused = True
                except PermissionError:
                    process_gone_or_reused = False

            if not (expired or force_reap_unexpired or process_gone_or_reused):
                continue

            if not process_gone_or_reused:
                terminate_verified_process(
                    pid,
                    expected_creation_time=int(creation_time) if creation_time is not None else None,
                    pgid=int(pgid) if pgid is not None else None,
                )

            temp_dir = str(raw.get("temp_profile_dir") or "")
            if temp_dir and Path(temp_dir).is_dir():
                shutil.rmtree(temp_dir, ignore_errors=True)
            path.unlink(missing_ok=True)
            reaped.append(sid)

        return reaped


def _as_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _normalize_compare_url(url: str) -> str:
    parsed = urlparse((url or "").strip())
    raw_path = parsed.path or "/"
    norm_path = posixpath.normpath(raw_path)
    if norm_path != "/":
        norm_path = norm_path.rstrip("/") or "/"
    if not norm_path.startswith("/"):
        norm_path = "/" + norm_path
    port = parsed.port
    default_port = 443 if parsed.scheme.lower() == "https" else 80
    netloc = (parsed.hostname or "").lower()
    if port is not None and port != default_port:
        netloc = f"{netloc}:{port}"
    return f"{parsed.scheme.lower()}://{netloc}{norm_path}"


def _normalize_compare_url_with_query(url: str) -> str:
    parsed = urlparse((url or "").strip())
    base = _normalize_compare_url(url)
    return f"{base}?{parsed.query}" if parsed.query else base


def validate_resume_for_execution(
    *,
    user_id: uuid.UUID,
    application: Application,
    profile: CandidateProfile,
    resume: Resume,
    mapping: ResumeJobMapping | None,
    storage_root: Path,
) -> Path:
    """Strictly validate ownership, approval, completion, path confinement, and live PDF checksum."""
    if application.user_id != user_id or profile.user_id != user_id:
        raise ApprovalValidationError("Application or profile ownership validation failed")
    if resume.profile_id != profile.id:
        raise ApprovalValidationError("Resume does not belong to candidate profile")
    if resume.is_master:
        raise ApprovalValidationError("Master resume cannot be attached as tailored application resume")
    if (
        mapping is None
        or mapping.resume_id != resume.id
        or mapping.job_id != application.job_id
        or mapping.profile_id != profile.id
        or mapping.status != "completed"
        or mapping.approval_status != "approved"
    ):
        raise ApprovalValidationError("Resume does not have an approved tailored ResumeJobMapping for this job")
    if resume.generation_status != "completed" or not resume.validation_passed:
        raise ApprovalValidationError("Tailored resume failed factual validation or is incomplete")
    if not resume.pdf_path:
        raise ApprovalValidationError("Approved tailored resume PDF artifact is missing")

    raw_path = Path(resume.pdf_path)
    if ".." in raw_path.parts or raw_path.is_symlink():
        raise ApprovalValidationError("Invalid resume artifact path")

    root_resolved = storage_root.resolve()
    try:
        candidate = raw_path.resolve(strict=True)
    except OSError as exc:
        raise ApprovalValidationError("Approved resume artifact is not accessible") from exc

    if candidate.is_symlink() or root_resolved not in candidate.parents or not candidate.is_file():
        raise ApprovalValidationError("Resume artifact fails storage confinement")

    actual_sha256 = hashlib.sha256(candidate.read_bytes()).hexdigest()
    if not hmac.compare_digest(actual_sha256, resume.sha256_checksum):
        raise ApprovalValidationError("Resume PDF checksum verification failed")
    return candidate


def load_verified_resume_snapshot(
    *,
    user_id: uuid.UUID,
    application: Application,
    profile: CandidateProfile,
    resume: Resume,
    mapping: ResumeJobMapping | None,
    storage_root: Path,
) -> tuple[Path, bytes, str]:
    """Validate resume confinement and capture an immutable SHA-256-verified in-memory PDF snapshot.

    Reading once into memory and verifying checksum on that exact byte buffer defeats post-validation
    disk-swap TOCTOU races before Playwright uploads the file.
    """
    candidate = validate_resume_for_execution(
        user_id=user_id,
        application=application,
        profile=profile,
        resume=resume,
        mapping=mapping,
        storage_root=storage_root,
    )
    try:
        with open(candidate, "rb") as fh:
            pdf_bytes = fh.read()
    except OSError as exc:
        raise ApprovalValidationError("Approved resume artifact is not readable") from exc

    if not pdf_bytes:
        raise ApprovalValidationError("Approved tailored resume PDF is empty")

    actual_sha256 = hashlib.sha256(pdf_bytes).hexdigest()
    if not hmac.compare_digest(actual_sha256, resume.sha256_checksum):
        raise ApprovalValidationError("Resume PDF checksum verification failed during snapshot read")
    return candidate, pdf_bytes, actual_sha256


def validate_approval_and_answers(
    *,
    application: Application,
    user_id: uuid.UUID,
    resume: Resume,
    verified_resume_checksum: str,
    approval_record: ApprovalRecord | None,
    answers: list[ApplicationAnswer],
    settings: Settings | None = None,
    now: datetime | None = None,
) -> tuple[dict[str, str], set[str], set[str], set[str], str]:
    """Validate approval freshness/snapshot hash and decrypt sensitive answers strictly in memory."""
    cfg = settings or get_settings()
    current_time = now or datetime.now(timezone.utc)

    if approval_record is None or approval_record.application_id != application.id:
        raise ApprovalValidationError("No ApprovalRecord found for application")
    if approval_record.decision != "approved":
        raise ApprovalValidationError(f"ApprovalRecord decision is not approved ({approval_record.decision})")
    if _as_utc(approval_record.expires_at) <= current_time:
        raise ApprovalValidationError("ApprovalRecord has expired")
    if approval_record.resume_id != resume.id:
        raise ApprovalValidationError("ApprovalRecord resume_id does not match application resume")
    if not hmac.compare_digest(approval_record.resume_checksum, verified_resume_checksum):
        raise ApprovalValidationError("ApprovalRecord resume_checksum does not match verified PDF checksum")
    if approval_record.destination_url != application.destination_url:
        raise ApprovalValidationError("ApprovalRecord destination_url does not match application")
    if approval_record.application_mode != application.application_mode:
        raise ApprovalValidationError("ApprovalRecord application_mode does not match application")

    verified_answers: dict[str, str] = {}
    verified_keys: set[str] = set()
    unverified_keys: set[str] = set()
    sensitive_unconfirmed_keys: set[str] = set()
    hash_pairs: list[tuple[str, str]] = []

    for ans in answers:
        if ans.is_sensitive:
            if not ans.answer_encrypted or not ans.key_version:
                raise ApprovalValidationError(f"Sensitive answer {ans.question_key!r} is missing encrypted payload")
            plaintext = decrypt_sensitive_answer(
                ans.answer_encrypted,
                key_version=ans.key_version,
                application_id=str(application.id),
                question_key=ans.question_key,
                settings=cfg,
            )
            hash_pairs.append((ans.question_key, plaintext))
            if ans.user_verified:
                verified_answers[ans.question_key] = plaintext
                verified_keys.add(ans.question_key)
            else:
                sensitive_unconfirmed_keys.add(ans.question_key)
                unverified_keys.add(ans.question_key)
        else:
            plain = (ans.answer_plaintext or "").strip()
            hash_pairs.append((ans.question_key, plain))
            if ans.user_verified and plain:
                verified_answers[ans.question_key] = plain
                verified_keys.add(ans.question_key)
            else:
                unverified_keys.add(ans.question_key)

    answers_hash = compute_answers_hash(hash_pairs)
    expected_snapshot = compute_approval_snapshot_hash(
        application_id=application.id,
        user_id=user_id,
        resume_id=resume.id,
        resume_checksum=verified_resume_checksum,
        canonical_job_key=application.canonical_job_key,
        destination_url=application.destination_url,
        answers_hash=answers_hash,
        application_mode=application.application_mode,
    )
    if not hmac.compare_digest(expected_snapshot, approval_record.approval_snapshot_hash):
        raise ApprovalValidationError("ApprovalRecord snapshot hash mismatch; re-approval required")

    return verified_answers, verified_keys, unverified_keys, sensitive_unconfirmed_keys, answers_hash


def save_masked_application_artifact(
    *,
    storage_root: Path,
    user_id: str | uuid.UUID,
    application_id: str | uuid.UUID,
    png_bytes: bytes,
) -> str:
    """Persist masked artifact inside owner-scoped directory; return opaque artifact_id."""
    uid = uuid.UUID(str(user_id)).hex
    aid = uuid.UUID(str(application_id)).hex
    artifact_id = str(uuid.uuid4())
    art_hex = uuid.UUID(artifact_id).hex
    root = storage_root.resolve()
    target_dir = (root / "artifacts" / uid).resolve()
    if root not in target_dir.parents:
        raise SSRFBlockedError("Invalid artifact directory confinement")
    target_dir.mkdir(parents=True, exist_ok=True)
    target_file = (target_dir / f"{aid}_{art_hex}.png").resolve()
    if target_dir not in target_file.parents:
        raise SSRFBlockedError("Invalid artifact file confinement")
    with open(target_file, "xb") as fh:
        fh.write(png_bytes)
    return artifact_id


def resolve_owned_artifact_bytes(
    *,
    storage_root: Path,
    user_id: uuid.UUID,
    application_id: uuid.UUID,
    artifact_id: str,
) -> bytes:
    """Load owner-scoped application artifact without exposing raw server filesystem paths."""
    uid = user_id.hex
    aid = application_id.hex
    art_hex = uuid.UUID(artifact_id).hex
    root = storage_root.resolve()
    target_dir = (root / "artifacts" / uid).resolve()
    target_file = (target_dir / f"{aid}_{art_hex}.png").resolve()
    if target_dir not in target_file.parents or not target_file.is_file():
        raise FileNotFoundError("Application artifact not found for owner")
    return target_file.read_bytes()


def purge_expired_application_artifacts(
    *,
    storage_root: Path,
    retention_days: int = 14,
    now: datetime | None = None,
) -> int:
    """Delete application artifacts older than retention_days."""
    current = now or datetime.now(timezone.utc)
    cutoff_ts = (current - timedelta(days=retention_days)).timestamp()
    base_dir = (storage_root.resolve() / "artifacts").resolve()
    if not base_dir.is_dir():
        return 0
    deleted = 0
    for fpath in base_dir.rglob("*.png"):
        try:
            if fpath.is_file() and fpath.stat().st_mtime <= cutoff_ts:
                fpath.unlink(missing_ok=True)
                deleted += 1
        except OSError:
            continue
    return deleted


_PREFILL_SUBMIT_GUARD_JS = """
(() => {
    const _block = (kind) => {
        throw new DOMException('Programmatic submission blocked by ApplyPilot: ' + kind, 'SecurityError');
    };
    try {
        Object.defineProperty(HTMLFormElement.prototype, 'submit', {
            value: function() { _block('form.submit'); },
            writable: false,
            configurable: false
        });
    } catch (_) {}
    try {
        Object.defineProperty(HTMLFormElement.prototype, 'requestSubmit', {
            value: function() { _block('form.requestSubmit'); },
            writable: false,
            configurable: false
        });
    } catch (_) {}
    window.addEventListener('click', (e) => {
        const target = e.target && e.target.closest ? e.target.closest("button, input[type='submit'], input[type='image']") : null;
        if (target && !e.isTrusted) {
            e.preventDefault();
            e.stopImmediatePropagation();
        }
    }, true);
    window.addEventListener('submit', (e) => {
        if (!e.isTrusted) {
            e.preventDefault();
            e.stopImmediatePropagation();
        }
    }, true);
})();
"""


ALLOWED_CSRF_FIELD_NAMES: frozenset[str] = frozenset(
    {
        "csrf_token",
        "_csrf",
        "csrfmiddlewaretoken",
        "__requestverificationtoken",
        "authenticity_token",
        "_token",
        "xsrf_token",
    }
)


def parse_submitted_request_payload(
    *,
    url: str,
    method: str,
    headers: dict[str, str],
    post_data_buffer: bytes | None,
) -> tuple[dict[str, list[str]], dict[str, str], bytes]:
    """Parse HTTP GET query or POST urlencoded/multipart wire payload into fields + file SHA-256 digests."""
    norm_method = (method or "GET").strip().upper()
    parsed = urlparse((url or "").strip())
    text_fields: dict[str, list[str]] = {}
    file_checksums: dict[str, str] = {}

    if norm_method == "GET":
        for k, v in parse_qsl(parsed.query, keep_blank_values=True):
            text_fields.setdefault(k, []).append(v)
        canonical_bytes = f"GET|{_normalize_compare_url_with_query(url)}".encode("utf-8")
        return text_fields, file_checksums, canonical_bytes

    raw_bytes = post_data_buffer or b""
    content_type = ""
    for hk, hv in headers.items():
        if hk.lower() == "content-type":
            content_type = hv.strip()
            break

    lower_ct = content_type.lower()
    if "application/x-www-form-urlencoded" in lower_ct or (not content_type and raw_bytes):
        try:
            decoded = raw_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SubmitBlockedViolationError("Non-UTF-8 form payload rejected") from exc
        for k, v in parse_qsl(decoded, keep_blank_values=True):
            text_fields.setdefault(k, []).append(v)
    elif "multipart/form-data" in lower_ct:
        mime_msg = BytesParser(policy=_EMAIL_DEFAULT_POLICY).parsebytes(
            f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("latin-1") + raw_bytes
        )
        if not mime_msg.is_multipart():
            raise SubmitBlockedViolationError("Malformed multipart/form-data request body")
        for part in mime_msg.iter_parts():
            field_name = part.get_param("name", header="content-disposition")
            if not field_name:
                raise SubmitBlockedViolationError("Multipart form part missing name")
            filename = part.get_filename()
            payload = part.get_payload(decode=True) or b""
            if filename is not None:
                file_checksums[str(field_name)] = hashlib.sha256(payload).hexdigest()
            else:
                text_fields.setdefault(str(field_name), []).append(payload.decode("utf-8", errors="strict"))
    elif raw_bytes:
        raise SubmitBlockedViolationError("Unsupported form Content-Type")

    canonical_bytes = f"{norm_method}|{_normalize_compare_url_with_query(url)}|".encode("utf-8") + raw_bytes
    return text_fields, file_checksums, canonical_bytes


@dataclass
class _HeldPendingSubmission:
    pending_submission_id: uuid.UUID
    route: Route
    url: str
    method: str
    page_url: str
    payload_sha256: str
    detected_at: datetime
    expires_at: datetime
    captured_fields: list[PendingSubmissionFieldPreview] = field(default_factory=list)


_EXTRA_SENSITIVE_WIRE_KEYWORDS: tuple[str, ...] = (
    "national insurance",
    "nino",
    "tax id",
    "tax_id",
    "tin",
    "ssn",
    "social security",
    "aadhaar",
    "passport",
    "date of birth",
    "birth date",
    "birthday",
    "dob",
    "bday",
    "work authorization",
    "authorized to work",
    "visa",
    "sponsorship",
    "citizenship",
    "phone",
    "mobile",
    "telephone",
    "email",
    "address",
    "street",
    "postal",
    "zip code",
    "zipcode",
    "residence",
    "salary",
    "compensation",
    "secret",
    "token",
    "password",
)

_SENSITIVE_INPUT_TYPES: frozenset[str] = frozenset({"password", "email", "tel", "date"})
_SENSITIVE_AUTOCOMPLETE_VALUES: frozenset[str] = frozenset(
    {
        "bday",
        "bday-day",
        "bday-month",
        "bday-year",
        "email",
        "tel",
        "tel-national",
        "tel-local",
        "street-address",
        "address-line1",
        "address-line2",
        "postal-code",
        "cc-number",
        "one-time-code",
        "new-password",
        "current-password",
    }
)
_SAFE_NON_SENSITIVE_FIELD_KEYS: frozenset[str] = frozenset(
    {
        "full_name",
        "name",
        "first_name",
        "last_name",
        "linkedin",
        "linkedin_url",
        "github",
        "github_url",
        "portfolio",
        "portfolio_url",
        "website",
        "location",
        "city",
        "why_company",
        "cover_letter",
        "summary",
        "skills",
    }
)


def _is_wire_field_sensitive(
    field_name: str,
    meta: dict[str, Any] | None,
    *,
    has_form_metadata: bool,
) -> bool:
    """Classify wire field sensitivity using form metadata and fail-closed defaults."""
    norm_key = field_name.strip().lower()
    if norm_key in ALLOWED_CSRF_FIELD_NAMES:
        return True

    if meta is None:
        # Unknown/ambiguous field when form metadata was provided -> fail closed
        if has_form_metadata:
            return True
        if classify_sensitive_question(field_name, field_name) is not None:
            return True
        if any(kw in norm_key.replace("_", " ") for kw in _EXTRA_SENSITIVE_WIRE_KEYWORDS):
            return True
        return norm_key not in _SAFE_NON_SENSITIVE_FIELD_KEYS

    if meta.get("is_sensitive") is True:
        return True
    input_type = str(meta.get("input_type") or "").strip().lower()
    field_type = str(meta.get("field_type") or "").strip().lower()
    autocomplete = str(meta.get("autocomplete") or "").strip().lower()
    if input_type in _SENSITIVE_INPUT_TYPES or field_type in _SENSITIVE_INPUT_TYPES:
        return True
    if autocomplete in _SENSITIVE_AUTOCOMPLETE_VALUES:
        return True

    label = str(meta.get("label") or "").strip()
    qkey = str(meta.get("question_key") or "").strip()
    combined = " ".join(p for p in (field_name, label, qkey, autocomplete) if p).lower()
    if classify_sensitive_question(combined, qkey or field_name) is not None:
        return True
    combined_spaced = combined.replace("_", " ").replace("-", " ")
    if any(kw in combined_spaced for kw in _EXTRA_SENSITIVE_WIRE_KEYWORDS):
        return True

    # If neither label nor key is recognized as non-sensitive, fail closed
    if not label and norm_key not in _SAFE_NON_SENSITIVE_FIELD_KEYS:
        return True
    return False


def _build_captured_field_previews(
    *,
    text_fields: dict[str, list[str]],
    file_checksums: dict[str, str],
    field_metadata: dict[str, dict[str, Any]] | None = None,
) -> list[PendingSubmissionFieldPreview]:
    """Build masked field-by-field preview strictly from parsed wire request payloads."""
    previews: list[PendingSubmissionFieldPreview] = []
    meta_map = field_metadata or {}
    has_form_metadata = bool(meta_map)
    for key in sorted(text_fields.keys()):
        vals = text_fields[key]
        if key.lower() in ALLOWED_CSRF_FIELD_NAMES:
            previews.append(
                PendingSubmissionFieldPreview(
                    field_name=key,
                    field_kind="csrf_token",
                    is_sensitive=True,
                    value_preview="[CSRF TOKEN REDACTED]",
                    value_count=len(vals),
                )
            )
        elif _is_wire_field_sensitive(key, meta_map.get(key), has_form_metadata=has_form_metadata):
            previews.append(
                PendingSubmissionFieldPreview(
                    field_name=key,
                    field_kind="text",
                    is_sensitive=True,
                    value_preview="[REDACTED — SENSITIVE OR AMBIGUOUS WIRE VALUE]",
                    value_count=len(vals),
                )
            )
        else:
            joined = "; ".join(v[:200] for v in vals)
            previews.append(
                PendingSubmissionFieldPreview(
                    field_name=key,
                    field_kind="text",
                    is_sensitive=False,
                    value_preview=joined,
                    value_count=len(vals),
                )
            )
    for fkey in sorted(file_checksums.keys()):
        previews.append(
            PendingSubmissionFieldPreview(
                field_name=fkey,
                field_kind="file",
                is_sensitive=False,
                value_preview=None,
                file_sha256=file_checksums[fkey],
                value_count=1,
            )
        )
    return previews


class SubmitGuardState:
    """Thread-safe Hold-and-Confirm submission gate binding exact held user navigation & body to ManualSubmitAuthorization."""

    def __init__(self, *, expected_destination_url: str) -> None:
        self.expected_destination_url = expected_destination_url
        self._lock = threading.Lock()
        self._authorization: ManualSubmitAuthorization | None = None
        self._pending_submission: _HeldPendingSubmission | None = None
        self._final_url: str | None = None
        self._detected_form_action: str | None = None
        self._detected_http_method: str | None = None
        self._prefill_completed: bool = False
        self._payload_policy_configured: bool = False
        self._expected_fields: dict[str, list[str]] = {}
        self._expected_file_fields: dict[str, str] = {}
        self._allowed_csrf_tokens: dict[str, str] = {}
        self._field_metadata: dict[str, dict[str, Any]] = {}
        self._live_answers_hash: str = ""
        self._live_resume_checksum: str = ""
        self._live_approval_snapshot_hash: str = ""
        self.blocked_attempts: int = 0
        self.authorized_submissions: int = 0
        self.blocked_redirects: int = 0
        self.last_blocked_redirect_target: str | None = None

    def mark_prefill_complete(
        self,
        *,
        final_url: str,
        detected_form_action: str | None,
        detected_http_method: str | None,
    ) -> None:
        with self._lock:
            self._final_url = final_url
            self._detected_form_action = detected_form_action
            self._detected_http_method = (detected_http_method or "").strip().upper() or None
            self._prefill_completed = True

    def set_expected_payload_policy(
        self,
        *,
        expected_fields: dict[str, list[str]],
        expected_file_fields: dict[str, str] | None = None,
        allowed_csrf_tokens: dict[str, str] | None = None,
        field_metadata: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        """Lock expected wire form fields, uploaded PDF checksum(s), and explicit baseline CSRF tokens."""
        with self._lock:
            self._expected_fields = {k: list(v) for k, v in expected_fields.items()}
            self._expected_file_fields = dict(expected_file_fields or {})
            self._field_metadata = {k: dict(v) for k, v in (field_metadata or {}).items()}
            filtered_csrf: dict[str, str] = {}
            for k, v in (allowed_csrf_tokens or {}).items():
                if k.lower() in ALLOWED_CSRF_FIELD_NAMES and str(v).strip():
                    filtered_csrf[k] = str(v)
            self._allowed_csrf_tokens = filtered_csrf
            self._payload_policy_configured = True

    def _verify_wire_payload_locked(
        self,
        *,
        url: str,
        method: str,
        headers: dict[str, str],
        post_data_buffer: bytes | None,
    ) -> str | None:
        payload_sha256, _previews = self._inspect_wire_payload_locked(
            url=url,
            method=method,
            headers=headers,
            post_data_buffer=post_data_buffer,
        )
        return payload_sha256

    def _inspect_wire_payload_locked(
        self,
        *,
        url: str,
        method: str,
        headers: dict[str, str],
        post_data_buffer: bytes | None,
    ) -> tuple[str | None, list[PendingSubmissionFieldPreview]]:
        try:
            text_fields, file_checksums, wire_bytes = parse_submitted_request_payload(
                url=url,
                method=method,
                headers=headers,
                post_data_buffer=post_data_buffer,
            )
        except SubmitBlockedViolationError:
            return None, []

        payload_sha256 = hashlib.sha256(wire_bytes).hexdigest()

        # Reject unexpected query parameters on POST when detected_form_action had none
        if method == "POST" and self._detected_form_action:
            act_q = urlparse(self._detected_form_action).query
            req_q = urlparse(url).query
            if req_q and req_q != act_q:
                return None, []

        previews = _build_captured_field_previews(
            text_fields=text_fields,
            file_checksums=file_checksums,
            field_metadata=self._field_metadata,
        )

        if not self._payload_policy_configured:
            return payload_sha256, previews

        # Strict verify every file input uploaded
        if set(file_checksums.keys()) != set(self._expected_file_fields.keys()):
            return None, []
        for fkey, fsha in self._expected_file_fields.items():
            if not hmac.compare_digest(file_checksums.get(fkey, ""), fsha):
                return None, []

        # Every expected approved field MUST match strictly; only explicit baseline CSRF tokens permitted additionally
        seen_expected: set[str] = set()
        for k, vals in text_fields.items():
            if k in self._expected_fields:
                exp_vals = self._expected_fields[k]
                if len(vals) != len(exp_vals) or not all(hmac.compare_digest(a, b) for a, b in zip(vals, exp_vals)):
                    return None, []
                seen_expected.add(k)
            elif k in self._allowed_csrf_tokens and k.lower() in ALLOWED_CSRF_FIELD_NAMES:
                if len(vals) != 1 or not hmac.compare_digest(vals[0], self._allowed_csrf_tokens[k]):
                    return None, []
            else:
                return None, []

        if seen_expected != set(self._expected_fields.keys()):
            return None, []

        return payload_sha256, previews

    def set_live_bindings(
        self,
        *,
        answers_hash: str,
        resume_checksum: str,
        approval_snapshot_hash: str,
    ) -> None:
        with self._lock:
            self._live_answers_hash = answers_hash
            self._live_resume_checksum = resume_checksum
            self._live_approval_snapshot_hash = approval_snapshot_hash

    def update_live_bindings(
        self,
        *,
        answers_hash: str | None = None,
        resume_checksum: str | None = None,
        approval_snapshot_hash: str | None = None,
    ) -> None:
        with self._lock:
            if answers_hash is not None:
                self._live_answers_hash = answers_hash
            if resume_checksum is not None:
                self._live_resume_checksum = resume_checksum
            if approval_snapshot_hash is not None:
                self._live_approval_snapshot_hash = approval_snapshot_hash

    def requires_authorization(
        self,
        *,
        url: str,
        method: str,
        is_navigation: bool,
        resource_type: str,
    ) -> bool:
        norm_method = (method or "GET").strip().upper()
        if norm_method not in {"GET", "HEAD", "OPTIONS"}:
            return True

        with self._lock:
            detected_action = self._detected_form_action
            prefill_done = self._prefill_completed
            final_url = self._final_url

        if detected_action and _normalize_compare_url(url) == _normalize_compare_url(detected_action):
            return True

        norm_url_q = _normalize_compare_url_with_query(url)
        norm_dest_q = _normalize_compare_url_with_query(self.expected_destination_url)
        norm_final_q = _normalize_compare_url_with_query(final_url) if final_url else None

        if is_navigation and "?" in url and norm_url_q != norm_dest_q and norm_url_q != norm_final_q:
            return True

        if prefill_done:
            if is_navigation and norm_url_q != norm_dest_q and norm_url_q != norm_final_q:
                return True
            if (resource_type or "").lower() in {"fetch", "xhr", "ping", "eventsource"}:
                return True

        return False

    @staticmethod
    def _urls_match_action(url: str, expected_action: str) -> bool:
        parsed_action = urlparse((expected_action or "").strip())
        if parsed_action.query:
            return _normalize_compare_url_with_query(url) == _normalize_compare_url_with_query(expected_action)
        return _normalize_compare_url(url) == _normalize_compare_url(expected_action)

    def hold_pending_submission(
        self,
        *,
        route: Route,
        url: str,
        method: str,
        page_url: str,
        is_navigation: bool,
        resource_type: str,
        is_redirect: bool = False,
        hold_ttl_seconds: int = 120,
        now: datetime | None = None,
    ) -> _HeldPendingSubmission | None:
        """Hold ONLY a genuine unredirected document form navigation matching inspected form & destination."""
        current = now or datetime.now(timezone.utc)
        norm_method = (method or "GET").strip().upper()
        norm_res_type = (resource_type or "").strip().lower()

        with self._lock:
            if is_redirect or not is_navigation or norm_res_type != "document":
                self.blocked_attempts += 1
                return None
            if (
                not self._prefill_completed
                or self._detected_form_action is None
                or self._detected_http_method is None
            ):
                self.blocked_attempts += 1
                return None
            if self.authorized_submissions > 0 or (self._authorization is not None and self._authorization.consumed):
                self.blocked_attempts += 1
                return None

            allowed_pages = {_normalize_compare_url(self.expected_destination_url)}
            if self._final_url:
                allowed_pages.add(_normalize_compare_url(self._final_url))
            if _normalize_compare_url(page_url) not in allowed_pages:
                self.blocked_attempts += 1
                return None

            if norm_method != self._detected_http_method:
                self.blocked_attempts += 1
                return None
            if not self._urls_match_action(url, self._detected_form_action):
                self.blocked_attempts += 1
                return None

            try:
                hdrs = dict(route.request.headers or {})
                post_buf = route.request.post_data_buffer
            except Exception:
                hdrs = {}
                post_buf = None
            payload_sha256, captured_fields = self._inspect_wire_payload_locked(
                url=url,
                method=norm_method,
                headers=hdrs,
                post_data_buffer=post_buf,
            )
            if payload_sha256 is None:
                self.blocked_attempts += 1
                return None

            if self._pending_submission is not None:
                if _as_utc(self._pending_submission.expires_at) > current:
                    self.blocked_attempts += 1
                    return None
                # Expire stale held route before replacing
                stale = self._pending_submission
                self._pending_submission = None
                try:
                    stale.route.abort("blockedbyclient")
                except Exception:
                    pass

            held = _HeldPendingSubmission(
                pending_submission_id=uuid.uuid4(),
                route=route,
                url=url,
                method=norm_method,
                page_url=page_url,
                payload_sha256=payload_sha256,
                detected_at=current,
                expires_at=current + timedelta(seconds=hold_ttl_seconds),
                captured_fields=captured_fields,
            )
            self._pending_submission = held
            return held

    def get_pending_submission_info(self, *, now: datetime | None = None) -> PendingSubmissionInfo | None:
        current = now or datetime.now(timezone.utc)
        stale_route: Route | None = None
        info: PendingSubmissionInfo | None = None
        with self._lock:
            pend = self._pending_submission
            if pend is not None:
                if _as_utc(pend.expires_at) <= current:
                    stale_route = pend.route
                    self._pending_submission = None
                    self.blocked_attempts += 1
                else:
                    info = PendingSubmissionInfo(
                        pending_submission_id=pend.pending_submission_id,
                        target_url=pend.url,
                        http_method=pend.method,  # type: ignore[arg-type]
                        payload_sha256=pend.payload_sha256,
                        detected_at=pend.detected_at,
                        expires_at=pend.expires_at,
                        captured_fields=list(pend.captured_fields),
                    )
        if stale_route is not None:
            try:
                stale_route.abort("blockedbyclient")
            except Exception:
                pass
        return info

    def abort_pending_submission(self) -> None:
        with self._lock:
            pend = self._pending_submission
            self._pending_submission = None
        if pend is not None:
            try:
                pend.route.abort("blockedbyclient")
            except Exception:
                pass

    def release_held_submission(
        self,
        *,
        auth: ManualSubmitAuthorization,
        pending_submission_id: uuid.UUID,
        live_answers_hash: str,
        live_resume_checksum: str,
        live_approval_snapshot_hash: str,
        expected_payload_sha256: str | None = None,
        now: datetime | None = None,
    ) -> Route:
        """Atomically verify bindings, consume auth once, verify held body digest, and return ONLY the exact held Route."""
        current = now or datetime.now(timezone.utc)
        with self._lock:
            if self.authorized_submissions > 0 or (self._authorization is not None and self._authorization.consumed):
                self.blocked_attempts += 1
                raise SubmitBlockedViolationError("Manual submit authorization already consumed; replay is forbidden")
            if auth.consumed:
                self.blocked_attempts += 1
                raise SubmitBlockedViolationError("ManualSubmitAuthorization is already consumed")
            if _as_utc(auth.expires_at) <= current:
                self.blocked_attempts += 1
                raise SubmitBlockedViolationError("ManualSubmitAuthorization has expired")
            if not hmac.compare_digest(live_answers_hash, auth.answers_hash):
                self.blocked_attempts += 1
                raise SubmitBlockedViolationError("Answers hash mismatch during manual submit confirmation")
            if not hmac.compare_digest(live_resume_checksum, auth.resume_checksum):
                self.blocked_attempts += 1
                raise SubmitBlockedViolationError("Resume checksum mismatch during manual submit confirmation")
            if not hmac.compare_digest(live_approval_snapshot_hash, auth.approval_snapshot_hash):
                self.blocked_attempts += 1
                raise SubmitBlockedViolationError("Approval snapshot mismatch during manual submit confirmation")

            pend = self._pending_submission
            if pend is None or _as_utc(pend.expires_at) <= current:
                self._pending_submission = None
                self.blocked_attempts += 1
                raise SubmitBlockedViolationError("No active held pending submission matches this confirmation")
            if pend.pending_submission_id != pending_submission_id:
                self.blocked_attempts += 1
                raise SubmitBlockedViolationError("Confirmation pending_submission_id does not match held request")
            if pend.method != auth.http_method or not self._urls_match_action(pend.url, auth.form_action):
                self.blocked_attempts += 1
                raise SubmitBlockedViolationError("Held request action/method does not match authorization")
            if _normalize_compare_url(pend.page_url) != _normalize_compare_url(auth.destination_url):
                self.blocked_attempts += 1
                raise SubmitBlockedViolationError("Held request page URL does not match authorized destination")

            # Re-verify held Route payload digest directly against approved policy before release
            try:
                live_hdrs = dict(pend.route.request.headers or {})
                live_buf = pend.route.request.post_data_buffer
            except Exception:
                live_hdrs = {}
                live_buf = None
            live_sha = self._verify_wire_payload_locked(
                url=pend.url,
                method=pend.method,
                headers=live_hdrs,
                post_data_buffer=live_buf,
            )
            if live_sha is None or not hmac.compare_digest(live_sha, pend.payload_sha256):
                self.blocked_attempts += 1
                raise SubmitBlockedViolationError("Held submission payload failed approved-answers integrity check")
            if expected_payload_sha256 is not None and not hmac.compare_digest(expected_payload_sha256, pend.payload_sha256):
                self.blocked_attempts += 1
                raise SubmitBlockedViolationError("Confirmed payload_sha256 does not match held submission body")

            auth.mark_consumed()
            self._authorization = auth
            self._live_answers_hash = live_answers_hash
            self._live_resume_checksum = live_resume_checksum
            self._live_approval_snapshot_hash = live_approval_snapshot_hash
            self.authorized_submissions += 1
            self._pending_submission = None
            return pend.route

    def record_blocked(self) -> None:
        with self._lock:
            self.blocked_attempts += 1

    def record_blocked_redirect(self, *, safe_target: str | None) -> None:
        with self._lock:
            self.blocked_attempts += 1
            self.blocked_redirects += 1
            self.last_blocked_redirect_target = safe_target


def forward_held_route_safely(
    route: Route,
    *,
    state: SubmitGuardState,
    egress_proxy: LocalSSRFEgressProxy | None = None,
) -> tuple[bool, int | None, str | None]:
    """Forward a single released submission with max_redirects=0 and fail closed on 301/302/303/307/308 redirects."""
    api_resp = route.fetch(max_redirects=0)
    status_code = int(api_resp.status)
    if 300 <= status_code < 400:
        raw_loc = (api_resp.headers or {}).get("location") or ""
        resolved_loc = urljoin(route.request.url, raw_loc) if raw_loc else ""
        safe_loc = _normalize_compare_url(resolved_loc) if resolved_loc else None
        if egress_proxy is not None and resolved_loc:
            try:
                egress_proxy.validate_url(resolved_loc, allow_websocket=False)
            except Exception:
                safe_loc = "[BLOCKED_SSRF_REDIRECT]"
        state.record_blocked_redirect(safe_target=safe_loc)
        route.abort("blockedbyclient")
        return False, status_code, safe_loc

    route.fulfill(response=api_resp)
    return True, status_code, None


def install_submit_protection(ctx: BrowserContext, *, state: SubmitGuardState) -> None:
    """Enforce DOM prototype lock + pre-tunnel Hold-and-Confirm network submission firewall."""
    ctx.add_init_script(_PREFILL_SUBMIT_GUARD_JS)

    def _submit_guard_route(route: Route) -> None:
        req = route.request
        method = (req.method or "GET").strip().upper()
        url = req.url

        if state.requires_authorization(
            url=url,
            method=method,
            is_navigation=req.is_navigation_request(),
            resource_type=req.resource_type or "",
        ):
            try:
                page_url = req.frame.page.url if req.frame and req.frame.page else state.expected_destination_url
            except Exception:
                page_url = state.expected_destination_url

            held = state.hold_pending_submission(
                route=route,
                url=url,
                method=method,
                page_url=page_url,
                is_navigation=req.is_navigation_request(),
                resource_type=req.resource_type or "",
                is_redirect=(req.redirected_from is not None),
            )
            if held is None:
                route.abort("blockedbyclient")
            return

        route.fallback()

    ctx.route("**/*", _submit_guard_route)


@dataclass
class _ManagedHandoffSession:
    handoff_session_id: uuid.UUID
    application_id: uuid.UUID
    user_id: uuid.UUID
    destination_url: str
    form_action: str | None
    http_method: str | None
    answers_hash: str
    resume_checksum: str
    approval_snapshot_hash: str
    created_at: datetime
    expires_at: datetime
    supervisor: Win32JobSupervisor
    process_registry: BrowserProcessRegistry
    ctrl_sock: socket.socket
    ctrl_buf: bytearray
    authorization: ManualSubmitAuthorization | None = None
    status: str = "active"


def _send_ctrl(sock: socket.socket, payload: dict[str, Any]) -> None:
    sock.sendall((json.dumps(payload, ensure_ascii=True) + "\n").encode("utf-8"))


def _recv_ctrl(sock: socket.socket, buf: bytearray) -> dict[str, Any]:
    while True:
        nl = buf.find(b"\n")
        if nl != -1:
            line = bytes(buf[:nl])
            del buf[: nl + 1]
            return json.loads(line.decode("utf-8"))
        chunk = sock.recv(65536)
        if not chunk:
            raise EOFError("Supervised browser worker closed connection")
        buf.extend(chunk)


class BrowserHandoffManager:
    """Server-side lifecycle, ownership, TTL, and ManualSubmitAuthorization manager."""

    def __init__(self, *, storage_root: Path) -> None:
        self.storage_root = storage_root
        self.process_registry = BrowserProcessRegistry(storage_root)
        self._lock = threading.Lock()
        self._sessions: dict[uuid.UUID, _ManagedHandoffSession] = {}

    def register_session(self, session: _ManagedHandoffSession) -> HandoffSessionInfo:
        with self._lock:
            self._sessions[session.handoff_session_id] = session
            return self._to_info(session)

    def _query_worker_status_locked(self, sess: _ManagedHandoffSession) -> dict[str, Any]:
        try:
            _send_ctrl(sess.ctrl_sock, {"cmd": "STATUS"})
            sess.ctrl_sock.settimeout(1.5)
            resp = _recv_ctrl(sess.ctrl_sock, sess.ctrl_buf)
        except Exception:
            return {
                "connected": False,
                "blocked_submit_attempts": 0,
                "authorized_submitted": 1 if (sess.authorization and sess.authorization.consumed) else 0,
                "pending_submission": None,
            }
        submitted_count = int(resp.get("authorized_submitted", 0))
        if submitted_count > 0 and sess.authorization is not None and not sess.authorization.consumed:
            sess.authorization.mark_consumed()
        return resp

    def _to_info(
        self,
        sess: _ManagedHandoffSession,
        *,
        pending_raw: dict[str, Any] | None = None,
    ) -> HandoffSessionInfo:
        pend = PendingSubmissionInfo.model_validate(pending_raw) if pending_raw else None
        return HandoffSessionInfo(
            handoff_session_id=sess.handoff_session_id,
            application_id=sess.application_id,
            status=sess.status,  # type: ignore[arg-type]
            created_at=sess.created_at,
            expires_at=sess.expires_at,
            manual_submit_armed=(sess.authorization is not None and not sess.authorization.consumed),
            pending_submission=pend,
        )

    def get_session_info(self, *, handoff_session_id: uuid.UUID, user_id: uuid.UUID) -> HandoffSessionInfo:
        with self._lock:
            sess = self._sessions.get(handoff_session_id)
            if sess is None or sess.user_id != user_id:
                raise ApprovalValidationError("Browser handoff session not found for user")
            status_resp = self._query_worker_status_locked(sess) if sess.status == "active" else {}
            return self._to_info(sess, pending_raw=status_resp.get("pending_submission"))

    def list_user_sessions(
        self,
        *,
        user_id: uuid.UUID,
        application_id: uuid.UUID | None = None,
    ) -> list[HandoffSessionInfo]:
        with self._lock:
            items: list[HandoffSessionInfo] = []
            for sess in self._sessions.values():
                if sess.user_id != user_id:
                    continue
                if application_id is not None and sess.application_id != application_id:
                    continue
                status_resp = self._query_worker_status_locked(sess) if sess.status == "active" else {}
                items.append(self._to_info(sess, pending_raw=status_resp.get("pending_submission")))
            return items

    def get_session_live_status(
        self,
        *,
        handoff_session_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> dict[str, Any]:
        """Query live worker submission counters, pending submission, and sync consumption."""
        with self._lock:
            sess = self._sessions.get(handoff_session_id)
            if sess is None or sess.user_id != user_id:
                raise ApprovalValidationError("Browser handoff session not found for user")
            resp = self._query_worker_status_locked(sess)
            return {
                "connected": bool(resp.get("connected", False)),
                "blocked_submit_attempts": int(resp.get("blocked_submit_attempts", 0)),
                "authorized_submitted": int(resp.get("authorized_submitted", 0)),
                "manual_submit_armed": bool(sess.authorization is not None and not sess.authorization.consumed),
                "consumed": bool(sess.authorization and sess.authorization.consumed),
                "pending_submission": resp.get("pending_submission"),
            }

    def authorize_manual_submit(
        self,
        *,
        user_id: uuid.UUID,
        application_id: uuid.UUID,
        request: ManualSubmitAuthorizationRequest,
        live_answers_hash: str,
        live_resume_checksum: str,
        live_approval_snapshot_hash: str,
        ttl_seconds: int = 120,
        now: datetime | None = None,
    ) -> ManualSubmitAuthorization:
        """Verify exact held pending submission and release it once via single-use authorization."""
        current = now or datetime.now(timezone.utc)
        with self._lock:
            sess = self._sessions.get(request.handoff_session_id)
            if sess is None or sess.user_id != user_id:
                raise ApprovalValidationError("Browser handoff session ownership check failed")
            if sess.application_id != application_id:
                raise ApprovalValidationError("Authorization application_id does not match handoff session")
            if sess.status != "active" or _as_utc(sess.expires_at) <= current:
                raise ApprovalValidationError("Browser handoff session has expired or closed")
            if sess.authorization is not None:
                raise SubmitBlockedViolationError(
                    "Manual submit authorization has already been issued for this handoff session"
                )
            if sess.form_action is None or sess.http_method is None:
                raise UnsupportedPlatformError("Form action/method is ambiguous; manual intervention required")
            if _normalize_compare_url(request.expected_form_action) != _normalize_compare_url(sess.form_action):
                raise ApprovalValidationError("Requested form action does not match inspected form action")
            if request.expected_http_method != sess.http_method:
                raise ApprovalValidationError("Requested HTTP method does not match inspected form method")
            if not hmac.compare_digest(live_answers_hash, sess.answers_hash):
                raise ApprovalValidationError("Answers hash changed after handoff")
            if not hmac.compare_digest(live_resume_checksum, sess.resume_checksum):
                raise ApprovalValidationError("Resume checksum changed after handoff")
            if not hmac.compare_digest(live_approval_snapshot_hash, sess.approval_snapshot_hash):
                raise ApprovalValidationError("Approval snapshot changed after handoff")

            live_status = self._query_worker_status_locked(sess)
            pending_raw = live_status.get("pending_submission")
            if not isinstance(pending_raw, dict):
                raise SubmitBlockedViolationError(
                    "No intercepted pending user submission found; click Submit in the browser first"
                )
            if str(pending_raw.get("pending_submission_id") or "") != str(request.pending_submission_id):
                raise SubmitBlockedViolationError(
                    "Requested pending_submission_id does not match the held browser submission"
                )
            if not hmac.compare_digest(
                str(pending_raw.get("payload_sha256") or ""),
                request.expected_payload_sha256,
            ):
                raise SubmitBlockedViolationError(
                    "Confirmed payload_sha256 does not match the held browser request body"
                )

            auth = ManualSubmitAuthorization(
                application_id=application_id,
                handoff_session_id=sess.handoff_session_id,
                user_id=user_id,
                destination_url=sess.destination_url,
                form_action=sess.form_action,
                http_method=sess.http_method,  # type: ignore[arg-type]
                answers_hash=live_answers_hash,
                resume_checksum=live_resume_checksum,
                approval_snapshot_hash=live_approval_snapshot_hash,
                issued_at=current,
                expires_at=min(_as_utc(sess.expires_at), current + timedelta(seconds=ttl_seconds)),
            )
            _send_ctrl(
                sess.ctrl_sock,
                {
                    "cmd": "ARM_MANUAL_SUBMIT",
                    "pending_submission_id": str(request.pending_submission_id),
                    "expected_payload_sha256": request.expected_payload_sha256,
                    "authorization": {
                        "application_id": str(auth.application_id),
                        "handoff_session_id": str(auth.handoff_session_id),
                        "user_id": str(auth.user_id),
                        "destination_url": auth.destination_url,
                        "form_action": auth.form_action,
                        "http_method": auth.http_method,
                        "answers_hash": auth.answers_hash,
                        "resume_checksum": auth.resume_checksum,
                        "approval_snapshot_hash": auth.approval_snapshot_hash,
                        "issued_at": auth.issued_at.isoformat(),
                        "expires_at": auth.expires_at.isoformat(),
                    },
                },
            )
            ack = _recv_ctrl(sess.ctrl_sock, sess.ctrl_buf)
            if not ack.get("ok"):
                if ack.get("redirect_blocked"):
                    # Authorization was already consumed on release; prevent replay cleanly
                    auth.mark_consumed()
                    sess.authorization = auth
                raise SubmitBlockedViolationError(
                    str(ack.get("error") or "Failed to confirm and release pending submission")
                )
            auth.mark_consumed()
            sess.authorization = auth
            return auth

    def close_session(
        self,
        *,
        handoff_session_id: uuid.UUID,
        user_id: uuid.UUID,
        expired: bool = False,
    ) -> HandoffSessionInfo:
        with self._lock:
            sess = self._sessions.get(handoff_session_id)
            if sess is None or sess.user_id != user_id:
                raise ApprovalValidationError("Browser handoff session not found for user")
            try:
                _send_ctrl(sess.ctrl_sock, {"cmd": "CLOSE"})
                sess.ctrl_sock.settimeout(1.0)
                _recv_ctrl(sess.ctrl_sock, sess.ctrl_buf)
            except Exception:
                pass
            finally:
                try:
                    sess.ctrl_sock.close()
                except OSError:
                    pass
                sess.supervisor.close(terminate_processes=True)
                self.process_registry.remove_manifest(str(handoff_session_id))
                sess.status = "expired" if expired else "closed"
                info = self._to_info(sess)
                self._sessions.pop(handoff_session_id, None)
            return info

    def sweep_expired_sessions(self, *, now: datetime | None = None) -> list[uuid.UUID]:
        current = now or datetime.now(timezone.utc)
        expired_ids: list[tuple[uuid.UUID, uuid.UUID]] = []
        with self._lock:
            for sid, sess in list(self._sessions.items()):
                if _as_utc(sess.expires_at) <= current:
                    expired_ids.append((sid, sess.user_id))
        closed: list[uuid.UUID] = []
        for sid, uid in expired_ids:
            try:
                self.close_session(handoff_session_id=sid, user_id=uid, expired=True)
                closed.append(sid)
            except ApprovalValidationError:
                continue
        self.process_registry.reap_abandoned_sessions(now=current)
        return closed


_HANDOFF_MANAGERS_LOCK = threading.Lock()
_HANDOFF_MANAGERS: dict[str, BrowserHandoffManager] = {}


def get_handoff_manager(storage_root: Path) -> BrowserHandoffManager:
    """Return a thread-safe per-storage-root BrowserHandoffManager shared across requests."""
    key = str(storage_root.resolve())
    with _HANDOFF_MANAGERS_LOCK:
        mgr = _HANDOFF_MANAGERS.get(key)
        if mgr is None:
            mgr = BrowserHandoffManager(storage_root=Path(key))
            _HANDOFF_MANAGERS[key] = mgr
        return mgr


def _assert_interactive_desktop_allowed() -> None:
    """Prevent accidental visible browser handoff inside background Celery worker tasks."""
    try:
        import celery

        task = celery.current_task
        if hasattr(task, "_get_current_object"):
            task = task._get_current_object()
        if task is not None and getattr(task, "request", None) is not None and getattr(task.request, "id", None):
            raise UnsupportedPlatformError("visible_handoff is forbidden inside background Celery workers")
    except ImportError:
        pass


class PlaywrightBrowserExecutor(ApplicationExecutionAdapter):
    """Isolated Checkpoint C Playwright browser prefill and controlled handoff executor."""

    adapter_name = "playwright_browser"
    mode = "human_approved_browser"

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        handoff_manager: BrowserHandoffManager | None = None,
        _allowed_test_origins: frozenset[tuple[str, int]] = frozenset(),
    ) -> None:
        self.settings = settings or get_settings()
        self.handoff_manager = handoff_manager or BrowserHandoffManager(
            storage_root=Path(self.settings.storage_dir)
        )
        # Private test-only local fixture origin allowlist; never exposed on ExecutionOptions/Settings.
        self._allowed_test_origins = _allowed_test_origins

    def execute_with_validated_context(
        self,
        *,
        ctx: ExecutionContext,
        user_id: uuid.UUID,
        profile: CandidateProfile,
        mapping: ResumeJobMapping | None,
        answers: list[ApplicationAnswer],
    ) -> ExecutionResult:
        """Validate resume/approval/answers, spawn supervised worker, prefill, and stop before submit."""
        if ctx.options.browser_mode == "visible_handoff":
            _assert_interactive_desktop_allowed()

        storage_root = Path(self.settings.storage_dir)
        verified_pdf, verified_pdf_bytes, verified_pdf_sha256 = load_verified_resume_snapshot(
            user_id=user_id,
            application=ctx.application,
            profile=profile,
            resume=ctx.resume,
            mapping=mapping,
            storage_root=storage_root,
        )
        verified_answers, verified_keys, unverified_keys, sensitive_unconf, answers_hash = (
            validate_approval_and_answers(
                application=ctx.application,
                user_id=user_id,
                resume=ctx.resume,
                verified_resume_checksum=verified_pdf_sha256,
                approval_record=ctx.approval_record,
                answers=answers,
                settings=self.settings,
            )
        )

        # Precheck URL before launching browser worker
        validate_destination_url(
            ctx.application.destination_url,
            allow_websocket=False,
            allowed_test_origins=self._allowed_test_origins,
        )

        # Reap any stale sessions safely before starting new one
        self.handoff_manager.process_registry.reap_abandoned_sessions()

        handoff_id = uuid.uuid4()
        now = datetime.now(timezone.utc)
        ttl = self.settings.browser_handoff_ttl_seconds
        expires_at = now + timedelta(seconds=ttl)
        capture_artifacts = bool(ctx.options.capture_artifacts or self.settings.capture_application_artifacts)

        ctrl_srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        ctrl_srv.bind(("127.0.0.1", 0))
        ctrl_srv.listen(1)
        ctrl_port = int(ctrl_srv.getsockname()[1])
        ctrl_srv.settimeout(float(ctx.options.timeout_seconds or self.settings.browser_execution_timeout_seconds))
        ctrl_secret = secrets.token_hex(32)

        supervisor = Win32JobSupervisor()
        conn: socket.socket | None = None
        worker_pid = 0
        worker_ct: int | None = None
        keep_alive = False

        try:
            backend_root = Path(__file__).resolve().parents[2]
            cmd = [sys.executable, "-m", "app.services._browser_worker", "--control-port", str(ctrl_port)]
            worker_pid, worker_ct = supervisor.spawn_suspended_in_job(
                cmd,
                cwd=backend_root,
                extra_env={"APPLYPILOT_CTRL_SECRET": ctrl_secret},
            )
            self.handoff_manager.process_registry.write_manifest(
                ProcessSessionManifest(
                    session_id=str(handoff_id),
                    application_id=str(ctx.application.id),
                    user_id=str(user_id),
                    pid=worker_pid,
                    creation_time=worker_ct,
                    pgid=worker_pid if not supervisor.uses_kernel_job_object else None,
                    temp_profile_dir="",
                    created_at=now.isoformat(),
                    expires_at=expires_at.isoformat(),
                )
            )

            conn, _ = ctrl_srv.accept()
            conn.settimeout(float(ctx.options.timeout_seconds or self.settings.browser_execution_timeout_seconds))
            buf = bytearray()

            challenge = secrets.token_hex(16)
            _send_ctrl(conn, {"challenge": challenge})
            auth_reply = _recv_ctrl(conn, buf)
            expected_sig = hmac.new(ctrl_secret.encode("utf-8"), challenge.encode("utf-8"), "sha256").hexdigest()
            if not hmac.compare_digest(str(auth_reply.get("hmac") or ""), expected_sig):
                raise SSRFBlockedError("Supervised browser worker control authentication failed")

            _send_ctrl(
                conn,
                {
                    "cmd": "PREFILL",
                    "handoff_session_id": str(handoff_id),
                    "destination_url": ctx.application.destination_url,
                    "verified_resume_filename": verified_pdf.name,
                    "verified_resume_bytes_b64": base64.b64encode(verified_pdf_bytes).decode("ascii"),
                    "verified_resume_checksum": verified_pdf_sha256,
                    "answers_hash": answers_hash,
                    "approval_snapshot_hash": ctx.approval_record.approval_snapshot_hash,
                    "verified_answers": verified_answers,
                    "verified_keys": sorted(verified_keys),
                    "unverified_keys": sorted(unverified_keys),
                    "sensitive_unconfirmed_keys": sorted(sensitive_unconf),
                    "options": ctx.options.model_dump(),
                    "require_os_egress_firewall": self.settings.require_os_egress_firewall,
                    "capture_artifacts": capture_artifacts,
                    "storage_root": str(storage_root),
                    "user_id": str(user_id),
                    "application_id": str(ctx.application.id),
                    "_allowed_test_origins": [[h, p] for h, p in sorted(self._allowed_test_origins)],
                },
            )
            try:
                resp = _recv_ctrl(conn, buf)
            except (EOFError, TimeoutError) as exc:
                raise UnsupportedPlatformError(
                    f"Supervised browser worker terminated before completing prefill ({type(exc).__name__})"
                ) from exc

            if not resp.get("ok"):
                err_type = resp.get("error_type")
                err_msg = str(resp.get("error") or "Browser prefill failed")
                if err_type == "RestrictionDetectedError":
                    return ExecutionResult(
                        application_id=ctx.application.id,
                        status="MANUAL_ACTION_REQUIRED",
                        adapter_name=self.adapter_name,
                        mode="human_approved_browser",
                        browser_mode=ctx.options.browser_mode,
                        stopped_before_submit=True,
                        blocked_submit_attempts=int(resp.get("blocked_submit_attempts", 0)),
                        restriction_detected=err_msg,
                        os_firewall_enforced=bool(resp.get("os_firewall_enforced", False)),
                        error_message=err_msg,
                    )
                if err_type == "SSRFBlockedError":
                    raise SSRFBlockedError(err_msg)
                raise UnsupportedPlatformError(err_msg)

            fields_filled = [FieldFillOutcome.model_validate(f) for f in resp.get("fields_filled", [])]
            session_info: HandoffSessionInfo | None = None
            if ctx.options.browser_mode == "visible_handoff":
                keep_alive = True
                session_info = self.handoff_manager.register_session(
                    _ManagedHandoffSession(
                        handoff_session_id=handoff_id,
                        application_id=ctx.application.id,
                        user_id=user_id,
                        destination_url=ctx.application.destination_url,
                        form_action=resp.get("detected_form_action"),
                        http_method=resp.get("detected_http_method"),
                        answers_hash=answers_hash,
                        resume_checksum=ctx.resume.sha256_checksum,
                        approval_snapshot_hash=ctx.approval_record.approval_snapshot_hash,
                        created_at=now,
                        expires_at=expires_at,
                        supervisor=supervisor,
                        process_registry=self.handoff_manager.process_registry,
                        ctrl_sock=conn,
                        ctrl_buf=buf,
                    )
                )

            return ExecutionResult(
                application_id=ctx.application.id,
                status="MANUAL_ACTION_REQUIRED",
                adapter_name=self.adapter_name,
                mode="human_approved_browser",
                browser_mode=ctx.options.browser_mode,
                stopped_before_submit=True,
                blocked_submit_attempts=int(resp.get("blocked_submit_attempts", 0)),
                detected_form_action=resp.get("detected_form_action"),
                detected_http_method=resp.get("detected_http_method"),
                fields_filled=fields_filled,
                unsupported_fields=list(resp.get("unsupported_fields", [])),
                resume_attached=bool(resp.get("resume_attached", False)),
                resume_checksum=ctx.resume.sha256_checksum if resp.get("resume_attached") else None,
                os_firewall_enforced=bool(resp.get("os_firewall_enforced", False)),
                artifact_ids=list(resp.get("artifact_ids", [])),
                handoff_session=session_info,
            )
        finally:
            try:
                ctrl_srv.close()
            except OSError:
                pass
            if not keep_alive:
                if conn is not None:
                    try:
                        conn.close()
                    except OSError:
                        pass
                supervisor.close(terminate_processes=True)
                self.handoff_manager.process_registry.remove_manifest(str(handoff_id))

    def execute(self, ctx: ExecutionContext) -> ExecutionResult:
        raise UnsupportedPlatformError(
            "Use execute_with_validated_context with verified profile, mapping, and encrypted answers"
        )

