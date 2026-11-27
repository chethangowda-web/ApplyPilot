import React from "react";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { DiscoverJobsView } from "@/components/DiscoverJobsView";
import { WorkflowWorkspace } from "@/components/WorkflowWorkspace";
import { Stage5ResumeReview } from "@/components/Stage5ResumeReview";
import type {
  JobDetailResponse,
  JobResumeHistoryResponse,
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
  usePathname: () => "/discover",
}));

const mockApi = vi.hoisted(() => ({
  listJobs: vi.fn(),
  listJobSources: vi.fn(),
  listApplications: vi.fn(),
  triggerDiscovery: vi.fn(),
  getJobDetail: vi.fn(),
  getJobResumeHistory: vi.fn(),
  getJobIntelligence: vi.fn(),
  getJobEvidence: vi.fn(),
  listResumeVersions: vi.fn(),
  tailorResumeForJob: vi.fn(),
  getTailoredResumeDetail: vi.fn(),
  getTailoringDiff: vi.fn(),
  getResumeValidation: vi.fn(),
  approveTailoredResume: vi.fn(),
  rejectTailoredResume: vi.fn(),
  tailoredResumeDownloadUrl: vi.fn((id: string) => `/api/backend/resumes/${id}/download`),
  analyzeJob: vi.fn(),
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
    required_skills: ["Python", "LangGraph"],
    preferred_skills: ["FastAPI"],
    trainable_skills: ["Docker"],
    responsibilities: ["Design stateful agent pipelines"],
    technical_stack: ["Python", "LangGraph"],
    experience_min: 0,
    experience_max: 2,
    education_requirements: ["B.Tech Computer Science"],
    graduation_criteria: [],
    mandatory_qualifications: [{ value: "Strong Python fundamentals" }],
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
    score: 89,
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
    confidence: 0.94,
    score_breakdown: {},
    recommendation: "strong_apply",
    explanation: "Matches required skills and experience.",
    scored_at: "2026-11-26T06:11:00Z",
  },
  raw_jd: "Design stateful agent pipelines with Python and LangGraph.",
  updated_at_source: null,
  expired_at: null,
  source_provenance: {},
};

const masterResume: ResumeMetadataResponse = {
  id: "master-1",
  profile_id: "prof-1",
  is_master: true,
  original_filename: "chethan_master.pdf",
  content_type: "application/pdf",
  file_size_bytes: 24576,
  version_label: "master-v1",
  sha256_checksum: "a".repeat(64),
  template_version: "master-upload-v1",
  ats_report: {},
  parsed_metadata: {},
  validation_passed: true,
  created_at: "2026-11-26T05:00:00Z",
};

describe("Milestone 7 Stage 2 — Discovery and Resume Intelligence Workflow", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    currentSearchParams = new URLSearchParams();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("renders DiscoverJobsView with filters, source badge, existing application detection, and JD preview", async () => {
    const user = userEvent.setup();
    mockApi.listJobs.mockResolvedValue([sampleJob]);
    mockApi.listJobSources.mockResolvedValue([
      {
        id: "src-1",
        source_name: "Greenhouse AI",
        connector_type: "greenhouse",
        enabled: true,
        health_status: "healthy",
        consecutive_failures: 0,
        last_error: null,
        last_run_at: null,
      },
    ]);
    mockApi.listApplications.mockResolvedValue([
      {
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
        created_at: "2026-11-26T06:00:00Z",
        updated_at: "2026-11-26T06:00:00Z",
      },
    ]);
    mockApi.getJobDetail.mockResolvedValue(sampleJob);

    render(<DiscoverJobsView />);

    await waitFor(() => {
      expect(screen.getByText("Applied AI Engineer")).toBeInTheDocument();
    });
    expect(screen.getByText("Greenhouse AI (greenhouse)")).toBeInTheDocument();
    expect(screen.getByText("APP: AWAITING_APPROVAL")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Resume Workflow/i })).toHaveAttribute(
      "href",
      "/workflows/job-101",
    );

    // Toggle untrusted JD preview
    await user.click(screen.getByRole("button", { name: /Preview JD/i }));
    await waitFor(() => {
      expect(
        screen.getByText(/Design stateful agent pipelines with Python and LangGraph/i),
      ).toBeInTheDocument();
    });

    // Apply remote filter -> calls backend with remote_policy=remote
    await user.selectOptions(screen.getByLabelText("Remote policy filter"), "remote");
    await waitFor(() => {
      expect(mockApi.listJobs).toHaveBeenLastCalledWith(
        expect.objectContaining({ remote_policy: "remote", offset: 0 }),
      );
    });
  });

  it("distinguishes hard eligibility failure from low score and blocks Step 4 progression", async () => {
    currentSearchParams = new URLSearchParams("step=3");
    const ineligibleJob: JobDetailResponse = {
      ...sampleJob,
      match: {
        ...sampleJob.match!,
        score: 82,
        eligible: false,
        hard_constraints_passed: false,
        eligibility_blockers: ["Requires 5+ years production experience"],
        recommendation: "do_not_apply",
        explanation: "Blocked by experience cap despite high keyword score.",
      },
    };
    mockApi.getJobDetail.mockResolvedValue(ineligibleJob);
    mockApi.listApplications.mockResolvedValue([]);
    mockApi.listResumeVersions.mockResolvedValue([masterResume]);
    mockApi.getJobResumeHistory.mockResolvedValue({
      job_id: "job-101",
      versions: [],
      outbox_jobs: [],
    });
    mockApi.getJobIntelligence.mockResolvedValue({
      job_id: "job-101",
      company: ineligibleJob.company,
      role_title: ineligibleJob.role_title,
      status: "active",
      requirements: ineligibleJob.requirements,
      match: ineligibleJob.match!,
      skill_coverage: {},
      score_disclaimer: "Rubric alignment score only.",
    });
    mockApi.getJobEvidence.mockResolvedValue({
      job_id: "job-101",
      profile_id: "prof-1",
      evidence_mapping: [],
      missing_evidence: [],
      confirmed_missing_skills: [],
      jd_evidence_items: {},
    });

    render(<WorkflowWorkspace jobId="job-101" />);

    await waitFor(() => {
      expect(
        screen.getByText(/Ineligible Role — Hard Constraints Failed/i),
      ).toBeInTheDocument();
    });
    expect(screen.getByText("82")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Continue to Step 4/i })).toBeDisabled();
  });

  it("triggers resume tailoring once, prevents duplicates while running, and recovers state after reload", async () => {
    const user = userEvent.setup();
    let histState: JobResumeHistoryResponse = {
      job_id: "job-101",
      versions: [],
      outbox_jobs: [],
    };

    mockApi.getJobDetail.mockResolvedValue(sampleJob);
    mockApi.listApplications.mockResolvedValue([]);
    mockApi.listResumeVersions.mockResolvedValue([masterResume]);
    mockApi.getJobIntelligence.mockResolvedValue({
      job_id: "job-101",
      company: sampleJob.company,
      role_title: sampleJob.role_title,
      status: "active",
      requirements: sampleJob.requirements,
      match: sampleJob.match!,
      skill_coverage: {},
      score_disclaimer: "Rubric alignment score only.",
    });
    mockApi.getJobEvidence.mockResolvedValue({
      job_id: "job-101",
      profile_id: "prof-1",
      evidence_mapping: [],
      missing_evidence: [],
      confirmed_missing_skills: [],
      jd_evidence_items: {},
    });
    mockApi.getJobResumeHistory.mockImplementation(async () => histState);
    mockApi.tailorResumeForJob.mockImplementation(async () => {
      histState = {
        job_id: "job-101",
        versions: [],
        outbox_jobs: [
          {
            id: "outbox-1",
            job_id: "job-101",
            profile_id: "prof-1",
            trigger_mode: "manual",
            evidence_fingerprint: "fp-1",
            quota_date: "2026-11-26",
            status: "running",
            attempt_count: 1,
            max_attempts: 3,
            thread_id: "th-tailor",
            resume_id: null,
            error_message: null,
            created_at: "2026-11-26T06:15:00Z",
            started_at: "2026-11-26T06:15:01Z",
            completed_at: null,
          },
        ],
      };
      return {
        accepted: true,
        created: true,
        idempotency_hit: false,
        status: "queued",
        reason: "queued",
        job_id: "job-101",
        generation_job_id: "outbox-1",
        dispatched_to_broker: true,
      };
    });

    const { unmount } = render(<WorkflowWorkspace jobId="job-101" />);

    await waitFor(() => {
      expect(screen.getByRole("button", { name: /Generate Tailored Resume/i })).toBeEnabled();
    });

    await user.click(screen.getByRole("button", { name: /Generate Tailored Resume/i }));
    await waitFor(() => {
      expect(mockApi.tailorResumeForJob).toHaveBeenCalledTimes(1);
      expect(screen.getByRole("button", { name: /Worker Generating/i })).toBeDisabled();
    });

    // Simulate browser reload while outbox job is still running -> recovers waiting state without re-triggering
    unmount();
    render(<WorkflowWorkspace jobId="job-101" />);

    await waitFor(() => {
      expect(screen.getByText(/Celery worker is generating/i)).toBeInTheDocument();
    });
    expect(mockApi.tailorResumeForJob).toHaveBeenCalledTimes(1);
  });

  it("renders Stage 5 comparison, unsupported claims, ATS metrics, and explicit approve/reject", async () => {
    const user = userEvent.setup();
    const onApprove = vi.fn(async () => {});
    const onReject = vi.fn(async () => {});

    const detail: TailoredResumeDetailResponse = {
      resume_id: "res-55",
      mapping_id: "map-55",
      job_id: "job-101",
      master_resume_id: "master-1",
      version_number: 1,
      version_label: "tailored-v1",
      is_active: true,
      status: "completed",
      approval_status: "pending_review",
      template_version: "ats-v1",
      evidence_fingerprint: "fp-55",
      validation_passed: false,
      evaluation_report: {
        required_skill_coverage: 0.9,
        factual_consistency_score: 0.85,
        pdf_parsing_quality_score: 1.0,
        disclaimer: "Deterministic alignment estimate only.",
      },
      created_at: "2026-11-26T06:20:00Z",
      run_id: "run-55",
      trigger_mode: "manual",
      sha256_checksum: "c".repeat(64),
      file_size_bytes: 18000,
      ats_report: { detected_heading_count: 5 },
      parsed_metadata: {},
      validation_report: {},
      tailoring_diff: {},
      token_usage: {},
      model_provider: "claude",
      approval_notes: null,
      approved_at: null,
      rejected_at: null,
    };

    render(
      <Stage5ResumeReview
        activeResume={{
          mapping_id: "map-55",
          resume_id: "res-55",
          job_id: "job-101",
          master_resume_id: "master-1",
          generation_job_id: "outbox-1",
          run_id: "run-55",
          trigger_mode: "manual",
          version_number: 1,
          is_active: true,
          evidence_fingerprint: "fp-55",
          status: "completed",
          approval_status: "pending_review",
          approval_notes: null,
          approved_at: null,
          rejected_at: null,
          model_provider: "claude",
          token_usage: {},
          error_message: null,
          created_at: "2026-11-26T06:20:00Z",
        }}
        tailoredDetail={detail}
        tailoringDiff={{
          resume_id: "res-55",
          mapping_id: "map-55",
          job_id: "job-101",
          version_number: 1,
          evidence_fingerprint: "fp-55",
          tailoring_diff: {
            keywords_emphasized: [{ keyword: "LangGraph", section: "skills" }],
            project_rewrites: [
              {
                project_id: "p-1",
                name: "ApplyPilot",
                before_summary: "Built scripts.",
                after_summary: "Engineered stateful LangGraph workflows.",
              },
            ],
          },
        }}
        validationReport={{
          resume_id: "res-55",
          mapping_id: "map-55",
          job_id: "job-101",
          validation_passed: false,
          validation_report: {
            supported_count: 8,
            rewritten_count: 2,
            unsupported_count: 1,
            unsupported_claims: [
              {
                claim_id: "c-1",
                claim_text: "Managed 50k RPS cluster",
                violation_reason: "Metric not present in verified profile",
              },
            ],
          },
          ats_report: { detected_heading_count: 5 },
        }}
        actionBusy={false}
        onApproveResume={onApprove}
        onRejectResume={onReject}
      />,
    );

    expect(screen.getByText("VALIDATION FAILED")).toBeInTheDocument();
    expect(screen.getByText(/Managed 50k RPS cluster/i)).toBeInTheDocument();
    expect(screen.getByText("Built scripts.")).toBeInTheDocument();
    expect(screen.getByText("Engineered stateful LangGraph workflows.")).toBeInTheDocument();
    expect(screen.getByText("90%")).toBeInTheDocument();

    await user.type(
      screen.getByPlaceholderText(/Optional review note/i),
      "Rejected due to unsupported metric",
    );
    await user.click(screen.getByRole("button", { name: /Reject Version/i }));
    expect(onReject).toHaveBeenCalledWith("res-55", "Rejected due to unsupported metric");

    await user.click(screen.getByRole("button", { name: /Approve Tailored Resume/i }));
    expect(onApprove).toHaveBeenCalledWith("res-55", "Rejected due to unsupported metric");
  });
});
