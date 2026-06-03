from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import yaml

from scoring.eligibility_utils import (
    detect_citizenship_required,
    detect_license_required,
)
from scoring.ghost_utils import compute_ghost_score
from scoring.language_utils import detect_language
from scoring.location_utils import classify_geo_tier, normalize_location

from .base import BaseScraper

LEVER_BASE = "https://api.lever.co/v0/postings"
USER_AGENT = (
    "Mozilla/5.0 (compatible; job-radar/0.1; "
    "+https://github.com/) httpx"
)
DEFAULT_INTER_REQUEST_SLEEP = 0.5
CONFIG_FILENAME = "company_boards.yaml"


def _load_slugs() -> list[str]:
    """Load Lever slug list from config/company_boards.yaml."""
    config_path = (
        Path(__file__).resolve().parent.parent / "config" / CONFIG_FILENAME
    )
    if not config_path.exists():
        return []
    with open(config_path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    raw = data.get("lever") or []
    return [str(s).strip() for s in raw if str(s).strip()]


def _ms_epoch_to_naive_utc(value: Any) -> datetime | None:
    if value is None:
        return None
    try:
        ms = int(value)
    except (TypeError, ValueError):
        return None
    try:
        return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).replace(
            tzinfo=None
        )
    except (OverflowError, OSError, ValueError):
        return None


class LeverScraper(BaseScraper):
    """Scraper for Lever public job boards.

    Iterates a curated slug list (config/company_boards.yaml). Each slug
    hits ``api.lever.co/v0/postings/{slug}?mode=json``; 404 = silently
    skip, other errors logged. Per-slug failures never bubble up.
    """

    source_name = "Lever"

    def __init__(
        self,
        client: httpx.Client | None = None,
        slugs: list[str] | None = None,
        sleep_between: float = DEFAULT_INTER_REQUEST_SLEEP,
    ):
        self._client = client
        self._slugs = slugs if slugs is not None else _load_slugs()
        self._sleep = sleep_between
        self._failed_slugs: list[tuple[str, str]] = []
        self._slugs_with_jobs = 0
        self._slugs_attempted = 0
        self._current_slug: str | None = None

    def _get(self, url: str) -> list[dict[str, Any]]:
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        if self._client is not None:
            response = self._client.get(url, headers=headers, timeout=30)
        else:
            response = httpx.get(url, headers=headers, timeout=30)
        response.raise_for_status()
        data = response.json()
        return data if isinstance(data, list) else []

    def fetch(self) -> list[dict[str, Any]]:
        all_jobs: list[dict[str, Any]] = []
        self._failed_slugs = []
        self._slugs_with_jobs = 0
        self._slugs_attempted = 0

        for slug in self._slugs:
            self._slugs_attempted += 1
            self._current_slug = slug
            url = f"{LEVER_BASE}/{slug}?mode=json"
            try:
                postings = self._get(url)
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code if exc.response else None
                if status == 404:
                    print(f"[Lever] slug not found: {slug}")
                else:
                    print(f"[Lever] HTTP {status} for {slug}")
                self._failed_slugs.append((slug, f"HTTP {status}"))
                if self._sleep > 0:
                    time.sleep(self._sleep)
                continue
            except Exception as exc:
                print(f"[Lever] fetch error for {slug}: {exc}")
                self._failed_slugs.append((slug, str(exc)))
                if self._sleep > 0:
                    time.sleep(self._sleep)
                continue

            count_before = len(all_jobs)
            for p in postings:
                if isinstance(p, dict):
                    p["__slug__"] = slug
                    all_jobs.append(p)
            if len(all_jobs) > count_before:
                self._slugs_with_jobs += 1

            if self._sleep > 0:
                time.sleep(self._sleep)

        self._current_slug = None
        return all_jobs

    def normalize(self, raw: dict[str, Any]) -> dict[str, Any] | None:
        posting_id = raw.get("id")
        title = raw.get("text")
        if not posting_id or not title:
            return None

        slug = raw.get("__slug__") or "unknown"
        body = raw.get("descriptionPlain") or raw.get("description") or ""

        categories = raw.get("categories") or {}
        location_str = (
            categories.get("location") if isinstance(categories, dict) else None
        )
        team = categories.get("team") if isinstance(categories, dict) else None
        commitment = (
            categories.get("commitment") if isinstance(categories, dict) else None
        )

        posted_at_val = _ms_epoch_to_naive_utc(raw.get("createdAt"))
        company = slug
        url = raw.get("hostedUrl") or raw.get("applyUrl") or ""

        metadata = {
            "company": company,
            "slug": slug,
            "location": location_str,
            "team": team,
            "commitment": commitment,
            "remote_type": "varied",
            "location_normalized": normalize_location(location_str, body),
            "geo_tier": classify_geo_tier(location_str, body),
            "language_detected": detect_language(body),
            "citizenship_required": detect_citizenship_required(body),
            "license_required": detect_license_required(body),
            "ghost_score": compute_ghost_score(
                {
                    "title": title,
                    "body": body,
                    "company": company,
                    "posted_at": posted_at_val,
                }
            ),
        }

        return {
            "external_id": f"lever_{slug}_{posting_id}",
            "title": title,
            "body": body,
            "url": url,
            "metadata_json": metadata,
            "posted_at": posted_at_val,
        }
