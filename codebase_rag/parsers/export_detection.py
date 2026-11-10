from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from itertools import product
from typing import NamedTuple

from tree_sitter import Node

from .. import constants as cs
from .cpp import utils as cpp_utils
from .cpp.overloads import cpp_overload_signature
from .lua import utils as lua_utils
from .utils import safe_decode_text

# Once inside a function body the declaration is a local, not a module-level
# export, so an `export` ancestor beyond this boundary must not count. A
# concise arrow (`() => class {}`) has no block, so the arrow itself is the
# boundary for what its expression body builds.
_JS_TS_EXPORT_STOP_TYPES = frozenset({cs.TS_STATEMENT_BLOCK, cs.TS_ARROW_FUNCTION})
# Textual markers whose presence in a top-level statement makes a JS file a
# CommonJS module rather than a classic page-scope script.
_JS_REQUIRE_CALL = (cs.JS_REQUIRE_KEYWORD + cs.CHAR_PAREN_OPEN).encode()
_JS_MODULE_EXPORTS = (
    cs.JS_MODULE_KEYWORD + cs.SEPARATOR_DOT + cs.JS_EXPORTS_KEYWORD
).encode()
_JS_EXPORTS_MEMBER = (cs.JS_EXPORTS_KEYWORD + cs.SEPARATOR_DOT).encode()
_JS_MODULE_KEYWORD_BYTES = cs.JS_MODULE_KEYWORD.encode()
_JS_EXPORTS_KEYWORD_BYTES = cs.JS_EXPORTS_KEYWORD.encode()
# Declarations that bind the module-level name an export statement refers to.
# A class expression is absent on purpose: `const X = class Y {}` binds X.
_JS_TS_BINDING_DECLARATION_TYPES = frozenset(
    {
        cs.TS_FUNCTION_DECLARATION,
        cs.TS_GENERATOR_FUNCTION_DECLARATION,
        cs.TS_FUNCTION_SIGNATURE,
        cs.TS_CLASS_DECLARATION,
        cs.TS_ABSTRACT_CLASS_DECLARATION,
        cs.TS_INTERFACE_DECLARATION,
        cs.TS_ENUM_DECLARATION,
        cs.TS_TYPE_ALIAS_DECLARATION,
        cs.TS_INTERNAL_MODULE,
        cs.TS_VARIABLE_DECLARATOR,
    }
)
_JS_TS_FIELD_DEFINITION_TYPES = frozenset(
    {cs.TS_PUBLIC_FIELD_DEFINITION, cs.TS_JS_FIELD_DEFINITION}
)
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
# Wrappers that pass their operand's value through unchanged: parentheses and
# TypeScript's assertions (`x as T`, `x satisfies T`, `x!`).
_JS_TS_VALUE_WRAPPER_TYPES = cs.TS_CAST_WRAPPER_TYPES | {cs.TS_PARENTHESIZED_EXPRESSION}
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
_PHP_CALLABLE_SCOPES = frozenset(cs.FQN_PHP_FUNCTION_TYPES)
_PHP_TYPE_DECLARATIONS = frozenset(cs.SPEC_PHP_CLASS_TYPES)
_PHP_NAMED_TYPE_DECLARATIONS = _PHP_TYPE_DECLARATIONS - {cs.TS_PHP_ANONYMOUS_CLASS}
_PHP_PRIVATE_BYTES = cs.PHP_VISIBILITY_PRIVATE.encode(cs.ENCODING_UTF8)


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
        case cs.SupportedLanguage.PHP:
            return _php_exported(node)
        case lang if lang in cs.JS_TS_LANGUAGES:
            return _js_ts_exported(node, name)
        case cs.SupportedLanguage.JAVA:
            return _java_exported(node)
        case cs.SupportedLanguage.CSHARP:
            return _csharp_exported(node)
        case cs.SupportedLanguage.RUST:
            return _rust_exported(node)
        case cs.SupportedLanguage.C:
            return _c_exported(node)
        case cs.SupportedLanguage.CPP:
            # A C++20 module `export`, or ordinary code's public API.
            return cpp_utils.is_exported(node) or _cpp_public_api(node)
        case cs.SupportedLanguage.SCALA:
            return _scala_exported(node)
        case cs.SupportedLanguage.DART:
            return _dart_exported(node, name)
        case cs.SupportedLanguage.LUA:
            return _lua_exported(node, name)
        case cs.SupportedLanguage.SQL:
            # A stored routine is a database object any role with EXECUTE
            # calls by name, from a client, C code or the database itself
            # (issue #3181).
            return True
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


def _php_exported(node: Node) -> bool:
    # PHP has no module privacy: a named class, interface, trait or enum and a
    # function declared outside any callable body are global once their file
    # loads, so code outside the repo can call them (issue #2472). A member
    # is API unless it is `private`; `protected` is the inheritance surface,
    # as for Java and TS. A closure, an anonymous class (with its members),
    # and a function or named type declared inside a callable's body cannot
    # be named from outside until that body runs: the graph's own edges
    # decide whether they are live.
    if node.type == cs.TS_PHP_METHOD_DECLARATION:
        return _php_member_exported(node)
    if node.type == cs.TS_PHP_FUNCTION_DEFINITION:
        return not _php_inside_callable(node)
    return _php_global_type(node)


def _php_global_type(node: Node) -> bool:
    # An `if (!class_exists(...))` block at file level is no callable, so a
    # polyfill class declared in it stays global.
    return node.type in _PHP_NAMED_TYPE_DECLARATIONS and not _php_inside_callable(node)


def _php_member_exported(node: Node) -> bool:
    owner = node.parent
    while owner is not None and owner.type not in _PHP_TYPE_DECLARATIONS:
        owner = owner.parent
    if owner is None or not _php_global_type(owner):
        return False
    # Keywords are case-insensitive in PHP: `PRIVATE function` is private too.
    return not any(
        child.type == cs.TS_PHP_VISIBILITY_MODIFIER
        and (child.text or b"").lower() == _PHP_PRIVATE_BYTES
        for child in node.children
    )


def _php_inside_callable(node: Node) -> bool:
    parent = node.parent
    while parent is not None:
        if parent.type in _PHP_CALLABLE_SCOPES:
            return True
        parent = parent.parent
    return False


_DART_PRIVATE_BYTE = cs.DART_PRIVATE_PREFIX.encode(cs.ENCODING_UTF8)


def _dart_exported(node: Node, name: str) -> bool:
    # Dart visibility is purely lexical: a leading underscore means
    # library-private, everything else public. A public member is externally
    # reachable only when EVERY enclosing type is also public, since a private
    # class/mixin/extension cannot be named outside its library
    # (`_Internal.doThing` is unreachable even though `doThing` has no
    # underscore). An UNNAMED extension (`extension on String {...}`) is
    # likewise usable only in its declaring library, so its members are private
    # too. Walk the ancestor type chain and treat any private link as private.
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
    if _in_commonjs_exported_class(node):
        return True
    # Two export forms: the declaration wrapped by `export` (caught by the
    # ancestor walk), and a separate `export { name }` / `export { x as y }` /
    # `export default x` / CommonJS `module.exports = x` elsewhere in the
    # module, which does not wrap the declaration and so must be matched by
    # name against the module's export statements.
    if _has_export_ancestor(node):
        return True
    if _named_by_module_export(node, name):
        return True
    return _is_script_global(node)


def _named_by_module_export(node: Node, name: str) -> bool:
    # A separate export names a MODULE-LEVEL binding, and everything declared
    # inside that binding's declaration is exported with it: the members of
    # `class X {}` + `export { X }` exactly as `_has_export_ancestor` exports
    # the members of `export class X {}` (honojs/hono's
    # `export { Hono as HonoBase }`, issue #2591). A member's own name never
    # counts, so `export { helper }` cannot root an unrelated `C.helper`. A
    # declaration outside any module-level binding (a function-local) keeps
    # being matched by its own name.
    export_names = _module_export_list_names(node)
    if not export_names:
        return False
    binding = _module_binding_name(node)
    return (name if binding is None else binding) in export_names


def _module_binding_name(node: Node) -> str | None:
    # The outermost binding declaration enclosing `node` at module level, found
    # by the same walk (and the same function-body boundary) as the `export`
    # ancestor rule; None once the walk enters a function body. `node` itself
    # is never a boundary: `const f = () => x` binds the arrow, it does not
    # sit inside one.
    binding = node if node.type in _JS_TS_BINDING_DECLARATION_TYPES else None
    current = node.parent
    while current is not None:
        if current.type in _JS_TS_EXPORT_STOP_TYPES:
            return None
        if current.type in _JS_TS_BINDING_DECLARATION_TYPES:
            binding = current
        current = current.parent
    if binding is None:
        return None
    name_node = binding.child_by_field_name(cs.FIELD_NAME)
    if name_node is None or name_node.text is None:
        return None
    return name_node.text.decode()


def _is_script_global(node: Node) -> bool:
    # Classic browser script: a JS/TS file with no import/export statement
    # and no CommonJS require/module.exports construct runs in page scope,
    # so every module-level declaration (and its class members) is a global
    # reachable from HTML/templates the graph cannot see (django's
    # OLMapWidget classes, core.js helpers). Function-local declarations
    # are still reached only through their enclosing scope.
    root = _module_root(node)
    return root is not None and not _has_module_construct(root)


def _module_root(node: Node) -> Node | None:
    # The tree root when `node` sits at module level, None when a function
    # encloses it. A top-level block (`if (...) {...}`, django's bare `{...}`
    # in core.js) is still module level: it runs at load, while a function
    # body runs only when the function is called.
    root = node
    current = node.parent
    while current is not None:
        if current.type in _JS_TS_FUNCTION_SCOPE_TYPES:
            return None
        root = current
        current = current.parent
    return root


# 1-slot memo of the last root's module-construct scan: files are ingested
# sequentially, so consecutive symbols of one file hit the slot and the
# O(top-level statements) scan runs once per FILE, not per symbol. The slot
# holds the root Node itself, which keeps its tree alive, so the entry can
# never alias a recycled node address; retaining one parse tree is the bounded
# cost (an unbounded Node-keyed lru_cache would pin every cached tree).
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
    if (
        _JS_REQUIRE_CALL in text
        or _JS_MODULE_EXPORTS in text
        or text.startswith(_JS_EXPORTS_MEMBER)
    ):
        return True
    return _JS_EXPORTS_KEYWORD_BYTES in text and _has_commonjs_export(statement)


def _has_commonjs_export(statement: Node) -> bool:
    # An export the textual markers miss (`exports["X"] = ...`,
    # `const X = exports.X = ...`, one in a module-level `if`) makes the file
    # a module too, by the same rule that publishes a class through it.
    # Function bodies are pruned: an export there runs only when called, so
    # that rule rejects it anyway.
    pending = [statement]
    while pending:
        node = pending.pop()
        if node.type in _JS_TS_FUNCTION_SCOPE_TYPES:
            continue
        if (
            node.type == cs.TS_JS_ASSIGNMENT_EXPRESSION
            and _is_top_level_commonjs_export(node)
        ):
            return True
        pending.extend(node.named_children)
    return False


_JS_MODULE_EXPORTS_MEMBER = _JS_MODULE_EXPORTS + cs.SEPARATOR_DOT.encode()


def _in_commonjs_exported_class(node: Node) -> bool:
    # A class expression inside the value of a top-level CommonJS export
    # (`module.exports = class {...}`, `module.exports = [class {...}]`,
    # `exports.Rule = class {...}`) is published by it the way
    # `export default class {...}` publishes its class, and its members follow
    # it (issue #2567). It has no name an export list could match. Only a class
    # counts: the methods of an exported object literal keep today's decision,
    # and a class built inside a function is that function's local.
    in_class = node.type == cs.TS_CLASS_EXPRESSION
    child = node
    current = node.parent
    while current is not None:
        if current.type == cs.TS_JS_ASSIGNMENT_EXPRESSION:
            return (
                in_class
                and current.child_by_field_name(cs.FIELD_RIGHT) == child
                and _is_top_level_commonjs_export(current)
            )
        if (
            current.type in _JS_TS_FUNCTION_SCOPE_TYPES
            or current.type in _JS_TS_EXPORT_STOP_TYPES
        ):
            return False
        if current.type == cs.TS_CLASS_EXPRESSION:
            in_class = True
        child = current
        current = current.parent
    return False


def _is_top_level_commonjs_export(assignment: Node) -> bool:
    # `module.exports = exports.Rule = value` chains assignments, each the
    # value of the next, also through parentheses
    # (`module.exports = (exports.Rule = value)`); any CommonJS target on the
    # chain publishes the value. The outermost assignment must run at load:
    # as a statement or a declarator's value (`const R = module.exports = v`),
    # at module level, a top-level `if` block included. The same assignment
    # inside a function exports nothing until that function is called.
    exported = False
    current = assignment
    while True:
        exported = exported or _is_commonjs_export_target(
            current.child_by_field_name(cs.FIELD_LEFT)
        )
        value, holder = _outermost_value(current)
        if (
            holder is None
            or holder.type != cs.TS_JS_ASSIGNMENT_EXPRESSION
            or holder.child_by_field_name(cs.FIELD_RIGHT) != value
        ):
            break
        current = holder
    return exported and _runs_at_module_load(value, holder)


def _outermost_value(node: Node) -> tuple[Node, Node | None]:
    # `node` with the value wrappers around it, and the node holding that.
    parent = node.parent
    while parent is not None and parent.type in _JS_TS_VALUE_WRAPPER_TYPES:
        node = parent
        parent = node.parent
    return node, parent


def _runs_at_module_load(value: Node, holder: Node | None) -> bool:
    if holder is None:
        return False
    if holder.type == cs.TS_VARIABLE_DECLARATOR:
        is_own_statement = holder.child_by_field_name(cs.FIELD_VALUE) == value
    else:
        is_own_statement = holder.type == cs.TS_EXPRESSION_STATEMENT
    return is_own_statement and _module_root(holder) is not None


def _is_commonjs_export_target(target: Node | None) -> bool:
    # `module.exports`, `module.exports.X`, `exports.X`, and the same with a
    # string key (`exports["X"]`), which names the export as `.X` does. A
    # computed key (`exports[pick()]`) names no export a caller could find.
    if target is None:
        return False
    if target.type == cs.TS_MEMBER_EXPRESSION:
        text = target.text or b""
        return (
            text == _JS_MODULE_EXPORTS
            or text.startswith(_JS_MODULE_EXPORTS_MEMBER)
            or text.startswith(_JS_EXPORTS_MEMBER)
        )
    if target.type != cs.TS_SUBSCRIPT_EXPRESSION:
        return False
    obj = target.child_by_field_name(cs.FIELD_OBJECT)
    key = target.child_by_field_name(cs.TS_FIELD_INDEX)
    return (
        obj is not None
        and obj.text in (_JS_MODULE_EXPORTS, _JS_EXPORTS_KEYWORD_BYTES)
        and key is not None
        and key.type == cs.TS_STRING
    )


def _js_ts_private_member(node: Node) -> bool:
    # TypeScript marks privacy with an `accessibility_modifier` (`private`),
    # while the ECMAScript form is a `#name` method whose name node is a
    # `private_property_identifier`; both are private regardless of an
    # exported enclosing class.
    member = _js_ts_member_declaration(node)
    if any(c.type == cs.TS_PRIVATE_PROPERTY_IDENTIFIER for c in member.children):
        return True
    modifier = next(
        (c for c in member.children if c.type == cs.TS_ACCESSIBILITY_MODIFIER), None
    )
    return modifier is not None and any(
        c.type == cs.TS_PRIVATE for c in modifier.children
    )


def _js_ts_member_declaration(node: Node) -> Node:
    # A property-arrow member (`private hide = () => 1`) is ingested from the
    # arrow itself, but its `private` modifier and `#name` sit on the enclosing
    # field definition, so privacy is read there.
    parent = node.parent
    if (
        parent is not None
        and parent.type in _JS_TS_FIELD_DEFINITION_TYPES
        and parent.child_by_field_name(cs.FIELD_VALUE) == node
    ):
        return parent
    return node


def _has_export_ancestor(node: Node) -> bool:
    current = node.parent
    while current is not None:
        if current.type == cs.TS_EXPORT_STATEMENT:
            return True
        if current.type in _JS_TS_EXPORT_STOP_TYPES:
            return False
        current = current.parent
    return False


# 1-slot memo of the last root's export-name scan, for the same reason and
# with the same lifetime argument as `_last_script_scan`: every declaration of
# a file asks, and the answer is per file.
_last_export_scan: tuple[Node, frozenset[str]] | None = None


def _module_export_list_names(node: Node) -> frozenset[str]:
    # Local names exported by module-level `export { ... }` / `export default`
    # / `export =` statements and CommonJS export assignments.
    # `export { local as exported }` still makes `local` reachable, so the
    # specifier's local name (its first identifier) is what counts.
    global _last_export_scan
    root = node
    while root.parent is not None:
        root = root.parent
    if _last_export_scan is not None and _last_export_scan[0] == root:
        return _last_export_scan[1]
    names: set[str] = set()
    for statement in root.children:
        if statement.type == cs.TS_EXPORT_STATEMENT:
            names.update(_export_statement_local_names(statement))
        elif statement.type == cs.TS_EXPRESSION_STATEMENT:
            names.update(_commonjs_export_local_names(statement))
    result = frozenset(names)
    _last_export_scan = (root, result)
    return result


def _first_child_of_type(node: Node, node_type: str) -> Node | None:
    return next((c for c in node.children if c.type == node_type), None)


def _export_statement_local_names(statement: Node) -> list[str]:
    # A re-export (`export { x } from './y'`) names another module's symbol,
    # not this file's declaration, so a clause with a source exports nothing.
    if _first_child_of_type(statement, cs.TS_STRING) is not None:
        return []
    clause = _first_child_of_type(statement, cs.TS_EXPORT_CLAUSE)
    if clause is not None:
        locals_ = (
            _first_child_of_type(specifier, cs.TS_IDENTIFIER)
            for specifier in clause.children
            if specifier.type == cs.TS_EXPORT_SPECIFIER
        )
        return [
            text.decode()
            for local in locals_
            if local is not None and (text := local.text) is not None
        ]
    # `export default X` and TypeScript's `export = X` (the CommonJS
    # `module.exports = X` spelling) both publish the module binding X.
    if (
        _first_child_of_type(statement, cs.TS_EXPORT_DEFAULT) is not None
        or _first_child_of_type(statement, cs.CHAR_EQUALS) is not None
    ):
        ident = _first_child_of_type(statement, cs.TS_IDENTIFIER)
        if ident is not None and ident.text is not None:
            return [ident.text.decode()]
    return []


def _commonjs_export_local_names(statement: Node) -> list[str]:
    # A top-level `module.exports = X`, `module.exports = { X, Y: Z }`,
    # `exports.Y = X` or `module.exports.Y = X` publishes the module bindings
    # it names, like an export clause. Only top-level statements are passed
    # in: an assignment inside a function runs when that function does, not at
    # module load.
    assignment = statement.named_children[0] if statement.named_children else None
    if assignment is None or assignment.type != cs.TS_JS_ASSIGNMENT_EXPRESSION:
        return []
    target = assignment.child_by_field_name(cs.FIELD_LEFT)
    value = assignment.child_by_field_name(cs.FIELD_RIGHT)
    if target is None or value is None:
        return []
    if _is_module_exports(target):
        if value.type == cs.TS_OBJECT:
            return _object_identifier_values(value)
        return _identifier_names(value)
    if target.type == cs.TS_MEMBER_EXPRESSION and _is_exports_object(
        target.child_by_field_name(cs.FIELD_OBJECT)
    ):
        return _identifier_names(value)
    return []


def _is_module_exports(node: Node) -> bool:
    if node.type != cs.TS_MEMBER_EXPRESSION:
        return False
    obj = node.child_by_field_name(cs.FIELD_OBJECT)
    prop = node.child_by_field_name(cs.FIELD_PROPERTY)
    return (
        obj is not None
        and prop is not None
        and obj.text == _JS_MODULE_KEYWORD_BYTES
        and prop.text == _JS_EXPORTS_KEYWORD_BYTES
    )


def _is_exports_object(node: Node | None) -> bool:
    if node is None:
        return False
    if node.type == cs.TS_IDENTIFIER:
        return node.text == _JS_EXPORTS_KEYWORD_BYTES
    return _is_module_exports(node)


def _object_identifier_values(obj: Node) -> list[str]:
    # `{ X }` (shorthand) and `{ Y: X }` both export the binding X; a value
    # that is not a bare identifier (`{ run() {} }`) names no binding.
    names: list[str] = []
    for child in obj.named_children:
        if child.type == cs.TS_SHORTHAND_PROPERTY_IDENTIFIER and child.text:
            names.append(child.text.decode())
        elif child.type == cs.TS_PAIR:
            value = child.child_by_field_name(cs.FIELD_VALUE)
            if value is not None:
                names.extend(_identifier_names(value))
    return names


def _identifier_names(node: Node) -> list[str]:
    if node.type == cs.TS_IDENTIFIER and node.text:
        return [node.text.decode()]
    return []


def _java_exported(node: Node) -> bool:
    modifiers = next((c for c in node.children if c.type == cs.TS_MODIFIERS), None)
    if modifiers is not None:
        if any(c.type == cs.JAVA_MODIFIER_PRIVATE for c in modifiers.children):
            return False
        if any(c.type in _JAVA_PUBLIC_MODIFIERS for c in modifiers.children):
            return True
    # Interface methods are public without a visibility modifier (JLS 9.4).
    parent = node.parent
    return (
        parent is not None
        and parent.type == cs.TS_JAVA_INTERFACE_BODY
        and parent.parent is not None
        and parent.parent.type == cs.TS_INTERFACE_DECLARATION
    )


def _csharp_exported(node: Node) -> bool:
    # C# has no modifiers container; visibility is individual `modifier`
    # children. public/internal/protected are external API surface.
    for child in node.children:
        if child.type == cs.TS_CSHARP_MODIFIER and child.text is not None:
            if child.text.decode(cs.ENCODING_UTF8) in _CSHARP_PUBLIC_MODIFIERS:
                return True
    # An explicit interface implementation (`IThing IThing.WithKey(...)`)
    # carries no modifier but is invocable from outside via the interface;
    # API surface (Polly's `IAsyncPolicy.WithPolicyKey`, `IDictionary.Keys`).
    for child in node.children:
        if child.type == cs.TS_CSHARP_EXPLICIT_INTERFACE_SPECIFIER:
            return True
    # An interface member carries no visibility modifier and is implicitly
    # PUBLIC; it IS the interface's API surface (Polly's
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
    if _rust_bare_pub(node):
        return True
    # A trait's methods take no `pub`: they are exactly as visible as the
    # trait, so a `pub trait`'s required and default methods are its API, and
    # dead-code reaches their impls through them (issue #2846).
    body = node.parent
    trait = body.parent if body is not None else None
    return (
        trait is not None
        and trait.type == cs.TS_RS_TRAIT_ITEM
        and _rust_bare_pub(trait)
    )


def _rust_bare_pub(node: Node) -> bool:
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


def _c_exported(node: Node) -> bool:
    # A C function without `static` has external linkage: any translation
    # unit may call it, so it is the library's API (issue #2707). A `static`
    # declaration earlier in the file gives the definition internal linkage
    # though it does not repeat the keyword (Greptile, PR #2952). A GCC
    # nested function, defined in another function's body, has no linkage
    # at all.
    if (
        node.type != cs.TS_CPP_FUNCTION_DEFINITION
        or _cpp_local_or_anonymous(node)
        or _cpp_static(node)
    ):
        return False
    name = _cpp_declared_name(node)
    return name is None or not _declared_static_before(
        node, _FunctionKey(name), cpp=False
    )


class _FunctionKey(NamedTuple):
    # What tells one function from another: its plain name and, in C++, its
    # namespace path and parameter types. C has one namespace and no
    # overloads, so both stay empty there.
    name: str
    namespace: tuple[str, ...] = ()
    parameters: str = ""


def _declared_static_before(definition: Node, key: _FunctionKey, *, cpp: bool) -> bool:
    # Whether an earlier `static` declaration of the same function makes it
    # file-private. A C++ declaration names the same function only in the
    # same namespace (reopened blocks included) and with the same parameter
    # types: `static void process(int)` leaves `process(double)` external
    # (bot review on PR #2952).
    pending = list(reversed(_tree_root(definition).children))
    while pending:
        node = pending.pop()
        if node.start_byte >= definition.start_byte:
            return False
        if node.type in cs.C_FILE_SCOPE_CONTAINER_TYPES or (
            cpp and node.type == cs.TS_NAMESPACE_DEFINITION
        ):
            pending.extend(reversed(node.children))
        elif _static_declaration_of(node, key, cpp=cpp):
            return True
    return False


def _static_declaration_of(node: Node, key: _FunctionKey, *, cpp: bool) -> bool:
    # Whether `node` is a `static` declaration of the function `key` names.
    if (
        node.type != cs.TS_CPP_DECLARATION
        or not _cpp_static(node)
        or _cpp_declared_name(node) != key.name
    ):
        return False
    return not cpp or (
        _cpp_lookup_scope(node) == key.namespace
        and _cpp_signature(node) == key.parameters
    )


def _cpp_namespace_path(node: Node) -> tuple[str, ...]:
    # The names of the namespaces around a node, outermost first.
    path: list[str] = []
    current = node.parent
    while current is not None:
        if current.type == cs.TS_NAMESPACE_DEFINITION:
            named = safe_decode_text(current.child_by_field_name(cs.FIELD_NAME))
            path[:0] = (named or "").split(cs.SEPARATOR_DOUBLE_COLON)
        current = current.parent
    return tuple(path)


def _cpp_signature(node: Node) -> str:
    # The parameter types that tell overloads apart, as member overloads are
    # told apart: names, defaults and a parameter's own `const` dropped, so
    # `static void f(int);` declares the `void f(const int n) {}` after it.
    signature = cpp_overload_signature(node)
    return signature.text if signature is not None else ""


def _cpp_declared_name(node: Node) -> str | None:
    # The name a function definition or declaration declares, as written.
    name = _cpp_declarator_name(node)
    return safe_decode_text(name) if name is not None else None


def _cpp_declarator_name(node: Node) -> Node | None:
    declarator = node.child_by_field_name(cs.FIELD_DECLARATOR)
    while declarator is not None and declarator.type != cs.TS_CPP_FUNCTION_DECLARATOR:
        declarator = declarator.child_by_field_name(cs.FIELD_DECLARATOR)
    return (
        declarator.child_by_field_name(cs.FIELD_DECLARATOR)
        if declarator is not None
        else None
    )


def _tree_root(node: Node) -> Node:
    root = node
    while root.parent is not None:
        root = root.parent
    return root


def _cpp_static(node: Node) -> bool:
    return any(
        child.type == cs.TS_CPP_STORAGE_CLASS_SPECIFIER
        and child.text == cs.CPP_KEYWORD_STATIC.encode()
        for child in node.children
    )


def _cpp_public_api(node: Node) -> bool:
    # Outside a module, a C++ function is API when another translation unit
    # can call it: a non-`static` namespace-scope function outside an
    # anonymous namespace, or a non-`private` member of a class that is not
    # local to a function (issue #2707). An out-of-line member definition
    # (`int Box::f() {}`) is left alone: its access is in the class, which is
    # usually in another file.
    #
    # A function template is registered as its `template_declaration`, so
    # the definition is read inside it; template and preprocessor wrappers
    # open no scope, so the scope is the one around them. `static` makes a
    # namespace-scope function file-private, but a member `static` only
    # means it needs no instance (bot review, PR #2952).
    definition = _cpp_function_definition(node)
    if definition is None:
        return False
    scope = _cpp_enclosing_scope(definition)
    if scope is None or _cpp_local_or_anonymous(scope):
        return False
    if scope.type == cs.TS_FIELD_DECLARATION_LIST:
        return _cpp_reachable_member(definition)
    if (
        scope.type == cs.CppNodeType.FRIEND_DECLARATION
        or scope.type in cs.CPP_NAMESPACE_SCOPE_TYPES
    ):
        return _cpp_external_function(definition, scope)
    return False


def _cpp_external_function(definition: Node, scope: Node) -> bool:
    # The one linkage check for every function that is no class member, at
    # namespace scope or a friend defined in its class, which no access
    # label governs and which argument-dependent lookup finds from any
    # translation unit (a hidden-friend `operator==`). It is API only with
    # external linkage: the namespace it belongs to, its qualifier read
    # through any alias, lies in no unnamed namespace, and neither the
    # definition nor an earlier declaration of the same function there is
    # `static` (Greptile, PR #2952).
    home = _cpp_function_home(definition, scope)
    if home is None or _cpp_internal(home) or _cpp_static(definition):
        return False
    name = (_cpp_declared_name(definition) or "").split(cs.SEPARATOR_DOUBLE_COLON)
    key = _FunctionKey(name[-1].strip(), home, _cpp_signature(definition))
    return not _declared_static_before(definition, key, cpp=True)


def _cpp_function_home(definition: Node, scope: Node) -> _CppPath | None:
    # The full path of the namespace a function that is no member belongs
    # to: the one its qualifier names (`void A::f() {}` after `namespace A
    # = N;` defines `N::f`), else the one around it; for a friend, that of
    # its outermost class. None when the qualifier names a class or nothing
    # the file declares: an out-of-line member, left alone.
    if scope.type == cs.CppNodeType.FRIEND_DECLARATION:
        bodies = _cpp_enclosing_bodies(definition)
        return _cpp_class_namespace(bodies[-1].parent) if bodies else None
    name = _cpp_declarator_name(definition)
    if name is None or name.type != cs.TS_CPP_QUALIFIED_IDENTIFIER:
        return _cpp_lookup_scope(definition)
    return _cpp_namespace_qualifier(name, definition)


def _cpp_class_namespace(owner: Node | None) -> _CppPath | None:
    # The namespace a class belongs to: the one its qualifier names, or
    # the one around it. None for a class defined out of line in another
    # class.
    if owner is None:
        return None
    name = owner.child_by_field_name(cs.FIELD_NAME)
    if (
        name is None
        or name.type != cs.TS_CPP_QUALIFIED_IDENTIFIER
        or _cpp_library_specialization(owner)
    ):
        return _cpp_lookup_scope(owner)
    return _cpp_namespace_qualifier(name, owner)


def _cpp_namespace_qualifier(name: Node, context: Node) -> _CppPath | None:
    # The namespace the qualifier of `name` names, `()` for `::`; None when
    # it names a class or nothing the file declares.
    path = _cpp_qualifier_path(name, context)
    if path is None or not path:
        return path
    scopes = _cpp_file_scopes(context)
    if path not in scopes.namespaces or path in scopes.classes:
        return None
    return path


def _cpp_internal(path: _CppPath) -> bool:
    # Whether `path` lies in an unnamed namespace at any depth: what is
    # declared there is file-private.
    return cs.CPP_UNNAMED_NAMESPACE in path


def _cpp_enclosing_scope(node: Node) -> Node | None:
    current = node
    while current.parent is not None and (
        current.parent.type == cs.TS_CPP_TEMPLATE_DECLARATION
        or current.parent.type in cs.CPP_PREPROC_CONDITIONAL_TYPES
    ):
        current = current.parent
    return current.parent


def _cpp_function_definition(node: Node) -> Node | None:
    current: Node | None = node
    while current is not None and current.type == cs.TS_CPP_TEMPLATE_DECLARATION:
        current = next(
            (
                child
                for child in current.named_children
                if child.type
                in (cs.TS_CPP_FUNCTION_DEFINITION, cs.TS_CPP_TEMPLATE_DECLARATION)
            ),
            None,
        )
    if current is None or current.type != cs.TS_CPP_FUNCTION_DEFINITION:
        return None
    return current


def _cpp_local_or_anonymous(scope: Node) -> bool:
    # Inside a function body (a local class) or an unnamed namespace: no
    # other translation unit can name it.
    current: Node | None = scope
    while current is not None:
        if current.type in (cs.TS_CPP_COMPOUND_STATEMENT, cs.TS_CPP_LAMBDA_EXPRESSION):
            return True
        if (
            current.type == cs.CppNodeType.NAMESPACE_DEFINITION
            and current.child_by_field_name(cs.FIELD_NAME) is None
        ):
            return True
        current = current.parent
    return False


def _cpp_reachable_member(member: Node) -> bool:
    # A member is only as visible as every class around it: a `public:`
    # member of a `private` nested class is as hidden as that class. A class
    # defined out of line (`struct Outer::Hidden {}`) takes its access from
    # `Outer`, usually in another file, so it is left alone like an
    # out-of-line member (Greptile, PR #2952).
    bodies = _cpp_enclosing_bodies(member)
    if any(_cpp_out_of_line_class(body.parent) for body in bodies):
        return False
    return _cpp_public_in_some_build(member, bodies)


def _cpp_enclosing_bodies(node: Node) -> list[Node]:
    # The bodies of the classes around `node`, innermost first.
    bodies: list[Node] = []
    current = node.parent
    while current is not None:
        if current.type == cs.TS_FIELD_DECLARATION_LIST:
            bodies.append(current)
        current = current.parent
    return bodies


def _cpp_out_of_line_class(owner: Node | None) -> bool:
    return (
        owner is not None
        and not _cpp_library_specialization(owner)
        and _cpp_qualifier_hides(owner.child_by_field_name(cs.FIELD_NAME), owner)
    )


def _cpp_library_specialization(owner: Node) -> bool:
    # `template <> struct std::hash<Foo> {}` (or a partial one) specializes
    # another library's template for this code, and that library calls its
    # members, so they are API though `std` is no namespace of this file. A
    # plain `struct X::Y {}` is still an out-of-line nested class.
    name = owner.child_by_field_name(cs.FIELD_NAME)
    if name is None or name.type != cs.TS_CPP_QUALIFIED_IDENTIFIER:
        return False
    qualifier, inner = _cpp_split_qualified(name)
    scope = _cpp_enclosing_scope(owner)
    if (
        inner is None
        or inner.type != cs.TS_TEMPLATE_TYPE
        or scope is None
        or scope.type not in cs.CPP_NAMESPACE_SCOPE_TYPES
    ):
        return False
    # `template <> struct Outer::Hidden<int> {}` specializes a member
    # template of a class, so that class's access to it decides (Greptile,
    # PR #2952); a template of a namespace inside an unnamed one is
    # file-private.
    if qualifier is None:
        return True
    if qualifier.type == cs.TS_TEMPLATE_TYPE:
        return False
    kind = _cpp_qualifier_kind(name, owner)
    return not kind.names_class and not kind.internal


class _PreprocFlag(NamedTuple):
    # What a preprocessor branch tests: whether `text` is defined, or the
    # value of the expression `text`.
    defined: bool
    text: str


type _PreprocBuild = dict[_PreprocFlag, bool]


def _cpp_public_in_some_build(member: Node, bodies: list[Node]) -> bool:
    # One build has to compile the member and make it public in every class
    # around it at once: with `Inner` public in `Outer` only under `#ifdef
    # F` and the member public in `Inner` only under `#ifndef F`, no build
    # exposes it (Greptile, PR #2952). The `#if` blocks around the outermost
    # class count too, and only the tests written before the member decide.
    tests = [
        (position, flag)
        for position, flag in _cpp_tests_around(member, bodies)
        if position < member.start_byte
    ]
    flags = sorted({flag for _, flag in tests})
    builds: list[_PreprocBuild | None] = (
        [
            dict(zip(flags, values, strict=True))
            for values in product((False, True), repeat=len(flags))
        ]
        if _cpp_flags_correlate(tests, member)
        else [None]
    )
    return any(
        _cpp_compiled(member, build)
        and all(_cpp_public_in(member, body, build) for body in bodies)
        for build in builds
    )


def _cpp_public_in(member: Node, body: Node, build: _PreprocBuild | None) -> bool:
    # Whether `build`, or with none some build, compiles `member` in the
    # class `body` other than as private. A label inside `#ifdef` still
    # governs what follows it, and after `#if X public: #else private:
    # #endif` a member is public in one build. Blocks that test the same
    # flag are in or out together, so `#ifdef F public: #endif #ifdef F
    # private: #endif` leaves no public build (Greptile, PR #2952).
    owner = body.parent
    default = (
        cs.CPP_ACCESS_PRIVATE
        if owner is not None and owner.type in cs.CPP_PRIVATE_BY_DEFAULT
        else cs.CPP_ACCESS_PUBLIC
    )
    accesses, found = _cpp_accesses_at(body, member, frozenset({default}), build)
    return found and accesses != {cs.CPP_ACCESS_PRIVATE}


def _cpp_compiled(node: Node, build: _PreprocBuild | None) -> bool:
    # Whether `build` compiles `node`: every `#if` chain around it takes the
    # branch that holds it. Without a build any branch may be taken.
    if build is None:
        return True
    child, current = node, node.parent
    while current is not None:
        if current.type in cs.CPP_PREPROC_CONDITIONAL_TYPES and not (
            _cpp_is_alternative(current, child)
        ):
            taken = _cpp_taken_branch(current, build)
            if taken is None or taken.id != current.id:
                return False
        child, current = current, current.parent
    return True


def _cpp_taken_branch(branch: Node, build: _PreprocBuild) -> Node | None:
    # The branch of `branch`'s `#if` chain that `build` compiles, if any.
    head = branch
    while head.parent is not None and _cpp_is_alternative(head.parent, head):
        head = head.parent
    return next(
        (taken for taken in _cpp_chain(head) if _cpp_branch_taken(taken, build)),
        None,
    )


def _cpp_is_alternative(conditional: Node, node: Node) -> bool:
    # Whether `node` is the `#elif` or `#else` that follows `conditional`.
    alternative = conditional.child_by_field_name(cs.FIELD_ALTERNATIVE)
    return alternative is not None and alternative.id == node.id


def _cpp_chain(head: Node) -> Iterator[Node]:
    # The branches of an `#if` chain: the `#if`, its `#elif`s and `#else`.
    branch: Node | None = head
    while branch is not None:
        yield branch
        branch = branch.child_by_field_name(cs.FIELD_ALTERNATIVE)


def _cpp_flags_correlate(tests: list[tuple[int, _PreprocFlag]], member: Node) -> bool:
    # Blocks on one flag agree only while nothing changes it between them:
    # after `#ifdef F public: #endif #undef F #ifdef F private: #endif` a
    # member is public when `F` starts defined. A `#define` or `#undef`
    # anywhere between two tests of its macro (the preprocessor is textual,
    # so one in a nested class or a method body counts too) leaves each
    # branch to be taken or not on its own (Greptile, PR #2952). One before
    # a macro's first test or after its last, as an include guard's
    # `#define` is, changes nothing the tests see.
    if not tests:
        return True
    if len({flag for _, flag in tests}) > cs.CPP_MAX_CORRELATED_CONDITIONS:
        return False
    spans = _cpp_test_spans(tests)
    return not any(
        name in spans and spans[name][0] < position < spans[name][1]
        for position, name in _cpp_macro_changes(member)
    )


def _cpp_test_spans(
    tests: list[tuple[int, _PreprocFlag]],
) -> dict[bytes, tuple[int, int]]:
    # Each macro the tests name, with where its first and its last test is.
    spans: dict[bytes, tuple[int, int]] = {}
    for position, flag in tests:
        text = flag.text.encode(cs.ENCODING_UTF8)
        for name in re.findall(cs.CPP_IDENTIFIER_PATTERN, text):
            first, last = spans.get(name, (position, position))
            spans[name] = (min(first, position), max(last, position))
    return spans


# 1-slot memo of the last root's `#define` and `#undef` directives, as
# `_last_script_scan`: every member under a preprocessor test asks, and the
# answer is per file.
_last_macro_changes: tuple[Node, tuple[tuple[int, bytes], ...]] | None = None


def _cpp_macro_changes(node: Node) -> tuple[tuple[int, bytes], ...]:
    # Where the file of `node` `#define`s or `#undef`s which macro.
    global _last_macro_changes
    root = _tree_root(node)
    if _last_macro_changes is not None and _last_macro_changes[0] == root:
        return _last_macro_changes[1]
    changes: list[tuple[int, bytes]] = []
    pending = [root]
    while pending:
        current = pending.pop()
        name = _cpp_changed_macro(current)
        if name is not None:
            changes.append((current.start_byte, name))
        else:
            pending.extend(current.named_children)
    _last_macro_changes = (root, tuple(changes))
    return _last_macro_changes[1]


def _cpp_changed_macro(node: Node) -> bytes | None:
    if node.type in cs.CPP_PREPROC_DEFINE_TYPES:
        name = node.child_by_field_name(cs.FIELD_NAME)
        return name.text if name is not None else None
    if node.type != cs.TS_CPP_PREPROC_CALL:
        return None
    directive = safe_decode_text(node.child_by_field_name(cs.CPP_FIELD_DIRECTIVE))
    argument = node.child_by_field_name(cs.CPP_FIELD_ARGUMENT)
    if "".join((directive or "").split()) != cs.CPP_PREPROC_UNDEF or argument is None:
        return None
    words = (argument.text or b"").split()
    return words[0] if words else None


def _cpp_accesses_at(
    block: Node, node: Node, entry: frozenset[str], build: _PreprocBuild | None
) -> tuple[frozenset[str], bool]:
    # The accesses that may hold where `node` sits in `block`, or at its end
    # when `node` is not in it (then the flag is False), given `entry` at
    # its start.
    accesses = entry
    alternative = block.child_by_field_name(cs.FIELD_ALTERNATIVE)
    for item in block.children:
        if alternative is not None and item.id == alternative.id:
            continue
        if item.type in cs.CPP_PREPROC_IF_TYPES:
            accesses, found = _cpp_accesses_through(item, node, accesses, build)
            if found:
                return accesses, True
        elif item.start_byte <= node.start_byte and node.end_byte <= item.end_byte:
            return accesses, True
        elif item.type == cs.TS_ACCESS_SPECIFIER and item.text is not None:
            accesses = frozenset({item.text.decode(cs.ENCODING_UTF8).strip()})
    return accesses, False


def _cpp_accesses_through(
    conditional: Node, node: Node, entry: frozenset[str], build: _PreprocBuild | None
) -> tuple[frozenset[str], bool]:
    # In a known build only the first branch whose test holds is compiled.
    # Without one, every branch starts from the accesses before `#if`; after
    # `#endif` any branch's end may hold, and so may `entry` unless an
    # `#else` makes some branch certain.
    branches: list[Node] = []
    current: Node | None = conditional
    while current is not None:
        branches.append(current)
        current = current.child_by_field_name(cs.FIELD_ALTERNATIVE)
    if build is not None:
        taken = next(
            (branch for branch in branches if _cpp_branch_taken(branch, build)), None
        )
        if taken is None:
            return entry, False
        return _cpp_accesses_at(taken, node, entry, build)
    after: set[str] = set()
    for branch in branches:
        accesses, found = _cpp_accesses_at(branch, node, entry, None)
        if found:
            return accesses, True
        after |= accesses
    if branches[-1].type != cs.TS_CPP_PREPROC_ELSE:
        after |= entry
    return frozenset(after), False


def _cpp_branch_taken(branch: Node, build: _PreprocBuild) -> bool:
    if branch.type == cs.TS_CPP_PREPROC_ELSE:
        return True
    flag, negated = _cpp_preproc_test(branch)
    return build.get(flag, False) != negated


def _cpp_tests_around(
    member: Node, bodies: list[Node]
) -> Iterator[tuple[int, _PreprocFlag]]:
    # The tests in the class bodies around `member` and those of the `#if`
    # chains around the outermost class.
    for body in bodies:
        yield from _cpp_preproc_tests(body)
    yield from _cpp_wrapper_tests(bodies[-1] if bodies else member)


def _cpp_preproc_tests(block: Node) -> Iterator[tuple[int, _PreprocFlag]]:
    # Every test of the `#if` chains in `block`, nested ones included.
    for item in block.children:
        if item.type not in cs.CPP_PREPROC_IF_TYPES:
            continue
        for branch in _cpp_chain(item):
            yield from _cpp_branch_test(branch)
            yield from _cpp_preproc_tests(branch)


def _cpp_wrapper_tests(node: Node) -> Iterator[tuple[int, _PreprocFlag]]:
    # The tests of the `#if` chains around `node`, an include guard's among
    # them, without those of the blocks inside.
    current = node.parent
    while current is not None:
        if current.type in cs.CPP_PREPROC_IF_TYPES:
            for branch in _cpp_chain(current):
                yield from _cpp_branch_test(branch)
        current = current.parent


def _cpp_branch_test(branch: Node) -> Iterator[tuple[int, _PreprocFlag]]:
    # The test a branch makes, with where it is written; `#else` makes none.
    if branch.type != cs.TS_CPP_PREPROC_ELSE:
        yield branch.start_byte, _cpp_preproc_test(branch)[0]


def _cpp_preproc_test(branch: Node) -> tuple[_PreprocFlag, bool]:
    # The flag a branch tests and whether it is negated: `#ifdef F`, `#if
    # defined(F)` and `#ifndef F` all test whether `F` is defined, so they
    # agree in every build.
    name = branch.child_by_field_name(cs.FIELD_NAME)
    if name is not None:
        negated = branch.children[0].type in cs.CPP_PREPROC_NEGATED_DIRECTIVES
        return _PreprocFlag(True, safe_decode_text(name) or ""), negated
    condition = branch.child_by_field_name(cs.TS_FIELD_CONDITION)
    negated = False
    while condition is not None and (
        condition.type == cs.TS_PARENTHESIZED_EXPRESSION
        or (
            condition.type == cs.TS_CPP_UNARY_EXPRESSION
            and safe_decode_text(condition.child_by_field_name(cs.FIELD_OPERATOR))
            == cs.CPP_OP_NOT
        )
    ):
        if condition.type == cs.TS_CPP_UNARY_EXPRESSION:
            negated = not negated
            condition = condition.child_by_field_name(cs.CPP_FIELD_ARGUMENT)
        else:
            condition = next(iter(condition.named_children), None)
    if condition is not None and condition.type == cs.TS_CPP_PREPROC_DEFINED:
        defined = next(
            (c for c in condition.named_children if c.type == cs.TS_CPP_IDENTIFIER),
            None,
        )
        return _PreprocFlag(True, safe_decode_text(defined) or ""), negated
    text = safe_decode_text(condition) or ""
    return _PreprocFlag(False, "".join(text.split())), negated


def _cpp_qualifier_hides(name: Node | None, context: Node) -> bool:
    # Whether a qualifier keeps what it qualifies out of the API: a class,
    # since the access is in that class, usually in another file, or a
    # namespace inside an unnamed one. `ns::Box::f` names a member of class
    # `Box` though `ns` is a namespace (Greptile, PR #2952), and a qualifier
    # the file does not declare is taken for a class. `void V::f() {}` with
    # `V` a namespace of the file is a namespace function and keeps its
    # linkage (bot review on PR #2952).
    if name is None or name.type != cs.TS_CPP_QUALIFIED_IDENTIFIER:
        return False
    qualifier = _cpp_qualifier_kind(name, context)
    return qualifier.names_class or qualifier.internal or not qualifier.names_namespace


def _cpp_split_qualified(name: Node) -> tuple[Node | None, Node | None]:
    # A qualified name's innermost qualifier and the plain name after it.
    qualifier = name.child_by_field_name(cs.FIELD_SCOPE)
    inner = name.child_by_field_name(cs.FIELD_NAME)
    while inner is not None and inner.type == cs.TS_CPP_QUALIFIED_IDENTIFIER:
        qualifier = inner.child_by_field_name(cs.FIELD_SCOPE)
        inner = inner.child_by_field_name(cs.FIELD_NAME)
    return qualifier, inner


class _CppQualifier(NamedTuple):
    # What a qualified name's qualifier is among the file's own scopes; an
    # `internal` namespace sits inside an unnamed one, so what it holds is
    # file-private.
    names_namespace: bool
    names_class: bool
    internal: bool = False


def _cpp_qualifier_kind(name: Node, context: Node) -> _CppQualifier:
    # The qualifier of `name` (`app::fmt` of `app::fmt::formatter<Foo>`)
    # looked up from the namespace around `context`, so a class `app::fmt`
    # is no `fmt` at global scope (Greptile, PR #2952). `::f` has the global
    # namespace as qualifier.
    path = _cpp_qualifier_path(name, context)
    if path is None:
        return _CppQualifier(names_namespace=False, names_class=False)
    if not path:
        return _CppQualifier(names_namespace=True, names_class=False)
    scopes = _cpp_file_scopes(context)
    return _CppQualifier(
        path in scopes.namespaces,
        path in scopes.classes,
        _cpp_internal(path),
    )


def _cpp_qualifier_path(name: Node, context: Node) -> _CppPath | None:
    # The full path the qualifier of the qualified `name` stands for: `()`
    # for the global namespace, None when the file does not declare it.
    written = _cpp_written(name, _cpp_lookup_scope(context))
    if len(written.parts) < 2:
        return ()
    qualifier = written._replace(parts=written.parts[:-1])
    return _cpp_resolve(_cpp_file_scopes(context), qualifier)


def _cpp_lookup_scope(node: Node) -> _CppPath:
    # The namespace `node` is written in, spelled as `_CppScopes` spells
    # paths, an unnamed namespace as `CPP_UNNAMED_NAMESPACE`.
    return tuple(part.strip() for part in _cpp_namespace_path(node))


def _cpp_written_path(name: Node) -> tuple[str, ...]:
    # A possibly qualified name's parts, template arguments dropped:
    # `std::hash<Foo>` is `("std", "hash")`.
    parts: list[str] = []
    current: Node | None = name
    while current is not None and current.type == cs.TS_CPP_QUALIFIED_IDENTIFIER:
        scope = current.child_by_field_name(cs.FIELD_SCOPE)
        if scope is not None:
            parts.append(_cpp_plain_name(scope))
        current = current.child_by_field_name(cs.FIELD_NAME)
    if current is not None:
        parts.append(_cpp_plain_name(current))
    return tuple(parts)


def _cpp_plain_name(node: Node) -> str:
    template = (
        node.child_by_field_name(cs.FIELD_NAME)
        if node.type == cs.TS_TEMPLATE_TYPE
        else None
    )
    text = safe_decode_text(template if template is not None else node)
    return "".join((text or "").split())


def _cpp_written(name: Node, scope: _CppPath) -> _CppWritten:
    # A name as written in namespace `scope`. A qualified identifier is read
    # part by part, template arguments dropped, and so is a plain one; a
    # nested namespace name (`a::b`, `::a::b`) holds no template, so its text
    # splits on `::`.
    if name.type == cs.TS_CPP_QUALIFIED_IDENTIFIER:
        anchored = name.child_by_field_name(cs.FIELD_SCOPE) is None
        return _CppWritten(scope, _cpp_written_path(name), anchored)
    if name.type != cs.TS_CPP_NESTED_NAMESPACE_SPECIFIER:
        return _CppWritten(scope, (_cpp_plain_name(name),), anchored=False)
    text = safe_decode_text(name) or ""
    parts = [part.strip() for part in text.split(cs.SEPARATOR_DOUBLE_COLON)]
    anchored = len(parts) > 1 and not parts[0]
    return _CppWritten(scope, tuple(part for part in parts if part), anchored)


type _CppPath = tuple[str, ...]


class _CppWritten(NamedTuple):
    # A name as written: the namespace it is written in, its parts, and
    # whether a leading `::` starts it at the global namespace.
    scope: _CppPath
    parts: _CppPath
    anchored: bool


class _CppScopes(NamedTuple):
    # What a file declares outside function bodies, by full path; an unnamed
    # namespace is the part `CPP_UNNAMED_NAMESPACE`, so whatever has it in
    # its path is file-private. `namespaces` holds namespace aliases too, and
    # `classes` type aliases. `transparent` maps a namespace to the unnamed
    # and inline ones in it, whose names are found from it too, `aliases`
    # maps a namespace alias or a using-declaration to the name it stands
    # for, and `usings` a namespace to those its using-directives bring in
    # (PR #2952).
    namespaces: frozenset[_CppPath]
    classes: frozenset[_CppPath]
    transparent: Mapping[_CppPath, tuple[_CppPath, ...]]
    aliases: Mapping[_CppPath, _CppWritten]
    usings: Mapping[_CppPath, tuple[_CppWritten, ...]]


def _cpp_resolve(
    scopes: _CppScopes, written: _CppWritten, *, usings: bool = True, hops: int = 0
) -> _CppPath | None:
    # The full path a written name stands for, looked up as C++ does: in
    # each namespace from where it is written outward, and in those a
    # using-directive there brings in, the first that declares its first
    # part decides. Each later part is then looked up in what the parts
    # before it name, and at every part a namespace alias or using-declaration
    # stands for the name it names, so `W::A` with `namespace W { namespace
    # A = N; }` is `N` (Greptile, PR #2952). None when the file declares no
    # first part.
    if not written.parts or hops > cs.CPP_MAX_ALIAS_HOPS:
        return None
    first = next(
        (
            candidate
            for base in _cpp_lookup_bases(scopes, written, usings=usings)
            if _cpp_declares(scopes, candidate := base + written.parts[:1])
        ),
        None,
    )
    if first is None:
        return None
    path = _cpp_expand(scopes, first, usings=usings, hops=hops)
    for part in written.parts[1:]:
        member = _cpp_member(scopes, path, part)
        path = _cpp_expand(scopes, member, usings=usings, hops=hops)
    return path


def _cpp_declares(scopes: _CppScopes, path: _CppPath) -> bool:
    return path in scopes.aliases or path in scopes.namespaces or path in scopes.classes


def _cpp_member(scopes: _CppScopes, path: _CppPath, part: str) -> _CppPath:
    # `part` looked up inside `path`: declared there, or in an unnamed or
    # inline namespace in it, at any depth (Greptile, PR #2952).
    return next(
        (
            candidate
            for inner in _cpp_transparent(scopes, path)
            if _cpp_declares(scopes, candidate := inner + (part,))
        ),
        path + (part,),
    )


def _cpp_expand(
    scopes: _CppScopes, path: _CppPath, *, usings: bool, hops: int
) -> _CppPath:
    # `path`, or what it stands for when it is an alias the file can follow.
    alias = scopes.aliases.get(path)
    if alias is None:
        return path
    target = _cpp_resolve(scopes, alias, usings=usings, hops=hops + 1)
    return path if target is None else target


def _cpp_lookup_bases(
    scopes: _CppScopes, written: _CppWritten, *, usings: bool
) -> Iterator[_CppPath]:
    # Where a name's first part is looked for, innermost first: each
    # namespace around where it is written, with the unnamed and inline ones
    # in it, each followed by the namespaces its using-directives bring in,
    # whose own using-directives are not followed.
    around = () if written.anchored else written.scope
    for depth in range(len(around), -1, -1):
        scope = around[:depth]
        yield from _cpp_transparent(scopes, scope)
        if not usings:
            continue
        for directive in scopes.usings.get(scope, ()):
            target = _cpp_resolve(scopes, directive, usings=False)
            if target is not None:
                yield from _cpp_transparent(scopes, target)


def _cpp_transparent(scopes: _CppScopes, scope: _CppPath) -> Iterator[_CppPath]:
    # `scope`, then the unnamed and inline namespaces in it and in those:
    # what they declare is found from `scope` too.
    pending = [scope]
    while pending:
        current = pending.pop(0)
        yield current
        pending.extend(scopes.transparent.get(current, ()))


class _CppScopeRecorder:
    # Collects a file's `_CppScopes` in one walk.

    def __init__(self) -> None:
        self.namespaces: set[_CppPath] = set()
        self.classes: set[_CppPath] = set()
        self.transparent: dict[_CppPath, list[_CppPath]] = {}
        self.aliases: dict[_CppPath, _CppWritten] = {}
        self.usings: dict[_CppPath, list[_CppWritten]] = {}

    def enter(self, node: Node, path: _CppPath) -> _CppPath:
        # Records what `node` declares in the namespace or class `path`, and
        # returns where its children declare.
        named = node.child_by_field_name(cs.FIELD_NAME)
        if node.type == cs.TS_NAMESPACE_DEFINITION:
            return self._namespace(node, named, path)
        if node.type in cs.CPP_TYPE_SPECIFIER_NODE_TYPES and named is not None:
            path += _cpp_written_path(named)
            self.classes.add(path)
        elif node.type == cs.TS_CPP_NAMESPACE_ALIAS_DEFINITION:
            self._namespace_alias(node, named, path)
        elif node.type == cs.TS_CPP_USING_DECLARATION:
            self._using(node, path)
        elif node.type in cs.CPP_TYPE_ALIAS_TYPES:
            self._type_alias(node, path)
        return path

    def _namespace(self, node: Node, named: Node | None, path: _CppPath) -> _CppPath:
        # `namespace a::b {}` declares `a` too. An unnamed namespace is one
        # per namespace around it; its names, and an inline namespace's, are
        # found from there too.
        parts = (
            _cpp_written(named, path).parts
            if named is not None
            else (cs.CPP_UNNAMED_NAMESPACE,)
        )
        outer = path
        for part in parts:
            outer, path = path, path + (part,)
            self.namespaces.add(path)
        if named is None or any(
            child.type == cs.CPP_KEYWORD_INLINE for child in node.children
        ):
            self.transparent.setdefault(outer, []).append(path)
        return path

    def _namespace_alias(self, node: Node, named: Node | None, path: _CppPath) -> None:
        target = node.named_children[-1] if node.named_child_count > 1 else None
        if named is None or target is None:
            return
        alias = path + (_cpp_plain_name(named),)
        self.namespaces.add(alias)
        self.aliases[alias] = _cpp_written(target, path)

    def _using(self, node: Node, path: _CppPath) -> None:
        # `using namespace Q;` brings `Q` into `path`; `using N::Box;`
        # declares `Box` there, standing for `N::Box`.
        target = node.named_children[-1] if node.named_child_count else None
        if target is None:
            return
        written = _cpp_written(target, path)
        if any(child.type == cs.CPP_KEYWORD_NAMESPACE for child in node.children):
            self.usings.setdefault(path, []).append(written)
        elif written.parts:
            self.aliases[path + written.parts[-1:]] = written

    def _type_alias(self, node: Node, path: _CppPath) -> None:
        # `using T = Box;` and `typedef Box T;` name a type, which can
        # qualify a name as its class does.
        for field in (cs.FIELD_NAME, cs.FIELD_DECLARATOR):
            for named in node.children_by_field_name(field):
                if named.type == cs.TS_TYPE_IDENTIFIER:
                    self.classes.add(path + (_cpp_plain_name(named),))

    def scopes(self) -> _CppScopes:
        return _CppScopes(
            frozenset(self.namespaces),
            frozenset(self.classes),
            {scope: tuple(inner) for scope, inner in self.transparent.items()},
            dict(self.aliases),
            {scope: tuple(found) for scope, found in self.usings.items()},
        )


# 1-slot memo of the last root's scopes, as `_last_script_scan`: every
# qualified name of a file asks, and the answer is per file.
_last_file_scopes: tuple[Node, _CppScopes] | None = None


def _cpp_file_scopes(node: Node) -> _CppScopes:
    global _last_file_scopes
    root = _tree_root(node)
    if _last_file_scopes is not None and _last_file_scopes[0] == root:
        return _last_file_scopes[1]
    recorder = _CppScopeRecorder()
    pending: list[tuple[Node, _CppPath]] = [(root, ())]
    while pending:
        current, path = pending.pop()
        if current.type == cs.TS_CPP_FUNCTION_DEFINITION:
            continue
        path = recorder.enter(current, path)
        pending.extend((child, path) for child in current.named_children)
    _last_file_scopes = (root, recorder.scopes())
    return _last_file_scopes[1]


def _scala_exported(node: Node) -> bool:
    # A Scala member or top-level definition is public unless declared
    # `private` (or `private[pkg]`, visible in that package alone);
    # `protected` stays API, as it does for Java. A def local to another
    # def's body is no API at all (issue #2707). An extension method
    # (`extension (x: Int) def twice`) belongs where its `extension` stands
    # (PR #2952).
    parent = node.parent
    while parent is not None and parent.type == cs.TS_SCALA_EXTENSION_DEFINITION:
        parent = parent.parent
    if parent is None or parent.type not in cs.SCALA_API_SCOPES:
        return False
    # Nor is a member of an object or class declared inside a def's body, or
    # of a `private` one: nothing outside its owner can name it (Greptile,
    # PR #2952).
    ancestor = node.parent
    while ancestor is not None:
        if ancestor.type in (
            cs.TS_SCALA_FUNCTION_DEFINITION,
            cs.TS_SCALA_BLOCK,
        ) or _scala_private(ancestor):
            return False
        ancestor = ancestor.parent
    return not _scala_private(node)


def _scala_private(node: Node) -> bool:
    modifiers = next(
        (c for c in node.children if c.type == cs.TS_SCALA_MODIFIERS), None
    )
    return modifiers is not None and any(
        access.type == cs.TS_SCALA_ACCESS_MODIFIER
        and any(c.type == cs.TS_SCALA_PRIVATE for c in access.children)
        for access in modifiers.children
    )


_LUA_FUNCTION_TYPES = frozenset(cs.SPEC_LUA_FUNCTION_TYPES)
# 1-slot memo of the last chunk's returned names, as `_last_script_scan`:
# a file's functions are resolved one after another.
_last_lua_returns: tuple[Node, frozenset[str]] | None = None


def _lua_exported(node: Node, name: str) -> bool:
    # A Lua module's API is the value its chunk returns, since `require`
    # hands exactly that to the caller: `return M` exposes every `M.x` and
    # `M:x`, `return named` that function, and `return { f = function ...
    # end, g = local_fn }` each entry (issue #2578). A function the value
    # does not reach is visible only inside the file.
    if lua_utils.is_module_return_value(node):
        return True
    root = node
    nested = False
    while root.parent is not None:
        root = root.parent
        nested = nested or root.type in _LUA_FUNCTION_TYPES
    returned = _lua_returned_names(root)
    return _lua_bracket_member_exported(node, returned) or _lua_binding_exported(
        node, name, returned, nested
    )


def _lua_bracket_member_exported(node: Node, returned: frozenset[str]) -> bool:
    # A key no name spells (`M["a.b"] = function`) leaves the function
    # nameless, but it is still a member of the table it is stored in.
    target = lua_utils.bracket_assignment_target(node)
    table = lua_utils.bracket_table_path(target) if target is not None else None
    return (
        bool(table)
        and any(
            table == exported or _lua_is_member(table, exported)
            for exported in returned
        )
        and not lua_utils.rebinds_locally(node, lua_utils.root_name(table))
    )


def _lua_binding_exported(
    node: Node, name: str, returned: frozenset[str], nested: bool
) -> bool:
    # A table path is matched by name, so the name must be the chunk's own
    # variable: `M.other` stored in a function's `local M` is not the
    # returned `M`'s member (Greptile, PR #2617).
    binding = _lua_binding_name(node, name)
    return (
        bool(binding)
        and _lua_binding_returned(node, binding, returned, nested)
        and not lua_utils.rebinds_locally(node, lua_utils.root_name(binding))
    )


def _lua_binding_returned(
    node: Node, binding: str, returned: frozenset[str], nested: bool
) -> bool:
    for exported in returned:
        if _lua_is_member(binding, exported):
            return True
        if binding != exported:
            continue
        # A table path names the same table from anywhere, but a bare name
        # is a variable: a `local function f` inside a function is not the
        # chunk's `f`, and a key of a table nobody holds binds no variable.
        if cs.SEPARATOR_DOT in exported or not (
            nested or lua_utils.is_field_value(node)
        ):
            return True
    return False


def _lua_is_member(path: str, table: str) -> bool:
    return path.startswith(
        (f"{table}{cs.SEPARATOR_DOT}", f"{table}{cs.LUA_METHOD_SEPARATOR}")
    )


def _lua_binding_name(node: Node, name: str) -> str | None:
    # The unified FQN path passes a declaration's LAST segment (`clamp` for
    # `function M.clamp()`), while the returned table is matched on the
    # whole target, so a declaration's name is read off the node itself.
    name_node = node.child_by_field_name(cs.FIELD_NAME)
    return safe_decode_text(name_node) if name_node is not None else name


def _lua_returned_names(chunk: Node) -> frozenset[str]:
    global _last_lua_returns
    if _last_lua_returns is not None and _last_lua_returns[0] == chunk:
        return _last_lua_returns[1]
    names: set[str] = set()
    for statement in chunk.children:
        if statement.type != cs.TS_RETURN_STATEMENT:
            continue
        for values in statement.children:
            if lua_utils.is_chunk_return_list(values):
                for value in values.named_children:
                    _collect_lua_value_names(value, names)
    _follow_lua_aliases(chunk, names)
    result = frozenset(names)
    _last_lua_returns = (chunk, result)
    return result


def _follow_lua_aliases(chunk: Node, names: set[str]) -> None:
    # A chunk-level alias names the same value as what it was assigned:
    # `local api = M; return api` returns `M`, and with `return M`,
    # `function api.g()` adds `g` to the returned table (Greptile, PR
    # #2617). Closed to a fixpoint, so a chain of aliases is followed.
    aliases = list(_lua_chunk_aliases(chunk))
    size = -1
    while size != len(names):
        size = len(names)
        for target, value in aliases:
            if target in names:
                _collect_lua_value_names(value, names)
            elif (path := _lua_value_path(value)) and path in names:
                names.add(target)


def _lua_chunk_aliases(chunk: Node) -> Iterator[tuple[str, Node]]:
    # (variable, value) of each chunk-level `local x = v` and `x = v`; a
    # multiple assignment pairs its variables and values by position.
    for statement in chunk.children:
        assignment = statement
        if statement.type == cs.TS_LUA_VARIABLE_DECLARATION:
            assignment = next(
                (
                    c
                    for c in statement.named_children
                    if c.type == cs.TS_LUA_ASSIGNMENT_STATEMENT
                ),
                statement,
            )
        if assignment.type != cs.TS_LUA_ASSIGNMENT_STATEMENT:
            continue
        targets = _lua_child_of_type(assignment, cs.TS_LUA_VARIABLE_LIST)
        values = _lua_child_of_type(assignment, cs.TS_LUA_EXPRESSION_LIST)
        if targets is None or values is None:
            continue
        for target, value in zip(
            targets.children_by_field_name(cs.FIELD_NAME),
            values.children_by_field_name(cs.FIELD_VALUE),
            strict=False,
        ):
            if path := _lua_value_path(target):
                yield path, value


def _lua_child_of_type(node: Node, node_type: str) -> Node | None:
    return next((c for c in node.named_children if c.type == node_type), None)


def _lua_value_path(value: Node) -> str | None:
    # The variable or table path an expression spells, if it is one.
    if value.type in (cs.TS_LUA_IDENTIFIER, cs.TS_DOT_INDEX_EXPRESSION):
        return safe_decode_text(value)
    if value.type == cs.TS_LUA_BRACKET_INDEX_EXPRESSION:
        return lua_utils.bracket_key_path(value)
    return None


def _collect_lua_value_names(value: Node, names: set[str]) -> None:
    # The variables and table paths a returned value hands out: `M`,
    # `M.sub`, `M["sub"]`, and those held by a returned table's entries.
    match value.type:
        case cs.TS_LUA_IDENTIFIER | cs.TS_DOT_INDEX_EXPRESSION:
            if text := safe_decode_text(value):
                names.add(text)
        case cs.TS_LUA_BRACKET_INDEX_EXPRESSION:
            if path := lua_utils.bracket_key_path(value):
                names.add(path)
        case cs.TS_PARENTHESIZED_EXPRESSION | cs.TS_LUA_TABLE_CONSTRUCTOR:
            for child in value.named_children:
                _collect_lua_value_names(child, names)
        case cs.TS_LUA_FIELD:
            if (entry := value.child_by_field_name(cs.FIELD_VALUE)) is not None:
                _collect_lua_value_names(entry, names)
