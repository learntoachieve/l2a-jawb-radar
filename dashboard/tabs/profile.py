"""
dashboard/tabs/profile.py

Streamlit Profile tab: upload resumes, switch the active one, review
parsed criteria, and edit manual preferences (target roles, locations,
seniority, work modality, salary, company blocklist).

Schema model (matches db/models.py):
  User --1:1--> Profile --1:N--> Resume
  Profile.active_resume_id  -> the resume whose criteria are in play
  Criterion.resume_id       -> NULL for manual, else the source resume
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import streamlit as st

from profiles import profile_manager as pm
from scoring.resume_parser import extract_text_from_pdf


RESUME_DIR = Path("data/resumes")
MAX_DISPLAY_ITEMS = 10

SENIORITY_OPTIONS = [
    ("Any",    "any"),
    ("Entry",  "entry"),
    ("Mid",    "mid"),
    ("Senior", "senior"),
]
WORK_MODALITY_OPTIONS = [
    ("Any",    "any"),
    ("Remote", "remote"),
    ("Hybrid", "hybrid"),
    ("Onsite", "onsite"),
]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def render(user_id: int) -> None:
    st.header("Profile")

    user = pm.get_user(user_id)
    default_name = user.display_name if user is not None else f"Profile {user_id}"
    profile = pm.get_or_create_profile(user_id, default_name)

    resumes = pm.list_resumes(profile.id)

    _render_resume_list(profile.id, profile.active_resume_id, resumes)
    _render_upload_form(profile.id, has_resumes=bool(resumes))

    summary = pm.get_profile_summary(profile.id)

    if summary.get("active_resume") is None:
        st.info(
            "Upload a resume above (or activate one) to manage skills and "
            "preferences against a parsed criteria set."
        )
        # Still let preferences/lists be edited even without a resume.
    else:
        active = summary["active_resume"]
        st.divider()
        st.subheader(f"Active resume: {active['label']}")

    _render_criteria_section(profile.id, summary.get("criteria", []))
    st.divider()
    _render_preferences_section(summary["profile"])
    st.divider()
    _render_target_roles_section(profile.id, summary.get("target_roles", []))
    st.divider()
    _render_locations_section(profile.id, summary.get("locations", []))
    st.divider()
    _render_blocklist_section(profile.id, summary.get("blocklist", []))


# ---------------------------------------------------------------------------
# Resume manager
# ---------------------------------------------------------------------------

def _format_date(dt) -> str:
    if dt is None:
        return "—"
    if isinstance(dt, datetime):
        return dt.strftime("%Y-%m-%d %H:%M")
    return str(dt)


def _resume_skill_count(profile_id: int, resume_id: int) -> dict[str, int]:
    """Count criteria for a specific Resume row (resume-sourced only).

    We hit get_active_criteria for the active resume, but for inactive
    resumes we need the raw count. Cheapest path: use the manager's
    helper to fetch all manual+active criteria then filter; for an
    arbitrary resume we'd ideally have a list_resume_criteria(), but
    summary already gave us the active count and per-card counts are
    only shown for non-active cards. Implement here as a lean query.
    """
    from sqlalchemy import select
    from db.database import get_session
    from db.models import Criterion

    with get_session() as session:
        rows = session.execute(
            select(Criterion.kind)
            .where(
                Criterion.profile_id == profile_id,
                Criterion.resume_id == resume_id,
            )
        ).scalars().all()
    counts = {"skill": 0, "role": 0, "keyword": 0}
    for kind in rows:
        counts[kind] = counts.get(kind, 0) + 1
    return counts


def _render_resume_list(profile_id: int, active_resume_id, resumes: list) -> None:
    st.subheader("Resumes")
    if not resumes:
        st.caption("No resumes yet. Upload one below to get started.")
        return

    for r in resumes:
        counts = _resume_skill_count(profile_id, r.id)
        is_active = (active_resume_id == r.id)
        with st.container(border=True):
            top, action = st.columns([4, 1])
            with top:
                label_md = f"**{r.label}**"
                if is_active:
                    label_md += " &nbsp;:green-background[ACTIVE]"
                st.markdown(label_md)
                st.caption(f"File: `{r.filename or '(none)'}`")
                st.caption(
                    f"Uploaded: {_format_date(r.parsed_at or r.created_at)}  •  "
                    f"Skills: {counts['skill']}  •  "
                    f"Roles: {counts['role']}  •  "
                    f"Keywords: {counts['keyword']}"
                )
            with action:
                if is_active:
                    st.markdown(":green[Active]")
                else:
                    if st.button("Set Active", key=f"activate_{r.id}", use_container_width=True):
                        pm.set_active_resume(profile_id, r.id)
                        st.rerun()
                if st.button("Delete", key=f"delete_{r.id}", use_container_width=True):
                    pm.delete_resume(profile_id, r.id)
                    st.rerun()


def _render_upload_form(profile_id: int, has_resumes: bool) -> None:
    with st.expander("Upload new resume", expanded=not has_resumes):
        with st.form("upload_resume", clear_on_submit=True):
            label = st.text_input(
                "Label",
                placeholder="e.g. Data Analytics, Help Desk, AWS / Cloud",
            )
            uploaded = st.file_uploader("Resume PDF", type=["pdf"])
            submitted = st.form_submit_button("Upload & Parse")

            if not submitted:
                return

            if not label.strip():
                st.error("Label is required.")
                return
            if uploaded is None:
                st.error("Please select a PDF file.")
                return

            RESUME_DIR.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            safe_name = uploaded.name.replace(" ", "_")
            save_path = RESUME_DIR / f"p{profile_id}_{ts}_{safe_name}"
            save_path.write_bytes(uploaded.getvalue())

            try:
                raw_text = extract_text_from_pdf(str(save_path))
            except Exception as exc:
                st.error(f"Failed to extract text from PDF: {exc}")
                return

            if not raw_text.strip():
                st.warning(
                    "Parsed PDF contained no extractable text — resume saved "
                    "but no criteria were generated."
                )

            resume = pm.upload_resume(
                profile_id=profile_id,
                label=label.strip(),
                filename=save_path.name,
                raw_text=raw_text,
            )
            st.success(f"Uploaded resume '{resume.label}'.")
            st.rerun()


# ---------------------------------------------------------------------------
# Criteria
# ---------------------------------------------------------------------------

def _render_kind_block(profile_id: int, kind: str, label: str, items: list[dict]) -> None:
    st.markdown(f"**{label}** ({len(items)})")
    if not items:
        st.caption(f"No {label.lower()} yet.")
    else:
        visible = items[:MAX_DISPLAY_ITEMS]
        hidden = items[MAX_DISPLAY_ITEMS:]
        _render_criteria_chips(visible)
        if hidden:
            with st.expander(f"Show all {len(items)} {label.lower()}"):
                _render_criteria_chips(hidden)

    singular = label[:-1] if label.endswith("s") else label
    with st.form(f"add_{kind}_{profile_id}", clear_on_submit=True):
        new_term = st.text_input(
            f"Add {singular.lower()}",
            key=f"new_{kind}_input_{profile_id}",
            label_visibility="collapsed",
            placeholder=f"Add {singular.lower()}...",
        )
        if st.form_submit_button(f"Add {singular}"):
            if new_term.strip():
                pm.add_manual_criterion(profile_id, new_term.strip(), kind=kind)
                st.rerun()


def _render_criteria_chips(items: list[dict]) -> None:
    """Render criteria as remove-button rows, two per row."""
    for i in range(0, len(items), 2):
        cols = st.columns(2)
        for col, c in zip(cols, items[i : i + 2]):
            with col:
                source_badge = "📄" if c["source"] == "resume" else "✍️"
                tier_badge = f" t{c['weight_tier']}" if c["source"] == "resume" else ""
                btn_label = f"{source_badge} {c['term']}{tier_badge}  ✕"
                if st.button(
                    btn_label,
                    key=f"rm_crit_{c['id']}",
                    use_container_width=True,
                    help="Remove this criterion",
                ):
                    pm.remove_criterion(c["id"])
                    st.rerun()


def _render_criteria_section(profile_id: int, criteria: list[dict]) -> None:
    st.subheader("Parsed skills & criteria")

    skills = sorted(
        [c for c in criteria if c["kind"] == "skill"],
        key=lambda c: (c["weight_tier"], c["term"]),
    )
    roles = sorted(
        [c for c in criteria if c["kind"] == "role"],
        key=lambda c: (c["weight_tier"], c["term"]),
    )
    keywords = sorted(
        [c for c in criteria if c["kind"] == "keyword"],
        key=lambda c: (c["weight_tier"], c["term"]),
    )

    c1, c2, c3 = st.columns(3)
    with c1:
        _render_kind_block(profile_id, "skill", "Skills", skills)
    with c2:
        _render_kind_block(profile_id, "role", "Roles", roles)
    with c3:
        _render_kind_block(profile_id, "keyword", "Keywords", keywords)


# ---------------------------------------------------------------------------
# Preferences
# ---------------------------------------------------------------------------

def _option_index(options: list[tuple[str, str]], value) -> int:
    if value is None:
        return 0
    target = value.value if hasattr(value, "value") else str(value)
    for i, (_, v) in enumerate(options):
        if v == target:
            return i
    return 0


def _render_preferences_section(profile: dict) -> None:
    st.subheader("Preferences")
    with st.form("preferences_form"):
        c1, c2 = st.columns(2)
        with c1:
            seniority_idx = _option_index(SENIORITY_OPTIONS, profile.get("seniority_level"))
            seniority_label = st.selectbox(
                "Seniority",
                options=[label for label, _ in SENIORITY_OPTIONS],
                index=seniority_idx,
            )
        with c2:
            modality_idx = _option_index(WORK_MODALITY_OPTIONS, profile.get("work_modality"))
            modality_label = st.selectbox(
                "Work modality",
                options=[label for label, _ in WORK_MODALITY_OPTIONS],
                index=modality_idx,
            )

        s1, s2 = st.columns(2)
        with s1:
            salary_min = st.number_input(
                "Salary min (USD, annual)",
                min_value=0, max_value=1_000_000, step=5_000,
                value=int(profile.get("salary_min") or 0),
            )
        with s2:
            salary_max = st.number_input(
                "Salary max (USD, annual)",
                min_value=0, max_value=1_000_000, step=5_000,
                value=int(profile.get("salary_max") or 0),
            )

        if st.form_submit_button("Save preferences", type="primary"):
            seniority_value = dict(SENIORITY_OPTIONS)[seniority_label]
            modality_value = dict(WORK_MODALITY_OPTIONS)[modality_label]
            pm.save_preferences(
                profile["id"],
                seniority_level=seniority_value,
                work_modality=modality_value,
                salary_min=int(salary_min) or None,
                salary_max=int(salary_max) or None,
            )
            st.success("Preferences saved.")
            st.rerun()


# ---------------------------------------------------------------------------
# Target roles
# ---------------------------------------------------------------------------

def _render_target_roles_section(profile_id: int, roles: list[dict]) -> None:
    st.subheader("Target roles")
    if not roles:
        st.caption("No target roles set.")
    for r in roles:
        cols = st.columns([5, 1])
        with cols[0]:
            st.write(f"• **{r['role_name']}** (priority {r['priority']})")
        with cols[1]:
            if st.button("Remove", key=f"rm_role_{r['id']}", use_container_width=True):
                pm.remove_target_role(r["id"])
                st.rerun()

    with st.form(f"add_target_role_{profile_id}", clear_on_submit=True):
        cols = st.columns([4, 1, 1])
        with cols[0]:
            role_name = st.text_input(
                "Role name", placeholder="e.g. Data Analyst",
                label_visibility="collapsed",
            )
        with cols[1]:
            priority = st.number_input(
                "Priority", min_value=1, max_value=10, value=1,
                label_visibility="collapsed",
            )
        with cols[2]:
            if st.form_submit_button("Add role"):
                if role_name.strip():
                    pm.add_target_role(profile_id, role_name.strip(), int(priority))
                    st.rerun()


# ---------------------------------------------------------------------------
# Locations
# ---------------------------------------------------------------------------

def _render_locations_section(profile_id: int, locations: list[dict]) -> None:
    st.subheader("Preferred locations")
    if not locations:
        st.caption("No locations set.")
    for loc in locations:
        cols = st.columns([5, 1])
        with cols[0]:
            parts = [p for p in (loc["city"], loc["state"], loc["country"]) if p]
            st.write("• " + ", ".join(parts) if parts else "• (empty)")
        with cols[1]:
            if st.button("Remove", key=f"rm_loc_{loc['id']}", use_container_width=True):
                pm.remove_location(loc["id"])
                st.rerun()

    with st.form(f"add_location_{profile_id}", clear_on_submit=True):
        cols = st.columns([3, 2, 1])
        with cols[0]:
            city = st.text_input("City", placeholder="City", label_visibility="collapsed")
        with cols[1]:
            state = st.text_input("State", placeholder="State (e.g. CA)", label_visibility="collapsed")
        with cols[2]:
            if st.form_submit_button("Add location"):
                if (city or "").strip() or (state or "").strip():
                    pm.add_location(profile_id, city, state)
                    st.rerun()


# ---------------------------------------------------------------------------
# Blocklist
# ---------------------------------------------------------------------------

def _render_blocklist_section(profile_id: int, blocklist: list[dict]) -> None:
    st.subheader("Company blocklist")
    if not blocklist:
        st.caption("No companies blocked.")
    for entry in blocklist:
        cols = st.columns([5, 1])
        with cols[0]:
            line = f"• **{entry['company_name']}**"
            if entry["reason"]:
                line += f" — _{entry['reason']}_"
            st.write(line)
        with cols[1]:
            if st.button("Remove", key=f"rm_block_{entry['id']}", use_container_width=True):
                pm.remove_from_blocklist(entry["id"])
                st.rerun()

    with st.form(f"add_blocklist_{profile_id}", clear_on_submit=True):
        cols = st.columns([2, 3, 1])
        with cols[0]:
            company = st.text_input("Company", placeholder="Company name", label_visibility="collapsed")
        with cols[1]:
            reason = st.text_input("Reason", placeholder="Reason (optional)", label_visibility="collapsed")
        with cols[2]:
            if st.form_submit_button("Block"):
                if company.strip():
                    pm.add_to_blocklist(profile_id, company.strip(), reason)
                    st.rerun()
