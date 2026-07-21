# Constants for the ast-grep structural search/replace toolkit (#415).
from __future__ import annotations

import re

from .languages import SupportedLanguage

# Optional dependency module name for the dependency guard.
MODULE_AST_GREP = "ast_grep_py"

# cgr language -> ast-grep language id. scala and dart are intentionally
# absent: ast-grep ships no grammar for them, so files in those languages
# are skipped by the tools rather than crashing the Rust binding.
AST_GREP_LANGUAGES: dict[SupportedLanguage, str] = {
    SupportedLanguage.PYTHON: "python",
    SupportedLanguage.JS: "javascript",
    SupportedLanguage.TS: "typescript",
    SupportedLanguage.TSX: "tsx",
    SupportedLanguage.RUST: "rust",
    SupportedLanguage.GO: "go",
    SupportedLanguage.JAVA: "java",
    SupportedLanguage.C: "c",
    SupportedLanguage.CPP: "cpp",
    SupportedLanguage.PHP: "php",
    SupportedLanguage.LUA: "lua",
    SupportedLanguage.CSHARP: "csharp",
}

# Metavariable tokens in a rewrite template. ast-grep's node.replace() does
# NOT interpolate metavars, so the service does it: $$$NAME (multi) and
# $NAME (single). One combined pattern, matched left-to-right in a single
# pass so text inserted for one metavar is never re-scanned for another.
AST_GREP_METAVAR_RE = re.compile(r"\$\$\$([A-Z_][A-Z0-9_]*)|\$([A-Z_][A-Z0-9_]*)")

# Cap on matches returned by a single structural search, so an over-broad
# pattern cannot flood the agent context. Truncation is reported, not silent.
AST_GREP_MAX_RESULTS = 200

# Result dict keys (StructuralSearchMatch / StructuralReplaceChange).
STRUCT_KEY_FILE = "file"
STRUCT_KEY_LINE = "line"
STRUCT_KEY_COLUMN = "column"
STRUCT_KEY_END_LINE = "end_line"
STRUCT_KEY_END_COLUMN = "end_column"
STRUCT_KEY_TEXT = "text"
STRUCT_KEY_MATCHES = "matches"
STRUCT_KEY_DIFF = "diff"
STRUCT_KEY_APPLIED = "applied"

# User-facing messages.
AST_GREP_NOT_AVAILABLE = (
    "Structural search/replace is not available. Install with: uv sync --extra ast-grep"
)
AST_GREP_NO_MATCHES = "No structural matches for pattern: {pattern}"
AST_GREP_UNKNOWN_LANGUAGE = (
    "Unknown or unsupported language '{language}'. Supported: {supported}"
)
AST_GREP_INVALID_PATTERN = "Invalid ast-grep pattern '{pattern}': {error}"
AST_GREP_TRUNCATED = (
    "Result cap of {limit} reached; narrow the pattern or raise the limit for more."
)
AST_GREP_DRY_RUN_HEADER = "Dry run: {count} file(s) would change. No files written."
AST_GREP_APPLIED_HEADER = "Applied: rewrote {count} file(s)."

# Approval-prompt display for a structural rewrite.
AST_GREP_APPROVAL_HEADER = "Structural replace"
AST_GREP_APPROVAL_PATTERN = "pattern:  {pattern}"
AST_GREP_APPROVAL_REWRITE = "rewrite:  {rewrite}"
AST_GREP_APPROVAL_DRY_RUN = "dry_run:  {dry_run}"
