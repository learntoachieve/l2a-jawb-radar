"""
SQLAlchemy 2.x mapped models for L2A Jawb Radar.

Table hierarchy:
  users
    └── profiles               (active_resume_id -> resumes.id)
          ├── profile_target_roles
          ├── profile_locations
          ├── profile_industries
          ├── profile_company_blocklist
          ├── resumes
          └── criteria         (resume_id -> resumes.id, nullable=manual)
  sources
  source_registry
  items (FK: source)
    ├── scores       (FK: profile, resume nullable)
    ├── tracking     (FK: profile, user)
    └── applications (FK: profile, resume nullable)
"""
from __future__ import annotations

import enum
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import (
    JSON, Boolean, DateTime, Enum as SAEnum,
    Float, ForeignKey, Integer, String, Text,
    UniqueConstraint, func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String, nullable=False)
    email: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


# ---------------------------------------------------------------------------
# Profiles
# ---------------------------------------------------------------------------

class SeniorityLevel(str, enum.Enum):
    entry = "entry"
    mid = "mid"
    senior = "senior"
    any = "any"


class WorkModality(str, enum.Enum):
    remote = "remote"
    hybrid = "hybrid"
    onsite = "onsite"
    any = "any"


class Profile(Base):
    __tablename__ = "profiles"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    resume_filename: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    resume_raw_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    parsed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    # Manual profile fields (set/refined via UI)
    seniority_level: Mapped[Optional[str]] = mapped_column(
        SAEnum(SeniorityLevel, name="seniority_level"), nullable=True
    )
    work_modality: Mapped[Optional[str]] = mapped_column(
        SAEnum(WorkModality, name="work_modality"), nullable=True
    )
    salary_min: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    salary_max: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # Active resume pointer (nullable: a profile may exist before any upload).
    # use_alter resolves the profiles<->resumes circular FK at CREATE time.
    active_resume_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("resumes.id", use_alter=True, name="fk_profiles_active_resume_id"),
        nullable=True,
    )

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class Resume(Base):
    """One uploaded resume version per row. A profile may own many."""
    __tablename__ = "resumes"

    id: Mapped[int] = mapped_column(primary_key=True)
    profile_id: Mapped[int] = mapped_column(ForeignKey("profiles.id"), nullable=False)
    label: Mapped[str] = mapped_column(String, nullable=False)
    filename: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    raw_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    parsed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class ProfileTargetRole(Base):
    __tablename__ = "profile_target_roles"

    id: Mapped[int] = mapped_column(primary_key=True)
    profile_id: Mapped[int] = mapped_column(ForeignKey("profiles.id"), nullable=False)
    role_name: Mapped[str] = mapped_column(String, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=1, nullable=False)  # 1 = highest
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class ProfileLocation(Base):
    __tablename__ = "profile_locations"

    id: Mapped[int] = mapped_column(primary_key=True)
    profile_id: Mapped[int] = mapped_column(ForeignKey("profiles.id"), nullable=False)
    city: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    state: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    country: Mapped[str] = mapped_column(String, default="US", nullable=False)
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class IndustryPreference(str, enum.Enum):
    preferred = "preferred"
    neutral = "neutral"
    avoid = "avoid"


class ProfileIndustry(Base):
    __tablename__ = "profile_industries"

    id: Mapped[int] = mapped_column(primary_key=True)
    profile_id: Mapped[int] = mapped_column(ForeignKey("profiles.id"), nullable=False)
    industry_name: Mapped[str] = mapped_column(String, nullable=False)
    preference: Mapped[str] = mapped_column(
        SAEnum(IndustryPreference, name="industry_preference"),
        default="preferred",
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class ProfileCompanyBlocklist(Base):
    __tablename__ = "profile_company_blocklist"

    id: Mapped[int] = mapped_column(primary_key=True)
    profile_id: Mapped[int] = mapped_column(ForeignKey("profiles.id"), nullable=False)
    company_name: Mapped[str] = mapped_column(String, nullable=False)
    reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class Criterion(Base):
    """Skills, roles, and keywords extracted from resume or added manually.

    ``resume_id`` is NULL for manually added criteria so they survive
    resume switches; set to the originating Resume row for resume-parsed
    criteria so we can scope to the active resume only.
    """
    __tablename__ = "criteria"

    id: Mapped[int] = mapped_column(primary_key=True)
    profile_id: Mapped[int] = mapped_column(ForeignKey("profiles.id"), nullable=False)
    resume_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("resumes.id"), nullable=True
    )
    term: Mapped[str] = mapped_column(String, nullable=False)
    weight: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    weight_tier: Mapped[int] = mapped_column(Integer, default=2, nullable=False)
    # tier 1 = strongest resume signal, 2 = solid, 3 = mentioned
    kind: Mapped[str] = mapped_column(String, nullable=False)   # skill / role / keyword
    match_type: Mapped[str] = mapped_column(String, default="fuzzy", nullable=False)
    source: Mapped[str] = mapped_column(String, default="resume", nullable=False)  # resume / manual
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------

class Source(Base):
    __tablename__ = "sources"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    type: Mapped[str] = mapped_column(String, nullable=False)  # api / rss / ats / html
    url: Mapped[str] = mapped_column(String, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_run_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class SourceRegistry(Base):
    """Company ATS board registry — tracks individual company job boards."""
    __tablename__ = "source_registry"

    id: Mapped[int] = mapped_column(primary_key=True)
    source_url: Mapped[str] = mapped_column(String, nullable=False)
    normalized_url: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    portal_type: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    company: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    label: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, default="manual_review", nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    last_verified_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


# ---------------------------------------------------------------------------
# Items (scraped jobs)
# ---------------------------------------------------------------------------

class Item(Base):
    __tablename__ = "items"

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"), nullable=False)
    external_id: Mapped[str] = mapped_column(String, nullable=False)
    title: Mapped[str] = mapped_column(String, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    url: Mapped[str] = mapped_column(String, nullable=False)
    metadata_json: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    # metadata_json carries: company, salary_text, location_raw, portal_type

    # Geocoding (async, post-scrape via Nominatim)
    lat: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    lng: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    geocoded_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    # Industry category (LLM classifier)
    industry_category: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    classified_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    posted_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    scraped_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    content_hash: Mapped[str] = mapped_column(String, nullable=False, index=True)

    __table_args__ = (
        UniqueConstraint("source_id", "external_id", name="uq_items_source_external"),
        UniqueConstraint("source_id", "url", name="uq_items_source_url"),
    )


# ---------------------------------------------------------------------------
# Scores
# ---------------------------------------------------------------------------

class Score(Base):
    __tablename__ = "scores"

    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id"), nullable=False)
    profile_id: Mapped[int] = mapped_column(ForeignKey("profiles.id"), nullable=False)
    # Which resume produced this score. Nullable so a profile with no
    # uploaded resume can still receive scores from manual criteria.
    resume_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("resumes.id"), nullable=True
    )
    score: Mapped[float] = mapped_column(Float, nullable=False)
    raw_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    matched_terms_json: Mapped[Optional[list[Any]]] = mapped_column(JSON, nullable=True)
    score_breakdown_json: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    computed_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    __table_args__ = (
        UniqueConstraint(
            "item_id", "profile_id", "resume_id",
            name="uq_scores_item_profile_resume",
        ),
    )


# ---------------------------------------------------------------------------
# Tracking (full application state machine)
# ---------------------------------------------------------------------------

class TrackingStatus(str, enum.Enum):
    new = "new"
    interested = "interested"   # batch-selected, not yet opened
    opened = "opened"           # URL opened in browser
    applied = "applied"
    recruiter_screen = "recruiter_screen"
    assessment = "assessment"
    interview = "interview"
    offer = "offer"
    rejected = "rejected"
    ghosted = "ghosted"
    skipped = "skipped"
    hidden = "hidden"
    superseded = "superseded"


# Valid transitions: what statuses can follow each status
ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "new":              frozenset({"interested", "opened", "applied", "skipped", "hidden"}),
    "interested":       frozenset({"opened", "applied", "skipped", "hidden"}),
    "opened":           frozenset({"applied", "skipped", "hidden", "recruiter_screen"}),
    "applied":          frozenset({"recruiter_screen", "assessment", "interview", "offer", "rejected", "ghosted", "superseded"}),
    "recruiter_screen": frozenset({"assessment", "interview", "offer", "rejected", "superseded"}),
    "assessment":       frozenset({"interview", "offer", "rejected", "superseded"}),
    "interview":        frozenset({"offer", "rejected", "superseded"}),
    "offer":            frozenset({"superseded"}),
    "rejected":         frozenset({"superseded"}),
    "ghosted":          frozenset({"superseded"}),
    "skipped":          frozenset({"opened", "interested", "applied", "hidden", "superseded"}),
    "hidden":           frozenset({"new", "superseded"}),
    "superseded":       frozenset({"opened", "applied", "skipped"}),
}


class Tracking(Base):
    __tablename__ = "tracking"

    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id"), nullable=False)
    profile_id: Mapped[int] = mapped_column(ForeignKey("profiles.id"), nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    status: Mapped[TrackingStatus] = mapped_column(
        SAEnum(TrackingStatus, name="tracking_status"), nullable=False
    )
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    applied_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_status_change_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("item_id", "profile_id", name="uq_tracking_item_profile"),
    )


# ---------------------------------------------------------------------------
# Applications
# ---------------------------------------------------------------------------

class Application(Base):
    __tablename__ = "applications"

    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id"), nullable=False)
    profile_id: Mapped[int] = mapped_column(ForeignKey("profiles.id"), nullable=False)
    resume_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("resumes.id"), nullable=True
    )
    resume_version: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    tailored_notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    keyword_diff_snapshot_json: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    submitted_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
