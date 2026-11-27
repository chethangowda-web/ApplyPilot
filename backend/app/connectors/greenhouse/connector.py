from datetime import datetime, timezone
import time
from typing import Any
import httpx
from app.connectors.base import ConnectorError, JobSourceConnector
from app.schemas.workflow import NormalizedJob
from app.services.discovery_prefilter import ConnectorScanMetrics, evaluate_job_prefilter
from app.services.jd_sanitizer import extract_deterministic_job_signals, html_to_clean_text


def _parse_iso_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


class GreenhouseConnector(JobSourceConnector):
    """Connector for the public Greenhouse Job Board API (read/discovery only)."""

    source_name = "greenhouse"
    connector_type = "official_job_board_api"

    def __init__(
        self,
        board_token: str,
        *,
        company_name: str | None = None,
        base_url: str = "https://boards-api.greenhouse.io/v1/boards",
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.board_token = board_token
        self.company_name = company_name or board_token
        self.base_url = base_url.rstrip("/")
        self.last_scan_metrics = ConnectorScanMetrics()
        self._etag: str | None = None

    async def search(
        self,
        query: str = "",
        location: str | None = None,
        filters: dict[str, Any] | None = None,
    ) -> list[NormalizedJob]:
        """Fetch Greenhouse public board once (API returns full board; ignores page/per_page) and pre-filter locally."""
        t0 = time.monotonic()
        filters = filters or {}
        smart_filter = bool(filters.get("smart_filter", False))
        per_page = filters.get("per_page")
        max_pages = int(filters.get("max_pages", 5))
        max_results = int(filters.get("max_results", 50))
        cursor_offset = int(filters.get("cursor", 0) or 0)
        results: list[NormalizedJob] = []
        metrics = ConnectorScanMetrics()

        url = f"{self.base_url}/{self.board_token}/jobs"
        headers: dict[str, str] = {}
        if filters.get("etag"):
            headers["If-None-Match"] = str(filters["etag"])
        elif self._etag:
            headers["If-None-Match"] = self._etag

        raw_jobs: list[dict[str, Any]] = []
        for page in range(1, max_pages + 1):
            params: dict[str, Any] = {"content": "true"}
            if per_page is not None:
                params["per_page"] = int(per_page)
                params["page"] = page
            resp = await self._request(
                "GET",
                url,
                params=params,
                headers=headers or None,
            )
            metrics.pages_scanned += 1
            if resp.status_code == 304:
                metrics.cache_hit = True
                metrics.duration_ms = (time.monotonic() - t0) * 1000.0
                self.last_scan_metrics = metrics
                return []
            if resp.status_code == 404:
                raise ConnectorError(f"Greenhouse board not found: {self.board_token}")
            if resp.status_code >= 400:
                raise ConnectorError(f"Greenhouse error {resp.status_code}")

            if resp.headers.get("etag"):
                self._etag = resp.headers["etag"]

            payload = resp.json()
            page_jobs = payload.get("jobs", []) if isinstance(payload, dict) else []
            raw_jobs.extend(page_jobs)
            meta = payload.get("meta", {}) if isinstance(payload, dict) else {}
            total_pages = meta.get("total_pages")
            if total_pages is None or page >= int(total_pages) or not page_jobs:
                break

        metrics.raw_fetched = len(raw_jobs)

        # Scan fairly from cursor_offset (wraps or stops at end) without alphabetical truncation bias
        total_raw = len(raw_jobs)
        scanned_indices = range(cursor_offset, total_raw) if cursor_offset < total_raw else range(0, total_raw)
        next_cursor_idx: int | None = None

        for idx in scanned_indices:
            raw = raw_jobs[idx]
            title = str(raw.get("title") or "").strip()
            loc_obj = raw.get("location") or {}
            loc_str = loc_obj.get("name") if isinstance(loc_obj, dict) else str(loc_obj or "") or None

            if smart_filter:
                pre = evaluate_job_prefilter(
                    title=title,
                    location=loc_str,
                    description="",
                    query=query,
                    requested_location=location,
                    smart_filter=True,
                )
                if not pre.keep:
                    if pre.reason in {"role_excluded", "role_non_target"}:
                        metrics.removed_by_role += 1
                    elif pre.reason == "location_outside_target":
                        metrics.removed_by_location += 1
                    elif pre.reason == "experience_senior":
                        metrics.removed_by_experience += 1
                    continue

            norm = self.normalize(raw)
            if smart_filter:
                post = evaluate_job_prefilter(
                    title=norm.job_title,
                    location=norm.location,
                    description=norm.description,
                    is_remote_flag=norm.is_remote,
                    experience_min=norm.experience_min,
                    query=query,
                    requested_location=location,
                    smart_filter=True,
                )
                if not post.keep:
                    if post.reason in {"role_excluded", "role_non_target"}:
                        metrics.removed_by_role += 1
                    elif post.reason == "location_outside_target":
                        metrics.removed_by_location += 1
                    elif post.reason == "experience_senior":
                        metrics.removed_by_experience += 1
                    continue
            else:
                if query and query.lower() not in f"{norm.job_title} {norm.description}".lower():
                    metrics.removed_by_role += 1
                    continue
                if location and location.lower() not in (norm.location or "").lower() and not norm.is_remote:
                    metrics.removed_by_location += 1
                    continue

            results.append(norm)
            if len(results) >= max_results and idx + 1 < total_raw:
                next_cursor_idx = idx + 1
                break

        metrics.shortlisted = len(results)
        metrics.next_cursor = str(next_cursor_idx) if next_cursor_idx is not None else None
        metrics.duration_ms = (time.monotonic() - t0) * 1000.0
        self.last_scan_metrics = metrics
        return results

    async def fetch_job(self, external_job_id: str) -> NormalizedJob | None:
        url = f"{self.base_url}/{self.board_token}/jobs/{external_job_id}"
        resp = await self._request("GET", url)
        if resp.status_code in (404, 410):
            return None
        if resp.status_code >= 400:
            raise ConnectorError(f"Greenhouse fetch_job HTTP {resp.status_code}")
        return self.normalize(resp.json())

    def normalize(self, raw_job: dict[str, Any]) -> NormalizedJob:
        ext_id = str(raw_job.get("id", ""))
        title = str(raw_job.get("title") or "").strip()
        loc_obj = raw_job.get("location") or {}
        location = loc_obj.get("name") if isinstance(loc_obj, dict) else str(loc_obj or "") or None
        raw_html = str(raw_job.get("content") or "")
        description = html_to_clean_text(raw_html)
        signals = extract_deterministic_job_signals(
            title=title,
            description=description,
            location=location,
        )
        canonical_url = str(raw_job.get("absolute_url") or f"https://boards.greenhouse.io/{self.board_token}/jobs/{ext_id}")

        return NormalizedJob(
            source=self.source_name,
            source_job_id=ext_id or None,
            external_job_id=ext_id or None,
            canonical_url=canonical_url,
            company=self.company_name,
            job_title=title,
            description=description,
            location=location,
            remote_policy=signals["remote_policy"],
            is_remote=signals["is_remote"],
            employment_type=signals["employment_type"],
            experience_min=signals["experience_min"],
            experience_max=signals["experience_max"],
            required_skills=signals["required_skills"],
            preferred_skills=signals["preferred_skills"],
            education_requirements=signals["education_requirements"],
            salary_text=signals["salary_text"],
            posted_at=_parse_iso_dt(raw_job.get("first_published")),
            updated_at=_parse_iso_dt(raw_job.get("updated_at")),
            discovered_at=datetime.now(timezone.utc),
            application_url=canonical_url,
            status="active",
            raw_metadata={
                "board_token": self.board_token,
                "departments": [d.get("name") for d in (raw_job.get("departments") or []) if isinstance(d, dict)],
                "prompt_injection_flags": signals["prompt_injection_flags"],
                "auto_submit_supported": False,
            },
        )

    async def check_status(self, external_job_id: str) -> str:
        url = f"{self.base_url}/{self.board_token}/jobs/{external_job_id}"
        try:
            resp = await self._request("GET", url)
        except (ConnectorError, httpx.HTTPError):
            return "unavailable"
        if resp.status_code == 200:
            return "active"
        if resp.status_code in (404, 410):
            return "expired"
        return "unavailable"
