"use client";

import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import {
  ArrowRight,
  ChevronLeft,
  ChevronRight,
  Compass,
  Eye,
  RefreshCw,
  Search,
} from "lucide-react";
import { ApiError, apiClient } from "@/lib/api-client";
import type {
  ApplicationResponse,
  JobDetailResponse,
  JobSourceHealthResponse,
  JobSummaryResponse,
} from "@/lib/types";
import { StatusBadge } from "@/components/StatusBadge";

const PAGE_SIZE = 10;

export function DiscoverJobsView() {
  const router = useRouter();
  const routerRef = useRef(router);
  routerRef.current = router;

  const [jobs, setJobs] = useState<JobSummaryResponse[]>([]);
  const [sources, setSources] = useState<JobSourceHealthResponse[]>([]);
  const [applications, setApplications] = useState<ApplicationResponse[]>([]);
  const [search, setSearch] = useState("");
  const [statusFilter, setStatusFilter] = useState("active");
  const [remoteFilter, setRemoteFilter] = useState("");
  const [employmentFilter, setEmploymentFilter] = useState("");
  const [eligibleOnly, setEligibleOnly] = useState(false);
  const [pageIndex, setPageIndex] = useState(0);

  const [previewJobId, setPreviewJobId] = useState<string | null>(null);
  const [previewDetail, setPreviewDetail] = useState<JobDetailResponse | null>(null);
  const [previewLoading, setPreviewLoading] = useState(false);

  const [loading, setLoading] = useState(true);
  const [runningDiscovery, setRunningDiscovery] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const loadJobs = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [list, srcList, appList] = await Promise.all([
        apiClient.listJobs({
          status: statusFilter || undefined,
          remote_policy: remoteFilter || undefined,
          employment_type: employmentFilter || undefined,
          eligible_only: eligibleOnly,
          search: search.trim() || undefined,
          limit: PAGE_SIZE,
          offset: pageIndex * PAGE_SIZE,
        }),
        apiClient.listJobSources().catch(() => []),
        apiClient.listApplications().catch(() => []),
      ]);
      setJobs(list);
      setSources(srcList);
      setApplications(appList);
    } catch (err) {
      if (err instanceof ApiError && err.isUnauthorized()) {
        routerRef.current.replace("/login?next=/discover");
        return;
      }
      setError(err instanceof Error ? err.message : "Failed to load discovered jobs");
    } finally {
      setLoading(false);
    }
  }, [eligibleOnly, employmentFilter, pageIndex, remoteFilter, search, statusFilter]);

  useEffect(() => {
    void loadJobs();
  }, [loadJobs]);

  const sourceById = useMemo(
    () => new Map(sources.map((s) => [s.id, s])),
    [sources],
  );
  const appByJobId = useMemo(
    () => new Map(applications.map((a) => [a.job_id, a])),
    [applications],
  );

  async function handleRunDiscovery() {
    setRunningDiscovery(true);
    setError(null);
    setMessage(null);
    try {
      const run = await apiClient.triggerDiscovery({
        query: search.trim(),
        max_pages: 2,
      });
      setMessage(
        `Discovery run ${run.run_id.slice(0, 8)}: ${run.discovered_count} fetched, ${run.created_count} created, ${run.duplicate_count} deduplicated, ${run.expired_count} expired.`,
      );
      setPageIndex(0);
      await loadJobs();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Discovery run failed");
    } finally {
      setRunningDiscovery(false);
    }
  }

  async function handleTogglePreview(jobId: string) {
    if (previewJobId === jobId) {
      setPreviewJobId(null);
      setPreviewDetail(null);
      return;
    }
    setPreviewJobId(jobId);
    setPreviewLoading(true);
    try {
      const detail = await apiClient.getJobDetail(jobId);
      setPreviewDetail(detail);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load job description preview");
      setPreviewJobId(null);
    } finally {
      setPreviewLoading(false);
    }
  }

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight text-[var(--ink-primary)]">
            Discover Jobs
          </h1>
          <p className="mt-1 text-sm text-[var(--ink-secondary)]">
            Inspect permitted, deduplicated openings and start or resume their 11-stage workflow.
          </p>
        </div>

        <button
          type="button"
          disabled={runningDiscovery}
          onClick={() => void handleRunDiscovery()}
          className="inline-flex items-center gap-2 rounded-xl bg-[var(--ink-primary)] px-4 py-2 text-xs font-semibold text-white hover:opacity-90 disabled:opacity-50"
        >
          <Compass className="h-4 w-4" aria-hidden="true" />
          <span>{runningDiscovery ? "Running Discovery..." : "Run Discovery Sync"}</span>
        </button>
      </div>

      {/* Search & Backend Filters */}
      <div className="rounded-2xl border border-[var(--border-hairline)] bg-[var(--bg-surface)] p-4 space-y-3">
        <form
          onSubmit={(e) => {
            e.preventDefault();
            setPageIndex(0);
            void loadJobs();
          }}
          className="flex flex-wrap gap-2"
        >
          <div className="relative flex-1 min-w-[220px]">
            <Search
              className="pointer-events-none absolute left-3 top-2.5 h-4 w-4 text-[var(--ink-muted)]"
              aria-hidden="true"
            />
            <input
              type="search"
              aria-label="Search company or role"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="Search company or role title..."
              className="w-full rounded-xl border border-[var(--border-hairline)] bg-[var(--bg-rail)] py-2 pl-9 pr-3 text-sm text-[var(--ink-primary)]"
            />
          </div>
          <button
            type="submit"
            className="inline-flex items-center gap-1.5 rounded-xl border border-[var(--border-strong)] bg-[var(--bg-surface)] px-4 py-2 text-xs font-semibold text-[var(--ink-primary)] hover:bg-[var(--bg-rail)]"
          >
            <RefreshCw className="h-3.5 w-3.5" aria-hidden="true" />
            <span>Apply Search</span>
          </button>
        </form>

        <div className="flex flex-wrap items-center gap-3 text-xs">
          <label className="flex items-center gap-1.5 text-[var(--ink-secondary)]">
            <span>Status</span>
            <select
              aria-label="Posting status filter"
              value={statusFilter}
              onChange={(e) => {
                setStatusFilter(e.target.value);
                setPageIndex(0);
              }}
              className="rounded-lg border border-[var(--border-hairline)] bg-[var(--bg-rail)] px-2.5 py-1.5 text-xs text-[var(--ink-primary)]"
            >
              <option value="">All</option>
              <option value="active">Active</option>
              <option value="expired">Expired</option>
              <option value="unavailable">Unavailable</option>
            </select>
          </label>

          <label className="flex items-center gap-1.5 text-[var(--ink-secondary)]">
            <span>Work Mode</span>
            <select
              aria-label="Remote policy filter"
              value={remoteFilter}
              onChange={(e) => {
                setRemoteFilter(e.target.value);
                setPageIndex(0);
              }}
              className="rounded-lg border border-[var(--border-hairline)] bg-[var(--bg-rail)] px-2.5 py-1.5 text-xs text-[var(--ink-primary)]"
            >
              <option value="">All</option>
              <option value="remote">Remote</option>
              <option value="hybrid">Hybrid</option>
              <option value="onsite">Onsite</option>
            </select>
          </label>

          <label className="flex items-center gap-1.5 text-[var(--ink-secondary)]">
            <span>Type</span>
            <select
              aria-label="Employment type filter"
              value={employmentFilter}
              onChange={(e) => {
                setEmploymentFilter(e.target.value);
                setPageIndex(0);
              }}
              className="rounded-lg border border-[var(--border-hairline)] bg-[var(--bg-rail)] px-2.5 py-1.5 text-xs text-[var(--ink-primary)]"
            >
              <option value="">All</option>
              <option value="internship">Internship</option>
              <option value="full_time">Full Time</option>
              <option value="contract">Contract</option>
            </select>
          </label>

          <label className="inline-flex items-center gap-1.5 text-[var(--ink-secondary)]">
            <input
              type="checkbox"
              checked={eligibleOnly}
              onChange={(e) => {
                setEligibleOnly(e.target.checked);
                setPageIndex(0);
              }}
              className="rounded border-[var(--border-strong)]"
            />
            <span>Eligible Only</span>
          </label>
        </div>
      </div>

      {message && (
        <div
          role="status"
          className="rounded-xl border border-[var(--state-completed-border)] bg-[var(--state-completed-bg)] p-3 text-xs text-[var(--state-completed-fg)]"
        >
          {message}
        </div>
      )}
      {error && (
        <div
          role="alert"
          className="flex items-center justify-between gap-3 rounded-xl border border-[var(--state-failed-border)] bg-[var(--state-failed-bg)] p-3 text-xs text-[var(--state-failed-fg)]"
        >
          <span>{error}</span>
          <button
            type="button"
            onClick={() => void loadJobs()}
            className="rounded-lg border border-[var(--state-failed-border)] bg-white px-2.5 py-1 font-semibold"
          >
            Retry
          </button>
        </div>
      )}

      {loading ? (
        <div className="space-y-3">
          {[1, 2, 3].map((n) => (
            <div
              key={n}
              className="h-24 animate-pulse rounded-2xl border border-[var(--border-hairline)] bg-[var(--bg-surface)]"
            />
          ))}
        </div>
      ) : jobs.length === 0 ? (
        <div className="rounded-2xl border border-dashed border-[var(--border-strong)] bg-[var(--bg-surface)] p-8 text-center text-xs text-[var(--ink-secondary)]">
          No discovered jobs match your filters. Try clearing filters or running Discovery Sync.
        </div>
      ) : (
        <div className="divide-y divide-[var(--border-hairline)] rounded-2xl border border-[var(--border-hairline)] bg-[var(--bg-surface)]">
          {jobs.map((job) => {
            const src = sourceById.get(job.source_id);
            const existingApp = appByJobId.get(job.id);
            const isPreviewOpen = previewJobId === job.id;

            return (
              <div key={job.id} className="p-4 sm:px-5 space-y-3">
                <div className="flex flex-wrap items-center justify-between gap-4">
                  <div className="min-w-0 space-y-1">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="text-xs font-semibold uppercase tracking-wider text-[var(--ink-muted)]">
                        {job.company}
                      </span>
                      <StatusBadge
                        state={job.status === "active" ? "completed" : "blocked"}
                        customLabel={job.status.toUpperCase()}
                        mono
                      />
                      {src && (
                        <span className="rounded bg-[var(--bg-rail)] px-2 py-0.5 text-[11px] font-medium text-[var(--ink-secondary)]">
                          {src.source_name} ({src.connector_type})
                        </span>
                      )}
                      {job.match && (
                        <StatusBadge
                          state={job.match.eligible ? "current" : "blocked"}
                          customLabel={
                            job.match.eligible
                              ? `ELIGIBLE · ${Math.round(job.match.score)}/100`
                              : `INELIGIBLE · ${Math.round(job.match.score)}/100`
                          }
                          mono
                        />
                      )}
                      {existingApp && (
                        <StatusBadge
                          state="waiting"
                          customLabel={`APP: ${existingApp.status}`}
                          mono
                        />
                      )}
                    </div>

                    <h2 className="text-base font-semibold text-[var(--ink-primary)]">
                      {job.role_title}
                    </h2>
                    <p className="text-xs text-[var(--ink-muted)]">
                      {job.location || "Remote / Unspecified"} · {job.remote_policy} ·{" "}
                      {job.employment_type}
                    </p>
                  </div>

                  <div className="flex flex-wrap items-center gap-2">
                    <button
                      type="button"
                      onClick={() => void handleTogglePreview(job.id)}
                      className="inline-flex items-center gap-1.5 rounded-xl border border-[var(--border-hairline)] bg-[var(--bg-rail)] px-3 py-2 text-xs font-medium text-[var(--ink-secondary)] hover:border-[var(--border-strong)]"
                    >
                      <Eye className="h-3.5 w-3.5" aria-hidden="true" />
                      <span>{isPreviewOpen ? "Hide JD" : "Preview JD"}</span>
                    </button>

                    <Link
                      href={`/workflows/${job.id}`}
                      className="inline-flex items-center gap-1.5 rounded-xl bg-[var(--accent-primary)] px-4 py-2 text-xs font-semibold text-white hover:opacity-90"
                    >
                      <span>{existingApp || job.match ? "Resume Workflow" : "Start Workflow"}</span>
                      <ArrowRight className="h-3.5 w-3.5" aria-hidden="true" />
                    </Link>
                  </div>
                </div>

                {isPreviewOpen && (
                  <div className="rounded-xl border border-[var(--border-hairline)] bg-[var(--bg-rail)] p-4 text-xs">
                    {previewLoading ? (
                      <p className="text-[var(--ink-muted)]">Loading sanitized job description...</p>
                    ) : previewDetail ? (
                      <div className="space-y-2">
                        <p className="font-semibold text-[var(--ink-primary)]">
                          Untrusted Job Description Preview (Rendered as Plain Text)
                        </p>
                        <pre className="max-h-48 overflow-y-auto whitespace-pre-wrap leading-relaxed text-[var(--ink-secondary)]">
                          {previewDetail.raw_jd}
                        </pre>
                      </div>
                    ) : null}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}

      {/* Pagination */}
      <div className="flex items-center justify-between text-xs text-[var(--ink-secondary)]">
        <span className="tabular-nums">Page {pageIndex + 1}</span>
        <div className="flex items-center gap-2">
          <button
            type="button"
            disabled={pageIndex === 0 || loading}
            onClick={() => setPageIndex((p) => Math.max(0, p - 1))}
            className="inline-flex items-center gap-1 rounded-lg border border-[var(--border-hairline)] bg-[var(--bg-surface)] px-3 py-1.5 font-medium disabled:opacity-45"
          >
            <ChevronLeft className="h-3.5 w-3.5" aria-hidden="true" />
            <span>Previous</span>
          </button>
          <button
            type="button"
            disabled={jobs.length < PAGE_SIZE || loading}
            onClick={() => setPageIndex((p) => p + 1)}
            className="inline-flex items-center gap-1 rounded-lg border border-[var(--border-hairline)] bg-[var(--bg-surface)] px-3 py-1.5 font-medium disabled:opacity-45"
          >
            <span>Next</span>
            <ChevronRight className="h-3.5 w-3.5" aria-hidden="true" />
          </button>
        </div>
      </div>
    </div>
  );
}
