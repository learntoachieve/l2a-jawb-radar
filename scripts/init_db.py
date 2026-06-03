"""
scripts/init_db.py
Initializes the database and seeds default users + sources.
Run once before first use: python scripts/init_db.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db.database import init_db, get_session
from db.models import User, Source


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


def main() -> None:
    print("Initializing database...")
    init_db()
    print("  ✓ Tables created")

    with get_session() as session:
        # Seed users
        for u in SEED_USERS:
            from sqlalchemy import select
            existing = session.execute(
                select(User).where(User.username == u["username"])
            ).scalar_one_or_none()
            if existing is None:
                session.add(User(username=u["username"], display_name=u["display_name"]))
                print(f"  ✓ User created: {u['username']}")
            else:
                print(f"  – User exists: {u['username']}")

        # Seed sources
        for s in SEED_SOURCES:
            from sqlalchemy import select
            existing = session.execute(
                select(Source).where(Source.name == s["name"])
            ).scalar_one_or_none()
            if existing is None:
                session.add(Source(**s))
                print(f"  ✓ Source created: {s['name']}")
            else:
                print(f"  – Source exists: {s['name']}")

    print("\nDone. Run `streamlit run dashboard/app.py` to launch the dashboard.")


if __name__ == "__main__":
    main()
