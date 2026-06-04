"""
BaseScraper — abstract base for all source scrapers.

Subclasses implement:
  fetch()     -> list[dict]   (raw items from source)
  normalize() -> dict | None  (raw -> normalized schema)

run() orchestrates: fetch -> normalize -> dedup -> insert -> Pass 2 JD fetch.

Pass 2 (``fetch_full_jd``) re-fetches each newly inserted item's URL
and replaces ``item.body`` with the cleaned visible text from the
landing page so the scoring engine matches against full requirements
instead of API summaries. It rate-limits to 1 req/sec per domain via
a module-level table and silently no-ops on any HTTP/parse failure.
"""
from __future__ import annotations

import hashlib
import re
import time
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup
from sqlalchemy import func, select

from db.database import get_session
from db.models import Item, Source


# ---------------------------------------------------------------------------
# Pass 2 configuration
# ---------------------------------------------------------------------------

# Browser-like UA so origins that 403 on httpx's default identifier
# (e.g. himalayas.app) still serve the rendered JD page. Combined with
# the Accept / Accept-Language headers below, this passes the bot
# checks at every JD origin we currently scrape.
PASS2_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
PASS2_HEADERS = {
    "User-Agent": PASS2_USER_AGENT,
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.5",
}
PASS2_TIMEOUT_SEC = 10.0
PASS2_MAX_BODY_CHARS = 8000
PASS2_RATE_LIMIT_SEC = 1.0

# Per-domain last fetch time (process-global so all scrapers share the budget).
_LAST_FETCH_AT: dict[str, float] = {}


def _normalized_content_hash(title: str, company: str | None, body: str) -> str:
    """
    Content hash resilient to HTML/whitespace noise.
    Strips HTML, unicode-normalizes, lowercases, collapses whitespace,
    truncates body to first 500 chars to ignore trailing boilerplate.
    Cross-source dedup: same job posted to Greenhouse + Lever hashes identically.
    """
    from scoring.text_utils import clean_html, normalize_unicode
    cleaned_body = normalize_unicode(clean_html(body or ""))
    body_part = cleaned_body[:500]
    title_part = normalize_unicode(title or "")
    company_part = normalize_unicode(company or "")
    combined = f"{title_part}\n{company_part}\n{body_part}"
    normalized = re.sub(r"\s+", " ", combined.lower()).strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _now_utc_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _extract_domain(url: str) -> str:
    """Return ``netloc`` (host[:port]) lowercased, empty on parse error."""
    try:
        return (urlparse(url).netloc or "").lower()
    except Exception:
        return ""


def _throttle(domain: str) -> None:
    """Block until at least PASS2_RATE_LIMIT_SEC has passed for ``domain``."""
    if not domain:
        return
    now = time.monotonic()
    last = _LAST_FETCH_AT.get(domain, 0.0)
    delta = now - last
    if 0 <= delta < PASS2_RATE_LIMIT_SEC:
        time.sleep(PASS2_RATE_LIMIT_SEC - delta)
    _LAST_FETCH_AT[domain] = time.monotonic()


def _visible_text_from_html(html_text: str) -> str:
    soup = BeautifulSoup(html_text, "html.parser")
    for tag in soup(["script", "style", "noscript", "iframe", "svg"]):
        tag.decompose()
    raw = soup.get_text(separator="\n")
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    return "\n".join(lines)


class BaseScraper(ABC):
    source_name: str = ""

    @abstractmethod
    def fetch(self) -> list[dict[str, Any]]:
        ...

    @abstractmethod
    def normalize(self, raw: dict[str, Any]) -> dict[str, Any] | None:
        ...

    # -----------------------------------------------------------------
    # Pass 2: full-JD fetch
    # -----------------------------------------------------------------

    def fetch_full_jd(self, url: str) -> str:
        """Fetch ``url`` and return cleaned visible text (max 8000 chars).

        Sends a browser-like ``Mozilla/5.0 ... Chrome/124`` User-Agent
        + Accept / Accept-Language headers (see ``PASS2_HEADERS``) so
        origins that 403 on httpx's default identifier (notably
        himalayas.app) still serve the rendered JD page.

        Never raises. Empty string means the caller should keep the
        Pass-1 body as-is. Rate-limited to 1 req/sec per host across
        all scrapers in this process.
        """
        if not url:
            return ""
        domain = _extract_domain(url)
        _throttle(domain)
        try:
            with httpx.Client(
                timeout=PASS2_TIMEOUT_SEC,
                follow_redirects=True,
                headers=PASS2_HEADERS,
            ) as client:
                response = client.get(url)
            if response.status_code >= 400:
                return ""
            text = _visible_text_from_html(response.text)
            return text[:PASS2_MAX_BODY_CHARS]
        except Exception:
            return ""

    def _get_source(self, session) -> Source:
        # init_db seeds source rows lowercased ("greenhouse"); scraper
        # classes carry capitalized source_name ("Greenhouse"). Match
        # case-insensitively so both conventions resolve.
        source = session.execute(
            select(Source).where(
                func.lower(Source.name) == self.source_name.lower()
            )
        ).scalar_one_or_none()
        if source is None:
            raise RuntimeError(
                f"Source {self.source_name!r} not seeded. Run scripts/init_db.py first."
            )
        return source

    def run(self) -> dict[str, int]:
        summary = {
            "fetched": 0, "new": 0, "duplicates": 0, "errors": 0,
            "pass2_fetched": 0, "pass2_empty": 0,
        }

        try:
            raw_items = self.fetch()
        except Exception as exc:
            summary["errors"] = 1
            print(f"[{self.source_name}] fetch error: {exc}")
            return summary

        summary["fetched"] = len(raw_items)

        with get_session() as session:
            source = self._get_source(session)

            for raw in raw_items:
                try:
                    norm = self.normalize(raw)
                    if norm is None:
                        continue

                    company = (norm.get("metadata_json") or {}).get("company")
                    h = _normalized_content_hash(
                        norm["title"], company, norm.get("body", "")
                    )

                    # Primary dedup: same source + external_id
                    existing = session.execute(
                        select(Item).where(
                            Item.source_id == source.id,
                            Item.external_id == str(norm["external_id"]),
                        )
                    ).scalar_one_or_none()
                    if existing is not None:
                        summary["duplicates"] += 1
                        continue

                    # Secondary dedup: identical content across sources
                    existing_by_hash = session.execute(
                        select(Item).where(Item.content_hash == h)
                    ).scalar_one_or_none()
                    if existing_by_hash is not None:
                        summary["duplicates"] += 1
                        continue

                    # SAVEPOINT around the insert + Pass-2 update.
                    # If the unique constraint (source_id, url) or
                    # (source_id, external_id) fires here, only this one
                    # item's changes are rolled back — the outer
                    # transaction (and every earlier successful insert)
                    # stays intact. Bare ``session.rollback()`` here
                    # would discard the whole batch's worth of accumulated
                    # work, which is the failure mode the original report
                    # was describing.
                    with session.begin_nested():
                        item = Item(
                            source_id=source.id,
                            external_id=str(norm["external_id"]),
                            title=norm["title"],
                            body=norm.get("body", ""),
                            url=norm["url"],
                            metadata_json=norm.get("metadata_json"),
                            posted_at=norm.get("posted_at"),
                            content_hash=h,
                        )
                        session.add(item)
                        session.flush()

                        # Pass 2: full JD fetch. On non-empty result,
                        # replace body so scoring runs against the full
                        # text. On empty (HTTP error, timeout, etc.),
                        # keep the Pass-1 body — never store empty.
                        full_body = self.fetch_full_jd(norm["url"])
                        if full_body:
                            item.body = full_body
                            summary["pass2_fetched"] += 1
                            # Recompute the content hash to reflect the
                            # richer body; downstream dedup uses this.
                            item.content_hash = _normalized_content_hash(
                                item.title, company, full_body
                            )
                        else:
                            summary["pass2_empty"] += 1
                            print(
                                f"[{self.source_name}] pass2 empty for "
                                f"{norm['url']!r}, keeping snippet body"
                            )
                        session.flush()
                    summary["new"] += 1

                except Exception as exc:
                    summary["errors"] += 1
                    print(f"[{self.source_name}] normalize/insert error: {exc}")
                    # ``begin_nested()`` rolled back its SAVEPOINT
                    # automatically when the exception unwound the
                    # ``with`` block, so the outer transaction is still
                    # valid and the loop can continue without manual
                    # session.rollback(). If a flush failure leaked out
                    # of the SAVEPOINT (shouldn't happen, but defensive),
                    # we still want the loop to keep going.
                    try:
                        if session.is_active is False:
                            session.rollback()
                    except Exception:
                        pass

            source.last_run_at = _now_utc_naive()
            session.commit()

        return summary
