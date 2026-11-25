from datetime import datetime, timezone
from app.core.config import get_settings
from app.main import app, health_check
from app.schemas.workflow import ApplicationStatus, JobRequirements, NormalizedJob


def test_health_endpoint_direct():
    payload = health_check()
    assert payload["status"] == "ok"
    assert payload["app"] == "ApplyPilot"
    assert payload["timezone"] == "Asia/Kolkata"
    assert app.title == "ApplyPilot AI API"


def test_settings_defaults():
    settings = get_settings()
    assert settings.daily_application_target == 15
    assert settings.min_job_match_score == 75
    assert settings.browser_submission_requires_approval is True


def test_workflow_schemas():
    job = NormalizedJob(
        source="greenhouse",
        source_job_id="gh-123",
        canonical_url="https://boards.greenhouse.io/example/jobs/123",
        company="Example AI",
        job_title="AI Engineer Intern",
        discovered_at=datetime.now(timezone.utc),
        description="Python, FastAPI, LangGraph",
    )
    reqs = JobRequirements(
        job_title=job.job_title,
        company=job.company,
        role_category="AI Engineer",
        required_skills=["Python", "FastAPI", "LangGraph"],
    )
    assert job.status == "active"
    assert reqs.required_skills == ["Python", "FastAPI", "LangGraph"]
    assert ApplicationStatus.SUBMITTED != ApplicationStatus.CONFIRMED
