"""The Rust extension (`rust/cgr_graph_rs`), imported in one place.

cgr does not run without it: importing `codebase_rag` fails when it is
missing. `require_rust_extension` is for code with no Python implementation
at all; `rust_extension` still serves the engine parts that keep one for the
inputs their Rust half declines.
"""

from __future__ import annotations

from functools import cache
from importlib import import_module
from types import ModuleType

from .. import exceptions as ex
from ..constants.embedded_graph import EG_RUST_MODULE


@cache
def rust_extension() -> ModuleType | None:
    try:
        return import_module(EG_RUST_MODULE)
    except ImportError:
        return None


def require_rust_extension() -> ModuleType:
    """The extension, or ImportError saying how to get it."""
    try:
        return import_module(EG_RUST_MODULE)
    except ImportError as error:
        raise ImportError(ex.RUST_EXTENSION_MISSING.format(error=error)) from error
