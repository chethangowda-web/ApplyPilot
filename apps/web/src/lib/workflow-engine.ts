import type {
  ApplicationDetailResponse,
  ApplicationResponse,
  JobDetailResponse,
  JobMatchResponse,
  JobResumeHistoryResponse,
  JobSummaryResponse,
  ResumeGenerationHistoryResponse,
  TailoredResumeSummaryResponse,
} from "@/lib/types";

export type StepVisualState =
  | "completed"
  | "current"
  | "waiting"
  | "blocked"
  | "failed"
  | "upcoming";

export type WorkflowStageId =
  | "job-discovery"
  | "jd-analysis"
  | "fit-eligibility"
  | "resume-tailoring"
  | "resume-approval"
  | "application-prepare"
  | "application-questions"
  | "final-approval"
  | "browser-handoff"
  | "hold-confirm"
  | "verification-tracking";

export interface WorkflowStageDefinition {
  stepNumber: number;
  id: WorkflowStageId;
  title: string;
  shortTitle: string;
  description: string;
  gateRule: string;
}

export const WORKFLOW_STAGES: readonly WorkflowStageDefinition[] = [
  {
    stepNumber: 1,
    id: "job-discovery",
    title: "Job Discovery",
    shortTitle: "Discovery",
    description: "Verify the canonical job posting provenance, destination URL, and active status.",
    gateRule: "Requires an active job posting from permitted discovery connectors.",
  },
  {
    stepNumber: 2,
    id: "jd-analysis",
    title: "Job Description Analysis",
    shortTitle: "JD Analysis",
    description: "Extract structured skills, responsibilities, eligibility criteria, and injection flags.",
    gateRule: "Requires persisted JobRequirements extraction.",
  },
  {
    stepNumber: 3,
    id: "fit-eligibility",
    title: "Candidate Fit & Eligibility",
    shortTitle: "Fit & Eligibility",
    description: "Evaluate hard eligibility rules and transparent 100-point rubric fit against your verified profile.",
    gateRule: "Requires deterministic eligibility and hard constraints to pass.",
  },
  {
    stepNumber: 4,
    id: "resume-tailoring",
    title: "Resume Tailoring",
    shortTitle: "Tailoring",
    description: "Generate a truthful, evidence-grounded tailored PDF resume via the durable outbox worker.",
    gateRule: "Requires a completed, factually validated tailored resume version.",
  },
  {
    stepNumber: 5,
    id: "resume-approval",
    title: "Resume Review & Approval",
    shortTitle: "Resume Approval",
    description: "Inspect the tailoring diff, factual claims, and ATS report, then explicitly approve or reject.",
    gateRule: "Requires explicit owner approval on the active tailored resume.",
  },
  {
    stepNumber: 6,
    id: "application-prepare",
    title: "Application Preparation & Form Understanding",
    shortTitle: "Prepare Form",
    description: "Evaluate policy rules, map verified evidence to application form fields, and create the application record.",
    gateRule: "Requires policy clearance and a prepared Application record.",
  },
  {
    stepNumber: 7,
    id: "application-questions",
    title: "Application Question Completion",
    shortTitle: "Questions",
    description: "Complete missing answers and verify sensitive fields (encrypted at rest via AES-256-GCM).",
    gateRule: "Requires every application question—including sensitive fields—to be verified.",
  },
  {
    stepNumber: 8,
    id: "final-approval",
    title: "Final Application Review & Approval",
    shortTitle: "Final Approval",
    description: "Review your immutable approval snapshot binding resume checksum, destination URL, and verified answers.",
    gateRule: "Requires a non-expired explicit owner ApprovalRecord.",
  },
  {
    stepNumber: 9,
    id: "browser-handoff",
    title: "Browser Prefill & Visible Handoff",
    shortTitle: "Browser Handoff",
    description: "Launch supervised isolated browser prefill under SSRF and Submit-guard protection without auto-submitting.",
    gateRule: "Requires approved snapshot execution and an active handoff session.",
  },
  {
    stepNumber: 10,
    id: "hold-confirm",
    title: "Hold-and-Confirm Submission",
    shortTitle: "Hold & Confirm",
    description: "Inspect the intercepted pending submission payload digest and grant single-use manual authorization.",
    gateRule: "Requires an intercepted pending submission matching expected_payload_sha256.",
  },
  {
    stepNumber: 11,
    id: "verification-tracking",
    title: "Submission Verification & Tracking",
    shortTitle: "Verification",
    description: "Record the manual submission outcome and reconcile trusted receipt evidence before CONFIRMED.",
    gateRule: "Separates USER_REPORTED_SUBMITTED and UNVERIFIED from verified CONFIRMED status.",
  },
] as const;

export interface StageDerivation {
  definition: WorkflowStageDefinition;
  state: StepVisualState;
  statusSummary: string;
  blockerReason: string | null;
  canInspect: boolean;
}

export interface DerivedWorkflowState {
  stages: StageDerivation[];
  authoritativeStep: number;
  completedCount: number;
  progressPercent: number;
  overallState: StepVisualState;
  backendStatusLabel: string;
  nextActionRequired: string;
  activeResumeVersion: ResumeGenerationHistoryResponse | null;
  latestResumeVersion: ResumeGenerationHistoryResponse | null;
  application: ApplicationResponse | null;
  isTerminalBlocked: boolean;
  lastActivityAt: string;
}

export interface WorkflowDerivationInput {
  job: JobSummaryResponse | JobDetailResponse;
  match?: JobMatchResponse | null;
  resumeHistory?: JobResumeHistoryResponse | null;
  applicationDetail?: ApplicationDetailResponse | null;
  nowMs?: number;
}

const POST_APPROVAL_STATUSES = new Set<string>([
  "APPROVED",
  "SUBMISSION_PENDING",
  "SUBMITTED",
  "USER_REPORTED_SUBMITTED",
  "UNVERIFIED",
  "MANUAL_ACTION_REQUIRED",
  "CONFIRMED",
  "INTERVIEW",
  "WITHDRAWN",
]);

const POST_SUBMIT_STATUSES = new Set<string>([
  "SUBMITTED",
  "USER_REPORTED_SUBMITTED",
  "CONFIRMED",
  "INTERVIEW",
]);

export function pickPreferredResumeVersions(
  resumeHistory?: JobResumeHistoryResponse | null,
): {
  activeCompleted: ResumeGenerationHistoryResponse | null;
  latestVersion: ResumeGenerationHistoryResponse | null;
} {
  const versions = [...(resumeHistory?.versions ?? [])].sort(
    (a, b) => b.version_number - a.version_number,
  );
  const latestVersion = versions[0] ?? null;
  const activeCompleted =
    versions.find((v) => v.is_active && v.status === "completed") ??
    versions.find((v) => v.status === "completed") ??
    null;
  return { activeCompleted, latestVersion };
}

export function deriveJobWorkflowState(input: WorkflowDerivationInput): DerivedWorkflowState {
  const { job, resumeHistory, applicationDetail } = input;
  const nowMs = input.nowMs ?? Date.now();
  const match = input.match ?? job.match ?? null;
  const app = applicationDetail?.application ?? null;
  const answers = applicationDetail?.answers ?? [];
  const approvalRecords = applicationDetail?.approval_records ?? [];
  const handoffSessions = applicationDetail?.active_handoff_sessions ?? [];

  const { activeCompleted, latestVersion } = pickPreferredResumeVersions(resumeHistory);
  const outboxJobs = [...(resumeHistory?.outbox_jobs ?? [])].sort(
    (a, b) => new Date(b.created_at).getTime() - new Date(a.created_at).getTime(),
  );
  const latestOutbox = outboxJobs[0] ?? null;

  const stages: StageDerivation[] = [];

  // Step 1: Job Discovery
  const jobActive = job.status === "active";
  stages.push({
    definition: WORKFLOW_STAGES[0],
    state: jobActive ? "completed" : "blocked",
    statusSummary: jobActive
      ? `Active posting tracked from discovery (${job.remote_policy}, ${job.employment_type}).`
      : `Posting status is "${job.status}". Inactive or expired jobs cannot proceed.`,
    blockerReason: jobActive ? null : `Job posting status is "${job.status}".`,
    canInspect: true,
  });

  // Step 2: Job Description Analysis
  const step1Done = stages[0].state === "completed";
  const hasReqs = job.requirements !== null;
  stages.push({
    definition: WORKFLOW_STAGES[1],
    state: !step1Done ? "upcoming" : hasReqs ? "completed" : "current",
    statusSummary: !step1Done
      ? "Blocked until posting returns to active status."
      : hasReqs
        ? `Extracted ${job.requirements!.required_skills.length} required skills (${job.requirements!.extraction_mode}).`
        : "Run structured JD extraction to identify skills, constraints, and prompt injections.",
    blockerReason: !step1Done
      ? stages[0].blockerReason
      : !hasReqs
        ? "Job description analysis has not been run yet."
        : null,
    canInspect: step1Done,
  });

  // Step 3: Fit & Eligibility
  const step2Done = stages[1].state === "completed";
  const eligiblePassed = Boolean(match && match.eligible && match.hard_constraints_passed);
  let step3State: StepVisualState = "upcoming";
  let step3Summary = "Waiting for JD analysis.";
  let step3Blocker: string | null = "Complete Job Description Analysis first.";
  if (step2Done) {
    if (!match) {
      step3State = "current";
      step3Summary = "Run Candidate Fit & Eligibility scoring against your Master Profile.";
      step3Blocker = "Eligibility and match scoring have not been evaluated yet.";
    } else if (!eligiblePassed) {
      step3State = "blocked";
      const blockers =
        match.eligibility_blockers.length > 0
          ? match.eligibility_blockers.join("; ")
          : match.explanation;
      step3Summary = `Eligibility blocked (${Math.round(match.score)}/100 score).`;
      step3Blocker = blockers || "Candidate does not satisfy hard job constraints.";
    } else {
      step3State = "completed";
      step3Summary = `Eligible — ${Math.round(match.score)}/100 rubric score (${match.recommendation}).`;
      step3Blocker = null;
    }
  }
  stages.push({
    definition: WORKFLOW_STAGES[2],
    state: step3State,
    statusSummary: step3Summary,
    blockerReason: step3Blocker,
    canInspect: step2Done,
  });

  // Step 4: Resume Tailoring
  const step3Done = stages[2].state === "completed";
  let step4State: StepVisualState = "upcoming";
  let step4Summary = "Waiting for eligibility clearance.";
  let step4Blocker: string | null = stages[2].blockerReason;
  if (step3Done) {
    if (activeCompleted) {
      step4State = "completed";
      step4Summary = `Tailored PDF v${activeCompleted.version_number} ready for review (${activeCompleted.model_provider ?? "deterministic"}).`;
      step4Blocker = null;
    } else if (latestOutbox && (latestOutbox.status === "queued" || latestOutbox.status === "running")) {
      step4State = "waiting";
      step4Summary = `Resume tailoring worker is ${latestOutbox.status} (attempt ${latestOutbox.attempt_count}/${latestOutbox.max_attempts}).`;
      step4Blocker = "Resume generation is still running in the outbox worker.";
    } else if (
      latestOutbox?.status === "failed" ||
      latestOutbox?.status === "validation_failed" ||
      latestVersion?.status === "failed" ||
      latestVersion?.status === "validation_failed"
    ) {
      const errMsg =
        latestOutbox?.error_message ||
        latestVersion?.error_message ||
        "Resume generation or factual validation failed.";
      step4State = "failed";
      step4Summary = `Generation failed: ${errMsg}`;
      step4Blocker = errMsg;
    } else {
      step4State = "current";
      step4Summary = "Queue truthful resume tailoring anchored strictly to verified profile facts.";
      step4Blocker = "No completed tailored resume version exists for this job yet.";
    }
  }
  stages.push({
    definition: WORKFLOW_STAGES[3],
    state: step4State,
    statusSummary: step4Summary,
    blockerReason: step4Blocker,
    canInspect: step3Done,
  });

  // Step 5: Resume Review & Approval
  const step4Done = stages[3].state === "completed";
  let step5State: StepVisualState = "upcoming";
  let step5Summary = "Waiting for a completed tailored resume.";
  let step5Blocker: string | null = stages[3].blockerReason;
  if (step4Done && activeCompleted) {
    if (activeCompleted.approval_status === "approved") {
      step5State = "completed";
      step5Summary = `Version v${activeCompleted.version_number} explicitly approved by owner.`;
      step5Blocker = null;
    } else if (activeCompleted.approval_status === "rejected") {
      step5State = "blocked";
      step5Summary = `Version v${activeCompleted.version_number} was rejected.`;
      step5Blocker = "Tailored resume was rejected. Regenerate a new version in Step 4.";
    } else {
      step5State = "waiting";
      step5Summary = `Version v${activeCompleted.version_number} is pending_review; explicit owner approval required.`;
      step5Blocker = "Explicit owner approval of the tailored resume is required before continuing.";
    }
  }
  stages.push({
    definition: WORKFLOW_STAGES[4],
    state: step5State,
    statusSummary: step5Summary,
    blockerReason: step5Blocker,
    canInspect: step4Done,
  });

  // Step 6: Application Preparation & Form Understanding
  const step5Done = stages[4].state === "completed";
  let step6State: StepVisualState = "upcoming";
  let step6Summary = "Waiting for approved tailored resume.";
  let step6Blocker: string | null = stages[4].blockerReason;
  if (app) {
    if (app.status === "DUPLICATE" || app.status === "EXPIRED") {
      step6State = "blocked";
      step6Summary = `Application halted with status ${app.status}.`;
      step6Blocker = app.error_message || `Application blocked (${app.status}).`;
    } else {
      step6State = "completed";
      step6Summary = `Application prepared in ${app.application_mode} mode (${app.status}).`;
      step6Blocker = null;
    }
  } else if (step5Done) {
    step6State = "current";
    step6Summary = "Run ApplicationPolicyRouter and FormUnderstandingAgent using the approved resume.";
    step6Blocker = "Application record has not been prepared yet.";
  }
  stages.push({
    definition: WORKFLOW_STAGES[5],
    state: step6State,
    statusSummary: step6Summary,
    blockerReason: step6Blocker,
    canInspect: step5Done || app !== null,
  });

  // Step 7: Application Question Completion
  const step6Done = stages[5].state === "completed";
  const unverifiedAnswers = answers.filter((a) => !a.user_verified);
  const sensitiveUnverified = unverifiedAnswers.filter((a) => a.is_sensitive);
  let step7State: StepVisualState = "upcoming";
  let step7Summary = "Waiting for application preparation.";
  let step7Blocker: string | null = stages[5].blockerReason;
  if (app && step6Done) {
    if (app.status === "NEEDS_USER_INPUT" || unverifiedAnswers.length > 0) {
      step7State = "waiting";
      step7Summary =
        sensitiveUnverified.length > 0
          ? `${unverifiedAnswers.length} question(s) pending verification (${sensitiveUnverified.length} sensitive).`
          : `${unverifiedAnswers.length} question(s) need user verification before approval.`;
      step7Blocker = "Submit verified answers for all pending questions before continuing.";
    } else {
      step7State = "completed";
      step7Summary =
        answers.length > 0
          ? `${answers.length} application answer(s) verified (sensitive values encrypted).`
          : "No unresolved form questions remain.";
      step7Blocker = null;
    }
  }
  stages.push({
    definition: WORKFLOW_STAGES[6],
    state: step7State,
    statusSummary: step7Summary,
    blockerReason: step7Blocker,
    canInspect: app !== null && step6Done,
  });

  // Step 8: Final Application Review & Approval
  const step7Done = stages[6].state === "completed";
  const latestApproval = approvalRecords[0] ?? null;
  const approvalExpired =
    app?.status === "APPROVED" &&
    latestApproval !== null &&
    new Date(latestApproval.expires_at).getTime() <= nowMs;
  let step8State: StepVisualState = "upcoming";
  let step8Summary = "Waiting for verified application answers.";
  let step8Blocker: string | null = stages[6].blockerReason;
  if (app && step7Done) {
    if (app.status === "AWAITING_APPROVAL") {
      step8State = "waiting";
      step8Summary = "Awaiting explicit owner review & immutable ApprovalRecord.";
      step8Blocker = "Explicit human approval is required before execution.";
    } else if (app.status === "REJECTED") {
      step8State = "blocked";
      step8Summary = "Application was explicitly rejected by the user.";
      step8Blocker = latestApproval?.notes || "Application is REJECTED and cannot execute.";
    } else if (approvalExpired) {
      step8State = "blocked";
      step8Summary = "Approval snapshot TTL has expired.";
      step8Blocker = "Re-approve the application to issue a fresh snapshot before browser execution.";
    } else if (app.status === "FAILED" && latestApproval?.decision !== "approved") {
      step8State = "failed";
      step8Summary = app.error_message || "Application failed prior to approval.";
      step8Blocker = app.error_message || "Re-approve from FAILED state after resolving the error.";
    } else if (POST_APPROVAL_STATUSES.has(app.status)) {
      step8State = "completed";
      step8Summary = `Snapshot approval granted (${app.application_mode}).`;
      step8Blocker = null;
    } else {
      step8State = "failed";
      step8Summary = `Unexpected status "${app.status}".`;
      step8Blocker = app.error_message || `Cannot advance from status ${app.status}.`;
    }
  }
  stages.push({
    definition: WORKFLOW_STAGES[7],
    state: step8State,
    statusSummary: step8Summary,
    blockerReason: step8Blocker,
    canInspect: app !== null && step7Done,
  });

  // Step 9: Browser Prefill & Visible Handoff
  const step8Done = stages[7].state === "completed";
  const activeHandoff = handoffSessions.find((s) => s.status === "active") ?? null;
  const latestHandoff = handoffSessions[0] ?? null;
  let step9State: StepVisualState = "upcoming";
  let step9Summary = "Waiting for Final Application Approval.";
  let step9Blocker: string | null = stages[7].blockerReason;
  if (app && step8Done) {
    if (POST_SUBMIT_STATUSES.has(app.status) || app.status === "UNVERIFIED") {
      step9State = "completed";
      step9Summary = "Supervised browser prefill & handoff phase completed.";
      step9Blocker = null;
    } else if (app.status === "MANUAL_ACTION_REQUIRED") {
      if (activeHandoff) {
        step9State = "completed";
        step9Summary = "Supervised browser prefilled and handed off (auto-submit disabled).";
        step9Blocker = null;
      } else {
        step9State = "waiting";
        step9Summary = `Handoff session is ${latestHandoff?.status ?? "not open"}; re-execute if approved or record outcome in Step 11.`;
        step9Blocker = "No active supervised browser handoff session is open on the backend.";
      }
    } else if (app.status === "SUBMISSION_PENDING") {
      step9State = "waiting";
      step9Summary = "Supervised browser prefill is currently executing.";
      step9Blocker = "Execution is still running.";
    } else if (app.status === "FAILED") {
      step9State = "failed";
      step9Summary = app.error_message || "Browser prefill failed cleanly without submitting.";
      step9Blocker = app.error_message || "Execution failed; re-approve in Step 8 to retry.";
    } else if (app.status === "APPROVED") {
      step9State = "current";
      step9Summary = "Launch supervised Playwright browser prefill (never auto-submits).";
      step9Blocker = "Browser prefill and handoff have not been launched yet.";
    }
  }
  stages.push({
    definition: WORKFLOW_STAGES[8],
    state: step9State,
    statusSummary: step9Summary,
    blockerReason: step9Blocker,
    canInspect: app !== null && step8Done,
  });

  // Step 10: Hold-and-Confirm Submission
  const step9Done = stages[8].state === "completed";
  const heldPending = activeHandoff?.pending_submission ?? null;
  let step10State: StepVisualState = "upcoming";
  let step10Summary = "Waiting for supervised browser prefill & handoff.";
  let step10Blocker: string | null = stages[8].blockerReason;
  if (app && step9Done) {
    if (POST_SUBMIT_STATUSES.has(app.status)) {
      step10State = "completed";
      step10Summary = "Manual submission confirmation completed / outcome recorded.";
      step10Blocker = null;
    } else if (app.status === "UNVERIFIED") {
      step10State = "failed";
      step10Summary = app.error_message || "Held submission failed verification or was interrupted.";
      step10Blocker = app.error_message || "Application is UNVERIFIED; proceed to Step 11.";
    } else if (app.status === "FAILED") {
      step10State = "failed";
      step10Summary = app.error_message || "Submission failed.";
      step10Blocker = app.error_message || "Application FAILED.";
    } else if (activeHandoff?.manual_submit_armed) {
      step10State = "completed";
      step10Summary = "One-time manual authorization released the held submission.";
      step10Blocker = null;
    } else if (heldPending) {
      step10State = "current";
      step10Summary = `Held submission ${heldPending.pending_submission_id.slice(0, 8)} awaiting explicit confirmation.`;
      step10Blocker = "Confirm the held submission details and payload digest to release.";
    } else if (app.status === "MANUAL_ACTION_REQUIRED") {
      step10State = "waiting";
      step10Summary = "Review the prefilled form in the visible browser and click Submit there to hold & inspect.";
      step10Blocker = "No pending submission has been intercepted from the browser yet.";
    }
  }
  stages.push({
    definition: WORKFLOW_STAGES[9],
    state: step10State,
    statusSummary: step10Summary,
    blockerReason: step10Blocker,
    canInspect: app !== null && (step9Done || app.status === "MANUAL_ACTION_REQUIRED"),
  });

  // Step 11: Submission Verification & Application Tracking
  let step11State: StepVisualState = "upcoming";
  let step11Summary = "Waiting for manual submission completion.";
  let step11Blocker: string | null = stages[9].blockerReason;
  if (app) {
    if (app.status === "CONFIRMED" || app.status === "INTERVIEW") {
      step11State = "completed";
      step11Summary = `Verified ${app.status}${app.external_ref_id ? ` (Ref: ${app.external_ref_id})` : ""}.`;
      step11Blocker = null;
    } else if (app.status === "USER_REPORTED_SUBMITTED") {
      step11State = "waiting";
      step11Summary = "User reported submitted; reconcile official receipt before CONFIRMED.";
      step11Blocker = "Independent confirmation receipt is still pending reconciliation.";
    } else if (app.status === "SUBMITTED") {
      step11State = "waiting";
      step11Summary = "Submitted; awaiting confirmation receipt verification.";
      step11Blocker = "Reconcile confirmation evidence to verify CONFIRMED status.";
    } else if (app.status === "UNVERIFIED") {
      step11State = "failed";
      step11Summary = `UNVERIFIED: ${app.error_message || "Submission state could not be verified automatically."}`;
      step11Blocker = app.error_message || "Inspect outcome or supply confirmation receipt.";
    } else if (app.status === "FAILED") {
      step11State = "failed";
      step11Summary = `FAILED: ${app.error_message || "Application failed cleanly."}`;
      step11Blocker = app.error_message || "Review error before retrying from Step 8.";
    } else if (stages[9].state === "completed") {
      step11State = "current";
      step11Summary = "Record browser submission outcome or reconcile confirmation receipt.";
      step11Blocker = null;
    } else if (app.status === "MANUAL_ACTION_REQUIRED") {
      step11State = "waiting";
      step11Summary = "After finishing in the browser, record outcome (submitted, interrupted, or failed).";
      step11Blocker = "Record submission outcome after browser handoff completes.";
    }
  }
  stages.push({
    definition: WORKFLOW_STAGES[10],
    state: step11State,
    statusSummary: step11Summary,
    blockerReason: step11Blocker,
    canInspect:
      app !== null &&
      (stages[9].state === "completed" ||
        ["SUBMITTED", "USER_REPORTED_SUBMITTED", "UNVERIFIED", "CONFIRMED", "INTERVIEW", "FAILED", "MANUAL_ACTION_REQUIRED"].includes(
          app.status,
        )),
  });

  // Compute authoritative step & progress strictly from derived backend stages
  const completedCount = stages.filter((s) => s.state === "completed").length;
  const progressPercent = Math.round((completedCount / WORKFLOW_STAGES.length) * 100);

  const attentionStage =
    stages.find((s) => s.state === "failed") ??
    stages.find((s) => s.state === "blocked") ??
    stages.find((s) => s.state === "current") ??
    stages.find((s) => s.state === "waiting") ??
    stages[stages.length - 1];

  const authoritativeStep = attentionStage.definition.stepNumber;

  let overallState: StepVisualState = "current";
  if (stages.some((s) => s.state === "failed")) {
    overallState = "failed";
  } else if (stages.some((s) => s.state === "blocked")) {
    overallState = "blocked";
  } else if (completedCount === WORKFLOW_STAGES.length) {
    overallState = "completed";
  } else if (attentionStage.state === "waiting") {
    overallState = "waiting";
  }

  let backendStatusLabel = app?.status ?? job.status.toUpperCase();
  if (!app) {
    if (job.status !== "active") {
      backendStatusLabel = job.status.toUpperCase();
    } else if (activeCompleted) {
      backendStatusLabel = `RESUME_${activeCompleted.approval_status.toUpperCase()}`;
    } else if (latestOutbox) {
      backendStatusLabel = `TAILOR_${latestOutbox.status.toUpperCase()}`;
    } else if (match) {
      backendStatusLabel = match.eligible ? "ELIGIBLE" : "INELIGIBLE";
    } else if (job.requirements) {
      backendStatusLabel = "ANALYZED";
    } else {
      backendStatusLabel = "DISCOVERED";
    }
  }

  const lastActivityAt =
    app?.updated_at ??
    latestVersion?.created_at ??
    latestOutbox?.created_at ??
    match?.scored_at ??
    job.last_seen_at;

  return {
    stages,
    authoritativeStep,
    completedCount,
    progressPercent,
    overallState,
    backendStatusLabel,
    nextActionRequired: attentionStage.statusSummary,
    activeResumeVersion: activeCompleted,
    latestResumeVersion: latestVersion,
    application: app,
    isTerminalBlocked:
      overallState === "blocked" &&
      (job.status !== "active" ||
        app?.status === "REJECTED" ||
        app?.status === "DUPLICATE" ||
        app?.status === "EXPIRED" ||
        step3State === "blocked"),
    lastActivityAt,
  };
}

export interface WorkflowOverviewItem {
  job: JobSummaryResponse;
  derived: DerivedWorkflowState;
}

export function buildWorkflowOverviewItems(params: {
  jobs: JobSummaryResponse[];
  tailoredResumes: TailoredResumeSummaryResponse[];
  applications: ApplicationResponse[];
  applicationDetailsById?: Record<string, ApplicationDetailResponse>;
  nowMs?: number;
}): WorkflowOverviewItem[] {
  const { jobs, tailoredResumes, applications, applicationDetailsById = {}, nowMs } = params;
  const jobsById = new Map(jobs.map((j) => [j.id, j]));
  const appsByJobId = new Map<string, ApplicationResponse>();
  for (const app of applications) {
    if (!appsByJobId.has(app.job_id)) {
      appsByJobId.set(app.job_id, app);
    }
  }
  const tailoredByJobId = new Map<string, TailoredResumeSummaryResponse[]>();
  for (const tr of tailoredResumes) {
    const bucket = tailoredByJobId.get(tr.job_id) ?? [];
    bucket.push(tr);
    tailoredByJobId.set(tr.job_id, bucket);
  }

  // Include all jobs that have started at least JD analysis / match / tailoring / application
  const activeJobIds = new Set<string>();
  for (const job of jobs) {
    if (job.requirements || job.match || appsByJobId.has(job.id) || tailoredByJobId.has(job.id)) {
      activeJobIds.add(job.id);
    }
  }

  const items: WorkflowOverviewItem[] = [];
  for (const jobId of activeJobIds) {
    const job = jobsById.get(jobId);
    if (!job) continue;
    const app = appsByJobId.get(jobId) ?? null;
    const trList = tailoredByJobId.get(jobId) ?? [];
    const mappedVersions: ResumeGenerationHistoryResponse[] = trList.map((tr) => ({
      mapping_id: tr.mapping_id,
      resume_id: tr.resume_id,
      job_id: tr.job_id,
      master_resume_id: tr.master_resume_id,
      generation_job_id: null,
      run_id: "",
      trigger_mode: "manual",
      version_number: tr.version_number,
      is_active: tr.is_active,
      evidence_fingerprint: tr.evidence_fingerprint,
      status: tr.status,
      approval_status: tr.approval_status,
      approval_notes: null,
      approved_at: null,
      rejected_at: null,
      model_provider: null,
      token_usage: {},
      error_message: null,
      created_at: tr.created_at,
    }));

    const detail: ApplicationDetailResponse | null = app
      ? applicationDetailsById[app.id] ?? {
          application: app,
          answers: [],
          approval_records: [],
          active_handoff_sessions: [],
          execution_history: [],
        }
      : null;

    const derived = deriveJobWorkflowState({
      job,
      match: job.match,
      resumeHistory: { job_id: job.id, versions: mappedVersions, outbox_jobs: [] },
      applicationDetail: detail,
      nowMs,
    });
    items.push({ job, derived });
  }

  return items.sort(
    (a, b) =>
      new Date(b.derived.lastActivityAt).getTime() - new Date(a.derived.lastActivityAt).getTime(),
  );
}
