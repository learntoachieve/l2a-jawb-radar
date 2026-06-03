"""Phase 4.7 match-score overhaul.

The v1 scoring engine (scoring/scorer.py) gave a literal "data analyst"
title hit a 64x weight (8 base × 4 criterion weight × 2 title boost)
while skill matches contributed at most 9 (3 cap × 3 weight). One title
match dwarfed five skill matches, which made staffing-agency reposts
that contained "Data Analyst" in the title outscore Ashby AI-lab roles
that genuinely matched 4-6 of the user's resume skills.

v2 splits the score into four explicit, separately-testable
sub-components weighted to a unit final score:

  match_score = (
      role_match_score    * 0.35
    + skill_match_score   * 0.40
    + title_family_score  * 0.15
    + body_keyword_score  * 0.10
  ) * 100

Each sub-score is in [0.0, 1.0] and the final is in [0, 100]. The
breakdown dict carries the four sub-scores, the matched terms per
component, and the family name so the dashboard's Details panel can
render an explainable trace.

Role-family multipliers are stored under
``MATCH_TITLE_FAMILY_WEIGHTS`` here so this module owns its own
scoring weights independent of land_score's narrower
``land_score_mult`` range in role_families.yaml. Both look the
family up via the same yaml taxonomy (``compute_title_family``);
they just apply different downstream weights.
"""
from __future__ import annotations

from typing import Any, Optional

from .text_utils import (
    clean_html,
    find_term_in_text,
    normalize_unicode,
    term_pattern,
)

# Number of distinct skill matches at which skill_match_score saturates
# at 1.0. JDs typically mention 3-7 of the user's skills; an 8-skill
# match is "comprehensively aligned." Used as the divisor for the
# weighted-points sum (assuming average tier weight of 2).
TARGET_SKILL_DENSITY = 8
TARGET_SKILL_POINTS = 16  # 8 × avg tier-2 weight of 2

# Sub-component weights. Must sum to 1.0.
# Phase 4.7 first-pass tuning: skill_match outweighs role_match so
# skill-aligned but title-mismatched JDs (e.g. AI-lab "Operations
# Engineer" with python/sql/analytics in the body) can compete with
# literal "Data Analyst" titles. The 5pp shift from role to skill is
# the architect's prescribed correction.
W_ROLE = 0.30
W_SKILL = 0.45
W_FAMILY = 0.15
W_KEYWORD = 0.10

# Title-family multipliers used at the match-score level. These are
# wider than land_score's title_family_mult (0.85-1.0): a 0.6 here
# means "IT Support is rewarded but reflects a real career-path step
# down from data analyst," and is layered with land_score's much
# softer family multiplier later. Family detection itself comes from
# config/role_families.yaml.
MATCH_TITLE_FAMILY_WEIGHTS: dict[str, float] = {
    "data_analyst_exact": 1.00,
    "data_analyst_adjacent": 0.95,
    "operations_quant_research_analyst": 0.85,
    # Phase 4.7.1: AI-lab / specialist families. Targeted at the
    # title shapes that Ashby AI labs (OpenAI, Anthropic, Mistral,
    # Perplexity, Cohere) use for roles whose JDs match the user's
    # skill set without containing literal "data analyst".
    "ai_lab_technical_staff": 0.85,
    "operations_specialist": 0.80,
    "legal_tech_compliance": 0.75,
    "junior_dev": 0.70,
    "qa_tester": 0.70,
    "it_support_pivot": 0.60,
    "cs_adjacent_misc": 0.55,
    # Default raised from 0.40 to 0.50 — many AI-lab titles fall
    # outside the existing taxonomy (e.g. "Member of Technical Staff",
    # "Operations Engineer", "Legal Tech Associate") and shouldn't be
    # halved at the family layer when the skill signal carries them.
    "default": 0.50,
}


def _tier_weight(tier: int) -> int:
    return {1: 3, 2: 2, 3: 1}.get(int(tier or 2), 2)


# ----- Sub-score 1: role_match_score -----


def role_match_score(item_title: str | None, role_terms: list[str]) -> float:
    """1.0 if a role criterion appears in the title at word boundary,
    0.5 if it appears only in the body but not the title, 0 otherwise.

    ``role_terms`` should already be the set of role-kind criterion
    terms. Body-presence is signalled separately via the body argument
    in compute_match_score; this function only looks at the title.
    """
    if not item_title or not role_terms:
        return 0.0
    title_norm = normalize_unicode(clean_html(item_title))
    for term in role_terms:
        if term and term_pattern(term).search(title_norm):
            return 1.0
    return 0.0


def role_match_score_with_body(
    item_title: str | None, item_body: str | None, role_terms: list[str]
) -> float:
    """Title hit -> 1.0. Body-only hit -> 0.5. No hit -> 0.0."""
    if role_match_score(item_title, role_terms) >= 1.0:
        return 1.0
    if not item_body or not role_terms:
        return 0.0
    body_norm = normalize_unicode(clean_html(item_body))
    for term in role_terms:
        if term and term_pattern(term).search(body_norm):
            return 0.5
    return 0.0


# ----- Sub-score 2: skill_match_score -----


def skill_match_score(
    body: str | None, skill_criteria: list[dict[str, Any]]
) -> tuple[float, list[str]]:
    """Return ``(score in [0, 1], list of matched terms)``.

    Each tier-weighted skill that appears in the JD body contributes
    its tier weight (3/2/1). The sum is normalised against
    ``TARGET_SKILL_POINTS`` (= 8 × avg tier weight of 2). A JD that
    matches 8 mid-tier skills lands at 1.0; 4 mid-tier matches → 0.5;
    no matches → 0.0.

    A single skill counted at most once even if it appears multiple
    times in the body — density across distinct skills, not repetition.
    """
    if not body or not skill_criteria:
        return 0.0, []
    body_norm = normalize_unicode(clean_html(body))
    points = 0
    matched: list[str] = []
    for c in skill_criteria:
        term = (c.get("term") or "").strip()
        if not term:
            continue
        if term_pattern(term).search(body_norm):
            points += _tier_weight(c.get("weight_tier", 2))
            matched.append(term)
    score = min(1.0, points / TARGET_SKILL_POINTS)
    return score, matched


# ----- Sub-score 3: title_family_score -----


def _detect_family(title: str | None, role_families_cfg: dict[str, Any]) -> str:
    """Return the family name for the first matching pattern, or 'default'."""
    if not title:
        return "default"
    title_norm = normalize_unicode(clean_html(title))
    for fam in role_families_cfg.get("families") or []:
        if not isinstance(fam, dict):
            continue
        for pat in fam.get("patterns") or []:
            if pat and term_pattern(str(pat)).search(title_norm):
                return str(fam.get("name") or "default")
    return "default"


def title_family_score(
    title: str | None, role_families_cfg: dict[str, Any]
) -> tuple[float, str]:
    """Return ``(weight in [0, 1], family name)`` for the JD title.

    Uses role_families.yaml for family detection; applies the
    match-level weight from ``MATCH_TITLE_FAMILY_WEIGHTS`` (deliberately
    wider than land_score's title_family_mult, since land_score also
    boosts the same family — see module docstring).
    """
    family = _detect_family(title, role_families_cfg)
    weight = MATCH_TITLE_FAMILY_WEIGHTS.get(
        family, MATCH_TITLE_FAMILY_WEIGHTS["default"]
    )
    return weight, family


# ----- Sub-score 4: body_keyword_score -----


def body_keyword_score(
    body: str | None, keyword_criteria: list[dict[str, Any]]
) -> tuple[float, list[str]]:
    """Light keyword-criterion presence signal. Each keyword criterion
    that hits the body counts once; normalised by ``len(criteria)`` so
    a JD that matches every keyword reaches 1.0.

    Distinct from skill_match: skill criteria are scored via
    skill_match_score and don't double-count here. Keyword criteria
    in this user's profile are softer signals (e.g. "analytics",
    "documentation") and contribute the smallest sub-component
    weight (10%) on purpose.
    """
    if not body or not keyword_criteria:
        return 0.0, []
    body_norm = normalize_unicode(clean_html(body))
    matched: list[str] = []
    for c in keyword_criteria:
        term = (c.get("term") or "").strip()
        if not term:
            continue
        if term_pattern(term).search(body_norm):
            matched.append(term)
    score = min(1.0, len(matched) / max(1, len(keyword_criteria)))
    return score, matched


# ----- Public API -----


def compute_match_score(
    item: dict[str, Any],
    profile_criteria: list[dict[str, Any]],
    role_families_cfg: dict[str, Any],
) -> tuple[float, dict[str, Any]]:
    """Compute v2 match_score and a per-component breakdown.

    ``item`` exposes ``title`` and ``body`` (HTML allowed; cleaned here).
    ``profile_criteria`` is a list of dicts with ``term``, ``kind``,
    and ``weight_tier`` (skills only) — exclude rows are ignored at
    this layer; eligibility is enforced by land_score's blocklist.
    """
    title = item.get("title") or ""
    body = item.get("body") or ""

    role_terms: list[str] = [
        (c.get("term") or "")
        for c in profile_criteria
        if (c.get("kind") or "") == "role"
    ]
    skill_criteria = [
        c for c in profile_criteria if (c.get("kind") or "") == "skill"
    ]
    keyword_criteria = [
        c for c in profile_criteria if (c.get("kind") or "") == "keyword"
    ]

    role_score = role_match_score_with_body(title, body, role_terms)
    skill_score, matched_skills = skill_match_score(body, skill_criteria)
    family_score, family_name = title_family_score(title, role_families_cfg)
    keyword_score, matched_keywords = body_keyword_score(body, keyword_criteria)

    final = (
        role_score * W_ROLE
        + skill_score * W_SKILL
        + family_score * W_FAMILY
        + keyword_score * W_KEYWORD
    ) * 100.0
    final = max(0.0, min(100.0, final))

    breakdown: dict[str, Any] = {
        "role_match_score": role_score,
        "skill_match_score": skill_score,
        "title_family_score": family_score,
        "title_family_matched": family_name,
        "body_keyword_score": keyword_score,
        "matched_skills": matched_skills,
        "matched_keywords": matched_keywords,
        "weights": {
            "role": W_ROLE,
            "skill": W_SKILL,
            "family": W_FAMILY,
            "keyword": W_KEYWORD,
        },
        "final": final,
    }
    return final, breakdown


def matched_terms_from_breakdown(
    breakdown: dict[str, Any]
) -> list[dict[str, Any]]:
    """Reshape v2 breakdown into the v1-compatible ``matched_terms_json``
    list shape that the dashboard's existing ``top_matched_terms`` field
    consumes.

    Each entry: ``{term, kind, contribution}``. ``contribution`` is the
    sub-component weighted contribution to the final score so the
    dashboard's "top 3 matched terms" still surfaces the highest-impact
    signals.
    """
    items: list[dict[str, Any]] = []
    family = breakdown.get("title_family_matched") or "default"
    items.append(
        {
            "term": family,
            "kind": "title_family",
            "contribution": float(
                (breakdown.get("title_family_score") or 0)
                * breakdown.get("weights", {}).get("family", W_FAMILY)
                * 100
            ),
            "occurrences": 1 if family != "default" else 0,
            "in_title": True,
        }
    )
    for term in breakdown.get("matched_skills") or []:
        items.append(
            {
                "term": term,
                "kind": "skill",
                "contribution": float(
                    breakdown.get("skill_match_score", 0)
                    * breakdown.get("weights", {}).get("skill", W_SKILL)
                    * 100
                    / max(1, len(breakdown.get("matched_skills") or []))
                ),
                "occurrences": 1,
                "in_title": False,
            }
        )
    for term in breakdown.get("matched_keywords") or []:
        items.append(
            {
                "term": term,
                "kind": "keyword",
                "contribution": float(
                    breakdown.get("body_keyword_score", 0)
                    * breakdown.get("weights", {}).get("keyword", W_KEYWORD)
                    * 100
                    / max(1, len(breakdown.get("matched_keywords") or []))
                ),
                "occurrences": 1,
                "in_title": False,
            }
        )
    return items
