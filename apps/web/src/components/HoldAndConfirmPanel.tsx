"use client";

import React, { useState } from "react";
import { Eye, EyeOff, FileCheck2, Lock, RefreshCw, ShieldAlert } from "lucide-react";
import type {
  ApplicationAnswerResponse,
  ApplicationDetailResponse,
  HandoffSessionInfo,
  JobSummaryResponse,
  ResumeGenerationHistoryResponse,
} from "@/lib/types";
import { StatusBadge } from "@/components/StatusBadge";

export interface HoldAndConfirmPanelProps {
  job: JobSummaryResponse;
  applicationDetail: ApplicationDetailResponse;
  approvedResume: ResumeGenerationHistoryResponse | null;
  resumeChecksum: string | null;
  activeHandoff: HandoffSessionInfo | null;
  busy: boolean;
  onRefreshHandoff: (handoffSessionId: string) => Promise<void>;
  onAuthorizeManualSubmit: (payload: {
    handoff_session_id: string;
    pending_submission_id: string;
    expected_form_action: string;
    expected_http_method: "GET" | "POST";
    expected_payload_sha256: string;
  }) => Promise<void>;
}

export function HoldAndConfirmPanel({
  job,
  applicationDetail,
  approvedResume,
  resumeChecksum,
  activeHandoff,
  busy,
  onRefreshHandoff,
  onAuthorizeManualSubmit,
}: HoldAndConfirmPanelProps) {
  const { application, answers } = applicationDetail;
  const pending = activeHandoff?.pending_submission ?? null;
  const [explicitAck, setExplicitAck] = useState(false);
  const [inspectSensitiveMeta, setInspectSensitiveMeta] = useState(false);
  const [shaOverride, setShaOverride] = useState("");

  const effectiveSha = (shaOverride || pending?.payload_sha256 || "").trim();
  const shaReady = /^[0-9a-fA-F]{64}$/.test(effectiveSha);
  const handoffEnded =
    activeHandoff?.status === "expired" ||
    activeHandoff?.status === "closed" ||
    activeHandoff?.status === "crashed";
  const pendingExpired = pending ? Date.parse(pending.expires_at) <= Date.now() : false;
  const canConfirm = Boolean(
    activeHandoff &&
      !handoffEnded &&
      pending &&
      !pendingExpired &&
      shaReady &&
      explicitAck &&
      !busy,
  );

  function renderAnswerPreview(ans: ApplicationAnswerResponse) {
    if (ans.is_sensitive) {
      return inspectSensitiveMeta
        ? ans.answer_preview || "[REDACTED — AES-256-GCM ENCRYPTED AT REST]"
        : "[Masked until inspection toggle enabled]";
    }
    return ans.answer_preview || "—";
  }

  return (
    <div className="space-y-5">
      <div className="rounded-xl border border-[var(--state-waiting-border)] bg-[var(--state-waiting-bg)] p-4">
        <div className="flex items-start gap-3">
          <ShieldAlert
            className="mt-0.5 h-5 w-5 shrink-0 text-[var(--state-waiting-fg)]"
            aria-hidden="true"
          />
          <div className="text-sm leading-relaxed text-[var(--state-waiting-fg)]">
            <p className="font-semibold">Hold-and-Confirm Security Boundary</p>
            <p className="mt-1 text-xs">
              ApplyPilot never clicks Submit on an employer portal automatically. First submit the
              prefilled form manually in the supervised browser window so ApplyPilot intercepts and
              holds the exact wire request here, then explicitly authorize that single held
              request by <code className="font-mono">pending_submission_id</code> and{" "}
              <code className="font-mono">expected_payload_sha256</code>.
            </p>
          </div>
        </div>
      </div>

      {(handoffEnded || pendingExpired || application.status === "FAILED" || application.status === "UNVERIFIED") && (
        <div className="rounded-xl border border-[var(--state-failed-border)] bg-[var(--state-failed-bg)] p-3.5 text-xs text-[var(--state-failed-fg)]">
          <p className="font-semibold">
            {pendingExpired
              ? "Held Pending Submission Expired"
              : handoffEnded
                ? `Handoff Session ${activeHandoff?.status?.toUpperCase()}`
                : `Application ${application.status}`}
          </p>
          <p className="mt-1">
            {application.error_message ||
              "An uncertain or expired submission is never automatically retried. Inspect the employer portal or record the outcome in Step 11."}
          </p>
        </div>
      )}

      {/* Employer & Resume Context */}
      <div className="grid gap-4 sm:grid-cols-2">
        <div className="rounded-xl border border-[var(--border-hairline)] bg-[var(--bg-rail)] p-4">
          <p className="text-xs font-semibold uppercase tracking-wider text-[var(--ink-muted)]">
            Employer Destination
          </p>
          <p className="mt-1.5 text-sm font-semibold text-[var(--ink-primary)]">
            {job.company} — {job.role_title}
          </p>
          <p className="mt-1 break-all font-mono text-xs text-[var(--ink-secondary)]">
            {application.destination_url}
          </p>
        </div>

        <div className="rounded-xl border border-[var(--border-hairline)] bg-[var(--bg-rail)] p-4">
          <p className="text-xs font-semibold uppercase tracking-wider text-[var(--ink-muted)]">
            Selected Approved Resume
          </p>
          <div className="mt-1.5 flex items-center gap-2 text-sm font-semibold text-[var(--ink-primary)]">
            <FileCheck2 className="h-4 w-4 text-[var(--state-completed-fg)]" aria-hidden="true" />
            <span>
              {approvedResume
                ? `Tailored PDF v${approvedResume.version_number} (${approvedResume.approval_status})`
                : `Resume ${application.resume_id.slice(0, 8)}`}
            </span>
          </div>
          <p className="mt-1 break-all font-mono text-xs text-[var(--ink-secondary)]">
            SHA-256: {resumeChecksum || application.resume_id}
          </p>
        </div>
      </div>

      {/* Server-Stored Approved Application Answers */}
      <div className="rounded-xl border border-[var(--border-hairline)] bg-[var(--bg-surface)] p-4">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div>
            <h3 className="text-sm font-semibold text-[var(--ink-primary)]">
              Previously Approved Application Answers (Database Record)
            </h3>
            <p className="text-xs text-[var(--ink-muted)]">
              These are your verified answers saved before handoff. Compare them against the actual
              captured wire request fields below before releasing the held submission.
            </p>
          </div>
          <button
            type="button"
            onClick={() => setInspectSensitiveMeta((v) => !v)}
            className="inline-flex items-center gap-1.5 rounded-lg border border-[var(--border-hairline)] bg-[var(--bg-rail)] px-2.5 py-1.5 text-xs font-medium text-[var(--ink-secondary)] hover:border-[var(--border-strong)]"
          >
            {inspectSensitiveMeta ? (
              <>
                <EyeOff className="h-3.5 w-3.5" aria-hidden="true" />
                <span>Hide Sensitive Status</span>
              </>
            ) : (
              <>
                <Eye className="h-3.5 w-3.5" aria-hidden="true" />
                <span>Inspect Sensitive Verification</span>
              </>
            )}
          </button>
        </div>

        {answers.length === 0 ? (
          <p className="mt-3 text-xs text-[var(--ink-muted)]">
            No extra questionnaire fields stored beyond the approved tailored PDF.
          </p>
        ) : (
          <dl className="mt-3 divide-y divide-[var(--border-hairline)] border-t border-[var(--border-hairline)]">
            {answers.map((ans) => (
              <div key={ans.id} className="grid gap-1 py-2.5 sm:grid-cols-12 sm:gap-4">
                <dt className="sm:col-span-5 text-xs font-medium text-[var(--ink-secondary)] flex items-center gap-1.5">
                  {ans.is_sensitive && (
                    <Lock className="h-3 w-3 shrink-0 text-[var(--state-waiting-fg)]" aria-hidden="true" />
                  )}
                  <span>{ans.question}</span>
                </dt>
                <dd className="sm:col-span-7 flex items-center justify-between gap-2 text-xs">
                  <span className="font-mono text-[var(--ink-primary)]">
                    {renderAnswerPreview(ans)}
                  </span>
                  <StatusBadge
                    state={ans.user_verified ? "completed" : "waiting"}
                    customLabel={ans.user_verified ? "Verified" : "Unverified"}
                  />
                </dd>
              </div>
            ))}
          </dl>
        )}
      </div>

      {/* Intercepted Pending Submission Contract */}
      <div className="rounded-xl border border-[var(--border-strong)] bg-[var(--bg-rail)] p-4">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="flex items-center gap-2">
            <h3 className="text-sm font-semibold text-[var(--ink-primary)]">
              Intercepted Pending Submission
            </h3>
            {pending ? (
              <StatusBadge state="current" customLabel="HELD READY" mono />
            ) : activeHandoff?.manual_submit_armed ? (
              <StatusBadge state="completed" customLabel="RELEASED" mono />
            ) : (
              <StatusBadge state="waiting" customLabel="AWAITING BROWSER SUBMIT" mono />
            )}
          </div>
          {activeHandoff && (
            <button
              type="button"
              disabled={busy}
              onClick={() => onRefreshHandoff(activeHandoff.handoff_session_id)}
              className="inline-flex items-center gap-1.5 rounded-lg border border-[var(--border-hairline)] bg-[var(--bg-surface)] px-2.5 py-1.5 text-xs font-medium text-[var(--ink-secondary)] hover:border-[var(--border-strong)] disabled:opacity-50"
            >
              <RefreshCw className="h-3.5 w-3.5" aria-hidden="true" />
              <span>Poll Handoff Session</span>
            </button>
          )}
        </div>

        {pending ? (
          <div className="mt-3 space-y-3">
            <dl className="grid gap-3 rounded-lg border border-[var(--border-hairline)] bg-[var(--bg-surface)] p-3 text-xs sm:grid-cols-2">
              <div>
                <dt className="text-[var(--ink-muted)]">pending_submission_id</dt>
                <dd className="mt-0.5 break-all font-mono font-medium text-[var(--ink-primary)]">
                  {pending.pending_submission_id}
                </dd>
              </div>
              <div>
                <dt className="text-[var(--ink-muted)]">expected_http_method</dt>
                <dd className="mt-0.5 font-mono font-semibold text-[var(--ink-primary)]">
                  {pending.http_method}
                </dd>
              </div>
              <div>
                <dt className="text-[var(--ink-muted)]">Detected At</dt>
                <dd className="mt-0.5 text-[var(--ink-primary)]">
                  {new Date(pending.detected_at).toLocaleString()}
                </dd>
              </div>
              <div>
                <dt className="text-[var(--ink-muted)]">Hold Expires At</dt>
                <dd className="mt-0.5 text-[var(--ink-primary)]">
                  {new Date(pending.expires_at).toLocaleString()}
                </dd>
              </div>
              <div className="sm:col-span-2">
                <dt className="text-[var(--ink-muted)]">expected_form_action</dt>
                <dd className="mt-0.5 break-all font-mono text-[var(--ink-primary)]">
                  {pending.target_url}
                </dd>
              </div>
              <div className="sm:col-span-2">
                <dt className="text-[var(--ink-muted)]">Held Payload Digest (payload_sha256)</dt>
                <dd className="mt-0.5 break-all font-mono text-[var(--ink-primary)]">
                  {pending.payload_sha256}
                </dd>
              </div>
            </dl>

            {/* Actual Intercepted Wire Request Field Preview */}
            <div className="rounded-lg border border-[var(--border-hairline)] bg-[var(--bg-surface)] p-3 text-xs space-y-2">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <span className="font-semibold text-[var(--ink-primary)]">
                  Actual Intercepted Request Fields (Captured Wire Payload)
                </span>
                <span className="font-mono text-[11px] text-[var(--ink-muted)]">
                  Bound to {pending.pending_submission_id.slice(0, 8)}…
                </span>
              </div>
              {!pending.captured_fields || pending.captured_fields.length === 0 ? (
                <p className="text-[var(--ink-muted)]">
                  No wire body or query fields captured on this {pending.http_method} navigation.
                </p>
              ) : (
                <ul className="divide-y divide-[var(--border-hairline)]">
                  {pending.captured_fields.map((field) => (
                    <li
                      key={`${field.field_kind}-${field.field_name}`}
                      className="flex flex-wrap items-center justify-between gap-2 py-2"
                    >
                      <div className="flex items-center gap-1.5">
                        {field.is_sensitive && (
                          <Lock
                            className="h-3 w-3 shrink-0 text-[var(--state-waiting-fg)]"
                            aria-hidden="true"
                          />
                        )}
                        <span className="font-mono font-medium text-[var(--ink-primary)]">
                          {field.field_name}
                        </span>
                        <span className="rounded bg-[var(--bg-rail)] px-1.5 py-0.5 font-mono text-[10px] text-[var(--ink-muted)]">
                          {field.field_kind}
                        </span>
                      </div>
                      <span className="break-all font-mono text-[11px] text-[var(--ink-secondary)]">
                        {field.field_kind === "file"
                          ? `PDF SHA-256: ${field.file_sha256 ?? "—"}`
                          : field.is_sensitive
                            ? field.value_preview || "[REDACTED — SENSITIVE WIRE VALUE]"
                            : field.value_preview ?? "—"}
                      </span>
                    </li>
                  ))}
                </ul>
              )}
            </div>

            <div>
              <label
                htmlFor="expected_payload_sha256_input"
                className="block text-xs font-medium text-[var(--ink-secondary)]"
              >
                Required confirmation binding: <code className="font-mono">expected_payload_sha256</code>
              </label>
              <input
                id="expected_payload_sha256_input"
                type="text"
                value={shaOverride || pending.payload_sha256}
                onChange={(e) => setShaOverride(e.target.value)}
                spellCheck={false}
                className="mt-1 w-full rounded-lg border border-[var(--border-hairline)] bg-[var(--bg-surface)] px-3 py-2 font-mono text-xs text-[var(--ink-primary)] focus:border-[var(--accent-primary)] focus:outline-none"
              />
              {!shaReady && (
                <p className="mt-1 text-xs text-[var(--state-failed-fg)]">
                  A valid 64-character hex SHA-256 digest is mandatory for confirmation.
                </p>
              )}
            </div>

            <label className="flex items-start gap-2.5 rounded-lg border border-[var(--border-hairline)] bg-[var(--bg-surface)] p-3 text-xs text-[var(--ink-secondary)]">
              <input
                type="checkbox"
                checked={explicitAck}
                onChange={(e) => setExplicitAck(e.target.checked)}
                className="mt-0.5 h-4 w-4 rounded border-[var(--border-strong)]"
              />
              <span>
                I inspected the employer destination, approved resume checksum, server-stored
                verified answers, and held request digest above, and I authorize ApplyPilot to
                release this single held <code className="font-mono">{pending.http_method}</code>{" "}
                submission.
              </span>
            </label>

            <button
              type="button"
              disabled={!canConfirm}
              onClick={() =>
                onAuthorizeManualSubmit({
                  handoff_session_id: activeHandoff!.handoff_session_id,
                  pending_submission_id: pending.pending_submission_id,
                  expected_form_action: pending.target_url,
                  expected_http_method: pending.http_method,
                  expected_payload_sha256: effectiveSha,
                })
              }
              className="inline-flex items-center justify-center rounded-xl bg-[var(--accent-primary)] px-4 py-2.5 text-sm font-semibold text-white transition-opacity hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-45"
            >
              {busy ? "Authorizing Held Submission..." : "Confirm & Release Held Submission"}
            </button>
          </div>
        ) : (
          <p className="mt-2.5 text-xs leading-relaxed text-[var(--ink-secondary)]">
            {activeHandoff?.manual_submit_armed
              ? "The single-use manual submit authorization has already been issued and consumed. Continue to Step 11 to record or reconcile your submission confirmation."
              : "No pending submission is currently held. Use the supervised Chromium window launched in Step 9, review the prefilled form fields, and click Submit inside that browser to capture pending_submission_id and expected_payload_sha256 here."}
          </p>
        )}
      </div>
    </div>
  );
}
