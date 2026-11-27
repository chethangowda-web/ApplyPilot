"use client";

import React, { useMemo, useState } from "react";
import {
  AlertTriangle,
  Ban,
  Bot,
  CheckCircle2,
  Eye,
  EyeOff,
  FileCheck2,
  HelpCircle,
  Lock,
  ShieldCheck,
  UserCheck,
} from "lucide-react";
import type {
  ApplicationAnswerInput,
  ApplicationAnswerResponse,
  ApplicationDetailResponse,
  ApplicationMode,
  ApplicationPreparationResult,
  JobDetailResponse,
  MappedApplicationAnswer,
  ResumeGenerationHistoryResponse,
  TailoredResumeDetailResponse,
} from "@/lib/types";
import type { StageDerivation } from "@/lib/workflow-engine";
import { StatusBadge } from "@/components/StatusBadge";

export type AnswerProvenanceKind =
  | "verified_profile"
  | "ai_draft"
  | "user_verified"
  | "requires_input";

export function classifyAnswerProvenance(params: {
  evidenceSource: string | null | undefined;
  userVerified: boolean;
  isSensitive: boolean;
  hasValue: boolean;
}): AnswerProvenanceKind {
  const src = (params.evidenceSource || "").trim();
  if (!params.hasValue || !params.userVerified) {
    if (src === "llm_draft_unverified") {
      return "ai_draft";
    }
    if (
      !params.isSensitive &&
      params.userVerified &&
      (src.startsWith("candidate_profiles.") ||
        src.startsWith("resumes.") ||
        src.startsWith("candidate_skills."))
    ) {
      return "verified_profile";
    }
    return "requires_input";
  }

  if (
    src.startsWith("candidate_profiles.") ||
    src.startsWith("resumes.") ||
    src.startsWith("candidate_skills.")
  ) {
    return "verified_profile";
  }
  if (src === "llm_draft_unverified") {
    return "ai_draft";
  }
  return "user_verified";
}

function ProvenanceBadge({ kind }: { kind: AnswerProvenanceKind }) {
  if (kind === "verified_profile") {
    return (
      <span className="inline-flex items-center gap-1 rounded-full border border-[var(--state-completed-border)] bg-[var(--state-completed-bg)] px-2 py-0.5 text-[11px] font-medium text-[var(--state-completed-fg)]">
        <ShieldCheck className="h-3 w-3" aria-hidden="true" />
        <span>Verified Profile Fact</span>
      </span>
    );
  }
  if (kind === "ai_draft") {
    return (
      <span className="inline-flex items-center gap-1 rounded-full border border-[var(--accent-primary-border)] bg-[var(--accent-primary-soft)] px-2 py-0.5 text-[11px] font-medium text-[var(--accent-primary)]">
        <Bot className="h-3 w-3" aria-hidden="true" />
        <span>AI Draft (Verify)</span>
      </span>
    );
  }
  if (kind === "user_verified") {
    return (
      <span className="inline-flex items-center gap-1 rounded-full border border-[var(--state-completed-border)] bg-[var(--state-completed-bg)] px-2 py-0.5 text-[11px] font-medium text-[var(--state-completed-fg)]">
        <UserCheck className="h-3 w-3" aria-hidden="true" />
        <span>User Confirmed</span>
      </span>
    );
  }
  return (
    <span className="inline-flex items-center gap-1 rounded-full border border-[var(--state-waiting-border)] bg-[var(--state-waiting-bg)] px-2 py-0.5 text-[11px] font-medium text-[var(--state-waiting-fg)]">
      <HelpCircle className="h-3 w-3" aria-hidden="true" />
      <span>Requires Your Input</span>
    </span>
  );
}

export function Stage6PreparePanel({
  job,
  activeResume,
  tailoredDetail,
  applicationDetail,
  preparationResult,
  actionBusy,
  fitScore,
  onPrepareApplication,
}: {
  job: JobDetailResponse;
  activeResume: ResumeGenerationHistoryResponse | null;
  tailoredDetail: TailoredResumeDetailResponse | null;
  applicationDetail: ApplicationDetailResponse | null;
  preparationResult: ApplicationPreparationResult | null;
  actionBusy: boolean;
  fitScore: number | null;
  onPrepareApplication: (preferredMode: ApplicationMode | null) => Promise<void>;
}) {
  const [preferredMode, setPreferredMode] = useState<ApplicationMode>("human_approved_browser");
  const app = applicationDetail?.application ?? null;
  const answers = applicationDetail?.answers ?? [];
  const formMapping = preparationResult?.form_mapping ?? null;

  const canPrepare =
    !app &&
    !actionBusy &&
    Boolean(activeResume?.resume_id && activeResume.approval_status === "approved");

  return (
    <div className="space-y-4">
      {!app && fitScore !== null && fitScore < 75 && (
        <div className="rounded-xl border border-[var(--state-waiting-border)] bg-[var(--state-waiting-bg)] px-3.5 py-2.5 text-xs text-[var(--state-waiting-fg)]">
          Current fit score is {Math.round(fitScore)}/100. Backend policy enforces{" "}
          <code className="font-mono">MIN_JOB_MATCH_SCORE</code> (default 75) during application
          preparation.
        </div>
      )}

      <div className="rounded-xl border border-[var(--border-hairline)] bg-[var(--bg-rail)] p-4 space-y-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div>
            <p className="text-sm font-semibold text-[var(--ink-primary)]">
              Policy Router & Form Understanding
            </p>
            <p className="text-xs text-[var(--ink-secondary)]">
              Extracts employer questions, separates required/optional &amp; sensitive fields, and
              creates one canonical Application record without duplicating submissions.
            </p>
          </div>
          {app && <StatusBadge state="completed" customLabel={app.status} mono />}
        </div>

        <dl className="grid gap-3 rounded-lg border border-[var(--border-hairline)] bg-[var(--bg-surface)] p-3 text-xs sm:grid-cols-2">
          <div>
            <dt className="text-[var(--ink-muted)]">Approved Tailored Resume</dt>
            <dd className="mt-0.5 font-medium text-[var(--ink-primary)]">
              {activeResume
                ? `v${activeResume.version_number} (${activeResume.approval_status})`
                : "Not approved yet"}
            </dd>
          </div>
          <div>
            <dt className="text-[var(--ink-muted)]">Employer Destination</dt>
            <dd className="mt-0.5 break-all font-mono text-[11px] text-[var(--ink-secondary)]">
              {app?.destination_url || job.application_url || job.canonical_url}
            </dd>
          </div>
          {tailoredDetail?.sha256_checksum && (
            <div className="sm:col-span-2">
              <dt className="text-[var(--ink-muted)]">Resume SHA-256 Checksum</dt>
              <dd className="mt-0.5 break-all font-mono text-[11px] text-[var(--ink-secondary)]">
                {tailoredDetail.sha256_checksum}
              </dd>
            </div>
          )}
        </dl>

        {!app && (
          <div className="space-y-2.5 pt-1">
            <div className="flex flex-wrap items-center gap-3">
              <label className="text-xs font-medium text-[var(--ink-secondary)]">
                Execution mode:
                <select
                  aria-label="Application execution mode"
                  value={preferredMode}
                  onChange={(e) => setPreferredMode(e.target.value as ApplicationMode)}
                  className="ml-2 rounded-lg border border-[var(--border-hairline)] bg-[var(--bg-surface)] px-2.5 py-1.5 text-xs text-[var(--ink-primary)]"
                >
                  <option value="human_approved_browser">
                    human_approved_browser (Recommended — Supervised prefill + Hold &amp; Confirm)
                  </option>
                  <option value="manual">
                    manual (Self-submit externally, then confirm)
                  </option>
                  <option value="official_api" disabled>
                    official_api — Unavailable (No official ATS adapter registered)
                  </option>
                </select>
              </label>
              <button
                type="button"
                disabled={!canPrepare}
                onClick={() => propsPrepare(preferredMode)}
                className="inline-flex items-center gap-2 rounded-xl bg-[var(--accent-primary)] px-4 py-2 text-xs font-semibold text-white hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-50"
              >
                <span>{actionBusy ? "Preparing Application..." : "Prepare Application"}</span>
              </button>
            </div>
            <p className="text-[11px] text-[var(--ink-muted)]">
              {preferredMode === "manual"
                ? "Manual mode: ApplyPilot prepares your verified answers and approved PDF, and you submit on the employer site yourself."
                : "Human-approved browser mode: ApplyPilot launches a supervised browser prefill and holds final submission until you explicitly confirm."}{" "}
              Official API submission is currently unavailable (registry empty; backend rejects unconfigured sources).
            </p>
          </div>
        )}
      </div>

      {preparationResult && (
        <div className="rounded-xl border border-[var(--border-hairline)] bg-[var(--bg-surface)] p-4 text-xs space-y-2.5">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <span className="font-semibold text-[var(--ink-primary)]">
              Policy Clearance · Daily Quota {preparationResult.policy.daily_used} /{" "}
              {preparationResult.policy.daily_limit}
            </span>
            <StatusBadge
              state={preparationResult.policy.allowed ? "completed" : "blocked"}
              customLabel={preparationResult.policy.allowed ? "ALLOWED" : "BLOCKED"}
              mono
            />
          </div>
          {preparationResult.policy.warnings.length > 0 && (
            <p className="text-[var(--state-waiting-fg)]">
              Warnings: {preparationResult.policy.warnings.join("; ")}
            </p>
          )}
          {formMapping && (
            <div className="grid gap-2 sm:grid-cols-4">
              <div className="rounded-lg bg-[var(--bg-rail)] p-2.5">
                <p className="text-[11px] text-[var(--ink-muted)]">Total Questions</p>
                <p className="mt-0.5 font-mono text-sm font-semibold tabular-nums">
                  {formMapping.questions.length}
                </p>
              </div>
              <div className="rounded-lg bg-[var(--bg-rail)] p-2.5">
                <p className="text-[11px] text-[var(--ink-muted)]">Missing Required</p>
                <p className="mt-0.5 font-mono text-sm font-semibold text-[var(--state-waiting-fg)] tabular-nums">
                  {formMapping.missing_required_keys.length}
                </p>
              </div>
              <div className="rounded-lg bg-[var(--bg-rail)] p-2.5">
                <p className="text-[11px] text-[var(--ink-muted)]">Missing Optional</p>
                <p className="mt-0.5 font-mono text-sm font-semibold tabular-nums">
                  {formMapping.missing_optional_keys.length}
                </p>
              </div>
              <div className="rounded-lg bg-[var(--bg-rail)] p-2.5">
                <p className="text-[11px] text-[var(--ink-muted)]">Sensitive Confirmations</p>
                <p className="mt-0.5 font-mono text-sm font-semibold text-[var(--state-waiting-fg)] tabular-nums">
                  {formMapping.sensitive_keys_requiring_confirmation.length}
                </p>
              </div>
            </div>
          )}
        </div>
      )}

      {app && (
        <dl className="grid gap-3 rounded-xl border border-[var(--border-hairline)] bg-[var(--bg-surface)] p-4 text-xs sm:grid-cols-2">
          <div>
            <dt className="text-[var(--ink-muted)]">Application ID</dt>
            <dd className="mt-0.5 font-mono text-[var(--ink-primary)]">{app.id}</dd>
          </div>
          <div>
            <dt className="text-[var(--ink-muted)]">Execution Mode &amp; Persisted Answers</dt>
            <dd className="mt-0.5 font-mono text-[var(--ink-primary)]">
              {app.application_mode} · {answers.length} saved answer(s)
            </dd>
          </div>
          <div className="sm:col-span-2">
            <dt className="text-[var(--ink-muted)]">Idempotency Key (Duplicate Guard)</dt>
            <dd className="mt-0.5 break-all font-mono text-[var(--ink-secondary)]">
              {app.idempotency_key}
            </dd>
          </div>
        </dl>
      )}
    </div>
  );

  function propsPrepare(mode: ApplicationMode) {
    return onPrepareApplication(mode);
  }
}

interface UnifiedQuestionRow {
  questionKey: string;
  question: string;
  fieldType: string;
  required: boolean;
  isSensitive: boolean;
  sensitiveCategory: string | null;
  answerPreview: string | null;
  evidenceSource: string | null;
  userVerified: boolean;
  provenance: AnswerProvenanceKind;
  reason: string | null;
}

export function Stage7QuestionsPanel({
  applicationDetail,
  preparationResult,
  actionBusy,
  onSubmitAnswers,
}: {
  applicationDetail: ApplicationDetailResponse | null;
  preparationResult: ApplicationPreparationResult | null;
  actionBusy: boolean;
  onSubmitAnswers: (items: ApplicationAnswerInput[]) => Promise<void>;
}) {
  const [answerDrafts, setAnswerDrafts] = useState<Record<string, string>>({});
  const [revealInputs, setRevealInputs] = useState<Record<string, boolean>>({});

  const app = applicationDetail?.application ?? null;
  const savedAnswers = useMemo(
    () => applicationDetail?.answers ?? [],
    [applicationDetail?.answers],
  );
  const mappedAnswers = useMemo(
    () => preparationResult?.form_mapping.mapped_answers ?? [],
    [preparationResult?.form_mapping.mapped_answers],
  );

  const unifiedRows = useMemo<UnifiedQuestionRow[]>(() => {
    const mappedByKey = new Map<string, MappedApplicationAnswer>(
      mappedAnswers.map((m) => [m.question_key, m]),
    );
    const savedByKey = new Map<string, ApplicationAnswerResponse>(
      savedAnswers.map((s) => [s.question_key, s]),
    );

    const rows: UnifiedQuestionRow[] = [];
    for (const ans of savedAnswers) {
      const meta = mappedByKey.get(ans.question_key);
      rows.push({
        questionKey: ans.question_key,
        question: ans.question,
        fieldType: meta?.field_type ?? "text",
        required: meta?.required ?? true,
        isSensitive: ans.is_sensitive,
        sensitiveCategory: meta?.sensitive_category ?? null,
        answerPreview: ans.answer_preview,
        evidenceSource: ans.evidence_source,
        userVerified: ans.user_verified,
        provenance: classifyAnswerProvenance({
          evidenceSource: ans.evidence_source,
          userVerified: ans.user_verified,
          isSensitive: ans.is_sensitive,
          hasValue: Boolean(ans.answer_preview),
        }),
        reason: meta?.reason ?? null,
      });
    }

    for (const m of mappedAnswers) {
      if (savedByKey.has(m.question_key)) continue;
      rows.push({
        questionKey: m.question_key,
        question: m.question,
        fieldType: m.field_type,
        required: m.required,
        isSensitive: m.is_sensitive,
        sensitiveCategory: m.sensitive_category,
        answerPreview: m.proposed_answer,
        evidenceSource: m.evidence_source,
        userVerified: m.verification_state === "verified",
        provenance: classifyAnswerProvenance({
          evidenceSource: m.evidence_source,
          userVerified: m.verification_state === "verified",
          isSensitive: m.is_sensitive,
          hasValue: Boolean(m.proposed_answer),
        }),
        reason: m.reason,
      });
    }

    return rows;
  }, [mappedAnswers, savedAnswers]);

  if (!app) {
    return (
      <div className="rounded-xl border border-[var(--border-hairline)] bg-[var(--bg-rail)] p-4 text-xs text-[var(--ink-secondary)]">
        Prepare the application in Step 6 first.
      </div>
    );
  }

  const editableStatus = app.status === "NEEDS_USER_INPUT" || app.status === "AWAITING_APPROVAL";

  const counts = {
    verifiedProfile: unifiedRows.filter((r) => r.provenance === "verified_profile").length,
    aiDraft: unifiedRows.filter((r) => r.provenance === "ai_draft").length,
    userConfirmed: unifiedRows.filter((r) => r.provenance === "user_verified").length,
    requiresInput: unifiedRows.filter((r) => r.provenance === "requires_input").length,
  };

  async function handleSaveAll(e: React.FormEvent) {
    e.preventDefault();
    const payload: ApplicationAnswerInput[] = [];
    for (const row of unifiedRows) {
      const draft = answerDrafts[row.questionKey];
      if (draft && draft.trim()) {
        payload.push({
          question_key: row.questionKey,
          question: row.question,
          answer: draft.trim(),
          is_sensitive: row.isSensitive,
          evidence_source:
            row.evidenceSource === "llm_draft_unverified"
              ? "llm_draft_user_confirmed"
              : row.evidenceSource || "user_input",
        });
      }
    }
    if (payload.length > 0) {
      await onSubmitAnswers(payload);
      setAnswerDrafts({});
    }
  }

  return (
    <form onSubmit={handleSaveAll} className="space-y-4">
      <div className="rounded-xl border border-[var(--border-hairline)] bg-[var(--bg-rail)] p-4 text-xs space-y-2">
        <p className="text-[var(--ink-secondary)]">
          ApplyPilot never fabricates answers or auto-confirms sensitive questions. Every field
          below shows whether it came from verified profile evidence, an unverified AI draft, or
          still requires your input.
        </p>
        <div className="flex flex-wrap gap-2 pt-1 tabular-nums">
          <span className="rounded-md bg-[var(--state-completed-bg)] px-2 py-0.5 font-medium text-[var(--state-completed-fg)]">
            Verified Profile: {counts.verifiedProfile}
          </span>
          <span className="rounded-md bg-[var(--accent-primary-soft)] px-2 py-0.5 font-medium text-[var(--accent-primary)]">
            AI Drafts: {counts.aiDraft}
          </span>
          <span className="rounded-md bg-[var(--state-completed-bg)] px-2 py-0.5 font-medium text-[var(--state-completed-fg)]">
            User Confirmed: {counts.userConfirmed}
          </span>
          <span className="rounded-md bg-[var(--state-waiting-bg)] px-2 py-0.5 font-medium text-[var(--state-waiting-fg)]">
            Needs Input: {counts.requiresInput}
          </span>
        </div>
      </div>

      {unifiedRows.length === 0 ? (
        <div className="rounded-xl border border-[var(--border-hairline)] bg-[var(--bg-surface)] p-4 text-xs text-[var(--ink-secondary)]">
          No extra form questions were extracted beyond your approved tailored resume.
        </div>
      ) : (
        <div className="space-y-3">
          {unifiedRows.map((row) => {
            const draftValue =
              answerDrafts[row.questionKey] ??
              (!row.isSensitive && row.provenance === "ai_draft" ? row.answerPreview ?? "" : "");
            const revealed = Boolean(revealInputs[row.questionKey]);

            return (
              <div
                key={row.questionKey}
                className="rounded-xl border border-[var(--border-hairline)] bg-[var(--bg-surface)] p-4 space-y-2.5"
              >
                <div className="flex flex-wrap items-start justify-between gap-2">
                  <div>
                    <label
                      htmlFor={`ans-${row.questionKey}`}
                      className="text-xs font-semibold text-[var(--ink-primary)]"
                    >
                      {row.question}{" "}
                      <span className="font-mono font-normal text-[var(--ink-muted)]">
                        ({row.questionKey} · {row.required ? "required" : "optional"})
                      </span>
                    </label>
                    {row.reason && (
                      <p className="mt-0.5 text-[11px] text-[var(--ink-muted)]">{row.reason}</p>
                    )}
                  </div>

                  <div className="flex flex-wrap items-center gap-1.5">
                    <ProvenanceBadge kind={row.provenance} />
                    {row.isSensitive && (
                      <StatusBadge
                        state="waiting"
                        customLabel={`AES-256-GCM${row.sensitiveCategory ? ` · ${row.sensitiveCategory}` : ""}`}
                        mono
                      />
                    )}
                  </div>
                </div>

                <p className="font-mono text-[11px] text-[var(--ink-muted)]">
                  Server record: {row.answerPreview ?? "[MISSING — NO VALUE SAVED]"}
                  {row.evidenceSource ? ` · source: ${row.evidenceSource}` : ""}
                </p>

                {editableStatus && (
                  <div className="flex items-center gap-2">
                    <input
                      id={`ans-${row.questionKey}`}
                      type={row.isSensitive && !revealed ? "password" : "text"}
                      value={draftValue}
                      onChange={(e) =>
                        setAnswerDrafts((prev) => ({
                          ...prev,
                          [row.questionKey]: e.target.value,
                        }))
                      }
                      placeholder={
                        row.isSensitive
                          ? "Enter sensitive answer to encrypt & explicitly verify..."
                          : "Enter or edit verified answer..."
                      }
                      className="flex-1 rounded-lg border border-[var(--border-hairline)] bg-[var(--bg-rail)] px-3 py-2 text-xs text-[var(--ink-primary)]"
                    />
                    {row.isSensitive && (
                      <button
                        type="button"
                        aria-label={revealed ? "Hide sensitive input" : "Show sensitive input"}
                        onClick={() =>
                          setRevealInputs((prev) => ({
                            ...prev,
                            [row.questionKey]: !prev[row.questionKey],
                          }))
                        }
                        className="rounded-lg border border-[var(--border-hairline)] p-2 text-[var(--ink-secondary)] hover:bg-[var(--bg-rail)]"
                      >
                        {revealed ? (
                          <EyeOff className="h-3.5 w-3.5" aria-hidden="true" />
                        ) : (
                          <Eye className="h-3.5 w-3.5" aria-hidden="true" />
                        )}
                      </button>
                    )}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}

      {editableStatus && unifiedRows.length > 0 && (
        <button
          type="submit"
          disabled={actionBusy || Object.values(answerDrafts).every((v) => !v?.trim())}
          className="inline-flex items-center gap-2 rounded-xl bg-[var(--accent-primary)] px-4 py-2 text-xs font-semibold text-white hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-50"
        >
          <Lock className="h-3.5 w-3.5" aria-hidden="true" />
          <span>{actionBusy ? "Saving & Encrypting..." : "Save & Verify Answers"}</span>
        </button>
      )}
    </form>
  );
}

export function Stage8ApprovalPanel({
  job,
  stage,
  activeResume,
  tailoredDetail,
  applicationDetail,
  preparationResult,
  actionBusy,
  onApproveApplication,
  onRejectApplication,
}: {
  job: JobDetailResponse;
  stage: StageDerivation;
  activeResume: ResumeGenerationHistoryResponse | null;
  tailoredDetail: TailoredResumeDetailResponse | null;
  applicationDetail: ApplicationDetailResponse | null;
  preparationResult: ApplicationPreparationResult | null;
  actionBusy: boolean;
  onApproveApplication: (notes: string) => Promise<void>;
  onRejectApplication: (notes: string) => Promise<void>;
}) {
  const [decisionNotes, setDecisionNotes] = useState("");
  const app = applicationDetail?.application ?? null;

  if (!app || !applicationDetail) {
    return (
      <div className="rounded-xl border border-[var(--border-hairline)] bg-[var(--bg-rail)] p-4 text-xs text-[var(--ink-secondary)]">
        Prepare and verify application questions in Steps 6–7 first.
      </div>
    );
  }

  const answers = applicationDetail.answers;
  const latestRec = applicationDetail.approval_records[0] ?? null;
  const canDecide = (app.status === "AWAITING_APPROVAL" || app.status === "FAILED") && !actionBusy;
  const missingOptional = preparationResult?.form_mapping.missing_optional_keys ?? [];
  const unverifiedCount = answers.filter((a) => !a.user_verified).length;
  const sensitiveCount = answers.filter((a) => a.is_sensitive).length;

  return (
    <div className="space-y-4">
      {/* Binding Summary */}
      <div className="rounded-xl border border-[var(--border-hairline)] bg-[var(--bg-rail)] p-4 text-xs space-y-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <span className="text-sm font-semibold text-[var(--ink-primary)]">
            Complete Application Review &amp; Snapshot Binding
          </span>
          <StatusBadge state={stage.state} customLabel={app.status} mono />
        </div>

        <dl className="grid gap-3 rounded-lg border border-[var(--border-hairline)] bg-[var(--bg-surface)] p-3 sm:grid-cols-2">
          <div>
            <dt className="text-[var(--ink-muted)]">Target Job &amp; Destination</dt>
            <dd className="mt-0.5 font-medium text-[var(--ink-primary)]">
              {job.company} — {job.role_title}
            </dd>
            <dd className="mt-0.5 break-all font-mono text-[11px] text-[var(--ink-secondary)]">
              {app.destination_url}
            </dd>
          </div>

          <div>
            <dt className="text-[var(--ink-muted)]">Selected Approved Resume</dt>
            <dd className="mt-0.5 flex items-center gap-1.5 font-medium text-[var(--ink-primary)]">
              <FileCheck2 className="h-3.5 w-3.5 text-[var(--state-completed-fg)]" aria-hidden="true" />
              <span>
                {activeResume
                  ? `Tailored PDF v${activeResume.version_number}`
                  : `Resume ${app.resume_id.slice(0, 8)}`}
              </span>
            </dd>
            <dd className="mt-0.5 break-all font-mono text-[11px] text-[var(--ink-secondary)]">
              SHA-256: {tailoredDetail?.sha256_checksum ?? app.resume_id}
            </dd>
          </div>

          <div>
            <dt className="text-[var(--ink-muted)]">Execution Mode</dt>
            <dd className="mt-0.5 font-mono text-[var(--ink-primary)]">{app.application_mode}</dd>
          </div>
          <div>
            <dt className="text-[var(--ink-muted)]">Answer Verification Summary</dt>
            <dd className="mt-0.5 text-[var(--ink-primary)] tabular-nums">
              {answers.length} saved · {sensitiveCount} sensitive encrypted · {unverifiedCount}{" "}
              unverified
            </dd>
          </div>
        </dl>
      </div>

      {/* Answer Summary Table */}
      <div className="rounded-xl border border-[var(--border-hairline)] bg-[var(--bg-surface)] p-4 text-xs space-y-2.5">
        <p className="font-semibold text-[var(--ink-primary)]">
          Application Answers Bound to Approval Snapshot ({answers.length})
        </p>
        {answers.length === 0 ? (
          <p className="text-[var(--ink-muted)]">
            No extra questionnaire fields attached beyond your approved tailored resume.
          </p>
        ) : (
          <ul className="divide-y divide-[var(--border-hairline)]">
            {answers.map((ans) => {
              const kind = classifyAnswerProvenance({
                evidenceSource: ans.evidence_source,
                userVerified: ans.user_verified,
                isSensitive: ans.is_sensitive,
                hasValue: Boolean(ans.answer_preview),
              });
              return (
                <li key={ans.id} className="flex flex-wrap items-center justify-between gap-2 py-2">
                  <div className="min-w-0">
                    <p className="font-medium text-[var(--ink-primary)]">{ans.question}</p>
                    <p className="font-mono text-[11px] text-[var(--ink-muted)]">
                      {ans.answer_preview ?? "[EMPTY]"}
                    </p>
                  </div>
                  <ProvenanceBadge kind={kind} />
                </li>
              );
            })}
          </ul>
        )}
      </div>

      {missingOptional.length > 0 && (
        <div className="flex items-start gap-2 rounded-xl border border-[var(--state-waiting-border)] bg-[var(--state-waiting-bg)] p-3 text-xs text-[var(--state-waiting-fg)]">
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" />
          <span>
            Optional fields left unanswered: <code className="font-mono">{missingOptional.join(", ")}</code>
          </span>
        </div>
      )}

      {latestRec && (
        <div className="rounded-xl border border-[var(--border-hairline)] bg-[var(--bg-surface)] p-4 text-xs space-y-1">
          <p className="font-semibold text-[var(--ink-primary)]">
            Immutable ApprovalRecord: {latestRec.decision.toUpperCase()}
          </p>
          <p className="break-all font-mono text-[var(--ink-secondary)]">
            approval_snapshot_hash: {latestRec.approval_snapshot_hash}
          </p>
          <p className="text-[var(--ink-muted)]">
            Decided: {new Date(latestRec.decided_at).toLocaleString()} · Expires:{" "}
            {new Date(latestRec.expires_at).toLocaleString()}
          </p>
        </div>
      )}

      {(app.status === "AWAITING_APPROVAL" || app.status === "FAILED") && (
        <div className="rounded-xl border border-[var(--state-waiting-border)] bg-[var(--state-waiting-bg)] p-4 space-y-3">
          <p className="text-xs font-semibold text-[var(--state-waiting-fg)]">
            Explicit Human Approval Required (Bound to Exact Resume, Answers, and Destination)
          </p>
          <input
            type="text"
            value={decisionNotes}
            onChange={(e) => setDecisionNotes(e.target.value)}
            placeholder="Optional approval or rejection notes..."
            className="w-full rounded-lg border border-[var(--border-hairline)] bg-[var(--bg-surface)] px-3 py-2 text-xs text-[var(--ink-primary)]"
          />
          <div className="flex flex-wrap gap-2">
            <button
              type="button"
              disabled={!canDecide}
              onClick={() => onApproveApplication(decisionNotes)}
              className="inline-flex items-center gap-1.5 rounded-xl bg-[var(--state-completed-fg)] px-4 py-2 text-xs font-semibold text-white hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-50"
            >
              <CheckCircle2 className="h-4 w-4" aria-hidden="true" />
              <span>{actionBusy ? "Recording Approval..." : "Approve Application Snapshot"}</span>
            </button>
            {app.status === "AWAITING_APPROVAL" && (
              <button
                type="button"
                disabled={!canDecide}
                onClick={() => onRejectApplication(decisionNotes)}
                className="inline-flex items-center gap-1.5 rounded-xl border border-[var(--state-blocked-border)] bg-[var(--bg-surface)] px-4 py-2 text-xs font-semibold text-[var(--state-blocked-fg)] hover:bg-[var(--state-blocked-bg)] disabled:cursor-not-allowed disabled:opacity-50"
              >
                <Ban className="h-4 w-4" aria-hidden="true" />
                <span>Reject Application</span>
              </button>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
