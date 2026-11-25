"""The Rust engine's frame (docs/reports/PHASE2_FRAME.md).

Phase 2 of the Rust rewrite moves the indexing work that does not depend on
a language into the extension: finding the files, hashing them, telling what
changed, the folder structure and the File nodes. It lands in steps behind
`ENABLED`, which stays off until the last one. Until then the indexer never
calls the frame, and tests hold each part to the Python code it replaces.

The extension's `Engine` (rust/cgr_graph_rs/src/engine) runs its work on a
thread pool of its own, with the interpreter lock released, and returns
results in the order of its inputs whatever the number of threads.
"""

from __future__ import annotations

from collections.abc import Sequence
from functools import cache
from pathlib import Path
from typing import Protocol

from .constants.embedded_graph import EG_RUST_ENGINE
from .embedded_graph.extension import rust_extension

# Whether the indexer uses the frame. Off until phase 2's last step; the
# parity tests compare the frame with the Python engine either way.
ENABLED: bool = False


class Engine(Protocol):
    @property
    def threads(self) -> int: ...

    def md5_files(self, paths: Sequence[Path]) -> list[str | None]: ...


def new_engine(threads: int = 0) -> Engine | None:
    """An engine on `threads` worker threads (0: one per core), or None when
    the extension is from before the frame."""
    engine_type = getattr(rust_extension(), EG_RUST_ENGINE, None)
    return None if engine_type is None else engine_type(threads)


@cache
def engine() -> Engine | None:
    """The process's engine, one thread per core, made on first use."""
    return new_engine()
