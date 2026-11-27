from functools import lru_cache
from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_INSECURE_SECRET_MARKERS = ("change-me", "dev-only", "secret-key", "example")


class Settings(BaseSettings):
    """Application settings loaded from environment variables or .env."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = Field(default="ApplyPilot", alias="APP_NAME")
    environment: str = Field(default="development", alias="ENVIRONMENT")
    timezone: str = Field(default="Asia/Kolkata", alias="TIMEZONE")

    database_url: str = Field(
        default="postgresql+psycopg://applypilot:applypilot@localhost:5434/applypilot",
        alias="DATABASE_URL",
    )
    redis_url: str = Field(default="redis://localhost:6381/0", alias="REDIS_URL")

    gemini_api_key: str = Field(default="", alias="GEMINI_API_KEY")
    anthropic_api_key: str = Field(default="", alias="ANTHROPIC_API_KEY")
    openai_api_key: str = Field(default="", alias="OPENAI_API_KEY")
    search_api_key: str = Field(default="", alias="SEARCH_API_KEY")

    google_client_id: str = Field(default="", alias="GOOGLE_CLIENT_ID")
    google_client_secret: str = Field(default="", alias="GOOGLE_CLIENT_SECRET")
    google_oauth_redirect_uri: str = Field(
        default="http://localhost:3000/api/integrations/gmail/callback",
        alias="GOOGLE_OAUTH_REDIRECT_URI",
    )
    gmail_confirmation_window_days: int = Field(
        default=14,
        ge=1,
        le=90,
        alias="GMAIL_CONFIRMATION_WINDOW_DAYS",
    )
    gmail_sync_interval_minutes: int = Field(
        default=15,
        ge=1,
        le=1440,
        alias="GMAIL_SYNC_INTERVAL_MINUTES",
    )
    gmail_sync_max_messages: int = Field(
        default=50,
        ge=1,
        le=250,
        alias="GMAIL_SYNC_MAX_MESSAGES",
    )
    gmail_sync_beat_enabled: bool = Field(
        default=False,
        alias="GMAIL_SYNC_BEAT_ENABLED",
    )

    secret_key: str = Field(
        default="dev-only-secret-key-change-me-minimum-32-bytes",
        alias="SECRET_KEY",
    )
    encryption_key: str = Field(default="", alias="ENCRYPTION_KEY")
    encryption_key_version: str = Field(default="v1", alias="ENCRYPTION_KEY_VERSION")
    encryption_previous_keys: str = Field(default="", alias="ENCRYPTION_PREVIOUS_KEYS")
    access_token_expire_minutes: int = Field(default=60, ge=5, le=10080, alias="ACCESS_TOKEN_EXPIRE_MINUTES")

    storage_dir: str = Field(default="../storage", alias="STORAGE_DIR")
    max_resume_size_bytes: int = Field(default=5 * 1024 * 1024, ge=1024, alias="MAX_RESUME_SIZE_BYTES")

    daily_application_target: int = Field(default=15, ge=1, le=50, alias="DAILY_APPLICATION_TARGET")
    max_applications_per_day: int = Field(default=15, ge=1, le=50, alias="MAX_APPLICATIONS_PER_DAY")
    min_job_match_score: int = Field(default=75, ge=0, le=100, alias="MIN_JOB_MATCH_SCORE")
    discovery_max_analyzed_jobs_per_run: int = Field(
        default=40,
        ge=1,
        le=250,
        alias="DISCOVERY_MAX_ANALYZED_JOBS_PER_RUN",
    )
    discovery_smart_filter_enabled: bool = Field(
        default=False,
        alias="DISCOVERY_SMART_FILTER_ENABLED",
    )

    auto_tailor_enabled: bool = Field(default=True, alias="AUTO_TAILOR_ENABLED")
    auto_tailor_daily_limit: int = Field(default=5, ge=0, le=50, alias="AUTO_TAILOR_DAILY_LIMIT")
    auto_tailor_min_score: int = Field(default=75, ge=0, le=100, alias="AUTO_TAILOR_MIN_SCORE")

    auto_submit_supported_apis: bool = Field(default=True, alias="AUTO_SUBMIT_SUPPORTED_APIS")
    browser_submission_requires_approval: bool = Field(
        default=True,
        alias="BROWSER_SUBMISSION_REQUIRES_APPROVAL",
    )
    pause_all_submissions: bool = Field(default=False, alias="PAUSE_ALL_SUBMISSIONS")
    approval_ttl_hours: int = Field(default=24, ge=1, le=720, alias="APPROVAL_TTL_HOURS")
    application_artifact_retention_days: int = Field(
        default=14,
        ge=1,
        le=365,
        alias="APPLICATION_ARTIFACT_RETENTION_DAYS",
    )
    browser_execution_timeout_seconds: int = Field(
        default=60,
        ge=10,
        le=600,
        alias="BROWSER_EXECUTION_TIMEOUT_SECONDS",
    )
    browser_handoff_ttl_seconds: int = Field(
        default=1800,
        ge=60,
        le=7200,
        alias="BROWSER_HANDOFF_TTL_SECONDS",
    )
    capture_application_artifacts: bool = Field(
        default=False,
        alias="CAPTURE_APPLICATION_ARTIFACTS",
    )
    require_os_egress_firewall: bool = Field(
        default=False,
        alias="REQUIRE_OS_EGRESS_FIREWALL",
    )
    os_egress_firewall_rule_prefix: str = Field(
        default="ApplyPilot-Browser-Egress",
        alias="OS_EGRESS_FIREWALL_RULE_PREFIX",
    )

    @model_validator(mode="after")
    def validate_production_secrets(self) -> "Settings":
        env = self.environment.strip().lower()
        if env in {"production", "prod", "staging"}:
            key = self.secret_key.strip()
            lowered = key.lower()
            if len(key) < 32 or any(marker in lowered for marker in _INSECURE_SECRET_MARKERS):
                raise ValueError(
                    "SECRET_KEY must be at least 32 characters and not a default/placeholder value in production"
                )
        return self

    def validate_encryption_configured(self) -> None:
        """Fail-closed check that ENCRYPTION_KEY is configured and non-placeholder."""
        enc = self.encryption_key.strip()
        if len(enc) < 16 or any(marker in enc.lower() for marker in _INSECURE_SECRET_MARKERS):
            raise ValueError(
                "ENCRYPTION_KEY must be configured (>= 16 chars, non-placeholder)"
            )


@lru_cache
def get_settings() -> Settings:
    """Return cached Settings instance."""
    return Settings()
