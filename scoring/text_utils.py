"""Pure text utilities — no DB access.

Shared by resume_parser, scorer, and jd_extractor so resume-side and
job-description-side matching stay consistent.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Iterable

from bs4 import BeautifulSoup, NavigableString


# ----- HTML cleaning -----

_BLOCK_TAGS = frozenset(
    {
        "p", "div", "section", "article", "header", "footer",
        "h1", "h2", "h3", "h4", "h5", "h6",
        "li", "ul", "ol",
        "table", "tr", "blockquote", "pre",
    }
)


def _walk(node, output: list[str]) -> None:
    for child in node.children:
        if isinstance(child, NavigableString):
            output.append(str(child))
        elif child.name == "br":
            output.append("\n")
        elif child.name in _BLOCK_TAGS:
            output.append("\n\n")
            _walk(child, output)
            output.append("\n\n")
        else:
            _walk(child, output)


def clean_html(raw: str) -> str:
    """Strip HTML tags, drop script/style content, collapse whitespace.

    Block-level elements are separated by paragraph breaks (``\\n\\n``);
    ``<br>`` becomes a single newline.
    """
    if not raw:
        return ""
    soup = BeautifulSoup(raw, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()

    parts: list[str] = []
    _walk(soup, parts)
    text = "".join(parts)

    text = re.sub(r"[ \t]+", " ", text)
    text = "\n".join(line.strip() for line in text.split("\n"))
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ----- Unicode normalization -----

_SMART_QUOTES = {
    "‘": "'", "’": "'", "‚": "'", "‛": "'",
    "“": '"', "”": '"', "„": '"', "‟": '"',
    "′": "'", "″": '"',
}
_ZERO_WIDTH = ("​", "‌", "‍", "⁠", "﻿")


def normalize_unicode(text: str) -> str:
    """Patch common PDF/scrape glitches and NFKC-normalize."""
    if not text:
        return ""
    text = text.replace("�", "—")
    for src, dst in _SMART_QUOTES.items():
        text = text.replace(src, dst)
    for ch in _ZERO_WIDTH:
        text = text.replace(ch, "")
    return unicodedata.normalize("NFKC", text)


# ----- Tokenization -----

_TOKEN_RE = re.compile(r"[a-z0-9+#/\-]+")
_SHORT_ALLOWLIST = frozenset({"r", "c", "js", "go"})


def tokenize(text: str) -> list[str]:
    """Lowercase tokenize while preserving ``+``, ``#``, ``/``, ``-``.

    So ``c++``, ``c#``, ``a/b``, ``scikit-learn`` come back intact.
    Tokens shorter than 2 chars are dropped unless in the allowlist
    (``r``, ``c``, ``js``, ``go``).
    """
    tokens = _TOKEN_RE.findall(text.lower())
    return [t for t in tokens if len(t) >= 2 or t in _SHORT_ALLOWLIST]


# ----- Term matching -----

def term_pattern(term: str) -> re.Pattern[str]:
    """Word-boundary, case-insensitive regex.

    Uses negative lookarounds instead of ``\\b`` so terms with non-word
    characters (``c++``, ``c#``, ``a/b testing``) match correctly.
    """
    escaped = re.escape(term)
    return re.compile(
        rf"(?<![A-Za-z0-9_]){escaped}(?![A-Za-z0-9_])",
        flags=re.IGNORECASE,
    )


def find_terms(text: str, terms: Iterable[str]) -> list[str]:
    """Subset of ``terms`` whose pattern appears in ``text``."""
    return [t for t in terms if term_pattern(t).search(text)]


def find_term_in_text(term: str, text: str) -> list[int]:
    """Character offsets where ``term`` matches ``text`` at word boundaries."""
    return [m.start() for m in term_pattern(term).finditer(text)]
