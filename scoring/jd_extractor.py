"""Extract per-item keywords for the resume-tailor feature.

For each item we score every unique post-stopword token by
``frequency * importance`` and return the top N.

Importance signals:
  - 2.0 if the token is in the skills/roles taxonomy
  - 1.5 if the token participated in a capitalized multi-word phrase
        (heuristic for product/tool names not in the taxonomy)
  - 1.0 otherwise
"""
from __future__ import annotations

import re
from collections import Counter

from db.models import Item

from .resume_parser import load_taxonomy
from .text_utils import clean_html, normalize_unicode, tokenize

# Small built-in stopword list. Deliberately stays under ~120 entries —
# we do not want to filter out real signal like "team", "data", "build".
STOPWORDS = frozenset(
    {
        "a", "an", "the", "and", "or", "but", "if", "while", "because",
        "as", "until", "of", "at", "by", "for", "with", "about", "against",
        "between", "into", "through", "during", "before", "after", "above",
        "below", "to", "from", "up", "down", "in", "out", "on", "off", "over",
        "under", "again", "further", "then", "once",
        "is", "am", "are", "was", "were", "be", "been", "being",
        "have", "has", "had", "having",
        "do", "does", "did", "doing", "done",
        "can", "could", "may", "might", "must", "shall", "should", "will",
        "would", "ll",
        "i", "me", "my", "myself", "we", "us", "our", "ours", "ourselves",
        "you", "your", "yours", "yourself", "yourselves",
        "he", "him", "his", "himself", "she", "her", "hers", "herself",
        "it", "its", "itself", "they", "them", "their", "theirs",
        "themselves",
        "this", "that", "these", "those",
        "what", "which", "who", "whom", "whose", "where", "when", "why",
        "how", "there", "here",
        "any", "all", "each", "every", "both", "few", "many", "more",
        "most", "other", "another", "some", "such", "no", "nor", "not",
        "only", "own", "same", "so", "than", "too", "very", "just",
        "even", "also", "well",
        "etc", "ie", "eg",
        "use", "used", "using", "make", "made", "making", "get", "got",
        "gets", "getting",
        "via", "per", "vs",
    }
)

_CAP_PHRASE_RE = re.compile(
    r"\b[A-Z][A-Za-z+#]+(?:\s+[A-Z][A-Za-z+#]+)+\b"
)


def _capitalized_phrase_tokens(text: str) -> set[str]:
    """Tokens that participated in any 2+-word capitalized phrase.

    Detected on the cleaned text *before* lowercasing — once tokenize()
    runs we lose the casing signal.
    """
    out: set[str] = set()
    for m in _CAP_PHRASE_RE.finditer(text):
        for word in m.group().lower().split():
            out.add(word)
    return out


def _taxonomy_terms() -> set[str]:
    tax = load_taxonomy()
    terms: set[str] = set()
    for category in (tax.get("skills") or {}).values():
        terms.update(category)
    terms.update(tax.get("roles") or [])
    return terms


def extract_keywords(item: Item, top_n: int = 25) -> list[dict]:
    cleaned = normalize_unicode(clean_html(item.body or ""))
    cap_tokens = _capitalized_phrase_tokens(cleaned)

    tokens = tokenize(cleaned)
    tokens = [t for t in tokens if t not in STOPWORDS]

    counts = Counter(tokens)
    taxonomy = _taxonomy_terms()

    scored: list[dict] = []
    for term, freq in counts.items():
        if term in taxonomy:
            importance = 2.0
        elif term in cap_tokens:
            importance = 1.5
        else:
            importance = 1.0
        scored.append(
            {"term": term, "frequency": freq, "importance": importance}
        )

    scored.sort(
        key=lambda x: (x["frequency"] * x["importance"], x["frequency"]),
        reverse=True,
    )
    return scored[:top_n]
