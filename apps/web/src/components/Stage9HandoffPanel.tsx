"use client";

import React from "react";
import {
  AlertTriangle,
  CheckCircle2,
  FileCheck2,
  Play,
  RefreshCw,
  ShieldCheck,
  XCircle,
} from "lucide-react";
import type {
  ApplicationDetailResponse,
  ExecutionResult,
  HandoffSessionInfo,
  JobDetailResponse,
  ResumeGenerationHistoryResponse,
  TailoredResumeDetailResponse,
} from "@/lib/types";
import { StatusBadge } from "@/components/StatusBadge";

export interface Stage9HandoffPanelProps {
  job: JobDetailResponse;
  applicationDetail: ApplicationDetailResponse;
  activeResume: ResumeGenerationHistoryResponse | null;
  tailoredDetail: TailoredResumeDetailResponse | null;
  activeHandoff: HandoffSessionInfo | null;
  executionResult: ExecutionResult | null;
  actionBusy: boolean;
  onExecuteApplication: (browserMode: "headless" | "visible_handoff") => Promise<void>;
  onRefreshHandoff: (handoffSessionId: string) => Promise<void>;
  onCloseHandoff: (handoffSessionId: string) => Promise<void>;
}

export function Stage9HandoffPanel({
  job,
  applicationDetail,
  activeResume,
  tailoredDetail,
  activeHandoff,
  executionResult,
  actionBusy,
  onExecuteApplication,
  onRefreshHandoff,
  onCloseHandoff,
}: Stage9HandoffPanelProps) {
  const app = applicationDetail.application;
  const latestHandoff = activeHandoff ?? applicationDetail.active_handoff_sessions[0] ?? null;
  const canLaunch = app.status === "APPROVED" && !activeHandoff && !actionBusy;

  const sessionExpiredOrEnded =
    latestHandoff &&
    (latestHandoff.status === "expired" ||
      latestHandoff.status === "closed" ||
      latestHandoff.status === "crashed");

  return (
    <div className="space-y-4">
      <div className="rounded-xl border border-[var(--accent-primary-border)] bg-[var(--accent-primary-soft)] p-4 text-xs text-[var(--ink-primary)]">
        <div className="flex items-start gap-2.5">
          <ShieldCheck className="mt-0.5 h-4 w-4 shrink-0 text-[var(--accent-primary)]" aria-hidden="true" />
          <div className="space-y-1">
            <p className="font-semibold">
              Human-Controlled Browser Handoff — Final Submit Remains Under Your Control
            </p>
            <p className="text-[var(--ink-secondary)]">
              ApplyPilot prefills approved fields and attaches your tailored PDF inside an
              SSRF-filtered, isolated Chromium context. ApplyPilot never clicks Submit
              automatically and never bypasses CAPTCHA, MFA, or authentication walls.
            </p>
          </div>
        </div>
      </div>

      {/* Application & Resume Context */}
      <div className="rounded-xl border border-[var(--border-hairline)] bg-[var(--bg-rail)] p-4 text-xs space-y-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div>
            <p className="text-sm font-semibold text-[var(--ink-primary)]">
              {job.company} — {job.role_title}
            </p>
            <p className="mt-0.5 break-all font-mono text-[11px] text-[var(--ink-secondary)]">
              {app.destination_url}
            </p>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <StatusBadge
              state={app.status === "FAILED" || app.status === "UNVERIFIED" ? "failed" : "current"}
              customLabel={`APP: ${app.status}`}
              mono
            />
            <StatusBadge
              state={
                latestHandoff?.status === "active"
                  ? "completed"
                  : sessionExpiredOrEnded
                    ? "failed"
                    : "waiting"
              }
              customLabel={`SESSION: ${(latestHandoff?.status ?? "not_started").toUpperCase()}`}
              mono
            />
          </div>
        </div>

        <div className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-[var(--border-hairline)] bg-[var(--bg-surface)] p-3">
          <div className="space-y-0.5">
            <p className="flex items-center gap-1.5 font-medium text-[var(--ink-primary)]">
              <FileCheck2 className="h-3.5 w-3.5 text-[var(--state-completed-fg)]" aria-hidden="true" />
              <span>
                {activeResume
                  ? `Tailored PDF v${activeResume.version_number} (${activeResume.approval_status})`
                  : `Resume ${app.resume_id.slice(0, 8)}`}
              </span>
            </p>
            <p className="break-all font-mono text-[11px] text-[var(--ink-muted)]">
              SHA-256: {tailoredDetail?.sha256_checksum ?? app.resume_id}
            </p>
          </div>

          <div className="flex flex-wrap items-center gap-2">
            {app.status === "APPROVED" && !activeHandoff && (
              <button
                type="button"
                disabled={!canLaunch}
                onClick={() => onExecuteApplication("visible_handoff")}
                className="inline-flex items-center gap-1.5 rounded-xl bg-[var(--accent-primary)] px-4 py-2 text-xs font-semibold text-white hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-50"
              >
                <Play className="h-3.5 w-3.5" aria-hidden="true" />
                <span>{actionBusy ? "Launching Handoff..." : "Launch Visible Browser Handoff"}</span>
              </button>
            )}
            {latestHandoff && (
              <button
                type="button"
                disabled={actionBusy}
                onClick={() => onRefreshHandoff(latestHandoff.handoff_session_id)}
                className="inline-flex items-center gap-1.5 rounded-lg border border-[var(--border-hairline)] bg-[var(--bg-rail)] px-3 py-1.5 text-xs font-medium text-[var(--ink-secondary)] hover:border-[var(--border-strong)] disabled:opacity-50"
              >
                <RefreshCw className="h-3.5 w-3.5" aria-hidden="true" />
                <span>Refresh Session Status</span>
              </button>
            )}
            {activeHandoff && (
              <button
                type="button"
                disabled={actionBusy}
                onClick={() => onCloseHandoff(activeHandoff.handoff_session_id)}
                className="inline-flex items-center gap-1.5 rounded-lg border border-[var(--state-blocked-border)] bg-[var(--bg-surface)] px-3 py-1.5 text-xs font-medium text-[var(--state-blocked-fg)] hover:bg-[var(--state-blocked-bg)] disabled:opacity-50"
              >
                <XCircle className="h-3.5 w-3.5" aria-hidden="true" />
                <span>Close Handoff Safely</span>
              </button>
            )}
          </div>
        </div>
      </div>

      {/* Session Lifecycle & Recovery Details */}
      {latestHandoff && (
        <div className="rounded-xl border border-[var(--border-hairline)] bg-[var(--bg-surface)] p-4 text-xs space-y-2">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <span className="font-semibold text-[var(--ink-primary)]">
              Browser Handoff Session Lifecycle
            </span>
            <span className="font-mono text-[11px] text-[var(--ink-muted)]">
              {latestHandoff.handoff_session_id}
            </span>
          </div>
          <p className="text-[var(--ink-secondary)]">
            Created: {new Date(latestHandoff.created_at).toLocaleString()} · Expires:{" "}
            {new Date(latestHandoff.expires_at).toLocaleString()} · Manual Submit Armed:{" "}
            <code className="font-mono">{String(latestHandoff.manual_submit_armed)}</code>
          </p>
          {sessionExpiredOrEnded && (
            <div className="flex items-start gap-2 rounded-lg border border-[var(--state-waiting-border)] bg-[var(--state-waiting-bg)] p-2.5 text-[var(--state-waiting-fg)]">
              <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" />
              <span>
                Handoff session is <strong>{latestHandoff.status}</strong>. Do not retry an
                uncertain submission automatically — inspect your portal or record the outcome in
                Step 11.
              </span>
            </div>
          )}
        </div>
      )}

      {/* Errors / Restrictions */}
      {(app.error_message || executionResult?.restriction_detected || executionResult?.error_message) && (
        <div className="flex items-start gap-2 rounded-xl border border-[var(--state-failed-border)] bg-[var(--state-failed-bg)] p-3.5 text-xs text-[var(--state-failed-fg)]">
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" />
          <div className="space-y-1">
            <p className="font-semibold">Restriction or Execution Notice Requiring User Action</p>
            <p>
              {executionResult?.restriction_detected ||
                executionResult?.error_message ||
                app.error_message}
            </p>
          </div>
        </div>
      )}

      {/* Prefill Field Outcomes & Unsupported Fields */}
      {executionResult && (
        <div className="rounded-xl border border-[var(--border-hairline)] bg-[var(--bg-surface)] p-4 text-xs space-y-3">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <span className="font-semibold text-[var(--ink-primary)]">
              Detected Form Fields &amp; Prefill Outcomes
            </span>
            <span className="text-[var(--ink-secondary)] tabular-nums">
              Stopped Before Submit: <strong>{String(executionResult.stopped_before_submit)}</strong> ·
              Blocked Attempts: <strong>{executionResult.blocked_submit_attempts}</strong>
            </span>
          </div>

          <dl className="grid gap-2 rounded-lg bg-[var(--bg-rail)] p-3 sm:grid-cols-3">
            <div>
              <dt className="text-[var(--ink-muted)]">Detected Form Action</dt>
              <dd className="mt-0.5 break-all font-mono text-[11px] text-[var(--ink-primary)]">
                {executionResult.detected_form_action || "—"}
              </dd>
            </div>
            <div>
              <dt className="text-[var(--ink-muted)]">HTTP Method</dt>
              <dd className="mt-0.5 font-mono text-[var(--ink-primary)]">
                {executionResult.detected_http_method || "—"}
              </dd>
            </div>
            <div>
              <dt className="text-[var(--ink-muted)]">Resume &amp; Isolation</dt>
              <dd className="mt-0.5 text-[var(--ink-primary)]">
                PDF attached: {String(executionResult.resume_attached)} · OS firewall:{" "}
                {String(executionResult.os_firewall_enforced)}
              </dd>
            </div>
          </dl>

          {executionResult.fields_filled.length > 0 && (
            <ul className="divide-y divide-[var(--border-hairline)]">
              {executionResult.fields_filled.map((f) => (
                <li
                  key={`${f.question_key}-${f.status}`}
                  className="flex flex-wrap items-center justify-between gap-2 py-2"
                >
                  <div>
                    <span className="font-mono font-medium text-[var(--ink-primary)]">
                      {f.question_key}
                    </span>
                    {f.reason && (
                      <p className="text-[11px] text-[var(--ink-muted)]">{f.reason}</p>
                    )}
                  </div>
                  <StatusBadge
                    state={f.status === "filled" ? "completed" : "waiting"}
                    customLabel={f.status}
                    mono
                  />
                </li>
              ))}
            </ul>
          )}

          {executionResult.unsupported_fields.length > 0 && (
            <div className="rounded-lg border border-[var(--state-waiting-border)] bg-[var(--state-waiting-bg)] p-2.5 text-[var(--state-waiting-fg)]">
              <p className="font-semibold">Unsupported / Manual Fields</p>
              <p className="mt-0.5 font-mono text-[11px]">
                {executionResult.unsupported_fields.join(", ")}
              </p>
            </div>
          )}

          {executionResult.fields_filled.length === 0 &&
            executionResult.unsupported_fields.length === 0 && (
              <p className="flex items-center gap-1.5 text-[var(--ink-muted)]">
                <CheckCircle2 className="h-3.5 w-3.5 text-[var(--state-completed-fg)]" aria-hidden="true" />
                <span>Prefill completed with no unsupported fields reported.</span>
              </p>
            )}
        </div>
      )}
    </div>
  );
}
