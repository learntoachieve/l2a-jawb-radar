"""Land score — pivot-aware ranking layered on top of match_score.

land_score = match_score
           * (1 + skill_density_bonus)   # -0.30 .. +0.30
           * source_quality_mult         # 0.85 .. 1.15
           * title_family_mult           # 0.65 .. 1.00 (per role-family taxonomy)
           * experience_match_mult       # 0.50 .. 1.10 (junior boost / senior penalty)
           * salary_mult                 # 0.85 / 1.00 / 0.95 / 1.00
           * eligibility_mult            # 0.0 (blocklist) or 1.0

Capped at [0.0, 100.0]. The match_score input is whatever the existing
scoring engine produced — this layer only re-ranks.

All sub-functions are pure (no DB or filesystem). YAML loaders live in
this module so callers can pass already-parsed dicts in tests.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from .eligibility_utils import detect_seniority
from .text_utils import term_pattern


_CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"

# Phase 4.7: skill density saturates when a JD hits this many
# mid-tier skill points (= 8 distinct skills × avg tier-2 weight 2).
# Same target as match_score_v2's TARGET_SKILL_DENSITY so the two
# layers agree on what "fully matched" means.
TARGET_DENSITY_POINTS = 16

# ----- Config loaders -----


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    return data if isinstance(data, dict) else {}


def load_role_families(path: Path | None = None) -> dict[str, Any]:
    return _load_yaml(path or _CONFIG_DIR / "role_families.yaml")


def load_source_quality(path: Path | None = None) -> dict[str, Any]:
    return _load_yaml(path or _CONFIG_DIR / "source_quality.yaml")


def load_title_blocklist(path: Path | None = None) -> list[str]:
    data = _load_yaml(path or _CONFIG_DIR / "title_blocklist.yaml")
    raw = data.get("blocked_patterns") or []
    return [str(p).lower() for p in raw if str(p).strip()]


# ----- Skill density -----


def _tier_weight(tier: int) -> int:
    """Tier 1 = 3 points, tier 2 = 2, tier 3 = 1."""
    return {1: 3, 2: 2, 3: 1}.get(int(tier or 2), 2)


def _compute_skill_density_bonus(
    body: str | None,
    profile_criteria: list[dict[str, Any]],
) -> tuple[float, list[str]]:
    """Return ``(bonus, matched_terms)`` where bonus is in [-0.20, +0.30].

    Phase 4.7 — divisor switched from "sum of all criterion tier
    weights" (~39 for the user's 19-criterion profile, which the
    largest realistic JD could never approach) to a target-density
    constant. A JD that hits 8 distinct mid-tier skills lands at
    normalised=1.0, matching match_score's TARGET_SKILL_DENSITY
    threshold so the two layers agree on what "comprehensively
    aligned" looks like.

    Asymmetric clamp range: down to -0.20, up to +0.30. The match-
    score layer already rewards skill density at 40% weight, so the
    land_score density bonus penalises softer than it rewards — it's
    a tilt, not a doubling.
    """
    target_points = TARGET_DENSITY_POINTS
    if not profile_criteria or target_points <= 0:
        return 0.0, []
    text = (body or "").lower()
    if not text:
        return -0.2, []

    matched_score = 0
    matched_terms: list[str] = []
    for c in profile_criteria:
        term = (c.get("term") or "").strip()
        if not term:
            continue
        if term_pattern(term).search(text):
            matched_score += _tier_weight(c.get("weight_tier", 2))
            matched_terms.append(term)

    normalized = min(1.0, matched_score / target_points)
    # Asymmetric: 0 hits -> -0.20, target+ -> +0.30. Linear segment
    # passes through 0 at normalized=0.4 (the rough density of a
    # mid-fit JD).
    if normalized <= 0.4:
        bonus = -0.20 + (0.20 / 0.4) * normalized
    else:
        bonus = (0.30 / 0.6) * (normalized - 0.4)
    return max(-0.20, min(0.30, bonus)), matched_terms


# ----- Source quality -----


def _compute_source_quality(source_name: str | None, config: dict[str, Any]) -> float:
    sources = config.get("sources") or {}
    default = float(config.get("default_multiplier", 1.0))
    if not source_name:
        return default
    return float(sources.get(source_name, default))


# ----- Title family -----


def _title_match(title: str, pattern: str) -> bool:
    """Whole-phrase, case-insensitive, word-boundary match."""
    if not title or not pattern:
        return False
    return term_pattern(pattern).search(title) is not None


def _compute_title_family(
    title: str | None, config: dict[str, Any]
) -> tuple[float, str]:
    """Return ``(multiplier, family_name)`` for the first matching family."""
    if not title:
        return float(config.get("default_multiplier", 0.7)), "default"
    families = config.get("families") or []
    for fam in families:
        if not isinstance(fam, dict):
            continue
        for pat in fam.get("patterns") or []:
            if _title_match(title, str(pat)):
                return float(fam.get("multiplier", 1.0)), str(fam.get("name", ""))
    return float(config.get("default_multiplier", 0.7)), "default"


# ----- Experience match -----

_JUNIOR_TERMS = (
    "junior", "jr.", "entry level", "entry-level", "associate",
    "graduate", "intern", "internship", "early career",
)

_SENIOR_TERMS = (
    "senior", "staff", "principal", "lead", "director",
    "head of", "vp", "chief",
)

_YEARS_REQ_RE = re.compile(
    r"\b(?:5|6|7|8|10)\s*\+?\s*years?\b|\bminimum\s+(?:5|7|8|10)\s+years?\b",
    re.IGNORECASE,
)


def _compute_experience_match(title: str | None, body: str | None) -> float:
    """Junior boost ×1.10; seniority terms ×0.7; 5+/7+/10+ years ×0.5.

    Multipliers stack multiplicatively, then clamped to [0.5, 1.1].
    """
    title_l = (title or "").lower()
    body_l = (body or "").lower()

    has_junior = any(term_pattern(t).search(title_l) for t in _JUNIOR_TERMS)
    has_senior = any(term_pattern(t).search(title_l) for t in _SENIOR_TERMS)
    has_years = bool(_YEARS_REQ_RE.search(body_l))

    mult = 1.0
    if has_junior:
        mult *= 1.10
    if has_senior:
        mult *= 0.70
    if has_years:
        mult *= 0.50
    return max(0.5, min(1.1, mult))


# ----- Eligibility (blocklist) -----


def _compute_eligibility_mult(
    title: str | None, blocklist: list[str]
) -> tuple[float, str | None]:
    """0.0 if title is blocklisted *or* carries explicit senior markers,
    else 1.0.

    Blocklist substring match is case-insensitive on the lowercased
    title — matches the spec's "Note: blocklist applies via substring
    match against lowercased title."

    Phase 4.8b adds a second floor: titles classified as "senior" by
    :func:`detect_seniority` (Senior / Sr / Lead / Principal / Staff /
    Manager / Director / II / III / IV…) are out of reach for the
    user's entry-level pivot, so they zero out here. Junior overrides
    inside detect_seniority keep "Sr SWE Intern" eligible.
    """
    if not title:
        return 1.0, None
    title_l = title.lower()
    for pat in blocklist:
        pat_l = pat.lower().strip()
        if not pat_l:
            continue
        if pat_l in title_l:
            return 0.0, pat_l
    if detect_seniority(title) == "senior":
        return 0.0, "senior title"
    return 1.0, None


# ----- Salary band -----


def _coerce_salary(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value) if value > 0 else None
    if isinstance(value, str):
        digits = re.sub(r"[^\d.]", "", value)
        try:
            f = float(digits) if digits else 0.0
        except ValueError:
            return None
        if f <= 0:
            return None
        # Bare-number heuristic: "120" almost certainly means $120K, not $120.
        if f < 1000:
            f *= 1000
        return f
    return None


def _compute_salary_mult(item: dict[str, Any]) -> tuple[float, str]:
    """Penalize sub-$60K (×0.85) and >$200K (×0.95) bands. No data ⇒ 1.0."""
    md = item.get("metadata_json") or {}
    salary_max = _coerce_salary(md.get("salary_max"))
    if salary_max is None:
        salary_max = _coerce_salary(md.get("salary_min"))
    if salary_max is None:
        return 1.0, "no posted salary"
    if salary_max < 60_000:
        return 0.85, f"${salary_max:,.0f} max — below $60k band"
    if salary_max > 200_000:
        return 0.95, f"${salary_max:,.0f} max — likely senior band"
    return 1.0, f"${salary_max:,.0f} max — neutral band"


# ----- Public API -----


def compute_land_score(
    item: dict[str, Any],
    profile_criteria: list[dict[str, Any]],
    role_families: dict[str, Any],
    source_quality: dict[str, Any],
    title_blocklist: list[str],
    match_score: float,
) -> tuple[float, dict[str, Any]]:
    """Compute land_score and a transparent breakdown.

    ``item`` should expose ``title``, ``body``, ``source_name`` (or
    ``metadata_json["source"]``), and ``metadata_json``.  Pass the
    item's existing match_score in directly so this layer never has
    to round-trip through the scoring engine.

    Returns ``(land_score, breakdown)``. The breakdown carries every
    intermediate multiplier plus the human-readable reason that
    produced it, so the dashboard can render an explainable detail
    panel.
    """
    title = item.get("title") or ""
    body = item.get("body") or ""
    source_name = item.get("source_name") or (item.get("metadata_json") or {}).get(
        "source"
    )

    bonus, matched_terms = _compute_skill_density_bonus(body, profile_criteria)
    src_mult = _compute_source_quality(source_name, source_quality)
    fam_mult, fam_name = _compute_title_family(title, role_families)
    exp_mult = _compute_experience_match(title, body)
    elig_mult, elig_reason = _compute_eligibility_mult(title, title_blocklist)
    sal_mult, sal_reason = _compute_salary_mult(item)

    score = (
        float(match_score or 0.0)
        * (1.0 + bonus)
        * src_mult
        * fam_mult
        * exp_mult
        * sal_mult
        * elig_mult
    )
    score = max(0.0, min(100.0, score))

    breakdown: dict[str, Any] = {
        "match_score": float(match_score or 0.0),
        "skill_density_bonus": bonus,
        "skills_matched_count": len(matched_terms),
        "skills_matched_terms": matched_terms,
        "source_quality_mult": src_mult,
        "title_family_mult": fam_mult,
        "title_family_matched": fam_name,
        "experience_match_mult": exp_mult,
        "salary_mult": sal_mult,
        "salary_reason": sal_reason,
        "eligibility_mult": elig_mult,
        "eligibility_reason": elig_reason,
        "land_score": score,
    }
    return score, breakdown
