from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from typing import Any

import httpx
from dotenv import load_dotenv

from scoring.eligibility_utils import (
    detect_citizenship_required,
    detect_license_required,
)
from scoring.ghost_utils import compute_ghost_score
from scoring.language_utils import detect_language
from scoring.location_utils import classify_geo_tier, normalize_location

from .base import BaseScraper

load_dotenv()

ADZUNA_BASE = "https://api.adzuna.com/v1/api/jobs/us/search"
USER_AGENT = (
    "Mozilla/5.0 (compatible; job-radar/0.1; +https://github.com/) httpx"
)
DEFAULT_PAGES_PER_TERM = 5
DEFAULT_RESULTS_PER_PAGE = 50
DEFAULT_INTER_REQUEST_SLEEP = 0.5

SEARCH_TERMS: list[str] = [
    "data analyst",
    "business analyst",
    "junior data analyst",
    "data analyst entry level",
    "qa engineer",
    "qa analyst",
    "junior software engineer",
    "software engineer entry level",
    "it support analyst",
    "help desk analyst",
]


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


class AdzunaScraper(BaseScraper):
    """Scraper for the Adzuna US public API.

    Fetches across SEARCH_TERMS to cover the entry-level analyst /
    QA / IT-support funnel. Up to ``pages_per_term`` pages of 50 each
    per term; stops early on a short page. ``time.sleep(0.5)`` between
    requests is the rate-limit-friendly default.
    """

    source_name = "Adzuna"

    def __init__(
        self,
        client: httpx.Client | None = None,
        pages_per_term: int = DEFAULT_PAGES_PER_TERM,
        results_per_page: int = DEFAULT_RESULTS_PER_PAGE,
        sleep_between: float = DEFAULT_INTER_REQUEST_SLEEP,
        search_terms: list[str] | None = None,
    ):
        self._client = client
        self._pages_per_term = pages_per_term
        self._results_per_page = results_per_page
        self._sleep = sleep_between
        self._search_terms = search_terms if search_terms is not None else SEARCH_TERMS
        self._current_term: str | None = None

        self._app_id = os.getenv("ADZUNA_APP_ID")
        self._app_key = os.getenv("ADZUNA_APP_KEY")

    def _check_credentials(self) -> None:
        if not self._app_id or not self._app_key:
            raise RuntimeError(
                "Adzuna credentials missing. Set ADZUNA_APP_ID and "
                "ADZUNA_APP_KEY in .env (see .env.example)."
            )

    def _get_page(self, page: int, what: str) -> dict[str, Any]:
        url = f"{ADZUNA_BASE}/{page}"
        params = {
            "app_id": self._app_id,
            "app_key": self._app_key,
            "results_per_page": self._results_per_page,
            "what": what,
        }
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        if self._client is not None:
            response = self._client.get(
                url, headers=headers, params=params, timeout=30
            )
        else:
            response = httpx.get(
                url, headers=headers, params=params, timeout=30
            )
        response.raise_for_status()
        data = response.json()
        return data if isinstance(data, dict) else {}

    def fetch(self) -> list[dict[str, Any]]:
        self._check_credentials()
        all_results: list[dict[str, Any]] = []

        for term in self._search_terms:
            self._current_term = term
            for page in range(1, self._pages_per_term + 1):
                try:
                    payload = self._get_page(page, term)
                except Exception as exc:
                    print(f"[Adzuna] page {page} for {term!r}: {exc}")
                    break

                results = payload.get("results") or []
                if not isinstance(results, list):
                    break

                for r in results:
                    if isinstance(r, dict):
                        # Tag each result with the search_term that surfaced it
                        # so downstream analytics can attribute matches.
                        r["__search_term__"] = term
                        all_results.append(r)

                if len(results) < self._results_per_page:
                    break

                if self._sleep > 0:
                    time.sleep(self._sleep)

            self._current_term = None

        return all_results

    def normalize(self, raw: dict[str, Any]) -> dict[str, Any] | None:
        if not raw.get("id") or not raw.get("title"):
            return None

        body = raw.get("description") or ""
        title = raw.get("title") or ""

        company_field = raw.get("company") or {}
        company = (
            company_field.get("display_name")
            if isinstance(company_field, dict)
            else None
        )

        location_field = raw.get("location") or {}
        location_str = (
            location_field.get("display_name")
            if isinstance(location_field, dict)
            else None
        )

        category_field = raw.get("category") or {}
        category = (
            category_field.get("label")
            if isinstance(category_field, dict)
            else None
        )

        salary_min = raw.get("salary_min")
        salary_max = raw.get("salary_max")
        posted_at_val = _parse_iso(raw.get("created"))
        metadata = {
            "company": company,
            "location": location_str,
            "salary_min": salary_min,
            "salary_max": salary_max,
            "category": category,
            "contract_type": raw.get("contract_type"),
            "contract_time": raw.get("contract_time"),
            "search_term": raw.get("__search_term__"),
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
                    "salary_min": salary_min,
                    "salary_max": salary_max,
                }
            ),
        }

        return {
            "external_id": f"adzuna_{raw['id']}",
            "title": title,
            "body": body,
            "url": raw.get("redirect_url") or "",
            "metadata_json": metadata,
            "posted_at": posted_at_val,
        }
