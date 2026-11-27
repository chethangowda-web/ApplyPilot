# ApplyPilot AI — Personal Daily Getting Started Guide

## 1. Prerequisites
- Docker Desktop (running PostgreSQL `pgvector` on port `5434` & Redis on port `6381`)
- Python `3.11+` (`backend/` dependencies installed)
- Node.js `20+` (`apps/web/` dependencies installed)

## 2. Environment Setup
Ensure `.env` (or `backend/.env`) configures at minimum:
- `DATABASE_URL=postgresql+psycopg://applypilot:applypilot@localhost:5434/applypilot`
- `REDIS_URL=redis://localhost:6381/0`
- `SECRET_KEY` (32+ random characters, non-placeholder in production)
- `ENCRYPTION_KEY` & `ENCRYPTION_KEY_VERSION=v1` (AES-256-GCM for sensitive answers & OAuth tokens)
- `DISCOVERY_SMART_FILTER_ENABLED=true` & `DISCOVERY_MAX_ANALYZED_JOBS_PER_RUN=40`
- Optional LLM keys: `GEMINI_API_KEY`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`
- Optional Gmail OAuth2 (`gmail.readonly`): `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_OAUTH_REDIRECT_URI=http://localhost:3000/api/integrations/gmail/callback`

## 3. Startup Commands (4 Terminals)

### Terminal 1 — Infrastructure & Migrations
```powershell
docker start applypilot-postgres applypilot-redis
cd "d:\resume design\ApplyPilot-AI\backend"
python -m alembic upgrade head
```

### Terminal 2 — FastAPI Backend (`http://127.0.0.1:8002`)
```powershell
cd "d:\resume design\ApplyPilot-AI\backend"
python -m uvicorn app.main:app --host 127.0.0.1 --port 8002
```
*(Keep `--workers 1` when using visible Chromium handoff sessions.)*

### Terminal 3 — Celery Worker
```powershell
cd "d:\resume design\ApplyPilot-AI\backend"
python -m celery -A app.workers.celery_app worker --pool=solo -l info
```

### Terminal 4 — Next.js Frontend (`http://localhost:3000`)
```powershell
cd "d:\resume design\ApplyPilot-AI\apps\web"
npm run start
```
*(Or `npm run dev` for development.)*

## 4. Health Checks
- Backend liveness: `http://127.0.0.1:8002/health`
- Backend readiness (PostgreSQL + Redis): `http://127.0.0.1:8002/health/ready`
- Web app: `http://localhost:3000` (Settings page shows backend health and Gmail connection status).

## 5. Daily Job-Search Procedure
1. Open `http://localhost:3000` and sign in (`HttpOnly` cookie session).
2. Confirm your Master Profile & verified skills/projects on **Profile and Resumes** (`/profile`).
3. Open **Discover Jobs** (`/discover`), run discovery against your configured Greenhouse/Lever/career sources, and review eligibility + match explanations.
4. Open a suitable job workflow (`/workflows/[jobId]`), review **JD Analysis** & **Candidate Fit** (`>= 75` recommended).
5. **Tailor Resume** (queued via Celery) → inspect diff & factual validation → explicitly **Approve Resume**.
6. Complete **Application Prep** (verify AI drafts, enter sensitive answers securely) → explicitly **Approve Application**.
7. Launch **Visible Browser Handoff** → inspect form → trigger submit → verify exact **Hold-and-Confirm** payload preview (`expected_payload_sha256`) → authorize release.
8. Record submission outcome (`submitted_in_browser` / `interrupted`) and track confirmations on **Applications** (`/applications`) and **Interview Lab** (`/interviews`).

## 6. Gmail OAuth2 Setup (Optional, Pending Credentials)
1. In Google Cloud Console, enable **Gmail API**.
2. Configure OAuth Consent Screen with scope `https://www.googleapis.com/auth/gmail.readonly` only.
3. Create a Web OAuth Client with Redirect URI `http://localhost:3000/api/integrations/gmail/callback`.
4. Add `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET` to `.env`, restart backend, and connect via `/settings`.

## 7. Troubleshooting & Safe Shutdown
- **Database/Redis unreachable:** Run `docker start applypilot-postgres applypilot-redis` and re-check `/health/ready`.
- **Safe shutdown:** Stop Next.js (`Ctrl+C`), Celery (`Ctrl+C`), and Uvicorn (`Ctrl+C`); leave Docker containers running or stop with `docker stop applypilot-postgres applypilot-redis`.
