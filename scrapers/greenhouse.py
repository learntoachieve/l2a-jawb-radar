from __future__ import annotations

import html
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
from scoring.text_utils import clean_html

from .base import BaseScraper

GREENHOUSE_BASE = "https://boards-api.greenhouse.io/v1/boards"
USER_AGENT = (
    "Mozilla/5.0 (compatible; job-radar/0.1; "
    "+https://github.com/) httpx"
)
DEFAULT_INTER_REQUEST_SLEEP = 0.5
CONFIG_FILENAME = "company_boards.yaml"


def _load_slugs() -> list[str]:
    """Load Greenhouse slug list from config/company_boards.yaml.

    Returns an empty list if the file is missing or the key is absent —
    the scraper then no-ops without raising, matching the spec's
    "graceful" theme for slugs.
    """
    config_path = (
        Path(__file__).resolve().parent.parent / "config" / CONFIG_FILENAME
    )
    if not config_path.exists():
        return []
    with open(config_path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    raw = data.get("greenhouse") or []
    return [str(s).strip() for s in raw if str(s).strip()]


def _parse_iso(value: str | None) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        normalized = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


class GreenhouseScraper(BaseScraper):
    """Scraper for Greenhouse public job boards.

    Iterates a curated slug list (config/company_boards.yaml). Each slug
    hits ``boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true``;
    404s are silently skipped, transient errors are logged. Failed
    boards never break the overall run.
    """

    source_name = "Greenhouse"

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

    def _get(self, url: str) -> dict[str, Any]:
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        if self._client is not None:
            response = self._client.get(url, headers=headers, timeout=30)
        else:
            response = httpx.get(url, headers=headers, timeout=30)
        response.raise_for_status()
        data = response.json()
        return data if isinstance(data, dict) else {}

    def fetch(self) -> list[dict[str, Any]]:
        all_jobs: list[dict[str, Any]] = []
        self._failed_slugs = []
        self._slugs_with_jobs = 0
        self._slugs_attempted = 0

        for slug in self._slugs:
            self._slugs_attempted += 1
            self._current_slug = slug
            url = f"{GREENHOUSE_BASE}/{slug}/jobs?content=true"
            try:
                payload = self._get(url)
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code if exc.response else None
                if status == 404:
                    print(f"[Greenhouse] slug not found: {slug}")
                else:
                    print(f"[Greenhouse] HTTP {status} for {slug}")
                self._failed_slugs.append((slug, f"HTTP {status}"))
                if self._sleep > 0:
                    time.sleep(self._sleep)
                continue
            except Exception as exc:
                print(f"[Greenhouse] fetch error for {slug}: {exc}")
                self._failed_slugs.append((slug, str(exc)))
                if self._sleep > 0:
                    time.sleep(self._sleep)
                continue

            jobs = payload.get("jobs") or []
            if isinstance(jobs, list):
                count_before = len(all_jobs)
                for j in jobs:
                    if isinstance(j, dict):
                        j["__slug__"] = slug
                        all_jobs.append(j)
                if len(all_jobs) > count_before:
                    self._slugs_with_jobs += 1

            if self._sleep > 0:
                time.sleep(self._sleep)

        self._current_slug = None
        return all_jobs

    def normalize(self, raw: dict[str, Any]) -> dict[str, Any] | None:
        job_id = raw.get("id")
        title = raw.get("title")
        if not job_id or not title:
            return None

        slug = raw.get("__slug__") or "unknown"
        # Greenhouse returns the JD as HTML-entity-encoded HTML (e.g. "&lt;p&gt;")
        # — unescape before stripping tags so the body comes out as plain text.
        body_raw = raw.get("content") or ""
        body = clean_html(html.unescape(body_raw)) if body_raw else ""

        location_field = raw.get("location") or {}
        location_str = (
            location_field.get("name")
            if isinstance(location_field, dict)
            else None
        )

        departments_raw = raw.get("departments") or []
        departments: list[str] = []
        for d in departments_raw:
            if isinstance(d, dict) and d.get("name"):
                departments.append(d["name"])

        offices_raw = raw.get("offices") or []
        offices: list[str] = []
        for o in offices_raw:
            if isinstance(o, dict) and o.get("name"):
                offices.append(o["name"])

        posted_at_val = _parse_iso(raw.get("updated_at") or raw.get("first_published"))

        company = slug
        url = raw.get("absolute_url") or ""

        metadata = {
            "company": company,
            "slug": slug,
            "location": location_str,
            "departments": departments,
            "offices": offices,
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
            "external_id": f"gh_{slug}_{job_id}",
            "title": title,
            "body": body,
            "url": url,
            "metadata_json": metadata,
            "posted_at": posted_at_val,
        }
