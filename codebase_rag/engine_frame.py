"""The Rust engine's frame (docs/reports/PHASE2_FRAME.md).

Phase 2 of the Rust rewrite moves the indexing work that does not depend on
a language into the extension: finding the files, hashing them, telling what
changed, the folder structure and the File nodes. It lands in steps behind
`ENABLED`, which stays off until the last one. Until then the indexer never
calls the frame, and tests hold each part to the Python code it replaces.

The extension's `Engine` (rust/cgr_graph_rs/src/engine) runs its work on a
thread pool of its own, with the interpreter lock released, and returns
results in the order of its inputs whatever the number of threads. Its
`IgnoreRules` (src/engine/ignore.rs) are the walk's ignore rules, a port of
`utils/path_utils.py`'s matching and of pathspec's patterns.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from functools import cache
from pathlib import Path
from typing import Protocol

from .constants.embedded_graph import EG_RUST_ENGINE, EG_RUST_IGNORE_RULES
from .embedded_graph.extension import rust_extension

# Whether the indexer uses the frame. Off until phase 2's last step; the
# parity tests compare the frame with the Python engine either way.
ENABLED: bool = False


class Engine(Protocol):
    @property
    def threads(self) -> int: ...

    def md5_files(self, paths: Sequence[Path]) -> list[str | None]: ...


class IgnoreRules(Protocol):
    """The ignore rules over one exclude set and one unignore set.

    Each method answers what the `utils/path_utils.py` function of its name
    answers when given those sets as `exclude_paths` and `unignore_paths`,
    and raises `ValueError` where that function raises on a pattern pathspec
    refuses. `should_skip_rel_path` is `should_skip_path` for the path's
    relative POSIX spelling, once it is known to resolve inside the root.
    """

    def matches_exclude(self, rel_path: str) -> bool: ...

    def matches_unignore(self, rel_path: str) -> bool: ...

    def should_keep_dir(self, dirname: str, dir_prefix: str) -> bool: ...

    def should_skip_rel_file(self, rel_path: str, dir_parts: Sequence[str]) -> bool: ...

    def should_skip_rel_path(self, rel_path: str, is_file: bool) -> bool: ...

    def unignore_names_this_file(self, rel_path: str) -> bool: ...

    def unignore_could_match_within(self, rel_dir: str) -> bool: ...

    def unignore_names_vcs_dir(self, rel_dir: str) -> bool: ...


def new_engine(threads: int = 0) -> Engine | None:
    """An engine on `threads` worker threads (0: one per core), or None when
    the extension is from before the frame."""
    engine_type = getattr(rust_extension(), EG_RUST_ENGINE, None)
    return None if engine_type is None else engine_type(threads)


@cache
def engine() -> Engine | None:
    """The process's engine, one thread per core, made on first use."""
    return new_engine()


def new_ignore_rules(
    exclude: Iterable[str] | None = None, unignore: Iterable[str] | None = None
) -> IgnoreRules | None:
    """The frame's ignore rules for `exclude` and `unignore`, or None when the
    extension is from before them."""
    rules_type = getattr(rust_extension(), EG_RUST_IGNORE_RULES, None)
    return None if rules_type is None else rules_type(exclude, unignore)
