"""Replace a file in one step, so no reader sees it half written.

A plain `write_text` truncates the file first and writes after, so a run
stopped in between, or a second process reading meanwhile, finds an empty or
partial file. Writing a sibling and renaming it over the target with
`os.replace`, atomic within a filesystem, leaves the previous file or the new
one, never a part of either.
"""

from __future__ import annotations

import contextlib
import os
import secrets
import stat
from pathlib import Path

from .. import constants as cs

__all__ = ["open_temp_sibling", "write_bytes_atomic", "write_text_atomic"]

# Bounded so a directory that somehow rejects every candidate ends the write
# instead of spinning; with 8 random bytes a collision is already vanishingly
# unlikely, so more than a few attempts would only mask a real filesystem
# problem.
_TEMP_NAME_ATTEMPTS = 8
# The mode `open(path, "w")` creates a file with, before the umask applies.
_NEW_FILE_MODE = 0o666


def open_temp_sibling(path: Path, mode: int) -> tuple[int, Path]:
    """A fresh file beside `path`, created exclusively and never through a link.

    `O_EXCL` fails rather than reusing an existing name and `O_NOFOLLOW`
    rather than opening a symlink's target, so neither a guessed name nor a
    planted link can redirect the write; the name is random besides. Named
    `<path name>.<random>.tmp`, the form cgr's state readers skip.
    """
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_BINARY", 0)
    )
    for _ in range(_TEMP_NAME_ATTEMPTS):
        candidate = path.with_name(
            f"{path.name}.{secrets.token_hex(8)}{cs.TMP_EXTENSION}"
        )
        try:
            return os.open(candidate, flags, mode), candidate
        except FileExistsError:
            continue
    raise OSError(f"could not create a temporary file beside {path}")


def write_bytes_atomic(path: Path, data: bytes) -> None:
    """Make `data` the content of `path` in one step.

    The file keeps its permission bits, and a new one gets the mode a plain
    write would give it. A symlink at `path` is replaced, not written
    through. On failure the previous file is left as it was and OSError
    propagates.
    """
    descriptor, temp = open_temp_sibling(path, _NEW_FILE_MODE)
    try:
        with os.fdopen(descriptor, "wb") as out:
            out.write(data)
        with contextlib.suppress(FileNotFoundError):
            os.chmod(temp, stat.S_IMODE(os.stat(path).st_mode))
        os.replace(temp, path)
    finally:
        # Gone already once the replace has run; otherwise the write failed.
        with contextlib.suppress(OSError):
            temp.unlink(missing_ok=True)


def write_text_atomic(path: Path, text: str) -> None:
    """`write_bytes_atomic` of `text` in UTF-8, its newlines written as
    `Path.write_text` writes them (`os.linesep`)."""
    if os.linesep != cs.LINE_FEED:
        text = text.replace(cs.LINE_FEED, os.linesep)
    write_bytes_atomic(path, text.encode(cs.ENCODING_UTF8))
