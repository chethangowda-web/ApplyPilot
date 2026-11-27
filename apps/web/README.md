# ApplyPilot AI — Workflow-First Web Frontend (Stage 1)

Guided sequential Next.js + TypeScript web interface for ApplyPilot AI connected directly to the FastAPI backend (`backend/`).

## Architecture (Stage 1)
- **Workflow-First Experience:**
  - `My Workflows` (`/`) lists active job workflows derived strictly from persisted backend state (`Job`, `JobRequirements`, `JobMatch`, `ResumeJobMapping`, `Application`).
  - `/workflows/[jobId]` guides one job at a time through all **11 stages** (Job Discovery -> JD Analysis -> Fit & Eligibility -> Resume Tailoring -> Resume Review & Approval -> Application Preparation -> Question Completion -> Final Application Approval -> Browser Prefill & Handoff -> Hold-and-Confirm Submission -> Submission Verification & Tracking).
  - Secondary views (`Discover Jobs` `/discover`, `Profile and Resumes` `/profile`, `Settings` `/settings`) support prerequisites without dashboard clutter.
- **No `localStorage` Authority:** Workflow step states, progress percentage, prerequisites, and blocked/failed/waiting states come strictly from backend APIs. Session auth uses `HttpOnly` cookie routes (`/api/session/*`) + server-to-server proxy (`/api/backend/[...path]`).
- **Backend Gap Audit:** See [`../../docs/WORKFLOW_PERSISTENCE_GAPS.md`](../../docs/WORKFLOW_PERSISTENCE_GAPS.md).

## Local Development

```bash
cd apps/web
npm install
BACKEND_API_URL=http://127.0.0.1:8002 npm run dev
```

## Quality Checks

```bash
npm run typecheck
npm run lint
npm run test
npm run build
```
