"""
dashboard/tabs/queue.py

The daily work surface. Shows the blended-rank queue from
``dashboard.data.get_today_queue`` with filters, pagination, and
per-row + batch tracking actions.

Filters live in-tab (the sidebar already holds the global user
selector). They run as Python post-filters over the full queue,
which is bounded at a few thousand rows.

Tracking writes go through ``write_tracking``, which respects
``ALLOWED_TRANSITIONS`` from ``db.models`` so a stale UI button
can't move a job into an illegal status.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import streamlit as st
from sqlalchemy import select

from dashboard.data import (
    DEFAULT_PAGE_SIZE,
    get_category_counts,
    get_today_queue,
)
from db.database import get_session
from db.models import ALLOWED_TRANSITIONS, Tracking, TrackingStatus


PAGE_SIZE = DEFAULT_PAGE_SIZE
ALL_FETCH_CAP = 10_000  # generous so filter+page covers the full queue
CATEGORY_FILTER_KEY = "queue_category_filter"
SCORE_THRESHOLD_KEY = "queue_score_threshold"
MODALITY_KEY = "queue_modality_filter"
DATE_KEY = "queue_date_filter"
LONGSHOTS_KEY = "queue_show_longshots"
PAGE_KEY = "queue_page"
CHECKED_KEY = "queue_checked"

MODALITY_OPTIONS = ["All", "Remote", "Hybrid", "Onsite"]
DATE_OPTIONS = ["Any", "Last 7 days", "Last 14 days", "Last 30 days"]
DATE_DAYS = {"Any": None, "Last 7 days": 7, "Last 14 days": 14, "Last 30 days": 30}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def render(user_id: int, profile_id: int) -> None:
    st.header("Queue")
    if CHECKED_KEY not in st.session_state:
        st.session_state[CHECKED_KEY] = set()

    rows = _fetch_all(profile_id)
    if not rows:
        st.info(
            "Queue is empty. Run `python scripts/cron_run.py` to populate "
            "items, then `score_profile(profile_id)` to score them."
        )
        return

    filtered = _apply_filters(rows, profile_id)
    _render_filter_panel(rows, profile_id, len(filtered))
    _render_pagination_and_rows(filtered, user_id, profile_id)
    _render_batch_bar(filtered, user_id, profile_id)


# ---------------------------------------------------------------------------
# Data fetch
# ---------------------------------------------------------------------------

def _fetch_all(profile_id: int) -> list[dict[str, Any]]:
    return get_today_queue(profile_id, page=1, page_size=ALL_FETCH_CAP)


# ---------------------------------------------------------------------------
# Filtering (pure Python over the fetched rows)
# ---------------------------------------------------------------------------

def _row_modality(row: dict[str, Any]) -> Optional[str]:
    """Best-effort modality detection from location + body hints stored on the row.

    The scraper stores ``remote_type`` in ``metadata_json`` but that
    isn't surfaced on the queue dict — we infer from the location
    string. "Remote" matches when location contains the word remote.
    """
    loc = (row.get("location") or "").lower()
    if "remote" in loc or "anywhere" in loc:
        return "Remote"
    if "hybrid" in loc:
        return "Hybrid"
    if loc:
        return "Onsite"
    return None


def _row_category_label(row: dict[str, Any]) -> str:
    """Same bucketing rule the donut uses, applied to a queue row.

    Priority: ``industry_category`` (LLM classifier — currently
    universally NULL in this dataset) -> ``title_family_matched``
    via ``_family_label`` -> "Other".
    """
    from dashboard.data import _family_label  # internal but stable

    cat = row.get("industry_category")
    if cat:
        return cat
    return _family_label(row.get("title_family_matched") or "") or "Other"


def _apply_filters(rows: list[dict[str, Any]], profile_id: int) -> list[dict[str, Any]]:
    cats = st.session_state.get(CATEGORY_FILTER_KEY) or []
    threshold = float(st.session_state.get(SCORE_THRESHOLD_KEY) or 0)
    modality = st.session_state.get(MODALITY_KEY) or "All"
    date_choice = st.session_state.get(DATE_KEY) or "Any"
    show_long = bool(st.session_state.get(LONGSHOTS_KEY, True))

    days = DATE_DAYS.get(date_choice)
    cutoff = (
        datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)
        if days is not None else None
    )

    def keep(row: dict[str, Any]) -> bool:
        if cats:
            if _row_category_label(row) not in cats:
                return False
        if row.get("blended_score", 0) < threshold:
            return False
        if not show_long and (row.get("score") or 0) < 25:
            return False
        if modality != "All":
            row_mod = _row_modality(row)
            if row_mod != modality:
                return False
        if cutoff is not None:
            posted = row.get("posted_at")
            if posted is None:
                return False
            if posted < cutoff:
                return False
        return True

    return [r for r in rows if keep(r)]


# ---------------------------------------------------------------------------
# Filter UI
# ---------------------------------------------------------------------------

def _render_filter_panel(
    all_rows: list[dict[str, Any]],
    profile_id: int,
    filtered_count: int,
) -> None:
    cat_rows = get_category_counts(profile_id)
    cat_options = [r["category"] for r in cat_rows]

    with st.expander("Filters", expanded=False):
        c1, c2, c3 = st.columns([3, 2, 2])
        with c1:
            st.multiselect(
                "Categories",
                options=cat_options,
                default=st.session_state.get(CATEGORY_FILTER_KEY) or [],
                key=CATEGORY_FILTER_KEY,
            )
        with c2:
            st.selectbox(
                "Work modality",
                options=MODALITY_OPTIONS,
                index=MODALITY_OPTIONS.index(
                    st.session_state.get(MODALITY_KEY, "All")
                ),
                key=MODALITY_KEY,
            )
        with c3:
            st.selectbox(
                "Posted",
                options=DATE_OPTIONS,
                index=DATE_OPTIONS.index(
                    st.session_state.get(DATE_KEY, "Any")
                ),
                key=DATE_KEY,
            )

        c4, c5 = st.columns([3, 2])
        with c4:
            st.slider(
                "Min blended score",
                min_value=0, max_value=100,
                value=int(st.session_state.get(SCORE_THRESHOLD_KEY, 0)),
                key=SCORE_THRESHOLD_KEY,
            )
        with c5:
            st.checkbox(
                "Show low-fit (< 25 score)",
                value=bool(st.session_state.get(LONGSHOTS_KEY, True)),
                key=LONGSHOTS_KEY,
            )

    st.caption(
        f"Showing **{filtered_count:,}** of **{len(all_rows):,}** scored jobs."
    )


# ---------------------------------------------------------------------------
# Pagination + row rendering
# ---------------------------------------------------------------------------

def _score_color(score: float) -> str:
    if score >= 75:
        return "#1B998B"
    if score >= 50:
        return "#F18F01"
    return "#777777"


def _score_badge(score: float) -> str:
    color = _score_color(score)
    return (
        f"<span style='background:{color};color:#fff;padding:3px 10px;"
        f"border-radius:14px;font-weight:600;font-size:0.85rem;'>"
        f"{score:.0f}</span>"
    )


def _chip(text: str, bg: str = "#EEF2F7", fg: str = "#324a5f") -> str:
    return (
        f"<span style='background:{bg};color:{fg};padding:2px 9px;"
        f"border-radius:10px;font-size:0.78rem;margin-right:5px;"
        f"display:inline-block;margin-bottom:3px;'>{text}</span>"
    )


def _format_posted(dt: Optional[datetime]) -> str:
    if dt is None:
        return "posted date unknown"
    days = (datetime.now(timezone.utc).replace(tzinfo=None) - dt).days
    if days <= 0:
        return "posted today"
    if days == 1:
        return "posted yesterday"
    return f"posted {days}d ago"


def _render_pagination_and_rows(
    filtered: list[dict[str, Any]],
    user_id: int,
    profile_id: int,
) -> None:
    if not filtered:
        st.info("No jobs match the current filters.")
        return

    total = len(filtered)
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    current_page = int(st.session_state.get(PAGE_KEY, 1))
    if current_page > pages:
        current_page = 1
        st.session_state[PAGE_KEY] = 1

    if pages > 1:
        st.selectbox(
            f"Page (1 - {pages})",
            options=list(range(1, pages + 1)),
            index=current_page - 1,
            key=PAGE_KEY,
        )
        current_page = int(st.session_state[PAGE_KEY])

    start = (current_page - 1) * PAGE_SIZE
    end = min(start + PAGE_SIZE, total)
    page_rows = filtered[start:end]
    st.caption(f"Rows {start + 1}–{end} of {total:,}")

    for row in page_rows:
        _render_row(row, user_id, profile_id)


def _render_row(row: dict[str, Any], user_id: int, profile_id: int) -> None:
    item_id = row["item_id"]
    with st.container(border=True):
        cb_col, body_col, badge_col, action_col = st.columns([0.5, 5, 1, 2])

        with cb_col:
            checked = item_id in st.session_state[CHECKED_KEY]
            new_val = st.checkbox(
                "select", value=checked, key=f"chk_{item_id}",
                label_visibility="collapsed",
            )
            if new_val and not checked:
                st.session_state[CHECKED_KEY].add(item_id)
            elif not new_val and checked:
                st.session_state[CHECKED_KEY].discard(item_id)

        with body_col:
            company = row.get("company") or "(unknown)"
            title = row.get("title") or "(no title)"
            st.markdown(
                f"**{company}** &nbsp;·&nbsp; {title}",
                unsafe_allow_html=True,
            )

            sub_parts: list[str] = []
            if row.get("location"):
                sub_parts.append(f"📍 {row['location']}")
            if row.get("salary_text"):
                sub_parts.append(f"💰 {row['salary_text']}")
            sub_parts.append(_format_posted(row.get("posted_at")))
            if row.get("tracking_status"):
                sub_parts.append(f"🏷 {row['tracking_status']}")
            st.caption(" · ".join(sub_parts))

            tags: list[str] = []
            sen = row.get("seniority_signal")
            if sen:
                tags.append(_chip(f"seniority: {sen}", bg="#F0E6F6", fg="#5a3870"))
            sm = row.get("salary_match")
            if sm and sm != "unknown":
                col = "#E7F4EA" if sm == "in_range" else "#FBEAEA"
                fg = "#1d6b3e" if sm == "in_range" else "#8a2727"
                tags.append(_chip(f"salary: {sm}", bg=col, fg=fg))
            for s in (row.get("matched_skills") or [])[:5]:
                tags.append(_chip(s))
            if tags:
                st.markdown("".join(tags), unsafe_allow_html=True)

            if row.get("dealbreakers"):
                st.warning(
                    f"Dealbreakers: {', '.join(row['dealbreakers'])}"
                )

        with badge_col:
            st.markdown(
                _score_badge(row.get("blended_score") or 0.0),
                unsafe_allow_html=True,
            )
            st.caption(f"match {row.get('score', 0):.0f} · rec {row.get('recency_score', 0):.0f}")

        with action_col:
            current_status = row.get("tracking_status")
            _render_row_actions(row, user_id, profile_id, current_status)


def _render_row_actions(
    row: dict[str, Any],
    user_id: int,
    profile_id: int,
    current_status: Optional[str],
) -> None:
    item_id = row["item_id"]
    url = row.get("url")

    if url:
        st.link_button("Open URL", url=url, use_container_width=True)

    # "Open" — record the click as a tracking event
    if _transition_allowed(current_status, TrackingStatus.opened):
        if st.button("Open & Track", key=f"open_{item_id}", use_container_width=True):
            write_tracking(item_id, profile_id, user_id, TrackingStatus.opened)
            st.toast(f"Tracked as opened: {row.get('title','')[:40]}", icon="📂")
            st.rerun()

    btn_cols = st.columns(2)
    with btn_cols[0]:
        if _transition_allowed(current_status, TrackingStatus.skipped):
            if st.button("Skip", key=f"skip_{item_id}", use_container_width=True):
                write_tracking(item_id, profile_id, user_id, TrackingStatus.skipped)
                st.rerun()
    with btn_cols[1]:
        if _transition_allowed(current_status, TrackingStatus.hidden):
            if st.button("Hide", key=f"hide_{item_id}", use_container_width=True):
                write_tracking(item_id, profile_id, user_id, TrackingStatus.hidden)
                st.rerun()


# ---------------------------------------------------------------------------
# Batch action bar
# ---------------------------------------------------------------------------

def _render_batch_bar(
    filtered: list[dict[str, Any]],
    user_id: int,
    profile_id: int,
) -> None:
    checked = st.session_state.get(CHECKED_KEY) or set()
    if not checked:
        return

    visible_ids = {r["item_id"] for r in filtered}
    selected = sorted(checked & visible_ids)
    if not selected:
        return

    st.divider()
    st.markdown(f"**{len(selected)} job(s) selected**")
    bcol1, bcol2, bcol3 = st.columns([2, 2, 6])
    with bcol1:
        if st.button(
            f"Queue {len(selected)} as Interested",
            key="batch_interested",
            type="primary",
            use_container_width=True,
        ):
            for item_id in selected:
                write_tracking(item_id, profile_id, user_id, TrackingStatus.interested)
            # Open the first one's URL in a new tab via link button -
            # Streamlit can't programmatically launch a tab, so we
            # surface the first URL as a link button below.
            st.session_state[CHECKED_KEY] = set()
            st.toast(f"Queued {len(selected)} jobs", icon="✅")
            first = next(
                (r for r in filtered if r["item_id"] == selected[0]),
                None,
            )
            if first and first.get("url"):
                st.session_state["_queue_batch_first_url"] = first["url"]
            st.rerun()
    with bcol2:
        if st.button("Clear selection", key="batch_clear", use_container_width=True):
            st.session_state[CHECKED_KEY] = set()
            st.rerun()

    first_url = st.session_state.pop("_queue_batch_first_url", None)
    if first_url:
        st.link_button(
            "Open first job in browser",
            url=first_url,
            use_container_width=False,
        )


# ---------------------------------------------------------------------------
# Tracking persistence
# ---------------------------------------------------------------------------

def _transition_allowed(
    current_status: Optional[str], new_status: TrackingStatus
) -> bool:
    """No tracking row yet -> any transition is fine.
    Otherwise consult ALLOWED_TRANSITIONS keyed by current status.
    """
    if not current_status:
        return True
    allowed = ALLOWED_TRANSITIONS.get(current_status, frozenset())
    return new_status.value in allowed


def write_tracking(
    item_id: int,
    profile_id: int,
    user_id: int,
    status: TrackingStatus,
) -> bool:
    """Upsert a Tracking row, respecting ALLOWED_TRANSITIONS.

    Returns True on write, False if the transition was illegal and
    silently skipped so a stale rerun doesn't crash the loop.
    """
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with get_session() as session:
        existing = session.execute(
            select(Tracking).where(
                Tracking.item_id == item_id,
                Tracking.profile_id == profile_id,
            )
        ).scalar_one_or_none()

        if existing is None:
            row = Tracking(
                item_id=item_id,
                profile_id=profile_id,
                user_id=user_id,
                status=status,
                last_status_change_at=now,
            )
            if status == TrackingStatus.applied:
                row.applied_at = now
            session.add(row)
            return True

        current_value = (
            existing.status.value
            if hasattr(existing.status, "value")
            else str(existing.status)
        )
        if current_value == status.value:
            return True
        allowed = ALLOWED_TRANSITIONS.get(current_value, frozenset())
        if status.value not in allowed:
            return False

        existing.status = status
        existing.last_status_change_at = now
        if status == TrackingStatus.applied and existing.applied_at is None:
            existing.applied_at = now
        return True
