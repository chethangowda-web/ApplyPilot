import type {
  ApplicationAnswerInput,
  ApplicationDetailResponse,
  ApplicationMode,
  ApplicationPreparationResult,
  ApplicationResponse,
  ApprovalDecisionResponse,
  ApprovalRecordResponse,
  CandidateProfileResponse,
  CandidateProfileUpsertRequest,
  DiscoveryRunResponse,
  ExecutionResult,
  FactualValidationReportResponse,
  HandoffSessionInfo,
  HealthResponse,
  JobDetailResponse,
  JobEligibilityResponse,
  JobEvidenceResponse,
  JobIntelligenceResponse,
  JobMatchResponse,
  JobResumeHistoryResponse,
  JobSourceHealthResponse,
  JobSummaryResponse,
  ManualSubmitAuthorizationRequest,
  ManualSubmitAuthorizationResponse,
  ResumeMetadataResponse,
  SessionStateResponse,
  TailorEnqueueResponse,
  TailoredResumeDetailResponse,
  TailoredResumeSummaryResponse,
  TailoringDiffResponse,
  WorkflowRunResponse,
} from "@/lib/types";

export class ApiError extends Error {
  readonly status: number;
  readonly detail: string;

  constructor(status: number, detail: string) {
    super(detail);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }

  isUnauthorized(): boolean {
    return this.status === 401;
  }

  isConflict(): boolean {
    return this.status === 409;
  }

  isValidationError(): boolean {
    return this.status === 422;
  }
}

async function parseErrorDetail(resp: Response): Promise<string> {
  const data = await resp.json().catch(() => null);
  if (data && typeof data === "object" && "detail" in data) {
    const detail = (data as { detail: unknown }).detail;
    if (typeof detail === "string") {
      return detail;
    }
    if (Array.isArray(detail)) {
      return detail
        .map((item) =>
          item && typeof item === "object" && "msg" in item
            ? String((item as { msg: unknown }).msg)
            : JSON.stringify(item),
        )
        .join("; ");
    }
    return JSON.stringify(detail);
  }
  return `Request failed with status ${resp.status}`;
}

async function requestJson<T>(url: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers);
  if (init?.body && !(init.body instanceof FormData) && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }

  const resp = await fetch(url, {
    ...init,
    headers,
    cache: "no-store",
  });

  if (!resp.ok) {
    throw new ApiError(resp.status, await parseErrorDetail(resp));
  }
  return (await resp.json()) as T;
}

export const apiClient = {
  // Session / Auth (HttpOnly cookie backed, never localStorage)
  getHealth(): Promise<HealthResponse> {
    return requestJson<HealthResponse>("/api/backend/health");
  },

  getSession(): Promise<SessionStateResponse> {
    return requestJson<SessionStateResponse>("/api/session/me");
  },

  login(email: string, password: string): Promise<SessionStateResponse> {
    return requestJson<SessionStateResponse>("/api/session/login", {
      method: "POST",
      body: JSON.stringify({ email, password }),
    });
  },

  register(email: string, password: string, timezone = "Asia/Kolkata"): Promise<SessionStateResponse> {
    return requestJson<SessionStateResponse>("/api/session/register", {
      method: "POST",
      body: JSON.stringify({ email, password, timezone }),
    });
  },

  logout(): Promise<{ authenticated: boolean }> {
    return requestJson<{ authenticated: boolean }>("/api/session/logout", {
      method: "POST",
    });
  },

  // Candidate Profile & Resumes
  getProfile(): Promise<CandidateProfileResponse> {
    return requestJson<CandidateProfileResponse>("/api/backend/profile");
  },

  upsertProfile(payload: CandidateProfileUpsertRequest): Promise<CandidateProfileResponse> {
    return requestJson<CandidateProfileResponse>("/api/backend/profile", {
      method: "POST",
      body: JSON.stringify(payload),
    });
  },

  addSkill(payload: {
    skill_name: string;
    category?: string;
    evidence_ref?: string | null;
    verified?: boolean;
  }): Promise<unknown> {
    return requestJson("/api/backend/profile/skills", {
      method: "POST",
      body: JSON.stringify({
        category: "Engineering",
        verified: true,
        ...payload,
      }),
    });
  },

  listResumeVersions(): Promise<ResumeMetadataResponse[]> {
    return requestJson<ResumeMetadataResponse[]>("/api/backend/resume/versions");
  },

  uploadResume(file: File, isMaster = true, versionLabel?: string): Promise<ResumeMetadataResponse> {
    const form = new FormData();
    form.append("file", file);
    form.append("is_master", String(isMaster));
    if (versionLabel) {
      form.append("version_label", versionLabel);
    }
    return requestJson<ResumeMetadataResponse>("/api/backend/resume/upload", {
      method: "POST",
      body: form,
    });
  },

  // Jobs & Discovery & Intelligence
  listJobSources(): Promise<JobSourceHealthResponse[]> {
    return requestJson<JobSourceHealthResponse[]>("/api/backend/jobs/sources/health");
  },

  triggerDiscovery(payload: { query?: string; location?: string; max_pages?: number } = {}): Promise<DiscoveryRunResponse> {
    return requestJson<DiscoveryRunResponse>("/api/backend/jobs/discover", {
      method: "POST",
      body: JSON.stringify(payload),
    });
  },

  listJobs(params: {
    status?: string;
    remote_policy?: string;
    employment_type?: string;
    role_category?: string;
    search?: string;
    eligible_only?: boolean;
    min_score?: number;
    limit?: number;
    offset?: number;
  } = {}): Promise<JobSummaryResponse[]> {
    const sp = new URLSearchParams();
    if (params.status) sp.set("status", params.status);
    if (params.remote_policy) sp.set("remote_policy", params.remote_policy);
    if (params.employment_type) sp.set("employment_type", params.employment_type);
    if (params.role_category) sp.set("role_category", params.role_category);
    if (params.search) sp.set("search", params.search);
    if (params.eligible_only) sp.set("eligible_only", "true");
    if (typeof params.min_score === "number") sp.set("min_score", String(params.min_score));
    if (typeof params.limit === "number") sp.set("limit", String(params.limit));
    if (typeof params.offset === "number") sp.set("offset", String(params.offset));
    const qs = sp.toString();
    return requestJson<JobSummaryResponse[]>(`/api/backend/jobs${qs ? `?${qs}` : ""}`);
  },

  getJobDetail(jobId: string): Promise<JobDetailResponse> {
    return requestJson<JobDetailResponse>(`/api/backend/jobs/${jobId}`);
  },

  analyzeJob(jobId: string, useLlm = true): Promise<JobMatchResponse> {
    return requestJson<JobMatchResponse>(`/api/backend/jobs/${jobId}/analyze?use_llm=${useLlm}`, {
      method: "POST",
    });
  },

  getJobIntelligence(jobId: string): Promise<JobIntelligenceResponse> {
    return requestJson<JobIntelligenceResponse>(`/api/backend/jobs/${jobId}/intelligence`);
  },

  getJobEvidence(jobId: string): Promise<JobEvidenceResponse> {
    return requestJson<JobEvidenceResponse>(`/api/backend/jobs/${jobId}/evidence`);
  },

  getJobEligibility(jobId: string): Promise<JobEligibilityResponse> {
    return requestJson<JobEligibilityResponse>(`/api/backend/jobs/${jobId}/eligibility`);
  },

  // Tailored Resumes
  listTailoredResumes(jobId?: string): Promise<TailoredResumeSummaryResponse[]> {
    const qs = jobId ? `?job_id=${encodeURIComponent(jobId)}` : "";
    return requestJson<TailoredResumeSummaryResponse[]>(`/api/backend/resumes/tailored${qs}`);
  },

  getJobResumeHistory(jobId: string): Promise<JobResumeHistoryResponse> {
    return requestJson<JobResumeHistoryResponse>(`/api/backend/jobs/${jobId}/resumes/history`);
  },

  tailorResumeForJob(
    jobId: string,
    options: { useLlm?: boolean; forceRegenerate?: boolean } = {},
  ): Promise<TailorEnqueueResponse> {
    return requestJson<TailorEnqueueResponse>(`/api/backend/jobs/${jobId}/tailor`, {
      method: "POST",
      body: JSON.stringify({
        use_llm: options.useLlm ?? true,
        force_regenerate: options.forceRegenerate ?? false,
      }),
    });
  },

  regenerateResume(resumeId: string, useLlm = true): Promise<TailorEnqueueResponse> {
    return requestJson<TailorEnqueueResponse>(`/api/backend/resumes/${resumeId}/regenerate`, {
      method: "POST",
      body: JSON.stringify({ use_llm: useLlm }),
    });
  },

  getTailoredResumeDetail(resumeId: string): Promise<TailoredResumeDetailResponse> {
    return requestJson<TailoredResumeDetailResponse>(`/api/backend/resumes/${resumeId}/tailored`);
  },

  getTailoringDiff(resumeId: string): Promise<TailoringDiffResponse> {
    return requestJson<TailoringDiffResponse>(`/api/backend/resumes/${resumeId}/diff`);
  },

  getResumeValidation(resumeId: string): Promise<FactualValidationReportResponse> {
    return requestJson<FactualValidationReportResponse>(`/api/backend/resumes/${resumeId}/validation`);
  },

  approveTailoredResume(resumeId: string, notes?: string): Promise<ApprovalDecisionResponse> {
    return requestJson<ApprovalDecisionResponse>(`/api/backend/resumes/${resumeId}/approve`, {
      method: "POST",
      body: JSON.stringify({ notes: notes || null }),
    });
  },

  rejectTailoredResume(resumeId: string, notes?: string): Promise<ApprovalDecisionResponse> {
    return requestJson<ApprovalDecisionResponse>(`/api/backend/resumes/${resumeId}/reject`, {
      method: "POST",
      body: JSON.stringify({ notes: notes || null }),
    });
  },

  tailoredResumeDownloadUrl(resumeId: string, approvedOnly = false): string {
    return `/api/backend/resumes/${resumeId}/download?approved_only=${approvedOnly}`;
  },

  // Applications & Hold-and-Confirm
  listApplications(): Promise<ApplicationResponse[]> {
    return requestJson<ApplicationResponse[]>("/api/backend/applications");
  },

  prepareApplication(payload: {
    job_id: string;
    resume_id: string;
    preferred_mode?: ApplicationMode | null;
  }): Promise<ApplicationPreparationResult> {
    return requestJson<ApplicationPreparationResult>("/api/backend/applications/prepare", {
      method: "POST",
      body: JSON.stringify(payload),
    });
  },

  getApplicationDetail(applicationId: string): Promise<ApplicationDetailResponse> {
    return requestJson<ApplicationDetailResponse>(`/api/backend/applications/${applicationId}`);
  },

  submitApplicationAnswers(
    applicationId: string,
    answers: ApplicationAnswerInput[],
  ): Promise<ApplicationDetailResponse> {
    return requestJson<ApplicationDetailResponse>(`/api/backend/applications/${applicationId}/answers`, {
      method: "POST",
      body: JSON.stringify({ answers }),
    });
  },

  approveApplication(applicationId: string, notes?: string): Promise<ApprovalRecordResponse> {
    return requestJson<ApprovalRecordResponse>(`/api/backend/applications/${applicationId}/approve`, {
      method: "POST",
      body: JSON.stringify({ notes: notes || null }),
    });
  },

  rejectApplication(applicationId: string, notes?: string): Promise<ApprovalRecordResponse> {
    return requestJson<ApprovalRecordResponse>(`/api/backend/applications/${applicationId}/reject`, {
      method: "POST",
      body: JSON.stringify({ notes: notes || null }),
    });
  },

  executeApplication(
    applicationId: string,
    options: { browser_mode?: "visible_handoff" | "headless"; capture_artifacts?: boolean } = {},
  ): Promise<ExecutionResult> {
    return requestJson<ExecutionResult>(`/api/backend/applications/${applicationId}/execute`, {
      method: "POST",
      body: JSON.stringify({
        options: {
          browser_mode: options.browser_mode ?? "visible_handoff",
          capture_artifacts: options.capture_artifacts ?? true,
        },
      }),
    });
  },

  getHandoffSession(applicationId: string, handoffSessionId: string): Promise<HandoffSessionInfo> {
    return requestJson<HandoffSessionInfo>(
      `/api/backend/applications/${applicationId}/handoff/${handoffSessionId}`,
    );
  },

  closeHandoffSession(applicationId: string, handoffSessionId: string): Promise<HandoffSessionInfo> {
    return requestJson<HandoffSessionInfo>(
      `/api/backend/applications/${applicationId}/handoff/${handoffSessionId}/close`,
      { method: "POST" },
    );
  },

  authorizeManualSubmit(
    applicationId: string,
    payload: ManualSubmitAuthorizationRequest,
  ): Promise<ManualSubmitAuthorizationResponse> {
    return requestJson<ManualSubmitAuthorizationResponse>(
      `/api/backend/applications/${applicationId}/authorize-manual-submit`,
      {
        method: "POST",
        body: JSON.stringify(payload),
      },
    );
  },

  recordSubmitOutcome(
    applicationId: string,
    payload: {
      outcome: "submitted_in_browser" | "failed" | "interrupted";
      handoff_session_id?: string | null;
      notes?: string | null;
    },
  ): Promise<ApplicationResponse> {
    return requestJson<ApplicationResponse>(`/api/backend/applications/${applicationId}/submit-outcome`, {
      method: "POST",
      body: JSON.stringify(payload),
    });
  },

  reconcileApplication(
    applicationId: string,
    confirmationEvidence: Record<string, unknown>,
  ): Promise<ApplicationResponse> {
    return requestJson<ApplicationResponse>(`/api/backend/applications/${applicationId}/reconcile`, {
      method: "POST",
      body: JSON.stringify({ confirmation_evidence: confirmationEvidence }),
    });
  },

  artifactImageUrl(applicationId: string, artifactId: string): string {
    return `/api/backend/applications/${applicationId}/artifacts/${encodeURIComponent(artifactId)}`;
  },

  getWorkflowRun(threadId: string): Promise<WorkflowRunResponse> {
    return requestJson<WorkflowRunResponse>(`/api/backend/workflows/${encodeURIComponent(threadId)}`);
  },
};
