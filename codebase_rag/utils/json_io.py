"""JSON decoding that fails like malformed JSON when the input is too deep.

The stdlib decoder recurses once per nesting level and raises
`RecursionError` -- a `RuntimeError`, not a `ValueError` -- on a document
nested a few thousand levels deep. Every reader of a repository's JSON
(`package.json`, `tsconfig.json`, contracts, cgr's own state files) catches
`ValueError` for a file it cannot use, so one hostile manifest escaped that
handling and aborted the whole index run (#2261). Raising
`json.JSONDecodeError` instead lets each reader's existing fallback apply.
"""

from __future__ import annotations

import json
from typing import IO

from .. import exceptions as ex
from ..types_defs import JsonValue

__all__ = ["load_json", "loads_json", "strip_jsonc"]


def loads_json(text: str) -> JsonValue:
    try:
        return json.loads(text)
    except RecursionError:
        raise json.JSONDecodeError(ex.JSON_TOO_DEEP, text, 0) from None


def load_json(stream: IO[str]) -> JsonValue:
    return loads_json(stream.read())


def strip_jsonc(text: str) -> str:
    """JSON text from JSONC: comments and trailing commas dropped outside strings.

    `tsconfig.json` is JSONC. A regex strip knew nothing of string literals,
    so the `/*` in `"@/*"` or `"**/*.ts"` opened a "comment" that ran to the
    next `*/`, the result was not JSON and the whole config was lost (#3178).
    """
    out: list[str] = []
    comma: int | None = None
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == '"':
            j = i + 1
            while j < n and text[j] != '"':
                j += 2 if text[j] == "\\" else 1
            out.append(text[i : j + 1])
            comma = None
            i = j + 1
            continue
        if text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
            continue
        if text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
            out.append(" ")
            continue
        if c in "}]" and comma is not None:
            out[comma] = ""
        if c == ",":
            comma = len(out)
        elif not c.isspace():
            comma = None
        out.append(c)
        i += 1
    return "".join(out)
