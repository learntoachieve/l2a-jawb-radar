"""
scripts/init_db.py
Initializes the database, ensures data directories exist, and seeds
default users + scraper sources.

Run once before first use: python scripts/init_db.py
Safe to re-run; existing rows are left untouched.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sqlalchemy import inspect, select

from db.database import get_engine, get_session, init_db
from db.models import Source, User


SEED_USERS = [
    {"username": "loris",  "display_name": "Loris (Venura)"},
    {"username": "robert", "display_name": "Robert"},
]

SEED_SOURCES = [
    {"name": "adzuna",         "type": "api",  "url": "https://api.adzuna.com/v1/api/jobs"},
    {"name": "greenhouse",     "type": "ats",  "url": "https://boards.greenhouse.io"},
    {"name": "lever",          "type": "ats",  "url": "https://jobs.lever.co"},
    {"name": "ashby",          "type": "ats",  "url": "https://jobs.ashbyhq.com"},
    {"name": "himalayas",      "type": "api",  "url": "https://himalayas.app/jobs/api"},
    {"name": "remoteok",       "type": "rss",  "url": "https://remoteok.com/remote-jobs.json"},
    {"name": "remotive",       "type": "rss",  "url": "https://remotive.com/api/remote-jobs"},
    {"name": "weworkremotely", "type": "rss",  "url": "https://weworkremotely.com/categories/remote-programming-jobs.rss"},
]

DATA_DIRS = [
    ROOT / "data",
    ROOT / "data" / "resumes",
]


def ensure_data_dirs() -> list[Path]:
    created: list[Path] = []
    for d in DATA_DIRS:
        if not d.exists():
            d.mkdir(parents=True, exist_ok=True)
            created.append(d)
    return created


def main() -> None:
    print("=" * 60)
    print("L2A Jawb Radar — DB init")
    print("=" * 60)

    print("\n[1/3] Ensuring data directories...")
    created_dirs = ensure_data_dirs()
    for d in DATA_DIRS:
        marker = "created" if d in created_dirs else "exists"
        print(f"  [{marker:>7}] {d.relative_to(ROOT)}")

    print("\n[2/3] Creating tables...")
    init_db()
    tables = sorted(inspect(get_engine()).get_table_names())
    for t in tables:
        print(f"  [   ok  ] {t}")

    print("\n[3/3] Seeding users and sources...")
    created_users: list[str] = []
    created_sources: list[str] = []
    with get_session() as session:
        for u in SEED_USERS:
            existing = session.execute(
                select(User).where(User.username == u["username"])
            ).scalar_one_or_none()
            if existing is None:
                session.add(User(username=u["username"], display_name=u["display_name"]))
                created_users.append(u["username"])
                print(f"  [  user ] created: {u['username']}")
            else:
                print(f"  [  user ] exists:  {u['username']}")

        for s in SEED_SOURCES:
            existing = session.execute(
                select(Source).where(Source.name == s["name"])
            ).scalar_one_or_none()
            if existing is None:
                session.add(Source(**s))
                created_sources.append(s["name"])
                print(f"  [ source] created: {s['name']}")
            else:
                print(f"  [ source] exists:  {s['name']}")

    print("\n" + "-" * 60)
    print("Summary")
    print("-" * 60)
    print(f"  Tables present:    {len(tables)}")
    print(f"  Users created:     {len(created_users)} ({', '.join(created_users) or 'none'})")
    print(f"  Sources created:   {len(created_sources)} ({', '.join(created_sources) or 'none'})")
    print(f"  Data dirs created: {len(created_dirs)}")
    print("\nDone. Launch the dashboard with:")
    print("  streamlit run dashboard/app.py")


if __name__ == "__main__":
    main()
