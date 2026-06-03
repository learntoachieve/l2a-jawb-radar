"""Himalayas remote-jobs aggregator scraper.

Public JSON API at ``https://himalayas.app/jobs/api`` — no key, no
auth, but capped at 20 jobs per request regardless of ``limit`` query
param. Pagination uses ``offset``. The corpus is large (six-figure
totalCount when probed during development), so the scraper takes a
hard cap of ``MAX_REQUESTS`` to keep runtime polite — at 20 jobs/req
× 1s sleep that's a single-digit-minute fetch.

Item dedup is handled by BaseScraper's normalised content_hash, which
catches the common case of Himalayas mirroring a Greenhouse / Lever
/ Ashby posting we already scraped from the source ATS. The cron run
should report ``duplicates`` >> ``new`` for Himalayas once the other
direct-employer scrapers have run, which is the expected pattern.
"""
from __future__ import annotations

import html
import time
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
from scoring.text_utils import clean_html

from .base import BaseScraper

HIMALAYAS_URL = "https://himalayas.app/jobs/api"
USER_AGENT = "Mozilla/5.0 (compatible; job-radar/0.1) httpx"

# Hard cap on pagination. 50 requests × 20 jobs/request = 1000 jobs per
# run. Architect-mandated to keep us polite to a free, unauthenticated
# endpoint that publishes 100K+ jobs.
MAX_REQUESTS = 50
PAGE_SIZE = 20  # Himalayas caps at 20 per request regardless of `limit`.
DEFAULT_SLEEP = 1.0  # polite gap between paginated requests


def _parse_epoch_seconds(value: Any) -> datetime | None:
    """Himalayas returns ``pubDate`` as Unix epoch seconds (sometimes a
    string). Returns ``None`` for missing / malformed values."""
    if value is None:
        return None
    try:
        secs = int(value)
    except (TypeError, ValueError):
        return None
    try:
        return datetime.fromtimestamp(secs, tz=timezone.utc).replace(tzinfo=None)
    except (OverflowError, OSError, ValueError):
        return None


def _build_location_string(raw: dict[str, Any]) -> str | None:
    """Stitch together a display-friendly location string from
    Himalayas's ``locationRestrictions`` (list of country names) and
    fall back to ``"Remote"`` for items with no restriction at all
    — almost every Himalayas posting is remote, so the absence of a
    restriction means worldwide-remote rather than missing-data."""
    restrictions = raw.get("locationRestrictions")
    if isinstance(restrictions, list) and restrictions:
        joined = ", ".join(
            str(r).strip() for r in restrictions if str(r).strip()
        )
        if joined:
            return f"Remote, {joined}"
    return "Remote"


class HimalayasScraper(BaseScraper):
    """Paginate ``himalayas.app/jobs/api`` until empty or cap reached."""

    source_name = "Himalayas"

    def __init__(
        self,
        client: httpx.Client | None = None,
        sleep_between: float = DEFAULT_SLEEP,
        max_requests: int = MAX_REQUESTS,
    ):
        self._client = client
        self._sleep = sleep_between
        self._max_requests = max_requests

    def _get(self, offset: int) -> dict[str, Any]:
        url = f"{HIMALAYAS_URL}?offset={offset}&limit={PAGE_SIZE}"
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        if self._client is not None:
            response = self._client.get(
                url, headers=headers, timeout=30, follow_redirects=True
            )
        else:
            response = httpx.get(
                url, headers=headers, timeout=30, follow_redirects=True
            )
        response.raise_for_status()
        data = response.json()
        return data if isinstance(data, dict) else {}

    def fetch(self) -> list[dict[str, Any]]:
        all_jobs: list[dict[str, Any]] = []
        offset = 0
        for _ in range(self._max_requests):
            try:
                payload = self._get(offset)
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code if exc.response else None
                if status == 429:
                    # Polite back-off, single retry.
                    print("[Himalayas] 429 rate-limited, sleeping 5s and retrying once")
                    time.sleep(5.0)
                    try:
                        payload = self._get(offset)
                    except Exception as inner:
                        print(f"[Himalayas] retry failed at offset={offset}: {inner}")
                        break
                else:
                    print(f"[Himalayas] HTTP {status} at offset={offset}; stopping")
                    break
            except Exception as exc:
                print(f"[Himalayas] fetch error at offset={offset}: {exc}")
                break

            jobs = payload.get("jobs") if isinstance(payload, dict) else None
            if not isinstance(jobs, list) or not jobs:
                break

            all_jobs.extend(j for j in jobs if isinstance(j, dict))

            if len(jobs) < PAGE_SIZE:
                # Server returned a partial page — we hit the end.
                break

            offset += PAGE_SIZE
            if self._sleep > 0:
                time.sleep(self._sleep)

        return all_jobs

    def normalize(self, raw: dict[str, Any]) -> dict[str, Any] | None:
        # Himalayas exposes ``guid`` (a stable URL) as the canonical ID.
        # Fall back to applicationLink, then companySlug+title, so
        # external_id is always populated.
        external_id_raw = (
            raw.get("guid")
            or raw.get("applicationLink")
            or (
                f"{raw.get('companySlug')}_{raw.get('title')}"
                if raw.get("companySlug") and raw.get("title")
                else None
            )
        )
        title = raw.get("title")
        if not external_id_raw or not title:
            return None

        body_raw = raw.get("description") or raw.get("excerpt") or ""
        body = clean_html(html.unescape(body_raw)) if body_raw else ""

        location_str = _build_location_string(raw)

        # Himalayas's `seniority` is a list of strings ("Senior",
        # "Director", "Mid-level", etc.). Pass through to metadata so
        # it can inform downstream filtering, but don't feed it back
        # into the title — the seniority hard-exclusion in Phase 4.8b
        # only inspects the title and that's the right behaviour.
        seniority_raw = raw.get("seniority") or []
        seniority = [
            str(s).strip()
            for s in seniority_raw
            if isinstance(s, str) and s.strip()
        ] if isinstance(seniority_raw, list) else []

        salary_min = raw.get("minSalary")
        salary_max = raw.get("maxSalary")
        currency = raw.get("currency")
        employment_type = raw.get("employmentType")
        company = raw.get("companyName") or (raw.get("companySlug") or "").title()
        url = raw.get("applicationLink") or raw.get("guid") or ""

        posted_at = _parse_epoch_seconds(raw.get("pubDate"))

        metadata = {
            "company": company,
            "company_slug": raw.get("companySlug"),
            "location": location_str,
            "salary_min": salary_min,
            "salary_max": salary_max,
            "salary_currency": currency,
            "employment_type": employment_type,
            "seniority": seniority,
            "categories": raw.get("categories") or [],
            "search_term": "himalayas_aggregator",
            "remote_type": "remote",
            "is_remote": True,
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
                    "posted_at": posted_at,
                    "salary_min": salary_min,
                    "salary_max": salary_max,
                }
            ),
        }

        return {
            "external_id": f"himalayas_{external_id_raw}",
            "title": title,
            "body": body,
            "url": url,
            "metadata_json": metadata,
            "posted_at": posted_at,
        }
