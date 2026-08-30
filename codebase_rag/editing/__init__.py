"""Graph-aware editing primitives: span-preserving patchers (issue #1529) and
transactions (issue #1528)."""

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
from .transaction import (
    EditTransaction,
    StagedFile,
    StagedTree,
    TransactionConflict,
    TransactionError,
    TransactionOutcome,
    VerificationResult,
    transaction,
    undo_last,
)

__all__ = [
    "EditTransaction",
    "PatchResult",
    "Patcher",
    "PatcherError",
    "SpanEdit",
    "StagedFile",
    "StagedTree",
    "TransactionConflict",
    "TransactionError",
    "TransactionOutcome",
    "VerificationResult",
    "apply_span_edits",
    "byte_to_line_col",
    "formatter_check",
    "line_col_to_byte",
    "transaction",
    "undo_last",
]
