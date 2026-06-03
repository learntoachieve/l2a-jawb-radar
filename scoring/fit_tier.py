"""Fit-tier classifier — derives "high_fit" / "stretch" / "long_shot"
from a job's score and a count of seniority/experience yellow flags.

Pure function; no DB access. Used by the dashboard queue to label
each row beyond the raw numeric score.

Tiers:
  high_fit  — score >= 80 AND zero yellow flags
  stretch   — score 50-79, OR score >= 80 with exactly one yellow flag
  long_shot — score < 50, OR score >= 50 with two or more yellow flags

Yellow flags:
  Title-side seniority terms (senior/staff/principal/lead/director/
    manager/head of/vp/chief) — only counted when the title doesn't
    also carry a junior-override term ("junior", "entry level"). The
    override avoids penalising "Junior to Mid-Senior Analyst" or
    similar where the level is actually entry-friendly.
  Title-side level numerals (II, III, IV) — counted regardless of
    junior override; "Data Analyst II" is mid-level no matter how
    you dress it.
  Body-side experience requirements ("5+ years", "minimum 5 years",
    "10+ years", etc.) — counted regardless of junior override.

Yellow-flag total is capped at 3.
"""
from __future__ import annotations

import re
from typing import Iterable

# Title-side seniority terms. Word-boundary matched (case-insensitive).
_TITLE_SENIORITY_TERMS: tuple[str, ...] = (
    "senior", "staff", "principal", "lead", "director", "manager",
    "head of", "vp", "chief",
)

# Title-side level numerals. Word-boundary matched separately so they
# survive junior-override (mid-level numeric tags are real signals).
_LEVEL_NUMERALS: tuple[str, ...] = ("ii", "iii", "iv")

# Body-side experience-requirement signals. Each pattern is a literal
# regex (not a re.escape'd term) so we can allow whitespace flex —
# e.g. "5+ years" should also match "5 + years" or "5+years".
_EXPERIENCE_PATTERNS: tuple[str, ...] = (
    r"\b5\s*\+\s*years",
    r"\b6\s*\+\s*years",
    r"\b7\s*\+\s*years",
    r"\b8\s*\+\s*years",
    r"\b10\s*\+\s*years",
    r"\b5\s*years\s+experience",
    r"\bminimum\s+5\s+years",
    r"\bminimum\s+of\s+5\s+years",
)

# Junior-override terms — when found in the title, suppress the
# title-seniority count but NOT the numerals or body experience checks.
_JUNIOR_OVERRIDES: tuple[str, ...] = (
    "junior", "jr.", "entry level", "entry-level", "early career",
)


def _word_boundary_pattern(term: str) -> re.Pattern[str]:
    """Negative-lookaround word boundary that handles non-word chars
    (e.g. ``head of`` has a space; ``jr.`` has a period)."""
    return re.compile(
        rf"(?<![A-Za-z0-9_]){re.escape(term)}(?![A-Za-z0-9_])",
        re.IGNORECASE,
    )


def _has_any_word_boundary(text: str, terms: Iterable[str]) -> bool:
    if not text:
        return False
    return any(_word_boundary_pattern(t).search(text) for t in terms)


def _count_word_boundary(text: str, terms: Iterable[str]) -> int:
    if not text:
        return 0
    return sum(1 for t in terms if _word_boundary_pattern(t).search(text))


def _count_regex_matches(text: str, patterns: Iterable[str]) -> int:
    if not text:
        return 0
    return sum(1 for p in patterns if re.search(p, text, re.IGNORECASE))


def count_yellow_flags(title: str | None, body: str | None) -> int:
    """Count seniority/experience signals across title + body.

    Capped at 3 to avoid pathological inflation from boilerplate.
    """
    title = title or ""
    body = body or ""

    junior_in_title = _has_any_word_boundary(title, _JUNIOR_OVERRIDES)

    flags = 0
    if not junior_in_title:
        flags += _count_word_boundary(title, _TITLE_SENIORITY_TERMS)
    flags += _count_word_boundary(title, _LEVEL_NUMERALS)
    flags += _count_regex_matches(body, _EXPERIENCE_PATTERNS)

    return min(flags, 3)


def classify_fit_tier(item: dict) -> str:
    """Bucket an item into "high_fit" / "stretch" / "long_shot".

    ``item`` must carry ``score`` (numeric, 0-100), ``title``, ``body``.
    Missing fields are treated as 0 / "" / "".
    """
    score = float(item.get("score") or 0.0)
    title = item.get("title") or ""
    body = item.get("body") or ""

    flags = count_yellow_flags(title, body)

    if score < 50:
        return "long_shot"
    if flags >= 2:
        return "long_shot"
    if score >= 80 and flags == 0:
        return "high_fit"
    return "stretch"
