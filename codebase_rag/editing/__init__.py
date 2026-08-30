"""Graph-aware editing primitives: span-preserving patchers (issue #1529)."""

from .patcher import (
    Patcher,
    PatcherError,
    PatchResult,
    SpanEdit,
    apply_span_edits,
    byte_to_line_col,
    formatter_check,
    line_col_to_byte,
)

__all__ = [
    "PatchResult",
    "Patcher",
    "PatcherError",
    "SpanEdit",
    "apply_span_edits",
    "byte_to_line_col",
    "formatter_check",
    "line_col_to_byte",
]
