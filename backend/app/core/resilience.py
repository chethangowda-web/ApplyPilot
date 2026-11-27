from dataclasses import dataclass, field
from datetime import datetime, timezone
import threading
import time
from typing import Any


class CircuitOpenError(RuntimeError):
    """Raised when an external integration circuit breaker is open."""


@dataclass
class CircuitBreaker:
    """Thread-safe three-state circuit breaker (closed -> open -> half_open)."""

    name: str
    failure_threshold: int = 3
    recovery_timeout_seconds: float = 30.0
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _state: str = field(default="closed", init=False)
    _failure_count: int = field(default=0, init=False)
    _opened_at: float | None = field(default=None, init=False)

    @property
    def state(self) -> str:
        with self._lock:
            if self._state == "open" and self._opened_at is not None:
                if (time.monotonic() - self._opened_at) >= self.recovery_timeout_seconds:
                    self._state = "half_open"
            return self._state

    def allow_request(self) -> bool:
        current = self.state
        return current in {"closed", "half_open"}

    def ensure_closed(self) -> None:
        if not self.allow_request():
            raise CircuitOpenError(f"Circuit breaker '{self.name}' is open")

    def record_success(self) -> None:
        with self._lock:
            self._state = "closed"
            self._failure_count = 0
            self._opened_at = None

    def record_failure(self) -> str:
        with self._lock:
            self._failure_count += 1
            if self._state == "half_open" or self._failure_count >= self.failure_threshold:
                self._state = "open"
                self._opened_at = time.monotonic()
            return self._state


class DeadLetterBuffer:
    """Bounded in-process dead-letter record store for failed background tasks (no PII/secrets)."""

    def __init__(self, max_entries: int = 200) -> None:
        self._max = max_entries
        self._lock = threading.Lock()
        self._items: list[dict[str, Any]] = []

    def push(
        self,
        *,
        task_name: str,
        task_id: str,
        error_type: str,
        workflow_id: str | None = None,
        retry_count: int = 0,
    ) -> dict[str, Any]:
        entry = {
            "task_name": task_name,
            "task_id": task_id,
            "error_type": error_type,
            "workflow_id": workflow_id,
            "retry_count": retry_count,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
        }
        with self._lock:
            self._items.insert(0, entry)
            if len(self._items) > self._max:
                self._items = self._items[: self._max]
        return entry

    def list_recent(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._items[:limit])


dead_letter_buffer = DeadLetterBuffer()
