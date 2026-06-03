"""Score a single Item against a single Profile.

Phase 3 — wraps the v2 match-score engine with three additive signal
extractors (seniority, salary, dealbreaker). Resume-aware: Score rows
are keyed by (item_id, profile_id, resume_id), so the same profile can
hold parallel score sets for each uploaded resume.

Public API:

  score_item_raw(item, profile, session, criteria=None)
      -> (raw_score, matched_terms): legacy single-resume path. Loads
      every criterion the profile owns and computes a v2 score with
      no signal extractors applied.

  score_item_full(item, profile, criteria_dicts, blocklist)
      -> (final_score, breakdown, matched_terms): the Phase 3 path.
      ``criteria_dicts`` should come from
      ``profile_manager.get_active_criteria`` so manual + active-resume
      criteria are merged correctly. Applies:
        - +5 if profile.seniority_level matches detected JD signal
        - -15 on conflict
        - salary_match flag (in_range / out_of_range / unknown)
        - dealbreakers => final score forced to 0
      Returns the final score, the extended breakdown dict, and the
      v1-shaped matched_terms list.

  upsert_score(item_id, profile_id, resume_id, normalized, raw,
               matched, breakdown, session)
      Upsert keyed by (item_id, profile_id, resume_id). The unique
      constraint guarantees idempotence within a single resume slot.

  score_item(item, profile, session) -> Score: legacy convenience
      wrapper used by single-shot callers. Persists with resume_id=None.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import Criterion, Item, Profile, Score

from .land_score import load_role_families
from .match_score_v2 import (
    compute_match_score,
    extract_dealbreakers,
    extract_salary_signal,
    extract_seniority_signal,
    matched_terms_from_breakdown,
)

# Bonus / penalty applied to the base v2 score after seniority detection.
SENIORITY_MATCH_BONUS = 5
SENIORITY_MISMATCH_PENALTY = -15

# Cached YAML config loaded lazily on first scoring call.
_role_families_cfg: dict | None = None


def _families() -> dict:
    global _role_families_cfg
    if _role_families_cfg is None:
        _role_families_cfg = load_role_families()
    return _role_families_cfg


def _now_utc_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _load_criteria(session: Session, profile_id: int) -> list[Criterion]:
    return (
        session.execute(
            select(Criterion).where(Criterion.profile_id == profile_id)
        )
        .scalars()
        .all()
    )


def _criteria_to_dicts(criteria: list[Criterion]) -> list[dict]:
    return [
        {
            "term": c.term,
            "kind": c.kind,
            "weight": c.weight,
            "weight_tier": int(getattr(c, "weight_tier", 2) or 2),
        }
        for c in criteria
    ]


def _profile_seniority(profile: Profile) -> Optional[str]:
    val = getattr(profile, "seniority_level", None)
    if val is None:
        return None
    if hasattr(val, "value"):
        val = val.value
    val = str(val).lower()
    return val if val and val != "any" else None


def _salary_overlap(
    profile: Profile,
    j_min: Optional[int],
    j_max: Optional[int],
) -> str:
    if j_min is None and j_max is None:
        return "unknown"
    p_min = profile.salary_min or 0
    p_max = profile.salary_max or 10_000_000
    lo = j_min if j_min is not None else 0
    hi = j_max if j_max is not None else 10_000_000
    return "in_range" if (hi >= p_min and lo <= p_max) else "out_of_range"


# ---------------------------------------------------------------------------
# Phase 3 path
# ---------------------------------------------------------------------------

def score_item_full(
    item: Item,
    profile: Profile,
    criteria_dicts: list[dict],
    blocklist: list[str],
) -> tuple[float, dict[str, Any], list[dict[str, Any]]]:
    """Compute v2 score + Phase 3 signals.

    ``criteria_dicts`` must already be filtered to the criteria the
    score should consider (typically active-resume + manual). This
    function never queries the DB.
    """
    item_dict = {"title": item.title or "", "body": item.body or ""}
    base_score, breakdown = compute_match_score(
        item_dict, criteria_dicts, _families()
    )

    body = item.body or ""
    seniority_signal = extract_seniority_signal(body)
    sal_min, sal_max = extract_salary_signal(body)
    dealbreakers = extract_dealbreakers(body, blocklist or [])

    profile_seniority = _profile_seniority(profile)
    if seniority_signal is None or profile_seniority is None:
        seniority_match = "n/a"
        seniority_adjustment = 0
    elif seniority_signal == profile_seniority:
        seniority_match = "match"
        seniority_adjustment = SENIORITY_MATCH_BONUS
    else:
        seniority_match = "mismatch"
        seniority_adjustment = SENIORITY_MISMATCH_PENALTY

    salary_match = _salary_overlap(profile, sal_min, sal_max)

    final = max(0.0, min(100.0, base_score + seniority_adjustment))
    if dealbreakers:
        final = 0.0

    breakdown.update({
        "base_match_score": base_score,
        "seniority_signal": seniority_signal,
        "seniority_match": seniority_match,
        "seniority_adjustment": seniority_adjustment,
        "salary_min": sal_min,
        "salary_max": sal_max,
        "salary_match": salary_match,
        "dealbreakers": dealbreakers,
        "final": final,
    })

    matched = matched_terms_from_breakdown(breakdown)
    return final, breakdown, matched


# ---------------------------------------------------------------------------
# Legacy single-resume path (still used by single-item callers)
# ---------------------------------------------------------------------------

def score_item_raw(
    item: Item,
    profile: Profile,
    session: Session,
    criteria: list[Criterion] | None = None,
) -> tuple[float, list[dict]]:
    """Pre-Phase-3 contract. Returns ``(raw_score, matched_terms)``.

    No signal extractors applied; preserved so legacy callers (existing
    tests, single-item rescore) keep their exact prior behavior.
    """
    if criteria is None:
        criteria = _load_criteria(session, profile.id)

    item_dict = {"title": item.title or "", "body": item.body or ""}
    profile_criteria = _criteria_to_dicts(criteria)

    score, breakdown = compute_match_score(
        item_dict, profile_criteria, _families()
    )
    matched = matched_terms_from_breakdown(breakdown)
    return max(0.0, score), matched


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def upsert_score(
    item_id: int,
    profile_id: int,
    resume_id: Optional[int],
    normalized: float,
    raw: float,
    matched: list[dict],
    breakdown: Optional[dict[str, Any]],
    session: Session,
) -> Score:
    """Insert or update a Score row keyed by (item_id, profile_id, resume_id).

    ``resume_id=None`` is a distinct key from any numeric resume id, so
    a profile with no active resume gets its own row that never clashes
    with a resume-scoped score.
    """
    resume_cond = (
        Score.resume_id.is_(None) if resume_id is None
        else Score.resume_id == resume_id
    )
    existing = session.execute(
        select(Score).where(
            Score.item_id == item_id,
            Score.profile_id == profile_id,
            resume_cond,
        )
    ).scalar_one_or_none()

    now = _now_utc_naive()
    if existing is None:
        row = Score(
            item_id=item_id,
            profile_id=profile_id,
            resume_id=resume_id,
            score=normalized,
            raw_score=raw,
            matched_terms_json=matched,
            score_breakdown_json=breakdown,
            computed_at=now,
        )
        session.add(row)
        session.flush()
        return row

    existing.score = normalized
    existing.raw_score = raw
    existing.matched_terms_json = matched
    existing.score_breakdown_json = breakdown
    existing.computed_at = now
    session.flush()
    return existing


def score_item(item: Item, profile: Profile, session: Session) -> Score:
    """Score one item against a profile and persist. Legacy single-shot path.

    Uses the active resume on the profile if any; the resulting Score
    row carries that resume_id (or None if the profile has no resume).
    """
    criteria = _load_criteria(session, profile.id)
    raw, matched = score_item_raw(item, profile, session, criteria=criteria)
    return upsert_score(
        item.id, profile.id, profile.active_resume_id,
        raw, raw, matched, None, session,
    )
