"""Code Graph RAG: build a queryable graph of a codebase and reason over it."""

from . import tree_sitter_point as _tree_sitter_point  # noqa: F401
from .embedded_graph.extension import require_rust_extension as _require_rust_extension

# The Rust extension does the work behind this package's interface, and
# there is no pure-Python stand-in: without it, nothing here runs.
_require_rust_extension()
