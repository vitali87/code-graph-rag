from __future__ import annotations

from tree_sitter import Node

from .. import constants as cs
from .cpp import utils as cpp_utils

# (H) Once inside a function body the declaration is a local, not a module-level
# (H) export, so an `export` ancestor beyond this boundary must not count.
_JS_TS_EXPORT_STOP_TYPES = frozenset({cs.TS_STATEMENT_BLOCK})
_JAVA_PUBLIC_MODIFIERS = frozenset(
    {cs.JAVA_MODIFIER_PUBLIC, cs.JAVA_MODIFIER_PROTECTED}
)
_PY_FUNCTION_SCOPES = frozenset({cs.TS_PY_FUNCTION_DEFINITION, cs.TS_PY_LAMBDA})


def is_exported(node: Node, name: str, language: cs.SupportedLanguage) -> bool:
    # (H) Whether a function/method is part of its module's public API surface.
    # (H) Public symbols seed dead-code reachability roots, so this follows each
    # (H) language's real visibility rule rather than a heuristic; unmodelled
    # (H) languages stay conservative (False) as before.
    match language:
        case cs.SupportedLanguage.PYTHON:
            return _python_exported(node, name)
        case cs.SupportedLanguage.GO:
            return _go_exported(name)
        case lang if lang in cs.JS_TS_LANGUAGES:
            return _js_ts_exported(node, name)
        case cs.SupportedLanguage.JAVA:
            return _java_exported(node)
        case cs.SupportedLanguage.RUST:
            return _rust_exported(node)
        case cs.SupportedLanguage.CPP:
            return cpp_utils.is_exported(node)
        case _:
            return False


def _python_exported(node: Node, name: str) -> bool:
    # (H) A function/class nested inside a function is a local closure, never public
    # (H) API, so it is not a reachability root regardless of its name; it is reached
    # (H) only through its enclosing scope. Only module-level definitions and class
    # (H) members (a method's ancestor chain has no enclosing function) are public.
    if _python_nested_in_function(node):
        return False
    if name.startswith(cs.PY_NAME_DUNDER) and name.endswith(cs.PY_NAME_DUNDER):
        return True
    return not name.startswith(cs.PY_NAME_UNDERSCORE)


def _python_nested_in_function(node: Node) -> bool:
    parent = node.parent
    while parent is not None:
        if parent.type in _PY_FUNCTION_SCOPES:
            return True
        parent = parent.parent
    return False


def _go_exported(name: str) -> bool:
    return bool(name) and name[0].isupper()


def _js_ts_exported(node: Node, name: str) -> bool:
    # (H) A `private` class member is never public API, even inside an exported
    # (H) class, so it must not seed a reachability root. `protected` stays
    # (H) exported: it is an inheritance surface reachable from other modules,
    # (H) matching the Java rule and staying conservative against false dead-flags.
    if _js_ts_private_member(node):
        return False
    # (H) Two export forms: the declaration wrapped by `export` (caught by the
    # (H) ancestor walk), and a separate `export { name }` / `export { x as y }`
    # (H) list elsewhere in the module, which does not wrap the declaration and so
    # (H) must be matched by name against the module's export clauses.
    if _has_export_ancestor(node):
        return True
    return bool(name) and name in _module_export_list_names(node)


def _js_ts_private_member(node: Node) -> bool:
    # (H) TypeScript marks privacy with an `accessibility_modifier` (`private`),
    # (H) while the ECMAScript form is a `#name` method whose name node is a
    # (H) `private_property_identifier`; both are private regardless of an
    # (H) exported enclosing class.
    if any(c.type == cs.TS_PRIVATE_PROPERTY_IDENTIFIER for c in node.children):
        return True
    modifier = next(
        (c for c in node.children if c.type == cs.TS_ACCESSIBILITY_MODIFIER), None
    )
    return modifier is not None and any(
        c.type == cs.TS_PRIVATE for c in modifier.children
    )


def _has_export_ancestor(node: Node) -> bool:
    current = node.parent
    while current is not None:
        if current.type == cs.TS_EXPORT_STATEMENT:
            return True
        if current.type in _JS_TS_EXPORT_STOP_TYPES:
            return False
        current = current.parent
    return False


def _module_export_list_names(node: Node) -> set[str]:
    # (H) Local names exported by module-level `export { ... }` / `export default`
    # (H) statements. `export { local as exported }` still makes `local` reachable,
    # (H) so the specifier's local name (its first identifier) is what counts.
    # (H) Rescanned per declaration; fine for real files, since a module rarely has
    # (H) enough top-level export statements for the linear scan to matter.
    root = node
    while root.parent is not None:
        root = root.parent
    names: set[str] = set()
    for statement in root.children:
        if statement.type != cs.TS_EXPORT_STATEMENT:
            continue
        # (H) A re-export (`export { x } from './y'`) names another module's
        # (H) symbol, not this file's declaration, so skip clauses with a source.
        if any(child.type == cs.TS_STRING for child in statement.children):
            continue
        clause = next(
            (c for c in statement.children if c.type == cs.TS_EXPORT_CLAUSE), None
        )
        if clause is not None:
            for specifier in clause.children:
                if specifier.type != cs.TS_EXPORT_SPECIFIER:
                    continue
                local = next(
                    (c for c in specifier.children if c.type == cs.TS_IDENTIFIER), None
                )
                if local is not None:
                    names.add(local.text.decode())
            continue
        if any(c.type == cs.TS_EXPORT_DEFAULT for c in statement.children):
            ident = next(
                (c for c in statement.children if c.type == cs.TS_IDENTIFIER), None
            )
            if ident is not None:
                names.add(ident.text.decode())
    return names


def _java_exported(node: Node) -> bool:
    modifiers = next((c for c in node.children if c.type == cs.TS_MODIFIERS), None)
    if modifiers is None:
        return False
    return any(c.type in _JAVA_PUBLIC_MODIFIERS for c in modifiers.children)


def _rust_exported(node: Node) -> bool:
    return any(c.type == cs.TS_PHP_VISIBILITY_MODIFIER for c in node.children)
