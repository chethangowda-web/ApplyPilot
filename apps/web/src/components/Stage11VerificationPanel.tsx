"use client";

import React, { useState } from "react";
import Link from "next/link";
import {
  AlertTriangle,
  ArrowUpRight,
  CheckCircle2,
  FileCheck2,
  Info,
  ShieldAlert,
} from "lucide-react";
import type { ApplicationDetailResponse } from "@/lib/types";
import type { StageDerivation } from "@/lib/workflow-engine";
import { StatusBadge } from "@/components/StatusBadge";

export interface Stage11VerificationPanelProps {
  applicationDetail: ApplicationDetailResponse;
  stage: StageDerivation;
  actionBusy: boolean;
  onRecordOutcome: (
    outcome: "submitted_in_browser" | "interrupted" | "failed",
    notes: string,
  ) => Promise<void>;
  onReconcileConfirmation: (receiptId: string, verificationSource: string) => Promise<void>;
}

export function Stage11VerificationPanel({
  applicationDetail,
  stage,
  actionBusy,
  onRecordOutcome,
  onReconcileConfirmation,
}: Stage11VerificationPanelProps) {
  const app = applicationDetail.application;
  const [outcomeNotes, setOutcomeNotes] = useState("");
  const [receiptRef, setReceiptRef] = useState("");
  const [receiptSource, setReceiptSource] = useState("verified_email_confirmation");

  const canRecordOutcome = ["SUBMISSION_PENDING", "MANUAL_ACTION_REQUIRED", "USER_REPORTED_SUBMITTED"].includes(
    app.status,
  );
  const canReportSubmittedInBrowser = app.status === "MANUAL_ACTION_REQUIRED";
  const canReconcile = ["SUBMITTED", "USER_REPORTED_SUBMITTED", "UNVERIFIED"].includes(app.status);
  const lastAuditEvent = applicationDetail.execution_history[0] ?? null;

  return (
    <div className="space-y-4">
      <div className="rounded-xl border border-[var(--border-hairline)] bg-[var(--bg-rail)] p-4 text-xs space-y-2.5">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <span className="text-sm font-semibold text-[var(--ink-primary)]">
            Submission Verification &amp; Application Tracking
          </span>
          <div className="flex flex-wrap items-center gap-2">
            <StatusBadge state={stage.state} customLabel={app.status} mono />
            <Link
              href={`/applications/${app.id}`}
              className="inline-flex items-center gap-1 rounded-lg border border-[var(--border-hairline)] bg-[var(--bg-surface)] px-2.5 py-1 text-[11px] font-medium text-[var(--ink-primary)] hover:border-[var(--border-strong)]"
            >
              <span>Timeline &amp; Detail</span>
              <ArrowUpRight className="h-3 w-3" aria-hidden="true" />
            </Link>
            <Link
              href="/applications"
              className="inline-flex items-center gap-1 rounded-lg border border-[var(--border-hairline)] bg-[var(--bg-surface)] px-2.5 py-1 text-[11px] font-medium text-[var(--ink-secondary)] hover:border-[var(--border-strong)]"
            >
              <span>My Applications</span>
            </Link>
          </div>
        </div>

        <p className="text-[var(--ink-secondary)]">
          <code className="font-mono">USER_REPORTED_SUBMITTED</code> records only your manual report
          and does <strong>not</strong> establish independently verified employer receipt.{" "}
          <code className="font-mono">CONFIRMED</code> requires a trusted server-side verifier.
        </p>

        {app.external_ref_id && (
          <div className="space-y-1 text-[var(--state-completed-fg)]">
            <p className="flex items-center gap-1.5 font-mono">
              <CheckCircle2 className="h-3.5 w-3.5" aria-hidden="true" />
              <span>Verified External Receipt ID: {app.external_ref_id}</span>
            </p>
            {typeof app.confirmation_evidence?.sender_domain === "string" && (
              <p className="text-[11px] text-[var(--ink-secondary)]">
                Verified via <code className="font-mono">{String(app.confirmation_evidence.sender_domain)}</code>
                {typeof app.confirmation_evidence?.received_at === "string"
                  ? ` · Received ${new Date(String(app.confirmation_evidence.received_at)).toLocaleString()}`
                  : ""}
              </p>
            )}
          </div>
        )}
      </div>

      {/* UNVERIFIED / Uncertain Submission Handling */}
      {app.status === "UNVERIFIED" && (
        <div className="rounded-xl border border-[var(--state-waiting-border)] bg-[var(--state-waiting-bg)] p-4 text-xs text-[var(--state-waiting-fg)] space-y-2">
          <div className="flex items-start gap-2">
            <ShieldAlert className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" />
            <div className="space-y-1">
              <p className="font-semibold">Uncertain Submission Outcome (UNVERIFIED)</p>
              <p>
                {app.error_message ||
                  "The submission outcome could not be verified automatically. ApplyPilot preserves uncertainty and blocks automatic retries to prevent duplicate employer submissions."}
              </p>
              {lastAuditEvent && (
                <p className="font-mono text-[11px]">
                  Last known event: {String(lastAuditEvent.action ?? "unknown")} (
                  {lastAuditEvent.created_at
                    ? new Date(String(lastAuditEvent.created_at)).toLocaleString()
                    : "timestamp unavailable"}
                  )
                </p>
              )}
              <p>
                Next action: Check your employer portal or email, then reconcile independent
                confirmation evidence below when available. Do not create a duplicate application.
              </p>
            </div>
          </div>
        </div>
      )}

      {app.status === "FAILED" && app.error_message && (
        <div className="flex items-start gap-2 rounded-xl border border-[var(--state-failed-border)] bg-[var(--state-failed-bg)] p-3.5 text-xs text-[var(--state-failed-fg)]">
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" />
          <span>{app.error_message}</span>
        </div>
      )}

      {canRecordOutcome && (
        <div className="rounded-xl border border-[var(--border-hairline)] bg-[var(--bg-surface)] p-4 space-y-3 text-xs">
          <div>
            <p className="font-semibold text-[var(--ink-primary)]">
              Record Browser Submission Outcome
            </p>
            <p className="mt-0.5 text-[var(--ink-muted)]">
              Reporting <code className="font-mono">submitted_in_browser</code> sets status to{" "}
              <code className="font-mono">USER_REPORTED_SUBMITTED</code> (never{" "}
              <code className="font-mono">CONFIRMED</code>).
            </p>
          </div>
          <input
            type="text"
            value={outcomeNotes}
            onChange={(e) => setOutcomeNotes(e.target.value)}
            placeholder="Optional notes (e.g. saw portal thank-you screen or connection dropped)..."
            className="w-full rounded-lg border border-[var(--border-hairline)] bg-[var(--bg-rail)] px-3 py-2 text-xs text-[var(--ink-primary)]"
          />
          <div className="flex flex-wrap gap-2">
            {canReportSubmittedInBrowser && (
              <button
                type="button"
                disabled={actionBusy}
                onClick={() => onRecordOutcome("submitted_in_browser", outcomeNotes)}
                className="rounded-lg bg-[var(--accent-primary)] px-3 py-1.5 font-semibold text-white hover:opacity-90 disabled:opacity-50"
              >
                Report Submitted in Browser
              </button>
            )}
            <button
              type="button"
              disabled={actionBusy}
              onClick={() => onRecordOutcome("interrupted", outcomeNotes)}
              className="rounded-lg border border-[var(--border-strong)] px-3 py-1.5 font-medium text-[var(--ink-primary)] hover:bg-[var(--bg-rail)] disabled:opacity-50"
            >
              Mark Interrupted (UNVERIFIED)
            </button>
            <button
              type="button"
              disabled={actionBusy}
              onClick={() => onRecordOutcome("failed", outcomeNotes)}
              className="rounded-lg border border-[var(--state-failed-border)] px-3 py-1.5 font-medium text-[var(--state-failed-fg)] hover:bg-[var(--state-failed-bg)] disabled:opacity-50"
            >
              Mark Failed
            </button>
          </div>
        </div>
      )}

      {canReconcile && (
        <div className="rounded-xl border border-[var(--border-hairline)] bg-[var(--bg-surface)] p-4 space-y-3 text-xs">
          <div className="space-y-1">
            <p className="font-semibold text-[var(--ink-primary)]">
              Reconcile Independent Confirmation Evidence
            </p>
            <p className="flex items-start gap-1.5 text-[var(--ink-muted)]">
              <Info className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden="true" />
              <span>
                Production confirmation verifiers (<code className="font-mono">_CONFIRMATION_VERIFIER_REGISTRY</code>)
                are not yet connected (Milestone 6 Gmail/ATS integration). The backend rejects
                unverified self-assertions and keeps this application as{" "}
                <code className="font-mono">{app.status}</code>.
              </span>
            </p>
          </div>
          <div className="grid gap-2 sm:grid-cols-2">
            <select
              aria-label="Confirmation verification source"
              value={receiptSource}
              onChange={(e) => setReceiptSource(e.target.value)}
              className="rounded-lg border border-[var(--border-hairline)] bg-[var(--bg-rail)] px-2.5 py-2 text-xs text-[var(--ink-primary)]"
            >
              <option value="verified_email_confirmation">verified_email_confirmation</option>
              <option value="official_api_receipt">official_api_receipt</option>
              <option value="ats_webhook_receipt">ats_webhook_receipt</option>
            </select>
            <input
              type="text"
              value={receiptRef}
              onChange={(e) => setReceiptRef(e.target.value)}
              placeholder="Receipt ID / confirmation message ID..."
              className="rounded-lg border border-[var(--border-hairline)] bg-[var(--bg-rail)] px-3 py-2 font-mono text-xs text-[var(--ink-primary)]"
            />
          </div>
          <button
            type="button"
            disabled={actionBusy || !receiptRef.trim()}
            onClick={() => onReconcileConfirmation(receiptRef.trim(), receiptSource)}
            className="inline-flex items-center gap-1.5 rounded-lg bg-[var(--state-completed-fg)] px-3 py-2 font-semibold text-white hover:opacity-90 disabled:opacity-50"
          >
            <FileCheck2 className="h-3.5 w-3.5" aria-hidden="true" />
            <span>Submit for Trusted Verification</span>
          </button>
        </div>
      )}
    </div>
  );
}
