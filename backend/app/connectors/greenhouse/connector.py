from datetime import datetime, timezone
from typing import Any
import httpx
from app.connectors.base import ConnectorError, JobSourceConnector
from app.schemas.workflow import NormalizedJob
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

    async def search(
        self,
        query: str = "",
        location: str | None = None,
        filters: dict[str, Any] | None = None,
    ) -> list[NormalizedJob]:
        filters = filters or {}
        per_page = int(filters.get("per_page", 100))
        max_pages = int(filters.get("max_pages", 5))
        results: list[NormalizedJob] = []

        for page in range(1, max_pages + 1):
            url = f"{self.base_url}/{self.board_token}/jobs"
            resp = await self._request(
                "GET",
                url,
                params={"content": "true", "per_page": per_page, "page": page},
            )
            if resp.status_code == 404:
                raise ConnectorError(f"Greenhouse board not found: {self.board_token}")
            if resp.status_code >= 400:
                raise ConnectorError(f"Greenhouse error {resp.status_code}")

            payload = resp.json()
            raw_jobs = payload.get("jobs", []) if isinstance(payload, dict) else []
            for raw in raw_jobs:
                norm = self.normalize(raw)
                if query and query.lower() not in f"{norm.job_title} {norm.description}".lower():
                    continue
                if location and location.lower() not in (norm.location or "").lower() and not norm.is_remote:
                    continue
                results.append(norm)

            meta = payload.get("meta", {}) if isinstance(payload, dict) else {}
            total_pages = meta.get("total_pages")
            if not raw_jobs:
                break
            if total_pages is not None:
                if page >= int(total_pages):
                    break
            elif len(raw_jobs) < per_page:
                break

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
