import hashlib
import json
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser
from bs4 import BeautifulSoup
from app.connectors.base import BlockedSourceError, ConnectorError, JobSourceConnector, assert_permitted_url
from app.schemas.workflow import NormalizedJob
from app.services.jd_sanitizer import (
    contains_auth_or_captcha_wall,
    extract_deterministic_job_signals,
    html_to_clean_text,
)


def _parse_dt(val: str | None) -> datetime | None:
    if not val:
        return None
    try:
        return datetime.fromisoformat(val.replace("Z", "+00:00"))
    except ValueError:
        return None


class CareerSiteConnector(JobSourceConnector):
    """Permitted public career-page connector honoring robots.txt and anti-bot/auth boundaries."""

    source_name = "career_site"
    connector_type = "public_career_page"

    def __init__(
        self,
        careers_url: str,
        *,
        company_name: str,
        user_agent: str = "ApplyPilotBot",
        respect_robots: bool = True,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        assert_permitted_url(careers_url)
        self.careers_url = careers_url
        self.company_name = company_name
        self.user_agent = user_agent
        self.respect_robots = respect_robots

    async def _check_robots_allowed(self, target_url: str) -> None:
        if not self.respect_robots:
            return
        parsed = urlparse(target_url)
        robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
        try:
            resp = await self._request("GET", robots_url)
        except ConnectorError:
            return
        if resp.status_code == 200 and resp.text:
            rp = RobotFileParser()
            rp.parse(resp.text.splitlines())
            if not rp.can_fetch(self.user_agent, target_url):
                raise BlockedSourceError(f"Blocked by robots.txt policy for {target_url}")

    async def search(
        self,
        query: str = "",
        location: str | None = None,
        filters: dict[str, Any] | None = None,
    ) -> list[NormalizedJob]:
        await self._check_robots_allowed(self.careers_url)
        resp = await self._request("GET", self.careers_url)
        if resp.status_code in (401, 403):
            raise BlockedSourceError(f"Authentication or access barrier ({resp.status_code}) on {self.careers_url}")
        if resp.status_code >= 400:
            raise ConnectorError(f"Career page HTTP {resp.status_code} on {self.careers_url}")

        if contains_auth_or_captcha_wall(resp.text):
            raise BlockedSourceError(f"CAPTCHA or login gate detected on {self.careers_url}; refusing bypass")

        soup = BeautifulSoup(resp.text, "lxml")
        jobs: list[NormalizedJob] = []

        # 1. Extract schema.org/JobPosting JSON-LD blocks (preferred structured data)
        for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
            raw_text = script.string or script.get_text()
            if not raw_text:
                continue
            try:
                data = json.loads(raw_text)
            except json.JSONDecodeError:
                continue
            items = data if isinstance(data, list) else [data]
            for item in items:
                if not isinstance(item, dict):
                    continue
                if item.get("@type") == "JobPosting":
                    norm = self.normalize(item)
                    if query and query.lower() not in f"{norm.job_title} {norm.description}".lower():
                        continue
                    jobs.append(norm)

        return jobs

    async def fetch_job(self, external_job_id: str) -> NormalizedJob | None:
        all_jobs = await self.search()
        for job in all_jobs:
            if job.external_job_id == external_job_id:
                return job
        return None

    def normalize(self, raw_job: dict[str, Any]) -> NormalizedJob:
        title = str(raw_job.get("title") or "").strip()
        raw_desc = str(raw_job.get("description") or "")
        description = html_to_clean_text(raw_desc)
        url = str(raw_job.get("url") or self.careers_url)
        if not url.startswith("http"):
            url = urljoin(self.careers_url, url)

        job_loc = raw_job.get("jobLocation") or {}
        location: str | None = None
        if isinstance(job_loc, dict):
            addr = job_loc.get("address") or {}
            if isinstance(addr, dict):
                parts = [addr.get("addressLocality"), addr.get("addressRegion"), addr.get("addressCountry")]
                location = ", ".join(str(p) for p in parts if p) or None

        workplace_type = "remote" if str(raw_job.get("jobLocationType") or "").upper() == "TELECOMMUTE" else None
        commitment = str(raw_job.get("employmentType") or "")
        signals = extract_deterministic_job_signals(
            title=title,
            description=description,
            location=location,
            workplace_type=workplace_type,
            commitment=commitment,
        )

        ext_id = str(raw_job.get("identifier", {}).get("value") if isinstance(raw_job.get("identifier"), dict) else "")
        if not ext_id:
            ext_id = hashlib.sha256(f"{self.company_name}|{title}|{url}".encode("utf-8")).hexdigest()[:16]

        valid_through = _parse_dt(raw_job.get("validThrough"))
        status = "active"
        if valid_through and valid_through < datetime.now(timezone.utc):
            status = "expired"

        return NormalizedJob(
            source=self.source_name,
            source_job_id=ext_id,
            external_job_id=ext_id,
            canonical_url=url,
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
            posted_at=_parse_dt(raw_job.get("datePosted")),
            discovered_at=datetime.now(timezone.utc),
            application_url=url,
            status=status,
            raw_metadata={
                "careers_url": self.careers_url,
                "prompt_injection_flags": signals["prompt_injection_flags"],
                "auto_submit_supported": False,
            },
        )

    async def check_status(self, external_job_id: str) -> str:
        try:
            job = await self.fetch_job(external_job_id)
        except BlockedSourceError:
            return "unavailable"
        except ConnectorError:
            return "unavailable"
        if job is None:
            return "expired"
        return job.status
