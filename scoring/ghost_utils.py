"""Heuristic 0-100 score flagging suspect / "ghost" job postings.

Hard signals (+30 each), medium (+15), weak (+5). Final score is
capped at 100. Conservative on false positives — ambiguous postings
land in the 50-79 "soft warn" band rather than the 80+ "hide".
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from .text_utils import clean_html, normalize_unicode, term_pattern

GHOST_HARD_THRESHOLD = 80
GHOST_WARN_THRESHOLD = 50

_HARD_PHRASES = [
    "telegram", "whatsapp", "signal me",
]
_GMAIL_PATTERN = re.compile(r"\b[a-z0-9._%+-]+@gmail\.com\b", re.IGNORECASE)
_TME_PATTERN = re.compile(r"\bt\.me/", re.IGNORECASE)

_ALWAYS_HIRING_PHRASES = [
    "always hiring",
    "we're always recruiting",
    "we are always recruiting",
    "ongoing recruitment",
    "rolling recruitment",
]

_RED_FLAG_TITLE_PHRASES = [
    "earn from home",
    "make money from home",
    "make money fast",
    "easy income",
    "easy money",
    "passive income job",
    "work from home opportunity",
]

_NO_EXP_PHRASES = ["no experience required", "no experience needed", "no prior experience"]
_REMOTE_PHRASES = ["remote", "work from home", "wfh"]
_HIGH_PAY_PHRASES = [
    "high pay", "high paying", "huge salary", "earn up to", "make up to",
]

_SENTENCE_END = re.compile(r"[.!?]+")
_URL_PATTERN = re.compile(r"https?://[^\s\)\]\}]+", re.IGNORECASE)

_EXEC_TITLE_PHRASES = [
    "ceo", "cto", "cfo", "coo", "vp ", "vice president",
    "director", "head of", "principal", "staff", "chief",
    "partner", "managing director",
]


def _has_any(text: str, patterns: list[str]) -> bool:
    for p in patterns:
        if term_pattern(p).search(text):
            return True
    return False


def _now_utc_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _days_old(posted_at: datetime | None) -> int | None:
    if posted_at is None:
        return None
    delta = _now_utc_naive() - posted_at
    return delta.days


def _is_executive_title(title: str | None) -> bool:
    if not title:
        return False
    lower = title.lower()
    for p in _EXEC_TITLE_PHRASES:
        if p in lower:
            return True
    return False


def _looks_like_telegram_or_personal_contact(text: str) -> bool:
    if _has_any(text, _HARD_PHRASES):
        return True
    if _TME_PATTERN.search(text):
        return True
    if _GMAIL_PATTERN.search(text):
        return True
    return False


def compute_ghost_score(item: dict[str, Any]) -> int:
    """Return a 0-100 ghost-likelihood score for an item dict.

    Expected keys (all optional, missing keys treated leniently):
      title, body, company, posted_at, salary_min, salary_max
    """
    score = 0

    body_raw = item.get("body") or ""
    title = item.get("title") or ""
    posted_at = item.get("posted_at")
    salary_min = item.get("salary_min")
    salary_max = item.get("salary_max")

    # Clean+normalize body once for text-based signals
    body_clean = normalize_unicode(clean_html(body_raw)) if body_raw else ""
    body_lower = body_clean.lower()
    title_lower = title.lower()

    # ----- Hard signals (+30 each) -----

    days_old = _days_old(posted_at)
    if days_old is not None and days_old > 60:
        score += 30

    if _looks_like_telegram_or_personal_contact(body_clean):
        score += 30

    if len(body_clean) < 200:
        score += 30

    # ----- Medium signals (+15 each) -----

    if salary_min and salary_max:
        try:
            sm = float(salary_min)
            sx = float(salary_max)
            if sm > 0 and sx / sm > 5:
                score += 15
        except (TypeError, ValueError):
            pass

    if salary_max:
        try:
            if float(salary_max) > 500_000 and not _is_executive_title(title):
                score += 15
        except (TypeError, ValueError):
            pass

    if any(p in body_lower for p in _ALWAYS_HIRING_PHRASES):
        score += 15

    has_no_exp = any(p in body_lower for p in _NO_EXP_PHRASES)
    has_remote = any(p in body_lower for p in _REMOTE_PHRASES)
    has_high_pay = any(p in body_lower for p in _HIGH_PAY_PHRASES)
    if has_no_exp and has_remote and has_high_pay:
        score += 15

    if any(p in title_lower for p in _RED_FLAG_TITLE_PHRASES):
        score += 15

    # ----- Weak signals (+5 each) -----

    if not _URL_PATTERN.search(body_raw) and not _URL_PATTERN.search(body_clean):
        score += 5

    if body_clean and len(_SENTENCE_END.findall(body_clean)) < 5:
        score += 5

    return min(100, score)


# Test fixtures.
EXAMPLE_LEGIT_JOB: dict[str, Any] = {
    "title": "Data Analyst",
    "company": "Acme Corp",
    "body": (
        "We are looking for a data analyst to join our team. "
        "Responsibilities include building reports, analyzing customer "
        "data, and presenting findings to stakeholders. You will work "
        "with our SQL warehouse and Tableau dashboards. Required: 2+ "
        "years of experience with SQL, Python, and data visualization. "
        "Visit our careers page at https://acme.com/careers to learn more."
    ),
    "posted_at": _now_utc_naive(),
    "salary_min": 70000,
    "salary_max": 90000,
}

EXAMPLE_GHOST_JOB: dict[str, Any] = {
    "title": "Earn From Home Easy Income",
    "company": "Quick Cash Co",
    "body": (
        "We're always hiring remote workers. No experience required. "
        "High pay. Contact via Telegram @ghostjob123 to apply."
    ),
    "posted_at": _now_utc_naive().replace(year=_now_utc_naive().year - 1),
    "salary_min": 50000,
    "salary_max": 350000,
}
