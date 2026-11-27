import argparse
import base64
from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import socket
import sys
import threading
from typing import Any
from urllib.parse import urljoin, urlparse
import uuid

from playwright.sync_api import Page, sync_playwright

from app.agents.application.form_understanding import classify_sensitive_question
from app.schemas.applications import (
    DetectedFormField,
    ExecutionOptions,
    FieldFillOutcome,
)
from app.services.browser_executor import (
    ALLOWED_CSRF_FIELD_NAMES,
    LocalSSRFEgressProxy,
    SubmitGuardState,
    build_protected_chromium_args,
    create_isolated_browser_context,
    forward_held_route_safely,
    install_submit_protection,
    save_masked_application_artifact,
    verify_windows_egress_firewall,
)
from app.services.execution_adapters import (
    ApprovalValidationError,
    ManualSubmitAuthorization,
    RestrictionDetectedError,
    SSRFBlockedError,
    SubmitBlockedViolationError,
)

_CAPTCHA_SELECTORS = (
    "iframe[src*='recaptcha']",
    ".g-recaptcha",
    "iframe[src*='hcaptcha']",
    ".h-captcha",
    "iframe[src*='challenges.cloudflare.com']",
    ".cf-turnstile",
)
_MFA_KEYWORDS = ("one-time password", "verification code", "two-factor", "2fa code", "authenticator app")
_APPLY_REDACT_OVERLAYS_JS = """
() => {
    const sel = "input[type='password'], input[data-ap-sensitive='1'], textarea[data-ap-sensitive='1'], select[data-ap-sensitive='1']";
    let style = document.getElementById('ap-redact-style');
    if (!style) {
        style = document.createElement('style');
        style.id = 'ap-redact-style';
        style.textContent = "input[type='password'], input[data-ap-sensitive='1'], textarea[data-ap-sensitive='1'], select[data-ap-sensitive='1'] { color: transparent !important; text-shadow: none !important; caret-color: transparent !important; }";
        (document.head || document.documentElement).appendChild(style);
    }
    document.querySelectorAll('.ap-redact-mask').forEach(el => el.remove());
    for (const field of document.querySelectorAll(sel)) {
        const rect = field.getBoundingClientRect();
        if (rect.width <= 0 || rect.height <= 0) continue;
        const mask = document.createElement('div');
        mask.className = 'ap-redact-mask';
        mask.textContent = '[REDACTED]';
        Object.assign(mask.style, {
            position: 'fixed',
            left: rect.left + 'px',
            top: rect.top + 'px',
            width: rect.width + 'px',
            height: rect.height + 'px',
            backgroundColor: '#111827',
            color: '#f9fafb',
            fontFamily: 'monospace',
            fontSize: '12px',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            zIndex: '2147483647',
            pointerEvents: 'none',
            borderRadius: '2px',
        });
        (document.body || document.documentElement).appendChild(mask);
    }
}
"""
_REMOVE_REDACT_OVERLAYS_JS = """
() => {
    document.querySelectorAll('.ap-redact-mask').forEach(el => el.remove());
    const style = document.getElementById('ap-redact-style');
    if (style) style.remove();
}
"""


def _send_frame(sock: socket.socket, payload: dict[str, Any]) -> None:
    raw = (json.dumps(payload, ensure_ascii=True) + "\n").encode("utf-8")
    sock.sendall(raw)


def _recv_frame(sock: socket.socket, buf: bytearray) -> dict[str, Any]:
    while True:
        nl = buf.find(b"\n")
        if nl != -1:
            line = bytes(buf[:nl])
            del buf[: nl + 1]
            return json.loads(line.decode("utf-8"))
        chunk = sock.recv(65536)
        if not chunk:
            raise EOFError("Control channel closed")
        buf.extend(chunk)


def _normalize_url_for_compare(url: str) -> str:
    parsed = urlparse((url or "").strip())
    path = parsed.path.rstrip("/") or "/"
    port = parsed.port
    default_port = 443 if parsed.scheme.lower() == "https" else 80
    netloc = parsed.hostname.lower() if parsed.hostname else ""
    if port is not None and port != default_port:
        netloc = f"{netloc}:{port}"
    return f"{parsed.scheme.lower()}://{netloc}{path}"


def detect_page_restrictions(page: Page) -> str | None:
    """Detect CAPTCHA, MFA, or login walls without attempting any bypass."""
    for sel in _CAPTCHA_SELECTORS:
        try:
            if page.locator(sel).count() > 0:
                return f"CAPTCHA widget detected ({sel})"
        except Exception:
            pass

    try:
        body_text = (page.locator("body").inner_text(timeout=1500) or "").lower()
    except Exception:
        body_text = ""

    for kw in _MFA_KEYWORDS:
        if kw in body_text:
            return f"MFA/verification barrier detected ({kw})"

    try:
        has_password = page.locator("input[type='password']").count() > 0
        has_form = page.locator("form").count() > 0
        if has_password and not has_form:
            return "Authentication/login wall detected"
        if has_password and any(w in body_text for w in ("sign in to continue", "log in to apply")):
            return "Login authentication required"
    except Exception:
        pass
    return None


def inspect_primary_form(page: Page, *, base_url: str) -> tuple[str | None, str | None, list[DetectedFormField]]:
    """Deterministically extract primary application form metadata and controls from the DOM."""
    raw = page.evaluate(
        """() => {
            const forms = Array.from(document.querySelectorAll('form'));
            const form = forms.length > 0 ? forms[0] : null;
            const action = form ? (form.getAttribute('action') || window.location.href) : null;
            const method = form ? ((form.getAttribute('method') || 'GET').toUpperCase()) : null;
            const container = form || document.body;
            const controls = Array.from(container.querySelectorAll('input, select, textarea'));

            const labelsById = {};
            for (const lbl of Array.from(document.querySelectorAll('label[for]'))) {
                const f = lbl.getAttribute('for');
                if (f) labelsById[f] = (lbl.innerText || lbl.textContent || '').trim();
            }

            const seenRadioGroups = new Set();
            const out = [];
            for (let idx = 0; idx < controls.length; idx++) {
                const el = controls[idx];
                const tag = el.tagName.toLowerCase();
                const type = (el.getAttribute('type') || (tag === 'textarea' ? 'textarea' : tag === 'select' ? 'select' : 'text')).toLowerCase();
                if (['hidden', 'submit', 'button', 'reset', 'image'].includes(type)) continue;

                const name = el.getAttribute('name') || '';
                const id = el.getAttribute('id') || '';
                if (type === 'radio') {
                    const rkey = name || id || `radio_${idx}`;
                    if (seenRadioGroups.has(rkey)) continue;
                    seenRadioGroups.add(rkey);
                }

                let label = '';
                if (id && labelsById[id]) label = labelsById[id];
                if (!label) {
                    const parentLbl = el.closest('label');
                    if (parentLbl) label = (parentLbl.innerText || parentLbl.textContent || '').trim();
                }
                if (!label) label = el.getAttribute('aria-label') || el.getAttribute('placeholder') || name || id;

                const selector = id
                    ? `#${CSS.escape(id)}`
                    : name
                    ? `${tag}[name="${name.replace(/"/g, '\\"')}"]`
                    : `${tag}:nth-of-type(${idx + 1})`;

                let options = [];
                if (tag === 'select') {
                    options = Array.from(el.querySelectorAll('option')).map(o => (o.value || o.textContent || '').trim()).filter(Boolean);
                } else if (type === 'radio' && name) {
                    options = Array.from(container.querySelectorAll(`input[type="radio"][name="${name.replace(/"/g, '\\"')}"]`)).map(r => (r.value || '').trim()).filter(Boolean);
                }

                out.push({
                    selector,
                    name,
                    id,
                    label: (label || '').trim(),
                    field_type: type,
                    required: el.hasAttribute('required') || el.getAttribute('aria-required') === 'true',
                    options,
                });
            }
            return { action, method, fields: out };
        }"""
    )
    action_raw = raw.get("action")
    abs_action = urljoin(base_url, action_raw) if action_raw else None
    method = raw.get("method")
    if method not in {"GET", "POST"}:
        method = None

    fields: list[DetectedFormField] = []
    for item in raw.get("fields", []):
        name = str(item.get("name") or "").strip()
        el_id = str(item.get("id") or "").strip()
        label = str(item.get("label") or name or el_id).strip()
        if not label:
            continue
        key_base = (name or el_id or label).strip().lower().replace("-", "_").replace(" ", "_")
        ftype = str(item.get("field_type") or "text").lower()
        if ftype not in {"text", "textarea", "select", "radio", "checkbox", "number", "date", "file", "url", "email", "phone"}:
            ftype = "text"
        fields.append(
            DetectedFormField(
                selector=str(item["selector"]),
                question_key=key_base[:160],
                label=label[:1000],
                field_type=ftype,  # type: ignore[arg-type]
                required=bool(item.get("required", True)),
                options=[str(o) for o in item.get("options", [])],
            )
        )
    return abs_action, method, fields


def _match_verified_answer_strict(
    *,
    field: DetectedFormField,
    verified_answers: dict[str, str],
    verified_keys: set[str],
    unverified_keys: set[str],
    sensitive_unconfirmed_keys: set[str],
) -> tuple[str | None, FieldFillOutcome]:
    """Match detected form field to verified answers WITHOUT fuzzy guessing."""
    candidates: list[str] = []
    norm_label = "_".join(field.label.lower().split())
    for k in verified_keys | unverified_keys | sensitive_unconfirmed_keys:
        if k == field.question_key or k == norm_label:
            candidates.append(k)

    # Allow strict unambiguous field_type match ONLY if unique
    if not candidates and field.field_type in {"email", "phone"}:
        type_matches = [k for k in verified_keys if k in {field.field_type, f"{field.field_type}_address", f"{field.field_type}_number"}]
        if len(type_matches) == 1:
            candidates = type_matches

    if len(candidates) > 1:
        return None, FieldFillOutcome(
            question_key=field.question_key,
            status="unsupported",
            reason="Ambiguous field mapping candidates",
        )

    is_sensitive = classify_sensitive_question(field.label, field.question_key) is not None
    if not candidates:
        if is_sensitive:
            return None, FieldFillOutcome(
                question_key=field.question_key,
                status="skipped_unverified",
                reason="Sensitive question requires explicit user input and confirmation",
            )
        return None, FieldFillOutcome(
            question_key=field.question_key,
            status="skipped_missing",
            reason="No exact verified answer found",
        )

    matched_key = candidates[0]
    if matched_key in sensitive_unconfirmed_keys or (is_sensitive and matched_key not in verified_keys):
        return None, FieldFillOutcome(
            question_key=field.question_key,
            status="skipped_unverified",
            reason="Sensitive answer lacks explicit user confirmation",
        )
    if matched_key in unverified_keys or matched_key not in verified_keys:
        return None, FieldFillOutcome(
            question_key=field.question_key,
            status="skipped_unverified",
            reason="Answer is unverified and cannot be auto-filled",
        )

    val = verified_answers.get(matched_key)
    if val is None or not str(val):
        return None, FieldFillOutcome(
            question_key=field.question_key,
            status="skipped_missing",
            reason="Verified answer value missing",
        )
    return val, FieldFillOutcome(question_key=field.question_key, status="filled")


def prefill_page_safely(
    *,
    page: Page,
    destination_url: str,
    verified_resume_bytes: bytes | None = None,
    verified_resume_filename: str = "resume.pdf",
    expected_resume_checksum: str | None = None,
    verified_resume_path: Path | None = None,
    verified_answers: dict[str, str],
    verified_keys: set[str],
    unverified_keys: set[str],
    sensitive_unconfirmed_keys: set[str],
    submit_state: SubmitGuardState,
    egress_proxy: LocalSSRFEgressProxy,
    timeout_ms: int = 30000,
    capture_artifacts: bool = False,
    storage_root: Path | None = None,
    user_id: str | None = None,
    application_id: str | None = None,
    egress_timeout_pause_ms: int = 100,
) -> dict[str, Any]:
    """Navigate, inspect, map, fill verified fields, attach resume, and STOP before submit."""
    resume_bytes: bytes
    resume_name = Path(verified_resume_filename or "resume.pdf").name or "resume.pdf"
    if verified_resume_bytes is not None:
        resume_bytes = verified_resume_bytes
    elif verified_resume_path is not None:
        resume_name = verified_resume_path.name or resume_name
        with open(verified_resume_path, "rb") as fh:
            resume_bytes = fh.read()
    else:
        raise ApprovalValidationError("Verified resume bytes or path must be provided")

    if not resume_bytes:
        raise ApprovalValidationError("Verified resume content is empty")
    if expected_resume_checksum is not None:
        actual_sha256 = hashlib.sha256(resume_bytes).hexdigest()
        if not hmac.compare_digest(actual_sha256, expected_resume_checksum):
            raise ApprovalValidationError("Verified resume buffer checksum verification failed")

    egress_proxy.validate_url(destination_url, allow_websocket=False)
    try:
        page.goto(destination_url, wait_until="domcontentloaded", timeout=timeout_ms)
    except Exception as exc:
        raise SSRFBlockedError(f"Navigation failed or blocked: {exc}") from exc

    # Revalidate post-redirect destination URL strictly
    final_url = page.url
    egress_proxy.validate_url(final_url, allow_websocket=False)

    barrier = detect_page_restrictions(page)
    if barrier:
        raise RestrictionDetectedError(barrier)

    form_action, form_method, detected_fields = inspect_primary_form(page, base_url=final_url)
    submit_state.mark_prefill_complete(
        final_url=final_url,
        detected_form_action=form_action,
        detected_http_method=form_method,
    )
    baseline_csrf_raw: dict[str, str] = page.evaluate(
        """(allowedNames) => {
            const allowed = new Set(allowedNames);
            const forms = Array.from(document.querySelectorAll('form'));
            const form = forms.length > 0 ? forms[0] : null;
            if (!form) return {};
            const out = {};
            for (const el of Array.from(form.querySelectorAll('input[type="hidden"][name]'))) {
                const n = (el.getAttribute('name') || '').trim();
                const v = (el.value || '');
                if (n && allowed.has(n.toLowerCase()) && v.trim()) {
                    out[n] = v;
                }
            }
            return out;
        }""",
        sorted(ALLOWED_CSRF_FIELD_NAMES),
    )
    outcomes: list[FieldFillOutcome] = []
    unsupported_fields: list[str] = []
    resume_attached = False
    expected_fields: dict[str, list[str]] = {}
    expected_file_fields: dict[str, str] = {}
    live_resume_sha = hashlib.sha256(resume_bytes).hexdigest()

    for f in detected_fields:
        loc = page.locator(f.selector).first
        ctrl_name = str(loc.evaluate("el => (el.getAttribute('name') || '').trim()") or "")
        if f.field_type == "file":
            loc.set_input_files(
                {
                    "name": resume_name,
                    "mimeType": "application/pdf",
                    "buffer": resume_bytes,
                }
            )
            resume_attached = True
            if ctrl_name:
                expected_file_fields[ctrl_name] = live_resume_sha
            outcomes.append(FieldFillOutcome(question_key=f.question_key, status="filled"))
            continue

        val, outcome = _match_verified_answer_strict(
            field=f,
            verified_answers=verified_answers,
            verified_keys=verified_keys,
            unverified_keys=unverified_keys,
            sensitive_unconfirmed_keys=sensitive_unconfirmed_keys,
        )
        if val is None:
            outcomes.append(outcome)
            if outcome.status == "unsupported":
                unsupported_fields.append(f.question_key)
            elif ctrl_name and f.field_type in {"text", "textarea", "email", "phone", "number", "date", "url"}:
                cur_val = str(loc.evaluate("el => el.value || ''") or "")
                expected_fields[ctrl_name] = [cur_val]
            continue

        is_sens = classify_sensitive_question(f.label, f.question_key) is not None
        if f.field_type in {"text", "textarea", "email", "phone", "number", "date", "url"}:
            if is_sens:
                loc.evaluate("el => el.setAttribute('data-ap-sensitive', '1')")
            loc.fill(val)
            if ctrl_name:
                expected_fields[ctrl_name] = [val]
            outcomes.append(outcome)
        elif f.field_type == "select":
            if f.options and val not in f.options:
                outcomes.append(
                    FieldFillOutcome(
                        question_key=f.question_key,
                        status="unsupported",
                        reason="Verified answer does not match select options",
                    )
                )
                unsupported_fields.append(f.question_key)
            else:
                loc.select_option(val)
                if ctrl_name:
                    expected_fields[ctrl_name] = [str(loc.evaluate("el => el.value || ''") or val)]
                outcomes.append(outcome)
        elif f.field_type == "checkbox":
            if val.lower() in {"true", "yes", "1"}:
                loc.check()
                if ctrl_name:
                    expected_fields[ctrl_name] = [str(loc.evaluate("el => el.getAttribute('value') || 'on'") or "on")]
            else:
                loc.uncheck()
            outcomes.append(outcome)
        elif f.field_type == "radio":
            if f.options and val not in f.options:
                outcomes.append(
                    FieldFillOutcome(
                        question_key=f.question_key,
                        status="unsupported",
                        reason="Verified answer does not match radio options",
                    )
                )
                unsupported_fields.append(f.question_key)
            else:
                radio_sel = f"input[type='radio'][value='{val}']"
                page.locator(radio_sel).first.check()
                if ctrl_name:
                    expected_fields[ctrl_name] = [val]
                outcomes.append(outcome)
        else:
            outcomes.append(FieldFillOutcome(question_key=f.question_key, status="unsupported", reason="Unsupported control"))
            unsupported_fields.append(f.question_key)

    submit_state.set_expected_payload_policy(
        expected_fields=expected_fields,
        expected_file_fields=expected_file_fields,
        allowed_csrf_tokens=baseline_csrf_raw,
    )

    if egress_timeout_pause_ms > 0:
        page.wait_for_timeout(egress_timeout_pause_ms)

    artifact_ids: list[str] = []
    if capture_artifacts and storage_root is not None and user_id and application_id:
        page.evaluate(_APPLY_REDACT_OVERLAYS_JS)
        try:
            png_bytes = page.screenshot(full_page=False)
        finally:
            try:
                page.evaluate(_REMOVE_REDACT_OVERLAYS_JS)
            except Exception:
                pass
        art_id = save_masked_application_artifact(
            storage_root=storage_root,
            user_id=user_id,
            application_id=application_id,
            png_bytes=png_bytes,
        )
        artifact_ids.append(art_id)

    return {
        "final_url": final_url,
        "detected_form_action": form_action,
        "detected_http_method": form_method,
        "fields_filled": [o.model_dump() for o in outcomes],
        "unsupported_fields": unsupported_fields,
        "resume_attached": resume_attached,
        "blocked_submit_attempts": submit_state.blocked_attempts,
        "artifact_ids": artifact_ids,
    }


def _run_worker(ctrl_port: int) -> int:
    ctrl_secret = os.environ.pop("APPLYPILOT_CTRL_SECRET", "")
    if not ctrl_secret:
        return 2

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(30.0)
    sock.connect(("127.0.0.1", ctrl_port))
    buf = bytearray()

    try:
        hello = _recv_frame(sock, buf)
        challenge = str(hello.get("challenge") or "")
        sig = hmac.new(ctrl_secret.encode("utf-8"), challenge.encode("utf-8"), "sha256").hexdigest()
        _send_frame(sock, {"hmac": sig})

        cmd_msg = _recv_frame(sock, buf)
        if cmd_msg.get("cmd") != "PREFILL":
            _send_frame(sock, {"ok": False, "error": "Unexpected initial command"})
            return 1

        worker_user_id = uuid.UUID(str(cmd_msg["user_id"]))
        worker_application_id = uuid.UUID(str(cmd_msg["application_id"]))
        worker_handoff_session_id = uuid.UUID(str(cmd_msg["handoff_session_id"]))
        verified_resume_checksum = str(cmd_msg["verified_resume_checksum"])
        verified_resume_filename = str(cmd_msg.get("verified_resume_filename") or "resume.pdf")
        worker_answers_hash = str(cmd_msg["answers_hash"])
        worker_approval_snapshot_hash = str(cmd_msg["approval_snapshot_hash"])
        try:
            verified_resume_bytes = base64.b64decode(str(cmd_msg["verified_resume_bytes_b64"]), validate=True)
        except Exception:
            _send_frame(sock, {"ok": False, "error_type": "ApprovalValidationError", "error": "Invalid resume payload"})
            return 1

        if not verified_resume_bytes or not hmac.compare_digest(
            hashlib.sha256(verified_resume_bytes).hexdigest(), verified_resume_checksum
        ):
            _send_frame(
                sock,
                {
                    "ok": False,
                    "error_type": "ApprovalValidationError",
                    "error": "Resume checksum verification failed inside worker",
                },
            )
            return 1

        options = ExecutionOptions.model_validate(cmd_msg["options"])
        allowed_test_origins = frozenset(
            (str(h), int(p)) for h, p in cmd_msg.get("_allowed_test_origins", [])
        )
        timeout_ms = (options.timeout_seconds or 60) * 1000
        headless = options.browser_mode == "headless"

        submit_state = SubmitGuardState(expected_destination_url=cmd_msg["destination_url"])
        proxy = LocalSSRFEgressProxy(_allowed_test_origins=allowed_test_origins)
        proxy.start()
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(
                    headless=headless,
                    args=build_protected_chromium_args(),
                )
                fw_status = verify_windows_egress_firewall(chromium_path=pw.chromium.executable_path)
                if cmd_msg.get("require_os_egress_firewall") and not fw_status.enforced:
                    browser.close()
                    _send_frame(
                        sock,
                        {
                            "ok": False,
                            "error_type": "SSRFBlockedError",
                            "error": f"OS firewall required but not enforced: {fw_status.reason}",
                            "os_firewall_enforced": False,
                        },
                    )
                    return 0

                ctx = create_isolated_browser_context(browser, egress_proxy=proxy)
                install_submit_protection(ctx, state=submit_state)
                page = ctx.new_page()
                try:
                    prefill_res = prefill_page_safely(
                        page=page,
                        destination_url=cmd_msg["destination_url"],
                        verified_resume_bytes=verified_resume_bytes,
                        verified_resume_filename=verified_resume_filename,
                        expected_resume_checksum=verified_resume_checksum,
                        verified_answers=cmd_msg["verified_answers"],
                        verified_keys=set(cmd_msg["verified_keys"]),
                        unverified_keys=set(cmd_msg["unverified_keys"]),
                        sensitive_unconfirmed_keys=set(cmd_msg["sensitive_unconfirmed_keys"]),
                        submit_state=submit_state,
                        egress_proxy=proxy,
                        timeout_ms=timeout_ms,
                        capture_artifacts=bool(cmd_msg.get("capture_artifacts")),
                        storage_root=Path(cmd_msg["storage_root"]) if cmd_msg.get("storage_root") else None,
                        user_id=cmd_msg.get("user_id"),
                        application_id=cmd_msg.get("application_id"),
                    )
                    _send_frame(
                        sock,
                        {
                            "ok": True,
                            "os_firewall_enforced": fw_status.enforced,
                            **prefill_res,
                        },
                    )
                except RestrictionDetectedError as exc:
                    _send_frame(
                        sock,
                        {
                            "ok": False,
                            "error_type": "RestrictionDetectedError",
                            "error": str(exc),
                            "os_firewall_enforced": fw_status.enforced,
                            "blocked_submit_attempts": submit_state.blocked_attempts,
                        },
                    )
                    ctx.close()
                    browser.close()
                    return 0
                except SSRFBlockedError as exc:
                    _send_frame(
                        sock,
                        {
                            "ok": False,
                            "error_type": "SSRFBlockedError",
                            "error": str(exc),
                            "os_firewall_enforced": fw_status.enforced,
                            "blocked_submit_attempts": submit_state.blocked_attempts,
                        },
                    )
                    ctx.close()
                    browser.close()
                    return 0
                except Exception as exc:
                    _send_frame(
                        sock,
                        {
                            "ok": False,
                            "error_type": type(exc).__name__,
                            "error": f"{type(exc).__name__}: {str(exc)[:300]}",
                            "os_firewall_enforced": fw_status.enforced,
                            "blocked_submit_attempts": submit_state.blocked_attempts,
                        },
                    )
                    try:
                        ctx.close()
                    except Exception:
                        pass
                    try:
                        browser.close()
                    except Exception:
                        pass
                    return 0

                if options.browser_mode == "headless":
                    ctx.close()
                    browser.close()
                    return 0

                # Visible handoff control loop: stay responsive to authorization & close commands.
                sock.settimeout(0.1)
                stop_event = threading.Event()
                while not stop_event.is_set() and browser.is_connected():
                    try:
                        page.wait_for_timeout(50)
                    except Exception:
                        break
                    try:
                        nxt = _recv_frame(sock, buf)
                    except socket.timeout:
                        continue
                    except EOFError:
                        break

                    c_type = nxt.get("cmd")
                    if c_type == "ARM_MANUAL_SUBMIT":
                        auth_raw = nxt["authorization"]
                        pend_raw = nxt.get("pending_submission_id")
                        try:
                            pending_id = uuid.UUID(str(pend_raw))
                        except Exception:
                            _send_frame(sock, {"ok": False, "error": "Missing or invalid pending_submission_id"})
                            continue
                        auth = ManualSubmitAuthorization(
                            application_id=uuid.UUID(str(auth_raw["application_id"])),
                            handoff_session_id=uuid.UUID(str(auth_raw["handoff_session_id"])),
                            user_id=uuid.UUID(str(auth_raw["user_id"])),
                            destination_url=str(auth_raw["destination_url"]),
                            form_action=str(auth_raw["form_action"]),
                            http_method=str(auth_raw["http_method"]),  # type: ignore[arg-type]
                            answers_hash=str(auth_raw["answers_hash"]),
                            resume_checksum=str(auth_raw["resume_checksum"]),
                            approval_snapshot_hash=str(auth_raw["approval_snapshot_hash"]),
                            issued_at=datetime.fromisoformat(str(auth_raw["issued_at"])),
                            expires_at=datetime.fromisoformat(str(auth_raw["expires_at"])),
                        )
                        if (
                            auth.user_id != worker_user_id
                            or auth.application_id != worker_application_id
                            or auth.handoff_session_id != worker_handoff_session_id
                        ):
                            _send_frame(
                                sock,
                                {"ok": False, "error": "Authorization identity/session binding mismatch"},
                            )
                            continue
                        exp_utc = (
                            auth.expires_at.replace(tzinfo=timezone.utc)
                            if auth.expires_at.tzinfo is None
                            else auth.expires_at.astimezone(timezone.utc)
                        )
                        if exp_utc <= datetime.now(timezone.utc):
                            _send_frame(
                                sock,
                                {"ok": False, "error": "Manual submit authorization is already expired"},
                            )
                            continue
                        exp_sha = str(nxt.get("expected_payload_sha256") or "")
                        if len(exp_sha) != 64:
                            _send_frame(
                                sock,
                                {"ok": False, "error": "Missing or invalid expected_payload_sha256"},
                            )
                            continue
                        try:
                            held_route = submit_state.release_held_submission(
                                auth=auth,
                                pending_submission_id=pending_id,
                                live_answers_hash=worker_answers_hash,
                                live_resume_checksum=verified_resume_checksum,
                                live_approval_snapshot_hash=worker_approval_snapshot_hash,
                                expected_payload_sha256=exp_sha,
                            )
                        except SubmitBlockedViolationError as exc:
                            _send_frame(sock, {"ok": False, "error": str(exc)})
                            continue
                        try:
                            fwd_ok, fwd_status, _blocked_loc = forward_held_route_safely(
                                held_route,
                                state=submit_state,
                                egress_proxy=proxy,
                            )
                        except Exception as exc:
                            _send_frame(sock, {"ok": False, "error": f"Failed to forward held submission: {exc}"})
                            continue
                        if not fwd_ok:
                            _send_frame(
                                sock,
                                {
                                    "ok": False,
                                    "redirect_blocked": True,
                                    "redirect_status": fwd_status,
                                    "error": f"Blocked unapproved server redirect (HTTP {fwd_status}); manual inspection required",
                                },
                            )
                            continue
                        _send_frame(sock, {"ok": True, "status": fwd_status})
                    elif c_type == "STATUS":
                        pending_info = submit_state.get_pending_submission_info()
                        _send_frame(
                            sock,
                            {
                                "ok": True,
                                "connected": browser.is_connected(),
                                "blocked_submit_attempts": submit_state.blocked_attempts,
                                "authorized_submitted": submit_state.authorized_submissions,
                                "pending_submission": (
                                    pending_info.model_dump(mode="json") if pending_info is not None else None
                                ),
                            },
                        )
                    elif c_type == "CLOSE":
                        submit_state.abort_pending_submission()
                        _send_frame(sock, {"ok": True})
                        stop_event.set()

                try:
                    submit_state.abort_pending_submission()
                    ctx.close()
                except Exception:
                    pass
                try:
                    browser.close()
                except Exception:
                    pass
        finally:
            proxy.stop()
    finally:
        sock.close()
    return 0


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--control-port", type=int, required=True)
    args = parser.parse_args()
    sys.exit(_run_worker(args.control_port))


if __name__ == "__main__":
    main()
