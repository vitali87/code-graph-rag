from __future__ import annotations

from tree_sitter import Node

from .. import constants as cs
from .cpp import utils as cpp_utils

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
    declared = {c.type for c in modifiers.children} if modifiers is not None else set()
    if declared & _JAVA_PUBLIC_MODIFIERS:
        return True
    # An interface's (or annotation type's) member is implicitly public unless
    # declared `private` (Java 9+), and is written without the keyword, so its
    # abstract, `default` and `static` methods are the interface's API
    # (issues #2847, #2701).
    return (
        node.parent is not None
        and node.parent.type in cs.JAVA_IMPLICITLY_PUBLIC_BODIES
        and cs.JAVA_MODIFIER_PRIVATE not in declared
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
