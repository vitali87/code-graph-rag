from __future__ import annotations

from collections.abc import Callable, Iterator
from functools import partial

from tree_sitter import Node

from .. import constants as cs
from .cpp import utils as cpp_utils
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
_JS_OBJECT_ASSIGN = (
    cs.JS_OBJECT_NAME + cs.SEPARATOR_DOT + cs.JS_OBJECT_ASSIGN_METHOD
).encode()
# A function expression invoked where it is written, directly or through
# `.call` / `.apply`, runs its body at once.
_JS_TS_INVOKABLE_FUNCTION_TYPES = frozenset(
    {cs.TS_FUNCTION_EXPRESSION, cs.TS_ARROW_FUNCTION}
)
_JS_INVOKE_METHODS = frozenset({cs.JS_METHOD_CALL, cs.JS_METHOD_APPLY})
# What can rebind a name between a CommonJS publication and the module scope.
_JS_TS_SCOPE_TYPES = frozenset(
    {
        cs.TS_FUNCTION_EXPRESSION,
        cs.TS_ARROW_FUNCTION,
        cs.TS_STATEMENT_BLOCK,
        cs.TS_JS_CATCH_CLAUSE,
        cs.TS_JS_FOR_STATEMENT,
        cs.TS_JS_FOR_IN_STATEMENT,
    }
)
# The fields under which a scope binds names of its own: a function its name
# and parameters, `catch` its parameter, a `for` head its declarations.
_JS_TS_BINDING_FIELDS: dict[str, tuple[str, ...]] = {
    cs.TS_FUNCTION_EXPRESSION: (cs.FIELD_NAME, cs.FIELD_PARAMETERS),
    cs.TS_ARROW_FUNCTION: (cs.FIELD_PARAMETER, cs.FIELD_PARAMETERS),
    cs.TS_JS_CATCH_CLAUSE: (cs.FIELD_PARAMETER,),
    cs.TS_JS_FOR_IN_STATEMENT: (cs.FIELD_LEFT,),
    cs.TS_JS_FOR_STATEMENT: (cs.TS_FIELD_INITIALIZER,),
}
# A function declaration's name, hoisted to the enclosing function even from
# a nested block in sloppy mode.
_JS_TS_NAMED_DECLARATION_TYPES = frozenset(
    {cs.TS_FUNCTION_DECLARATION, cs.TS_GENERATOR_FUNCTION_DECLARATION}
)
# Declarations a block binds for itself alone.
_JS_TS_BLOCK_DECLARATION_TYPES = _JS_TS_NAMED_DECLARATION_TYPES | {
    cs.TS_CLASS_DECLARATION
}
_JS_TS_BINDING_NAME_TYPES = frozenset(
    {cs.TS_IDENTIFIER, cs.TS_SHORTHAND_PROPERTY_IDENTIFIER_PATTERN}
)
# A value naming a binding: `y` in `exports.X = y`, `{ A }` shorthand.
_JS_TS_PUBLISHED_NAME_TYPES = frozenset(
    {cs.TS_IDENTIFIER, cs.TS_SHORTHAND_PROPERTY_IDENTIFIER}
)
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
        case cs.SupportedLanguage.LUA:
            return _lua_exported(node, name)
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


def is_commonjs_module(root: Node) -> bool:
    """Whether a JS/TS module publishes its values through CommonJS alone.

    Only then is what `module.exports` publishes its export table. What it
    imports says nothing about what it exports, so an `import type` or a
    value import beside `module.exports` leaves it CommonJS (Greptile, PR
    #3355), as does a type-only export, which is erased at runtime. Any value
    export statement makes its ES exports the module's whole API, even beside
    `module.exports`: an ES module's `module` is not its export object, and
    following its bindings could pass on a private import. A module with
    neither exports nothing, so it is not CommonJS either. A publication
    counts only when it runs at load (`_CommonjsReader`): the text
    `module.exports` in a string, a comment, a read or a deferred function
    body does not.
    """
    if any(
        statement.type == cs.TS_EXPORT_STATEMENT
        and not _is_export_assignment(statement)
        and not _is_type_only_export(statement)
        for statement in root.children
    ):
        return False
    reader = _CommonjsReader(root)
    return any(map(reader.publishes, reader.events()))


def _is_export_assignment(statement: Node) -> bool:
    return any(
        child.type == cs.TS_EXPORT_ASSIGNMENT_TOKEN for child in statement.children
    )


def _is_type_only_export(statement: Node) -> bool:
    # `export type { T }`, `export interface I`, `export type A = ...`, and a
    # clause whose every specifier is `type` (`export { type T }`; CodeRabbit,
    # PR #3355). One value specifier (`export { type T, U }`) exports a value,
    # and `export {}` names none.
    for child in statement.children:
        if (
            child.type == cs.TS_TYPE_ONLY_TOKEN
            or child.type in cs.TS_TYPE_ONLY_EXPORT_DECLARATIONS
        ):
            return True
        if child.type == cs.TS_EXPORT_CLAUSE:
            return all(
                _is_type_only_specifier(specifier)
                for specifier in child.named_children
                if specifier.type == cs.TS_EXPORT_SPECIFIER
            )
    return False


def _is_type_only_specifier(specifier: Node) -> bool:
    return any(child.type == cs.TS_TYPE_ONLY_TOKEN for child in specifier.children)


def commonjs_export_bindings(root: Node) -> dict[str, str]:
    """Each name a CommonJS module publishes at load, with the binding it names.

    The re-export walk follows a published name and nothing else: a binding
    the module keeps to itself (`const { Hidden } = require('./lib')` beside
    `module.exports = { Public }`) is not an export (CodeRabbit, PR #3355).
    `module.exports = { A, B: x, 'C': y }` publishes A, B and C as the
    bindings A, x and y; `exports.X = y`, `module.exports.X = y` and
    `exports["X"] = y` publish X as y; `Object.assign(module.exports, {...})`
    publishes the keys it copies; TypeScript's `export = { ... }` reads as
    `module.exports = { ... }`. A value that is no bare module binding
    (`= lib`, `= require('./x')`, `= class {}`, a spread, a computed key, a
    name an inner scope binds) records nothing: a missing edge, never a wrong
    one. The publications replay in evaluation order (`_LoadedExports`), so
    a replaced `module.exports` keeps none of what the old object held.
    """
    return dict(_LoadedExports(_CommonjsReader(root)).replay(root))


class _CommonjsReader:
    """The CommonJS export events of one module, as they run while it loads.

    An event is an assignment chain to `module.exports`, `exports.X`,
    `exports["X"]` or `exports` itself, an `Object.assign` onto the exports,
    or TypeScript's `export =`, wherever it sits in an expression
    (`Object.freeze(module.exports = {...})`, parentheses, a top-level `if`)
    or in a function called on the spot (Greptile, PR #3355). A function only
    defined runs later, if ever, so its body is not walked. A name a scope
    between the event and the module binds is that scope's own
    (`(function (Svc) { exports.Public = Svc })(Other)`): it names no module
    binding, and a rebound `exports` or `module` publishes nothing (Greptile,
    PR #3355).
    """

    __slots__ = ("_root", "_declared")

    def __init__(self, root: Node) -> None:
        self._root = root
        self._declared: dict[tuple[int, int, str], frozenset[str]] = {}

    def events(self) -> Iterator[Node]:
        for statement in self._root.children:
            if statement.type == cs.TS_EXPORT_STATEMENT:
                if _is_export_assignment(statement):
                    yield statement
            elif _JS_EXPORTS_KEYWORD_BYTES in (statement.text or b""):
                yield from filter(self._is_event, _load_time_nodes(statement))

    def publishes(self, event: Node) -> bool:
        # Re-pointing `exports` alone (`exports = {}`) publishes nothing.
        if event.type == cs.TS_JS_ASSIGNMENT_EXPRESSION:
            return not all(map(_is_bare_exports, self.chain(event)[0]))
        return True

    def chain(self, head: Node) -> tuple[list[Node], Node | None]:
        # The exports targets `a = b = value` assigns, outermost first, and the
        # value they all receive, looked through parentheses.
        targets: list[Node] = []
        node: Node | None = head
        while node is not None and node.type == cs.TS_JS_ASSIGNMENT_EXPRESSION:
            target = _unwrapped(node.child_by_field_name(cs.FIELD_LEFT))
            if target is not None and self.is_exports_reference(target):
                targets.append(target)
            node = _unwrapped(node.child_by_field_name(cs.FIELD_RIGHT))
        return targets, node

    def object_assign(self, call: Node) -> tuple[Node, list[Node]] | None:
        # `Object.assign(module.exports, ...)` or `Object.assign(exports, ...)`:
        # the exports object and the objects copied onto it, else None.
        callee = call.child_by_field_name(cs.FIELD_FUNCTION)
        arguments = call.child_by_field_name(cs.FIELD_ARGUMENTS)
        if callee is None or arguments is None or callee.text != _JS_OBJECT_ASSIGN:
            return None
        copied = arguments.named_children
        target = _unwrapped(copied[0]) if copied else None
        if target is None or not _is_exports_object(target):
            return None
        return (target, copied[1:]) if self.is_exports_reference(target) else None

    def object_bindings(self, value: Node | None) -> dict[str, str | None]:
        # The names an object literal publishes, each with the module binding
        # it names, or None for a value that names none (`{ run() {} }` and
        # spreads and computed keys name no published name at all).
        value = _unwrapped(value)
        if value is None or value.type != cs.TS_OBJECT:
            return {}
        published: dict[str, str | None] = {}
        for child in value.named_children:
            if child.type == cs.TS_SHORTHAND_PROPERTY_IDENTIFIER:
                key, binding = safe_decode_text(child), self.binding(child)
            elif child.type == cs.TS_PAIR:
                key = _property_key(child.child_by_field_name(cs.FIELD_KEY))
                binding = self.binding(child.child_by_field_name(cs.FIELD_VALUE))
            else:
                continue
            if key:
                published[key] = binding
        return published

    def binding(self, value: Node | None) -> str | None:
        # The module binding a published value names: a bare name no scope on
        # the way to the module rebinds.
        value = _unwrapped(value)
        if (
            value is None
            or value.type not in _JS_TS_PUBLISHED_NAME_TYPES
            or _is_bare_exports(value)
            or self._is_shadowed(value)
        ):
            return None
        return safe_decode_text(value)

    def _is_event(self, node: Node) -> bool:
        if node.type == cs.TS_JS_ASSIGNMENT_EXPRESSION:
            return _is_chain_head(node) and bool(self.chain(node)[0])
        return (
            node.type == cs.TS_CALL_EXPRESSION and self.object_assign(node) is not None
        )

    def is_exports_reference(self, node: Node) -> bool:
        # `exports`, `module.exports` or a property of them, as the module's
        # own: no scope on the way rebinds `exports` or `module`.
        base = _exports_base(node)
        return base is not None and not self._is_shadowed(base)

    def _is_shadowed(self, identifier: Node) -> bool:
        name = safe_decode_text(identifier)
        scope = identifier.parent
        while scope is not None and scope != self._root:
            if scope.type in _JS_TS_SCOPE_TYPES and name in self._names_bound_by(scope):
                return True
            scope = scope.parent
        return False

    def _names_bound_by(self, scope: Node) -> frozenset[str]:
        key = (scope.start_byte, scope.end_byte, scope.type)
        if (names := self._declared.get(key)) is None:
            names = self._declared[key] = frozenset(_declared_names(scope))
        return names


def _load_time_nodes(statement: Node) -> Iterator[Node]:
    pending = [statement]
    while pending:
        node = pending.pop()
        if node.type in _JS_TS_FUNCTION_SCOPE_TYPES and not _is_invoked_on_the_spot(
            node
        ):
            continue
        yield node
        pending.extend(reversed(node.named_children))


def _is_invoked_on_the_spot(function: Node) -> bool:
    # `(function () {...})()`, `(() => {...})()`, `!function () {...}()` and
    # `(function () {...}).call(this)` run their body while the module loads.
    if function.type not in _JS_TS_INVOKABLE_FUNCTION_TYPES:
        return False
    callee, holder = _outermost_value(function)
    if (
        holder is not None
        and holder.type == cs.TS_MEMBER_EXPRESSION
        and holder.child_by_field_name(cs.FIELD_OBJECT) == callee
        and safe_decode_text(holder.child_by_field_name(cs.FIELD_PROPERTY))
        in _JS_INVOKE_METHODS
    ):
        callee, holder = holder, holder.parent
    return (
        holder is not None
        and holder.type == cs.TS_CALL_EXPRESSION
        and holder.child_by_field_name(cs.FIELD_FUNCTION) == callee
    )


def _declared_names(scope: Node) -> Iterator[str]:
    # The names `scope` itself binds. A block binds its own `let`, `const`,
    # `class` and function declarations, and keeps them: a closed block does
    # not shadow what follows it (Greptile, PR #3355). A function binds its
    # name, its parameters, and every `var` and function declaration in its
    # body, hoisted out of nested blocks but not out of nested functions.
    # `catch` binds its parameter and a `for` head its declarations.
    # Over-approximate (a default value's names count too): an extra name
    # costs a missing edge only.
    if scope.type == cs.TS_STATEMENT_BLOCK:
        yield from _block_names(scope)
        return
    for field in _JS_TS_BINDING_FIELDS.get(scope.type, ()):
        yield from _identifiers(scope.child_by_field_name(field))
    if scope.type in _JS_TS_INVOKABLE_FUNCTION_TYPES:
        yield from _hoisted_names(scope.child_by_field_name(cs.FIELD_BODY))


def _block_names(block: Node) -> Iterator[str]:
    for statement in block.named_children:
        if statement.type == cs.TS_LEXICAL_DECLARATION:
            yield from _declarator_names(statement)
        elif statement.type in _JS_TS_BLOCK_DECLARATION_TYPES:
            yield from _identifiers(statement.child_by_field_name(cs.FIELD_NAME))


def _hoisted_names(body: Node | None) -> Iterator[str]:
    # `var` declarations and function declarations anywhere in a function's
    # body bind in the function.
    pending = [body] if body is not None else []
    while pending:
        node = pending.pop()
        if node.type in _JS_TS_FUNCTION_SCOPE_TYPES:
            if node.type in _JS_TS_NAMED_DECLARATION_TYPES:
                yield from _identifiers(node.child_by_field_name(cs.FIELD_NAME))
            continue
        if node.type == cs.TS_VARIABLE_DECLARATION:
            yield from _declarator_names(node)
        elif node.type == cs.TS_JS_FOR_IN_STATEMENT and _declares_var(node):
            yield from _identifiers(node.child_by_field_name(cs.FIELD_LEFT))
        pending.extend(node.named_children)


def _declares_var(loop: Node) -> bool:
    kind = loop.child_by_field_name(cs.FIELD_KIND)
    return kind is not None and kind.type == cs.TS_JS_VAR_KIND


def _declarator_names(declaration: Node) -> Iterator[str]:
    for declarator in declaration.named_children:
        if declarator.type == cs.TS_VARIABLE_DECLARATOR:
            yield from _identifiers(declarator.child_by_field_name(cs.FIELD_NAME))


def _identifiers(node: Node | None) -> Iterator[str]:
    # A TypeScript class names itself with a type identifier; inside a
    # pattern only value identifiers bind, so a parameter's type does not.
    if node is not None and node.type == cs.TS_TYPE_IDENTIFIER:
        if text := safe_decode_text(node):
            yield text
        return
    pending = [node] if node is not None else []
    while pending:
        current = pending.pop()
        if current.type in _JS_TS_BINDING_NAME_TYPES and (
            text := safe_decode_text(current)
        ):
            yield text
        pending.extend(current.named_children)


def _is_chain_head(assignment: Node) -> bool:
    # `a = b = value` is one event, read from its outermost assignment.
    value, holder = _outermost_value(assignment)
    return not (
        holder is not None
        and holder.type == cs.TS_JS_ASSIGNMENT_EXPRESSION
        and holder.child_by_field_name(cs.FIELD_RIGHT) == value
    )


def _unwrapped(node: Node | None) -> Node | None:
    while (
        node is not None
        and node.type in _JS_TS_VALUE_WRAPPER_TYPES
        and node.named_children
    ):
        node = node.named_children[0]
    return node


def _is_bare_exports(node: Node | None) -> bool:
    return (
        node is not None
        and node.type == cs.TS_IDENTIFIER
        and node.text == _JS_EXPORTS_KEYWORD_BYTES
    )


def _exports_base(target: Node) -> Node | None:
    # The `exports` or `module` identifier an exports target starts from:
    # `exports`, `module.exports`, `exports.X`, `module.exports.X` or
    # `exports["X"]`, parentheses looked through; else None.
    if _is_bare_exports(target):
        return target
    if _is_module_exports(target):
        return target.child_by_field_name(cs.FIELD_OBJECT)
    if _commonjs_export_name(target) is None:
        return None
    owner = _unwrapped(target.child_by_field_name(cs.FIELD_OBJECT))
    return _exports_base(owner) if owner is not None else None


class _LoadedExports:
    """What `module.exports` holds once the module has loaded.

    `module.exports` and `exports` are two references to objects (Greptile,
    PR #3355): replacing `module.exports` drops what the old object held
    (CodeRabbit, PR #3355) while `exports` keeps pointing at it, so a later
    `exports.X = ...` publishes nothing; `exports = module.exports` re-points
    `exports` at the current object, and `module.exports = exports`
    publishes the object `exports` holds, names and all. The events replay
    in JavaScript's evaluation order: an assignment's target object is fixed
    before its value runs, and the assignments inside a value run before the
    one that holds it. An opaque value leaves an object with no known names,
    and an opaque value assigned to a name drops that name.
    """

    __slots__ = ("_reader", "_objects", "_module", "_exports")

    def __init__(self, reader: _CommonjsReader) -> None:
        self._reader = reader
        # The names each object holds, by id; both references start on the
        # object `module.exports` begins with.
        self._objects: list[dict[str, str]] = [{}]
        self._module = 0
        self._exports = 0

    def replay(self, root: Node) -> dict[str, str]:
        for statement in root.children:
            if statement.type == cs.TS_EXPORT_STATEMENT:
                if _is_export_assignment(statement) and statement.named_children:
                    # TypeScript's `export = value` is `module.exports = value`.
                    self._module = self._object_of(statement.named_children[0])
            elif _JS_EXPORTS_KEYWORD_BYTES in (statement.text or b""):
                self._replay(statement)
        return self._objects[self._module]

    def _replay(self, statement: Node) -> None:
        # Iterative, so a deep expression cannot exhaust the stack: an event
        # leaves a step behind that runs once what it evaluates first has.
        work: list[Node | Callable[[], None]] = [statement]
        while work:
            item = work.pop()
            if not isinstance(item, Node):
                item()
            elif item.type not in _JS_TS_FUNCTION_SCOPE_TYPES or (
                _is_invoked_on_the_spot(item)
            ):
                work.extend(self._begin(item))

    def _begin(self, node: Node) -> list[Node | Callable[[], None]]:
        if node.type == cs.TS_JS_ASSIGNMENT_EXPRESSION and _is_chain_head(node):
            targets, value = self._reader.chain(node)
            if targets:
                owners = [self._owner(target) for target in targets]
                step = partial(self._assign, targets, owners, value)
                return [step, value] if value is not None else [step]
        elif node.type == cs.TS_CALL_EXPRESSION and (
            (assigned := self._reader.object_assign(node)) is not None
        ):
            step = partial(self._copy, self._owner_of(assigned[0]), assigned[1])
            return [step, *reversed(assigned[1])]
        return list(reversed(node.named_children))

    def _assign(
        self, targets: list[Node], owners: list[int | None], value: Node | None
    ) -> None:
        binding = self._reader.binding(value)
        held: int | None = None
        # `a = b = value` assigns its innermost target first.
        for target, owner in zip(reversed(targets), reversed(owners)):
            if owner is not None:
                self._publish(owner, _commonjs_export_name(target), binding)
                continue
            held = self._object_of(value) if held is None else held
            if _is_module_exports(target):
                self._module = held
            else:
                self._exports = held

    def _copy(self, owner: int | None, sources: list[Node]) -> None:
        if owner is None:
            return
        for source in sources:
            for name, binding in self._reader.object_bindings(source).items():
                self._publish(owner, name, binding)

    def _object_of(self, value: Node | None) -> int:
        # The object a value evaluates to: the one `module.exports` or
        # `exports` holds, else a new one holding an object literal's names.
        reference = _unwrapped(value)
        if reference is not None and self._reader.is_exports_reference(reference):
            if (owner := self._owner_of(reference)) is not None:
                return owner
        literal = self._reader.object_bindings(value)
        self._objects.append({name: b for name, b in literal.items() if b})
        return len(self._objects) - 1

    def _owner(self, target: Node) -> int | None:
        # The object a property target (`exports.X`) writes to, fixed now.
        if _commonjs_export_name(target) is None:
            return None
        return self._owner_of(_unwrapped(target.child_by_field_name(cs.FIELD_OBJECT)))

    def _owner_of(self, reference: Node | None) -> int | None:
        if reference is not None and _is_module_exports(reference):
            return self._module
        return self._exports if _is_bare_exports(reference) else None

    def _publish(self, owner: int, name: str | None, binding: str | None) -> None:
        if name is None:
            return
        if binding:
            self._objects[owner][name] = binding
        else:
            # An opaque value replaces what the name held before.
            self._objects[owner].pop(name, None)


def _commonjs_export_name(target: Node | None) -> str | None:
    # The X of `exports.X`, `module.exports.X` or `exports["X"]`.
    if target is None or not _is_exports_object(
        _unwrapped(target.child_by_field_name(cs.FIELD_OBJECT))
    ):
        return None
    if target.type == cs.TS_MEMBER_EXPRESSION:
        return safe_decode_text(target.child_by_field_name(cs.FIELD_PROPERTY))
    if target.type == cs.TS_SUBSCRIPT_EXPRESSION:
        return _string_content(
            _unwrapped(target.child_by_field_name(cs.TS_FIELD_INDEX))
        )
    return None


def _property_key(key: Node | None) -> str | None:
    if key is not None and key.type == cs.TS_PROPERTY_IDENTIFIER:
        return safe_decode_text(key)
    return _string_content(key)


def _string_content(node: Node | None) -> str | None:
    if node is None or node.type != cs.TS_STRING:
        return None
    fragment = next(
        (c for c in node.named_children if c.type == cs.TS_STRING_FRAGMENT), None
    )
    return safe_decode_text(fragment)


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
