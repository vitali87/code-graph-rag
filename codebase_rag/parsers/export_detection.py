from __future__ import annotations

from tree_sitter import Node

from .. import constants as cs
from .cpp import utils as cpp_utils

# Once inside a function body the declaration is a local, not a module-level
# export, so an `export` ancestor beyond this boundary must not count.
_JS_TS_EXPORT_STOP_TYPES = frozenset({cs.TS_STATEMENT_BLOCK})
# Textual markers whose presence in a top-level statement makes a JS file a
# CommonJS module rather than a classic page-scope script.
_JS_REQUIRE_CALL = (cs.JS_REQUIRE_KEYWORD + cs.CHAR_PAREN_OPEN).encode()
_JS_MODULE_EXPORTS = (
    cs.JS_MODULE_KEYWORD + cs.SEPARATOR_DOT + cs.JS_EXPORTS_KEYWORD
).encode()
_JS_EXPORTS_MEMBER = (cs.JS_EXPORTS_KEYWORD + cs.SEPARATOR_DOT).encode()
# Real function scopes. A bare `{ ... }` statement_block at top level
# (django's core.js) is NOT one: prototype mutations and var/function
# declarations inside it still land in page scope, so only a function
# ancestor makes a script declaration local.
_JS_TS_FUNCTION_SCOPE_TYPES = frozenset(
    {
        cs.TS_FUNCTION_DECLARATION,
        cs.TS_GENERATOR_FUNCTION_DECLARATION,
        cs.TS_FUNCTION_EXPRESSION,
        cs.TS_ARROW_FUNCTION,
        cs.TS_METHOD_DEFINITION,
    }
)
_JAVA_PUBLIC_MODIFIERS = frozenset(
    {cs.JAVA_MODIFIER_PUBLIC, cs.JAVA_MODIFIER_PROTECTED}
)
_CSHARP_PUBLIC_MODIFIERS = frozenset(
    {
        cs.TS_CSHARP_MODIFIER_PUBLIC,
        cs.TS_CSHARP_MODIFIER_INTERNAL,
        cs.TS_CSHARP_MODIFIER_PROTECTED,
    }
)
_PY_FUNCTION_SCOPES = frozenset({cs.TS_PY_FUNCTION_DEFINITION, cs.TS_PY_LAMBDA})


def is_exported(node: Node, name: str, language: cs.SupportedLanguage) -> bool:
    # Whether a function/method is part of its module's public API surface.
    # Public symbols seed dead-code reachability roots, so this follows each
    # language's real visibility rule rather than a heuristic; unmodelled
    # languages stay conservative (False) as before.
    match language:
        case cs.SupportedLanguage.PYTHON:
            return _python_exported(node, name)
        case cs.SupportedLanguage.GO:
            return _go_exported(name)
        case lang if lang in cs.JS_TS_LANGUAGES:
            return _js_ts_exported(node, name)
        case cs.SupportedLanguage.JAVA:
            return _java_exported(node)
        case cs.SupportedLanguage.CSHARP:
            return _csharp_exported(node)
        case cs.SupportedLanguage.RUST:
            return _rust_exported(node)
        case cs.SupportedLanguage.CPP:
            return cpp_utils.is_exported(node)
        case cs.SupportedLanguage.DART:
            return _dart_exported(node, name)
        case _:
            return False


def _python_exported(node: Node, name: str) -> bool:
    # A function/class nested inside a function is a local closure, never public
    # API, so it is not a reachability root regardless of its name; it is reached
    # only through its enclosing scope. Only module-level definitions and class
    # members (a method's ancestor chain has no enclosing function) are public.
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


_DART_PRIVATE_BYTE = cs.DART_PRIVATE_PREFIX.encode(cs.ENCODING_UTF8)


def _dart_exported(node: Node, name: str) -> bool:
    # Dart visibility is purely lexical: a leading underscore means
    # library-private, everything else is public. A public member is only
    # externally reachable when EVERY enclosing type is also public, since
    # a private class/mixin/extension cannot be named outside its library
    # (`_Internal.doThing` is unreachable from other libraries even though
    # `doThing` has no underscore). An UNNAMED extension (`extension on
    # String {...}`, no name field) is likewise usable only in its
    # declaring library, so its members are private too. Walk the ancestor
    # type chain and treat any private link as private.
    if name.startswith(cs.DART_PRIVATE_PREFIX):
        return False
    ancestor = node.parent
    while ancestor is not None:
        if ancestor.type in cs.DART_TYPE_DECLARATION_NODE_TYPES:
            type_name = ancestor.child_by_field_name(cs.FIELD_NAME)
            if type_name is None or type_name.text is None:
                # only an extension_declaration legitimately lacks a name,
                # and an unnamed one is library-private
                if ancestor.type == cs.TS_DART_EXTENSION_DECLARATION:
                    return False
            elif type_name.text.startswith(_DART_PRIVATE_BYTE):
                return False
        ancestor = ancestor.parent
    return True


def _js_ts_exported(node: Node, name: str) -> bool:
    # A `private` class member is never public API, even inside an exported
    # class, so it must not seed a reachability root. `protected` stays
    # exported: it is an inheritance surface reachable from other modules,
    # matching the Java rule and staying conservative against false dead-flags.
    if _js_ts_private_member(node):
        return False
    # Two export forms: the declaration wrapped by `export` (caught by the
    # ancestor walk), and a separate `export { name }` / `export { x as y }`
    # list elsewhere in the module, which does not wrap the declaration and so
    # must be matched by name against the module's export clauses.
    if _has_export_ancestor(node):
        return True
    if bool(name) and name in _module_export_list_names(node):
        return True
    return _is_script_global(node)


def _is_script_global(node: Node) -> bool:
    # Classic browser script: a JS/TS file with no import/export statement
    # and no CommonJS require/module.exports construct runs in page scope,
    # so every module-level declaration (and its class members) is a global
    # reachable from HTML/templates the graph cannot see (django's
    # OLMapWidget classes, core.js helpers). Function-local declarations
    # are still reached only through their enclosing scope.
    root = node
    current = node.parent
    while current is not None:
        if current.type in _JS_TS_FUNCTION_SCOPE_TYPES:
            return False
        root = current
        current = current.parent
    return not _has_module_construct(root)


# 1-slot memo of the last root's module-construct scan: files are ingested
# sequentially, so consecutive symbols of one file hit the slot and the
# O(top-level statements) scan runs once per FILE instead of once per
# symbol. The slot holds the root Node itself, which keeps its tree alive,
# so the entry can never alias a recycled node address; retaining one
# parse tree is the bounded cost (an unbounded Node-keyed lru_cache would
# pin every cached tree in memory).
_last_script_scan: tuple[Node, bool] | None = None


def _has_module_construct(root: Node) -> bool:
    global _last_script_scan
    if _last_script_scan is not None and _last_script_scan[0] == root:
        return _last_script_scan[1]
    result = any(_is_module_construct(stmt) for stmt in root.children)
    _last_script_scan = (root, result)
    return result


def _is_module_construct(statement: Node) -> bool:
    if statement.type in (cs.TS_IMPORT_STATEMENT, cs.TS_EXPORT_STATEMENT):
        return True
    text = statement.text or b""
    return (
        _JS_REQUIRE_CALL in text
        or _JS_MODULE_EXPORTS in text
        or text.startswith(_JS_EXPORTS_MEMBER)
    )


def _js_ts_private_member(node: Node) -> bool:
    # TypeScript marks privacy with an `accessibility_modifier` (`private`),
    # while the ECMAScript form is a `#name` method whose name node is a
    # `private_property_identifier`; both are private regardless of an
    # exported enclosing class.
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
    # Local names exported by module-level `export { ... }` / `export default`
    # statements. `export { local as exported }` still makes `local` reachable,
    # so the specifier's local name (its first identifier) is what counts.
    # Rescanned per declaration; fine for real files, since a module rarely has
    # enough top-level export statements for the linear scan to matter.
    root = node
    while root.parent is not None:
        root = root.parent
    names: set[str] = set()
    for statement in root.children:
        if statement.type != cs.TS_EXPORT_STATEMENT:
            continue
        # A re-export (`export { x } from './y'`) names another module's
        # symbol, not this file's declaration, so skip clauses with a source.
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


def _csharp_exported(node: Node) -> bool:
    # C# has no modifiers container; visibility is individual `modifier`
    # children. public/internal/protected are external API surface.
    for child in node.children:
        if child.type == cs.TS_CSHARP_MODIFIER and child.text is not None:
            if child.text.decode(cs.ENCODING_UTF8) in _CSHARP_PUBLIC_MODIFIERS:
                return True
    # An explicit interface implementation (`IThing IThing.WithKey(...)`)
    # carries no modifier but is invocable from outside via the interface --
    # API surface (Polly's `IAsyncPolicy.WithPolicyKey`, `IDictionary.Keys`).
    for child in node.children:
        if child.type == cs.TS_CSHARP_EXPLICIT_INTERFACE_SPECIFIER:
            return True
    # An interface member carries no visibility modifier and is implicitly
    # PUBLIC -- it IS the interface's API surface (Polly's
    # IAsyncPolicy.ExecuteAsync overloads, flagged dead without this).
    parent = node.parent
    if (
        parent is not None
        and parent.type == cs.TS_CSHARP_DECLARATION_LIST
        and parent.parent is not None
        and parent.parent.type == cs.TS_CSHARP_INTERFACE_DECLARATION
    ):
        return True
    # With no explicit visibility a TOP-LEVEL type defaults to `internal`
    # (API surface -> exported); a nested type or any member defaults to
    # `private` -> not exported.
    return _is_csharp_top_level_type(node)


def _is_csharp_top_level_type(node: Node) -> bool:
    if node.type not in cs.SPEC_CSHARP_CLASS_TYPES:
        return False
    parent = node.parent
    if parent is None:
        return False
    # A type directly under the file root (file-scoped namespace or no
    # namespace) or under a block namespace's declaration_list is top level;
    # a type whose declaration_list belongs to another TYPE is nested.
    if parent.type == cs.TS_CSHARP_COMPILATION_UNIT:
        return True
    if parent.type == cs.TS_CSHARP_DECLARATION_LIST:
        grandparent = parent.parent
        return (
            grandparent is not None
            and grandparent.type == cs.TS_CSHARP_NAMESPACE_DECLARATION
        )
    return False


def _rust_exported(node: Node) -> bool:
    # Only unrestricted `pub` is an external API root. A restricted visibility
    # (`pub(crate)`, `pub(super)`, `pub(in path)`) is visible only within the
    # crate/module, so an uncalled one is genuinely dead and must not be seeded
    # as a root. Bare `pub` is a lone keyword child; a restriction adds `(...)`.
    if node.type == cs.TS_RS_MACRO_DEFINITION:
        return _rust_macro_exported(node)
    modifier = next(
        (c for c in node.children if c.type == cs.TS_PHP_VISIBILITY_MODIFIER), None
    )
    return modifier is not None and modifier.child_count == 1


def _rust_macro_exported(node: Node) -> bool:
    # macro_rules! takes no `pub`; a preceding #[macro_export] attribute is
    # what publishes it (to the crate root) as library API. Comments (incl.
    # /// doc comments) are named siblings that interleave the attribute and
    # the definition, so skip them.
    prev = node.prev_named_sibling
    while prev is not None and prev.type in (
        cs.TS_RS_ATTRIBUTE_ITEM,
        *cs.RS_COMMENT_TYPES,
    ):
        if (
            prev.type == cs.TS_RS_ATTRIBUTE_ITEM
            and prev.text is not None
            and cs.RS_MACRO_EXPORT_ATTR in prev.text.decode(cs.ENCODING_UTF8)
        ):
            return True
        prev = prev.prev_named_sibling
    return False
