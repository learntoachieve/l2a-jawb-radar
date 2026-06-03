"""Iterate items in the DB to score and to extract keywords in bulk."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select

from db.database import get_session
from db.models import Criterion, Item, KeywordExtract, Profile, Score

from .jd_extractor import extract_keywords
from .scorer import score_item_raw, upsert_score


def _now_utc_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _bucket(score: float) -> str:
    if score < 25:
        return "0-25"
    if score < 50:
        return "25-50"
    if score < 75:
        return "50-75"
    return "75-100"


def score_all_items(profile_name: str, force: bool = False) -> dict:
    """Single-pass scoring against a named profile.

    Phase 4.7: v2 ``score_item_raw`` emits a 0-100 score directly, so
    the second-pass dataset-relative normalisation that v1 relied on is
    no longer needed. The Score row's ``score`` field equals
    ``raw_score`` for every item — both columns kept in sync for
    backwards-compatible downstream consumers (the dashboard reads
    ``score``; older queries may inspect ``raw_score``).
    """
    summary = {
        "total_items": 0,
        "scored": 0,
        "skipped": 0,
        "errors": 0,
        "score_distribution": {"0-25": 0, "25-50": 0, "50-75": 0, "75-100": 0},
    }

    with get_session() as session:
        profile = session.execute(
            select(Profile).where(Profile.name == profile_name)
        ).scalar_one_or_none()
        if profile is None:
            raise RuntimeError(f"Profile not found: {profile_name!r}")

        criteria = (
            session.execute(
                select(Criterion).where(Criterion.profile_id == profile.id)
            )
            .scalars()
            .all()
        )

        items = session.execute(select(Item)).scalars().all()
        summary["total_items"] = len(items)

        existing_scores = {
            s.item_id: s
            for s in session.execute(
                select(Score).where(Score.profile_id == profile.id)
            )
            .scalars()
            .all()
        }

        for item in items:
            try:
                if not force:
                    cached = existing_scores.get(item.id)
                    if (
                        cached is not None
                        and cached.raw_score is not None
                        and profile.parsed_at is not None
                        and cached.computed_at >= profile.parsed_at
                    ):
                        summary["skipped"] += 1
                        summary["score_distribution"][_bucket(cached.score)] += 1
                        continue

                raw, matched = score_item_raw(
                    item, profile, session, criteria=criteria
                )
                upsert_score(item.id, profile.id, raw, raw, matched, session)
                summary["scored"] += 1
                summary["score_distribution"][_bucket(raw)] += 1
            except Exception as exc:
                summary["errors"] += 1
                print(f"[scorer] error on item {item.id}: {exc}")

        session.commit()

    return summary


def extract_all_keywords(force: bool = False) -> dict:
    summary = {"total": 0, "extracted": 0, "skipped": 0, "errors": 0}

    with get_session() as session:
        items = session.execute(select(Item)).scalars().all()
        summary["total"] = len(items)

        existing_ids = set(
            session.execute(select(KeywordExtract.item_id)).scalars().all()
        )

        for item in items:
            try:
                if not force and item.id in existing_ids:
                    summary["skipped"] += 1
                    continue

                keywords = extract_keywords(item)

                row = session.execute(
                    select(KeywordExtract).where(KeywordExtract.item_id == item.id)
                ).scalar_one_or_none()
                if row is None:
                    session.add(
                        KeywordExtract(
                            item_id=item.id,
                            keywords_json=keywords,
                            extracted_at=_now_utc_naive(),
                        )
                    )
                else:
                    row.keywords_json = keywords
                    row.extracted_at = _now_utc_naive()

                summary["extracted"] += 1
            except Exception as exc:
                summary["errors"] += 1
                print(f"[jd_extractor] error on item {item.id}: {exc}")

        session.commit()

    return summary
