"""Batch scoring + score retention.

Phase 3 rewrites the previous prototype (which referenced a non-existent
``KeywordExtract`` model). The two public entry points are:

  score_profile(profile_id)
      Score every recent item (posted within 30 days) against the
      profile's active resume, skipping any item that already has a
      score row for (profile_id, active_resume_id). Uses the Phase 3
      ``score_item_full`` path so dealbreakers, salary, and seniority
      signals all flow into the persisted breakdown.

  purge_stale_scores(days=30)
      Delete score rows whose item is older than ``days`` and which
      have no tracking row for the same (item_id, profile_id) — items
      the user has touched stay in the scoreboard forever.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import and_, exists, or_, select

from db.database import get_session
from db.models import (
    Item,
    Profile,
    ProfileCompanyBlocklist,
    Score,
    Tracking,
)

from .geo_filter import is_us_or_remote as _is_us_or_remote
from .scorer import score_item_full, upsert_score


def _now_utc_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _load_blocklist(session, profile_id: int) -> list[str]:
    rows = session.execute(
        select(ProfileCompanyBlocklist.company_name).where(
            ProfileCompanyBlocklist.profile_id == profile_id
        )
    ).scalars().all()
    return [r for r in rows if r]


def _company_blocked(metadata_json, blocklist: list[str]) -> bool:
    """Case-insensitive substring match of metadata.company against blocklist.

    Same semantics as ``dashboard.data._company_is_blocked`` — kept here
    so batch scoring doesn't depend on the dashboard layer.
    """
    if not blocklist:
        return False
    company = (metadata_json or {}).get("company") if metadata_json else None
    if not company:
        return False
    c = str(company).lower()
    return any(term and term.strip().lower() in c for term in blocklist)


def score_profile(profile_id: int) -> dict[str, Any]:
    """Score recent items against this profile's active resume.

    Recent = items.posted_at within the last 30 days OR items with a
    NULL posted_at (some scrapers don't supply one and we'd rather
    over-score than silently drop unattributed posts).

    Skips any item that already has a Score for (profile_id, active_resume_id).
    """
    # Defer to avoid an import cycle: profile_manager -> scoring -> scorer
    # -> match_score_v2 -> text_utils. profile_manager itself imports
    # scoring.resume_parser, so a top-level import here is safe in
    # principle but the lazy form keeps batch.py importable even if
    # profile_manager is mid-edit.
    from profiles.profile_manager import get_active_criteria

    summary = {"scored": 0, "skipped": 0, "errors": 0, "profile_id": profile_id}

    criteria_dicts = get_active_criteria(profile_id)

    with get_session() as session:
        profile = session.get(Profile, profile_id)
        if profile is None:
            summary["error"] = f"profile {profile_id} not found"
            return summary

        active_resume_id = profile.active_resume_id
        blocklist = _load_blocklist(session, profile_id)

        cutoff = _now_utc_naive() - timedelta(days=30)
        items = session.execute(
            select(Item).where(
                or_(Item.posted_at.is_(None), Item.posted_at >= cutoff)
            )
        ).scalars().all()

        # Pre-load existing (item_id) for this (profile_id, active_resume_id)
        # so we don't issue a SELECT per item.
        resume_cond = (
            Score.resume_id.is_(None) if active_resume_id is None
            else Score.resume_id == active_resume_id
        )
        scored_item_ids = set(
            session.execute(
                select(Score.item_id).where(
                    Score.profile_id == profile_id,
                    resume_cond,
                )
            ).scalars().all()
        )

        for item in items:
            if item.id in scored_item_ids:
                summary["skipped"] += 1
                continue
            if _company_blocked(item.metadata_json, blocklist):
                # Don't even score blocklisted-company items. The dashboard
                # filters them again at query time, but skipping here
                # keeps the scores table clean.
                summary["skipped"] += 1
                continue
            if not _is_us_or_remote(item.metadata_json):
                # US-only for now: international postings waste scoring
                # capacity and inflate the corpus. Same predicate the
                # dashboard queue uses, so the two layers never disagree.
                summary["skipped"] += 1
                continue
            try:
                final, breakdown, matched = score_item_full(
                    item, profile, criteria_dicts, blocklist,
                )
                upsert_score(
                    item_id=item.id,
                    profile_id=profile_id,
                    resume_id=active_resume_id,
                    normalized=final,
                    raw=breakdown.get("base_match_score", final),
                    matched=matched,
                    breakdown=breakdown,
                    session=session,
                )
                summary["scored"] += 1
            except Exception as exc:
                summary["errors"] += 1
                print(f"[batch] error item {item.id}: {exc}")

        session.commit()

    return summary


def purge_stale_scores(days: int = 30) -> int:
    """Delete scores whose item is older than ``days`` AND has no tracking
    row for the same (item_id, profile_id).

    Items the user is tracking (interested, applied, interview, offer,
    ghosted, etc.) keep their scores forever so the pipeline tab can
    still reference them.

    Returns the number of deleted rows.
    """
    cutoff = _now_utc_naive() - timedelta(days=days)

    with get_session() as session:
        # Find score rows where:
        #  - linked item.posted_at < cutoff (NULL posted_at => skip)
        #  - and no tracking row exists for the same (item_id, profile_id)
        tracking_exists = (
            select(Tracking.id).where(
                Tracking.item_id == Score.item_id,
                Tracking.profile_id == Score.profile_id,
            ).exists()
        )

        targets = session.execute(
            select(Score.id).join(Item, Item.id == Score.item_id).where(
                Item.posted_at.is_not(None),
                Item.posted_at < cutoff,
                ~tracking_exists,
            )
        ).scalars().all()

        if not targets:
            return 0

        deleted = 0
        # Chunked delete keeps the parameter list under typical DB limits.
        chunk = 500
        for start in range(0, len(targets), chunk):
            ids = targets[start : start + chunk]
            session.execute(
                Score.__table__.delete().where(Score.id.in_(ids))
            )
            deleted += len(ids)

        session.commit()
        return deleted
