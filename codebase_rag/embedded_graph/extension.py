"""The optional Rust extension (`rust/cgr_graph_rs`), imported in one place.

Each part of the engine it accelerates (procedures, the Cypher parser) asks
here and keeps its Python implementation for when the module is missing, or
is a build that predates the function it wants.
"""

from __future__ import annotations

from functools import cache
from importlib import import_module
from types import ModuleType

from ..constants.embedded_graph import EG_RUST_MODULE


@cache
def rust_extension() -> ModuleType | None:
    try:
        return import_module(EG_RUST_MODULE)
    except ImportError:
        return None
