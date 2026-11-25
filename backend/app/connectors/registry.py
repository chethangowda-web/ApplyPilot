from typing import Any
import httpx
from app.connectors.base import ConnectorError, JobSourceConnector, assert_permitted_url
from app.connectors.career_sites.connector import CareerSiteConnector
from app.connectors.greenhouse.connector import GreenhouseConnector
from app.connectors.lever.connector import LeverConnector
from app.database.models import JobSource


def build_connector(
    source: JobSource,
    *,
    client: httpx.AsyncClient | None = None,
) -> JobSourceConnector:
    """Instantiate the permitted JobSourceConnector matching a persisted JobSource row."""
    cfg: dict[str, Any] = dict(source.config or {})
    rate_cfg: dict[str, Any] = dict(source.rate_limit_config or {})
    min_interval = float(rate_cfg.get("min_interval_seconds", 0.0) or 0.0)
    timeout = float(rate_cfg.get("timeout_seconds", 15.0) or 15.0)
    max_retries = int(rate_cfg.get("max_retries", 2) or 2)

    if source.connector_type == "greenhouse":
        board_token = str(cfg.get("board_token") or "").strip()
        if not board_token:
            raise ConnectorError(f"Missing board_token in Greenhouse source {source.source_name}")
        kwargs: dict[str, Any] = {
            "company_name": cfg.get("company_name") or source.source_name,
            "min_interval_seconds": min_interval,
            "timeout_seconds": timeout,
            "max_retries": max_retries,
            "client": client,
        }
        if source.base_url:
            assert_permitted_url(source.base_url)
            kwargs["base_url"] = source.base_url
        return GreenhouseConnector(board_token, **kwargs)

    if source.connector_type == "lever":
        slug = str(cfg.get("company_slug") or "").strip()
        if not slug:
            raise ConnectorError(f"Missing company_slug in Lever source {source.source_name}")
        kwargs = {
            "company_name": cfg.get("company_name") or source.source_name,
            "min_interval_seconds": min_interval,
            "timeout_seconds": timeout,
            "max_retries": max_retries,
            "client": client,
        }
        if source.base_url:
            assert_permitted_url(source.base_url)
            kwargs["base_url"] = source.base_url
        return LeverConnector(slug, **kwargs)

    if source.connector_type == "career_site":
        careers_url = str(cfg.get("careers_url") or source.base_url or "").strip()
        if not careers_url:
            raise ConnectorError(f"Missing careers_url in CareerSite source {source.source_name}")
        assert_permitted_url(careers_url)
        return CareerSiteConnector(
            careers_url,
            company_name=str(cfg.get("company_name") or source.source_name),
            respect_robots=bool(cfg.get("respect_robots", True)),
            min_interval_seconds=min_interval,
            timeout_seconds=timeout,
            max_retries=max_retries,
            client=client,
        )

    raise ConnectorError(f"Unsupported connector_type: {source.connector_type}")
