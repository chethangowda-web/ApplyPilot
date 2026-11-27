import React from "react";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { MyWorkflowsView } from "@/components/MyWorkflowsView";
import { WorkflowWorkspace } from "@/components/WorkflowWorkspace";
import { HoldAndConfirmPanel } from "@/components/HoldAndConfirmPanel";
import { AppShell } from "@/components/AppShell";
import type {
  ApplicationDetailResponse,
  JobDetailResponse,
  JobIntelligenceResponse,
} from "@/lib/types";

const mockReplace = vi.fn();
const mockPush = vi.fn();
const mockRefresh = vi.fn();
let currentSearchParams = new URLSearchParams();
let currentPathname = "/";

vi.mock("next/navigation", () => ({
  useRouter: () => ({
    replace: mockReplace,
    push: mockPush,
    refresh: mockRefresh,
  }),
  useSearchParams: () => currentSearchParams,
  usePathname: () => currentPathname,
}));

const mockApi = vi.hoisted(() => ({
  listJobs: vi.fn(),
  listTailoredResumes: vi.fn(),
  listResumeVersions: vi.fn(async () => []),
  listApplications: vi.fn(),
  getApplicationDetail: vi.fn(),
  getJobDetail: vi.fn(),
  getJobResumeHistory: vi.fn(),
  getJobIntelligence: vi.fn(),
  getJobEvidence: vi.fn(),
  analyzeJob: vi.fn(),
  getTailoredResumeDetail: vi.fn(),
  getTailoringDiff: vi.fn(),
  getResumeValidation: vi.fn(),
  logout: vi.fn(),
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

const baseJob: JobDetailResponse = {
  id: "11111111-1111-4111-8111-111111111111",
  source_id: "22222222-2222-4222-8222-222222222222",
  source_job_id: "gh-1",
  canonical_url: "https://boards.greenhouse.io/acme/jobs/1",
  company: "Acme AI",
  role_title: "Applied AI Engineer",
  location: "Bengaluru, India",
  remote_policy: "hybrid",
  is_remote: false,
  employment_type: "full_time",
  salary_min: null,
  salary_max: null,
  salary_currency: null,
  salary_text: null,
  application_url: "https://boards.greenhouse.io/acme/jobs/1",
  posted_at: "2026-11-25T10:00:00Z",
  first_seen_at: "2026-11-25T10:00:00Z",
  last_seen_at: "2026-11-26T09:00:00Z",
  repost_count: 0,
  status: "active",
  requirements: null,
  match: null,
  raw_jd: "Build FastAPI and LangGraph workflows.",
  updated_at_source: null,
  expired_at: null,
  source_provenance: {},
};

const analyzedReqs = {
  role_category: "AI Engineer",
  location: "Bengaluru, India",
  remote_policy: "hybrid",
  employment_type: "full_time",
  required_skills: ["Python", "LangGraph"],
  preferred_skills: ["FastAPI"],
  trainable_skills: [],
  responsibilities: [],
  technical_stack: ["Python", "FastAPI"],
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
  extracted_at: "2026-11-26T09:05:00Z",
};

const analyzedMatch = {
  job_id: baseJob.id,
  profile_id: "p1",
  score: 91,
  eligible: true,
  hard_constraints_passed: true,
  required_skill_coverage: 1,
  preferred_skill_coverage: 1,
  semantic_similarity: 0.9,
  matched_skills: ["Python", "LangGraph"],
  missing_required_skills: [],
  preferred_gaps: [],
  evidence_mapping: [],
  missing_evidence: [],
  eligibility_blockers: [],
  uncertainties: [],
  confidence: 0.96,
  score_breakdown: {},
  recommendation: "strong_apply",
  explanation: "Strong match.",
  scored_at: "2026-11-26T09:06:00Z",
};

describe("Workflow-first frontend Stage 1 components", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    currentSearchParams = new URLSearchParams();
    currentPathname = "/";
  });

  it("renders minimal 4-item navigation shell", () => {
    render(
      <AppShell
        user={{
          id: "u1",
          email: "candidate@example.com",
          timezone: "Asia/Kolkata",
          is_active: true,
          created_at: "2026-11-26T00:00:00Z",
        }}
      >
        <div>Shell content</div>
      </AppShell>,
    );
    expect(screen.getByRole("link", { name: /My Workflows/i })).toHaveAttribute("href", "/");
    expect(screen.getByRole("link", { name: /Discover Jobs/i })).toHaveAttribute("href", "/discover");
    expect(screen.getByRole("link", { name: /Profile and Resumes/i })).toHaveAttribute(
      "href",
      "/profile",
    );
    expect(screen.getByRole("link", { name: /Settings/i })).toHaveAttribute("href", "/settings");
    expect(screen.getByText("candidate@example.com")).toBeInTheDocument();
  });

  it("renders My Workflows overview cards with company, role, step progress, and resume CTA", async () => {
    mockApi.listJobs.mockResolvedValue([
      { ...baseJob, requirements: analyzedReqs, match: analyzedMatch },
    ]);
    mockApi.listTailoredResumes.mockResolvedValue([]);
    mockApi.listApplications.mockResolvedValue([]);

    render(<MyWorkflowsView />);

    await waitFor(() => {
      expect(screen.getByText("Applied AI Engineer")).toBeInTheDocument();
    });
    expect(screen.getByText("Acme AI")).toBeInTheDocument();
    expect(screen.getByText(/Step 4 of 11/i)).toBeInTheDocument();
    expect(screen.getByText("27%")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Resume Workflow/i })).toHaveAttribute(
      "href",
      `/workflows/${baseJob.id}`,
    );
  });

  it("enforces prerequisite-gated navigation and executes JD analysis in WorkflowWorkspace", async () => {
    const user = userEvent.setup();
    let analyzed = false;

    mockApi.listApplications.mockResolvedValue([]);
    mockApi.getJobResumeHistory.mockResolvedValue({
      job_id: baseJob.id,
      versions: [],
      outbox_jobs: [],
    });
    mockApi.getJobDetail.mockImplementation(async () =>
      analyzed ? { ...baseJob, requirements: analyzedReqs, match: analyzedMatch } : baseJob,
    );
    mockApi.getJobIntelligence.mockImplementation(async (): Promise<JobIntelligenceResponse> => ({
      job_id: baseJob.id,
      company: baseJob.company,
      role_title: baseJob.role_title,
      status: baseJob.status,
      requirements: analyzedReqs,
      match: analyzedMatch,
      skill_coverage: {},
      score_disclaimer: "Rubric alignment score only.",
    }));
    mockApi.getJobEvidence.mockResolvedValue({
      job_id: baseJob.id,
      profile_id: "p1",
      evidence_mapping: [],
      missing_evidence: [],
      confirmed_missing_skills: [],
      jd_evidence_items: {},
    });
    mockApi.analyzeJob.mockImplementation(async () => {
      analyzed = true;
      return analyzedMatch;
    });

    render(<WorkflowWorkspace jobId={baseJob.id} />);

    await waitFor(() => {
      expect(
        screen.getByRole("heading", { name: "Job Description Analysis" }),
      ).toBeInTheDocument();
    });

    // Continue is blocked until JD analysis actually completes on backend
    const continueBtn = screen.getByRole("button", { name: /Continue to Step 3/i });
    expect(continueBtn).toBeDisabled();

    // Execute JD Analysis action
    await user.click(screen.getByRole("button", { name: /Analyze Job Description/i }));

    await waitFor(() => {
      expect(mockApi.analyzeJob).toHaveBeenCalledWith(baseJob.id, true);
    });
    await waitFor(() => {
      expect(screen.getByText(/Job description analyzed and saved/i)).toBeInTheDocument();
    });

    // Verify JD analysis & eligibility completed -> active step is now Step 4, so Previous Step opens Step 3 cleanly
    await waitFor(() => {
      expect(screen.getByRole("heading", { name: "Resume Tailoring" })).toBeInTheDocument();
    });
    await user.click(screen.getByRole("button", { name: /Previous Step/i }));
    expect(mockReplace).toHaveBeenCalledWith(`/workflows/${baseJob.id}?step=3`, {
      scroll: false,
    });
  });

  it("renders Hold-and-Confirm details and requires explicit confirmation + expected_payload_sha256", async () => {
    const user = userEvent.setup();
    const onAuthorize = vi.fn(async () => {});
    const onRefresh = vi.fn(async () => {});
    const digest = "a".repeat(64);
    const appDetail: ApplicationDetailResponse = {
      application: {
        id: "app-99",
        user_id: "u1",
        job_id: baseJob.id,
        resume_id: "res-1",
        canonical_job_key: "canon",
        idempotency_key: "idem",
        application_mode: "human_approved_browser",
        status: "MANUAL_ACTION_REQUIRED",
        destination_url: "https://boards.greenhouse.io/acme/jobs/1",
        thread_id: "thread-99",
        external_ref_id: null,
        confirmation_evidence: {},
        submitted_at: null,
        confirmed_at: null,
        next_followup_at: null,
        error_message: null,
        created_at: "2026-11-26T09:00:00Z",
        updated_at: "2026-11-26T09:10:00Z",
      },
      answers: [
        {
          id: "ans-s1",
          question_key: "work_auth",
          question: "Work authorization status?",
          is_sensitive: true,
          answer_preview: "[REDACTED — VERIFIED]",
          evidence_source: "user_input",
          user_verified: true,
          created_at: "2026-11-26T09:05:00Z",
        },
      ],
      approval_records: [],
      active_handoff_sessions: [
        {
          handoff_session_id: "hs-77",
          application_id: "app-99",
          status: "active",
          created_at: "2026-11-26T09:08:00Z",
          expires_at: "2099-11-26T09:38:00Z",
          manual_submit_armed: false,
          pending_submission: {
            pending_submission_id: "pend-55",
            target_url: "https://boards.greenhouse.io/acme/jobs/1/apply",
            http_method: "POST",
            payload_sha256: digest,
            detected_at: "2026-11-26T09:09:00Z",
            expires_at: "2099-11-26T09:11:00Z",
          },
        },
      ],
      execution_history: [],
    };

    render(
      <HoldAndConfirmPanel
        job={baseJob}
        applicationDetail={appDetail}
        approvedResume={null}
        resumeChecksum={"b".repeat(64)}
        activeHandoff={appDetail.active_handoff_sessions[0]}
        busy={false}
        onRefreshHandoff={onRefresh}
        onAuthorizeManualSubmit={onAuthorize}
      />,
    );

    expect(screen.getByText("pend-55")).toBeInTheDocument();
    expect(screen.getByText("https://boards.greenhouse.io/acme/jobs/1/apply")).toBeInTheDocument();
    expect(screen.getAllByText("POST").length).toBeGreaterThanOrEqual(1);

    const confirmBtn = screen.getByRole("button", {
      name: /Confirm & Release Held Submission/i,
    });
    expect(confirmBtn).toBeDisabled();

    await user.click(screen.getByRole("checkbox"));
    expect(confirmBtn).toBeEnabled();

    await user.click(confirmBtn);
    expect(onAuthorize).toHaveBeenCalledWith({
      handoff_session_id: "hs-77",
      pending_submission_id: "pend-55",
      expected_form_action: "https://boards.greenhouse.io/acme/jobs/1/apply",
      expected_http_method: "POST",
      expected_payload_sha256: digest,
    });
  });
});
