import React from "react";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  Stage6PreparePanel,
  Stage7QuestionsPanel,
  Stage8ApprovalPanel,
  classifyAnswerProvenance,
} from "@/components/Stage6To8ApplicationPrep";
import { WorkflowWorkspace } from "@/components/WorkflowWorkspace";
import type {
  ApplicationDetailResponse,
  ApplicationPreparationResult,
  JobDetailResponse,
  ResumeMetadataResponse,
  TailoredResumeDetailResponse,
} from "@/lib/types";

const mockReplace = vi.fn();
const mockPush = vi.fn();
const mockRefresh = vi.fn();
let currentSearchParams = new URLSearchParams();

vi.mock("next/navigation", () => ({
  useRouter: () => ({
    replace: mockReplace,
    push: mockPush,
    refresh: mockRefresh,
  }),
  useSearchParams: () => currentSearchParams,
  usePathname: () => "/workflows/job-101",
}));

const mockApi = vi.hoisted(() => ({
  listJobs: vi.fn(),
  listJobSources: vi.fn(),
  listApplications: vi.fn(),
  getApplicationDetail: vi.fn(),
  prepareApplication: vi.fn(),
  submitApplicationAnswers: vi.fn(),
  approveApplication: vi.fn(),
  rejectApplication: vi.fn(),
  getJobDetail: vi.fn(),
  getJobResumeHistory: vi.fn(),
  getJobIntelligence: vi.fn(),
  getJobEvidence: vi.fn(),
  listResumeVersions: vi.fn(),
  getTailoredResumeDetail: vi.fn(),
  getTailoringDiff: vi.fn(),
  getResumeValidation: vi.fn(),
  tailoredResumeDownloadUrl: vi.fn((id: string) => `/api/backend/resumes/${id}/download`),
}));

vi.mock("@/lib/api-client", () => ({
  ApiError: class ApiError extends Error {
    status: number;
    detail: string;
    constructor(status: number, detail: string) {
      super(detail);
      this.status = status;
      this.detail = detail;
    }
    isUnauthorized() {
      return this.status === 401;
    }
  },
  apiClient: mockApi,
}));

const sampleJob: JobDetailResponse = {
  id: "job-101",
  source_id: "src-1",
  source_job_id: "gh-101",
  canonical_url: "https://boards.greenhouse.io/acme/jobs/101",
  company: "Acme Robotics",
  role_title: "Applied AI Engineer",
  location: "Bengaluru, India",
  remote_policy: "hybrid",
  is_remote: false,
  employment_type: "full_time",
  salary_min: null,
  salary_max: null,
  salary_currency: null,
  salary_text: null,
  application_url: "https://boards.greenhouse.io/acme/jobs/101",
  posted_at: "2026-11-26T00:00:00Z",
  first_seen_at: "2026-11-26T00:00:00Z",
  last_seen_at: "2026-11-26T06:00:00Z",
  repost_count: 0,
  status: "active",
  requirements: {
    role_category: "AI Engineer",
    location: "Bengaluru, India",
    remote_policy: "hybrid",
    employment_type: "full_time",
    required_skills: ["Python"],
    preferred_skills: [],
    trainable_skills: [],
    responsibilities: [],
    technical_stack: ["Python"],
    experience_min: 0,
    experience_max: 2,
    education_requirements: [],
    graduation_criteria: [],
    mandatory_qualifications: [],
    preferred_qualifications: [],
    application_instructions: [],
    evidence_items: {},
    extraction_mode: "deterministic",
    internship_eligible: false,
    fresher_eligible: true,
    uncertainties: [],
    prompt_injection_detected: false,
    extracted_at: "2026-11-26T06:10:00Z",
  },
  match: {
    job_id: "job-101",
    profile_id: "prof-1",
    score: 92,
    eligible: true,
    hard_constraints_passed: true,
    required_skill_coverage: 1,
    preferred_skill_coverage: 1,
    semantic_similarity: 0.9,
    matched_skills: ["Python"],
    missing_required_skills: [],
    preferred_gaps: [],
    evidence_mapping: [],
    missing_evidence: [],
    eligibility_blockers: [],
    uncertainties: [],
    confidence: 0.95,
    score_breakdown: {},
    recommendation: "strong_apply",
    explanation: "Strong fit.",
    scored_at: "2026-11-26T06:11:00Z",
  },
  raw_jd: "Build Python AI systems.",
  updated_at_source: null,
  expired_at: null,
  source_provenance: {},
};

const masterResume: ResumeMetadataResponse = {
  id: "master-1",
  profile_id: "prof-1",
  is_master: true,
  original_filename: "master.pdf",
  content_type: "application/pdf",
  file_size_bytes: 10240,
  version_label: "master-v1",
  sha256_checksum: "a".repeat(64),
  template_version: "master-upload-v1",
  ats_report: {},
  parsed_metadata: {},
  validation_passed: true,
  created_at: "2026-11-26T05:00:00Z",
};

const tailoredDetail: TailoredResumeDetailResponse = {
  resume_id: "res-1",
  mapping_id: "map-1",
  job_id: "job-101",
  master_resume_id: "master-1",
  version_number: 1,
  version_label: "tailored-v1",
  is_active: true,
  status: "completed",
  approval_status: "approved",
  template_version: "ats-v1",
  evidence_fingerprint: "fp-1",
  validation_passed: true,
  evaluation_report: {},
  created_at: "2026-11-26T06:20:00Z",
  run_id: "run-1",
  trigger_mode: "manual",
  sha256_checksum: "b".repeat(64),
  file_size_bytes: 12000,
  ats_report: {},
  parsed_metadata: {},
  validation_report: {},
  tailoring_diff: {},
  token_usage: {},
  model_provider: "claude",
  approval_notes: null,
  approved_at: "2026-11-26T06:22:00Z",
  rejected_at: null,
};

describe("Milestone 7 Stage 3 — Application Preparation (Steps 6–8)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    currentSearchParams = new URLSearchParams();
  });

  it("classifies verified profile facts, AI drafts, user confirmed, and missing answers accurately", () => {
    expect(
      classifyAnswerProvenance({
        evidenceSource: "candidate_profiles.email",
        userVerified: true,
        isSensitive: false,
        hasValue: true,
      }),
    ).toBe("verified_profile");

    expect(
      classifyAnswerProvenance({
        evidenceSource: "llm_draft_unverified",
        userVerified: false,
        isSensitive: false,
        hasValue: true,
      }),
    ).toBe("ai_draft");

    expect(
      classifyAnswerProvenance({
        evidenceSource: "user_input",
        userVerified: true,
        isSensitive: true,
        hasValue: true,
      }),
    ).toBe("user_verified");

    expect(
      classifyAnswerProvenance({
        evidenceSource: "candidate_profile.confirmed_preferences",
        userVerified: false,
        isSensitive: true,
        hasValue: true,
      }),
    ).toBe("requires_input");
  });

  it("distinguishes verified profile, AI draft, and sensitive missing fields in Stage 7 and saves verified inputs", async () => {
    const user = userEvent.setup();
    const onSubmitAnswers = vi.fn(async () => {});

    const appDetail: ApplicationDetailResponse = {
      application: {
        id: "app-1",
        user_id: "u-1",
        job_id: "job-101",
        resume_id: "res-1",
        canonical_job_key: "ck-1",
        idempotency_key: "ik-1",
        application_mode: "human_approved_browser",
        status: "NEEDS_USER_INPUT",
        destination_url: sampleJob.canonical_url,
        thread_id: "th-1",
        external_ref_id: null,
        confirmation_evidence: {},
        submitted_at: null,
        confirmed_at: null,
        next_followup_at: null,
        error_message: null,
        created_at: "2026-11-26T06:25:00Z",
        updated_at: "2026-11-26T06:25:00Z",
      },
      answers: [
        {
          id: "a-1",
          question_key: "email",
          question: "Email Address",
          is_sensitive: false,
          answer_preview: "candidate@example.com",
          evidence_source: "candidate_profiles.email",
          user_verified: true,
          created_at: "2026-11-26T06:25:00Z",
        },
        {
          id: "a-2",
          question_key: "why_company",
          question: "Why Acme Robotics?",
          is_sensitive: false,
          answer_preview: "AI drafted interest in robotics.",
          evidence_source: "llm_draft_unverified",
          user_verified: false,
          created_at: "2026-11-26T06:25:00Z",
        },
        {
          id: "a-3",
          question_key: "work_authorization",
          question: "Are you authorized to work in India?",
          is_sensitive: true,
          answer_preview: "[REDACTED — UNVERIFIED]",
          evidence_source: "candidate_profile.confirmed_preferences",
          user_verified: false,
          created_at: "2026-11-26T06:25:00Z",
        },
      ],
      approval_records: [],
      active_handoff_sessions: [],
      execution_history: [],
    };

    const prepResult: ApplicationPreparationResult = {
      application_id: "app-1",
      status: "NEEDS_USER_INPUT",
      policy: {
        allowed: true,
        selected_mode: "human_approved_browser",
        submission_paused: false,
        blocker_reasons: [],
        warnings: [],
        canonical_job_key: "ck-1",
        idempotency_key: "ik-1",
        daily_used: 1,
        daily_limit: 15,
      },
      form_mapping: {
        questions: [],
        mapped_answers: [
          {
            question_key: "portfolio_notes",
            question: "Additional Portfolio Notes",
            field_type: "textarea",
            required: false,
            is_sensitive: false,
            sensitive_category: null,
            verification_state: "missing",
            proposed_answer: null,
            evidence_source: null,
            requires_user_confirmation: false,
            reason: "No verified profile or resume evidence found",
          },
        ],
        missing_required_keys: [],
        missing_optional_keys: ["portfolio_notes"],
        unverified_keys: ["why_company", "work_authorization"],
        sensitive_keys_requiring_confirmation: ["work_authorization"],
        ready_for_approval: false,
      },
    };

    render(
      <Stage7QuestionsPanel
        applicationDetail={appDetail}
        preparationResult={prepResult}
        actionBusy={false}
        onSubmitAnswers={onSubmitAnswers}
      />,
    );

    expect(screen.getByText("Verified Profile Fact")).toBeInTheDocument();
    expect(screen.getByText("AI Draft (Verify)")).toBeInTheDocument();
    expect(screen.getAllByText("Requires Your Input").length).toBeGreaterThanOrEqual(2);
    expect(screen.getByText(/AES-256-GCM/)).toBeInTheDocument();

    const sensitiveInput = screen.getByLabelText(/Are you authorized to work in India/i);
    expect(sensitiveInput).toHaveAttribute("type", "password");
    await user.type(sensitiveInput, "Yes, citizen");

    await user.click(screen.getByRole("button", { name: /Save & Verify Answers/i }));
    expect(onSubmitAnswers).toHaveBeenCalledWith(
      expect.arrayContaining([
        expect.objectContaining({
          question_key: "work_authorization",
          answer: "Yes, citizen",
          is_sensitive: true,
        }),
      ]),
    );
  });

  it("prevents duplicate preparation and approval clicks and recovers Steps 6–8 state across reload", async () => {
    let appState: ApplicationDetailResponse | null = null;
    let resolvePrepare: (() => void) | null = null;
    let resolveApprove: (() => void) | null = null;

    mockApi.getJobDetail.mockResolvedValue(sampleJob);
    mockApi.listResumeVersions.mockResolvedValue([masterResume]);
    mockApi.getJobIntelligence.mockResolvedValue({
      job_id: "job-101",
      company: "Acme Robotics",
      role_title: "Applied AI Engineer",
      status: "active",
      requirements: sampleJob.requirements,
      match: sampleJob.match!,
      skill_coverage: {},
      score_disclaimer: "Rubric only.",
    });
    mockApi.getJobEvidence.mockResolvedValue({
      job_id: "job-101",
      profile_id: "prof-1",
      evidence_mapping: [],
      missing_evidence: [],
      confirmed_missing_skills: [],
      jd_evidence_items: {},
    });
    mockApi.getJobResumeHistory.mockResolvedValue({
      job_id: "job-101",
      outbox_jobs: [],
      versions: [
        {
          mapping_id: "map-1",
          resume_id: "res-1",
          job_id: "job-101",
          master_resume_id: "master-1",
          generation_job_id: "out-1",
          run_id: "run-1",
          trigger_mode: "manual",
          version_number: 1,
          is_active: true,
          evidence_fingerprint: "fp-1",
          status: "completed",
          approval_status: "approved",
          approval_notes: null,
          approved_at: "2026-11-26T06:22:00Z",
          rejected_at: null,
          model_provider: "claude",
          token_usage: {},
          error_message: null,
          created_at: "2026-11-26T06:20:00Z",
        },
      ],
    });
    mockApi.getTailoredResumeDetail.mockResolvedValue(tailoredDetail);
    mockApi.getTailoringDiff.mockResolvedValue(null);
    mockApi.getResumeValidation.mockResolvedValue(null);
    mockApi.listApplications.mockImplementation(async () =>
      appState ? [appState.application] : [],
    );
    mockApi.getApplicationDetail.mockImplementation(async () => appState);

    mockApi.prepareApplication.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolvePrepare = () => {
            appState = {
              application: {
                id: "app-1",
                user_id: "u-1",
                job_id: "job-101",
                resume_id: "res-1",
                canonical_job_key: "ck-1",
                idempotency_key: "ik-1",
                application_mode: "human_approved_browser",
                status: "AWAITING_APPROVAL",
                destination_url: sampleJob.canonical_url,
                thread_id: "th-1",
                external_ref_id: null,
                confirmation_evidence: {},
                submitted_at: null,
                confirmed_at: null,
                next_followup_at: null,
                error_message: null,
                created_at: "2026-11-26T06:25:00Z",
                updated_at: "2026-11-26T06:25:00Z",
              },
              answers: [],
              approval_records: [],
              active_handoff_sessions: [],
              execution_history: [],
            };
            resolve({
              application_id: "app-1",
              status: "AWAITING_APPROVAL",
              policy: {
                allowed: true,
                selected_mode: "human_approved_browser",
                submission_paused: false,
                blocker_reasons: [],
                warnings: [],
                canonical_job_key: "ck-1",
                idempotency_key: "ik-1",
                daily_used: 1,
                daily_limit: 15,
              },
              form_mapping: {
                questions: [],
                mapped_answers: [],
                missing_required_keys: [],
                missing_optional_keys: [],
                unverified_keys: [],
                sensitive_keys_requiring_confirmation: [],
                ready_for_approval: true,
              },
            });
          };
        }),
    );

    mockApi.approveApplication.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveApprove = () => {
            appState = {
              ...appState!,
              application: { ...appState!.application, status: "APPROVED" },
              approval_records: [
                {
                  id: "rec-1",
                  application_id: "app-1",
                  decision: "approved",
                  actor: "user:u-1",
                  resume_id: "res-1",
                  resume_checksum: "b".repeat(64),
                  destination_url: sampleJob.canonical_url,
                  application_mode: "human_approved_browser",
                  approval_snapshot_hash: "c".repeat(64),
                  notes: null,
                  decided_at: "2026-11-26T06:30:00Z",
                  expires_at: "2099-11-27T06:30:00Z",
                },
              ],
            };
            resolve(appState.approval_records[0]);
          };
        }),
    );

    const { unmount } = render(<WorkflowWorkspace jobId="job-101" />);

    const prepBtn = await screen.findByRole("button", { name: /Prepare Application/i });
    fireEvent.click(prepBtn);
    fireEvent.click(prepBtn);
    expect(mockApi.prepareApplication).toHaveBeenCalledTimes(1);

    await act(async () => {
      resolvePrepare?.();
    });

    const approveBtn = await screen.findByRole("button", {
      name: /Approve Application Snapshot/i,
    });
    fireEvent.click(approveBtn);
    fireEvent.click(approveBtn);
    expect(mockApi.approveApplication).toHaveBeenCalledTimes(1);

    await act(async () => {
      resolveApprove?.();
    });

    await waitFor(() => {
      expect(screen.getByText(/Immutable application approval snapshot created/i)).toBeInTheDocument();
    });

    // Reload workflow -> restores Step 9 (APPROVED) from backend without re-preparing or re-approving
    unmount();
    render(<WorkflowWorkspace jobId="job-101" />);
    await waitFor(() => {
      expect(
        screen.getByRole("heading", { name: "Browser Prefill & Visible Handoff" }),
      ).toBeInTheDocument();
    });
    expect(mockApi.prepareApplication).toHaveBeenCalledTimes(1);
    expect(mockApi.approveApplication).toHaveBeenCalledTimes(1);
  });

  it("renders Stage6PreparePanel and Stage8ApprovalPanel standalone bindings cleanly", () => {
    render(
      <Stage6PreparePanel
        job={sampleJob}
        activeResume={null}
        tailoredDetail={tailoredDetail}
        applicationDetail={null}
        preparationResult={null}
        actionBusy={false}
        fitScore={60}
        onPrepareApplication={vi.fn(async () => {})}
      />,
    );
    expect(screen.getByText(/Current fit score is 60\/100/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Prepare Application/i })).toBeDisabled();

    // Execution mode policy: official_api must be disabled/unavailable; manual vs human_approved_browser distinguished
    const officialOption = screen.getByRole("option", { name: /official_api/i }) as HTMLOptionElement;
    expect(officialOption.disabled).toBe(true);
    expect(officialOption.textContent).toMatch(/Unavailable/i);
    expect(screen.getByText(/Human-approved browser mode:/i)).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Application execution mode"), {
      target: { value: "manual" },
    });
    expect(screen.getByText(/Manual mode:/i)).toBeInTheDocument();

    render(
      <Stage8ApprovalPanel
        job={sampleJob}
        stage={{
          definition: {
            stepNumber: 8,
            id: "final-approval",
            title: "Final Application Review & Approval",
            shortTitle: "Final Approval",
            description: "Review snapshot.",
            gateRule: "Requires approval.",
          },
          state: "waiting",
          statusSummary: "Waiting",
          blockerReason: "Waiting",
          canInspect: true,
        }}
        activeResume={null}
        tailoredDetail={tailoredDetail}
        applicationDetail={null}
        preparationResult={null}
        actionBusy={false}
        onApproveApplication={vi.fn(async () => {})}
        onRejectApplication={vi.fn(async () => {})}
      />,
    );
    expect(
      screen.getByText(/Prepare and verify application questions in Steps 6–7 first/i),
    ).toBeInTheDocument();
  });
});
