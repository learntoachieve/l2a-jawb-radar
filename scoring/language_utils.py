"""Best-effort offline language detection — flag obviously-non-English postings.

Heuristic: tokenize the text, count tokens that match an English
stopword set, divide by total tokens.

Density tiers:
   >= 8%   "en"
   3 - 8%  "mixed"  (treated as English-acceptable downstream)
   <  3%   "other"

Conservative on purpose — for short text or empty input we return
"en" so we never hide a borderline case.
"""
from __future__ import annotations

from .text_utils import tokenize

# Reusable English stopword set. Hand-rolled because we want tight
# control over what counts as a stopword "signal" — adding too many
# function-class words would water down the density measurement.
ENGLISH_STOPWORDS: frozenset[str] = frozenset(
    {
        "the", "and", "is", "was", "of", "to", "a", "in", "that", "it",
        "for", "on", "with", "as", "by", "this", "but", "or", "an", "be",
        "are", "from", "we", "you", "your", "our", "have", "has", "will",
        "can", "all", "their", "they", "them", "his", "her", "its",
        "would", "could", "should", "if", "not", "no", "yes", "do", "does",
        "did", "been", "being", "had", "having", "what", "which", "who",
        "when", "where", "why", "how", "any", "some", "more", "most",
        "such", "only", "own", "same", "than", "too", "very", "just",
        "also", "well", "even", "much", "many", "other", "another",
        "each", "every", "both", "few", "use", "used", "make", "made",
        "look", "want", "need", "go", "come", "take", "give",
        "find", "tell", "ask", "work", "seem", "feel", "try", "leave",
        "after", "before", "between", "during", "without", "while",
        "about", "across", "against", "around", "among", "behind", "below",
        "above", "off", "over", "under", "into", "onto", "out",
        "i", "me", "my", "us", "him", "she", "he",
        "at", "through",
        "people", "team", "build", "role", "company", "join",
    }
)


def detect_language(text: str) -> str:
    """Return ``"en"``, ``"mixed"``, or ``"other"`` for ``text``."""
    if not text:
        return "en"

    tokens = tokenize(text)
    total = len(tokens)
    if total < 20:
        return "en"

    matches = sum(1 for t in tokens if t in ENGLISH_STOPWORDS)
    density = matches / total

    if density >= 0.08:
        return "en"
    if density >= 0.03:
        return "mixed"
    return "other"
