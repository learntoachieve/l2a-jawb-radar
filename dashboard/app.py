"""
dashboard/app.py

Streamlit entry point. Run with:
    streamlit run dashboard/app.py

Sidebar picks the active user (loris / robert). Tabs are ordered to
match the daily workflow:

    Overview | Queue | Map | Pipeline | Profile

Profile lives last because users only touch it during onboarding or
when swapping resumes. Overview and Queue are the surfaces they use
every day.

``active_user_id`` and ``active_profile_id`` are stashed in
``st.session_state`` so tab switches and donut-driven filters survive
reruns.
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
from dashboard.tabs import overview as overview_tab
from dashboard.tabs import profile as profile_tab
from dashboard.tabs import queue as queue_tab


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


def _ensure_profile(user_id: int) -> int | None:
    """Resolve or create the user's profile and stash its id in session state."""
    user = pm.get_user(user_id)
    name = user.display_name if user else f"Profile {user_id}"
    profile = pm.get_or_create_profile(user_id, name)
    st.session_state["active_profile_id"] = profile.id
    return profile.id


def main() -> None:
    st.sidebar.title("L2A Jawb Radar")
    user_id = _select_user()
    profile_id: int | None = None
    if user_id is not None:
        profile_id = _ensure_profile(user_id)

    st.sidebar.divider()
    st.sidebar.caption(
        "Phase 4: Overview + Queue.\n\n"
        "Map and Pipeline tabs are still stubs."
    )

    tab_overview, tab_queue, tab_map, tab_pipeline, tab_profile = st.tabs(
        ["Overview", "Queue", "Map", "Pipeline", "Profile"]
    )

    with tab_overview:
        if user_id is None or profile_id is None:
            st.info("Seed users first to use this tab.")
        else:
            overview_tab.render(user_id=user_id, profile_id=profile_id)

    with tab_queue:
        if user_id is None or profile_id is None:
            st.info("Seed users first to use this tab.")
        else:
            queue_tab.render(user_id=user_id, profile_id=profile_id)

    with tab_map:
        st.header("Map")
        st.info("Geocoded job map coming soon (Phase 5).")

    with tab_pipeline:
        st.header("Pipeline")
        st.info("Application tracking pipeline coming soon (Phase 6).")

    with tab_profile:
        if user_id is None:
            st.info("Seed users first to use this tab.")
        else:
            profile_tab.render(user_id)


main()
