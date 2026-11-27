"use client";

import React, { useEffect, useState } from "react";
import { AlertTriangle, Ban, CheckCircle2, Download, FileText } from "lucide-react";
import { apiClient } from "@/lib/api-client";
import type {
  FactualValidationReportResponse,
  ResumeGenerationHistoryResponse,
  TailoredResumeDetailResponse,
  TailoringDiffResponse,
} from "@/lib/types";
import { StatusBadge } from "@/components/StatusBadge";

interface ValidatedClaimLike {
  claim_id?: string;
  category?: string;
  claim_text?: string;
  violation_reason?: string | null;
}

interface SkillReorderLike {
  skill_name?: string;
  original_rank?: number;
  tailored_rank?: number;
  reason?: string;
}

interface ProjectRewriteLike {
  project_id?: string;
  name?: string;
  before_summary?: string;
  after_summary?: string;
  before_bullets?: string[];
  after_bullets?: string[];
}

interface ExperienceRewriteLike {
  experience_id?: string;
  company?: string;
  role?: string;
  before_bullets?: string[];
  after_bullets?: string[];
}

export interface Stage5ResumeReviewProps {
  activeResume: ResumeGenerationHistoryResponse;
  tailoredDetail: TailoredResumeDetailResponse | null;
  tailoringDiff: TailoringDiffResponse | null;
  validationReport: FactualValidationReportResponse | null;
  actionBusy: boolean;
  onApproveResume: (resumeId: string, notes: string) => Promise<void>;
  onRejectResume: (resumeId: string, notes: string) => Promise<void>;
}

const EVAL_LABELS: Array<[string, string]> = [
  ["required_skill_coverage", "Required Skill Coverage"],
  ["preferred_skill_coverage", "Preferred Skill Coverage"],
  ["relevant_evidence_coverage", "Relevant Evidence Coverage"],
  ["keyword_coverage", "Keyword Coverage"],
  ["factual_consistency_score", "Factual Consistency"],
  ["readability_score", "Readability"],
  ["pdf_parsing_quality_score", "PDF Parsing Quality"],
];

export function Stage5ResumeReview({
  activeResume,
  tailoredDetail,
  tailoringDiff,
  validationReport,
  actionBusy,
  onApproveResume,
  onRejectResume,
}: Stage5ResumeReviewProps) {
  const [decisionNotes, setDecisionNotes] = useState("");
  const [showPdfPreview, setShowPdfPreview] = useState(false);
  const [pdfBlobUrl, setPdfBlobUrl] = useState<string | null>(null);
  const [pdfPreviewError, setPdfPreviewError] = useState<string | null>(null);

  const resumeId = activeResume.resume_id;

  useEffect(() => {
    if (!showPdfPreview || !resumeId) {
      return;
    }
    let cancelled = false;
    let createdUrl: string | null = null;
    setPdfPreviewError(null);

    fetch(apiClient.tailoredResumeDownloadUrl(resumeId, false), { cache: "no-store" })
      .then(async (resp) => {
        if (!resp.ok) {
          throw new Error(`Unable to load PDF preview (${resp.status})`);
        }
        return resp.blob();
      })
      .then((blob) => {
        if (cancelled) return;
        createdUrl = URL.createObjectURL(new Blob([blob], { type: "application/pdf" }));
        setPdfBlobUrl(createdUrl);
      })
      .catch((err) => {
        if (!cancelled) {
          setPdfPreviewError(err instanceof Error ? err.message : "Failed to load PDF preview");
        }
      });

    return () => {
      cancelled = true;
      if (createdUrl) {
        URL.revokeObjectURL(createdUrl);
      }
      setPdfBlobUrl(null);
    };
  }, [resumeId, showPdfPreview]);

  if (!resumeId) {
    return null;
  }

  const isPending = activeResume.approval_status === "pending_review";
  const isApproved = activeResume.approval_status === "approved";

  const valBody = (validationReport?.validation_report ??
    tailoredDetail?.validation_report ??
    {}) as Record<string, unknown>;
  const unsupportedClaims = Array.isArray(valBody.unsupported_claims)
    ? (valBody.unsupported_claims as ValidatedClaimLike[])
    : [];

  const evalBody = (tailoredDetail?.evaluation_report ?? {}) as Record<string, unknown>;
  const diffBody = (tailoringDiff?.tailoring_diff ??
    tailoredDetail?.tailoring_diff ??
    {}) as Record<string, unknown>;

  const skillsReordered = Array.isArray(diffBody.skills_reordered)
    ? (diffBody.skills_reordered as SkillReorderLike[])
    : [];
  const projectRewrites = Array.isArray(diffBody.project_rewrites)
    ? (diffBody.project_rewrites as ProjectRewriteLike[])
    : [];
  const experienceRewrites = Array.isArray(diffBody.experience_rewrites)
    ? (diffBody.experience_rewrites as ExperienceRewriteLike[])
    : [];
  const keywordsEmphasized = Array.isArray(diffBody.keywords_emphasized)
    ? (diffBody.keywords_emphasized as Array<{ keyword?: string; section?: string }>)
    : [];

  return (
    <div className="space-y-4">
      {/* Header Card + PDF Preview / Download */}
      <div className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-[var(--border-hairline)] bg-[var(--bg-rail)] p-4">
        <div>
          <p className="text-sm font-semibold text-[var(--ink-primary)]">
            Tailored Resume v{activeResume.version_number} (
            {tailoredDetail?.version_label ?? "PDF"})
          </p>
          <p className="mt-0.5 break-all font-mono text-xs text-[var(--ink-secondary)]">
            SHA-256: {tailoredDetail?.sha256_checksum ?? "verified server-side"}
          </p>
        </div>

        <div className="flex flex-wrap items-center gap-2">
          <StatusBadge
            state={isApproved ? "completed" : isPending ? "waiting" : "blocked"}
            customLabel={activeResume.approval_status.toUpperCase()}
            mono
          />
          <button
            type="button"
            onClick={() => setShowPdfPreview((v) => !v)}
            className="inline-flex items-center gap-1.5 rounded-lg border border-[var(--border-strong)] bg-[var(--bg-surface)] px-3 py-1.5 text-xs font-medium text-[var(--ink-primary)] hover:bg-[var(--bg-subtle)]"
          >
            <FileText className="h-3.5 w-3.5" aria-hidden="true" />
            <span>{showPdfPreview ? "Hide PDF Preview" : "Preview PDF"}</span>
          </button>
          <a
            href={apiClient.tailoredResumeDownloadUrl(resumeId, false)}
            target="_blank"
            rel="noreferrer"
            className="inline-flex items-center gap-1.5 rounded-lg border border-[var(--border-strong)] bg-[var(--bg-surface)] px-3 py-1.5 text-xs font-medium text-[var(--ink-primary)] hover:bg-[var(--bg-subtle)]"
          >
            <Download className="h-3.5 w-3.5" aria-hidden="true" />
            <span>Download PDF</span>
          </a>
        </div>
      </div>

      {showPdfPreview && (
        <div className="overflow-hidden rounded-xl border border-[var(--border-hairline)] bg-[var(--bg-surface)]">
          {pdfPreviewError ? (
            <p className="p-4 text-xs text-[var(--state-failed-fg)]">{pdfPreviewError}</p>
          ) : pdfBlobUrl ? (
            <iframe
              title={`Tailored resume v${activeResume.version_number} PDF preview`}
              src={pdfBlobUrl}
              className="h-[460px] w-full"
            />
          ) : (
            <p className="p-4 text-xs text-[var(--ink-muted)]">Loading authenticated PDF preview...</p>
          )}
        </div>
      )}

      {/* Factual Validation Results & Unsupported Claims */}
      {validationReport && (
        <div className="rounded-xl border border-[var(--border-hairline)] bg-[var(--bg-surface)] p-4 text-xs space-y-2.5">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <span className="font-semibold text-[var(--ink-primary)]">
              Authoritative Factual Validation
            </span>
            <StatusBadge
              state={validationReport.validation_passed ? "completed" : "failed"}
              customLabel={validationReport.validation_passed ? "VALIDATION PASSED" : "VALIDATION FAILED"}
              mono
            />
          </div>
          <p className="text-[var(--ink-secondary)] tabular-nums">
            Supported facts: <strong>{String(valBody.supported_count ?? 0)}</strong> · Rewritten
            facts: <strong>{String(valBody.rewritten_count ?? 0)}</strong> · Unsupported claims:{" "}
            <strong>{String(valBody.unsupported_count ?? unsupportedClaims.length)}</strong>
            {typeof valBody.llm_crosscheck_passed === "boolean"
              ? ` · Cross-check: ${valBody.llm_crosscheck_passed ? "passed" : "flagged"}`
              : ""}
          </p>

          {unsupportedClaims.length > 0 && (
            <div className="rounded-lg border border-[var(--state-failed-border)] bg-[var(--state-failed-bg)] p-3 text-[var(--state-failed-fg)] space-y-1.5">
              <div className="flex items-center gap-1.5 font-semibold">
                <AlertTriangle className="h-3.5 w-3.5 shrink-0" aria-hidden="true" />
                <span>Unsupported Claims Detected ({unsupportedClaims.length})</span>
              </div>
              <ul className="list-disc pl-4 space-y-1">
                {unsupportedClaims.map((claim, idx) => (
                  <li key={claim.claim_id ?? idx}>
                    <span className="font-medium">{claim.claim_text}</span>
                    {claim.violation_reason ? ` — ${claim.violation_reason}` : ""}
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>
      )}

      {/* 7-Metric ATS & Evaluation Breakdown */}
      <div className="rounded-xl border border-[var(--border-hairline)] bg-[var(--bg-surface)] p-4 text-xs space-y-2.5">
        <div className="flex items-center justify-between">
          <span className="font-semibold text-[var(--ink-primary)]">
            ATS & Quality Evaluation
          </span>
          {typeof tailoredDetail?.ats_report?.detected_heading_count === "number" && (
            <span className="font-mono text-[11px] text-[var(--ink-muted)]">
              Headings parsed: {String(tailoredDetail.ats_report.detected_heading_count)}
            </span>
          )}
        </div>
        <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
          {EVAL_LABELS.map(([key, label]) => {
            const raw = evalBody[key];
            if (typeof raw !== "number") return null;
            return (
              <div
                key={key}
                className="rounded-lg border border-[var(--border-hairline)] bg-[var(--bg-rail)] px-3 py-2"
              >
                <p className="text-[11px] text-[var(--ink-muted)]">{label}</p>
                <p className="mt-0.5 font-mono text-sm font-semibold text-[var(--ink-primary)] tabular-nums">
                  {Math.round(raw * 100)}%
                </p>
              </div>
            );
          })}
        </div>
        {typeof evalBody.disclaimer === "string" && (
          <p className="text-[11px] text-[var(--ink-muted)]">{evalBody.disclaimer}</p>
        )}
      </div>

      {/* Original vs Tailored Comparison & Highlighted Changes */}
      <div className="rounded-xl border border-[var(--border-hairline)] bg-[var(--bg-surface)] p-4 text-xs space-y-3">
        <h3 className="font-semibold text-[var(--ink-primary)]">
          Original vs. Tailored Comparison & Highlighted Changes
        </h3>

        {skillsReordered.length === 0 &&
        projectRewrites.length === 0 &&
        experienceRewrites.length === 0 &&
        keywordsEmphasized.length === 0 ? (
          <p className="text-[var(--ink-muted)]">
            No structured rewrite diff entries recorded for this version.
          </p>
        ) : (
          <div className="space-y-3">
            {keywordsEmphasized.length > 0 && (
              <div className="flex flex-wrap items-center gap-1.5">
                <span className="font-medium text-[var(--ink-secondary)]">Keywords emphasized:</span>
                {keywordsEmphasized.map((k, idx) => (
                  <span
                    key={`${k.keyword}-${idx}`}
                    className="rounded-md border border-[var(--accent-primary-border)] bg-[var(--accent-primary-soft)] px-2 py-0.5 font-medium text-[var(--accent-primary)]"
                  >
                    {k.keyword}
                  </span>
                ))}
              </div>
            )}

            {skillsReordered.length > 0 && (
              <div className="space-y-1">
                <p className="font-medium text-[var(--ink-secondary)]">Skills Reordered</p>
                <ul className="space-y-1">
                  {skillsReordered.slice(0, 6).map((s, idx) => (
                    <li
                      key={`${s.skill_name}-${idx}`}
                      className="rounded-lg bg-[var(--bg-rail)] px-3 py-1.5 text-[var(--ink-secondary)]"
                    >
                      <strong className="text-[var(--ink-primary)]">{s.skill_name}</strong> (rank{" "}
                      {s.original_rank ?? "—"} → {s.tailored_rank ?? "—"}): {s.reason}
                    </li>
                  ))}
                </ul>
              </div>
            )}

            {projectRewrites.map((proj, idx) => (
              <div
                key={proj.project_id ?? idx}
                className="rounded-lg border border-[var(--border-hairline)] bg-[var(--bg-rail)] p-3 space-y-2"
              >
                <p className="font-semibold text-[var(--ink-primary)]">Project: {proj.name}</p>
                <div className="grid gap-3 sm:grid-cols-2">
                  <div>
                    <p className="text-[11px] font-semibold uppercase text-[var(--ink-muted)]">
                      Original
                    </p>
                    <p className="mt-1 text-[var(--ink-secondary)]">{proj.before_summary}</p>
                  </div>
                  <div>
                    <p className="text-[11px] font-semibold uppercase text-[var(--state-completed-fg)]">
                      Tailored
                    </p>
                    <p className="mt-1 text-[var(--ink-primary)]">{proj.after_summary}</p>
                  </div>
                </div>
              </div>
            ))}

            {experienceRewrites.map((exp, idx) => (
              <div
                key={exp.experience_id ?? idx}
                className="rounded-lg border border-[var(--border-hairline)] bg-[var(--bg-rail)] p-3 space-y-2"
              >
                <p className="font-semibold text-[var(--ink-primary)]">
                  Experience: {exp.role} @ {exp.company}
                </p>
                <div className="grid gap-3 sm:grid-cols-2">
                  <div>
                    <p className="text-[11px] font-semibold uppercase text-[var(--ink-muted)]">
                      Original Bullets
                    </p>
                    <ul className="mt-1 list-disc pl-4 space-y-1 text-[var(--ink-secondary)]">
                      {(exp.before_bullets ?? []).map((b, bIdx) => (
                        <li key={bIdx}>{b}</li>
                      ))}
                    </ul>
                  </div>
                  <div>
                    <p className="text-[11px] font-semibold uppercase text-[var(--state-completed-fg)]">
                      Tailored Bullets
                    </p>
                    <ul className="mt-1 list-disc pl-4 space-y-1 text-[var(--ink-primary)]">
                      {(exp.after_bullets ?? []).map((b, bIdx) => (
                        <li key={bIdx}>{b}</li>
                      ))}
                    </ul>
                  </div>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* Explicit Owner Approval Gate */}
      {!isApproved && (
        <div className="rounded-xl border border-[var(--state-waiting-border)] bg-[var(--state-waiting-bg)] p-4 space-y-3">
          <p className="text-xs font-semibold text-[var(--state-waiting-fg)]">
            Explicit Owner Approval Required (Never Auto-Approved)
          </p>
          <input
            type="text"
            value={decisionNotes}
            onChange={(e) => setDecisionNotes(e.target.value)}
            placeholder="Optional review note recorded in ResumeJobMapping..."
            className="w-full rounded-lg border border-[var(--border-hairline)] bg-[var(--bg-surface)] px-3 py-2 text-xs text-[var(--ink-primary)]"
          />
          <div className="flex flex-wrap gap-2">
            <button
              type="button"
              disabled={actionBusy}
              onClick={() => onApproveResume(resumeId, decisionNotes)}
              className="inline-flex items-center gap-1.5 rounded-xl bg-[var(--state-completed-fg)] px-4 py-2 text-xs font-semibold text-white hover:opacity-90 disabled:opacity-50"
            >
              <CheckCircle2 className="h-4 w-4" aria-hidden="true" />
              <span>Approve Tailored Resume</span>
            </button>
            <button
              type="button"
              disabled={actionBusy}
              onClick={() => onRejectResume(resumeId, decisionNotes)}
              className="inline-flex items-center gap-1.5 rounded-xl border border-[var(--state-blocked-border)] bg-[var(--bg-surface)] px-4 py-2 text-xs font-semibold text-[var(--state-blocked-fg)] hover:bg-[var(--state-blocked-bg)] disabled:opacity-50"
            >
              <Ban className="h-4 w-4" aria-hidden="true" />
              <span>Reject Version</span>
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
