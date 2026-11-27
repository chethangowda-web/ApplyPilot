# ApplyPilot AI — Backend Workflow Persistence & Contract Gap Audit

Before implementing Milestone 7 Stage 1 (`apps/web`), all existing FastAPI routes and Pydantic v2 contracts under `backend/app/api/routes/` and `backend/app/schemas/` were inspected. Because `localStorage` must never be treated as the authoritative record of application state, Stage 1 derives workflow step state, progress percentage, prerequisites, and exceptional states strictly from backend records (`Job`, `JobRequirements`, `JobMatch`, `ResumeJobMapping`, `ResumeGenerationJob`, `Application`, `ApplicationAnswer`, `ApprovalRecord`, and `HandoffSessionInfo`).

## Verified Backend Strengths Already Used by Stage 1
- **Server-authoritative gates & transitions:** `ALLOWED_APPLICATION_TRANSITIONS`, `ResumeJobMapping.approval_status` (`pending_review` / `approved` / `rejected`), `ApplicationAnswer.user_verified`, immutable `ApprovalRecord` snapshots, and `PendingSubmissionInfo` (`payload_sha256`) are enforced server-side.
- **Zero-auto-submit Hold-and-Confirm:** `POST /api/applications/{application_id}/authorize-manual-submit` mandates `expected_payload_sha256` matching the intercepted held route.

## Identified Backend Persistence & API Gaps (for Future Minimal Extensions)

1. **`GET /api/applications` Lacks Job Metadata & Pre-Preparation Workflows**
   - `GET /api/applications` returns bare `ApplicationResponse` IDs (`job_id`, `resume_id`) without `company` or `role_title`, and only covers jobs where `POST /api/applications/prepare` has already been called.
   - Jobs already in Stages 2–5 (`JobRequirements`, `JobMatch`, `ResumeGenerationJob`, `ResumeJobMapping`) have no single unified list endpoint (`GET /api/workflows/{thread_id}` queries a single thread).
   - **Stage 1 handling:** `apps/web` composes `/api/jobs`, `/api/resumes/tailored`, `/api/applications`, and per-job `/api/jobs/{job_id}/resumes/history` + `/api/applications/{application_id}` exclusively from FastAPI.
   - **Minimal future extension:** Add a `GET /api/workflows/overview` summary endpoint joining active jobs, latest match/tailoring status, and application status cleanly in one query.

2. **Transient `FormMappingResult` & `PolicyDecision` on `POST /api/applications/prepare`**
   - `ApplicationPreparationResult` returns rich `FormMappingResult` (`field_type`, `required`, `options`, `sensitive_category`, `reason`) and `PolicyDecision` (`blocker_reasons`, `warnings`, `daily_used`, `daily_limit`), but `ApplicationDetailResponse` (`GET /api/applications/{application_id}`) returns only persisted `ApplicationAnswerResponse` rows (`question_key`, `question`, `is_sensitive`, `answer_preview`, `evidence_source`, `user_verified`) without field type metadata or stored policy warnings.
   - **Stage 1 handling:** Progress and pending verification remain 100% persisted via `Application.status` and `ApplicationAnswer.user_verified`, while newly returned `ApplicationPreparationResult` enhances the active Stage 6/7 session and gracefully falls back to persisted `ApplicationAnswerResponse` inputs on resume.
   - **Minimal future extension:** Persist non-sensitive form field metadata (`field_type`, `required`, `options`) and policy summary JSON on `applications` and expose them in `ApplicationDetailResponse`.

3. **Held Wire Payload Readable Field Preview on `PendingSubmissionInfo`**
   - `PendingSubmissionInfo` exposes `pending_submission_id`, `target_url`, `http_method`, `payload_sha256`, `detected_at`, and `expires_at`, while the parsed field preview of the held HTTP submission body is not yet returned in `PendingSubmissionInfo`.
   - **Stage 1 handling:** Hold-and-Confirm displays the live `PendingSubmissionInfo` metadata, the approved tailored resume checksum, and the persisted `ApplicationAnswerResponse` summary (with sensitive fields displayed strictly as `[REDACTED — VERIFIED]` / `[REDACTED — UNVERIFIED]`).
   - **Minimal future extension:** Expose a redacted `safe_held_fields` summary on `PendingSubmissionInfo` directly from `SubmitGuardState._verify_wire_payload_locked`.

4. **Server-Persisted Pause State & Bookmarked Viewing Step**
   - Workflow progress automatically saves via backend state transitions; however, `Application` / `WorkflowRun` does not currently store an explicit `paused_by_user` boolean or `bookmarked_step` when inspecting an earlier completed step.
   - **Stage 1 handling:** Backward step inspection uses URL query state (`?step=N`) while authoritative unlock state and completion stay strictly derived from FastAPI records.

5. **CORS & Non-Secret Policy Settings Endpoint**
   - `backend/app/main.py` does not configure `CORSMiddleware`, and `GET /health` exposes only `{ status, app, environment, timezone }` without policy limits (`max_applications_per_day`, `auto_tailor_daily_limit`, `pause_all_submissions`).
   - **Stage 1 handling:** `apps/web` serves same-origin Next.js BFF Route Handlers (`/api/session/*` and `/api/backend/[...path]`) backed by an `HttpOnly` session cookie, eliminating CORS failures and keeping JWTs out of `localStorage` without modifying the committed Milestone 5 backend.

6. **Stage 2 — Master Resume Selection on `POST /api/jobs/{job_id}/tailor`**
   - `TailorJobRequest` currently accepts `{ use_llm, force_regenerate }` without a `master_resume_id` override; the backend outbox service automatically binds generation to the candidate's latest uploaded Master Resume (`is_master=True`).
   - **Stage 2 handling:** Stage 4 displays all uploaded Master Resumes from `GET /api/resume/versions`, highlights the latest Master Resume that the backend will use, and prompts the user to upload one in `/profile` if none exists.
   - **Minimal future extension:** Add an optional `master_resume_id: uuid.UUID | None` to `TailorJobRequest` if multi-master selection per job is desired.
