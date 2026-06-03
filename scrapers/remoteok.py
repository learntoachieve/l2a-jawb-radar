from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx

from scoring.eligibility_utils import (
    detect_citizenship_required,
    detect_license_required,
)
from scoring.ghost_utils import compute_ghost_score
from scoring.language_utils import detect_language
from scoring.location_utils import classify_geo_tier, normalize_location

from .base import BaseScraper

REMOTEOK_URL = "https://remoteok.com/api"
USER_AGENT = (
    "Mozilla/5.0 (compatible; job-radar/0.1; "
    "+https://github.com/) httpx"
)


class RemoteOKScraper(BaseScraper):
    """Scraper for the RemoteOK public JSON API.

    The API returns a list whose index 0 is metadata; jobs follow at index 1+.
    RemoteOK 403s default Python user-agents, so we set a browser-like UA.
    """

    source_name = "RemoteOK"

    def __init__(self, client: httpx.Client | None = None):
        self._client = client

    def fetch(self) -> list[dict[str, Any]]:
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        if self._client is not None:
            response = self._client.get(REMOTEOK_URL, headers=headers, timeout=30)
        else:
            response = httpx.get(REMOTEOK_URL, headers=headers, timeout=30)
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, list) or not data:
            return []
        return [row for row in data[1:] if isinstance(row, dict)]

    def normalize(self, raw: dict[str, Any]) -> dict[str, Any] | None:
        if not raw.get("id") or not raw.get("position"):
            return None

        posted_at: datetime | None = None
        epoch = raw.get("epoch")
        if epoch is not None:
            try:
                posted_at = datetime.fromtimestamp(int(epoch), tz=timezone.utc).replace(
                    tzinfo=None
                )
            except (TypeError, ValueError, OSError):
                posted_at = None

        body = raw.get("description") or ""
        title = raw["position"]
        salary_min = raw.get("salary_min")
        salary_max = raw.get("salary_max")
        metadata = {
            "company": raw.get("company"),
            "location": raw.get("location"),
            "salary_min": salary_min,
            "salary_max": salary_max,
            "tags": raw.get("tags") or [],
            "logo": raw.get("logo") or raw.get("company_logo"),
            "remote_type": "remote",
            "location_normalized": normalize_location(raw.get("location"), body),
            "geo_tier": classify_geo_tier(raw.get("location"), body),
            "language_detected": detect_language(body),
            "citizenship_required": detect_citizenship_required(body),
            "license_required": detect_license_required(body),
            "ghost_score": compute_ghost_score(
                {
                    "title": title,
                    "body": body,
                    "company": raw.get("company"),
                    "posted_at": posted_at,
                    "salary_min": salary_min,
                    "salary_max": salary_max,
                }
            ),
        }

        return {
            "external_id": str(raw["id"]),
            "title": title,
            "body": body,
            "url": raw.get("url") or f"https://remoteok.com/remote-jobs/{raw['id']}",
            "metadata_json": metadata,
            "posted_at": posted_at,
        }
