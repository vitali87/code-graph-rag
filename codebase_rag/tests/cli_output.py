"""The plain text of what a `cgr` run printed, for tests to assert on."""

from __future__ import annotations

import re

# Every CSI escape sequence, as click's `unstyle` strips them: rich styles its
# output whenever it detects a capable sink, CI logs among them.
_ANSI_RE = re.compile(r"\x1b\[[;?0-9]*[a-zA-Z]")


def unstyle(text: str) -> str:
    return _ANSI_RE.sub("", text)
