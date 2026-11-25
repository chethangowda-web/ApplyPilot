# ApplyPilot AI

Personal multi-agent job discovery, truthful resume tailoring, policy-controlled application operations, and interview preparation system built with **LangGraph**, **FastAPI**, **PostgreSQL (`pgvector`)**, **Redis + Celery**, **Next.js**, and **Tauri**.

## Architecture & Documentation

Start with the specification suite in `docs/`:
- [PRD](docs/PRD.md) — Product vision, target roles, core principles, and 12-step workflow
- [System Architecture](docs/ARCHITECTURE.md) — 6-layer architecture, LangGraph Master & Subgraphs, Multi-Model Router, FastAPI contracts
- [Agent Specifications](docs/AGENT_SPECIFICATIONS.md) — Contracts for all 20 logical agents & deterministic services
- [Database Schema](docs/DATABASE_SCHEMA.md) — PostgreSQL + `pgvector` tables, application status lifecycle, and constraints
- [Security & Compliance](docs/SECURITY.md) — Prompt injection defense, fact protection, LinkedIn/CAPTCHA boundaries, auditability
- [Implementation Roadmap](docs/IMPLEMENTATION_ROADMAP.md) — 8-milestone plan and 12-scenario behavioral test matrix
- [Antigravity System Instructions](docs/ANTIGRAVITY_SYSTEM_INSTRUCTIONS.md) — Engineering rules for incremental milestone development

## Current Status: Milestone 1 Scaffold

- Repository directory layout (`apps/`, `backend/`, `storage/`, `infrastructure/`, `docs/`)
- Local infrastructure definition (`docker-compose.yml`, `.env.example`)
- FastAPI + Pydantic v2 Milestone 1 skeleton (`backend/app/main.py`, `core/config.py`, `schemas/workflow.py`)
- Smoke tests (`backend/tests/test_health.py`)

## Quick Start (Local Development)

```bash
cp .env.example .env
docker compose up -d --wait postgres redis
cd backend
pip install -e ".[dev]"
alembic upgrade head
uvicorn app.main:app --reload --port 8002
pytest
```
