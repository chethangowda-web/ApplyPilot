import asyncio
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse
import httpx
from app.schemas.workflow import NormalizedJob

_BLOCKED_DOMAINS = ("linkedin.com", "lnkd.in")


class ConnectorError(Exception):
    """Base error for source connectors."""


class RateLimitError(ConnectorError):
    """Raised when a job source returns HTTP 429 or exhausts retry budget."""


class BlockedSourceError(ConnectorError):
    """Raised when a URL/domain is forbidden by compliance rules or robots.txt."""


@dataclass
class ConnectorHealth:
    source_name: str
    status: str  # healthy | degraded | unhealthy
    last_latency_ms: float | None = None
    consecutive_failures: int = 0
    last_error: str | None = None
    checked_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


def assert_permitted_url(url: str) -> None:
    """Enforce compliance boundaries: never scrape/automate LinkedIn or non-HTTP schemes."""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise BlockedSourceError(f"Unsupported URL scheme: {parsed.scheme}")
    host = (parsed.hostname or "").lower()
    if any(host == dom or host.endswith(f".{dom}") for dom in _BLOCKED_DOMAINS):
        raise BlockedSourceError(
            "Unauthorized LinkedIn scraping/automation is prohibited by ApplyPilot security policy"
        )


class JobSourceConnector(ABC):
    """Common interface for all permitted job discovery connectors."""

    source_name: str
    connector_type: str

    def __init__(
        self,
        *,
        timeout_seconds: float = 15.0,
        max_retries: int = 2,
        min_interval_seconds: float = 0.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.min_interval_seconds = min_interval_seconds
        self._external_client = client
        self._last_request_ts = 0.0
        self._consecutive_failures = 0
        self._last_error: str | None = None
        self._last_latency_ms: float | None = None

    async def _throttle(self) -> None:
        if self.min_interval_seconds <= 0:
            return
        elapsed = time.monotonic() - self._last_request_ts
        if elapsed < self.min_interval_seconds:
            await asyncio.sleep(self.min_interval_seconds - elapsed)

    async def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        assert_permitted_url(url)
        last_exc: Exception | None = None

        for attempt in range(self.max_retries + 1):
            await self._throttle()
            start = time.monotonic()
            try:
                if self._external_client is not None:
                    resp = await self._external_client.request(
                        method, url, timeout=self.timeout_seconds, **kwargs
                    )
                else:
                    async with httpx.AsyncClient(timeout=self.timeout_seconds, follow_redirects=True) as client:
                        resp = await client.request(method, url, **kwargs)
                self._last_request_ts = time.monotonic()
                self._last_latency_ms = round((self._last_request_ts - start) * 1000, 2)

                if resp.status_code == 429:
                    retry_after = float(resp.headers.get("Retry-After", "0") or 0)
                    last_exc = RateLimitError(f"Rate limited (429) by {self.source_name}")
                    if attempt < self.max_retries:
                        await asyncio.sleep(min(retry_after or (0.05 * (2**attempt)), 2.0))
                        continue
                    self._record_failure(str(last_exc))
                    raise last_exc

                if resp.status_code >= 500:
                    last_exc = ConnectorError(f"Upstream {resp.status_code} from {self.source_name}")
                    if attempt < self.max_retries:
                        await asyncio.sleep(0.05 * (2**attempt))
                        continue
                    self._record_failure(str(last_exc))
                    raise last_exc

                self._consecutive_failures = 0
                self._last_error = None
                return resp

            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                self._last_request_ts = time.monotonic()
                last_exc = ConnectorError(f"Network/timeout error on {self.source_name}: {type(exc).__name__}")
                if attempt < self.max_retries:
                    await asyncio.sleep(0.05 * (2**attempt))
                    continue
                self._record_failure(str(last_exc))
                raise last_exc from exc

        raise last_exc or ConnectorError(f"Request failed for {self.source_name}")

    def _record_failure(self, message: str) -> None:
        self._consecutive_failures += 1
        self._last_error = message

    def get_health(self) -> ConnectorHealth:
        if self._consecutive_failures == 0:
            status = "healthy"
        elif self._consecutive_failures < 3:
            status = "degraded"
        else:
            status = "unhealthy"
        return ConnectorHealth(
            source_name=self.source_name,
            status=status,
            last_latency_ms=self._last_latency_ms,
            consecutive_failures=self._consecutive_failures,
            last_error=self._last_error,
        )

    @abstractmethod
    async def search(
        self,
        query: str = "",
        location: str | None = None,
        filters: dict[str, Any] | None = None,
    ) -> list[NormalizedJob]:
        """Search active openings from this source."""

    @abstractmethod
    async def fetch_job(self, external_job_id: str) -> NormalizedJob | None:
        """Fetch a single job by external source ID, or None if not found/expired."""

    @abstractmethod
    def normalize(self, raw_job: dict[str, Any]) -> NormalizedJob:
        """Normalize raw source payload into canonical NormalizedJob."""

    @abstractmethod
    async def check_status(self, external_job_id: str) -> str:
        """Return active | expired | unavailable for an existing external job ID."""
