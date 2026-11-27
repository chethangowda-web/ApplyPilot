# ApplyPilot AI — Implementation Roadmap & Testing Strategy

Build and validate **one milestone at a time**. Preserve passing tests and a working system after every milestone.

---

## 1. Eight-Milestone Plan

| Milestone | Scope | Key Deliverable & Acceptance Criteria |
|---|---|---|
| **1. Foundation** *(Current)* | Repo scaffold, Docker/PostgreSQL/`pgvector`/Redis, FastAPI skeleton, auth, Master Profile models, Master Resume upload & parsing | Candidate can securely upload a Master Resume, edit verified profile facts, and retrieve the profile via API. |
| **2. Job Discovery** | Greenhouse & Lever connectors, permitted search/career connectors, `NormalizedJob`, deduplication, posting-status checks | Connectors return normalized jobs with provenance, timestamps, and duplicate rejection. |
| **3. Job Intelligence** | Gemini JD extraction (`JobRequirements`), schema validation, hard eligibility rules, 100-pt weighted scoring | Every discovered job has structured requirements, deterministic eligibility decision, and explainable score. |
| **4. Resume Intelligence** | Profile evidence linking, content-derived fingerprints, Claude-first tailoring, PDF-only PyMuPDF generation, PyMuPDF ATS parsing check, authoritative per-claim factual validator, 7-metric evaluator, PostgreSQL transactional outbox + Celery/Redis worker, canonical `ResumeJobMapping` approval | Generated PDF resumes are ATS-parseable, 100% fact-checked, linked to target job IDs, and strictly gated behind owner `pending_review` -> `approved` decisions. |
| **5. Application Automation** | Form schema parsing, verified answer mapping, Policy Router, LangGraph `interrupt()` human approval, Playwright isolated workflow, submission verification | Applications can be prepared, paused for approval, submitted only via permitted routes, and verified before `CONFIRMED`. |
| **6. Career Intelligence** | Gmail OAuth2 notifications, Application Tracker, Interview Kit generator (8 modules), adaptive Mock Interview, Interview Memory, Follow-ups | Confirmed applications trigger accurate emails and generate persisted, resume-grounded Interview Kits. |
| **7. Web & Desktop** | Shared Next.js + React 8-section dashboard, SSE/WebSocket live updates, Tauri Windows shell, tray controls, desktop notifications, Celery + Redis scheduler | Web and desktop share FastAPI backend behavior, show live workflow state, and run scheduled discovery safely. |
| **8. Production Hardening** | Circuit breakers, dead-letter queue, idempotency stress tests, prompt-injection tests, cost/token observability, 50–100 JD evaluation suite | Full E2E test suite passes; system hardened for reliable daily personal operation. |

---

## 2. Mandatory Behavioral Test Matrix

Before enabling live submissions, build a **50–100 representative JD evaluation dataset** and enforce automated tests for all 12 critical scenarios:

| Scenario | Required Expected Behavior |
|---|---|
| **Duplicate job** | Rejected or linked to existing canonical record |
| **Expired job** | Blocked from tailoring/submission (`EXPIRED`) |
| **Missing profile detail** | Pauses in `NEEDS_USER_INPUT` and requests user input |
| **Unsupported application API** | Routes through human approval (`interrupt()`) and permitted path |
| **Invalid / unverified resume fact** | Fails validation gate and blocks submission |
| **LLM provider outage** | Safe fallback via `ModelRouter` or graceful pause (never bypasses validation) |
| **Browser timeout after submit** | Checks/verifies submission status before any retry |
| **Repeated workflow execution** | Idempotency key prevents duplicate submission |
| **Rejected human approval** | Transitions to skipped/rejected; zero submission action |
| **Confirmed application** | Updates tracker to `CONFIRMED`, sends accurate Gmail notice, triggers Interview Kit |
| **Prompt injection in JD** | Treated as untrusted data; ignored by system & tools |
| **CAPTCHA encountered** | Stops immediately (`MANUAL_ACTION_REQUIRED`) and notifies user |

---

## 3. Recommended Development Sequence

1. PRD, Architecture, Agent Specs, Schema, Security & Roadmap docs *(Milestone 1 setup)*.
2. Core config, PostgreSQL + `pgvector` schema & Alembic migrations, auth, Master Profile & Resume Vault.
3. Discovery connectors + Deduplication.
4. JD Intelligence + Transparent Scoring Engine.
5. Resume Intelligence + Deterministic ATS/Fact Validation Gates.
6. Application Policy Router + `interrupt()` Approval + Verification.
7. Gmail Notifications + Interview Kits + Interview Memory.
8. Next.js Web Dashboard + Tauri Desktop + Celery Automation + Hardening.

---

## 4. Milestone 1 Verification Status — **CLOSED / ACCEPTED** (2026-11-25)

### Passed (Live Infrastructure & Full Test Suite)
- **Docker Desktop & Containers:** Verified Docker Engine `29.7.2` (`linux`). Configured non-colliding host ports in `docker-compose.yml` & `.env.example` (`POSTGRES_PORT=5434`, `REDIS_PORT=6381`, `BACKEND_PORT=8002`) so ApplyPilot runs cleanly alongside existing local containers (`orbit-*`, `policy-rag-*`, `devlens-*`). Started `applypilot-postgres` (`pgvector/pgvector:pg16`, port `5434`) and `applypilot-redis` (`redis:7-alpine`, port `6381`), both verified `(healthy)`.
- **Live PostgreSQL Migrations (`tests/live_postgres_verify.py`):** Executed `alembic upgrade head`, verified `vector` extension on PostgreSQL `16.15`, verified all 8 tables (`users`, `candidate_profiles`, `candidate_skills`, `projects`, `experiences`, `certifications`, `resumes`, `audit_logs`), foreign keys, indexes, and `candidate_skills.embedding` type `vector(768)`, ran direct ORM CRUD + `VECTOR(768)` round-trip, and verified `alembic downgrade base` → `alembic upgrade head` reapply.
- **Live Redis & Live PostgreSQL API Integration:** Verified Redis `PING`, user registration/login/JWT `/api/auth/me`, `POST /api/profile`, `POST /api/profile/skills`, immutable PDF & DOCX upload/parsing/listing/download, duplicate SHA-256 rejection (`409`), cross-user isolation (`404`), and invalid JWT rejection (`401`) against live PostgreSQL.
- **Automated Test Suite (`python -m pytest -v`):** **16 passed** (unit, SQLite migration/rollback, offline PostgreSQL DDL, auth, profile CRUD & isolation, resume vault, production `SECRET_KEY` enforcement, bcrypt bounds, path traversal, and log PII/secret redaction).
- **Lint & Compose Validation:** `python -m ruff check app tests alembic` passed with 0 errors; `docker compose config -q` exited `0`.

### Milestone 1 Acceptance Criteria Sign-off
- Candidate can securely register/authenticate, upload immutable Master PDF/DOCX resumes, edit verified profile facts with strict per-user isolation, and retrieve structured profiles and resume versions backed by live PostgreSQL (`pgvector`) + Redis. **Milestone 1 is officially closed.**

---

## 5. Milestone 2 Verification Status — **CLOSED / ACCEPTED** (2026-11-25)

### Implemented Scope
- **Connectors & Compliance:** `JobSourceConnector` ABC, `GreenhouseConnector` (pagination, HTML sanitization, status checks), `LeverConnector` (pagination, `salaryRange`, status checks), `CareerSiteConnector` (JSON-LD `JobPosting`, `robots.txt` check, CAPTCHA/login refusal), and `assert_permitted_url` strictly blocking LinkedIn scraping/automation.
- **Normalization & Prompt-Injection Defense:** Expanded `NormalizedJob` (`external_job_id`, `remote_policy`, `is_remote`, `employment_type`, `experience_min/max`, `required/preferred_skills`, `education_requirements`, `salary_*`, `posted_at/updated_at/discovered_at`, `raw_metadata`) and `jd_sanitizer.py` flagging untrusted injection directives.
- **Persistence, Deduplication & Freshness:** Migration `0002_milestone_2_job_discovery.py` adding `job_sources`, `jobs`, `job_requirements`, and `job_matches` (`VECTOR(768)`, unique `canonical_url`, `(source_id, source_job_id)`, `(job_id, profile_id)`), plus multi-tier deduplication, `first_seen_at`/`last_seen_at` tracking, repost counting, and non-destructive expiry.
- **Deterministic Eligibility & Ranking:** `scoring_service.py` enforcing target roles (`SDE`, `SWE`, `AI/ML`, `Applied AI`, `Backend`), India + Remote Worldwide geography, Internship/Fresher/`0–2` years experience, education/graduation checks, missing-data uncertainty handling, and 100-pt transparent weighted scoring.
- **LangGraph & FastAPI APIs:** `build_discovery_graph` / `run_discovery_workflow` with `MemorySaver` checkpointer + `/api/jobs/sources`, `/api/jobs/sources/health`, `/api/jobs/discover`, `/api/jobs`, `/api/jobs/{id}`, `/api/jobs/{id}/match`, `/api/jobs/{id}/analyze`, `/api/jobs/{id}/status`.

### Verification Results (2026-11-25)
- **Live PostgreSQL (`16.15`, `pgvector`) & Redis:** Verified `applypilot-postgres` (`5434`) & `applypilot-redis` (`6381`) healthy; ran `live_postgres_verify.py` verifying `0001` + `0002` upgrade/downgrade/reapply, all 12 tables, `jobs.jd_embedding` `vector(768)`, direct ORM CRUD, and live Milestone 1 + Milestone 2 discovery/scoring APIs.
- **Automated Test Suite (`python -m pytest -v`):** **26 passed in 12.55s** (connectors, pagination, rate limits, timeouts, robots/CAPTCHA/LinkedIn boundaries, prompt-injection flagging, deduplication, reposts, expiry, eligibility/missing-data gates, LangGraph discovery graph, repeated workflow idempotency, and all Milestone 1 tests).
- **Lint & Compose Config:** `python -m ruff check app tests alembic` passed (`All checks passed!`); `docker compose config -q` exited `0`.
- **Milestone 2 Acceptance Criteria:** Connectors return normalized jobs with provenance, timestamps, duplicate/repost handling, expiry tracking, deterministic eligibility & ranking, and LangGraph state execution backed by live PostgreSQL (`pgvector`) + Redis. **Milestone 2 is officially closed.**

---

## 6. Milestone 3 Verification Status — **CLOSED / ACCEPTED** (2026-11-25)

### Implemented Scope
- **Stage 1 — `ModelRouter` (`app/llm/`):** Provider-independent `ModelRouter` with `GeminiProvider` primary, `ClaudeProvider`, and `OpenAICompatibleProvider`, structured Pydantic validation, bounded retries, exponential backoff, fallback routing, token/cost tracking, and key scrubbing.
- **Stage 2 — `JDAnalyzerAgent` (`app/agents/job_intelligence/jd_analyzer.py`):** Extracts required/preferred/trainable skills, responsibilities, experience, education/graduation criteria, mandatory/preferred qualifications, and application instructions with verbatim JD evidence quotes and randomized `<UNTRUSTED_JD_DATA_*>` prompt-injection delimiters + deterministic fallback.
- **Stage 3 & 4 — Semantic Matching & Explainable Scoring (`semantic_matching_service.py`, `scoring_service.py`, `database/types.py`):** 768-dim embedding sync (`pgvector` `<=>` `cosine_distance` comparator on PostgreSQL, cosine fallback on SQLite) across skills, projects, experiences, and certifications; strict separation of `missing_evidence` vs. `confirmed_missing_skills`; transparent rubric with blockers, skill coverages, confidence/uncertainty, and explicit non-probability disclaimer.
- **Stage 5 — LangGraph & PostgreSQL Checkpointing (`checkpointer.py`, `main.py`, `intelligence_graph.py`, `discovery_graph.py`, migration `0003`):** `AsyncPostgresSaver` native checkpointing on compatible event loops plus `_SyncPostgresAsyncBridge` over `PostgresSaver` for Windows `ProactorEventLoop` (initialized once at FastAPI startup/before write locks; `MemorySaver` on SQLite), `WorkflowRun` + immutable `JobAnalysisHistory` tracking, and idempotent thread replay.
- **Stage 6 & 7 — 60-JD Benchmark & Intelligence APIs:** Curated 60-JD benchmark (`benchmark_dataset.py`, `eval_benchmark.py`) and endpoints `/api/jobs/{id}/analyze`, `/api/jobs/{id}/intelligence`, `/api/jobs/{id}/evidence`, `/api/jobs/{id}/eligibility`, `/api/jobs/{id}/history`, and `/api/workflows/{thread_id}`.

### Verification Results (2026-11-25)
- **Live PostgreSQL (`16.15`, `pgvector`) & Redis (`tests/live_postgres_verify.py`):** Verified `applypilot-postgres` (`5434`) & `applypilot-redis` (`6381`) healthy; ran Alembic `0003 -> 0002 -> 0001 -> base -> 0001 -> 0002 -> 0003` round-trip, direct ORM CRUD + `VECTOR(768)` check, live pgvector `cosine_distance` queries, LangGraph `checkpoints` row persistence (`checkpoint_blobs`, `checkpoint_migrations`, `checkpoint_writes`, `checkpoints` alongside all 14 application tables + `alembic_version`), and Milestone 1–3 API endpoints (`/api/jobs/{id}/analyze`, `/intelligence`, `/evidence`, `/eligibility`, `/history`, `/api/workflows/{thread_id}`).
- **60-JD Evaluation Benchmark (`python -u tests/eval_benchmark.py`):**
  - `dataset_size`: `60`
  - `required_skills_metrics`: Precision `0.9053`, Recall `1.0`, F1 `0.9503`
  - `preferred_skills_metrics`: Precision `1.0`, Recall `1.0`, F1 `1.0`
  - `experience_extraction_accuracy`: `1.0`
  - `education_extraction_accuracy`: `1.0`
  - `evidence_grounding_rate`: `0.9953`
  - `eligibility_accuracy`: `0.9667`
  - `scoring_consistency_rate`: `1.0`
  - `prompt_injection_detection`: Precision `1.0`, Recall `1.0`, F1 `1.0`
- **Automated Test Suite (`python -m pytest -v`):** **38 passed in 13.55s** (0 failed, 0 skipped; 2 third-party pytest deprecation warnings from `anyio`/`dash`).
- **Lint & Compose Config:** `python -m ruff check app tests alembic` passed (`All checks passed!`); `docker compose config -q` exited `0`.
- **Known Limitations:** Live external LLM API calls (`GeminiProvider` / `ClaudeProvider` / `OpenAICompatibleProvider`) were validated with mocked HTTP transports and fallback tests rather than billed live provider credentials in CI; offline deterministic extraction runs when provider API keys are unset.
- **Milestone 3 Acceptance Criteria:** Explainable AI Job Intelligence evaluates discovered opportunities against verified candidate evidence with strict grounding, prompt-injection isolation, hard eligibility overrides, pgvector semantic matching, PostgreSQL LangGraph checkpointing, and versioned analysis history. **Milestone 3 is officially closed.**

---

## 7. Milestone 4 Verification Status — **CLOSED / ACCEPTED** (2026-11-26)

### Implemented Scope
- **Stage 1 — Schema, Config & Migrations (`0004_milestone_4`):** Added `AUTO_TAILOR_ENABLED`, `AUTO_TAILOR_DAILY_LIMIT`, `AUTO_TAILOR_MIN_SCORE` (strictly separate from `MIN_JOB_MATCH_SCORE`), extended `resumes` (immutable artifact metadata + validation/evaluation/structured JSON), added `auto_tailor_daily_quotas`, `resume_generation_jobs` (PostgreSQL transactional outbox with worker lease/heartbeat fields & `uq_resume_gen_jobs_active_idem`), and `resume_job_mappings` (sole canonical `pending_review` / `approved` / `rejected` state store with `uq_resume_job_mapping_active_completed`).
- **Stage 2 — Evidence, Fingerprinting & PDF Rendering:** `ResumeAnalyzerAgent` (PDF/DOCX/missing-master fallback), `EvidenceRetriever` (pgvector ranking, unverified skill & `planned_features` exclusion, content-derived education/achievement IDs), SHA-256 `compute_evidence_fingerprint`, and single-template ATS PDF rendering via PyMuPDF `fitz.Story` with clickable URI annotations, overflow guard, and round-trip text validation.
- **Stage 3 — Tailoring, ATS, Authoritative Validation & Evaluation:** `ResumeTailoringAgent` (Claude-first via `ModelRouter` with `<UNTRUSTED_JD_DATA_*>` isolation & deterministic fallback + `TailoringDiff`), `ATSOptimizationAgent`, authoritative per-claim `FactualValidatorAgent` (zero silent passes; catches cross-project technology leakage, promoted `planned_features`, fabricated metrics/skills/certs, altered dates/GPAs; optional non-overriding OpenAI cross-check), and 7-metric `ResumeEvaluatorAgent`.
- **Stage 4 — LangGraph Resume Intelligence Subgraph:** `build_resume_intelligence_graph` / `run_resume_intelligence_workflow` with `managed_checkpointer`, bounded revision retries, validation-before-PDF enforcement, atomic active-version replacement (preserving prior active versions on failure/crash and cleaning up orphaned PDFs on rollback), and non-blocking post-commit auto-tailoring triggers in `discovery_graph.py` & `intelligence_graph.py`.
- **Stage 5 — Durable Outbox & Celery/Redis Worker:** `quota_service.py` (atomic `Asia/Kolkata` daily quota reservation via `ON CONFLICT DO NOTHING` + `SELECT ... FOR UPDATE`), `outbox_service.py` (`TransactionalOutboxDispatcher`, `FOR UPDATE SKIP LOCKED` claiming, lease renewal, graceful shutdown release, expired-lease & pending-outbox crash recovery, and `reconcile_orphaned_tailored_pdfs` grace-period orphan cleanup after hard worker crashes), and `app/workers/celery_app.py` + `resume_tasks.py` (`task_acks_late=True`, `task_reject_on_worker_lost=True`, bounded retries).
- **Stage 6 — FastAPI Resume Intelligence APIs:** `POST /api/jobs/{id}/tailor`, `GET /api/jobs/{id}/resumes/history`, `GET /api/resumes/tailored`, `GET /api/resumes/{id}/tailored`, `GET /api/resumes/{id}/diff`, `GET /api/resumes/{id}/validation`, `GET /api/resumes/{id}/download` (SHA-256 integrity + `approved_only`), `POST /api/resumes/{id}/regenerate`, `POST /api/resumes/{id}/approve`, `POST /api/resumes/{id}/reject`, plus server-side `require_approved_tailored_resume_for_submission`.

### Verification Results (2026-11-26)
- **Automated Test Suite (`python -m pytest -q`, `task-1168`):** **71 passed, 2 warnings in 26.98s** (exit code `0`; uses in-memory/file-backed SQLite, `TestClient`, mocked LLM HTTP transports, and subprocess `os._exit` / fault-injection hooks for deterministic crash/adversarial tests).
- **Lint & Compose Config (`task-1132`):** `python -m ruff check app tests` (`All checks passed!`) and `docker compose config --quiet` (`docker compose config OK`), exit code `0`.
- **Disposable PostgreSQL Alembic `0004` Audit (`tests/live_postgres_verify.py` against `applypilot_alembic_audit`, `task-1116`):** Verified `0001..0004` upgrade → `base` downgrade → `0001..0004` reapply, `pgvector` `VECTOR(768)`, ORM CRUD, and live M1–M3 APIs on PostgreSQL `16.15` (exit code `0`) without downgrading the main `applypilot` database.
- **Live PostgreSQL + Redis + Celery Worker Verification (`tests/live_stage5_verify.py`, `task-1132`):** Passed all 6 live infrastructure checks (exit code `0`):
  - 12-thread concurrent atomic daily quota enforcement (`limit=5`) on live PostgreSQL
  - 8-thread concurrent auto/manual logical idempotency
  - 4-thread `FOR UPDATE SKIP LOCKED` concurrent worker claiming
  - Live `celery -A app.workers.celery_app worker --pool=solo` execution over Redis (`6381`) + PostgreSQL (`5434`) + `PostgresSaver` checkpoints + PDF generation
  - Expired-lease crash recovery completed by live Celery worker
  - Live FastAPI Stage 6 endpoints → Celery execution → `404` cross-user isolation → SHA-256 PDF download → 6-thread concurrent `approve` vs `reject` locking.
- **Known Limitations:** Live external paid LLM API calls (`ClaudeProvider` / `OpenAICompatibleProvider` / `GeminiProvider`) were verified using `httpx.MockTransport` and deterministic fallback rather than live external API keys; DOCX is supported only for master resume ingestion (tailored output is PDF-only per finalized product preference).
- **Milestone 4 Acceptance Criteria:** **Milestone 4 is officially CLOSED / ACCEPTED.**

---

## 8. Milestone 5 Verification Status — **CLOSED / ACCEPTED** (2026-11-26)

### Implemented Scope
- **Stage 1 — Schema, Encryption, Security & Migration (`0005_milestone_5`):** Extended `app/core/config.py`, `.env.example`, and `app/core/security.py` with versioned AES-256-GCM field encryption (`ENCRYPTION_KEY`, `ENCRYPTION_KEY_VERSION`, `ENCRYPTION_PREVIOUS_KEYS`, `encrypt_sensitive_answer`, `decrypt_sensitive_answer`, `compute_answers_hash`, `compute_approval_snapshot_hash`), added `applications`, `application_answers`, and append-only `approval_records` in `app/database/models.py` and `alembic/versions/0005_milestone_5_applications.py` (including partial unique index `uq_applications_active_user_canonical_job`, SQLite/PostgreSQL check constraints on status/mode/encryption, and PostgreSQL triggers `trg_approval_records_immutable` & `trg_audit_logs_immutable` via `applypilot_prevent_append_only_mutation()`), plus Pydantic v2 contracts in `app/schemas/applications.py`.
- **Stage 2 — Policy Router, Form Understanding & LangGraph Approval Flow (`application_policy_service.py`, `form_understanding.py`, `application_graph.py`):** Deterministic `ApplicationPolicyRouter` enforcing LinkedIn/CAPTCHA/login/pause/daily-quota/canonical-dedup/expired-posting/approved-resume gates; `FormUnderstandingAgent` with `classify_sensitive_question` and strict verified-evidence mapping; and `build_application_graph` / `run_application_prepare_workflow` with `interrupt()` pause for `NEEDS_USER_INPUT` and `AWAITING_APPROVAL`.
- **Stage 3 (Checkpoints A–D & Hold-and-Confirm Hardening) — Isolated Browser Execution & Manual Handoff (`execution_adapters.py`, `browser_executor.py`, `_browser_worker.py`, `application_execution_service.py`, `scripts/provision_browser_firewall.ps1`):**
  - Kept `OfficialApiSubmissionAdapter` registry fail-closed empty (no invented ATS submission APIs).
  - Implemented `LocalSSRFEgressProxy` (`127.0.0.1` ephemeral bind, single DNS resolution + every-IP validation blocking private/loopback/link-local/metadata/CGNAT/reserved/multicast, TCP 80/443 only, direct validated-IP socket connect), Chromium forced-proxy flags with QUIC/WebRTC/UDP/service-worker disabled, and Windows `Win32JobSupervisor.spawn_suspended_in_job()` lifecycle supervision + optional OS firewall rule provisioning script (`scripts/provision_browser_firewall.ps1`).
  - Implemented supervised visible Playwright prefill (`_browser_worker.py` + `SubmitGuardState`) that **never submits automatically**, holds user-initiated submission routes (`pending_submission_id`) with single-use `ManualSubmitAuthorization` verification, enforces Hold-and-Confirm payload binding (`expected_payload_sha256` required at API/manager/worker boundaries, `parse_submitted_request_payload`, `_verify_wire_payload_locked`, narrow `ALLOWED_CSRF_FIELD_NAMES`), blocks server-side redirects (`forward_held_route_safely` via `route.fetch(max_redirects=0)` covering HTTP 301/302/303/307/308), and writes masked PII-free artifacts.
  - Added FastAPI `/api/applications` routes (`prepare`, list, detail, `answers`, `approve`, `reject`, `execute`, `handoff/{handoff_session_id}`, `authorize-manual-submit`, `handoff/{handoff_session_id}/close`, `submit-outcome`, `reconcile`, `artifacts/{artifact_id}`) enforcing authenticated owner derivation, TTL/snapshot/checksum checks, and strict separation of `USER_REPORTED_SUBMITTED` vs. `CONFIRMED`.

### Verification Results (2026-11-26)
- **Automated Test Suite (`python -m pytest -v`):** **118 passed, 0 failed, 0 skipped** across all Milestones 1–5 suites (`test_milestone5_stage1_schema.py`, `test_milestone5_stage2_policy_and_forms.py`, `test_milestone5_stage3_execution.py`, `test_milestone5_checkpoint_d_api_and_service.py`, plus all existing M1–M4 tests), including Hold-and-Confirm JS/XHR/fetch/redirect adversarial containment and mandatory `expected_payload_sha256` rejection tests.
- **Lint & PowerShell Validation:** `python -m ruff check app tests` (`All checks passed!`) and PowerShell AST parser check on `scripts/provision_browser_firewall.ps1` (`ps_parse_errors=0`).
- **Disposable PostgreSQL Alembic `0005` Audit (`applypilot_m5_audit` on PostgreSQL `16.15`):** Verified clean baseline upgrade to `0004_milestone_4` → upgrade to `0005_milestone_5` (`applications`, `application_answers`, `approval_records`, indexes, foreign keys, check constraints, and append-only triggers `trg_approval_records_immutable` & `trg_audit_logs_immutable` blocking mutation) → downgrade to `0004_milestone_4` (clean removal of M5 tables, triggers, and trigger function) → re-upgrade to `0005_milestone_5` without touching the development `applypilot` database.
- **Known Limitations:** Live ATS portal accounts and billed external LLM APIs were not called in CI (validated against local HTTP fixtures, Playwright Chromium, and `httpx.MockTransport` / deterministic fallbacks); `OfficialApiSubmissionAdapter` registry remains intentionally empty until official ATS API credentials/contracts are configured.
- **Milestone 5 Acceptance Criteria:** **Milestone 5 is officially CLOSED / ACCEPTED.**

## 9. Milestone 7 Verification Status — **CLOSED / ACCEPTED** (2026-11-27)

### Implemented Scope (`apps/web`, `feat/milestone-7-web-frontend`)
- **Stage 1 — Workflow-First Next.js Architecture & HttpOnly BFF Security (`apps/web/src/app/api/*`, `lib/server-backend.ts`, `lib/workflow-engine.ts`, `components/WorkflowWorkspace.tsx`):**
  - Built an 11-step guided sequential workflow UI (`WorkflowStepper`, `WorkflowWorkspace`, `WorkflowStageBody`, `MyWorkflowsView`) driven strictly by authoritative backend state (`deriveJobWorkflowState`).
  - Implemented Next.js BFF session routes (`/api/session/login`, `/api/session/register`, `/api/session/logout`, `/api/session/me`) and allowlisted `/api/backend/[...path]` proxy storing JWTs strictly in `HttpOnly` cookies (`SameSite=Lax`, `Secure` in production, zero `localStorage` tokens), enforcing same-origin CSRF checks on state-changing routes, and preventing open-redirect or path-traversal bypasses.
- **Stage 2 — Job Discovery & Resume Intelligence (`DiscoverJobsView.tsx`, `Stage5ResumeReview.tsx`):**
  - Implemented job discovery with search/filters/pagination, eligibility and existing-application detection, JD intelligence extraction, rubric fit scoring, Celery tailored-resume outbox polling/recovery, diff/unsupported-claim inspection, explicit resume approval/rejection, and authenticated `blob:` PDF preview/download without leaking server paths or tokens.
- **Stage 3 — Application Preparation & Answer Verification (`Stage6To8ApplicationPrep.tsx`):**
  - Implemented Step 6 (`Stage6PreparePanel` with policy clearance, daily quota display, and disabled/unavailable `official_api` mode enforcement), Step 7 (`Stage7QuestionsPanel` distinguishing **Verified Profile Fact**, **AI Draft (Verify)**, **User Confirmed**, and **Requires Your Input** with masked `type="password"` sensitive inputs and explicit server-side AES-256-GCM encryption), and Step 8 (`Stage8ApprovalPanel` binding target job, destination URL, resume SHA-256 checksum, and verified answers to an immutable `ApprovalRecord`).
- **Stage 4 — Browser Handoff, Hold-and-Confirm & Additive Captured Request Preview (`Stage9HandoffPanel.tsx`, `HoldAndConfirmPanel.tsx`, `backend/app/schemas/applications.py`, `backend/app/services/browser_executor.py`, `backend/app/services/_browser_worker.py`):**
  - Built Step 9 (`Stage9HandoffPanel` supporting launch, session status refresh, active-session recovery, safe close, fill outcomes, unsupported fields, and expired/failed session handling without auto-submitting) and Step 10 (`HoldAndConfirmPanel` displaying `pending_submission_id`, `expected_form_action`, `expected_http_method`, `detected_at`, `expires_at`, and mandatory `expected_payload_sha256`).
  - Added the minimal additive `PendingSubmissionFieldPreview` / `PendingSubmissionInfo.captured_fields` backend extension derived strictly from `parse_submitted_request_payload` wire bytes, using form metadata (`label`, `input_type`, `autocomplete`, `question_key`) with fail-closed masking for sensitive, CSRF, and unknown/ambiguous fields.
- **Stage 5 — Submission Verification & Application Tracking (`Stage11VerificationPanel.tsx`, `ApplicationsTrackerView.tsx`, `ApplicationDetailTimelineView.tsx`):**
  - Built Step 11 outcome recording (`submitted_in_browser`, `interrupted`, `failed`) and reconciliation boundary keeping `USER_REPORTED_SUBMITTED`, `UNVERIFIED`, and `CONFIRMED` strictly separate, plus `/applications` tracker with status filters and `/applications/[applicationId]` backend-derived chronological timeline.

### Verification Results (2026-11-27)
- **Frontend Suite (`apps/web`):** `npm run typecheck`, `npm run lint`, `npm run test` (**8 test files, 33 tests passed**), and `npm run build` all passed cleanly.
- **Backend Suite (`backend`):** `python -m ruff check app tests alembic` passed (`All checks passed!`); `python -m pytest -q` passed (**119 passed, 0 failed**, including adversarial Hold-and-Confirm & `captured_fields` wire-preview masking tests).
- **Live Full-Stack E2E (Disposable PostgreSQL, Redis, FastAPI, Celery, Next.js, Local Fixture Employer):** Passed end-to-end across Stages 1–5 including Step 7 provenance/encryption/blocked-approval, Step 9–10 live Hold-and-Confirm release (`0` fixture POSTs before confirmation, `1` after confirmation, `409`/`422`/`404` security rejections), and Step 11 `/applications` tracker + `/applications/{id}` timeline + reload/logout/login persistence.
- **Remaining Limitations:** Production `_CONFIRMATION_VERIFIER_REGISTRY` remains empty until **Milestone 6** (Gmail OAuth / ATS webhook trusted confirmation integrations); `BrowserHandoffManager` holds live Chromium worker handles in memory per backend process.
- **Milestone 7 Acceptance Criteria:** **Milestone 7 is officially CLOSED / ACCEPTED.**

---

## 10. Milestone 6, Milestone 8 & Desktop Verification Status (2026-11-27)

### Implemented Scope
- **Milestone 6 — Career Intelligence (`0006_milestone_6` & `0007_milestone_6_career`):**
  - **Gmail OAuth2 & Independent Confirmation:** PKCE S256 + single-use Redis `state` (`600s` TTL) + Next.js BFF `/api/integrations/gmail/callback` (`HttpOnly` cookie session binding), AES-256-GCM OAuth refresh-token encryption (`oauth:{user_id}:{provider}:{token_kind}:{version}` AAD), `gmail.readonly` incremental `historyId` sync + bounded `newer_than:14d` fallback, deterministic-first `email_confirmation_matcher.py`, and server-owned `GmailConfirmationVerifier` registered in `_CONFIRMATION_VERIFIER_REGISTRY` (FastAPI lifespan & Celery workers), plus `/settings` and Step 11 confirmation UI.
  - **Follow-ups, 8-Module Interview Kit, Adaptive Mock Interview & Interview Memory:** `ApplicationFollowup` (editable drafts, snooze/complete, never auto-sends), `InterviewKit` (8 modules grounded in approved resume SHA-256 & verified evidence, progress/export), `InterviewSession`/`InterviewAttempt` (adaptive difficulty & transparent rubric), `InterviewMemory` (user-controlled CRUD), and `/interviews` UI.
- **Milestone 8 — Production Hardening & Observability:**
  - `CircuitBreaker` & `DeadLetterBuffer` (`app/core/resilience.py`), `ObservabilityRegistry` & per-workflow cost budgets (`app/services/observability_service.py`), `/health/ready` & `/api/ops/observability` (`app/api/routes/ops.py`), operational recovery runbook (`docs/OPERATIONAL_RECOVERY.md`), and email confirmation precision/recall evaluation (`test_milestone8_hardening_and_eval.py`).
- **Desktop Shell (`apps/desktop`):**
  - Tauri v2 shell (`src-tauri/tauri.conf.json`, `Cargo.toml`, `src/main.rs`) reusing `apps/web` with CSP, notification plugin, and `/health/ready` check (no local secret/token storage).

### Known Limitations
- Live Google Cloud OAuth2 consent flow requires configuring `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET` in `.env` (validated in CI with `httpx.MockTransport`).
- Rust/Cargo is not installed on the local Windows host, so `cargo tauri build` native binary compilation is pending Rust toolchain installation.



