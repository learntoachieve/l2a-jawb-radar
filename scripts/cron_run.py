"""
scripts/cron_run.py — nightly orchestrator.

Steps:
  1. Run every enabled scraper (each ``run()`` returns a summary dict).
  2. For every user that owns a profile with an active resume, call
     ``scoring.batch.score_profile()`` so the new items get scored.
  3. Call ``scoring.batch.purge_stale_scores(30)`` to clean rows whose
     items are over 30 days old AND aren't tracked.

Output is ASCII only and prefixed by [scrape] / [score] / [purge] so
it survives Windows cp1252 stdout without PYTHONIOENCODING=utf-8.
Individual scrapers may raise — each one is wrapped so the rest of the
pipeline keeps running.

Run with: python scripts/cron_run.py
"""
from __future__ import annotations

import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sqlalchemy import select

from db.database import get_session
from db.models import Profile, Source, User
from scoring.batch import purge_stale_scores, score_profile


def _import_scrapers() -> list[tuple[str, type]]:
    """Return [(source_name, scraper_class)] for every scraper module.

    Imports are wrapped so a missing optional dependency in one scraper
    doesn't kill the cron. Each scraper is paired with the ``sources.name``
    value seeded by init_db so we can honour the ``enabled`` flag.
    """
    pairs: list[tuple[str, type]] = []
    candidates = [
        ("adzuna",         "scrapers.adzuna",         "AdzunaScraper"),
        ("ashby",          "scrapers.ashby",          "AshbyScraper"),
        ("greenhouse",     "scrapers.greenhouse",     "GreenhouseScraper"),
        ("himalayas",      "scrapers.himalayas",      "HimalayasScraper"),
        ("lever",          "scrapers.lever",          "LeverScraper"),
        ("remoteok",       "scrapers.remoteok",       "RemoteOKScraper"),
        ("remotive",       "scrapers.remotive",       "RemotiveScraper"),
        ("weworkremotely", "scrapers.weworkremotely", "WeWorkRemotelyScraper"),
    ]
    for source_name, mod_path, cls_name in candidates:
        try:
            mod = __import__(mod_path, fromlist=[cls_name])
            cls = getattr(mod, cls_name, None)
            if cls is None:
                print(f"[scrape] WARN: {mod_path} has no {cls_name}")
                continue
            pairs.append((source_name, cls))
        except Exception as exc:
            print(f"[scrape] WARN: failed to import {mod_path}: {exc}")
    return pairs


def _enabled_source_names() -> set[str]:
    with get_session() as session:
        rows = session.execute(
            select(Source.name).where(Source.enabled.is_(True))
        ).scalars().all()
        return {n.lower() for n in rows}


def _profiles_to_score() -> list[tuple[Profile, User]]:
    with get_session() as session:
        rows = session.execute(
            select(Profile, User)
            .join(User, User.id == Profile.user_id)
            .where(Profile.active_resume_id.is_not(None))
        ).all()
        return [(p, u) for (p, u) in rows]


def run_scrapers() -> list[tuple[str, dict]]:
    enabled = _enabled_source_names()
    summaries: list[tuple[str, dict]] = []

    for source_name, scraper_cls in _import_scrapers():
        if source_name.lower() not in enabled:
            print(f"[scrape] {source_name}: skipped (disabled in sources table)")
            continue
        try:
            scraper = scraper_cls()
        except Exception as exc:
            print(f"[scrape] {source_name}: init failed - {exc}")
            summaries.append((source_name, {"new": 0, "duplicates": 0, "errors": 1}))
            continue
        try:
            s = scraper.run()
        except Exception:
            print(f"[scrape] {source_name}: run() raised:")
            traceback.print_exc()
            s = {"new": 0, "duplicates": 0, "errors": 1}
        summaries.append((source_name, s))
        new = s.get("new", 0)
        dup = s.get("duplicates", 0)
        err = s.get("errors", 0)
        p2 = s.get("pass2_fetched", 0)
        p2e = s.get("pass2_empty", 0)
        print(
            f"[scrape] {source_name}: {new} new, {dup} duplicates, "
            f"{err} errors  (pass2: {p2} fetched, {p2e} empty)"
        )
    return summaries


def run_scoring() -> list[tuple[str, str, dict]]:
    out: list[tuple[str, str, dict]] = []
    for profile, user in _profiles_to_score():
        try:
            s = score_profile(profile.id)
        except Exception as exc:
            print(f"[score]  profile {user.username}/{profile.name}: error - {exc}")
            out.append((user.username, profile.name, {"errors": 1}))
            continue
        out.append((user.username, profile.name, s))
        scored = s.get("scored", 0)
        skipped = s.get("skipped", 0)
        errors = s.get("errors", 0)
        print(
            f"[score]  profile {user.username}/{profile.name}: "
            f"{scored} scored, {skipped} skipped, {errors} errors"
        )
    if not out:
        print("[score]  no profiles with an active resume - nothing to score")
    return out


def run_purge() -> int:
    try:
        deleted = purge_stale_scores(days=30)
    except Exception as exc:
        print(f"[purge]  error - {exc}")
        return 0
    print(f"[purge]  deleted {deleted} stale scores")
    return deleted


def main() -> int:
    print("=" * 60)
    print("L2A Jawb Radar - cron run")
    print("=" * 60)

    print("\n[1/3] scrapers")
    print("-" * 60)
    run_scrapers()

    print("\n[2/3] scoring")
    print("-" * 60)
    run_scoring()

    print("\n[3/3] purge")
    print("-" * 60)
    run_purge()

    print("\nDone.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
