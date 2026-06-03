from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import yaml
from sqlalchemy import select

from db.database import get_session
from db.models import Criterion, Profile
from .text_utils import find_terms

TAXONOMY_PATH = Path(__file__).resolve().parent.parent / "config" / "skills_taxonomy.yaml"


def _extract_text_pdf(path: Path) -> str:
    import pdfplumber

    with pdfplumber.open(str(path)) as pdf:
        return "\n".join((page.extract_text() or "") for page in pdf.pages)


def _extract_text_docx(path: Path) -> str:
    import docx  # python-docx

    document = docx.Document(str(path))
    return "\n".join(p.text for p in document.paragraphs)


def extract_text(file_path: str) -> str:
    path = Path(file_path)
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return _extract_text_pdf(path)
    if suffix == ".docx":
        return _extract_text_docx(path)
    raise ValueError(
        f"Unsupported resume format: {suffix!r}. Supported: .pdf, .docx"
    )


def load_taxonomy(path: Path | str = TAXONOMY_PATH) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _now_utc_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def parse_resume(file_path: str, profile_name: str) -> Profile:
    """Parse a resume, populate the named profile, and refresh its criteria.

    Resume-sourced criteria are replaced on each call so re-parsing the same
    resume does not duplicate rows. Manual criteria (source != "resume") are
    preserved.
    """
    raw_text = extract_text(file_path)
    taxonomy = load_taxonomy()

    skill_terms: list[str] = []
    for category in (taxonomy.get("skills") or {}).values():
        skill_terms.extend(category)
    role_terms: list[str] = list(taxonomy.get("roles") or [])
    keyword_terms: list[str] = list(taxonomy.get("keywords") or [])

    matched_skills = find_terms(raw_text, skill_terms)
    matched_roles = find_terms(raw_text, role_terms)
    matched_keywords = find_terms(raw_text, keyword_terms)

    with get_session() as session:
        profile = session.execute(
            select(Profile).where(Profile.name == profile_name)
        ).scalar_one_or_none()

        if profile is None:
            profile = Profile(name=profile_name)
            session.add(profile)
            session.flush()

        profile.resume_filename = Path(file_path).name
        profile.resume_raw_text = raw_text
        profile.parsed_at = _now_utc_naive()

        existing = session.execute(
            select(Criterion).where(
                Criterion.profile_id == profile.id,
                Criterion.source == "resume",
            )
        ).scalars().all()
        for c in existing:
            session.delete(c)
        session.flush()

        for term in matched_skills:
            session.add(
                Criterion(
                    profile_id=profile.id,
                    term=term,
                    kind="skill",
                    weight=3,
                    match_type="fuzzy",
                    source="resume",
                )
            )
        for term in matched_roles:
            session.add(
                Criterion(
                    profile_id=profile.id,
                    term=term,
                    kind="role",
                    weight=4,
                    match_type="fuzzy",
                    source="resume",
                )
            )
        for term in matched_keywords:
            session.add(
                Criterion(
                    profile_id=profile.id,
                    term=term,
                    kind="keyword",
                    weight=3,
                    match_type="fuzzy",
                    source="resume",
                )
            )

        session.commit()
        session.refresh(profile)
        return profile
