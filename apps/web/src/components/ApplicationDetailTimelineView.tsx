"use client";

import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { ArrowLeft, CheckCircle2, Clock, RefreshCw } from "lucide-react";
import { ApiError, apiClient } from "@/lib/api-client";
import type {
  ApplicationDetailResponse,
  JobDetailResponse,
  JobResumeHistoryResponse,
  TailoredResumeDetailResponse,
} from "@/lib/types";
import { deriveJobWorkflowState } from "@/lib/workflow-engine";
import { StatusBadge } from "@/components/StatusBadge";
import { Stage11VerificationPanel } from "@/components/Stage11VerificationPanel";

interface TimelineEntry {
  id: string;
  label: string;
  timestamp: string | null;
  detail: string;
  occurred: boolean;
}

export function ApplicationDetailTimelineView({ applicationId }: { applicationId: string }) {
  const router = useRouter();
  const routerRef = useRef(router);
  routerRef.current = router;

  const [loading, setLoading] = useState(true);
  const [actionBusy, setActionBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const [detail, setDetail] = useState<ApplicationDetailResponse | null>(null);
  const [job, setJob] = useState<JobDetailResponse | null>(null);
  const [resumeHistory, setResumeHistory] = useState<JobResumeHistoryResponse | null>(null);
  const [tailoredDetail, setTailoredDetail] = useState<TailoredResumeDetailResponse | null>(null);

  const loadDetail = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const appDetail = await apiClient.getApplicationDetail(applicationId);
      setDetail(appDetail);
      const [jobRes, histRes, tailRes] = await Promise.all([
        apiClient.getJobDetail(appDetail.application.job_id),
        apiClient.getJobResumeHistory(appDetail.application.job_id).catch(() => null),
        apiClient.getTailoredResumeDetail(appDetail.application.resume_id).catch(() => null),
      ]);
      setJob(jobRes);
      setResumeHistory(histRes);
      setTailoredDetail(tailRes);
    } catch (err) {
      if (err instanceof ApiError && err.isUnauthorized()) {
        routerRef.current.replace(`/login?next=/applications/${applicationId}`);
        return;
      }
      setError(err instanceof Error ? err.message : "Failed to load application detail.");
    } finally {
      setLoading(false);
    }
  }, [applicationId]);

  useEffect(() => {
    void loadDetail();
  }, [loadDetail]);

  const derived = useMemo(() => {
    if (!job || !detail) return null;
    return deriveJobWorkflowState({
      job,
      resumeHistory,
      applicationDetail: detail,
    });
  }, [detail, job, resumeHistory]);

  const timeline = useMemo<TimelineEntry[]>(() => {
    if (!detail || !job) return [];
    const app = detail.application;
    const auditByAction = new Map<string, { created_at?: string; metadata?: Record<string, unknown> }>();
    for (const ev of detail.execution_history) {
      const act = String(ev.action ?? "");
      if (act && !auditByAction.has(act)) {
        auditByAction.set(act, {
          created_at: ev.created_at ? String(ev.created_at) : undefined,
          metadata: (ev.metadata as Record<string, unknown>) ?? {},
        });
      }
    }

    const versions = resumeHistory?.versions ?? [];
    const activeMapping =
      versions.find((r) => r.resume_id === app.resume_id) ?? versions[0] ?? null;
    const latestApproval = detail.approval_records[0] ?? null;
    const latestHandoff = detail.active_handoff_sessions[0] ?? null;
    const verifiedAnswersCount = detail.answers.filter((a) => a.user_verified).length;

    const handoffLaunchedTs =
      latestHandoff?.created_at ??
      auditByAction.get("application.execution_handoff_ready")?.created_at ??
      null;
    const pendingCapturedTs = latestHandoff?.pending_submission?.detected_at ?? null;
    const manualAuthTs = auditByAction.get("application.manual_submit_authorized")?.created_at ?? null;
    const outcomeTs =
      app.submitted_at ??
      auditByAction.get("application.submission_outcome_recorded")?.created_at ??
      null;

    return [
      {
        id: "job_selected",
        label: "Job selected",
        timestamp: job.first_seen_at || app.created_at,
        detail: `${job.company} — ${job.role_title}`,
        occurred: true,
      },
      {
        id: "jd_analyzed",
        label: "JD analyzed",
        timestamp: job.requirements?.extracted_at ?? null,
        detail: job.requirements
          ? `${job.requirements.required_skills.length} required skills extracted`
          : "Pending JD analysis",
        occurred: Boolean(job.requirements),
      },
      {
        id: "resume_tailored",
        label: "Resume tailored",
        timestamp: activeMapping?.created_at ?? tailoredDetail?.created_at ?? null,
        detail: activeMapping
          ? `Tailored PDF v${activeMapping.version_number}`
          : "Not tailored yet",
        occurred: Boolean(activeMapping || tailoredDetail),
      },
      {
        id: "resume_approved",
        label: "Resume approved",
        timestamp: activeMapping?.approved_at ?? tailoredDetail?.approved_at ?? null,
        detail:
          activeMapping?.approval_status === "approved"
            ? "Explicit owner resume approval recorded"
            : "Pending resume approval",
        occurred: activeMapping?.approval_status === "approved",
      },
      {
        id: "app_prepared",
        label: "Application prepared",
        timestamp: auditByAction.get("application.prepared")?.created_at ?? app.created_at,
        detail: `Mode: ${app.application_mode}`,
        occurred: true,
      },
      {
        id: "answers_verified",
        label: "Answers verified",
        timestamp: auditByAction.get("application.answers_updated")?.created_at ?? null,
        detail: `${verifiedAnswersCount} / ${detail.answers.length} stored answer(s) verified`,
        occurred:
          detail.answers.length === 0
            ? app.status !== "NEEDS_USER_INPUT"
            : verifiedAnswersCount === detail.answers.length,
      },
      {
        id: "app_approved",
        label: "Application approved",
        timestamp: latestApproval?.decided_at ?? null,
        detail: latestApproval
          ? `Snapshot ${latestApproval.approval_snapshot_hash.slice(0, 12)}…`
          : "Awaiting explicit approval",
        occurred: latestApproval?.decision === "approved",
      },
      {
        id: "handoff_launched",
        label: "Browser handoff launched",
        timestamp: handoffLaunchedTs,
        detail: handoffLaunchedTs ? "Supervised browser prefill opened" : "Not launched yet",
        occurred: Boolean(handoffLaunchedTs),
      },
      {
        id: "pending_captured",
        label: "Pending submission captured",
        timestamp: pendingCapturedTs,
        detail: latestHandoff?.pending_submission
          ? `pending_submission_id: ${latestHandoff.pending_submission.pending_submission_id}`
          : "No active held submission in memory",
        occurred: Boolean(pendingCapturedTs),
      },
      {
        id: "request_authorized",
        label: "Request authorized and released",
        timestamp: manualAuthTs,
        detail: manualAuthTs ? "Single-use manual submit authorization issued" : "Not authorized yet",
        occurred: Boolean(manualAuthTs),
      },
      {
        id: "outcome_recorded",
        label: "Outcome recorded",
        timestamp: outcomeTs,
        detail: outcomeTs ? `Status: ${app.status}` : "No outcome recorded yet",
        occurred: Boolean(outcomeTs),
      },
      {
        id: "trusted_confirmed",
        label: "Trusted confirmation received",
        timestamp: app.confirmed_at,
        detail:
          app.status === "CONFIRMED"
            ? `Receipt ID: ${app.external_ref_id ?? "verified"}`
            : "Missing independent verifier receipt (Milestone 6)",
        occurred: app.status === "CONFIRMED",
      },
    ];
  }, [detail, job, resumeHistory, tailoredDetail]);

  async function handleOutcome(
    outcome: "submitted_in_browser" | "interrupted" | "failed",
    notes: string,
  ) {
    if (!detail) return;
    setActionBusy(true);
    setError(null);
    setNotice(null);
    try {
      await apiClient.recordSubmitOutcome(detail.application.id, {
        outcome,
        notes: notes || null,
      });
      await loadDetail();
      setNotice("Submission outcome recorded.");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to record outcome.");
    } finally {
      setActionBusy(false);
    }
  }

  async function handleReconcile(receiptId: string, verificationSource: string) {
    if (!detail) return;
    setActionBusy(true);
    setError(null);
    setNotice(null);
    try {
      await apiClient.reconcileApplication(detail.application.id, {
        confirmation_evidence: {
          external_ref_id: receiptId,
          verification_source: verificationSource,
        },
      });
      await loadDetail();
      setNotice("Independent confirmation verified.");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Reconciliation rejected by backend verifier.");
    } finally {
      setActionBusy(false);
    }
  }

  if (loading) {
    return (
      <div className="rounded-2xl border border-[var(--border-hairline)] bg-[var(--bg-surface)] p-8 text-sm text-[var(--ink-secondary)]">
        Loading application timeline from backend...
      </div>
    );
  }

  if (!detail || !job || !derived) {
    return (
      <div className="rounded-2xl border border-[var(--state-failed-border)] bg-[var(--state-failed-bg)] p-6 text-sm text-[var(--state-failed-fg)]">
        {error || "Application not found."}
      </div>
    );
  }

  const app = detail.application;
  const step11Stage = derived.stages[10];

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <Link
            href="/applications"
            className="inline-flex items-center gap-1 text-xs font-medium text-[var(--ink-muted)] hover:text-[var(--ink-primary)]"
          >
            <ArrowLeft className="h-3.5 w-3.5" aria-hidden="true" />
            <span>Back to My Applications</span>
          </Link>
          <h1 className="mt-1 text-xl font-semibold tracking-tight text-[var(--ink-primary)]">
            {job.company} — {job.role_title}
          </h1>
          <p className="mt-0.5 break-all font-mono text-xs text-[var(--ink-secondary)]">
            {app.destination_url}
          </p>
        </div>

        <div className="flex flex-wrap items-center gap-2">
          <StatusBadge state={step11Stage.state} customLabel={app.status} mono />
          <button
            type="button"
            onClick={() => void loadDetail()}
            className="inline-flex items-center gap-1.5 rounded-xl border border-[var(--border-hairline)] bg-[var(--bg-surface)] px-3 py-2 text-xs font-medium text-[var(--ink-secondary)] hover:border-[var(--border-strong)]"
          >
            <RefreshCw className="h-3.5 w-3.5" aria-hidden="true" />
            <span>Refresh</span>
          </button>
          <Link
            href={`/workflows/${app.job_id}`}
            className="rounded-xl bg-[var(--ink-primary)] px-3 py-2 text-xs font-semibold text-white hover:opacity-90"
          >
            Open Workflow
          </Link>
        </div>
      </div>

      {error && (
        <div className="rounded-xl border border-[var(--state-failed-border)] bg-[var(--state-failed-bg)] p-3 text-xs text-[var(--state-failed-fg)]">
          {error}
        </div>
      )}
      {notice && (
        <div className="rounded-xl border border-[var(--state-completed-border)] bg-[var(--state-completed-bg)] p-3 text-xs text-[var(--state-completed-fg)]">
          {notice}
        </div>
      )}

      {/* Backend-Derived Chronological Timeline */}
      <section className="rounded-2xl border border-[var(--border-hairline)] bg-[var(--bg-surface)] p-5 space-y-4">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h2 className="text-sm font-semibold text-[var(--ink-primary)]">
            Backend-Derived Application Timeline
          </h2>
          <span className="text-xs text-[var(--ink-muted)]">
            Only persisted backend timestamps are shown (never fabricated)
          </span>
        </div>

        <ol className="divide-y divide-[var(--border-hairline)]">
          {timeline.map((item) => (
            <li key={item.id} className="flex flex-wrap items-center justify-between gap-2 py-2.5 text-xs">
              <div className="flex items-start gap-2.5">
                {item.occurred ? (
                  <CheckCircle2
                    className="mt-0.5 h-4 w-4 shrink-0 text-[var(--state-completed-fg)]"
                    aria-hidden="true"
                  />
                ) : (
                  <Clock
                    className="mt-0.5 h-4 w-4 shrink-0 text-[var(--ink-muted)]"
                    aria-hidden="true"
                  />
                )}
                <div>
                  <p className="font-medium text-[var(--ink-primary)]">{item.label}</p>
                  <p className="text-[var(--ink-muted)]">{item.detail}</p>
                </div>
              </div>
              <span className="font-mono text-[11px] text-[var(--ink-secondary)]">
                {item.timestamp ? new Date(item.timestamp).toLocaleString() : "—"}
              </span>
            </li>
          ))}
        </ol>
      </section>

      <Stage11VerificationPanel
        applicationDetail={detail}
        stage={step11Stage}
        actionBusy={actionBusy}
        onRecordOutcome={handleOutcome}
        onReconcileConfirmation={handleReconcile}
      />
    </div>
  );
}
