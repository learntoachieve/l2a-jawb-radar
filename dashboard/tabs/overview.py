"""
dashboard/tabs/overview.py

The first tab — the daily landing surface. Renders:

  1. A four-metric KPI bar (queue size, high-fit count, applications
     this week, jobs in the active interview pipeline).
  2. A Plotly donut chart of the queue grouped by role family.
     Clicking a segment writes ``queue_category_filter`` to session
     state. Streamlit doesn't support programmatic tab switching, so
     the chart caption tells the user to flip to the Queue tab to
     see the filtered list.
  3. The top 5 untouched jobs by blended score, rendered as cards
     with a colour-coded score badge, matched-skill chips, and an
     "Open & Queue" button that marks the job as ``interested`` and
     opens the URL in a new tab.
"""
from __future__ import annotations

from typing import Any

import plotly.graph_objects as go
import streamlit as st

from dashboard.data import (
    get_category_counts,
    get_pipeline_counts,
    get_queue_total,
    get_today_queue,
)
from db.database import get_session
from db.models import Tracking, TrackingStatus


# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------

TOP_PICK_LIMIT = 5
DONUT_SEGMENT_KEY = "queue_category_filter"
DONUT_NAV_HINT_KEY = "_overview_donut_nav_hint"


def render(user_id: int, profile_id: int) -> None:
    st.header("Overview")

    _render_metric_bar(profile_id)
    st.divider()
    _render_donut(profile_id)
    st.divider()
    _render_top_picks(user_id, profile_id)


# ---------------------------------------------------------------------------
# KPI bar
# ---------------------------------------------------------------------------

def _render_metric_bar(profile_id: int) -> None:
    total = get_queue_total(profile_id)
    counts = get_pipeline_counts(profile_id)

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Scoreable today",   total)
    c2.metric("High-fit (>= 75)",  counts.get("high_fit", 0))
    c3.metric("Applied this week", counts.get("applied_this_week", 0))
    c4.metric("Open in pipeline",  counts.get("open_in_pipeline", 0))


# ---------------------------------------------------------------------------
# Donut chart
# ---------------------------------------------------------------------------

# Distinct colours for real role families. Deliberately excludes any
# gray so the muted "Other" bucket stays visually separable from the
# meaningful categories.
_PALETTE = [
    "#2E86AB", "#A23B72", "#F18F01", "#C73E1D", "#3B7A57",
    "#6B4E71", "#8FB339", "#D88C9A", "#1B998B", "#E07A5F",
]
_OTHER_LABEL = "Other"
_OTHER_COLOR = "#888888"  # muted gray — de-emphasises the catch-all bucket
_LABEL_MIN_PCT = 2.0      # suppress inline labels on slices smaller than this


def _ordered_donut_data(
    rows: list[dict[str, Any]]
) -> tuple[list[str], list[int], list[str]]:
    """Return (labels, values, colors) with ``Other`` forced last + gray.

    Real categories are sorted by count descending and coloured from the
    fixed palette; ``Other`` is always appended last in muted gray so the
    90%-ish catch-all never visually dominates the meaningful slices.
    """
    real = sorted(
        (r for r in rows if r["category"] != _OTHER_LABEL),
        key=lambda r: r["count"],
        reverse=True,
    )
    other = [r for r in rows if r["category"] == _OTHER_LABEL]

    labels: list[str] = []
    values: list[int] = []
    colors: list[str] = []
    for i, r in enumerate(real):
        labels.append(r["category"])
        values.append(r["count"])
        colors.append(_PALETTE[i % len(_PALETTE)])
    for r in other:
        labels.append(r["category"])
        values.append(r["count"])
        colors.append(_OTHER_COLOR)
    return labels, values, colors


def _donut_legend_html(
    labels: list[str], values: list[int], colors: list[str]
) -> str:
    """Inline swatch + category → count legend rendered below the chart so
    the small slices (whose inline labels are suppressed) stay readable.
    """
    chips = []
    for lbl, val, color in zip(labels, values, colors):
        chips.append(
            "<span style='display:inline-block;margin:2px 14px 2px 0;"
            "font-size:0.82rem;white-space:nowrap;'>"
            f"<span style='display:inline-block;width:10px;height:10px;"
            f"border-radius:2px;background:{color};margin-right:6px;"
            "vertical-align:middle;'></span>"
            f"{lbl} <b>{val}</b></span>"
        )
    return "<div style='line-height:1.9;'>" + "".join(chips) + "</div>"


def _render_donut(profile_id: int) -> None:
    rows = get_category_counts(profile_id)
    if not rows:
        st.info(
            "No scoreable jobs in the queue yet. "
            "Run `python scripts/cron_run.py` to populate items, then "
            "score them with `score_profile(profile_id)`."
        )
        return

    labels, values, colors = _ordered_donut_data(rows)
    total = sum(values) or 1
    # Suppress inline labels on slices < _LABEL_MIN_PCT so small families
    # don't crowd / clip against each other; the legend below keeps them
    # readable. Full detail still shows on hover.
    slice_text = [
        lbl if (val / total * 100.0) >= _LABEL_MIN_PCT else ""
        for lbl, val in zip(labels, values)
    ]

    fig = go.Figure(
        data=[
            go.Pie(
                labels=labels,
                values=values,
                sort=False,  # keep our ordering (Other stays last)
                hole=0.55,
                marker=dict(colors=colors, line=dict(color="#FFFFFF", width=1)),
                text=slice_text,
                textinfo="text",
                textposition="outside",
                hovertemplate="<b>%{label}</b><br>%{value} jobs (%{percent})<extra></extra>",
            )
        ]
    )
    fig.update_layout(
        title="Today's job landscape",
        showlegend=False,
        height=400,
        margin=dict(t=40, b=80, l=20, r=20),
    )

    # Streamlit's plotly_chart supports point selection — clicking a
    # slice surfaces it via the returned event. We use that to set
    # the queue category filter. Tab switching isn't programmable in
    # st.tabs, so we drop a hint into session state and surface it
    # as a caption below the chart.
    event = st.plotly_chart(
        fig,
        use_container_width=True,
        on_select="rerun",
        selection_mode="points",
        key="overview_donut",
    )

    # Category -> count legend below the chart. Inline slice labels are
    # suppressed for small slices, so this keeps every family readable.
    st.markdown(
        _donut_legend_html(labels, values, colors),
        unsafe_allow_html=True,
    )

    selection = (event or {}).get("selection") or {}
    points = selection.get("points") or []
    if points:
        chosen = points[0].get("label")
        if chosen:
            current = st.session_state.get(DONUT_SEGMENT_KEY) or []
            if chosen not in current:
                st.session_state[DONUT_SEGMENT_KEY] = [chosen]
                st.session_state[DONUT_NAV_HINT_KEY] = chosen

    nav_hint = st.session_state.get(DONUT_NAV_HINT_KEY)
    if nav_hint:
        st.success(
            f"Filter set to **{nav_hint}** — switch to the Queue tab "
            f"to see the {len([nav_hint])} matching family."
        )
        if st.button("Clear filter", key="overview_clear_filter"):
            st.session_state.pop(DONUT_SEGMENT_KEY, None)
            st.session_state.pop(DONUT_NAV_HINT_KEY, None)
            st.rerun()


# ---------------------------------------------------------------------------
# Top picks
# ---------------------------------------------------------------------------

def _score_color(score: float) -> str:
    if score >= 75:
        return "#1B998B"  # green
    if score >= 50:
        return "#F18F01"  # orange
    return "#777777"      # gray


def _render_score_badge(score: float) -> str:
    color = _score_color(score)
    return (
        f"<span style='background:{color};color:#fff;padding:3px 10px;"
        f"border-radius:14px;font-weight:600;font-size:0.85rem;'>"
        f"{score:.0f}</span>"
    )


def _render_chip(text: str, bg: str = "#EEF2F7", fg: str = "#324a5f") -> str:
    return (
        f"<span style='background:{bg};color:{fg};padding:2px 9px;"
        f"border-radius:10px;font-size:0.78rem;margin-right:5px;"
        f"display:inline-block;margin-bottom:3px;'>{text}</span>"
    )


def _mark_interested(item_id: int, profile_id: int, user_id: int) -> None:
    """Idempotent: create or upgrade a tracking row to ``interested``."""
    from sqlalchemy import select

    with get_session() as session:
        existing = session.execute(
            select(Tracking).where(
                Tracking.item_id == item_id,
                Tracking.profile_id == profile_id,
            )
        ).scalar_one_or_none()
        if existing is None:
            session.add(Tracking(
                item_id=item_id,
                profile_id=profile_id,
                user_id=user_id,
                status=TrackingStatus.interested,
            ))
        elif existing.status == TrackingStatus.new:
            existing.status = TrackingStatus.interested


def _render_top_picks(user_id: int, profile_id: int) -> None:
    st.subheader("Today's top picks")

    # Pull a generous slice so we can skip rows that already have
    # tracking and still land on TOP_PICK_LIMIT untouched picks.
    candidates = get_today_queue(profile_id, page=1, page_size=50)
    fresh = [r for r in candidates if not r.get("tracking_status")]
    picks = fresh[:TOP_PICK_LIMIT]

    if not picks:
        st.caption("No untouched jobs in the queue right now. Nice problem to have.")
        return

    for row in picks:
        with st.container(border=True):
            head_left, head_right = st.columns([5, 1])
            with head_left:
                company = row.get("company") or "(unknown company)"
                title = row.get("title") or "(no title)"
                st.markdown(
                    f"**{company}** &nbsp; · &nbsp; {title}",
                    unsafe_allow_html=True,
                )
            with head_right:
                st.markdown(
                    _render_score_badge(row.get("blended_score") or 0.0),
                    unsafe_allow_html=True,
                )

            # Matched-skill chips
            skills = row.get("matched_skills") or []
            if skills:
                chips_html = "".join(_render_chip(s) for s in skills[:4])
                st.markdown(chips_html, unsafe_allow_html=True)

            # Location / modality / seniority line
            meta_parts: list[str] = []
            if row.get("location"):
                meta_parts.append(f"📍 {row['location']}")
            if row.get("seniority_signal"):
                meta_parts.append(f"🪜 {row['seniority_signal']}")
            if row.get("salary_match") and row["salary_match"] != "unknown":
                meta_parts.append(f"💵 {row['salary_match']}")
            if meta_parts:
                st.caption(" · ".join(meta_parts))

            # Action row
            act_a, act_b, _ = st.columns([1, 1, 4])
            with act_a:
                if row.get("url"):
                    st.link_button(
                        "Open URL",
                        url=row["url"],
                        use_container_width=True,
                    )
            with act_b:
                if st.button(
                    "Mark interested",
                    key=f"overview_pick_{row['item_id']}",
                    use_container_width=True,
                ):
                    _mark_interested(row["item_id"], profile_id, user_id)
                    st.toast(f"Queued: {title[:40]}", icon="✅")
                    st.rerun()
