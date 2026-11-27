# ApplyPilot AI — Operational Recovery & Deployment Guide

## 1. PostgreSQL Backup & Restore
```powershell
docker exec applypilot-postgres pg_dump -U applypilot -Fc applypilot > applypilot_backup.dump
docker exec -i applypilot-postgres pg_restore -U applypilot -d applypilot --clean --if-exists < applypilot_backup.dump
```

## 2. Migration Safety
Always test Alembic upgrades/downgrades against a disposable database before running against the development/production database:
```powershell
cd backend
python -m alembic upgrade head
```

## 3. Worker Crash & Uncertain Submission Recovery
- **Celery outbox tasks (`resume_tasks`, `gmail_tasks`):** Configured with `task_acks_late=True` and `task_reject_on_worker_lost=True`. Expired leases are reclaimed via `recover_outbox_jobs(db)` on startup.
- **Browser Handoff Sessions:** Live Chromium handles are process-local (`BrowserHandoffManager`). If the backend or browser worker crashes mid-submit, **never** automatically retry an employer submission. Mark uncertain outcomes `UNVERIFIED` and reconcile via Gmail read-only confirmation sync or manual verification.
- **Multi-worker Deployment:** Run FastAPI with a single worker (`--workers 1`) when active `visible_handoff` Chromium sessions are held in process memory, or route handoff requests with sticky session affinity.
