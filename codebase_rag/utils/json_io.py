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

__all__ = ["load_json", "loads_json"]


def loads_json(text: str) -> JsonValue:
    try:
        return json.loads(text)
    except RecursionError:
        raise json.JSONDecodeError(ex.JSON_TOO_DEEP, text, 0) from None


def load_json(stream: IO[str]) -> JsonValue:
    return loads_json(stream.read())
