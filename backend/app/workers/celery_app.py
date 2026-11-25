from celery import Celery
from app.core.config import get_settings


def create_celery_app(redis_url: str | None = None) -> Celery:
    """Create the durable Celery application backed by Redis with late acknowledgements."""
    cfg = get_settings()
    broker = redis_url or cfg.redis_url
    app = Celery(
        "applypilot_workers",
        broker=broker,
        backend=broker,
        include=["app.workers.resume_tasks"],
    )
    app.conf.update(
        task_serializer="json",
        result_serializer="json",
        accept_content=["json"],
        timezone="Asia/Kolkata",
        enable_utc=True,
        # Durable delivery & crash safety
        task_acks_late=True,
        task_reject_on_worker_lost=True,
        worker_prefetch_multiplier=1,
        task_track_started=True,
        worker_cancel_long_running_tasks_on_connection_loss=True,
        # Fail fast if Redis broker is unreachable so discovery & outbox persistence stay non-blocking
        broker_connection_retry_on_startup=False,
        broker_transport_options={
            "socket_connect_timeout": 2,
            "socket_timeout": 2,
            "retry_on_timeout": False,
        },
        result_backend_transport_options={
            "socket_connect_timeout": 2,
            "socket_timeout": 2,
        },
        task_soft_time_limit=180,
        task_time_limit=240,
    )
    return app


celery_app = create_celery_app()
