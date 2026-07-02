from __future__ import annotations

from tree_sitter import Node

from .. import constants as cs
from .cpp import utils as cpp_utils

# (H) Once inside a function body the declaration is a local, not a module-level
# (H) export, so an `export` ancestor beyond this boundary must not count.
_JS_TS_EXPORT_STOP_TYPES = frozenset({cs.TS_STATEMENT_BLOCK})
_JAVA_PUBLIC_MODIFIERS = frozenset(
    {cs.JAVA_MODIFIER_PUBLIC, cs.JAVA_VISIBILITY_PROTECTED}
)


def is_exported(node: Node, name: str, language: cs.SupportedLanguage) -> bool:
    # (H) Whether a function/method is part of its module's public API surface.
    # (H) Public symbols seed dead-code reachability roots, so this follows each
    # (H) language's real visibility rule rather than a heuristic; unmodelled
    # (H) languages stay conservative (False) as before.
    match language:
        case cs.SupportedLanguage.PYTHON:
            return _python_exported(name)
        case cs.SupportedLanguage.GO:
            return _go_exported(name)
        case lang if lang in cs.JS_TS_LANGUAGES:
            return _has_export_ancestor(node)
        case cs.SupportedLanguage.JAVA:
            return _java_exported(node)
        case cs.SupportedLanguage.RUST:
            return _rust_exported(node)
        case cs.SupportedLanguage.CPP:
            return cpp_utils.is_exported(node)
        case _:
            return False


def _python_exported(name: str) -> bool:
    if name.startswith(cs.PY_NAME_DUNDER) and name.endswith(cs.PY_NAME_DUNDER):
        return True
    return not name.startswith(cs.PY_NAME_UNDERSCORE)


def _go_exported(name: str) -> bool:
    return bool(name) and name[0].isupper()


def _has_export_ancestor(node: Node) -> bool:
    current = node.parent
    while current is not None:
        if current.type == cs.TS_EXPORT_STATEMENT:
            return True
        if current.type in _JS_TS_EXPORT_STOP_TYPES:
            return False
        current = current.parent
    return False


def _java_exported(node: Node) -> bool:
    modifiers = next((c for c in node.children if c.type == cs.TS_MODIFIERS), None)
    if modifiers is None:
        return False
    return any(c.type in _JAVA_PUBLIC_MODIFIERS for c in modifiers.children)


def _rust_exported(node: Node) -> bool:
    return any(c.type == cs.TS_PHP_VISIBILITY_MODIFIER for c in node.children)
