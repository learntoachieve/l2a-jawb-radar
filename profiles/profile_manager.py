"""
profiles/profile_manager.py

Business-logic layer between the dashboard and the DB models.

Schema invariants this layer enforces:
  - One ``User`` owns exactly one ``Profile``.
  - One ``Profile`` owns many ``Resume`` rows.
  - ``Profile.active_resume_id`` points at the resume the rest of the
    app should score / match against. NULL until the first upload.
  - ``Criterion.resume_id`` ties a criterion to the resume that
    produced it. Manual criteria use ``resume_id = NULL`` so they
    survive resume switches.

All resume / criteria operations key off ``profile_id``. The exception
is ``get_active_profile(user_id)`` and ``list_users()`` which sit one
level up.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from db.database import get_session
from db.models import (
    Criterion,
    Profile,
    ProfileCompanyBlocklist,
    ProfileLocation,
    ProfileTargetRole,
    Resume,
    SeniorityLevel,
    User,
    WorkModality,
)
from scoring.resume_parser import load_taxonomy
from scoring.text_utils import find_terms


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _extract_terms(raw_text: str) -> tuple[list[str], list[str], list[str]]:
    taxonomy = load_taxonomy()
    skill_terms: list[str] = []
    for category in (taxonomy.get("skills") or {}).values():
        skill_terms.extend(category)
    role_terms = list(taxonomy.get("roles") or [])
    keyword_terms = list(taxonomy.get("keywords") or [])
    return (
        find_terms(raw_text, skill_terms),
        find_terms(raw_text, role_terms),
        find_terms(raw_text, keyword_terms),
    )


def _insert_resume_criteria(
    session: Session,
    profile_id: int,
    resume_id: int,
    raw_text: str,
) -> None:
    skills, roles, keywords = _extract_terms(raw_text)
    for term in skills:
        session.add(Criterion(
            profile_id=profile_id, resume_id=resume_id,
            term=term, kind="skill",
            weight=3, weight_tier=2, source="resume",
        ))
    for term in roles:
        session.add(Criterion(
            profile_id=profile_id, resume_id=resume_id,
            term=term, kind="role",
            weight=4, weight_tier=1, source="resume",
        ))
    for term in keywords:
        session.add(Criterion(
            profile_id=profile_id, resume_id=resume_id,
            term=term, kind="keyword",
            weight=3, weight_tier=2, source="resume",
        ))


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

def create_user(username: str, display_name: str) -> User:
    with get_session() as session:
        existing = session.execute(
            select(User).where(User.username == username)
        ).scalar_one_or_none()
        if existing is not None:
            return existing
        user = User(username=username, display_name=display_name)
        session.add(user)
        session.flush()
        return user


def list_users() -> list[User]:
    with get_session() as session:
        rows = session.execute(
            select(User).where(User.is_active.is_(True)).order_by(User.username)
        ).scalars().all()
        return list(rows)


def get_user(user_id: int) -> Optional[User]:
    with get_session() as session:
        return session.get(User, user_id)


# ---------------------------------------------------------------------------
# Profiles (one per user)
# ---------------------------------------------------------------------------

def get_or_create_profile(user_id: int, name: str) -> Profile:
    """Return the single Profile for this user; create one if absent.

    ``name`` is only used when a profile doesn't yet exist. Existing
    profiles are returned as-is regardless of their stored name to
    preserve the one-profile-per-user invariant.
    """
    with get_session() as session:
        existing = session.execute(
            select(Profile).where(Profile.user_id == user_id)
        ).scalar_one_or_none()
        if existing is not None:
            return existing
        profile = Profile(user_id=user_id, name=name)
        session.add(profile)
        session.flush()
        return profile


def get_active_profile(user_id: int) -> Optional[Profile]:
    """The single profile belonging to ``user_id`` (or None)."""
    with get_session() as session:
        return session.execute(
            select(Profile).where(Profile.user_id == user_id)
        ).scalar_one_or_none()


# ---------------------------------------------------------------------------
# Resumes
# ---------------------------------------------------------------------------

def upload_resume(
    profile_id: int,
    label: str,
    filename: str,
    raw_text: str,
) -> Resume:
    """Insert a new Resume row, parse its criteria, and adopt it as active
    if the profile has no active resume yet.

    The newly created Resume is returned. Existing resumes and their
    criteria are not touched.
    """
    with get_session() as session:
        profile = session.get(Profile, profile_id)
        if profile is None:
            raise ValueError(f"Profile {profile_id} not found")

        resume = Resume(
            profile_id=profile_id,
            label=label,
            filename=filename,
            raw_text=raw_text,
            parsed_at=_now(),
        )
        session.add(resume)
        session.flush()  # populate resume.id

        _insert_resume_criteria(session, profile_id, resume.id, raw_text)

        if profile.active_resume_id is None:
            profile.active_resume_id = resume.id

        session.flush()
        return resume


def set_active_resume(profile_id: int, resume_id: int) -> Profile:
    """Point ``Profile.active_resume_id`` at ``resume_id``.

    Raises ValueError if the resume doesn't belong to this profile.
    """
    with get_session() as session:
        profile = session.get(Profile, profile_id)
        if profile is None:
            raise ValueError(f"Profile {profile_id} not found")
        resume = session.get(Resume, resume_id)
        if resume is None or resume.profile_id != profile_id:
            raise ValueError(
                f"Resume {resume_id} not found for profile {profile_id}"
            )
        profile.active_resume_id = resume_id
        session.flush()
        return profile


def list_resumes(profile_id: int) -> list[Resume]:
    """All Resume rows for this profile, newest first."""
    with get_session() as session:
        rows = session.execute(
            select(Resume)
            .where(Resume.profile_id == profile_id)
            .order_by(Resume.created_at.desc())
        ).scalars().all()
        return list(rows)


def delete_resume(profile_id: int, resume_id: int) -> None:
    """Delete a resume and its resume-sourced criteria.

    If the deleted resume was the profile's active one, the active
    pointer is moved to the next-most-recent resume, or NULL if none
    remain. Manual criteria (``resume_id IS NULL``) are preserved.
    """
    with get_session() as session:
        profile = session.get(Profile, profile_id)
        if profile is None:
            return
        resume = session.get(Resume, resume_id)
        if resume is None or resume.profile_id != profile_id:
            return

        was_active = profile.active_resume_id == resume_id
        if was_active:
            profile.active_resume_id = None
            session.flush()

        session.execute(
            Criterion.__table__.delete().where(Criterion.resume_id == resume_id)
        )
        session.delete(resume)
        session.flush()

        if was_active:
            next_resume = session.execute(
                select(Resume)
                .where(Resume.profile_id == profile_id)
                .order_by(Resume.created_at.desc())
                .limit(1)
            ).scalar_one_or_none()
            if next_resume is not None:
                profile.active_resume_id = next_resume.id
                session.flush()


# ---------------------------------------------------------------------------
# Preferences
# ---------------------------------------------------------------------------

_PREF_FIELDS = {"seniority_level", "work_modality", "salary_min", "salary_max"}


def save_preferences(profile_id: int, **kwargs: Any) -> Profile:
    """Patch any subset of {seniority_level, work_modality, salary_min, salary_max}."""
    with get_session() as session:
        profile = session.get(Profile, profile_id)
        if profile is None:
            raise ValueError(f"Profile {profile_id} not found")
        for key, value in kwargs.items():
            if key not in _PREF_FIELDS:
                continue
            if isinstance(value, (SeniorityLevel, WorkModality)):
                value = value.value
            setattr(profile, key, value)
        session.flush()
        return profile


# ---------------------------------------------------------------------------
# Target roles
# ---------------------------------------------------------------------------

def add_target_role(profile_id: int, role_name: str, priority: int = 1) -> ProfileTargetRole:
    with get_session() as session:
        role = ProfileTargetRole(
            profile_id=profile_id,
            role_name=role_name.strip(),
            priority=priority,
        )
        session.add(role)
        session.flush()
        return role


def remove_target_role(role_id: int) -> None:
    with get_session() as session:
        row = session.get(ProfileTargetRole, role_id)
        if row is not None:
            session.delete(row)


def list_target_roles(profile_id: int) -> list[ProfileTargetRole]:
    with get_session() as session:
        rows = session.execute(
            select(ProfileTargetRole)
            .where(ProfileTargetRole.profile_id == profile_id)
            .order_by(ProfileTargetRole.priority, ProfileTargetRole.role_name)
        ).scalars().all()
        return list(rows)


# ---------------------------------------------------------------------------
# Locations
# ---------------------------------------------------------------------------

def add_location(
    profile_id: int,
    city: Optional[str],
    state: Optional[str],
    country: str = "US",
) -> ProfileLocation:
    with get_session() as session:
        loc = ProfileLocation(
            profile_id=profile_id,
            city=(city or "").strip() or None,
            state=(state or "").strip() or None,
            country=country,
        )
        session.add(loc)
        session.flush()
        return loc


def remove_location(location_id: int) -> None:
    with get_session() as session:
        row = session.get(ProfileLocation, location_id)
        if row is not None:
            session.delete(row)


def list_locations(profile_id: int) -> list[ProfileLocation]:
    with get_session() as session:
        rows = session.execute(
            select(ProfileLocation)
            .where(ProfileLocation.profile_id == profile_id)
            .order_by(ProfileLocation.is_primary.desc(), ProfileLocation.city)
        ).scalars().all()
        return list(rows)


# ---------------------------------------------------------------------------
# Company blocklist
# ---------------------------------------------------------------------------

def add_to_blocklist(
    profile_id: int,
    company_name: str,
    reason: str = "",
) -> ProfileCompanyBlocklist:
    with get_session() as session:
        entry = ProfileCompanyBlocklist(
            profile_id=profile_id,
            company_name=company_name.strip(),
            reason=(reason or "").strip() or None,
        )
        session.add(entry)
        session.flush()
        return entry


def remove_from_blocklist(entry_id: int) -> None:
    with get_session() as session:
        row = session.get(ProfileCompanyBlocklist, entry_id)
        if row is not None:
            session.delete(row)


def list_blocklist(profile_id: int) -> list[ProfileCompanyBlocklist]:
    with get_session() as session:
        rows = session.execute(
            select(ProfileCompanyBlocklist)
            .where(ProfileCompanyBlocklist.profile_id == profile_id)
            .order_by(ProfileCompanyBlocklist.company_name)
        ).scalars().all()
        return list(rows)


# ---------------------------------------------------------------------------
# Criteria
# ---------------------------------------------------------------------------

def add_manual_criterion(
    profile_id: int,
    term: str,
    kind: str = "skill",
    weight: int = 3,
    weight_tier: int = 2,
) -> Criterion:
    """Insert a manual criterion with ``resume_id = NULL``.

    Manual criteria are always returned by ``get_active_criteria`` and
    survive resume switches and deletions.
    """
    with get_session() as session:
        c = Criterion(
            profile_id=profile_id,
            resume_id=None,
            term=term.strip(),
            kind=kind,
            weight=weight,
            weight_tier=weight_tier,
            source="manual",
            match_type="fuzzy",
        )
        session.add(c)
        session.flush()
        return c


def remove_criterion(criterion_id: int) -> None:
    with get_session() as session:
        c = session.get(Criterion, criterion_id)
        if c is not None:
            session.delete(c)


def get_active_criteria(profile_id: int) -> list[dict]:
    """Manual criteria + the active resume's criteria.

    Returns dicts (not ORM objects) so the UI can render them after
    the session closes. Ordered by kind, then weight tier, then term.
    """
    with get_session() as session:
        profile = session.get(Profile, profile_id)
        if profile is None:
            return []
        active_id = profile.active_resume_id

        if active_id is None:
            condition = Criterion.resume_id.is_(None)
        else:
            condition = or_(
                Criterion.resume_id == active_id,
                Criterion.resume_id.is_(None),
            )

        rows = session.execute(
            select(Criterion)
            .where(Criterion.profile_id == profile_id, condition)
            .order_by(Criterion.kind, Criterion.weight_tier, Criterion.term)
        ).scalars().all()
        return [
            {
                "id": c.id,
                "term": c.term,
                "kind": c.kind,
                "weight": c.weight,
                "weight_tier": c.weight_tier,
                "source": c.source,
                "match_type": c.match_type,
                "resume_id": c.resume_id,
            }
            for c in rows
        ]


# ---------------------------------------------------------------------------
# Combined summary
# ---------------------------------------------------------------------------

def get_profile_summary(profile_id: int) -> dict:
    """Everything the Profile tab needs to render, in one round-trip."""
    with get_session() as session:
        profile = session.get(Profile, profile_id)
        if profile is None:
            return {}

        active_resume = None
        if profile.active_resume_id is not None:
            active_resume = session.get(Resume, profile.active_resume_id)

        resume_count = session.execute(
            select(Resume).where(Resume.profile_id == profile_id)
        ).scalars().all()
        resume_count = len(resume_count)

        if profile.active_resume_id is None:
            criteria_condition = Criterion.resume_id.is_(None)
        else:
            criteria_condition = or_(
                Criterion.resume_id == profile.active_resume_id,
                Criterion.resume_id.is_(None),
            )

        criteria = session.execute(
            select(Criterion)
            .where(Criterion.profile_id == profile_id, criteria_condition)
            .order_by(Criterion.kind, Criterion.weight_tier, Criterion.term)
        ).scalars().all()
        target_roles = session.execute(
            select(ProfileTargetRole)
            .where(ProfileTargetRole.profile_id == profile_id)
            .order_by(ProfileTargetRole.priority, ProfileTargetRole.role_name)
        ).scalars().all()
        locations = session.execute(
            select(ProfileLocation)
            .where(ProfileLocation.profile_id == profile_id)
        ).scalars().all()
        blocklist = session.execute(
            select(ProfileCompanyBlocklist)
            .where(ProfileCompanyBlocklist.profile_id == profile_id)
            .order_by(ProfileCompanyBlocklist.company_name)
        ).scalars().all()

        criteria_dicts = [
            {
                "id": c.id, "term": c.term, "kind": c.kind,
                "weight": c.weight, "weight_tier": c.weight_tier,
                "source": c.source, "match_type": c.match_type,
                "resume_id": c.resume_id,
            }
            for c in criteria
        ]

        return {
            "profile": {
                "id": profile.id,
                "user_id": profile.user_id,
                "name": profile.name,
                "active_resume_id": profile.active_resume_id,
                "seniority_level": profile.seniority_level,
                "work_modality": profile.work_modality,
                "salary_min": profile.salary_min,
                "salary_max": profile.salary_max,
                "created_at": profile.created_at,
            },
            "active_resume": (
                {
                    "id": active_resume.id,
                    "label": active_resume.label,
                    "filename": active_resume.filename,
                    "parsed_at": active_resume.parsed_at,
                    "created_at": active_resume.created_at,
                }
                if active_resume is not None
                else None
            ),
            "resume_count": resume_count,
            "criteria": criteria_dicts,
            "skill_count":   sum(1 for c in criteria_dicts if c["kind"] == "skill"),
            "role_count":    sum(1 for c in criteria_dicts if c["kind"] == "role"),
            "keyword_count": sum(1 for c in criteria_dicts if c["kind"] == "keyword"),
            "target_roles": [
                {"id": r.id, "role_name": r.role_name, "priority": r.priority}
                for r in target_roles
            ],
            "locations": [
                {
                    "id": l.id, "city": l.city, "state": l.state,
                    "country": l.country, "is_primary": l.is_primary,
                }
                for l in locations
            ],
            "blocklist": [
                {"id": b.id, "company_name": b.company_name, "reason": b.reason}
                for b in blocklist
            ],
        }
