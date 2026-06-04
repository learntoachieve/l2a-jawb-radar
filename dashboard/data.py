"""
dashboard/data.py — read-only queue layer for the Streamlit UI.

``get_today_queue`` returns the ranked list of scoreable items for a
profile. Items are blended-ranked by:

    blended = score * 0.7 + recency_score_pct * 0.3

where ``recency_score_pct`` decays linearly from 100.0 (posted today)
to 0.0 (posted 30+ days ago). Both score and blended are on a 0-100
scale so the queue stays interpretable.

Filters applied:
  - score row belongs to (profile_id, profile.active_resume_id)
  - item posted within the last 30 days (or posted_at IS NULL — kept
    with a depressed recency_score so missing dates don't silently
    erase the queue)
  - dealbreakers (score_breakdown_json.dealbreakers) is empty/missing
  - tracking status is NOT 'hidden' or 'skipped' (or no tracking row)

``get_queue_total`` returns the post-filter total so the UI can render
pagination correctly.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import aliased

from db.database import get_session
from db.models import (
    Item,
    Profile,
    ProfileCompanyBlocklist,
    Score,
    Tracking,
    TrackingStatus,
)
# US-only filter (shared with scoring.batch so the queue and the scorer
# agree on what counts as a US/remote-US posting).
from scoring.geo_filter import is_us_or_remote as _is_us_or_remote


DEFAULT_PAGE_SIZE = 300
RETENTION_DAYS = 30
W_SCORE = 0.7
W_RECENCY = 0.3
HIDDEN_STATUSES = {"hidden", "skipped"}


def _now_utc_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _load_blocklist_terms(session, profile_id: int) -> list[str]:
    """Return lowercased company-name strings for case-insensitive matching."""
    rows = session.execute(
        select(ProfileCompanyBlocklist.company_name).where(
            ProfileCompanyBlocklist.profile_id == profile_id
        )
    ).scalars().all()
    return [r.strip().lower() for r in rows if r and r.strip()]


def _company_is_blocked(metadata_json: Optional[dict], blocklist: list[str]) -> bool:
    """Case-insensitive substring match of metadata.company against blocklist.

    Implemented in Python because metadata_json's company field lives
    inside a JSON column and SQLite/Postgres disagree on the path
    operator — substring match in the result loop stays portable and
    keeps the hot path readable. Page sizes are bounded (300 by
    default) so the linear scan is cheap.
    """
    if not blocklist:
        return False
    company = (metadata_json or {}).get("company")
    if not company:
        return False
    c = str(company).lower()
    return any(term in c for term in blocklist)


def _recency_pct(posted_at: Optional[datetime], now: datetime) -> float:
    """0-100. 100 today, 0 at 30+ days. NULL posted_at -> 0 (neutral floor)."""
    if posted_at is None:
        return 0.0
    age_days = (now - posted_at).total_seconds() / 86_400.0
    if age_days <= 0:
        return 100.0
    if age_days >= RETENTION_DAYS:
        return 0.0
    return (1.0 - age_days / RETENTION_DAYS) * 100.0


def _row_to_dict(item: Item, score: Score, tracking: Optional[Tracking],
                 blended: float, recency_pct: float) -> dict[str, Any]:
    meta = item.metadata_json or {}
    breakdown = score.score_breakdown_json or {}
    tracking_status = None
    if tracking is not None and tracking.status is not None:
        tracking_status = (
            tracking.status.value
            if hasattr(tracking.status, "value")
            else str(tracking.status)
        )
    return {
        "item_id": item.id,
        "title": item.title,
        "company": meta.get("company"),
        "url": item.url,
        "location": meta.get("location_normalized") or meta.get("location"),
        "salary_text": meta.get("salary_text") or meta.get("salary"),
        "posted_at": item.posted_at,
        "score": float(score.score) if score.score is not None else 0.0,
        "blended_score": blended,
        "recency_score": recency_pct,
        "matched_skills": breakdown.get("matched_skills") or [],
        "seniority_signal": breakdown.get("seniority_signal"),
        "salary_match": breakdown.get("salary_match"),
        "dealbreakers": breakdown.get("dealbreakers") or [],
        # Surfaced so the Queue tab can filter by the donut's category.
        # Falls back to None when the score predates the breakdown
        # field; the tab maps None to the "Other" bucket via
        # ``_family_label``.
        "title_family_matched": breakdown.get("title_family_matched"),
        "industry_category": item.industry_category,
        "tracking_status": tracking_status,
    }


def _base_query(profile_id: int, active_resume_id: Optional[int], cutoff: datetime):
    """Build the joined items+scores+tracking query with all filters except
    the dealbreaker check (which lives in JSON and is applied in Python).
    """
    resume_cond = (
        Score.resume_id.is_(None) if active_resume_id is None
        else Score.resume_id == active_resume_id
    )
    return (
        select(Item, Score, Tracking)
        .join(Score, Score.item_id == Item.id)
        .outerjoin(
            Tracking,
            and_(
                Tracking.item_id == Item.id,
                Tracking.profile_id == profile_id,
            ),
        )
        .where(
            Score.profile_id == profile_id,
            resume_cond,
            or_(
                Tracking.status.is_(None),
                Tracking.status.notin_(
                    [TrackingStatus.hidden, TrackingStatus.skipped]
                ),
            ),
            or_(Item.posted_at.is_(None), Item.posted_at >= cutoff),
        )
    )


def get_today_queue(
    profile_id: int,
    page: int = 1,
    page_size: int = DEFAULT_PAGE_SIZE,
) -> list[dict[str, Any]]:
    """Return the blended-ranked queue, paginated.

    Page 1 is rows 0..page_size-1. Ranking and dealbreaker filtering
    happen in Python (the breakdown JSON isn't queryable portably on
    SQLite vs Postgres), so this loads all matching rows, ranks, then
    slices the page. For 300-row default page sizes that's fine.
    """
    page = max(1, int(page or 1))
    page_size = max(1, int(page_size or DEFAULT_PAGE_SIZE))
    now = _now_utc_naive()
    cutoff = now - timedelta(days=RETENTION_DAYS)

    with get_session() as session:
        profile = session.get(Profile, profile_id)
        active_resume_id = profile.active_resume_id if profile else None
        blocklist = _load_blocklist_terms(session, profile_id)

        rows = session.execute(
            _base_query(profile_id, active_resume_id, cutoff)
        ).all()

        results: list[dict[str, Any]] = []
        for item, score, tracking in rows:
            breakdown = score.score_breakdown_json or {}
            if breakdown.get("dealbreakers"):
                continue
            if _company_is_blocked(item.metadata_json, blocklist):
                continue
            if not _is_us_or_remote(item.metadata_json):
                continue
            recency_pct = _recency_pct(item.posted_at, now)
            score_val = float(score.score) if score.score is not None else 0.0
            blended = score_val * W_SCORE + recency_pct * W_RECENCY
            results.append(
                _row_to_dict(item, score, tracking, blended, recency_pct)
            )

    results.sort(key=lambda r: r["blended_score"], reverse=True)
    start = (page - 1) * page_size
    return results[start : start + page_size]


def _family_label(slug: str) -> str:
    """Pretty-print a role-family slug for the donut.

    ``operations_specialist`` -> ``Operations Specialist``.
    ``ai_lab_technical_staff`` -> ``AI Lab Technical Staff``.
    """
    if not slug or slug == "default":
        return "Other"
    upper = {"ai", "ml", "qa", "it", "cs", "us"}
    parts = []
    for token in slug.replace("-", "_").split("_"):
        if token.lower() in upper:
            parts.append(token.upper())
        else:
            parts.append(token.capitalize())
    return " ".join(parts)


def get_category_counts(profile_id: int) -> list[dict[str, Any]]:
    """Return ``[{category, count}, ...]`` for the Overview donut.

    Bucketing priority:
      1. ``items.industry_category`` if populated (LLM classifier — not
         live yet but supported for forward-compat).
      2. ``scores.score_breakdown_json.title_family_matched`` — the
         role family our scoring engine recognized in the title.
      3. ``"Other"`` if neither is available or if the family is the
         ``default`` bucket.

    The same dealbreaker + blocklist + recency filters as
    ``get_today_queue`` apply, so the donut counts only items that
    would appear in the queue.
    """
    now = _now_utc_naive()
    cutoff = now - timedelta(days=RETENTION_DAYS)

    with get_session() as session:
        profile = session.get(Profile, profile_id)
        active_resume_id = profile.active_resume_id if profile else None
        blocklist = _load_blocklist_terms(session, profile_id)

        rows = session.execute(
            _base_query(profile_id, active_resume_id, cutoff)
        ).all()

        counts: dict[str, int] = {}
        for item, score, _tracking in rows:
            breakdown = score.score_breakdown_json or {}
            if breakdown.get("dealbreakers"):
                continue
            if _company_is_blocked(item.metadata_json, blocklist):
                continue
            if not _is_us_or_remote(item.metadata_json):
                continue
            cat = item.industry_category
            if not cat:
                cat = _family_label(breakdown.get("title_family_matched") or "")
            cat = cat or "Other"
            counts[cat] = counts.get(cat, 0) + 1

    return [
        {"category": k, "count": v}
        for k, v in sorted(counts.items(), key=lambda kv: kv[1], reverse=True)
    ]


_PIPELINE_OPEN_STATUSES = {
    TrackingStatus.opened,
    TrackingStatus.recruiter_screen,
    TrackingStatus.assessment,
    TrackingStatus.interview,
}


def get_pipeline_counts(profile_id: int) -> dict[str, int]:
    """Top-bar metrics for the Overview tab.

    Returns::

        {
            "high_fit": <queue rows with score >= 75>,
            "applied_this_week": <tracking rows applied within 7 days>,
            "open_in_pipeline": <tracking rows in active intermediate statuses>,
        }

    ``high_fit`` is computed against the same filtered queue as
    ``get_today_queue`` so the metric matches what the user sees.
    The two tracking counts ignore queue filters (they're about the
    pipeline, not the queue).
    """
    now = _now_utc_naive()
    cutoff = now - timedelta(days=RETENTION_DAYS)
    week_ago = now - timedelta(days=7)

    with get_session() as session:
        profile = session.get(Profile, profile_id)
        active_resume_id = profile.active_resume_id if profile else None
        blocklist = _load_blocklist_terms(session, profile_id)

        rows = session.execute(
            _base_query(profile_id, active_resume_id, cutoff)
        ).all()

        high_fit = 0
        for item, score, _tracking in rows:
            breakdown = score.score_breakdown_json or {}
            if breakdown.get("dealbreakers"):
                continue
            if _company_is_blocked(item.metadata_json, blocklist):
                continue
            if not _is_us_or_remote(item.metadata_json):
                continue
            if (score.score or 0) >= 75:
                high_fit += 1

        applied_this_week = session.execute(
            select(Tracking).where(
                Tracking.profile_id == profile_id,
                Tracking.status == TrackingStatus.applied,
                Tracking.applied_at.is_not(None),
                Tracking.applied_at >= week_ago,
            )
        ).scalars().all()

        open_pipeline = session.execute(
            select(Tracking).where(
                Tracking.profile_id == profile_id,
                Tracking.status.in_(list(_PIPELINE_OPEN_STATUSES)),
            )
        ).scalars().all()

    return {
        "high_fit": high_fit,
        "applied_this_week": len(applied_this_week),
        "open_in_pipeline": len(open_pipeline),
    }


def get_queue_total(profile_id: int) -> int:
    """Count of items that would appear in the queue across all pages."""
    now = _now_utc_naive()
    cutoff = now - timedelta(days=RETENTION_DAYS)

    with get_session() as session:
        profile = session.get(Profile, profile_id)
        active_resume_id = profile.active_resume_id if profile else None
        blocklist = _load_blocklist_terms(session, profile_id)

        rows = session.execute(
            _base_query(profile_id, active_resume_id, cutoff)
        ).all()
        total = 0
        for item, score, _tracking in rows:
            breakdown = score.score_breakdown_json or {}
            if breakdown.get("dealbreakers"):
                continue
            if _company_is_blocked(item.metadata_json, blocklist):
                continue
            if not _is_us_or_remote(item.metadata_json):
                continue
            total += 1
        return total
