import React from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { Stage11VerificationPanel } from "@/components/Stage11VerificationPanel";
import { ApplicationsTrackerView } from "@/components/ApplicationsTrackerView";
import { ApplicationDetailTimelineView } from "@/components/ApplicationDetailTimelineView";
import type {
  ApplicationDetailResponse,
  ApplicationResponse,
  JobDetailResponse,
  JobSummaryResponse,
} from "@/lib/types";
import type { StageDerivation } from "@/lib/workflow-engine";

const mockPush = vi.fn();
const mockReplace = vi.fn();

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: mockPush, replace: mockReplace, refresh: vi.fn() }),
}));

const mockApi = vi.hoisted(() => ({
  listApplications: vi.fn(),
  listJobs: vi.fn(),
  listTailoredResumes: vi.fn(),
  getApplicationDetail: vi.fn(),
  getJobDetail: vi.fn(),
  getJobResumeHistory: vi.fn(),
  getTailoredResumeDetail: vi.fn(),
  recordSubmitOutcome: vi.fn(),
  reconcileApplication: vi.fn(),
}));

vi.mock("@/lib/api-client", () => ({
  ApiError: class ApiError extends Error {
    status: number;
    constructor(status: number, message: string) {
      super(message);
      this.status = status;
    }
    isUnauthorized() {
      return this.status === 401;
    }
  },
  apiClient: mockApi,
}));

function makeApp(id: string, status: ApplicationResponse["status"], jobId = "job-1"): ApplicationResponse {
  return {
    id,
    user_id: "u-1",
    job_id: jobId,
    resume_id: "res-1",
    canonical_job_key: `gh:${id}`,
    idempotency_key: `idem-${id}`,
    application_mode: "human_approved_browser",
    status,
    destination_url: "https://boards.greenhouse.io/acme/jobs/101",
    thread_id: `app-${id}`,
    external_ref_id: status === "CONFIRMED" ? "rcpt-999" : null,
    confirmation_evidence: {},
    submitted_at: status === "USER_REPORTED_SUBMITTED" ? "2026-11-26T10:00:00Z" : null,
    confirmed_at: status === "CONFIRMED" ? "2026-11-26T10:05:00Z" : null,
    next_followup_at: null,
    error_message: status === "UNVERIFIED" ? "Worker disconnected after authorized release." : null,
    created_at: "2026-11-26T09:00:00Z",
    updated_at: "2026-11-26T10:00:00Z",
  };
}

const sampleStage: StageDerivation = {
  definition: {
    stepNumber: 11,
    id: "verification-tracking",
    title: "Submission Verification & Tracking",
    shortTitle: "Verification",
    description: "Record outcome & verify receipt.",
    gateRule: "Requires handoff.",
  },
  state: "waiting",
  statusSummary: "Waiting",
  blockerReason: null,
  canInspect: true,
};

const sampleJobSummary: JobSummaryResponse = {
  id: "job-1",
  source_id: "src-1",
  source_job_id: "101",
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
  last_seen_at: "2026-11-26T00:00:00Z",
  repost_count: 0,
  status: "active",
  requirements: null,
  match: null,
};

describe("Milestone 7 Stage 5 — Submission Verification & Application Tracking", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("renders Stage11VerificationPanel outcome recording, UNVERIFIED handling, and reconciliation boundary", async () => {
    const onOutcome = vi.fn(async () => {});
    const onReconcile = vi.fn(async () => {});

    const detail: ApplicationDetailResponse = {
      application: makeApp("app-1", "MANUAL_ACTION_REQUIRED"),
      answers: [],
      approval_records: [],
      active_handoff_sessions: [],
      execution_history: [
        {
          action: "application.manual_submit_authorized",
          actor: "user:u-1",
          created_at: "2026-11-26T09:55:00Z",
          metadata: {},
        },
      ],
    };

    const { rerender } = render(
      <Stage11VerificationPanel
        applicationDetail={detail}
        stage={sampleStage}
        actionBusy={false}
        onRecordOutcome={onOutcome}
        onReconcileConfirmation={onReconcile}
      />,
    );

    expect(
      screen.getByText(/establish independently verified employer receipt/i),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Report Submitted in Browser/i }));
    await waitFor(() => expect(onOutcome).toHaveBeenCalledWith("submitted_in_browser", ""));

    // UNVERIFIED state shows uncertainty explanation, last known event, and reconciliation form
    rerender(
      <Stage11VerificationPanel
        applicationDetail={{ ...detail, application: makeApp("app-1", "UNVERIFIED") }}
        stage={sampleStage}
        actionBusy={false}
        onRecordOutcome={onOutcome}
        onReconcileConfirmation={onReconcile}
      />,
    );

    expect(screen.getByText(/Uncertain Submission Outcome \(UNVERIFIED\)/i)).toBeInTheDocument();
    expect(screen.getByText(/application\.manual_submit_authorized/i)).toBeInTheDocument();
    expect(screen.getByText(/_CONFIRMATION_VERIFIER_REGISTRY/i)).toBeInTheDocument();

    fireEvent.change(screen.getByPlaceholderText(/Receipt ID/i), {
      target: { value: "msg-123" },
    });
    fireEvent.click(screen.getByRole("button", { name: /Submit for Trusted Verification/i }));
    await waitFor(() =>
      expect(onReconcile).toHaveBeenCalledWith("msg-123", "verified_email_confirmation"),
    );
  });

  it("renders ApplicationsTrackerView with distinct status badges and working filters", async () => {
    mockApi.listApplications.mockResolvedValue([
      makeApp("app-1", "APPROVED"),
      makeApp("app-2", "USER_REPORTED_SUBMITTED"),
      makeApp("app-3", "UNVERIFIED"),
      makeApp("app-4", "CONFIRMED"),
      makeApp("app-5", "FAILED"),
    ]);
    mockApi.listJobs.mockResolvedValue([sampleJobSummary]);
    mockApi.listTailoredResumes.mockResolvedValue([]);

    render(<ApplicationsTrackerView />);

    await waitFor(() => {
      expect(screen.getByText("My Applications Tracker")).toBeInTheDocument();
    });
    expect(screen.getByText("All (5)")).toBeInTheDocument();
    expect(screen.getByText("Submitted (User Reported) (1)")).toBeInTheDocument();
    expect(screen.getByText("Unverified (1)")).toBeInTheDocument();
    expect(screen.getByText("Confirmed (1)")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("tab", { name: /Unverified \(1\)/i }));
    expect(screen.getByText(/Submission uncertain/i)).toBeInTheDocument();
  });

  it("renders ApplicationDetailTimelineView with backend-derived timestamps without inventing missing events", async () => {
    const detail: ApplicationDetailResponse = {
      application: makeApp("app-1", "USER_REPORTED_SUBMITTED"),
      answers: [],
      approval_records: [
        {
          id: "rec-1",
          application_id: "app-1",
          decision: "approved",
          actor: "user:u-1",
          resume_id: "res-1",
          resume_checksum: "a".repeat(64),
          destination_url: "https://boards.greenhouse.io/acme/jobs/101",
          application_mode: "human_approved_browser",
          approval_snapshot_hash: "b".repeat(64),
          notes: null,
          decided_at: "2026-11-26T09:30:00Z",
          expires_at: "2099-11-27T09:30:00Z",
        },
      ],
      active_handoff_sessions: [],
      execution_history: [
        {
          action: "application.prepared",
          actor: "user:u-1",
          created_at: "2026-11-26T09:10:00Z",
          metadata: {},
        },
      ],
    };
    const jobDetail: JobDetailResponse = {
      ...sampleJobSummary,
      raw_jd: "JD",
      updated_at_source: null,
      expired_at: null,
      source_provenance: {},
    };
    mockApi.getApplicationDetail.mockResolvedValue(detail);
    mockApi.getJobDetail.mockResolvedValue(jobDetail);
    mockApi.getJobResumeHistory.mockResolvedValue([]);
    mockApi.getTailoredResumeDetail.mockResolvedValue(null);

    render(<ApplicationDetailTimelineView applicationId="app-1" />);

    await waitFor(() => {
      expect(screen.getByText("Backend-Derived Application Timeline")).toBeInTheDocument();
    });
    expect(screen.getByText("Application approved")).toBeInTheDocument();
    expect(screen.getByText(/Missing independent verifier receipt/i)).toBeInTheDocument();
  });
});
