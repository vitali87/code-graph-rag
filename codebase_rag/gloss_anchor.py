"""The text-quote anchor of a Gloss (issue #1808, stage five).

A note records its subject's `anchor_hash` so a same-name move can be
followed by content, but the hash keeps the definition's name (a signature
change is when a note should go stale), so a RENAME reads as no match. This
is the tier below it: a digest of the definition's own text with its name
masked out and whitespace normalised, plus digests of the non-blank lines
either side of it. A renamed definition keeps its quote; only the body
matters. The prefix and suffix are tie-breakers between definitions whose
bodies are identical (`return None` is a common body), never a match on
their own.

Digests rather than the text itself, the SonarQube precedent (issues are
tracked across scans by a line hash "excluding white spaces"): a note on a
long function would otherwise carry a copy of it, and comparing is all this
tier does. Versioned, so a later format is never compared to this one.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from typing import NamedTuple

from . import constants as cs

# (project name, path relative to that project's root) -> the file's text,
# or None when the caller cannot read it (another project's checkout, a file
# gone from disk). Scoped by project so a note about one project is never
# matched against a same-named path under another's root.
SourceReader = Callable[[str, str], str | None]

_MASK = "\x00"


class TextAnchor(NamedTuple):
    quote: str
    prefix: str
    suffix: str


def _digest(text: str) -> str:
    return f"{cs.ANCHOR_QUOTE_VERSION}{hashlib.sha256(text.encode(cs.ENCODING_UTF8)).hexdigest()}"


def _normalised(text: str, name: str | None) -> str:
    # The name is masked as a whole token so `run` inside `rerun` survives.
    masked = re.sub(rf"\b{re.escape(name)}\b", _MASK, text) if name else text
    return " ".join(masked.split())


def text_anchor(
    source: str, name: str | None, start_line: int, end_line: int
) -> TextAnchor | None:
    """The anchor of the definition on `start_line`..`end_line` (1-based,
    inclusive), or None when the span does not fit the source."""
    lines = source.splitlines()
    if not (1 <= start_line <= end_line <= len(lines)):
        return None
    before = [line for line in lines[: start_line - 1] if line.strip()]
    after = [line for line in lines[end_line:] if line.strip()]
    return TextAnchor(
        quote=_digest(_normalised("\n".join(lines[start_line - 1 : end_line]), name)),
        prefix=_digest(
            _normalised("\n".join(before[-cs.ANCHOR_CONTEXT_LINES :]), name)
        ),
        suffix=_digest(_normalised("\n".join(after[: cs.ANCHOR_CONTEXT_LINES]), name)),
    )


def is_comparable_quote(value: object) -> bool:
    return isinstance(value, str) and value.startswith(cs.ANCHOR_QUOTE_VERSION)
