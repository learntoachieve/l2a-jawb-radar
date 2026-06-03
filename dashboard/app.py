"""
dashboard/app.py

Streamlit entry point. Run with:
    streamlit run dashboard/app.py

Sidebar picks the active user (loris / robert). Tabs expose Profile
(implemented) plus Queue, Map, and Pipeline as stubs that will be
filled in by later phases.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Allow running `streamlit run dashboard/app.py` from the repo root.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import streamlit as st

from profiles import profile_manager as pm
from dashboard.tabs import profile as profile_tab


st.set_page_config(
    page_title="L2A Jawb Radar",
    layout="wide",
    initial_sidebar_state="expanded",
)


def _select_user() -> int | None:
    users = pm.list_users()
    if not users:
        st.sidebar.error(
            "No users in the database.\n\n"
            "Run `python scripts/init_db.py` from the repo root, then reload."
        )
        return None

    options = {u.username: u for u in users}
    usernames = list(options.keys())

    stored = st.session_state.get("active_username")
    default_idx = usernames.index(stored) if stored in options else 0

    selected = st.sidebar.selectbox(
        "User",
        options=usernames,
        format_func=lambda u: options[u].display_name,
        index=default_idx,
        key="active_username",
    )
    user = options[selected]
    st.session_state["active_user_id"] = user.id
    st.sidebar.caption(f"User #{user.id} · @{user.username}")
    return user.id


def main() -> None:
    st.sidebar.title("L2A Jawb Radar")
    user_id = _select_user()
    st.sidebar.divider()
    st.sidebar.caption(
        "Phase 2: profile onboarding\n\n"
        "Queue, Map, and Pipeline tabs land in later phases."
    )

    tab_profile, tab_queue, tab_map, tab_pipeline = st.tabs(
        ["Profile", "Queue", "Map", "Pipeline"]
    )

    with tab_profile:
        if user_id is None:
            st.info("Seed users first to use this tab.")
        else:
            profile_tab.render(user_id)

    with tab_queue:
        st.header("Queue")
        st.info("Scoring queue lands with the scraper wiring in Phase 3.")

    with tab_map:
        st.header("Map")
        st.info("Geocoded job map lands once items have lat/lng.")

    with tab_pipeline:
        st.header("Pipeline")
        st.info("Application tracking pipeline lands after the queue is in.")


main()
