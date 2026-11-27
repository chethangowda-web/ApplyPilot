"use client";

import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import {
  AlertTriangle,
  ArrowLeft,
  ArrowRight,
  Ban,
  Clock,
  PauseCircle,
  RefreshCw,
} from "lucide-react";
import { ApiError, apiClient } from "@/lib/api-client";
import type {
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
import { deriveJobWorkflowState } from "@/lib/workflow-engine";
import { StatusBadge } from "@/components/StatusBadge";
import { WorkflowStepper } from "@/components/WorkflowStepper";
import { WorkflowStageBody } from "@/components/WorkflowStageBody";

export interface WorkflowWorkspaceProps {
  jobId: string;
}

export function WorkflowWorkspace({ jobId }: WorkflowWorkspaceProps) {
  const router = useRouter();
  const routerRef = useRef(router);
  routerRef.current = router;
  const searchParams = useSearchParams();

  const [loading, setLoading] = useState(true);
  const [actionBusy, setActionBusy] = useState(false);
  const [errorBanner, setErrorBanner] = useState<string | null>(null);
  const [noticeBanner, setNoticeBanner] = useState<string | null>(null);

  const [job, setJob] = useState<JobDetailResponse | null>(null);
  const [intelligence, setIntelligence] = useState<JobIntelligenceResponse | null>(null);
  const [evidence, setEvidence] = useState<JobEvidenceResponse | null>(null);
  const [resumeHistory, setResumeHistory] = useState<JobResumeHistoryResponse | null>(null);
  const [tailoredDetail, setTailoredDetail] = useState<TailoredResumeDetailResponse | null>(null);
  const [tailoringDiff, setTailoringDiff] = useState<TailoringDiffResponse | null>(null);
  const [validationReport, setValidationReport] = useState<FactualValidationReportResponse | null>(
    null,
  );
  const [applicationDetail, setApplicationDetail] = useState<ApplicationDetailResponse | null>(null);
  const [masterResumes, setMasterResumes] = useState<ResumeMetadataResponse[]>([]);
  const [preparationResult, setPreparationResult] = useState<ApplicationPreparationResult | null>(
    null,
  );
  const [executionResult, setExecutionResult] = useState<ExecutionResult | null>(null);

  const loadWorkflow = useCallback(async () => {
    setErrorBanner(null);
    try {
      const [jobResp, appsResp, histResp, resumeList] = await Promise.all([
        apiClient.getJobDetail(jobId),
        apiClient.listApplications(),
        apiClient.getJobResumeHistory(jobId).catch(() => ({
          job_id: jobId,
          versions: [],
          outbox_jobs: [],
        })),
        apiClient.listResumeVersions().catch(() => []),
      ]);
      setJob(jobResp);
      setResumeHistory(histResp);
      setMasterResumes(resumeList.filter((r) => r.is_master));

      if (jobResp.requirements || jobResp.match) {
        const [intelResp, evResp] = await Promise.all([
          apiClient.getJobIntelligence(jobId).catch(() => null),
          apiClient.getJobEvidence(jobId).catch(() => null),
        ]);
        setIntelligence(intelResp);
        setEvidence(evResp);
      } else {
        setIntelligence(null);
        setEvidence(null);
      }

      const activeVersion =
        [...histResp.versions]
          .sort((a, b) => b.version_number - a.version_number)
          .find((v) => v.is_active && v.status === "completed" && v.resume_id) ??
        histResp.versions.find((v) => v.status === "completed" && v.resume_id) ??
        null;

      if (activeVersion?.resume_id) {
        const [detailResp, diffResp, valResp] = await Promise.all([
          apiClient.getTailoredResumeDetail(activeVersion.resume_id).catch(() => null),
          apiClient.getTailoringDiff(activeVersion.resume_id).catch(() => null),
          apiClient.getResumeValidation(activeVersion.resume_id).catch(() => null),
        ]);
        setTailoredDetail(detailResp);
        setTailoringDiff(diffResp);
        setValidationReport(valResp);
      } else {
        setTailoredDetail(null);
        setTailoringDiff(null);
        setValidationReport(null);
      }

      const matchingApp = appsResp.find((a) => a.job_id === jobId) ?? null;
      if (matchingApp) {
        const appDet = await apiClient.getApplicationDetail(matchingApp.id);
        setApplicationDetail(appDet);
      } else {
        setApplicationDetail(null);
      }
    } catch (err) {
      if (err instanceof ApiError && err.isUnauthorized()) {
        routerRef.current.push(`/login?next=${encodeURIComponent(`/workflows/${jobId}`)}`);
        return;
      }
      setErrorBanner(err instanceof Error ? err.message : "Failed to load workflow state.");
    } finally {
      setLoading(false);
    }
  }, [jobId]);

  useEffect(() => {
    setLoading(true);
    setPreparationResult(null);
    setExecutionResult(null);
    setNoticeBanner(null);
    void loadWorkflow();
  }, [loadWorkflow]);

  const derived = useMemo(() => {
    if (!job) return null;
    return deriveJobWorkflowState({
      job,
      match: intelligence?.match ?? job.match,
      resumeHistory,
      applicationDetail,
    });
  }, [job, intelligence, resumeHistory, applicationDetail]);

  // Poll automatically when outbox resume generation is queued or running
  useEffect(() => {
    const outbox = resumeHistory?.outbox_jobs ?? [];
    const activeJob = outbox.some((j) => j.status === "queued" || j.status === "running");
    if (!activeJob) return;
    const id = setInterval(() => {
      void loadWorkflow();
    }, 2500);
    return () => clearInterval(id);
  }, [resumeHistory, loadWorkflow]);

  const requestedStepParam = Number(searchParams.get("step") || "0");
  const viewStep = useMemo(() => {
    if (!derived) return 1;
    if (
      Number.isInteger(requestedStepParam) &&
      requestedStepParam >= 1 &&
      requestedStepParam <= derived.stages.length
    ) {
      const target = derived.stages[requestedStepParam - 1];
      if (target.canInspect || target.state !== "upcoming") {
        return requestedStepParam;
      }
    }
    return derived.authoritativeStep;
  }, [derived, requestedStepParam]);

  function navigateToStep(stepNumber: number) {
    setNoticeBanner(null);
    router.replace(`/workflows/${jobId}?step=${stepNumber}`, { scroll: false });
  }

  async function runAction(fn: () => Promise<void>, successMessage?: string) {
    setActionBusy(true);
    setErrorBanner(null);
    setNoticeBanner(null);
    try {
      await fn();
      await loadWorkflow();
      if (successMessage) {
        setNoticeBanner(successMessage);
      }
    } catch (err) {
      setErrorBanner(err instanceof Error ? err.message : "Workflow action failed.");
    } finally {
      setActionBusy(false);
    }
  }

  if (loading) {
    return (
      <div className="rounded-2xl border border-[var(--border-hairline)] bg-[var(--bg-surface)] p-8 text-sm text-[var(--ink-secondary)]">
        Loading workflow state directly from ApplyPilot backend...
      </div>
    );
  }

  if (!job || !derived) {
    return (
      <div className="space-y-4 rounded-2xl border border-[var(--state-failed-border)] bg-[var(--state-failed-bg)] p-6 text-sm text-[var(--state-failed-fg)]">
        <p className="font-semibold">Unable to load job workflow</p>
        <p className="text-xs">{errorBanner || "Job record could not be found."}</p>
        <Link href="/" className="inline-flex items-center gap-1.5 text-xs font-semibold underline">
          <ArrowLeft className="h-3.5 w-3.5" aria-hidden="true" />
          <span>Back to My Workflows</span>
        </Link>
      </div>
    );
  }

  const currentStage = derived.stages[viewStep - 1];
  const nextStage = viewStep < derived.stages.length ? derived.stages[viewStep] : null;
  const canMovePrevious = viewStep > 1;
  const canContinue =
    currentStage.state === "completed" && nextStage !== null && nextStage.canInspect;
  const isAtAuthoritativeStep = viewStep === derived.authoritativeStep;

  return (
    <div className="space-y-5">
      {/* Workflow Header */}
      <div className="flex flex-wrap items-start justify-between gap-4 rounded-2xl border border-[var(--border-hairline)] bg-[var(--bg-surface)] p-5">
        <div className="space-y-1">
          <div className="flex flex-wrap items-center gap-2 text-xs text-[var(--ink-muted)]">
            <Link href="/" className="inline-flex items-center gap-1 hover:text-[var(--ink-primary)]">
              <ArrowLeft className="h-3.5 w-3.5" aria-hidden="true" />
              <span>My Workflows</span>
            </Link>
            <span>/</span>
            <span className="font-mono">Step {viewStep} of {derived.stages.length}</span>
          </div>
          <h1 className="text-xl font-semibold tracking-tight text-[var(--ink-primary)]">
            {job.company} — {job.role_title}
          </h1>
          <p className="text-xs text-[var(--ink-secondary)]">
            {job.location || "Remote / Unspecified"} · Last saved activity:{" "}
            {new Date(derived.lastActivityAt).toLocaleString()}
          </p>
        </div>

        <div className="flex flex-wrap items-center gap-2">
          <StatusBadge state={derived.overallState} customLabel={derived.backendStatusLabel} mono />
          <button
            type="button"
            disabled={actionBusy}
            onClick={() => void runAction(async () => {})}
            className="inline-flex items-center gap-1.5 rounded-lg border border-[var(--border-hairline)] bg-[var(--bg-rail)] px-3 py-1.5 text-xs font-medium text-[var(--ink-secondary)] hover:border-[var(--border-strong)]"
          >
            <RefreshCw className="h-3.5 w-3.5" aria-hidden="true" />
            <span>Sync State</span>
          </button>
          <Link
            href="/"
            className="inline-flex items-center gap-1.5 rounded-lg border border-[var(--border-hairline)] bg-[var(--bg-surface)] px-3 py-1.5 text-xs font-medium text-[var(--ink-primary)] hover:bg-[var(--bg-rail)]"
          >
            <PauseCircle className="h-3.5 w-3.5" aria-hidden="true" />
            <span>Save & Return Later</span>
          </Link>
        </div>
      </div>

      {errorBanner && (
        <div
          role="alert"
          className="flex items-start gap-2.5 rounded-xl border border-[var(--state-failed-border)] bg-[var(--state-failed-bg)] p-4 text-xs text-[var(--state-failed-fg)]"
        >
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" />
          <div>
            <p className="font-semibold">Backend Request Error</p>
            <p className="mt-0.5">{errorBanner}</p>
          </div>
        </div>
      )}

      {noticeBanner && (
        <div
          role="status"
          className="rounded-xl border border-[var(--state-completed-border)] bg-[var(--state-completed-bg)] p-3.5 text-xs font-medium text-[var(--state-completed-fg)]"
        >
          {noticeBanner}
        </div>
      )}

      {/* Main Grid: Center Active Stage Workspace + Right Persistent 11-Stage Stepper */}
      <div className="grid gap-6 lg:grid-cols-12 lg:items-start">
        <section
          aria-labelledby="active-stage-heading"
          className="lg:col-span-8 lg:order-1 rounded-2xl border border-[var(--border-hairline)] bg-[var(--bg-surface)] p-5 sm:p-6 space-y-5"
        >
          <div className="flex flex-wrap items-start justify-between gap-3 border-b border-[var(--border-hairline)] pb-4">
            <div>
              <div className="flex items-center gap-2">
                <span className="font-mono text-xs font-semibold text-[var(--ink-muted)]">
                  STAGE {currentStage.definition.stepNumber} OF {derived.stages.length}
                </span>
                <StatusBadge state={currentStage.state} />
                {!isAtAuthoritativeStep && (
                  <button
                    type="button"
                    onClick={() => navigateToStep(derived.authoritativeStep)}
                    className="rounded-md bg-[var(--accent-primary-soft)] px-2 py-0.5 text-[11px] font-semibold text-[var(--accent-primary)] hover:underline"
                  >
                    Jump to Active Step {derived.authoritativeStep}
                  </button>
                )}
              </div>
              <h2
                id="active-stage-heading"
                className="mt-1 text-lg font-semibold text-[var(--ink-primary)]"
              >
                {currentStage.definition.title}
              </h2>
              <p className="mt-1 text-sm text-[var(--ink-secondary)]">
                {currentStage.definition.description}
              </p>
            </div>
          </div>

          {/* Exceptional / Blocker Callout when not completed */}
          {currentStage.state === "blocked" && currentStage.blockerReason && (
            <div className="flex items-start gap-2.5 rounded-xl border border-[var(--state-blocked-border)] bg-[var(--state-blocked-bg)] p-4 text-xs text-[var(--state-blocked-fg)]">
              <Ban className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" />
              <div>
                <p className="font-semibold">Workflow Blocked by Backend Rule</p>
                <p className="mt-0.5">{currentStage.blockerReason}</p>
              </div>
            </div>
          )}

          {currentStage.state === "failed" && currentStage.blockerReason && (
            <div className="flex items-start gap-2.5 rounded-xl border border-[var(--state-failed-border)] bg-[var(--state-failed-bg)] p-4 text-xs text-[var(--state-failed-fg)]">
              <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" />
              <div>
                <p className="font-semibold">Backend Exceptional / Failed State</p>
                <p className="mt-0.5">{currentStage.blockerReason}</p>
              </div>
            </div>
          )}

          {currentStage.state === "waiting" && currentStage.blockerReason && (
            <div className="flex items-start gap-2.5 rounded-xl border border-[var(--state-waiting-border)] bg-[var(--state-waiting-bg)] p-4 text-xs text-[var(--state-waiting-fg)]">
              <Clock className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" />
              <div>
                <p className="font-semibold">Waiting for Verification or Human Action</p>
                <p className="mt-0.5">{currentStage.blockerReason}</p>
              </div>
            </div>
          )}

          <WorkflowStageBody
            viewStep={viewStep}
            stage={currentStage}
            derived={derived}
            job={job}
            intelligence={intelligence}
            evidence={evidence}
            resumeHistory={resumeHistory}
            masterResumes={masterResumes}
            tailoredDetail={tailoredDetail}
            tailoringDiff={tailoringDiff}
            validationReport={validationReport}
            applicationDetail={applicationDetail}
            preparationResult={preparationResult}
            executionResult={executionResult}
            actionBusy={actionBusy}
            onAnalyzeJob={(useLlm) =>
              runAction(
                async () => {
                  const m = await apiClient.analyzeJob(jobId, useLlm);
                  setIntelligence((prev) => (prev ? { ...prev, match: m } : prev));
                },
                "Job description analyzed and saved to backend.",
              )
            }
            onTailorResume={(forceRegenerate) =>
              runAction(
                async () => {
                  await apiClient.tailorResumeForJob(jobId, {
                    useLlm: true,
                    forceRegenerate,
                  });
                },
                "Tailoring job queued in transactional outbox.",
              )
            }
            onApproveResume={(resumeId, notes) =>
              runAction(
                async () => {
                  await apiClient.approveTailoredResume(resumeId, notes);
                },
                "Tailored resume explicitly approved by owner.",
              )
            }
            onRejectResume={(resumeId, notes) =>
              runAction(
                async () => {
                  await apiClient.rejectTailoredResume(resumeId, notes);
                },
                "Tailored resume rejected.",
              )
            }
            onPrepareApplication={(mode: ApplicationMode | null) =>
              runAction(
                async () => {
                  if (!derived.activeResumeVersion?.resume_id) {
                    throw new Error("Approved tailored resume ID is missing.");
                  }
                  const res = await apiClient.prepareApplication({
                    job_id: jobId,
                    resume_id: derived.activeResumeVersion.resume_id,
                    preferred_mode: mode,
                  });
                  setPreparationResult(res);
                },
                "Application prepared and policy checked.",
              )
            }
            onSubmitAnswers={(items) =>
              runAction(
                async () => {
                  if (!applicationDetail?.application.id) return;
                  await apiClient.submitApplicationAnswers(applicationDetail.application.id, items);
                },
                "Verified application answers saved (sensitive fields encrypted).",
              )
            }
            onApproveApplication={(notes) =>
              runAction(
                async () => {
                  if (!applicationDetail?.application.id) return;
                  await apiClient.approveApplication(applicationDetail.application.id, notes);
                },
                "Immutable application approval snapshot created.",
              )
            }
            onRejectApplication={(notes) =>
              runAction(
                async () => {
                  if (!applicationDetail?.application.id) return;
                  await apiClient.rejectApplication(applicationDetail.application.id, notes);
                },
                "Application rejected.",
              )
            }
            onExecuteApplication={(browserMode) =>
              runAction(
                async () => {
                  if (!applicationDetail?.application.id) return;
                  const res = await apiClient.executeApplication(applicationDetail.application.id, {
                    browser_mode: browserMode,
                    capture_artifacts: true,
                  });
                  setExecutionResult(res);
                },
                "Supervised browser handoff opened without auto-submitting.",
              )
            }
            onRefreshHandoff={(handoffSessionId) =>
              runAction(async () => {
                if (!applicationDetail?.application.id) return;
                await apiClient.getHandoffSession(
                  applicationDetail.application.id,
                  handoffSessionId,
                );
              })
            }
            onCloseHandoff={(handoffSessionId) =>
              runAction(
                async () => {
                  if (!applicationDetail?.application.id) return;
                  await apiClient.closeHandoffSession(
                    applicationDetail.application.id,
                    handoffSessionId,
                  );
                },
                "Browser handoff session closed.",
              )
            }
            onAuthorizeManualSubmit={(payload) =>
              runAction(
                async () => {
                  if (!applicationDetail?.application.id) return;
                  await apiClient.authorizeManualSubmit(applicationDetail.application.id, payload);
                },
                "Held submission released via explicit single-use confirmation.",
              )
            }
            onRecordOutcome={(outcome, notes) =>
              runAction(
                async () => {
                  if (!applicationDetail?.application.id) return;
                  await apiClient.recordSubmitOutcome(applicationDetail.application.id, {
                    outcome,
                    notes: notes || null,
                  });
                },
                "Submission outcome recorded.",
              )
            }
            onReconcileConfirmation={(receiptId, verificationSource) =>
              runAction(
                async () => {
                  if (!applicationDetail?.application.id) return;
                  await apiClient.reconcileApplication(applicationDetail.application.id, {
                    verification_source: verificationSource,
                    external_ref_id: receiptId,
                    confirmed_at: new Date().toISOString(),
                  });
                },
                "Confirmation receipt reconciled cleanly.",
              )
            }
          />

          {/* Step Navigation Footer Gated Strictly by Backend State */}
          <div className="flex flex-wrap items-center justify-between gap-3 border-t border-[var(--border-hairline)] pt-4">
            <button
              type="button"
              disabled={!canMovePrevious || actionBusy}
              onClick={() => navigateToStep(viewStep - 1)}
              className="inline-flex items-center gap-1.5 rounded-xl border border-[var(--border-strong)] bg-[var(--bg-surface)] px-4 py-2 text-xs font-semibold text-[var(--ink-primary)] hover:bg-[var(--bg-rail)] disabled:cursor-not-allowed disabled:opacity-45"
            >
              <ArrowLeft className="h-3.5 w-3.5" aria-hidden="true" />
              <span>Previous Step</span>
            </button>

            <div className="flex flex-wrap items-center gap-3">
              {!canContinue && nextStage && (
                <span className="max-w-sm text-right text-xs text-[var(--ink-muted)]">
                  {currentStage.blockerReason || currentStage.definition.gateRule}
                </span>
              )}
              {nextStage && (
                <button
                  type="button"
                  disabled={!canContinue || actionBusy}
                  onClick={() => navigateToStep(viewStep + 1)}
                  className="inline-flex items-center gap-1.5 rounded-xl bg-[var(--ink-primary)] px-4 py-2 text-xs font-semibold text-white hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-40"
                >
                  <span>Continue to Step {viewStep + 1}</span>
                  <ArrowRight className="h-3.5 w-3.5" aria-hidden="true" />
                </button>
              )}
            </div>
          </div>
        </section>

        <div className="lg:col-span-4 lg:order-2 lg:sticky lg:top-6">
          <WorkflowStepper
            derived={derived}
            activeViewStep={viewStep}
            onSelectStep={navigateToStep}
          />
        </div>
      </div>
    </div>
  );
}
