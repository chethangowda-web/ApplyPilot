import React from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { Stage9HandoffPanel } from "@/components/Stage9HandoffPanel";
import { HoldAndConfirmPanel } from "@/components/HoldAndConfirmPanel";
import type {
  ApplicationDetailResponse,
  ExecutionResult,
  HandoffSessionInfo,
  JobDetailResponse,
  ResumeGenerationHistoryResponse,
  TailoredResumeDetailResponse,
} from "@/lib/types";

const sampleJob: JobDetailResponse = {
  id: "job-101",
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
  status: "active",
  posted_at: "2026-11-26T00:00:00Z",
  first_seen_at: "2026-11-26T00:00:00Z",
  last_seen_at: "2026-11-26T00:00:00Z",
  repost_count: 0,
  match: null,
  requirements: null,
  raw_jd: "Applied AI Engineer JD",
  updated_at_source: null,
  expired_at: null,
  source_provenance: {},
};

const approvedResume: ResumeGenerationHistoryResponse = {
  mapping_id: "map-1",
  resume_id: "res-1",
  job_id: "job-101",
  master_resume_id: "master-1",
  generation_job_id: "gen-1",
  run_id: "run-1",
  trigger_mode: "manual",
  version_number: 2,
  is_active: true,
  evidence_fingerprint: "fp-1",
  status: "completed",
  approval_status: "approved",
  approval_notes: null,
  approved_at: "2026-11-26T05:00:00Z",
  rejected_at: null,
  model_provider: "claude",
  token_usage: {},
  error_message: null,
  created_at: "2026-11-26T05:00:00Z",
};

const tailoredDetail: TailoredResumeDetailResponse = {
  resume_id: "res-1",
  mapping_id: "map-1",
  job_id: "job-101",
  master_resume_id: "master-1",
  version_number: 2,
  version_label: "tailored-v2",
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
  sha256_checksum: "a".repeat(64),
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

const baseAppDetail: ApplicationDetailResponse = {
  application: {
    id: "app-1",
    user_id: "user-1",
    job_id: "job-101",
    resume_id: "res-1",
    canonical_job_key: "greenhouse:101",
    idempotency_key: "idem-1",
    application_mode: "human_approved_browser",
    status: "APPROVED",
    destination_url: "https://boards.greenhouse.io/acme/jobs/101",
    thread_id: "app-1",
    external_ref_id: null,
    confirmation_evidence: {},
    submitted_at: null,
    confirmed_at: null,
    next_followup_at: null,
    error_message: null,
    created_at: "2026-11-26T05:00:00Z",
    updated_at: "2026-11-26T05:00:00Z",
  },
  answers: [
    {
      id: "ans-1",
      question_key: "full_name",
      question: "Full Name",
      is_sensitive: false,
      answer_preview: "Aarav Mehta",
      evidence_source: "candidate_profiles.full_name",
      user_verified: true,
      created_at: "2026-11-26T05:00:00Z",
    },
    {
      id: "ans-2",
      question_key: "work_authorization",
      question: "Work Authorization",
      is_sensitive: true,
      answer_preview: "[REDACTED - VERIFIED]",
      evidence_source: "user_input",
      user_verified: true,
      created_at: "2026-11-26T05:00:00Z",
    },
  ],
  approval_records: [],
  active_handoff_sessions: [],
  execution_history: [],
};

describe("Milestone 7 Stage 4 — Browser Handoff & Hold-and-Confirm", () => {
  it("supports launching, refreshing, closing, and field outcome inspection in Stage9HandoffPanel", async () => {
    const onExecute = vi.fn(async () => {});
    const onRefresh = vi.fn(async () => {});
    const onClose = vi.fn(async () => {});

    const { rerender } = render(
      <Stage9HandoffPanel
        job={sampleJob}
        applicationDetail={baseAppDetail}
        activeResume={approvedResume}
        tailoredDetail={tailoredDetail}
        activeHandoff={null}
        executionResult={null}
        actionBusy={false}
        onExecuteApplication={onExecute}
        onRefreshHandoff={onRefresh}
        onCloseHandoff={onClose}
      />,
    );

    expect(screen.getByText(/Final Submit Remains Under Your Control/i)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Launch Visible Browser Handoff/i }));
    await waitFor(() => expect(onExecute).toHaveBeenCalledWith("visible_handoff"));

    const activeSession: HandoffSessionInfo = {
      handoff_session_id: "sess-1",
      application_id: "app-1",
      status: "active",
      created_at: "2026-11-26T06:00:00Z",
      expires_at: "2099-11-26T06:30:00Z",
      manual_submit_armed: false,
      pending_submission: null,
    };

    const execResult: ExecutionResult = {
      application_id: "app-1",
      status: "MANUAL_ACTION_REQUIRED",
      adapter_name: "playwright",
      mode: "human_approved_browser",
      browser_mode: "visible_handoff",
      stopped_before_submit: true,
      blocked_submit_attempts: 0,
      restriction_detected: null,
      detected_form_action: "https://boards.greenhouse.io/acme/jobs/101/submit",
      detected_http_method: "POST",
      fields_filled: [
        { question_key: "full_name", status: "filled", reason: null },
        { question_key: "custom_essay", status: "skipped_missing", reason: "No verified answer" },
      ],
      unsupported_fields: ["portfolio_video"],
      resume_attached: true,
      resume_checksum: "a".repeat(64),
      os_firewall_enforced: true,
      artifact_ids: [],
      handoff_session: activeSession,
      error_message: null,
    };

    rerender(
      <Stage9HandoffPanel
        job={sampleJob}
        applicationDetail={{
          ...baseAppDetail,
          application: { ...baseAppDetail.application, status: "MANUAL_ACTION_REQUIRED" },
          active_handoff_sessions: [activeSession],
        }}
        activeResume={approvedResume}
        tailoredDetail={tailoredDetail}
        activeHandoff={activeSession}
        executionResult={execResult}
        actionBusy={false}
        onExecuteApplication={onExecute}
        onRefreshHandoff={onRefresh}
        onCloseHandoff={onClose}
      />,
    );

    // No duplicate launch button when active handoff is open
    expect(
      screen.queryByRole("button", { name: /Launch Visible Browser Handoff/i }),
    ).not.toBeInTheDocument();
    expect(screen.getByText(/full_name/i)).toBeInTheDocument();
    expect(screen.getByText(/portfolio_video/i)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /Refresh Session Status/i }));
    fireEvent.click(screen.getByRole("button", { name: /Close Handoff Safely/i }));
    expect(onRefresh).toHaveBeenCalledWith("sess-1");
    expect(onClose).toHaveBeenCalledWith("sess-1");
  });

  it("displays captured wire preview separately from approved answers and binds Hold-and-Confirm", async () => {
    const onAuthorize = vi.fn(async () => {});
    const pendingSha = "b".repeat(64);
    const activeHandoff: HandoffSessionInfo = {
      handoff_session_id: "sess-1",
      application_id: "app-1",
      status: "active",
      created_at: "2026-11-26T06:00:00Z",
      expires_at: "2099-11-26T06:30:00Z",
      manual_submit_armed: false,
      pending_submission: {
        pending_submission_id: "pend-1",
        target_url: "https://boards.greenhouse.io/acme/jobs/101/submit",
        http_method: "POST",
        payload_sha256: pendingSha,
        detected_at: "2026-11-26T06:01:00Z",
        expires_at: "2099-11-26T06:10:00Z",
        captured_fields: [
          {
            field_name: "full_name",
            field_kind: "text",
            is_sensitive: false,
            value_preview: "Aarav Mehta",
            file_sha256: null,
            value_count: 1,
          },
          {
            field_name: "work_authorization",
            field_kind: "text",
            is_sensitive: true,
            value_preview: "[REDACTED — SENSITIVE WIRE VALUE]",
            file_sha256: null,
            value_count: 1,
          },
          {
            field_name: "resume",
            field_kind: "file",
            is_sensitive: false,
            value_preview: null,
            file_sha256: "a".repeat(64),
            value_count: 1,
          },
        ],
      },
    };

    render(
      <HoldAndConfirmPanel
        job={sampleJob}
        applicationDetail={{
          ...baseAppDetail,
          application: { ...baseAppDetail.application, status: "MANUAL_ACTION_REQUIRED" },
        }}
        approvedResume={approvedResume}
        resumeChecksum={"a".repeat(64)}
        activeHandoff={activeHandoff}
        busy={false}
        onRefreshHandoff={vi.fn(async () => {})}
        onAuthorizeManualSubmit={onAuthorize}
      />,
    );

    expect(
      screen.getByText(/Previously Approved Application Answers \(Database Record\)/i),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/Actual Intercepted Request Fields \(Captured Wire Payload\)/i),
    ).toBeInTheDocument();
    expect(screen.getByText(/\[REDACTED — SENSITIVE WIRE VALUE\]/i)).toBeInTheDocument();

    const confirmBtn = screen.getByRole("button", { name: /Confirm & Release Held Submission/i });
    expect(confirmBtn).toBeDisabled();

    // Tampered / invalid SHA blocks confirmation
    const shaInput = screen.getByLabelText(/Required confirmation binding/i);
    fireEvent.change(shaInput, { target: { value: "deadbeef" } });
    fireEvent.click(screen.getByRole("checkbox"));
    expect(confirmBtn).toBeDisabled();

    // Restoring exact SHA enables confirmation and submits exact binding
    fireEvent.change(shaInput, { target: { value: pendingSha } });
    expect(confirmBtn).toBeEnabled();
    fireEvent.click(confirmBtn);

    await waitFor(() =>
      expect(onAuthorize).toHaveBeenCalledWith({
        handoff_session_id: "sess-1",
        pending_submission_id: "pend-1",
        expected_form_action: "https://boards.greenhouse.io/acme/jobs/101/submit",
        expected_http_method: "POST",
        expected_payload_sha256: pendingSha,
      }),
    );
  });

  it("blocks confirmation when pending submission or handoff session is expired", () => {
    const expiredHandoff: HandoffSessionInfo = {
      handoff_session_id: "sess-exp",
      application_id: "app-1",
      status: "expired",
      created_at: "2020-01-01T00:00:00Z",
      expires_at: "2020-01-01T00:30:00Z",
      manual_submit_armed: false,
      pending_submission: {
        pending_submission_id: "pend-exp",
        target_url: "https://boards.greenhouse.io/acme/jobs/101/submit",
        http_method: "POST",
        payload_sha256: "c".repeat(64),
        detected_at: "2020-01-01T00:01:00Z",
        expires_at: "2020-01-01T00:05:00Z",
        captured_fields: [],
      },
    };

    render(
      <HoldAndConfirmPanel
        job={sampleJob}
        applicationDetail={{
          ...baseAppDetail,
          application: { ...baseAppDetail.application, status: "MANUAL_ACTION_REQUIRED" },
        }}
        approvedResume={approvedResume}
        resumeChecksum={"a".repeat(64)}
        activeHandoff={expiredHandoff}
        busy={false}
        onRefreshHandoff={vi.fn(async () => {})}
        onAuthorizeManualSubmit={vi.fn(async () => {})}
      />,
    );

    expect(screen.getByText(/Held Pending Submission Expired/i)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("checkbox"));
    expect(
      screen.getByRole("button", { name: /Confirm & Release Held Submission/i }),
    ).toBeDisabled();
  });
});
