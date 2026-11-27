export type ApplicationMode = "official_api" | "human_approved_browser" | "manual";

export type ApplicationStatus =
  | "ELIGIBLE"
  | "MATCHED"
  | "RESUME_READY"
  | "NEEDS_USER_INPUT"
  | "AWAITING_APPROVAL"
  | "APPROVED"
  | "SUBMISSION_PENDING"
  | "SUBMITTED"
  | "USER_REPORTED_SUBMITTED"
  | "UNVERIFIED"
  | "CONFIRMED"
  | "INTERVIEW"
  | "REJECTED"
  | "WITHDRAWN"
  | "EXPIRED"
  | "FAILED"
  | "DUPLICATE"
  | "MANUAL_ACTION_REQUIRED";

export interface UserResponse {
  id: string;
  email: string;
  timezone: string;
  is_active: boolean;
  created_at: string;
}

export interface SessionStateResponse {
  authenticated: boolean;
  user: UserResponse | null;
}

export interface HealthResponse {
  status: string;
  app: string;
  environment: string;
  timezone: string;
}

export interface CandidateSkillOut {
  id: string;
  skill_name: string;
  category: string;
  proficiency: string | null;
  evidence_ref: string | null;
  verified: boolean;
  created_at: string;
}

export interface ProjectOut {
  id: string;
  name: string;
  summary: string;
  technologies: string[];
  implemented_features: string[];
  planned_features: string[];
  verified_metrics: string[];
  repo_url: string | null;
  live_url: string | null;
  start_date: string | null;
  end_date: string | null;
  created_at: string;
}

export interface ExperienceOut {
  id: string;
  company: string;
  role: string;
  location: string | null;
  start_date: string;
  end_date: string | null;
  is_current: boolean;
  verified_responsibilities: string[];
  created_at: string;
}

export interface CertificationOut {
  id: string;
  name: string;
  issuer: string;
  issued_date: string | null;
  credential_url: string | null;
  verified: boolean;
  created_at: string;
}

export interface CandidateProfileResponse {
  id: string;
  user_id: string;
  full_name: string;
  phone: string | null;
  email: string;
  location: string | null;
  github_url: string | null;
  linkedin_url: string | null;
  portfolio_url: string | null;
  summary: string | null;
  education: Array<Record<string, unknown>>;
  achievements: string[];
  confirmed_preferences: Record<string, unknown>;
  skills: CandidateSkillOut[];
  projects: ProjectOut[];
  experiences: ExperienceOut[];
  certifications: CertificationOut[];
  created_at: string;
  updated_at: string;
}

export interface CandidateProfileUpsertRequest {
  full_name: string;
  phone?: string | null;
  email: string;
  location?: string | null;
  github_url?: string | null;
  linkedin_url?: string | null;
  portfolio_url?: string | null;
  summary?: string | null;
  education: Array<Record<string, unknown>>;
  achievements: string[];
  confirmed_preferences: Record<string, unknown>;
}

export interface ResumeMetadataResponse {
  id: string;
  profile_id: string;
  is_master: boolean;
  original_filename: string;
  content_type: string;
  file_size_bytes: number;
  version_label: string;
  sha256_checksum: string;
  template_version: string;
  ats_report: Record<string, unknown>;
  parsed_metadata: Record<string, unknown>;
  validation_passed: boolean;
  created_at: string;
}

export interface JobRequirementResponse {
  role_category: string;
  location: string | null;
  remote_policy: string;
  employment_type: string;
  required_skills: string[];
  preferred_skills: string[];
  trainable_skills: string[];
  responsibilities: string[];
  technical_stack: string[];
  experience_min: number | null;
  experience_max: number | null;
  education_requirements: string[];
  graduation_criteria: Array<Record<string, unknown>>;
  mandatory_qualifications: Array<Record<string, unknown>>;
  preferred_qualifications: Array<Record<string, unknown>>;
  application_instructions: Array<Record<string, unknown>>;
  evidence_items: Record<string, unknown>;
  extraction_mode: string;
  internship_eligible: boolean | null;
  fresher_eligible: boolean | null;
  uncertainties: string[];
  prompt_injection_detected: boolean;
  extracted_at: string;
}

export interface JobMatchResponse {
  job_id: string;
  profile_id: string;
  score: number;
  eligible: boolean;
  hard_constraints_passed: boolean;
  required_skill_coverage: number;
  preferred_skill_coverage: number;
  semantic_similarity: number | null;
  matched_skills: string[];
  missing_required_skills: string[];
  preferred_gaps: string[];
  evidence_mapping: Array<Record<string, unknown>>;
  missing_evidence: Array<Record<string, unknown>>;
  eligibility_blockers: string[];
  uncertainties: string[];
  confidence: number;
  score_breakdown: Record<string, unknown>;
  recommendation: string;
  explanation: string;
  scored_at: string;
}

export interface JobSummaryResponse {
  id: string;
  source_id: string;
  source_job_id: string | null;
  canonical_url: string;
  company: string;
  role_title: string;
  location: string | null;
  remote_policy: string;
  is_remote: boolean | null;
  employment_type: string;
  salary_min: number | null;
  salary_max: number | null;
  salary_currency: string | null;
  salary_text: string | null;
  application_url: string | null;
  posted_at: string | null;
  first_seen_at: string;
  last_seen_at: string;
  repost_count: number;
  status: string;
  requirements: JobRequirementResponse | null;
  match: JobMatchResponse | null;
}

export interface JobDetailResponse extends JobSummaryResponse {
  raw_jd: string;
  updated_at_source: string | null;
  expired_at: string | null;
  source_provenance: Record<string, unknown>;
}

export interface JobIntelligenceResponse {
  job_id: string;
  company: string;
  role_title: string;
  status: string;
  requirements: JobRequirementResponse | null;
  match: JobMatchResponse;
  skill_coverage: Record<string, unknown>;
  score_disclaimer: string;
}

export interface JobEvidenceResponse {
  job_id: string;
  profile_id: string;
  evidence_mapping: Array<Record<string, unknown>>;
  missing_evidence: Array<Record<string, unknown>>;
  confirmed_missing_skills: string[];
  jd_evidence_items: Record<string, unknown>;
}

export interface JobEligibilityResponse {
  job_id: string;
  eligible: boolean;
  hard_constraints_passed: boolean;
  eligibility_blockers: string[];
  uncertainties: string[];
  confidence: number;
  recommendation: string;
  explanation: string;
}

export interface JobSourceHealthResponse {
  id: string;
  source_name: string;
  connector_type: string;
  enabled: boolean;
  health_status: string;
  consecutive_failures: number;
  last_error: string | null;
  last_run_at: string | null;
}

export interface DiscoveryRunResponse {
  run_id: string;
  sources_queried: number;
  discovered_count: number;
  created_count: number;
  duplicate_count: number;
  reposted_count: number;
  expired_count: number;
  quality_matches_count: number;
  recommended_job_ids: string[];
  errors: Array<Record<string, unknown>>;
}

export interface OutboxJobStatusResponse {
  id: string;
  job_id: string;
  profile_id: string;
  trigger_mode: string;
  evidence_fingerprint: string;
  quota_date: string | null;
  status: string;
  attempt_count: number;
  max_attempts: number;
  thread_id: string;
  resume_id: string | null;
  error_message: string | null;
  created_at: string;
  started_at: string | null;
  completed_at: string | null;
}

export interface TailorEnqueueResponse {
  accepted: boolean;
  created: boolean;
  idempotency_hit: boolean;
  status: string;
  reason: string;
  job_id: string;
  generation_job_id: string | null;
  existing_resume_id: string | null;
  evidence_fingerprint: string | null;
  dispatched_to_broker: boolean;
  outbox_job: OutboxJobStatusResponse | null;
}

export interface ResumeGenerationHistoryResponse {
  mapping_id: string;
  resume_id: string | null;
  job_id: string;
  master_resume_id: string | null;
  generation_job_id: string | null;
  run_id: string;
  trigger_mode: string;
  version_number: number;
  is_active: boolean;
  evidence_fingerprint: string;
  status: string;
  approval_status: string;
  approval_notes: string | null;
  approved_at: string | null;
  rejected_at: string | null;
  model_provider: string | null;
  token_usage: Record<string, unknown>;
  error_message: string | null;
  created_at: string;
}

export interface JobResumeHistoryResponse {
  job_id: string;
  versions: ResumeGenerationHistoryResponse[];
  outbox_jobs: OutboxJobStatusResponse[];
}

export interface TailoredResumeSummaryResponse {
  resume_id: string;
  mapping_id: string;
  job_id: string;
  master_resume_id: string | null;
  version_number: number;
  version_label: string;
  is_active: boolean;
  status: string;
  approval_status: string;
  template_version: string;
  evidence_fingerprint: string;
  validation_passed: boolean;
  evaluation_report: Record<string, unknown>;
  created_at: string;
}

export interface TailoredResumeDetailResponse extends TailoredResumeSummaryResponse {
  run_id: string;
  trigger_mode: string;
  sha256_checksum: string | null;
  file_size_bytes: number | null;
  ats_report: Record<string, unknown>;
  parsed_metadata: Record<string, unknown>;
  validation_report: Record<string, unknown>;
  tailoring_diff: Record<string, unknown>;
  token_usage: Record<string, unknown>;
  model_provider: string | null;
  approval_notes: string | null;
  approved_at: string | null;
  rejected_at: string | null;
}

export interface TailoringDiffResponse {
  resume_id: string;
  mapping_id: string;
  job_id: string;
  version_number: number;
  evidence_fingerprint: string;
  tailoring_diff: Record<string, unknown>;
}

export interface FactualValidationReportResponse {
  resume_id: string;
  mapping_id: string | null;
  job_id: string | null;
  validation_passed: boolean;
  validation_report: Record<string, unknown>;
  ats_report: Record<string, unknown>;
}

export interface ApprovalDecisionResponse {
  mapping_id: string;
  resume_id: string;
  job_id: string;
  version_number: number;
  status: string;
  approval_status: string;
  approval_notes: string | null;
  approved_at: string | null;
  rejected_at: string | null;
  idempotent: boolean;
}

export interface ApplicationAnswerInput {
  question_key: string;
  question: string;
  answer: string;
  is_sensitive: boolean;
  evidence_source?: string | null;
}

export interface ApplicationAnswerResponse {
  id: string;
  question_key: string;
  question: string;
  is_sensitive: boolean;
  answer_preview: string | null;
  evidence_source: string | null;
  user_verified: boolean;
  created_at: string;
}

export interface ApprovalRecordResponse {
  id: string;
  application_id: string;
  decision: "approved" | "rejected";
  actor: string;
  resume_id: string;
  resume_checksum: string;
  destination_url: string;
  application_mode: ApplicationMode;
  approval_snapshot_hash: string;
  notes: string | null;
  decided_at: string;
  expires_at: string;
}

export interface ApplicationResponse {
  id: string;
  user_id: string | null;
  job_id: string;
  resume_id: string;
  canonical_job_key: string;
  idempotency_key: string;
  application_mode: ApplicationMode;
  status: ApplicationStatus;
  destination_url: string;
  thread_id: string;
  external_ref_id: string | null;
  confirmation_evidence: Record<string, unknown>;
  submitted_at: string | null;
  confirmed_at: string | null;
  next_followup_at: string | null;
  error_message: string | null;
  created_at: string;
  updated_at: string;
}

export interface MappedApplicationAnswer {
  question_key: string;
  question: string;
  field_type: string;
  required: boolean;
  is_sensitive: boolean;
  sensitive_category: string | null;
  verification_state: "verified" | "unverified" | "missing";
  proposed_answer: string | null;
  evidence_source: string | null;
  requires_user_confirmation: boolean;
  reason: string | null;
}

export interface FormMappingResult {
  questions: Array<Record<string, unknown>>;
  mapped_answers: MappedApplicationAnswer[];
  missing_required_keys: string[];
  missing_optional_keys: string[];
  unverified_keys: string[];
  sensitive_keys_requiring_confirmation: string[];
  ready_for_approval: boolean;
}

export interface PolicyDecision {
  allowed: boolean;
  selected_mode: ApplicationMode | null;
  submission_paused: boolean;
  blocker_reasons: string[];
  warnings: string[];
  canonical_job_key: string;
  idempotency_key: string;
  daily_used: number;
  daily_limit: number;
}

export interface ApplicationPreparationResult {
  application_id: string;
  status: ApplicationStatus;
  policy: PolicyDecision;
  form_mapping: FormMappingResult;
}

export interface PendingSubmissionFieldPreview {
  field_name: string;
  field_kind: "text" | "file" | "csrf_token";
  is_sensitive: boolean;
  value_preview: string | null;
  file_sha256: string | null;
  value_count: number;
}

export interface PendingSubmissionInfo {
  pending_submission_id: string;
  target_url: string;
  http_method: "GET" | "POST";
  payload_sha256: string;
  detected_at: string;
  expires_at: string;
  captured_fields?: PendingSubmissionFieldPreview[];
}

export interface HandoffSessionInfo {
  handoff_session_id: string;
  application_id: string;
  status: "active" | "closed" | "expired" | "crashed";
  created_at: string;
  expires_at: string;
  manual_submit_armed: boolean;
  pending_submission: PendingSubmissionInfo | null;
}

export interface FieldFillOutcome {
  question_key: string;
  status: "filled" | "skipped_unverified" | "skipped_missing" | "unsupported";
  reason: string | null;
}

export interface ExecutionResult {
  application_id: string;
  status: ApplicationStatus;
  adapter_name: string;
  mode: ApplicationMode;
  browser_mode: "headless" | "visible_handoff" | null;
  stopped_before_submit: boolean;
  blocked_submit_attempts: number;
  restriction_detected: string | null;
  detected_form_action: string | null;
  detected_http_method: "GET" | "POST" | null;
  fields_filled: FieldFillOutcome[];
  unsupported_fields: string[];
  resume_attached: boolean;
  resume_checksum: string | null;
  os_firewall_enforced: boolean;
  artifact_ids: string[];
  handoff_session: HandoffSessionInfo | null;
  error_message: string | null;
}

export interface ManualSubmitAuthorizationRequest {
  handoff_session_id: string;
  pending_submission_id: string;
  expected_form_action: string;
  expected_http_method: "GET" | "POST";
  expected_payload_sha256: string;
}

export interface ManualSubmitAuthorizationResponse {
  authorization_id: string;
  application_id: string;
  handoff_session_id: string;
  destination_url: string;
  form_action: string;
  http_method: "GET" | "POST";
  issued_at: string;
  expires_at: string;
  consumed: boolean;
}

export interface ApplicationDetailResponse {
  application: ApplicationResponse;
  answers: ApplicationAnswerResponse[];
  approval_records: ApprovalRecordResponse[];
  active_handoff_sessions: HandoffSessionInfo[];
  execution_history: Array<{
    action: string;
    actor: string;
    created_at: string;
    metadata: Record<string, unknown>;
  }>;
}

export interface WorkflowRunResponse {
  id: string;
  thread_id: string;
  workflow_type: string;
  current_stage: string;
  status: string;
  checkpoint_metadata: Record<string, unknown>;
  started_at: string;
  updated_at: string;
}

export interface GmailConnectionSummary {
  id: string;
  provider: string;
  mailbox_email: string;
  status: "connected" | "revoked" | "expired" | "disconnected" | "error";
  granted_scopes: string[];
  last_synced_at: string | null;
  last_sync_status: "idle" | "running" | "succeeded" | "failed" | null;
  last_sync_error: string | null;
  revoked_at: string | null;
  disconnected_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface GmailStatusResponse {
  connected: boolean;
  connections: GmailConnectionSummary[];
}

export interface GmailOAuthStartResponse {
  authorization_url: string;
  provider: "google_gmail";
  scopes: string[];
}

export interface GmailSyncTriggerResponse {
  status: string;
  connections_queued: number;
  message: string;
}

export interface SanitizedEmailConfirmationMetadata {
  id: string;
  sender_domain: string;
  received_at: string;
  match_status: "matched" | "confirmed" | "ambiguous" | "rejected" | "unmatched";
  matched_application_id: string | null;
  match_signals: Record<string, unknown>;
  processed_at: string;
}

export interface FollowupResponse {
  id: string;
  application_id: string;
  reminder_type: "initial_followup" | "second_followup" | "interview_thank_you" | "custom";
  status: "pending" | "completed" | "snoozed" | "cancelled";
  due_at: string;
  snoozed_until: string | null;
  completed_at: string | null;
  interval_days: number;
  draft_subject: string;
  draft_body: string;
  recruiter_context: Record<string, unknown>;
  is_overdue: boolean;
  created_at: string;
  updated_at: string;
}

export interface InterviewKitResponse {
  id: string;
  application_id: string;
  job_id: string;
  resume_id: string;
  resume_checksum: string;
  version: number;
  is_active: boolean;
  modules_json: Record<string, Record<string, unknown>>;
  progress_json: Record<string, unknown>;
  user_feedback: string | null;
  readiness_estimate: number;
  created_at: string;
  updated_at: string;
}

export interface InterviewAttemptResponse {
  id: string;
  session_id: string;
  question_index: number;
  module_key: string;
  difficulty: string;
  question_text: string;
  expected_structure: string[];
  hints: string[];
  candidate_answer: string;
  feedback_json: Record<string, unknown>;
  score: number;
  weak_topics: string[];
  strong_topics: string[];
  attempted_at: string;
}

export interface InterviewSessionResponse {
  id: string;
  kit_id: string;
  round_type: string;
  difficulty: "easy" | "medium" | "hard";
  status: "active" | "completed";
  current_question_index: number;
  current_question_json: Record<string, unknown>;
  report_json: Record<string, unknown>;
  overall_score: number | null;
  attempts: InterviewAttemptResponse[];
  started_at: string;
  completed_at: string | null;
}

export interface InterviewMemoryResponse {
  id: string;
  memory_type: string;
  topic: string;
  summary: string;
  source: string;
  confidence: number;
  evidence_refs: string[];
  attempt_count: number;
  last_score: number | null;
  created_at: string;
  updated_at: string;
}
