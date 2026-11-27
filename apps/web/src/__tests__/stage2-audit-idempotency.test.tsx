import React from "react";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Stage5ResumeReview } from "@/components/Stage5ResumeReview";
import { WorkflowWorkspace } from "@/components/WorkflowWorkspace";
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
  usePathname: () => "/workflows/job-101",
}));

const mockApi = vi.hoisted(() => ({
  listJobs: vi.fn(),
  listJobSources: vi.fn(),
  listApplications: vi.fn(async () => []),
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
  tailoredResumeDownloadUrl: vi.fn(
    (id: string, approvedOnly = false) =>
      `/api/backend/resumes/${id}/download?approved_only=${approvedOnly}`,
  ),
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

function makeJob(id: string, company: string, role: string, analyzed = true): JobDetailResponse {
  return {
    id,
    source_id: "src-1",
    source_job_id: id,
    canonical_url: `https://boards.greenhouse.io/acme/jobs/${id}`,
    company,
    role_title: role,
    location: "Bengaluru, India",
    remote_policy: "hybrid",
    is_remote: false,
    employment_type: "full_time",
    salary_min: null,
    salary_max: null,
    salary_currency: null,
    salary_text: null,
    application_url: `https://boards.greenhouse.io/acme/jobs/${id}`,
    posted_at: "2026-11-26T00:00:00Z",
    first_seen_at: "2026-11-26T00:00:00Z",
    last_seen_at: "2026-11-26T06:00:00Z",
    repost_count: 0,
    status: "active",
    requirements: analyzed
      ? {
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
        }
      : null,
    match: analyzed
      ? {
          job_id: id,
          profile_id: "prof-1",
          score: 90,
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
          explanation: "Strong match.",
          scored_at: "2026-11-26T06:11:00Z",
        }
      : null,
    raw_jd: "Build Python AI systems.",
    updated_at_source: null,
    expired_at: null,
    source_provenance: {},
  };
}

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

describe("Stage 2 Final Audit — PDF security, idempotency, polling, and workflow isolation", () => {
  const originalFetch = global.fetch;

  beforeEach(() => {
    vi.clearAllMocks();
    currentSearchParams = new URLSearchParams();
  });

  afterEach(() => {
    global.fetch = originalFetch;
    vi.useRealTimers();
  });

  it("loads PDF preview via authenticated proxy fetch into blob URL and cleans up without leaking paths/tokens", async () => {
    const user = userEvent.setup();
    const origCreate = URL.createObjectURL;
    const origRevoke = URL.revokeObjectURL;
    const createMock = vi.fn(() => "blob:http://localhost/pdf-1");
    const revokeMock = vi.fn();
    URL.createObjectURL = createMock;
    URL.revokeObjectURL = revokeMock;

    global.fetch = vi.fn(async () => {
      return new Response(new Uint8Array([0x25, 0x50, 0x44, 0x46]), {
        status: 200,
        headers: { "Content-Type": "application/pdf" },
      });
    }) as unknown as typeof fetch;

    const detail: TailoredResumeDetailResponse = {
      resume_id: "res-1",
      mapping_id: "map-1",
      job_id: "job-101",
      master_resume_id: "master-1",
      version_number: 1,
      version_label: "tailored-v1",
      is_active: true,
      status: "completed",
      approval_status: "pending_review",
      template_version: "ats-v1",
      evidence_fingerprint: "fp-1",
      validation_passed: true,
      evaluation_report: {},
      created_at: "2026-11-26T06:20:00Z",
      run_id: "run-1",
      trigger_mode: "manual",
      sha256_checksum: "c".repeat(64),
      file_size_bytes: 4,
      ats_report: {},
      parsed_metadata: {},
      validation_report: {},
      tailoring_diff: {},
      token_usage: {},
      model_provider: "claude",
      approval_notes: null,
      approved_at: null,
      rejected_at: null,
    };

    const { unmount } = render(
      <Stage5ResumeReview
        activeResume={{
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
        tailoringDiff={null}
        validationReport={null}
        actionBusy={false}
        onApproveResume={vi.fn(async () => {})}
        onRejectResume={vi.fn(async () => {})}
      />,
    );

    await user.click(screen.getByRole("button", { name: /Preview PDF/i }));
    await waitFor(() => {
      expect(screen.getByTitle(/Tailored resume v1 PDF preview/i)).toHaveAttribute(
        "src",
        "blob:http://localhost/pdf-1",
      );
    });
    expect(global.fetch).toHaveBeenCalledWith(
      "/api/backend/resumes/res-1/download?approved_only=false",
      { cache: "no-store" },
    );

    unmount();
    expect(revokeMock).toHaveBeenCalledWith("blob:http://localhost/pdf-1");
    URL.createObjectURL = origCreate;
    URL.revokeObjectURL = origRevoke;
  });

  it("prevents duplicate generation on double-click and recovers through multiple Celery polling cycles", async () => {
    const jobA = makeJob("job-101", "Acme AI", "AI Engineer", true);
    let pollStage = 0;
    let resolveTailor: (() => void) | null = null;

    mockApi.getJobDetail.mockResolvedValue(jobA);
    mockApi.listResumeVersions.mockResolvedValue([masterResume]);
    mockApi.getJobIntelligence.mockResolvedValue({
      job_id: "job-101",
      company: "Acme AI",
      role_title: "AI Engineer",
      status: "active",
      requirements: jobA.requirements,
      match: jobA.match!,
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

    mockApi.getJobResumeHistory.mockImplementation(async (): Promise<JobResumeHistoryResponse> => {
      if (pollStage === 0) {
        return { job_id: "job-101", versions: [], outbox_jobs: [] };
      }
      if (pollStage === 1) {
        return {
          job_id: "job-101",
          versions: [],
          outbox_jobs: [
            {
              id: "out-1",
              job_id: "job-101",
              profile_id: "prof-1",
              trigger_mode: "manual",
              evidence_fingerprint: "fp-1",
              quota_date: "2026-11-26",
              status: "queued",
              attempt_count: 0,
              max_attempts: 3,
              thread_id: "th-1",
              resume_id: null,
              error_message: null,
              created_at: "2026-11-26T06:15:00Z",
              started_at: null,
              completed_at: null,
            },
          ],
        };
      }
      if (pollStage === 2) {
        return {
          job_id: "job-101",
          versions: [],
          outbox_jobs: [
            {
              id: "out-1",
              job_id: "job-101",
              profile_id: "prof-1",
              trigger_mode: "manual",
              evidence_fingerprint: "fp-1",
              quota_date: "2026-11-26",
              status: "running",
              attempt_count: 1,
              max_attempts: 3,
              thread_id: "th-1",
              resume_id: null,
              error_message: null,
              created_at: "2026-11-26T06:15:00Z",
              started_at: "2026-11-26T06:15:02Z",
              completed_at: null,
            },
          ],
        };
      }
      return {
        job_id: "job-101",
        outbox_jobs: [
          {
            id: "out-1",
            job_id: "job-101",
            profile_id: "prof-1",
            trigger_mode: "manual",
            evidence_fingerprint: "fp-1",
            quota_date: "2026-11-26",
            status: "completed",
            attempt_count: 1,
            max_attempts: 3,
            thread_id: "th-1",
            resume_id: "res-1",
            error_message: null,
            created_at: "2026-11-26T06:15:00Z",
            started_at: "2026-11-26T06:15:02Z",
            completed_at: "2026-11-26T06:15:05Z",
          },
        ],
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
            approval_status: "pending_review",
            approval_notes: null,
            approved_at: null,
            rejected_at: null,
            model_provider: "claude",
            token_usage: {},
            error_message: null,
            created_at: "2026-11-26T06:15:05Z",
          },
        ],
      };
    });

    mockApi.tailorResumeForJob.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveTailor = () => {
            pollStage = 1;
            resolve({
              accepted: true,
              created: true,
              idempotency_hit: false,
              status: "queued",
              reason: "queued",
              job_id: "job-101",
              generation_job_id: "out-1",
              dispatched_to_broker: true,
            });
          };
        }),
    );

    render(<WorkflowWorkspace jobId="job-101" />);

    const genBtn = await screen.findByRole("button", { name: /Generate Tailored Resume/i });

    // Rapid double-click must only trigger ONE tailor request
    fireEvent.click(genBtn);
    fireEvent.click(genBtn);
    expect(mockApi.tailorResumeForJob).toHaveBeenCalledTimes(1);

    vi.useFakeTimers({ shouldAdvanceTime: true });
    await act(async () => {
      resolveTailor?.();
    });

    await waitFor(() => {
      expect(screen.getAllByText("TAILOR_QUEUED").length).toBeGreaterThanOrEqual(1);
    });

    // Polling cycle 1 -> running
    pollStage = 2;
    await act(async () => {
      vi.advanceTimersByTime(2600);
    });
    await waitFor(() => {
      expect(screen.getAllByText("TAILOR_RUNNING").length).toBeGreaterThanOrEqual(1);
    });

    // Polling cycle 2 -> completed (lands on Step 5 pending_review, never auto-approved)
    pollStage = 3;
    await act(async () => {
      vi.advanceTimersByTime(2600);
    });
    await waitFor(() => {
      expect(screen.getByRole("heading", { name: "Resume Review & Approval" })).toBeInTheDocument();
    });
    expect(screen.getByText("PENDING_REVIEW")).toBeInTheDocument();
    expect(mockApi.tailorResumeForJob).toHaveBeenCalledTimes(1);
  });

  it("prevents duplicate approval/rejection clicks and isolates state when switching independent jobs", async () => {
    const jobA = makeJob("job-101", "Acme AI", "AI Engineer", true);
    const jobB = makeJob("job-202", "Beta Corp", "Backend Engineer", false);

    mockApi.listResumeVersions.mockResolvedValue([masterResume]);
    mockApi.getJobDetail.mockImplementation(async (id: string) => (id === "job-101" ? jobA : jobB));
    mockApi.getJobIntelligence.mockImplementation(async (id: string) => ({
      job_id: id,
      company: id === "job-101" ? "Acme AI" : "Beta Corp",
      role_title: id === "job-101" ? "AI Engineer" : "Backend Engineer",
      status: "active",
      requirements: jobA.requirements,
      match: jobA.match!,
      skill_coverage: {},
      score_disclaimer: "Rubric only.",
    }));
    mockApi.getJobEvidence.mockResolvedValue({
      job_id: "job-101",
      profile_id: "prof-1",
      evidence_mapping: [],
      missing_evidence: [],
      confirmed_missing_skills: [],
      jd_evidence_items: {},
    });

    let approved = false;
    mockApi.getJobResumeHistory.mockImplementation(async (id: string) => {
      if (id !== "job-101") {
        return { job_id: id, versions: [], outbox_jobs: [] };
      }
      return {
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
            approval_status: approved ? "approved" : "pending_review",
            approval_notes: null,
            approved_at: approved ? "2026-11-26T06:21:00Z" : null,
            rejected_at: null,
            model_provider: "claude",
            token_usage: {},
            error_message: null,
            created_at: "2026-11-26T06:20:00Z",
          },
        ],
      };
    });

    let resolveApprove: (() => void) | null = null;
    mockApi.approveTailoredResume.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveApprove = () => {
            approved = true;
            resolve({
              mapping_id: "map-1",
              resume_id: "res-1",
              job_id: "job-101",
              version_number: 1,
              status: "completed",
              approval_status: "approved",
              approval_notes: null,
              approved_at: "2026-11-26T06:21:00Z",
              rejected_at: null,
              idempotent: false,
            });
          };
        }),
    );

    const { rerender } = render(<WorkflowWorkspace jobId="job-101" />);

    const approveBtn = await screen.findByRole("button", { name: /Approve Tailored Resume/i });
    fireEvent.click(approveBtn);
    fireEvent.click(approveBtn);
    expect(mockApi.approveTailoredResume).toHaveBeenCalledTimes(1);

    await act(async () => {
      resolveApprove?.();
    });

    await waitFor(() => {
      expect(screen.getByText(/Tailored resume explicitly approved by owner/i)).toBeInTheDocument();
    });

    // Switch to independent unanalyzed job-202 -> must start at Step 2 with zero leakage from job-101
    rerender(<WorkflowWorkspace jobId="job-202" />);
    await waitFor(() => {
      expect(screen.getByText(/Beta Corp — Backend Engineer/i)).toBeInTheDocument();
    });
    expect(screen.getByRole("heading", { name: "Job Description Analysis" })).toBeInTheDocument();
    expect(screen.queryByText(/Tailored resume explicitly approved/i)).not.toBeInTheDocument();
  });
});
