"use client";

import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { ArrowRight, RefreshCw } from "lucide-react";
import { ApiError, apiClient } from "@/lib/api-client";
import type {
  ApplicationResponse,
  JobSummaryResponse,
  TailoredResumeSummaryResponse,
} from "@/lib/types";
import { deriveJobWorkflowState } from "@/lib/workflow-engine";
import { StatusBadge } from "@/components/StatusBadge";

export type TrackerFilter =
  | "all"
  | "APPROVED"
  | "USER_REPORTED_SUBMITTED"
  | "UNVERIFIED"
  | "CONFIRMED"
  | "FAILED";

interface TrackerRow {
  application: ApplicationResponse;
  job: JobSummaryResponse | null;
  resumeLabel: string;
  workflowStateLabel: string;
  nextRequiredAction: string;
  lastActivityAt: string;
}

function deriveNextAction(status: string): string {
  switch (status) {
    case "NEEDS_USER_INPUT":
      return "Complete required / sensitive questions in Step 7.";
    case "AWAITING_APPROVAL":
      return "Review and approve snapshot in Step 8.";
    case "APPROVED":
      return "Launch supervised browser handoff in Step 9.";
    case "MANUAL_ACTION_REQUIRED":
      return "Complete browser handoff / Hold-and-Confirm or record outcome in Step 11.";
    case "USER_REPORTED_SUBMITTED":
      return "Awaiting trusted confirmation verifier (Milestone 6) or reconciliation.";
    case "UNVERIFIED":
      return "Submission uncertain — inspect portal & reconcile evidence without retrying.";
    case "CONFIRMED":
      return "Trusted confirmation recorded.";
    case "FAILED":
      return "Inspect error notice and re-approve if safe.";
    default:
      return "Open workflow to inspect current state.";
  }
}

export function ApplicationsTrackerView() {
  const router = useRouter();
  const routerRef = useRef(router);
  routerRef.current = router;

  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState<TrackerFilter>("all");
  const [rows, setRows] = useState<TrackerRow[]>([]);

  const loadTracker = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [apps, jobs, resumes] = await Promise.all([
        apiClient.listApplications(),
        apiClient.listJobs({ limit: 100 }),
        apiClient.listTailoredResumes().catch(() => [] as TailoredResumeSummaryResponse[]),
      ]);
      const jobsById = new Map(jobs.map((j) => [j.id, j]));
      const resumesById = new Map(resumes.map((r) => [r.resume_id, r]));

      const built: TrackerRow[] = apps.map((app) => {
        const job = jobsById.get(app.job_id) ?? null;
        const resume = resumesById.get(app.resume_id) ?? null;
        const derived = job
          ? deriveJobWorkflowState({
              job,
              applicationDetail: {
                application: app,
                answers: [],
                approval_records: [],
                active_handoff_sessions: [],
                execution_history: [],
              },
            })
          : null;
        const lastActivityAt =
          app.confirmed_at || app.submitted_at || app.updated_at || app.created_at;
        return {
          application: app,
          job,
          resumeLabel: resume
            ? `Tailored v${resume.version_number} (${resume.version_label})`
            : `Resume ${app.resume_id.slice(0, 8)}…`,
          workflowStateLabel: derived
            ? `Step ${derived.authoritativeStep}: ${derived.stages[derived.authoritativeStep - 1]?.definition.shortTitle ?? app.status}`
            : app.status,
          nextRequiredAction: deriveNextAction(app.status),
          lastActivityAt,
        };
      });
      setRows(built);
    } catch (err) {
      if (err instanceof ApiError && err.isUnauthorized()) {
        routerRef.current.replace("/login?next=/applications");
        return;
      }
      setError(err instanceof Error ? err.message : "Failed to load applications.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadTracker();
  }, [loadTracker]);

  const counts = useMemo(
    () => ({
      all: rows.length,
      APPROVED: rows.filter((r) => r.application.status === "APPROVED").length,
      USER_REPORTED_SUBMITTED: rows.filter(
        (r) => r.application.status === "USER_REPORTED_SUBMITTED",
      ).length,
      UNVERIFIED: rows.filter((r) => r.application.status === "UNVERIFIED").length,
      CONFIRMED: rows.filter((r) => r.application.status === "CONFIRMED").length,
      FAILED: rows.filter((r) => r.application.status === "FAILED").length,
    }),
    [rows],
  );

  const filtered = useMemo(() => {
    if (filter === "all") return rows;
    return rows.filter((r) => r.application.status === filter);
  }, [filter, rows]);

  const filterButtons: { key: TrackerFilter; label: string }[] = [
    { key: "all", label: `All (${counts.all})` },
    { key: "APPROVED", label: `Approved (${counts.APPROVED})` },
    {
      key: "USER_REPORTED_SUBMITTED",
      label: `Submitted (User Reported) (${counts.USER_REPORTED_SUBMITTED})`,
    },
    { key: "UNVERIFIED", label: `Unverified (${counts.UNVERIFIED})` },
    { key: "CONFIRMED", label: `Confirmed (${counts.CONFIRMED})` },
    { key: "FAILED", label: `Failed (${counts.FAILED})` },
  ];

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="text-xl font-semibold tracking-tight text-[var(--ink-primary)]">
            My Applications Tracker
          </h1>
          <p className="mt-1 text-sm text-[var(--ink-secondary)]">
            Authoritative backend application states. <code className="font-mono">USER_REPORTED_SUBMITTED</code>,{" "}
            <code className="font-mono">UNVERIFIED</code>, and <code className="font-mono">CONFIRMED</code>{" "}
            are tracked strictly separately.
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <button
            type="button"
            onClick={() => void loadTracker()}
            className="inline-flex items-center gap-1.5 rounded-xl border border-[var(--border-hairline)] bg-[var(--bg-surface)] px-3 py-2 text-xs font-medium text-[var(--ink-secondary)] hover:border-[var(--border-strong)]"
          >
            <RefreshCw className="h-3.5 w-3.5" aria-hidden="true" />
            <span>Refresh</span>
          </button>
          <Link
            href="/"
            className="inline-flex items-center gap-1.5 rounded-xl border border-[var(--border-hairline)] bg-[var(--bg-surface)] px-3 py-2 text-xs font-medium text-[var(--ink-primary)] hover:border-[var(--border-strong)]"
          >
            <span>My Workflows</span>
          </Link>
        </div>
      </div>

      <div className="flex flex-wrap gap-2" role="tablist" aria-label="Application status filters">
        {filterButtons.map((btn) => (
          <button
            key={btn.key}
            type="button"
            role="tab"
            aria-selected={filter === btn.key}
            onClick={() => setFilter(btn.key)}
            className={`rounded-full px-3 py-1.5 text-xs font-medium transition-colors ${
              filter === btn.key
                ? "bg-[var(--ink-primary)] text-white"
                : "border border-[var(--border-hairline)] bg-[var(--bg-surface)] text-[var(--ink-secondary)] hover:border-[var(--border-strong)]"
            }`}
          >
            {btn.label}
          </button>
        ))}
      </div>

      {error && (
        <div className="rounded-xl border border-[var(--state-failed-border)] bg-[var(--state-failed-bg)] p-4 text-sm text-[var(--state-failed-fg)]">
          {error}
        </div>
      )}

      {loading ? (
        <div className="rounded-2xl border border-[var(--border-hairline)] bg-[var(--bg-surface)] p-8 text-sm text-[var(--ink-secondary)]">
          Loading applications from backend...
        </div>
      ) : filtered.length === 0 ? (
        <div className="rounded-2xl border border-[var(--border-hairline)] bg-[var(--bg-surface)] p-8 text-center text-sm text-[var(--ink-secondary)]">
          No applications match the selected filter.
        </div>
      ) : (
        <div className="space-y-3">
          {filtered.map((row) => {
            const { application: app, job } = row;
            const badgeState =
              app.status === "CONFIRMED"
                ? "completed"
                : app.status === "FAILED"
                  ? "failed"
                  : app.status === "UNVERIFIED"
                    ? "waiting"
                    : "current";
            return (
              <div
                key={app.id}
                className="rounded-2xl border border-[var(--border-hairline)] bg-[var(--bg-surface)] p-4 shadow-xs space-y-3"
              >
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div>
                    <h2 className="text-base font-semibold text-[var(--ink-primary)]">
                      {job ? `${job.company} — ${job.role_title}` : `Application ${app.id.slice(0, 8)}`}
                    </h2>
                    <p className="mt-0.5 text-xs text-[var(--ink-muted)]">
                      {job?.location || "Location unspecified"} · Created{" "}
                      {new Date(app.created_at).toLocaleDateString()} · Last activity{" "}
                      {new Date(row.lastActivityAt).toLocaleString()}
                    </p>
                  </div>
                  <StatusBadge state={badgeState} customLabel={app.status} mono />
                </div>

                <dl className="grid gap-3 rounded-xl bg-[var(--bg-rail)] p-3 text-xs sm:grid-cols-3">
                  <div>
                    <dt className="text-[var(--ink-muted)]">Workflow State</dt>
                    <dd className="mt-0.5 font-medium text-[var(--ink-primary)]">
                      {row.workflowStateLabel}
                    </dd>
                  </div>
                  <div>
                    <dt className="text-[var(--ink-muted)]">Selected Resume</dt>
                    <dd className="mt-0.5 font-mono text-[var(--ink-primary)]">
                      {row.resumeLabel}
                    </dd>
                  </div>
                  <div>
                    <dt className="text-[var(--ink-muted)]">Next Required Action</dt>
                    <dd className="mt-0.5 text-[var(--ink-primary)]">{row.nextRequiredAction}</dd>
                  </div>
                </dl>

                <div className="flex flex-wrap items-center justify-end gap-2">
                  <Link
                    href={`/workflows/${app.job_id}`}
                    className="rounded-lg border border-[var(--border-hairline)] bg-[var(--bg-rail)] px-3 py-1.5 text-xs font-medium text-[var(--ink-secondary)] hover:border-[var(--border-strong)]"
                  >
                    Open Workflow
                  </Link>
                  <Link
                    href={`/applications/${app.id}`}
                    className="inline-flex items-center gap-1 rounded-lg bg-[var(--ink-primary)] px-3 py-1.5 text-xs font-semibold text-white hover:opacity-90"
                  >
                    <span>Detail &amp; Timeline</span>
                    <ArrowRight className="h-3.5 w-3.5" aria-hidden="true" />
                  </Link>
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
