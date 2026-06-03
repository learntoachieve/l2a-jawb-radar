"""Best-effort offline location bucketing.

``normalize_location(raw, body=None)`` returns one of:
    Remote-US | Remote-Global | US | EU | UK | Canada | India |
    Brazil | LatAm | Asia-Other | Africa | Australia/NZ | Unknown

Order matters: Remote-US is checked before Remote-Global so an
explicit US-only remote post doesn't fall into the global bucket.

No external geocoding APIs — all matching uses word-boundary
patterns from text_utils.term_pattern, so 2-letter state codes
like "CA" don't false-match inside words like "Canada".
"""
from __future__ import annotations

from typing import Iterable

from .text_utils import term_pattern

LOCATION_RULES: list[tuple[str, list[str]]] = [
    (
        "Remote-US",
        [
            "remote (us)", "remote us", "remote, us", "remote - us",
            "us-only", "us only", "united states only",
            "remote in the us", "remote in us", "remote within us",
            "us residents only", "us-based", "us based",
        ],
    ),
    (
        "Remote-Global",
        [
            "remote", "worldwide", "anywhere", "global",
            "fully remote", "100% remote",
        ],
    ),
    (
        "US",
        [
            "united states", "usa", "u.s.a", "u.s.",
            # Major cities
            "new york", "nyc", "los angeles", "san francisco",
            "chicago", "houston", "phoenix", "philadelphia",
            "san diego", "dallas", "austin", "boston", "seattle",
            "denver", "atlanta", "miami", "portland", "minneapolis",
            "detroit", "baltimore", "milwaukee", "sacramento",
            "kansas city", "las vegas", "raleigh", "tampa",
            "washington dc", "washington d.c.",
            # Full state names — bare 2-letter state codes are deliberately
            # excluded because they collide with everyday words and
            # foreign place names (e.g. "DE" in "Rio de Janeiro", "MA",
            # "OR", "IN"). Full-name coverage is sufficient when paired
            # with city names; the regex word-boundary still protects
            # against substring matches.
            "california", "texas", "florida", "pennsylvania",
            "illinois", "ohio", "north carolina", "michigan",
            "new jersey", "virginia", "arizona", "tennessee",
            "massachusetts", "indiana", "missouri", "maryland",
            "wisconsin", "colorado", "minnesota", "south carolina",
            "alabama", "louisiana", "kentucky", "oregon", "oklahoma",
            "connecticut", "utah", "iowa", "nevada", "arkansas",
            "mississippi", "kansas", "new mexico", "nebraska",
            "west virginia", "idaho", "hawaii", "new hampshire",
            "maine", "montana", "rhode island", "delaware",
            "south dakota", "north dakota", "alaska", "vermont",
            "wyoming",
        ],
    ),
    (
        "EU",
        [
            "european union", "europe", "eu only", "eu-only", "emea",
            "germany", "france", "netherlands", "spain", "italy",
            "poland", "sweden", "norway", "denmark", "finland",
            "belgium", "austria", "ireland", "portugal", "greece",
            "czech republic", "romania", "berlin", "amsterdam",
            "paris", "madrid", "rome", "stockholm", "dublin",
            "warsaw", "lisbon", "vienna", "munich", "barcelona",
        ],
    ),
    (
        "UK",
        [
            "united kingdom", "uk", "u.k.", "great britain", "england",
            "scotland", "wales", "london", "manchester", "birmingham",
            "edinburgh", "glasgow", "liverpool", "bristol", "cardiff",
        ],
    ),
    (
        "Canada",
        [
            "canada", "toronto", "vancouver", "montreal", "ontario",
            "british columbia", "quebec", "alberta", "calgary", "ottawa",
        ],
    ),
    (
        "India",
        [
            "india", "bangalore", "bengaluru", "hyderabad", "mumbai",
            "delhi", "chennai", "pune", "kolkata", "ahmedabad",
            "noida", "gurgaon", "gurugram",
        ],
    ),
    (
        "Brazil",
        [
            "brazil", "brasil", "são paulo", "sao paulo",
            "rio de janeiro", "belo horizonte", "salvador",
            "brasília", "brasilia", "fortaleza", "curitiba",
            "porto alegre", "recife",
        ],
    ),
    (
        "LatAm",
        [
            "latin america", "latam", "south america",
            "mexico", "argentina", "colombia", "chile", "peru",
            "venezuela", "ecuador", "uruguay", "paraguay", "bolivia",
            "guatemala", "costa rica", "panama", "dominican republic",
            "puerto rico", "buenos aires", "mexico city", "bogota",
            "santiago", "lima",
        ],
    ),
    (
        "Asia-Other",
        [
            # South Asia (excluding India, which has its own bucket)
            "pakistan", "bangladesh", "sri lanka", "nepal",
            "karachi", "lahore", "dhaka", "colombo",
            # Southeast Asia
            "philippines", "vietnam", "indonesia", "singapore",
            "thailand", "malaysia", "cambodia", "laos", "myanmar",
            "manila", "jakarta", "ho chi minh", "bangkok",
            "kuala lumpur",
            # East Asia
            "japan", "south korea", "china", "hong kong", "taiwan",
            "tokyo", "seoul", "shanghai", "beijing", "taipei",
        ],
    ),
    (
        "Africa",
        [
            "south africa", "nigeria", "kenya", "egypt", "morocco",
            "ghana", "tunisia", "algeria", "ethiopia", "tanzania",
            "uganda", "lagos", "nairobi", "cairo", "casablanca",
            "johannesburg", "cape town",
        ],
    ),
    (
        "Australia/NZ",
        [
            "australia", "new zealand", "sydney", "melbourne",
            "brisbane", "auckland", "wellington", "perth", "adelaide",
        ],
    ),
]


def _has_any(text: str, terms: Iterable[str]) -> bool:
    for term in terms:
        if term_pattern(term).search(text):
            return True
    return False


# ----- Geo-tier classification (display-side preference boost) -----

# Per-metro term lists. Future profiles for other home metros add entries
# here without touching call sites.
HOME_METRO_TERMS: dict[str, list[str]] = {
    "sacramento": [
        "sacramento", "roseville", "folsom", "elk grove",
        "davis", "west sacramento", "rancho cordova",
    ],
}

# Per-region term lists for the "regional" tier — typically the home state
# plus its broader region (e.g. west coast for California).
REGIONAL_TERMS_BY_REGION: dict[str, list[str]] = {
    "california": [
        # California state and major metros
        "california",
        "bay area", "san francisco", "sf", "oakland", "berkeley",
        "palo alto", "mountain view", "san jose", "silicon valley",
        "los angeles", "la", "san diego", "santa cruz", "monterey",
        "fresno", "bakersfield",
        # West Coast + nearby
        "oregon", "portland",
        "seattle", "washington state", "wa",
        "nevada", "reno", "las vegas",
    ],
}

# Buckets from normalize_location that map to the new "foreign" tier —
# i.e. the user can't take the role from Sacramento.
FOREIGN_BUCKETS: frozenset[str] = frozenset({
    "EU", "UK", "Canada", "India", "Brazil", "LatAm",
    "Asia-Other", "Africa", "Australia/NZ",
})


def _has_foreign_signal(text: str) -> bool:
    """True when the text contains a term from any foreign LOCATION_RULES bucket.

    Used to override the "remote -> local" shortcut for posts like
    "Toronto, Remote in Canada" — normalize_location prefers
    Remote-Global there, but the post is actually Canada-only.
    """
    for bucket, terms in LOCATION_RULES:
        if bucket in FOREIGN_BUCKETS and _has_any(text, terms):
            return True
    return False


def classify_geo_tier(
    raw_location: str | None,
    body: str | None = None,
    home_metro: str = "sacramento",
    home_region: str = "california",
) -> str:
    """Bucket a posting's location into "local", "regional", "domestic",
    "foreign", or "unknown" relative to the configured home metro/region.

    Used for display-time soft boosts in the dashboard queue. Independent
    of (and complementary to) ``normalize_location`` which handles
    hard-filter buckets.

    Body is used as a fallback only when ``raw_location`` is empty —
    same convention as ``normalize_location``.
    """
    candidates: list[str] = []
    if raw_location:
        candidates.append(str(raw_location))
    if body and not raw_location:
        candidates.append(str(body))
    if not candidates:
        return "unknown"

    text = " | ".join(candidates)
    bucket = normalize_location(raw_location, body)

    # Foreign: the user can't apply from Sacramento. Checked before the
    # metro/remote/regional logic so a post like "Toronto, Remote in Canada"
    # correctly reads as foreign instead of falling into local via the
    # remote-anywhere shortcut. Both the bucket and a direct text scan
    # are checked because Remote-Global beats Canada in normalize_location's
    # rule order, so the bucket alone misses "remote + foreign-city" cases.
    if bucket in FOREIGN_BUCKETS:
        return "foreign"
    if bucket == "Remote-Global" and _has_foreign_signal(text):
        return "foreign"

    metro_terms = HOME_METRO_TERMS.get((home_metro or "").lower(), [])
    regional_terms = REGIONAL_TERMS_BY_REGION.get(
        (home_region or "").lower(), []
    )

    # Local: explicit home-metro city, OR remote-anywhere (when a home
    # region is configured). Remote-anywhere is "local" because the user
    # can take it from their home metro just fine.
    if metro_terms and _has_any(text, metro_terms):
        return "local"
    if home_region and _has_any(text, ["remote"]):
        return "local"

    # Regional: home state + broader region.
    if regional_terms and _has_any(text, regional_terms):
        return "regional"

    # Domestic: anything else US — defer to normalize_location's bucket.
    if bucket in ("US", "Remote-US", "Remote-Global"):
        return "domestic"

    return "unknown"


def normalize_location(
    raw: str | None, body: str | None = None
) -> str:
    """Bucket a job posting into one of the LOCATION_RULES categories.

    ``raw`` is the source-provided location string (may be None). If
    that's empty we fall back to scanning ``body``. First match wins.
    """
    candidates: list[str] = []
    if raw:
        candidates.append(str(raw))
    if body and not raw:
        # Only scan body when we have no raw location signal — the body
        # is full of location-ish noise (e.g. "California Bar exam") that
        # would over-match if we always merged it in.
        candidates.append(str(body))

    if not candidates:
        return "Unknown"

    text = " | ".join(candidates)
    for bucket, terms in LOCATION_RULES:
        if _has_any(text, terms):
            return bucket
    return "Unknown"
