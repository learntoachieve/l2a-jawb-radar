"""Detect citizenship-required and license-required gating language.

Both detectors return False on empty input. Both look for *restrictive*
phrasing only — inclusive language ("US citizens, GCs, and visa holders
welcome") is not a hit.

Phase 4.6b adds an inclusive-override list to detect_citizenship_required
so that "Must be US citizen or permanent resident" / "authorized to
work in the US" phrasing returns False even when a restrictive-pattern
substring is also present. Permanent residents and US-authorized non-
citizens fit the user's eligibility, so these JDs shouldn't get filtered
out as citizenship-required.

Phase 4.8b adds detect_seniority — a title-only check that returns
"junior", "senior", or "mid". Used by land_score to hard-exclude items
whose title carries explicit senior markers. Body mentions of "senior"
don't trigger; only the title is inspected.
"""
from __future__ import annotations

import re

from .text_utils import term_pattern

# Citizenship / sponsorship gating language. Each entry indicates a
# *restriction* on who can take the role, not an inclusive description
# of who's welcome to apply. Clearance language is split out below
# because clearance-required roles can never be overridden.
CITIZENSHIP_PATTERNS: list[str] = [
    "must be us citizen",
    "must be a us citizen",
    "must be u.s. citizen",
    "us citizen only",
    "u.s. citizen only",
    "us citizens only",
    "us citizenship required",
    "u.s. citizenship required",
    "no sponsorship",
    "unable to sponsor",
    "cannot sponsor",
    "we do not sponsor",
    "we cannot sponsor",
    "we are unable to sponsor",
]

# Clearance language. Hard-restrictive: a TS/SCI or polygraph role is
# inaccessible regardless of any inclusive wording elsewhere in the
# JD, so detect_citizenship_required short-circuits when one of these
# matches before the inclusive-override check runs.
CLEARANCE_PATTERNS: list[str] = [
    "active security clearance",
    "active clearance",
    "secret clearance",
    "top secret",
    "ts/sci",
    "ts sci",
    "public trust clearance",
    "doe q clearance",
    "polygraph",
    "ability to obtain clearance",
    "must be able to obtain clearance",
    "clearance required",
    "clearance is required",
]

# Inclusive phrasing — when present, the JD welcomes permanent residents
# and US-authorized applicants, so the restrictive patterns above
# should NOT mark the role as citizenship-required.
CITIZENSHIP_INCLUSIVE_PATTERNS: list[str] = [
    "us citizen or permanent resident",
    "u.s. citizen or permanent resident",
    "us citizens or permanent residents",
    "u.s. citizens or permanent residents",
    "us citizens, green card holders",
    "us citizens, gcs",
    "us citizens and lawful permanent residents",
    "u.s. citizens and lawful permanent residents",
    "authorized to work in the united states",
    "authorized to work in the us",
    "authorized to work in the u.s.",
    "must be authorized to work in the us",
    "authorized to work in the us without sponsorship",
    "no visa sponsorship",
    "without requiring sponsorship",
    "without sponsorship now or in the future",
]


# License / vehicle requirements
LICENSE_PATTERNS: list[str] = [
    "valid driver's license required",
    "valid drivers license required",
    "driver's license required",
    "drivers license required",
    "must have valid driver's license",
    "must have valid drivers license",
    "must possess driver's license",
    "must possess drivers license",
    "cdl required",
    "commercial driver's license",
    "commercial drivers license",
    "personal vehicle required",
    "must have own vehicle",
    "must have a personal vehicle",
    "reliable transportation required",
    "must have reliable transportation",
]


def _has_any(text: str, patterns: list[str]) -> bool:
    for p in patterns:
        if term_pattern(p).search(text):
            return True
    return False


def detect_citizenship_required(text: str | None) -> bool:
    """Returns True iff the text contains citizenship/clearance gating
    language. ``None`` or empty returns False.

    Inclusive override: even if a restrictive substring matches, the
    detector returns False when the JD also says something like "US
    citizen or permanent resident" — permanent residents and other
    US-authorized applicants fit the eligibility. Active security
    clearances always count as restrictive (no inclusive override
    overrides clearance language)."""
    if not text:
        return False

    # Clearance language is always restrictive — no override should
    # make a TS/SCI / polygraph role appear unrestricted.
    if _has_any(text, CLEARANCE_PATTERNS):
        return True

    if _has_any(text, CITIZENSHIP_INCLUSIVE_PATTERNS):
        return False

    return _has_any(text, CITIZENSHIP_PATTERNS)


def detect_license_required(text: str | None) -> bool:
    """Returns True iff the text *requires* a driver's license / vehicle.
    Aspirational language ("willing to travel", "occasional driving")
    is not a hit. ``None`` or empty returns False."""
    if not text:
        return False
    return _has_any(text, LICENSE_PATTERNS)


# ----- Phase 4.8b: title-only seniority detection -----

# Lowercase patterns matched against the lowercased title. The
# user is an entry-level pivot (CS grad, IT support background) —
# titles carrying these markers are out of reach regardless of
# match_score, so land_score zeroes them out via eligibility_mult.
_SENIORITY_TITLE_PATTERNS: tuple[str, ...] = (
    r"\bsenior\b",
    r"\bsr\.?\b",
    r"\blead\b",
    r"\bprincipal\b",
    r"\bstaff\b",
    r"\bdirector\b",
    r"\bmanager\b",
    r"\bmanaging\b",
    r"\bhead\s+of\b",
    r"\bvp\b",
    r"\bvice\s+president\b",
    r"\bchief\b",
)

# Roman numerals II / III / IV are common seniority suffixes
# ("Data Analyst II", "Engineer III"). Matched case-sensitively
# against the original-cased title to avoid false positives from
# random caps in body text or all-caps titles. V is intentionally
# excluded — too easy to collide with letters in a name or acronym.
_ROMAN_NUMERAL_PATTERNS: tuple[str, ...] = (
    r"\bII\b",
    r"\bIII\b",
    r"\bIV\b",
)

# Junior / entry-level overrides. When any of these match, the title
# is classified as "junior" even if a senior marker also appears
# (e.g. "Sr SWE Intern" → kept as junior).
_JUNIOR_OVERRIDE_PATTERNS: tuple[str, ...] = (
    r"\bjunior\b",
    r"\bjr\.?\b",
    r"\bassociate\b",
    r"\bintern\b",
    r"\binternship\b",
    r"\bentry[\s-]?level\b",
    r"\btrainee\b",
    r"\bgraduate\b",
    r"\bnew[\s-]?grad\b",
    r"\bapprentice\b",
)


def detect_seniority(title: str | None) -> str:
    """Classify ``title`` as ``"junior"``, ``"senior"``, or ``"mid"``.

    Junior markers (intern, associate, junior, ...) override senior
    markers — handles edge cases like "Sr SWE Intern" where the role
    is genuinely entry-level despite carrying "Sr" in the title.

    Returns ``"mid"`` for titles with no markers either way, and for
    ``None`` / empty input.
    """
    if not title:
        return "mid"
    title_lower = title.lower()

    if any(re.search(p, title_lower) for p in _JUNIOR_OVERRIDE_PATTERNS):
        return "junior"

    if any(re.search(p, title_lower) for p in _SENIORITY_TITLE_PATTERNS):
        return "senior"

    # Roman numerals checked against original casing, not lowercased.
    if any(re.search(p, title) for p in _ROMAN_NUMERAL_PATTERNS):
        return "senior"

    return "mid"
