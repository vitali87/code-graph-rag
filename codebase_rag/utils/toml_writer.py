"""TOML text exactly as tomli-w wrote it, for cgr's workspace files.

The writing is the Rust extension's (rust/cgr_graph_rs/src/formats/toml.rs),
with tomli-w's defaults (four-space indent, no multi-line strings): a
table's plain values first, then its sub-tables; an array of tables stays
inline unless one of its tables renders longer than a 100-character line,
in which case every one becomes a `[[name]]` section. Strings, booleans,
ints, floats, lists, tuples and mappings are written; anything else raises
`TypeError`.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

from ..constants.embedded_graph import EG_RUST_TOML_DUMPS
from ..embedded_graph.extension import require_rust_extension

__all__ = ["dumps"]

dumps: Callable[[Mapping[str, object]], str] = getattr(
    require_rust_extension(), EG_RUST_TOML_DUMPS
)
