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

ASHBY_BASE = "https://api.ashbyhq.com/posting-api/job-board"
USER_AGENT = (
    "Mozilla/5.0 (compatible; job-radar/0.1; "
    "+https://github.com/) httpx"
)
DEFAULT_INTER_REQUEST_SLEEP = 0.5
CONFIG_FILENAME = "company_boards.yaml"

# Pretty display names for known Ashby slugs. Slugs not listed fall back
# to ``slug.title()`` so an unknown new entry doesn't render as "newco"
# in the queue.
ASHBY_DISPLAY_NAMES: dict[str, str] = {
    "anthropic": "Anthropic",
    "openai": "OpenAI",
    "perplexity": "Perplexity",
    "characterai": "Character.AI",
    "cohere": "Cohere",
    "mistral": "Mistral AI",
    "runwayml": "Runway",
    "replicate": "Replicate",
    "huggingface": "Hugging Face",
    "weights-and-biases": "Weights & Biases",
    "modal": "Modal",
    "braintrust": "Braintrust",
    "harvey": "Harvey",
    "sierra": "Sierra",
    "hebbia": "Hebbia",
    "glean": "Glean",
    "decagon": "Decagon",
    "linear": "Linear",
    "posthog": "PostHog",
    "browserbase": "Browserbase",
    "anysphere": "Cursor",
    "vercel": "Vercel",
    "supabase": "Supabase",
    "prisma": "Prisma",
    "replit": "Replit",
    "suno": "Suno",
    "notion": "Notion",
    "mercor": "Mercor",
    "clay": "Clay",
    "rampnetwork": "Ramp",
    "pika": "Pika",
    "11x": "11x",
    "daloopa": "Daloopa",
    "finegrain": "Finegrain",
    "articul8": "Articul8",
    # Phase 4.8c additions
    "weaviate": "Weaviate",
    "pinecone": "Pinecone",
    "langchain": "LangChain",
    "llamaindex": "LlamaIndex",
    "baseten": "Baseten",
    "neon": "Neon",
    "vellum": "Vellum",
    "anyscale": "Anyscale",
    "contextual": "Contextual AI",
    "prefect": "Prefect",
}


def _load_slugs() -> list[str]:
    """Load Ashby slug list from config/company_boards.yaml."""
    config_path = (
        Path(__file__).resolve().parent.parent / "config" / CONFIG_FILENAME
    )
    if not config_path.exists():
        return []
    with open(config_path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    raw = data.get("ashby") or []
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


def _display_name(slug: str) -> str:
    return ASHBY_DISPLAY_NAMES.get(slug, slug.title())


class AshbyScraper(BaseScraper):
    """Scraper for Ashby public job boards.

    Iterates a curated slug list (config/company_boards.yaml). Each slug
    hits ``api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true``.
    404 / network / JSON errors are logged per-slug and skipped — one
    bad slug never breaks the overall run.
    """

    source_name = "Ashby"

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
            url = f"{ASHBY_BASE}/{slug}?includeCompensation=true"
            try:
                payload = self._get(url)
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code if exc.response else None
                if status == 404:
                    print(f"[Ashby] slug not found: {slug}")
                else:
                    print(f"[Ashby] HTTP {status} for {slug}")
                self._failed_slugs.append((slug, f"HTTP {status}"))
                if self._sleep > 0:
                    time.sleep(self._sleep)
                continue
            except Exception as exc:
                print(f"[Ashby] fetch error for {slug}: {exc}")
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

        # Ashby's descriptionHtml is HTML-entity-encoded similar to
        # Greenhouse — unescape first, then strip tags via clean_html
        # so the body lands as plain text. Some postings expose
        # descriptionPlain too; prefer that when present.
        body_plain = raw.get("descriptionPlain") or ""
        if body_plain:
            body = body_plain
        else:
            body_raw = raw.get("descriptionHtml") or raw.get("description") or ""
            body = clean_html(html.unescape(body_raw)) if body_raw else ""

        # location can be a string or absent. Some Ashby payloads include
        # an ``address`` field with richer location info; prefer the
        # longer of the two so we don't miss "San Francisco, CA" vs "USA".
        location_str_a = raw.get("location")
        location_str_b = raw.get("address")
        if isinstance(location_str_a, str) and isinstance(location_str_b, str):
            location_str = (
                location_str_a if len(location_str_a) >= len(location_str_b)
                else location_str_b
            )
        else:
            location_str = (
                location_str_a if isinstance(location_str_a, str) else None
            ) or (
                location_str_b if isinstance(location_str_b, str) else None
            )

        team = raw.get("team") if isinstance(raw.get("team"), str) else None
        employment_type = (
            raw.get("employmentType")
            if isinstance(raw.get("employmentType"), str)
            else None
        )

        comp = raw.get("compensation") or {}
        salary_min = None
        salary_max = None
        if isinstance(comp, dict):
            salary_min = comp.get("compensationTierSummary") or comp.get("min")
            salary_max = comp.get("max")
            # Ashby's compensation block is often a free-form string;
            # treat unknown shapes as None rather than copy-pasting
            # objects into metadata.
            if not isinstance(salary_min, (int, float, str)):
                salary_min = None
            if not isinstance(salary_max, (int, float, str)):
                salary_max = None

        posted_at_val = _parse_iso(
            raw.get("publishedAt") or raw.get("updatedAt")
        )

        company = _display_name(slug)
        url = raw.get("jobUrl") or raw.get("applyUrl") or ""

        metadata = {
            "company": company,
            "slug": slug,
            "location": location_str,
            "team": team,
            "employment_type": employment_type,
            "salary_min": salary_min,
            "salary_max": salary_max,
            "search_term": "direct_board",
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
            "external_id": f"ashby_{slug}_{job_id}",
            "title": title,
            "body": body,
            "url": url,
            "metadata_json": metadata,
            "posted_at": posted_at_val,
        }
