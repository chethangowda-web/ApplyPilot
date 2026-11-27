import { describe, expect, it } from "vitest";
import {
  WORKFLOW_STAGES,
  buildWorkflowOverviewItems,
  deriveJobWorkflowState,
} from "@/lib/workflow-engine";
import type {
  ApplicationDetailResponse,
  JobDetailResponse,
  JobResumeHistoryResponse,
} from "@/lib/types";

function makeJob(overrides: Partial<JobDetailResponse> = {}): JobDetailResponse {
  return {
    id: "11111111-1111-4111-8111-111111111111",
    source_id: "22222222-2222-4222-8222-222222222222",
    source_job_id: "gh-101",
    canonical_url: "https://boards.greenhouse.io/acme/jobs/101",
    company: "Acme Robotics",
    role_title: "Junior AI Engineer",
    location: "Bengaluru, India",
    remote_policy: "hybrid",
    is_remote: false,
    employment_type: "full_time",
    salary_min: null,
    salary_max: null,
    salary_currency: null,
    salary_text: null,
    application_url: "https://boards.greenhouse.io/acme/jobs/101",
    posted_at: "2026-11-25T09:00:00Z",
    first_seen_at: "2026-11-25T10:00:00Z",
    last_seen_at: "2026-11-26T08:00:00Z",
    repost_count: 0,
    status: "active",
    requirements: null,
    match: null,
    raw_jd: "Build Python & LangGraph agents.",
    updated_at_source: null,
    expired_at: null,
    source_provenance: {},
    ...overrides,
  };
}

const sampleReqs = {
  role_category: "AI Engineer",
  location: "Bengaluru, India",
  remote_policy: "hybrid",
  employment_type: "full_time",
  required_skills: ["Python", "LangGraph"],
  preferred_skills: ["FastAPI"],
  trainable_skills: [],
  responsibilities: ["Build reliable LLM pipelines"],
  technical_stack: ["Python", "FastAPI", "PostgreSQL"],
  experience_min: 0,
  experience_max: 2,
  education_requirements: ["B.Tech CS"],
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
  extracted_at: "2026-11-26T08:10:00Z",
};

const eligibleMatch = {
  job_id: "11111111-1111-4111-8111-111111111111",
  profile_id: "33333333-3333-4333-8333-333333333333",
  score: 88,
  eligible: true,
  hard_constraints_passed: true,
  required_skill_coverage: 1,
  preferred_skill_coverage: 1,
  semantic_similarity: 0.91,
  matched_skills: ["Python", "LangGraph"],
  missing_required_skills: [],
  preferred_gaps: [],
  evidence_mapping: [],
  missing_evidence: [],
  eligibility_blockers: [],
  uncertainties: [],
  confidence: 0.95,
  score_breakdown: {},
  recommendation: "strong_apply",
  explanation: "Strong alignment on Python and LangGraph.",
  scored_at: "2026-11-26T08:12:00Z",
};

describe("workflow-engine authoritative derivation", () => {
  it("tracks all 11 stages and starts active unanalyzed job at step 2", () => {
    expect(WORKFLOW_STAGES).toHaveLength(11);
    const derived = deriveJobWorkflowState({ job: makeJob() });
    expect(derived.authoritativeStep).toBe(2);
    expect(derived.completedCount).toBe(1);
    expect(derived.progressPercent).toBe(9);
    expect(derived.stages[0].state).toBe("completed");
    expect(derived.stages[1].state).toBe("current");
    expect(derived.stages[2].state).toBe("upcoming");
  });

  it("marks inactive/expired posting as blocked at step 1", () => {
    const derived = deriveJobWorkflowState({ job: makeJob({ status: "expired" }) });
    expect(derived.overallState).toBe("blocked");
    expect(derived.isTerminalBlocked).toBe(true);
    expect(derived.stages[0].state).toBe("blocked");
    expect(derived.authoritativeStep).toBe(1);
    expect(derived.completedCount).toBe(0);
  });

  it("blocks workflow at step 3 when hard eligibility fails", () => {
    const derived = deriveJobWorkflowState({
      job: makeJob({
        requirements: sampleReqs,
        match: {
          ...eligibleMatch,
          eligible: false,
          hard_constraints_passed: false,
          eligibility_blockers: ["Requires 5+ years production experience"],
        },
      }),
    });
    expect(derived.overallState).toBe("blocked");
    expect(derived.authoritativeStep).toBe(3);
    expect(derived.stages[2].state).toBe("blocked");
    expect(derived.stages[3].canInspect).toBe(false);
  });

  it("waits in step 5 when tailored resume is pending_review and blocks when rejected", () => {
    const historyPending: JobResumeHistoryResponse = {
      job_id: "11111111-1111-4111-8111-111111111111",
      versions: [
        {
          mapping_id: "m1",
          resume_id: "r1",
          job_id: "11111111-1111-4111-8111-111111111111",
          master_resume_id: "m0",
          generation_job_id: "g1",
          run_id: "run-1",
          trigger_mode: "manual",
          version_number: 1,
          is_active: true,
          evidence_fingerprint: "abc",
          status: "completed",
          approval_status: "pending_review",
          approval_notes: null,
          approved_at: null,
          rejected_at: null,
          model_provider: "claude",
          token_usage: {},
          error_message: null,
          created_at: "2026-11-26T08:15:00Z",
        },
      ],
      outbox_jobs: [],
    };
    const waitingState = deriveJobWorkflowState({
      job: makeJob({ requirements: sampleReqs, match: eligibleMatch }),
      resumeHistory: historyPending,
    });
    expect(waitingState.overallState).toBe("waiting");
    expect(waitingState.authoritativeStep).toBe(5);
    expect(waitingState.stages[4].state).toBe("waiting");
    expect(waitingState.stages[5].canInspect).toBe(false);

    const rejectedState = deriveJobWorkflowState({
      job: makeJob({ requirements: sampleReqs, match: eligibleMatch }),
      resumeHistory: {
        ...historyPending,
        versions: [{ ...historyPending.versions[0], approval_status: "rejected" }],
      },
    });
    expect(rejectedState.overallState).toBe("blocked");
    expect(rejectedState.stages[4].state).toBe("blocked");
  });

  it("handles NEEDS_USER_INPUT, AWAITING_APPROVAL, Hold-and-Confirm, and UNVERIFIED distinctly", () => {
    const baseResumeHistory: JobResumeHistoryResponse = {
      job_id: "11111111-1111-4111-8111-111111111111",
      versions: [
        {
          mapping_id: "m1",
          resume_id: "r1",
          job_id: "11111111-1111-4111-8111-111111111111",
          master_resume_id: "m0",
          generation_job_id: "g1",
          run_id: "run-1",
          trigger_mode: "manual",
          version_number: 1,
          is_active: true,
          evidence_fingerprint: "abc",
          status: "completed",
          approval_status: "approved",
          approval_notes: null,
          approved_at: "2026-11-26T08:18:00Z",
          rejected_at: null,
          model_provider: "claude",
          token_usage: {},
          error_message: null,
          created_at: "2026-11-26T08:15:00Z",
        },
      ],
      outbox_jobs: [],
    };

    const baseAppDetail: ApplicationDetailResponse = {
      application: {
        id: "app-1",
        user_id: "u1",
        job_id: "11111111-1111-4111-8111-111111111111",
        resume_id: "r1",
        canonical_job_key: "canon",
        idempotency_key: "idem",
        application_mode: "human_approved_browser",
        status: "NEEDS_USER_INPUT",
        destination_url: "https://boards.greenhouse.io/acme/jobs/101",
        thread_id: "thread-1",
        external_ref_id: null,
        confirmation_evidence: {},
        submitted_at: null,
        confirmed_at: null,
        next_followup_at: null,
        error_message: null,
        created_at: "2026-11-26T08:20:00Z",
        updated_at: "2026-11-26T08:21:00Z",
      },
      answers: [
        {
          id: "ans-1",
          question_key: "work_auth",
          question: "Are you legally authorized to work?",
          is_sensitive: true,
          answer_preview: "[REDACTED — UNVERIFIED]",
          evidence_source: null,
          user_verified: false,
          created_at: "2026-11-26T08:20:00Z",
        },
      ],
      approval_records: [],
      active_handoff_sessions: [],
      execution_history: [],
    };

    const needsInput = deriveJobWorkflowState({
      job: makeJob({ requirements: sampleReqs, match: eligibleMatch }),
      resumeHistory: baseResumeHistory,
      applicationDetail: baseAppDetail,
    });
    expect(needsInput.authoritativeStep).toBe(7);
    expect(needsInput.overallState).toBe("waiting");

    const heldSubmit = deriveJobWorkflowState({
      job: makeJob({ requirements: sampleReqs, match: eligibleMatch }),
      resumeHistory: baseResumeHistory,
      applicationDetail: {
        ...baseAppDetail,
        application: { ...baseAppDetail.application, status: "MANUAL_ACTION_REQUIRED" },
        answers: [{ ...baseAppDetail.answers[0], user_verified: true }],
        approval_records: [
          {
            id: "rec-1",
            application_id: "app-1",
            decision: "approved",
            actor: "user:u1",
            resume_id: "r1",
            resume_checksum: "c".repeat(64),
            destination_url: "https://boards.greenhouse.io/acme/jobs/101",
            application_mode: "human_approved_browser",
            approval_snapshot_hash: "d".repeat(64),
            notes: null,
            decided_at: "2026-11-26T08:25:00Z",
            expires_at: "2099-11-27T08:25:00Z",
          },
        ],
        active_handoff_sessions: [
          {
            handoff_session_id: "hs-1",
            application_id: "app-1",
            status: "active",
            created_at: "2026-11-26T08:26:00Z",
            expires_at: "2099-11-26T08:56:00Z",
            manual_submit_armed: false,
            pending_submission: {
              pending_submission_id: "ps-1",
              target_url: "https://boards.greenhouse.io/acme/jobs/101/apply",
              http_method: "POST",
              payload_sha256: "e".repeat(64),
              detected_at: "2026-11-26T08:27:00Z",
              expires_at: "2099-11-26T08:29:00Z",
            },
          },
        ],
      },
    });
    expect(heldSubmit.authoritativeStep).toBe(10);
    expect(heldSubmit.stages[9].state).toBe("current");

    const unverified = deriveJobWorkflowState({
      job: makeJob({ requirements: sampleReqs, match: eligibleMatch }),
      resumeHistory: baseResumeHistory,
      applicationDetail: {
        ...baseAppDetail,
        application: {
          ...baseAppDetail.application,
          status: "UNVERIFIED",
          error_message: "Server redirect blocked",
        },
        answers: [{ ...baseAppDetail.answers[0], user_verified: true }],
      },
    });
    expect(unverified.overallState).toBe("failed");
    expect(unverified.stages[9].state).toBe("failed");
    expect(unverified.stages[10].state).toBe("failed");

    // USER_REPORTED_SUBMITTED must NEVER be displayed as CONFIRMED or 100% completed
    const userReported = deriveJobWorkflowState({
      job: makeJob({ requirements: sampleReqs, match: eligibleMatch }),
      resumeHistory: baseResumeHistory,
      applicationDetail: {
        ...baseAppDetail,
        application: { ...baseAppDetail.application, status: "USER_REPORTED_SUBMITTED" },
        answers: [{ ...baseAppDetail.answers[0], user_verified: true }],
      },
    });
    expect(userReported.backendStatusLabel).toBe("USER_REPORTED_SUBMITTED");
    expect(userReported.stages[10].state).toBe("waiting");
    expect(userReported.progressPercent).toBeLessThan(100);
  });

  it("builds My Workflows overview items from server state without inventing progress", () => {
    const items = buildWorkflowOverviewItems({
      jobs: [
        makeJob({ id: "j-unstarted", requirements: null, match: null }),
        makeJob({
          id: "j-started",
          company: "Nova Labs",
          role_title: "Backend Intern",
          requirements: sampleReqs,
          match: eligibleMatch,
        }),
      ],
      tailoredResumes: [],
      applications: [],
    });
    expect(items).toHaveLength(1);
    expect(items[0].job.company).toBe("Nova Labs");
    expect(items[0].derived.authoritativeStep).toBe(4);
    expect(items[0].derived.progressPercent).toBe(27);
  });
});
