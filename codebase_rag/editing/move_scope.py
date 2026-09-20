"""Parsing and import-statement helpers for move planning."""

from __future__ import annotations

import ast
import re
from pathlib import Path

from tree_sitter import Node

from .. import constants as cs
from .imports import (
    _JS_NAMED,
    _JS_SPEC,
    _PY_IMPORT,
    ImportSite,
    _local_name,
    _match_py_from,
    _relative_specifier,
    _split_names,
)
from .move_types import _Cut

_IDENTIFIER = r"(?<![\w.])%s(?!\w)"
_JS_LANGUAGES = frozenset({cs.SupportedLanguage.JS, cs.SupportedLanguage.TS})
_WRAPPERS = frozenset({cs.TS_PY_DECORATED_DEFINITION, cs.TS_EXPORT_STATEMENT})
_IMPORT_TYPES = frozenset(
    {cs.TS_PY_IMPORT_STATEMENT, cs.TS_PY_IMPORT_FROM_STATEMENT, cs.TS_IMPORT_STATEMENT}
)


def _text(node: Node | None) -> str:
    if node is None or node.text is None:
        return ""
    return node.text.decode(cs.ENCODING_UTF8, errors="replace")


def _module_bound(text: str, module: str) -> bool:
    """Whether `text` binds `module`'s root name via a plain `import`.

    The move rewrites call sites to `pkg.new.helper(...)`, which needs
    `pkg` bound. Only a plain `import pkg.new` does that -- or a deeper
    `import pkg.new.sub`, which binds `pkg` just the same (verified
    against a real interpreter; it must keep answering True).

    Deciding this by searching the raw text was wrong in the direction
    that breaks code. `import pkg.new as n` binds only `n`; a comment or
    a string containing the words binds nothing. Each made the caller
    skip adding the real import AFTER the call sites had been rewritten,
    and both versions parse, so the postcondition could not catch it
    either -- the program died at runtime with
    `NameError: name 'pkg' is not defined` on the line the move wrote.

    Unparseable input answers False: the caller then adds an import it
    may not need, which is recoverable, rather than omitting one it does.
    """
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return False
    wanted = module.split(".")
    for node in ast.walk(tree):
        if not isinstance(node, ast.Import):
            continue
        for alias in node.names:
            # `import x as y` binds only `y`, never x's root.
            if alias.asname is None and alias.name.split(".")[: len(wanted)] == wanted:
                return True
    return False


def _uses(text: str, name: str) -> bool:
    return re.search(_IDENTIFIER % re.escape(name), text) is not None


def _definition_at(root: Node, line: int, col: int) -> Node | None:
    """The outermost definition whose own name token starts at (line, col)."""
    stack = [root]
    while stack:
        node = stack.pop()
        named = node.child_by_field_name(cs.FIELD_NAME)
        if (
            named is not None
            and named.start_point == (line - 1, col)
            and (node.type not in (cs.TS_IDENTIFIER, cs.TS_PY_IDENTIFIER))
        ):
            return node
        if node.start_point[0] <= line - 1 <= node.end_point[0]:
            stack.extend(reversed(node.children))
    return None


def _cut_span(source: bytes, node: Node) -> _Cut:
    """Whole lines of the definition plus decorators, export and comments."""
    target = node
    if target.parent is not None and target.parent.type in _WRAPPERS:
        target = target.parent
    first = target
    sibling = target.prev_named_sibling
    while (
        sibling is not None
        and sibling.type == cs.TS_COMMENT
        and sibling.end_point[0] + 1 == first.start_point[0]
    ):
        first = sibling
        sibling = sibling.prev_named_sibling
    start = source.rfind(b"\n", 0, first.start_byte) + 1
    end = source.find(b"\n", target.end_byte)
    end = len(source) if end < 0 else end + 1
    text = source[start:end].decode(cs.ENCODING_UTF8, errors="replace")
    # Swallow the blank lines that separated it from what follows.
    while source[end : end + 1] == b"\n":
        end += 1
    return _Cut(start, end, text)


def _import_block_end(source: bytes, root: Node) -> int:
    """Byte offset just after the last top-level import (0 when none)."""
    end = 0
    for child in root.children:
        if child.type in _IMPORT_TYPES:
            end = source.find(b"\n", child.end_byte)
            end = len(source) if end < 0 else end + 1
    return end


def _strip_project(qn: str, project: str) -> str:
    prefix = f"{project}{cs.SEPARATOR_DOT}"
    return qn[len(prefix) :] if qn.startswith(prefix) else qn


# --- statement helpers -----------------------------------------------------------------


def _statement_text(source: bytes, site: ImportSite) -> str:
    from .patcher import line_col_to_byte

    start = line_col_to_byte(source, site.line, site.col)
    end = line_col_to_byte(source, site.end_line, site.end_col)
    return source[start:end].decode(cs.ENCODING_UTF8, errors="replace")


def _narrow_statement(
    statement: str,
    alias: str,
    language: cs.SupportedLanguage | None,
    old_path: str,
    new_path: str,
) -> str | None:
    """The statement reduced to the entry binding `alias`, respelled for
    the new file where the specifier is relative."""
    if language in _JS_LANGUAGES:
        spec = _JS_SPEC.search(statement)
        if spec is None:
            return None
        text = statement
        if spec.group("spec").startswith("."):
            target = (Path(old_path).parent / spec.group("spec")).as_posix()
            text = (
                statement[: spec.start("spec")]
                + _relative_specifier(new_path, target)
                + statement[spec.end("spec") :]
            )
        named = _JS_NAMED.search(text)
        if named is None:
            return text.strip()
        entries = [e.strip() for e in named.group("names").split(",") if e.strip()]
        kept = [e for e in entries if _local_name(e) == alias]
        if not kept:
            return None
        return (
            text[: named.start()] + "{ " + ", ".join(kept) + " }" + text[named.end() :]
        ).strip()
    if parsed := _match_py_from(statement):
        # main replaced the _PY_FROM regex with a token parser returning
        # (lead, module, mid, names); only those two fields are needed here.
        _lead, module, _mid, raw_names = parsed
        entries, _open, _close = _split_names(raw_names)
        kept = [e for e in entries if _local_name(e) == alias]
        if not kept:
            return None
        return f"from {module} import {kept[0]}"
    if _PY_IMPORT.match(statement):
        return statement.strip()
    return statement.strip()
