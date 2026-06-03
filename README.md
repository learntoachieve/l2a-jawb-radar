# L2A Jawb Radar 🎯

**A full-loop job search pipeline tool by Learn to Achieve.**

Discover → Collect → Score → Queue → Apply → Track

---

## What it does

- Scrapes 8+ job sources daily (Adzuna, Greenhouse, Lever, Ashby, Himalayas, RemoteOK, Remotive, WeWorkRemotely)
- Scores every job against your personal resume-driven profile using a 4-component match engine
- Shows today's ranked queue in a Streamlit dashboard with a category donut chart, map view, and pipeline kanban
- Tracks your full application lifecycle from "interested" to "offer/rejected"
- Supports multiple users with isolated profiles

## Quick Start

```bash
# 1. Clone and set up environment
git clone https://github.com/learntoachieve/l2a-jawb-radar
cd l2a-jawb-radar
python -m venv .venv
source .venv/bin/activate       # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# 2. Configure
cp .env.example .env
# Edit .env — add your ADZUNA_APP_ID/KEY and ANTHROPIC_API_KEY

# 3. Initialize database + seed users/sources
python scripts/init_db.py

# 4. Upload your resume and create your profile via the dashboard
streamlit run dashboard/app.py

# 5. Run a manual scrape
python scripts/cron_run.py
```

## Project Structure

```
l2a-jawb-radar/
├── config/          YAML: skills taxonomy, role families, source quality
├── db/              SQLAlchemy models + session factory
├── scrapers/        8 source scrapers (Adzuna, Greenhouse, Lever, Ashby, ...)
├── scoring/         v2 match engine (role/skill/family/keyword sub-scores)
├── pipeline/        Application state machine + tracking
├── profiles/        Resume parser + profile onboarding
├── dashboard/       Streamlit app (Overview, Queue, Map, Pipeline, Profile tabs)
├── scripts/         init_db, cron_run, backfill utilities
└── tests/           pytest suite
```

## Architecture

See [docs/architecture_spec.md](docs/architecture_spec.md) for the full design spec.

---

*Built by Venura Wijenayake (Loris) and Robert for Learn to Achieve, a California 501(c)(3) civic-tech nonprofit.*
