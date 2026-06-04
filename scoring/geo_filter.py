"""US-only geo filter shared by the dashboard queue and batch scoring.

The tool is US-only for now: international postings (LatAm, EU, UK,
India, etc.) should neither appear in the queue nor consume scoring
capacity. ``is_us_or_remote`` is the single source of truth for that
decision so the dashboard and the scorer never disagree.

Decision order (first signal wins):

  1. ``geo_tier == "foreign"`` — the authoritative, pre-computed bucket
     stored on every item by ``location_utils.classify_geo_tier``
     ("foreign" == the user can't take the role from their home metro).
  2. ``location_normalized`` (a.k.a. ``normalized``) in a foreign bucket
     (EU / UK / Canada / India / Brazil / LatAm / Asia-Other / Africa /
     Australia-NZ).
  3. A raw ``location`` / ``location_raw`` string containing an explicit
     non-US indicator (", UK", ", India", "Latin America", ...).

Anything else — including bare "Remote", "Remote-Global", "US",
"Unknown", or a missing location — is kept: those are treated as
US-or-remote unless an explicit foreign signal says otherwise.
"""
from __future__ import annotations

from typing import Optional

from .location_utils import FOREIGN_BUCKETS

# Substring indicators scanned against the raw location string. Lowercased.
# Comma-prefixed forms ("(, india") are deliberate: they avoid false
# positives inside US place names (", india" never matches "Indiana",
# ", uk" never matches "Paducah"). The geo_tier / FOREIGN_BUCKETS checks
# above already catch most cases; this is a belt-and-suspenders pass for
# items whose location string wasn't bucketed.
_NON_US_INDICATORS: tuple[str, ...] = (
    ", uk",
    ", india",
    ", germany",
    "latin america",
    "latam",
    "europe",
    "philippines",
    "canada",
    "brazil",
    "australia",
    "remote - international",
    "remote-international",
)


def is_us_or_remote(metadata_json: Optional[dict]) -> bool:
    """Return True to keep (US or US-remote), False to exclude (foreign)."""
    m = metadata_json or {}

    geo_tier = str(m.get("geo_tier") or "").strip().lower()
    if geo_tier == "foreign":
        return False

    normalized = str(m.get("location_normalized") or m.get("normalized") or "").strip()
    if normalized in FOREIGN_BUCKETS:
        return False

    location = str(m.get("location_raw") or m.get("location") or "").lower()
    if location and any(ind in location for ind in _NON_US_INDICATORS):
        return False

    return True


# Backwards/brief-compatible alias — the brief refers to the predicate as
# ``_is_us_or_remote``; both names point at the same function.
_is_us_or_remote = is_us_or_remote
