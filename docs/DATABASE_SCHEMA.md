# ApplyPilot AI — Database Schema & Lifecycle Specification

**Primary Database:** PostgreSQL 16+  
**Vector Extension:** `pgvector` (semantic retrieval over verified candidate evidence, JDs, and interview memory)  
**Workflow Checkpointer:** LangGraph `PostgresSaver` (checkpoint tables managed alongside application tables)  
**Migrations:** Alembic (`backend/alembic/`)

---

## 1. Core Tables

| Table | Key Columns & Purpose |
|---|---|
| `users` | `id (UUID PK)`, `email (UNIQUE)`, `hashed_password`, `timezone (default 'Asia/Kolkata')`, `account_preferences (JSONB)`, `created_at` |
| `candidate_profiles` | `id (UUID PK)`, `user_id (FK -> users)`, `full_name`, `phone`, `email`, `location`, `github_url`, `linkedin_url`, `portfolio_url`, `education (JSONB)`, `confirmed_preferences (JSONB)`, `updated_at` |
| `candidate_skills` | `id (UUID PK)`, `profile_id (FK)`, `skill_name`, `category`, `proficiency`, `evidence_ref`, `verified (BOOL)`, `embedding (VECTOR(1536/768))` |
| `projects` | `id (UUID PK)`, `profile_id (FK)`, `name`, `summary`, `technologies (TEXT[])`, `implemented_features (JSONB)`, `planned_features (JSONB)`, `verified_metrics (JSONB)`, `repo_url`, `live_url`, `start_date`, `end_date`, `embedding (VECTOR(768))` |
| `experiences` | `id (UUID PK)`, `profile_id (FK)`, `company`, `role`, `location`, `start_date`, `end_date`, `is_current`, `verified_responsibilities (JSONB)`, `embedding (VECTOR(768))` |
| `certifications` | `id (UUID PK)`, `profile_id (FK)`, `name`, `issuer`, `issued_date`, `credential_url`, `verified (BOOL)`, `embedding (VECTOR(768))` |
| `job_sources` | `id (UUID PK)`, `source_name (UNIQUE)`, `connector_type`, `base_url`, `config (JSONB)`, `enabled (BOOL)`, `rate_limit_config (JSONB)`, `last_run_at`, `health_status`, `consecutive_failures`, `last_error` |
| `jobs` | `id (UUID PK)`, `source_id (FK)`, `source_job_id`, `canonical_url (UNIQUE)`, `dedup_key`, `content_checksum`, `company`, `role_title`, `location`, `remote_policy`, `is_remote`, `employment_type`, `salary_*`, `raw_jd (TEXT)`, `application_url`, `posted_at`, `updated_at_source`, `first_seen_at`, `last_seen_at`, `repost_count`, `status`, `expired_at`, `source_provenance (JSONB)`, `jd_embedding (VECTOR(768))` |
| `job_requirements` | `id (UUID PK)`, `job_id (FK UNIQUE)`, `role_category`, `location`, `remote_policy`, `employment_type`, `required_skills (JSONB)`, `preferred_skills (JSONB)`, `trainable_skills (JSONB)`, `responsibilities (JSONB)`, `technical_stack (JSONB)`, `experience_min`, `experience_max`, `education_requirements (JSONB)`, `graduation_criteria (JSONB)`, `mandatory_qualifications (JSONB)`, `preferred_qualifications (JSONB)`, `application_instructions (JSONB)`, `evidence_items (JSONB)`, `extraction_mode`, `internship_eligible`, `fresher_eligible`, `sponsorship_required`, `uncertainties (JSONB)`, `prompt_injection_detected (BOOL)`, `extracted_at` |
| `job_matches` | `id (UUID PK)`, `job_id (FK)`, `profile_id (FK)`, `score`, `eligible (BOOL)`, `hard_constraints_passed (BOOL)`, `required_skill_coverage`, `preferred_skill_coverage`, `semantic_similarity`, `matched_skills (JSONB)`, `missing_required_skills (JSONB)`, `preferred_gaps (JSONB)`, `evidence_mapping (JSONB)`, `missing_evidence (JSONB)`, `eligibility_blockers (JSONB)`, `uncertainties (JSONB)`, `confidence`, `score_breakdown (JSONB)`, `recommendation`, `explanation`, `scored_at` |
| `job_analysis_history` | `id (UUID PK)`, `job_id (FK)`, `profile_id (FK)`, `run_id`, `extraction_mode`, `model_provider`, `overall_score`, `eligible (BOOL)`, `confidence`, `snapshot (JSONB)`, `created_at` |
| `resumes` | `id (UUID PK)`, `profile_id (FK)`, `master_resume_id (FK self)`, `is_master (BOOL)`, `version_label`, `docx_path`, `pdf_path`, `sha256_checksum`, `template_version`, `generation_status`, `ats_report (JSONB)`, `parsed_metadata (JSONB)`, `validation_report (JSONB)`, `evaluation_report (JSONB)`, `structured_content (JSONB)`, `validation_passed (BOOL)`, `created_at` *(immutable artifact store; no approval state)* |
| `auto_tailor_daily_quotas` | `user_id (PK FK)`, `quota_date (PK DATE, Asia/Kolkata)`, `reserved_count (INT >= 0)`, `updated_at` *(row-locked atomic auto-tailoring quota ledger)* |
| `resume_generation_jobs` | `id (UUID PK)`, `user_id (FK)`, `profile_id (FK)`, `job_id (FK)`, `master_resume_id (FK)`, `trigger_mode ('auto'|'manual'|'regenerate')`, `evidence_fingerprint`, `quota_date`, `idempotency_key (partial UNIQUE active)`, `status`, `attempt_count`, `max_attempts`, `worker_id`, `lease_expires_at`, `last_heartbeat_at`, `thread_id`, `resume_id (FK)`, `error_message`, `created_at`, `started_at`, `completed_at` *(PostgreSQL transactional outbox)* |
| `resume_job_mappings` | `id (UUID PK)`, `resume_id (FK)`, `job_id (FK)`, `profile_id (FK)`, `master_resume_id (FK)`, `generation_job_id (FK UNIQUE)`, `run_id`, `trigger_mode`, `version_number`, `is_active (BOOL, partial UNIQUE active completed)`, `evidence_fingerprint`, `status`, `approval_status ('pending_review'|'approved'|'rejected')`, `approval_notes`, `approved_at`, `rejected_at`, `tailoring_diff (JSONB)`, `model_provider`, `token_usage (JSONB)`, `error_message`, `created_at` *(sole canonical approval-state store)* |
| `applications` | `id (UUID PK)`, `user_id (FK)`, `job_id (FK)`, `resume_id (FK)`, `idempotency_key (UNIQUE)`, `application_mode`, `status`, `external_ref_id`, `confirmation_evidence (JSONB)`, `submitted_at`, `confirmed_at`, `next_followup_at` |
| `application_answers` | `id (UUID PK)`, `application_id (FK)`, `question`, `answer`, `evidence_source`, `user_verified (BOOL)` |
| `approval_records` | `id (UUID PK)`, `application_id (FK)`, `decision ('approved'|'rejected')`, `actor`, `notes`, `decided_at` *(append-only / immutable)* |
| `email_notifications` | `id (UUID PK)`, `application_id (FK)`, `notification_type`, `recipient`, `subject`, `delivery_status`, `gmail_message_id`, `sent_at` |
| `interview_kits` | `id (UUID PK)`, `application_id (FK UNIQUE)`, `resume_id (FK)`, `company_research (JSONB)`, `questions (JSONB)`, `solutions (JSONB)`, `dsa_problems (JSONB)`, `system_design (JSONB)`, `behavioral (JSONB)`, `readiness_estimate (NUMERIC)`, `created_at` |
| `interview_attempts` | `id (UUID PK)`, `kit_id (FK)`, `question_ref`, `candidate_answer`, `feedback (JSONB)`, `score (NUMERIC)`, `weak_topics (TEXT[])`, `attempted_at` |
| `workflow_runs` | `id (UUID PK)`, `thread_id (UNIQUE)`, `user_id (FK)`, `job_id (FK nullable)`, `workflow_type`, `current_stage`, `status`, `checkpoint_metadata (JSONB)`, `started_at`, `updated_at` |
| `audit_logs` | `id (UUID PK)`, `actor`, `action`, `resource_type`, `resource_id`, `event_metadata (JSONB)`, `created_at` *(append-only / immutable, PII-redacted)* |

---

## 2. Application Status Lifecycle

```text
DISCOVERED
  -> ANALYZED
  -> ELIGIBLE
  -> MATCHED
  -> RESUME_READY
  -> AWAITING_APPROVAL
  -> APPROVED
  -> SUBMISSION_PENDING
  -> SUBMITTED
  -> CONFIRMED
  -> INTERVIEW / REJECTED / WITHDRAWN
```

### Non-Happy-Path States
- `EXPIRED` — Posting closed prior to generation or submission.
- `FAILED` — Exhausted bounded retries or unrecoverable error.
- `NEEDS_USER_INPUT` — Unanswered or sensitive form question requires candidate input.
- `DUPLICATE` — Blocked by canonical URL or `(user_id, canonical_job_id)` deduplication.
- `MANUAL_ACTION_REQUIRED` — CAPTCHA, MFA, login wall, or unsupported site encountered.

**Critical Invariant:** `SUBMITTED` and `CONFIRMED` must remain strictly distinct. `SUBMITTED` records that an API call or approved form submit completed; `CONFIRMED` is set only after independent verification (receipt ID, confirmation screen, or email).

---

## 3. Critical Integrity Constraints
- Unique `canonical_url` (and composite `(source_id, source_job_id)` where available) on `jobs`.
- Unique `idempotency_key` on `applications` (`sha256(user_id + canonical_job_key)`) to guarantee zero duplicate submissions across retries or LangGraph checkpoint replays.
- Append-only triggers/application rules on `approval_records` and `audit_logs`.
- Encrypted storage for sensitive candidate fields and zero plaintext API keys/OAuth tokens in database logs.
