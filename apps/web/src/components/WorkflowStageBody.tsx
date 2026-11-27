"use client";

import React, { useState } from "react";
import Link from "next/link";
import {
  ExternalLink,
  RefreshCw,
  Sparkles,
} from "lucide-react";
import type {
  ApplicationAnswerInput,
  ApplicationDetailResponse,
  ApplicationMode,
  ApplicationPreparationResult,
  ExecutionResult,
  FactualValidationReportResponse,
  JobDetailResponse,
  JobEvidenceResponse,
  JobIntelligenceResponse,
  JobResumeHistoryResponse,
  ResumeMetadataResponse,
  TailoredResumeDetailResponse,
  TailoringDiffResponse,
} from "@/lib/types";
import type { DerivedWorkflowState, StageDerivation } from "@/lib/workflow-engine";
import { StatusBadge } from "@/components/StatusBadge";
import { HoldAndConfirmPanel } from "@/components/HoldAndConfirmPanel";
import { Stage5ResumeReview } from "@/components/Stage5ResumeReview";
import { Stage9HandoffPanel } from "@/components/Stage9HandoffPanel";
import { Stage11VerificationPanel } from "@/components/Stage11VerificationPanel";
import {
  Stage6PreparePanel,
  Stage7QuestionsPanel,
  Stage8ApprovalPanel,
} from "@/components/Stage6To8ApplicationPrep";

export interface WorkflowStageBodyProps {
  viewStep: number;
  stage: StageDerivation;
  derived: DerivedWorkflowState;
  job: JobDetailResponse;
  intelligence: JobIntelligenceResponse | null;
  evidence: JobEvidenceResponse | null;
  resumeHistory: JobResumeHistoryResponse | null;
  masterResumes: ResumeMetadataResponse[];
  tailoredDetail: TailoredResumeDetailResponse | null;
  tailoringDiff: TailoringDiffResponse | null;
  validationReport: FactualValidationReportResponse | null;
  applicationDetail: ApplicationDetailResponse | null;
  preparationResult: ApplicationPreparationResult | null;
  executionResult: ExecutionResult | null;
  actionBusy: boolean;
  onAnalyzeJob: (useLlm: boolean) => Promise<void>;
  onTailorResume: (forceRegenerate: boolean) => Promise<void>;
  onApproveResume: (resumeId: string, notes: string) => Promise<void>;
  onRejectResume: (resumeId: string, notes: string) => Promise<void>;
  onPrepareApplication: (preferredMode: ApplicationMode | null) => Promise<void>;
  onSubmitAnswers: (items: ApplicationAnswerInput[]) => Promise<void>;
  onApproveApplication: (notes: string) => Promise<void>;
  onRejectApplication: (notes: string) => Promise<void>;
  onExecuteApplication: (browserMode: "visible_handoff" | "headless") => Promise<void>;
  onRefreshHandoff: (handoffSessionId: string) => Promise<void>;
  onCloseHandoff: (handoffSessionId: string) => Promise<void>;
  onAuthorizeManualSubmit: (payload: {
    handoff_session_id: string;
    pending_submission_id: string;
    expected_form_action: string;
    expected_http_method: "GET" | "POST";
    expected_payload_sha256: string;
  }) => Promise<void>;
  onRecordOutcome: (
    outcome: "submitted_in_browser" | "failed" | "interrupted",
    notes: string,
  ) => Promise<void>;
  onReconcileConfirmation: (receiptId: string, verificationSource: string) => Promise<void>;
}

export function WorkflowStageBody(props: WorkflowStageBodyProps) {
  const {
    viewStep,
    stage,
    derived,
    job,
    intelligence,
    evidence,
    resumeHistory,
    tailoredDetail,
    tailoringDiff,
    validationReport,
    applicationDetail,
    preparationResult,
    executionResult,
    actionBusy,
  } = props;

  const [selectedMasterId, setSelectedMasterId] = useState<string>("");

  const match = intelligence?.match ?? job.match ?? null;
  const activeResume = derived.activeResumeVersion;
  const app = applicationDetail?.application ?? null;
  const activeHandoff =
    applicationDetail?.active_handoff_sessions.find((s) => s.status === "active") ?? null;

  // Step 1: Job Discovery
  if (viewStep === 1) {
    return (
      <div className="space-y-4">
        <dl className="grid gap-3 rounded-xl border border-[var(--border-hairline)] bg-[var(--bg-rail)] p-4 text-sm sm:grid-cols-2">
          <div>
            <dt className="text-xs text-[var(--ink-muted)]">Company & Role</dt>
            <dd className="mt-0.5 font-semibold text-[var(--ink-primary)]">
              {job.company} — {job.role_title}
            </dd>
          </div>
          <div>
            <dt className="text-xs text-[var(--ink-muted)]">Posting Status</dt>
            <dd className="mt-1">
              <StatusBadge
                state={job.status === "active" ? "completed" : "blocked"}
                customLabel={job.status.toUpperCase()}
                mono
              />
            </dd>
          </div>
          <div>
            <dt className="text-xs text-[var(--ink-muted)]">Location & Remote Policy</dt>
            <dd className="mt-0.5 text-[var(--ink-secondary)]">
              {job.location || "Unspecified"} ({job.remote_policy}, {job.employment_type})
            </dd>
          </div>
          <div>
            <dt className="text-xs text-[var(--ink-muted)]">First Seen / Reposts</dt>
            <dd className="mt-0.5 text-[var(--ink-secondary)] tabular-nums">
              {new Date(job.first_seen_at).toLocaleString()} · {job.repost_count} repost(s)
            </dd>
          </div>
          <div className="sm:col-span-2">
            <dt className="text-xs text-[var(--ink-muted)]">Canonical / Application URL</dt>
            <dd className="mt-0.5 flex items-center gap-2 break-all font-mono text-xs text-[var(--ink-secondary)]">
              <span>{job.application_url || job.canonical_url}</span>
              <a
                href={job.application_url || job.canonical_url}
                target="_blank"
                rel="noreferrer"
                className="inline-flex shrink-0 items-center gap-1 text-[var(--accent-primary)] hover:underline"
              >
                <ExternalLink className="h-3.5 w-3.5" aria-hidden="true" />
              </a>
            </dd>
          </div>
        </dl>

        <details className="rounded-xl border border-[var(--border-hairline)] bg-[var(--bg-surface)] p-4">
          <summary className="cursor-pointer text-xs font-semibold text-[var(--ink-secondary)]">
            View Sanitized Raw Job Description ({job.raw_jd.length} chars)
          </summary>
          <pre className="mt-3 max-h-64 overflow-y-auto whitespace-pre-wrap text-xs leading-relaxed text-[var(--ink-secondary)]">
            {job.raw_jd}
          </pre>
        </details>
      </div>
    );
  }

  // Step 2: JD Analysis
  if (viewStep === 2) {
    const req = intelligence?.requirements ?? job.requirements;
    const mandatoryQuals = (req?.mandatory_qualifications ?? [])
      .map((q) => String(q.value ?? q.text ?? "").trim())
      .filter(Boolean);
    const preferredQuals = (req?.preferred_qualifications ?? [])
      .map((q) => String(q.value ?? q.text ?? "").trim())
      .filter(Boolean);

    return (
      <div className="space-y-4">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <p className="text-sm text-[var(--ink-secondary)]">
            {req
              ? `Structured JD intelligence extracted via ${req.extraction_mode}. Untrusted JD text is rendered strictly as data.`
              : "Extract required skills, responsibilities, qualifications, and prompt-injection checks."}
          </p>
          <button
            type="button"
            disabled={actionBusy || job.status !== "active"}
            onClick={() => props.onAnalyzeJob(true)}
            className="inline-flex items-center gap-2 rounded-xl bg-[var(--accent-primary)] px-4 py-2 text-sm font-semibold text-white hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-50"
          >
            <Sparkles className="h-4 w-4" aria-hidden="true" />
            <span>{actionBusy ? "Analyzing..." : req ? "Re-run JD Analysis" : "Analyze Job Description"}</span>
          </button>
        </div>

        {req && (
          <div className="space-y-4">
            {req.prompt_injection_detected && (
              <div className="rounded-xl border border-[var(--state-waiting-border)] bg-[var(--state-waiting-bg)] p-3 text-xs text-[var(--state-waiting-fg)]">
                <strong>Prompt Injection Signal Detected:</strong> Untrusted directives inside this JD were quarantined and ignored by the analyzer.
              </div>
            )}

            <dl className="grid gap-3 rounded-xl border border-[var(--border-hairline)] bg-[var(--bg-surface)] p-4 text-xs sm:grid-cols-3">
              <div>
                <dt className="text-[var(--ink-muted)]">Role Category</dt>
                <dd className="mt-0.5 font-semibold text-[var(--ink-primary)]">{req.role_category}</dd>
              </div>
              <div>
                <dt className="text-[var(--ink-muted)]">Experience Requirements</dt>
                <dd className="mt-0.5 font-semibold text-[var(--ink-primary)] tabular-nums">
                  {req.experience_min ?? "—"} to {req.experience_max ?? "—"} yrs
                </dd>
              </div>
              <div>
                <dt className="text-[var(--ink-muted)]">Location & Remote Constraint</dt>
                <dd className="mt-0.5 font-semibold text-[var(--ink-primary)]">
                  {req.location || job.location || "Unspecified"} ({req.remote_policy})
                </dd>
              </div>
            </dl>

            <div className="grid gap-4 sm:grid-cols-2">
              <div className="rounded-xl border border-[var(--border-hairline)] bg-[var(--bg-rail)] p-4">
                <p className="text-xs font-semibold uppercase tracking-wider text-[var(--ink-muted)]">
                  Required Skills ({req.required_skills.length})
                </p>
                <div className="mt-2 flex flex-wrap gap-1.5">
                  {req.required_skills.length === 0 ? (
                    <span className="text-xs text-[var(--ink-muted)]">None explicitly extracted</span>
                  ) : (
                    req.required_skills.map((skill) => (
                      <span
                        key={skill}
                        className="rounded-md border border-[var(--border-hairline)] bg-[var(--bg-surface)] px-2 py-0.5 text-xs font-medium text-[var(--ink-primary)]"
                      >
                        {skill}
                      </span>
                    ))
                  )}
                </div>
              </div>

              <div className="rounded-xl border border-[var(--border-hairline)] bg-[var(--bg-rail)] p-4">
                <p className="text-xs font-semibold uppercase tracking-wider text-[var(--ink-muted)]">
                  Preferred & Trainable Skills ({req.preferred_skills.length + req.trainable_skills.length})
                </p>
                <div className="mt-2 flex flex-wrap gap-1.5">
                  {req.preferred_skills.length === 0 && req.trainable_skills.length === 0 ? (
                    <span className="text-xs text-[var(--ink-muted)]">None explicitly extracted</span>
                  ) : (
                    [...req.preferred_skills, ...req.trainable_skills].map((skill) => (
                      <span
                        key={skill}
                        className="rounded-md border border-[var(--border-hairline)] bg-[var(--bg-surface)] px-2 py-0.5 text-xs text-[var(--ink-secondary)]"
                      >
                        {skill}
                      </span>
                    ))
                  )}
                </div>
              </div>
            </div>

            {req.responsibilities.length > 0 && (
              <div className="rounded-xl border border-[var(--border-hairline)] bg-[var(--bg-surface)] p-4 text-xs space-y-1.5">
                <p className="font-semibold text-[var(--ink-primary)]">
                  Responsibilities ({req.responsibilities.length})
                </p>
                <ul className="list-disc pl-4 space-y-1 text-[var(--ink-secondary)]">
                  {req.responsibilities.map((item, idx) => (
                    <li key={idx}>{item}</li>
                  ))}
                </ul>
              </div>
            )}

            <div className="grid gap-4 sm:grid-cols-2 text-xs">
              <div className="rounded-xl border border-[var(--border-hairline)] bg-[var(--bg-rail)] p-4 space-y-1.5">
                <p className="font-semibold text-[var(--ink-primary)]">Education Requirements</p>
                {req.education_requirements.length === 0 ? (
                  <p className="text-[var(--ink-muted)]">No strict degree filter extracted.</p>
                ) : (
                  <ul className="list-disc pl-4 space-y-1 text-[var(--ink-secondary)]">
                    {req.education_requirements.map((ed, idx) => (
                      <li key={idx}>{ed}</li>
                    ))}
                  </ul>
                )}
              </div>

              <div className="rounded-xl border border-[var(--border-hairline)] bg-[var(--bg-rail)] p-4 space-y-1.5">
                <p className="font-semibold text-[var(--ink-primary)]">
                  Qualifications & Uncertainties
                </p>
                {mandatoryQuals.length === 0 && preferredQuals.length === 0 && req.uncertainties.length === 0 ? (
                  <p className="text-[var(--ink-muted)]">No extra qualifications or ambiguities flagged.</p>
                ) : (
                  <ul className="list-disc pl-4 space-y-1 text-[var(--ink-secondary)]">
                    {mandatoryQuals.map((q, idx) => (
                      <li key={`mq-${idx}`}>Mandatory: {q}</li>
                    ))}
                    {preferredQuals.map((q, idx) => (
                      <li key={`pq-${idx}`}>Preferred: {q}</li>
                    ))}
                    {req.uncertainties.map((u, idx) => (
                      <li key={`unc-${idx}`} className="text-[var(--state-waiting-fg)]">
                        Ambiguity: {u}
                      </li>
                    ))}
                  </ul>
                )}
              </div>
            </div>
          </div>
        )}
      </div>
    );
  }

  // Step 3: Fit & Eligibility
  if (viewStep === 3) {
    const reqSkills = intelligence?.requirements?.required_skills ?? job.requirements?.required_skills ?? [];
    const prefSkills = intelligence?.requirements?.preferred_skills ?? job.requirements?.preferred_skills ?? [];
    const matchedSet = new Set((match?.matched_skills ?? []).map((s) => s.toLowerCase()));
    const prefGapSet = new Set((match?.preferred_gaps ?? []).map((s) => s.toLowerCase()));
    const matchedPrefSkills = prefSkills.filter((s) => !prefGapSet.has(s.toLowerCase()));
    const matchedReqSkills =
      reqSkills.length > 0
        ? reqSkills.filter((s) => matchedSet.has(s.toLowerCase()))
        : (match?.matched_skills ?? []);

    return (
      <div className="space-y-4">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <p className="text-sm text-[var(--ink-secondary)]">
            Hard eligibility constraints are evaluated independently from rubric alignment score.
          </p>
          <button
            type="button"
            disabled={actionBusy || !job.requirements}
            onClick={() => props.onAnalyzeJob(true)}
            className="inline-flex items-center gap-2 rounded-xl border border-[var(--border-strong)] bg-[var(--bg-surface)] px-3.5 py-2 text-xs font-semibold text-[var(--ink-primary)] hover:bg-[var(--bg-rail)] disabled:opacity-50"
          >
            <span>{actionBusy ? "Scoring..." : "Re-evaluate Eligibility & Fit"}</span>
          </button>
        </div>

        {match ? (
          <div className="space-y-4">
            <div className="grid gap-3 sm:grid-cols-3">
              <div className="rounded-xl border border-[var(--border-hairline)] bg-[var(--bg-rail)] p-4">
                <p className="text-xs text-[var(--ink-muted)]">Hard Eligibility Gate</p>
                <div className="mt-1.5">
                  <StatusBadge
                    state={match.eligible && match.hard_constraints_passed ? "completed" : "blocked"}
                    customLabel={
                      match.eligible && match.hard_constraints_passed ? "ELIGIBLE" : "INELIGIBLE"
                    }
                    mono
                  />
                </div>
              </div>
              <div className="rounded-xl border border-[var(--border-hairline)] bg-[var(--bg-rail)] p-4">
                <p className="text-xs text-[var(--ink-muted)]">Overall Match Score</p>
                <p className="mt-1 text-2xl font-semibold text-[var(--ink-primary)] tabular-nums">
                  {Math.round(match.score)}
                  <span className="text-sm font-normal text-[var(--ink-muted)]"> / 100</span>
                </p>
              </div>
              <div className="rounded-xl border border-[var(--border-hairline)] bg-[var(--bg-rail)] p-4">
                <p className="text-xs text-[var(--ink-muted)]">Recommendation & Confidence</p>
                <p className="mt-1 text-sm font-semibold text-[var(--ink-primary)]">
                  {match.recommendation} ({Math.round(match.confidence * 100)}%)
                </p>
              </div>
            </div>

            {(!match.eligible || !match.hard_constraints_passed) && (
              <div className="rounded-xl border border-[var(--state-blocked-border)] bg-[var(--state-blocked-bg)] p-4 text-xs text-[var(--state-blocked-fg)]">
                <p className="font-semibold">
                  Ineligible Role — Hard Constraints Failed (Cannot Proceed to Tailoring)
                </p>
                <ul className="mt-1.5 list-disc pl-4 space-y-1">
                  {(match.eligibility_blockers.length > 0
                    ? match.eligibility_blockers
                    : [match.explanation]
                  ).map((b) => (
                    <li key={b}>{b}</li>
                  ))}
                </ul>
              </div>
            )}

            {match.eligible && match.hard_constraints_passed && match.score < 75 && (
              <div className="rounded-xl border border-[var(--state-waiting-border)] bg-[var(--state-waiting-bg)] p-4 text-xs text-[var(--state-waiting-fg)]">
                <p className="font-semibold">Eligible Role with Lower Fit Score ({Math.round(match.score)}/100)</p>
                <p className="mt-1">
                  This job passes hard eligibility constraints, so resume tailoring is permitted,
                  but <code className="font-mono">ApplicationPolicyRouter</code> requires{" "}
                  <code className="font-mono">MIN_JOB_MATCH_SCORE</code> (75) before Step 6
                  application preparation.
                </p>
              </div>
            )}

            <div className="rounded-xl border border-[var(--border-hairline)] bg-[var(--bg-surface)] p-4 text-xs leading-relaxed text-[var(--ink-secondary)] space-y-1.5">
              <p className="font-semibold text-[var(--ink-primary)]">Decision Explanation</p>
              <p>{match.explanation}</p>
              <p className="text-[11px] text-[var(--ink-muted)]">
                {intelligence?.score_disclaimer ||
                  "Estimated rubric alignment score; not a hiring probability."}
              </p>
            </div>

            <div className="grid gap-4 sm:grid-cols-3 text-xs">
              <div className="rounded-xl border border-[var(--border-hairline)] bg-[var(--bg-rail)] p-4">
                <p className="font-semibold text-[var(--state-completed-fg)]">
                  Required Matches ({matchedReqSkills.length})
                </p>
                <p className="mt-1.5 text-[var(--ink-secondary)]">
                  {matchedReqSkills.join(", ") || "None"}
                </p>
              </div>
              <div className="rounded-xl border border-[var(--border-hairline)] bg-[var(--bg-rail)] p-4">
                <p className="font-semibold text-[var(--accent-primary)]">
                  Preferred Matches ({matchedPrefSkills.length})
                </p>
                <p className="mt-1.5 text-[var(--ink-secondary)]">
                  {matchedPrefSkills.join(", ") || "None"}
                </p>
              </div>
              <div className="rounded-xl border border-[var(--border-hairline)] bg-[var(--bg-rail)] p-4">
                <p className="font-semibold text-[var(--state-waiting-fg)]">
                  Missing Requirements ({match.missing_required_skills.length})
                </p>
                <p className="mt-1.5 text-[var(--ink-secondary)]">
                  {match.missing_required_skills.join(", ") || "None"}
                </p>
                {evidence && evidence.missing_evidence.length > 0 && (
                  <p className="mt-1 text-[11px] text-[var(--ink-muted)]">
                    {evidence.missing_evidence.length} item(s) lack verified profile evidence.
                  </p>
                )}
              </div>
            </div>
          </div>
        ) : (
          <div className="rounded-xl border border-[var(--border-hairline)] bg-[var(--bg-rail)] p-4 text-xs text-[var(--ink-secondary)]">
            No eligibility assessment saved yet. Click Re-evaluate Eligibility & Fit above.
          </div>
        )}
      </div>
    );
  }

  // Step 4: Resume Tailoring
  if (viewStep === 4) {
    const outbox = resumeHistory?.outbox_jobs ?? [];
    const versions = resumeHistory?.versions ?? [];
    const isGenerating = outbox.some((j) => j.status === "queued" || j.status === "running");
    const effectiveMasterId = selectedMasterId || props.masterResumes[0]?.id || "";
    const hasMasterResume = props.masterResumes.length > 0;

    return (
      <div className="space-y-4">
        <div className="rounded-xl border border-[var(--border-hairline)] bg-[var(--bg-rail)] p-4 text-xs space-y-2.5">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <span className="font-semibold text-[var(--ink-primary)]">
              Master Resume Source
            </span>
            <Link href="/profile" className="text-[var(--accent-primary)] hover:underline">
              Manage Master Resumes
            </Link>
          </div>
          {!hasMasterResume ? (
            <p className="text-[var(--state-waiting-fg)]">
              No Master Resume uploaded yet. Upload a PDF or DOCX in Profile and Resumes first.
            </p>
          ) : (
            <div className="space-y-1">
              <select
                aria-label="Master resume for tailoring"
                value={effectiveMasterId}
                onChange={(e) => setSelectedMasterId(e.target.value)}
                className="w-full rounded-lg border border-[var(--border-hairline)] bg-[var(--bg-surface)] px-3 py-2 text-xs text-[var(--ink-primary)]"
              >
                {props.masterResumes.map((mr, idx) => (
                  <option key={mr.id} value={mr.id}>
                    {mr.version_label} · {mr.original_filename}
                    {idx === 0 ? " (Active latest master)" : ""}
                  </option>
                ))}
              </select>
              <p className="text-[11px] text-[var(--ink-muted)]">
                Backend outbox generation automatically binds to your latest uploaded Master Resume.
              </p>
            </div>
          )}
        </div>

        <div className="flex flex-wrap items-center justify-between gap-3">
          <p className="text-sm text-[var(--ink-secondary)]">
            Every tailored PDF starts in <code className="font-mono">pending_review</code> and is
            deduplicated by evidence fingerprint so reloads never spawn duplicate jobs.
          </p>
          <button
            type="button"
            disabled={actionBusy || stage.state === "upcoming" || isGenerating || !hasMasterResume}
            onClick={() => props.onTailorResume(Boolean(activeResume))}
            className="inline-flex items-center gap-2 rounded-xl bg-[var(--accent-primary)] px-4 py-2 text-sm font-semibold text-white hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-50"
          >
            <Sparkles className="h-4 w-4" aria-hidden="true" />
            <span>
              {isGenerating
                ? "Worker Generating..."
                : actionBusy
                  ? "Queueing..."
                  : activeResume
                    ? "Regenerate New Version"
                    : "Generate Tailored Resume"}
            </span>
          </button>
        </div>

        {isGenerating && (
          <div className="flex items-center gap-2 rounded-xl border border-[var(--state-waiting-border)] bg-[var(--state-waiting-bg)] p-3 text-xs text-[var(--state-waiting-fg)]">
            <RefreshCw className="h-4 w-4 animate-spin shrink-0" aria-hidden="true" />
            <span>
              Celery worker is generating and factually validating your resume. Safe to reload or
              leave—progress is persisted in PostgreSQL outbox.
            </span>
          </div>
        )}

        {outbox.length > 0 && (
          <div className="rounded-xl border border-[var(--border-hairline)] bg-[var(--bg-rail)] p-4">
            <p className="text-xs font-semibold uppercase tracking-wider text-[var(--ink-muted)]">
              Celery Outbox Generation Jobs ({outbox.length})
            </p>
            <ul className="mt-2 space-y-2 text-xs">
              {outbox.slice(0, 4).map((jobItem) => (
                <li
                  key={jobItem.id}
                  className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-[var(--border-hairline)] bg-[var(--bg-surface)] px-3 py-2"
                >
                  <span className="font-mono text-[var(--ink-primary)]">
                    {jobItem.trigger_mode} · attempt {jobItem.attempt_count}/{jobItem.max_attempts}
                  </span>
                  <div className="flex items-center gap-2">
                    {jobItem.error_message && (
                      <span className="text-[var(--state-failed-fg)]">{jobItem.error_message}</span>
                    )}
                    <StatusBadge
                      state={
                        jobItem.status === "completed"
                          ? "completed"
                          : jobItem.status === "failed" || jobItem.status === "validation_failed"
                            ? "failed"
                            : "waiting"
                      }
                      customLabel={jobItem.status.toUpperCase()}
                      mono
                    />
                  </div>
                </li>
              ))}
            </ul>
          </div>
        )}

        <div className="rounded-xl border border-[var(--border-hairline)] bg-[var(--bg-surface)] p-4">
          <p className="text-xs font-semibold uppercase tracking-wider text-[var(--ink-muted)]">
            Tailored Resume Versions ({versions.length})
          </p>
          {versions.length === 0 ? (
            <p className="mt-2 text-xs text-[var(--ink-muted)]">
              No tailored resume versions generated yet.
            </p>
          ) : (
            <ul className="mt-2 space-y-2 text-xs">
              {versions.map((ver) => (
                <li
                  key={ver.mapping_id}
                  className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-[var(--border-hairline)] bg-[var(--bg-rail)] px-3 py-2"
                >
                  <div>
                    <span className="font-semibold text-[var(--ink-primary)]">
                      Version v{ver.version_number}
                    </span>
                    <span className="ml-2 font-mono text-[11px] text-[var(--ink-muted)]">
                      fp:{ver.evidence_fingerprint.slice(0, 10)}
                    </span>
                  </div>
                  <StatusBadge
                    state={
                      ver.status === "completed"
                        ? ver.approval_status === "approved"
                          ? "completed"
                          : ver.approval_status === "rejected"
                            ? "blocked"
                            : "waiting"
                        : "failed"
                    }
                    customLabel={`${ver.status} / ${ver.approval_status}`}
                    mono
                  />
                </li>
              ))}
            </ul>
          )}
        </div>
      </div>
    );
  }

  // Step 5: Resume Review & Approval
  if (viewStep === 5) {
    if (!activeResume || !activeResume.resume_id) {
      return (
        <div className="rounded-xl border border-[var(--border-hairline)] bg-[var(--bg-rail)] p-4 text-xs text-[var(--ink-secondary)]">
          Generate a completed tailored resume in Step 4 before reviewing and approving it here.
        </div>
      );
    }

    return (
      <Stage5ResumeReview
        activeResume={activeResume}
        tailoredDetail={tailoredDetail}
        tailoringDiff={tailoringDiff}
        validationReport={validationReport}
        actionBusy={actionBusy}
        onApproveResume={props.onApproveResume}
        onRejectResume={props.onRejectResume}
      />
    );
  }

  // Step 6: Application Preparation & Form Understanding
  if (viewStep === 6) {
    return (
      <Stage6PreparePanel
        job={job}
        activeResume={activeResume}
        tailoredDetail={tailoredDetail}
        applicationDetail={applicationDetail}
        preparationResult={preparationResult}
        actionBusy={actionBusy}
        fitScore={match ? match.score : null}
        onPrepareApplication={props.onPrepareApplication}
      />
    );
  }

  // Step 7: Application Question Completion
  if (viewStep === 7) {
    return (
      <Stage7QuestionsPanel
        applicationDetail={applicationDetail}
        preparationResult={preparationResult}
        actionBusy={actionBusy}
        onSubmitAnswers={props.onSubmitAnswers}
      />
    );
  }

  // Step 8: Final Application Review & Approval
  if (viewStep === 8) {
    return (
      <Stage8ApprovalPanel
        job={job}
        stage={stage}
        activeResume={activeResume}
        tailoredDetail={tailoredDetail}
        applicationDetail={applicationDetail}
        preparationResult={preparationResult}
        actionBusy={actionBusy}
        onApproveApplication={props.onApproveApplication}
        onRejectApplication={props.onRejectApplication}
      />
    );
  }

  // Step 9: Browser Prefill & Visible Handoff
  if (viewStep === 9) {
    if (!app || !applicationDetail) {
      return (
        <div className="rounded-xl border border-[var(--border-hairline)] bg-[var(--bg-rail)] p-4 text-xs text-[var(--ink-secondary)]">
          Complete application approval first.
        </div>
      );
    }

    return (
      <Stage9HandoffPanel
        job={job}
        applicationDetail={applicationDetail}
        activeResume={activeResume}
        tailoredDetail={tailoredDetail}
        activeHandoff={activeHandoff}
        executionResult={executionResult}
        actionBusy={actionBusy}
        onExecuteApplication={props.onExecuteApplication}
        onRefreshHandoff={props.onRefreshHandoff}
        onCloseHandoff={props.onCloseHandoff}
      />
    );
  }

  // Step 10: Hold-and-Confirm Submission
  if (viewStep === 10) {
    if (!app || !applicationDetail) {
      return (
        <div className="rounded-xl border border-[var(--border-hairline)] bg-[var(--bg-rail)] p-4 text-xs text-[var(--ink-secondary)]">
          Complete approval and browser prefill first.
        </div>
      );
    }

    return (
      <HoldAndConfirmPanel
        job={job}
        applicationDetail={applicationDetail}
        approvedResume={activeResume}
        resumeChecksum={tailoredDetail?.sha256_checksum ?? null}
        activeHandoff={activeHandoff}
        busy={actionBusy}
        onRefreshHandoff={props.onRefreshHandoff}
        onAuthorizeManualSubmit={props.onAuthorizeManualSubmit}
      />
    );
  }

  // Step 11: Submission Verification & Tracking
  if (!app || !applicationDetail) {
    return (
      <div className="rounded-xl border border-[var(--border-hairline)] bg-[var(--bg-rail)] p-4 text-xs text-[var(--ink-secondary)]">
        Application has not reached submission tracking yet.
      </div>
    );
  }

  return (
    <Stage11VerificationPanel
      applicationDetail={applicationDetail}
      stage={stage}
      actionBusy={actionBusy}
      onRecordOutcome={props.onRecordOutcome}
      onReconcileConfirmation={props.onReconcileConfirmation}
    />
  );
}
