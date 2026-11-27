"use client";

import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { ArrowRight, Compass, RefreshCw } from "lucide-react";
import { ApiError, apiClient } from "@/lib/api-client";
import {
  WORKFLOW_STAGES,
  buildWorkflowOverviewItems,
  type WorkflowOverviewItem,
} from "@/lib/workflow-engine";
import { StatusBadge } from "@/components/StatusBadge";

type OverviewFilter = "all" | "attention" | "active" | "completed";

export function MyWorkflowsView() {
  const router = useRouter();
  const routerRef = useRef(router);
  routerRef.current = router;

  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState<OverviewFilter>("all");
  const [items, setItems] = useState<WorkflowOverviewItem[]>([]);

  const loadOverview = useCallback(async () => {
    setError(null);
    setLoading(true);
    try {
      const [jobs, tailoredResumes, applications] = await Promise.all([
        apiClient.listJobs({ limit: 100 }),
        apiClient.listTailoredResumes().catch(() => []),
        apiClient.listApplications(),
      ]);

      // Fetch rich details only for owned active applications so Step 7-11 status is exact
      const detailEntries = await Promise.all(
        applications.slice(0, 25).map(async (app) => {
          try {
            const detail = await apiClient.getApplicationDetail(app.id);
            return [app.id, detail] as const;
          } catch {
            return null;
          }
        }),
      );
      const detailMap = Object.fromEntries(
        detailEntries.filter((x): x is NonNullable<typeof x> => x !== null),
      );

      setItems(
        buildWorkflowOverviewItems({
          jobs,
          tailoredResumes,
          applications,
          applicationDetailsById: detailMap,
        }),
      );
    } catch (err) {
      if (err instanceof ApiError && err.isUnauthorized()) {
        routerRef.current.replace("/login?next=/");
        return;
      }
      setError(err instanceof Error ? err.message : "Failed to load workflows");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadOverview();
  }, [loadOverview]);

  const counts = useMemo(() => {
    const attention = items.filter((i) =>
      ["waiting", "blocked", "failed"].includes(i.derived.overallState),
    ).length;
    const completed = items.filter((i) => i.derived.overallState === "completed").length;
    const active = items.filter((i) => i.derived.overallState === "current").length;
    return { all: items.length, attention, active, completed };
  }, [items]);

  const filtered = useMemo(() => {
    if (filter === "attention") {
      return items.filter((i) =>
        ["waiting", "blocked", "failed"].includes(i.derived.overallState),
      );
    }
    if (filter === "active") {
      return items.filter((i) => i.derived.overallState === "current");
    }
    if (filter === "completed") {
      return items.filter((i) => i.derived.overallState === "completed");
    }
    return items;
  }, [filter, items]);

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight text-[var(--ink-primary)]">
            My Workflows
          </h1>
          <p className="mt-1 text-sm text-[var(--ink-secondary)]">
            Resume each job application one verified step at a time using persisted backend state.
          </p>
        </div>

        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={() => void loadOverview()}
            disabled={loading}
            className="inline-flex items-center gap-1.5 rounded-xl border border-[var(--border-hairline)] bg-[var(--bg-surface)] px-3 py-2 text-xs font-medium text-[var(--ink-secondary)] hover:border-[var(--border-strong)]"
          >
            <RefreshCw className="h-3.5 w-3.5" aria-hidden="true" />
            <span>Refresh</span>
          </button>
          <Link
            href="/applications"
            className="inline-flex items-center gap-1.5 rounded-xl border border-[var(--border-hairline)] bg-[var(--bg-surface)] px-3 py-2 text-xs font-medium text-[var(--ink-primary)] hover:border-[var(--border-strong)]"
          >
            <span>Applications Tracker</span>
          </Link>
          <Link
            href="/discover"
            className="inline-flex items-center gap-1.5 rounded-xl bg-[var(--ink-primary)] px-3.5 py-2 text-xs font-semibold text-white hover:opacity-90"
          >
            <Compass className="h-3.5 w-3.5" aria-hidden="true" />
            <span>Start Workflow from Jobs</span>
          </Link>
        </div>
      </div>

      {/* Lightweight Secondary Summary & Filters */}
      <div className="flex flex-wrap items-center justify-between gap-3 rounded-2xl border border-[var(--border-hairline)] bg-[var(--bg-surface)] px-4 py-3">
        <div className="flex flex-wrap items-center gap-1.5" role="tablist" aria-label="Filter workflows">
          {(
            [
              ["all", `All (${counts.all})`],
              ["attention", `Needs Attention (${counts.attention})`],
              ["active", `In Progress (${counts.active})`],
              ["completed", `Completed (${counts.completed})`],
            ] as const
          ).map(([key, label]) => (
            <button
              key={key}
              type="button"
              role="tab"
              aria-selected={filter === key}
              onClick={() => setFilter(key)}
              className={`rounded-lg px-3 py-1.5 text-xs font-medium transition-colors ${
                filter === key
                  ? "bg-[var(--bg-subtle)] text-[var(--ink-primary)] shadow-[inset_0_0_0_1px_var(--border-strong)]"
                  : "text-[var(--ink-secondary)] hover:bg-[var(--bg-rail)]"
              }`}
            >
              {label}
            </button>
          ))}
        </div>

        <p className="text-xs text-[var(--ink-muted)] tabular-nums">
          {counts.attention} waiting/blocked · {counts.completed} confirmed
        </p>
      </div>

      {error && (
        <div
          role="alert"
          className="rounded-xl border border-[var(--state-failed-border)] bg-[var(--state-failed-bg)] p-4 text-xs text-[var(--state-failed-fg)]"
        >
          {error}
        </div>
      )}

      {loading ? (
        <div className="grid gap-4 sm:grid-cols-2">
          {[1, 2].map((n) => (
            <div
              key={n}
              className="h-44 animate-pulse rounded-2xl border border-[var(--border-hairline)] bg-[var(--bg-surface)] p-5"
            />
          ))}
        </div>
      ) : filtered.length === 0 ? (
        <div className="rounded-2xl border border-dashed border-[var(--border-strong)] bg-[var(--bg-surface)] p-8 text-center">
          <p className="text-sm font-semibold text-[var(--ink-primary)]">
            {items.length === 0
              ? "No active job workflows yet"
              : "No workflows match the selected filter"}
          </p>
          <p className="mt-1 text-xs text-[var(--ink-secondary)]">
            {items.length === 0
              ? "Upload your Master Profile & Resume, then select a discovered job to begin Stage 1."
              : "Switch tabs above or browse Discover Jobs to start a new application workflow."}
          </p>
          <div className="mt-4 flex justify-center gap-2">
            <Link
              href="/discover"
              className="inline-flex items-center gap-1.5 rounded-xl bg-[var(--accent-primary)] px-4 py-2 text-xs font-semibold text-white"
            >
              <span>Browse Discover Jobs</span>
              <ArrowRight className="h-3.5 w-3.5" aria-hidden="true" />
            </Link>
            <Link
              href="/profile"
              className="inline-flex items-center rounded-xl border border-[var(--border-strong)] bg-[var(--bg-surface)] px-4 py-2 text-xs font-semibold text-[var(--ink-primary)]"
            >
              <span>Profile & Resumes</span>
            </Link>
          </div>
        </div>
      ) : (
        <div className="grid gap-4 md:grid-cols-2">
          {filtered.map(({ job, derived }) => {
            const stageMeta = WORKFLOW_STAGES[derived.authoritativeStep - 1];
            return (
              <article
                key={job.id}
                className="flex flex-col justify-between rounded-2xl border border-[var(--border-hairline)] bg-[var(--bg-surface)] p-5 transition-colors hover:border-[var(--border-strong)]"
              >
                <div className="space-y-3">
                  <div className="flex items-start justify-between gap-3">
                    <div className="min-w-0">
                      <p className="text-xs font-semibold uppercase tracking-wider text-[var(--ink-muted)]">
                        {job.company}
                      </p>
                      <h2 className="mt-0.5 truncate text-base font-semibold text-[var(--ink-primary)]">
                        {job.role_title}
                      </h2>
                    </div>
                    <StatusBadge
                      state={derived.overallState}
                      customLabel={derived.backendStatusLabel}
                      mono
                    />
                  </div>

                  <div className="space-y-1.5">
                    <div className="flex items-center justify-between text-xs">
                      <span className="font-medium text-[var(--ink-secondary)]">
                        Step {derived.authoritativeStep} of {derived.stages.length} ·{" "}
                        {stageMeta.shortTitle}
                      </span>
                      <span className="font-mono font-semibold text-[var(--ink-primary)] tabular-nums">
                        {derived.progressPercent}%
                      </span>
                    </div>
                    <div className="h-2 w-full overflow-hidden rounded-full bg-[var(--bg-subtle)]">
                      <div
                        className={`h-full ${
                          derived.overallState === "failed"
                            ? "bg-[var(--state-failed-fg)]"
                            : derived.overallState === "blocked"
                              ? "bg-[var(--state-blocked-fg)]"
                              : derived.overallState === "waiting"
                                ? "bg-[var(--state-waiting-fg)]"
                                : "bg-[var(--state-completed-fg)]"
                        }`}
                        style={{ width: `${derived.progressPercent}%` }}
                      />
                    </div>
                  </div>

                  <p className="rounded-xl border border-[var(--border-hairline)] bg-[var(--bg-rail)] px-3 py-2 text-xs leading-relaxed text-[var(--ink-secondary)]">
                    <span className="font-semibold text-[var(--ink-primary)]">Next action: </span>
                    {derived.nextActionRequired}
                  </p>
                </div>

                <div className="mt-4 flex items-center justify-between gap-3 border-t border-[var(--border-hairline)] pt-3">
                  <span className="text-[11px] text-[var(--ink-muted)] tabular-nums">
                    Updated {new Date(derived.lastActivityAt).toLocaleString()}
                  </span>
                  <Link
                    href={`/workflows/${job.id}`}
                    className="inline-flex items-center gap-1.5 rounded-xl bg-[var(--ink-primary)] px-3.5 py-2 text-xs font-semibold text-white hover:opacity-90"
                  >
                    <span>
                      {derived.overallState === "blocked" || derived.overallState === "failed"
                        ? "Inspect Blocker"
                        : "Resume Workflow"}
                    </span>
                    <ArrowRight className="h-3.5 w-3.5" aria-hidden="true" />
                  </Link>
                </div>
              </article>
            );
          })}
        </div>
      )}
    </div>
  );
}
