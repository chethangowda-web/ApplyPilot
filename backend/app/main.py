from contextlib import asynccontextmanager
from fastapi import FastAPI
from app.api.routes.applications import router as applications_router
from app.api.routes.auth import router as auth_router
from app.api.routes.career import router as career_router
from app.api.routes.integrations import router as integrations_router
from app.api.routes.jobs import router as jobs_router
from app.api.routes.ops import router as ops_router
from app.api.routes.profile import router as profile_router
from app.api.routes.resume import router as resume_router
from app.api.routes.resumes_intelligence import router as resumes_intelligence_router
from app.api.routes.workflows import router as workflows_router
from app.core.config import get_settings
from app.core.logging import get_logger
from app.database.session import get_session_factory
from app.graphs.checkpointer import ensure_postgres_checkpointer_setup
from app.services.gmail_confirmation_verifier import register_gmail_confirmation_verifier
from app.services.outbox_service import recover_outbox_jobs

settings = get_settings()
logger = get_logger("applypilot.main")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    register_gmail_confirmation_verifier()
    db_url = get_settings().database_url
    if db_url.startswith("postgresql"):
        try:
            ensure_postgres_checkpointer_setup(db_url)
        except Exception as exc:
            logger.warning(
                "startup_checkpointer_setup_skipped",
                extra={"extra_fields": {"error_type": type(exc).__name__}},
            )
        try:
            with get_session_factory()() as db:
                recover_outbox_jobs(db)
        except Exception as exc:
            logger.warning(
                "startup_outbox_recovery_skipped",
                extra={"extra_fields": {"error_type": type(exc).__name__}},
            )
    try:
        yield
    finally:
        from app.services import application_policy_service as policy_service
        from app.services.gmail_confirmation_verifier import GmailConfirmationVerifier

        policy_service._CONFIRMATION_VERIFIER_REGISTRY.pop(GmailConfirmationVerifier.verifier_id, None)


app = FastAPI(
    title=f"{settings.app_name} AI API",
    version="0.1.0",
    description="Backend API for ApplyPilot AI multi-agent job acquisition system.",
    lifespan=lifespan,
)

app.include_router(auth_router)
app.include_router(profile_router)
app.include_router(resume_router)
app.include_router(resumes_intelligence_router)
app.include_router(jobs_router)
app.include_router(workflows_router)
app.include_router(applications_router)
app.include_router(integrations_router)
app.include_router(career_router)
app.include_router(ops_router)


@app.get("/health", tags=["system"])
def health_check() -> dict[str, str]:
    """Return service health and active environment."""
    return {
        "status": "ok",
        "app": settings.app_name,
        "environment": settings.environment,
        "timezone": settings.timezone,
    }
