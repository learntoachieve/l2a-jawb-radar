"""Score a single Item against a single Profile.

Phase 4.7 — this module is now a thin shim over scoring/match_score_v2.
The v2 sub-component formula:

  match_score = (
      role_match_score    * 0.35
    + skill_match_score   * 0.40
    + title_family_score  * 0.15
    + body_keyword_score  * 0.10
  ) * 100

The matched-terms list returned via score_item_raw is reshaped from
the v2 breakdown into the v1-compatible {term, kind, contribution}
shape that the dashboard's top_matched_terms field expects, so
downstream UI keeps working without changes.

Public API:
  score_item_raw(item, profile, session, criteria=None)
      -> (raw_score, matched_terms): pure computation, no persistence.

  score_item(item, profile, session) -> Score: persists a Score row.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import Criterion, Item, Profile, Score

from .land_score import load_role_families
from .match_score_v2 import (
    compute_match_score,
    matched_terms_from_breakdown,
)

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


def score_item_raw(
    item: Item,
    profile: Profile,
    session: Session,
    criteria: list[Criterion] | None = None,
) -> tuple[float, list[dict]]:
    """Compute v2 match_score and a v1-shaped matched-terms list.

    Returns ``(raw_score, matched_terms)``. Floored at zero. Pass
    ``criteria`` to avoid a redundant query when scoring many items
    against the same profile.
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


def upsert_score(
    item_id: int,
    profile_id: int,
    normalized: float,
    raw: float,
    matched: list[dict],
    session: Session,
) -> Score:
    """Insert or update a Score row keyed by (item_id, profile_id)."""
    existing = session.execute(
        select(Score).where(
            Score.item_id == item_id,
            Score.profile_id == profile_id,
        )
    ).scalar_one_or_none()

    now = _now_utc_naive()
    if existing is None:
        row = Score(
            item_id=item_id,
            profile_id=profile_id,
            score=normalized,
            raw_score=raw,
            matched_terms_json=matched,
            computed_at=now,
        )
        session.add(row)
        session.flush()
        return row

    existing.score = normalized
    existing.raw_score = raw
    existing.matched_terms_json = matched
    existing.computed_at = now
    session.flush()
    return existing


def score_item(item: Item, profile: Profile, session: Session) -> Score:
    """Score one item and persist. v2 emits a 0-100 raw score directly,
    so the persisted ``score`` field equals the raw_score for single-
    item callers. Batch scoring (``batch.score_all_items``) optionally
    re-normalises across the dataset for legacy callers."""
    criteria = _load_criteria(session, profile.id)
    raw, matched = score_item_raw(item, profile, session, criteria=criteria)
    # v2 raw_score is already on a 0-100 scale, so no further
    # normalization is needed for single-item callers.
    return upsert_score(item.id, profile.id, raw, raw, matched, session)
