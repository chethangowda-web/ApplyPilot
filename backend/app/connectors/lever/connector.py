from datetime import datetime, timezone
from typing import Any
import httpx
from app.connectors.base import ConnectorError, JobSourceConnector
from app.schemas.workflow import NormalizedJob
from app.services.jd_sanitizer import extract_deterministic_job_signals, html_to_clean_text


def _epoch_ms_to_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(float(value) / 1000.0, tz=timezone.utc)
    except (TypeError, ValueError):
        return None


class LeverConnector(JobSourceConnector):
    """Connector for the public Lever Postings API (read/discovery only)."""

    source_name = "lever"
    connector_type = "official_postings_api"

    def __init__(
        self,
        company_slug: str,
        *,
        company_name: str | None = None,
        base_url: str = "https://api.lever.co/v0/postings",
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.company_slug = company_slug
        self.company_name = company_name or company_slug
        self.base_url = base_url.rstrip("/")

    async def search(
        self,
        query: str = "",
        location: str | None = None,
        filters: dict[str, Any] | None = None,
    ) -> list[NormalizedJob]:
        filters = filters or {}
        limit = int(filters.get("limit", 100))
        max_pages = int(filters.get("max_pages", 5))
        results: list[NormalizedJob] = []

        for page_idx in range(max_pages):
            skip = page_idx * limit
            url = f"{self.base_url}/{self.company_slug}"
            resp = await self._request(
                "GET",
                url,
                params={"mode": "json", "limit": limit, "skip": skip},
            )
            if resp.status_code == 404:
                raise ConnectorError(f"Lever company board not found: {self.company_slug}")
            if resp.status_code >= 400:
                raise ConnectorError(f"Lever error {resp.status_code}")

            raw_jobs = resp.json()
            if not isinstance(raw_jobs, list) or not raw_jobs:
                break

            for raw in raw_jobs:
                norm = self.normalize(raw)
                if query and query.lower() not in f"{norm.job_title} {norm.description}".lower():
                    continue
                if location and location.lower() not in (norm.location or "").lower() and not norm.is_remote:
                    continue
                results.append(norm)

            if len(raw_jobs) < limit:
                break

        return results

    async def fetch_job(self, external_job_id: str) -> NormalizedJob | None:
        url = f"{self.base_url}/{self.company_slug}/{external_job_id}"
        resp = await self._request("GET", url)
        if resp.status_code in (404, 410):
            return None
        if resp.status_code >= 400:
            raise ConnectorError(f"Lever fetch_job HTTP {resp.status_code}")
        return self.normalize(resp.json())

    def normalize(self, raw_job: dict[str, Any]) -> NormalizedJob:
        ext_id = str(raw_job.get("id", ""))
        title = str(raw_job.get("text") or "").strip()
        categories = raw_job.get("categories") or {}
        location = categories.get("location")
        commitment = categories.get("commitment")
        workplace_type = raw_job.get("workplaceType")

        parts: list[str] = []
        if raw_job.get("descriptionPlain"):
            parts.append(str(raw_job["descriptionPlain"]))
        elif raw_job.get("description"):
            parts.append(html_to_clean_text(str(raw_job["description"])))

        for item in raw_job.get("lists") or []:
            if isinstance(item, dict):
                if item.get("text"):
                    parts.append(str(item["text"]))
                if item.get("content"):
                    parts.append(html_to_clean_text(str(item["content"])))

        if raw_job.get("additionalPlain"):
            parts.append(str(raw_job["additionalPlain"]))
        description = "\n".join(p for p in parts if p).strip()

        signals = extract_deterministic_job_signals(
            title=title,
            description=description,
            location=location,
            workplace_type=workplace_type,
            commitment=commitment,
        )

        salary_range = raw_job.get("salaryRange") or {}
        salary_min = salary_range.get("min") if isinstance(salary_range, dict) else None
        salary_max = salary_range.get("max") if isinstance(salary_range, dict) else None
        salary_currency = salary_range.get("currency") if isinstance(salary_range, dict) else None

        canonical_url = str(raw_job.get("hostedUrl") or f"https://jobs.lever.co/{self.company_slug}/{ext_id}")
        apply_url = str(raw_job.get("applyUrl") or canonical_url)

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
            salary_min=float(salary_min) if salary_min is not None else None,
            salary_max=float(salary_max) if salary_max is not None else None,
            salary_currency=salary_currency,
            salary_text=signals["salary_text"],
            posted_at=_epoch_ms_to_dt(raw_job.get("createdAt")),
            discovered_at=datetime.now(timezone.utc),
            application_url=apply_url,
            status="active",
            raw_metadata={
                "company_slug": self.company_slug,
                "team": categories.get("team"),
                "prompt_injection_flags": signals["prompt_injection_flags"],
                "auto_submit_supported": False,
            },
        )

    async def check_status(self, external_job_id: str) -> str:
        url = f"{self.base_url}/{self.company_slug}/{external_job_id}"
        try:
            resp = await self._request("GET", url)
        except (ConnectorError, httpx.HTTPError):
            return "unavailable"
        if resp.status_code == 200:
            return "active"
        if resp.status_code in (404, 410):
            return "expired"
        return "unavailable"
