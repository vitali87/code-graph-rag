"""What a CommonJS module publishes, read conservatively (PR #3355).

The re-export walk follows a CommonJS barrel only through the names it
publishes (`module.exports = { Svc }`), never through a binding it keeps to
itself. JavaScript offers endless ways to write exports, so the reader models
a deliberately small subset, and a module that writes its exports any other
way records nothing: a missing edge, never a wrong one.

The subset: statements at the module's top level, or at the top level of a
synchronous function called on the spot (`(function () {...})()`,
`(() => {...})()`, `!function () {...}()`, `(function () {...}).call(this)`),
each one of

- an assignment chain whose every target is `module.exports`, `exports`,
  `exports.X`, `module.exports.X` or `exports['X']`;
- `Object.assign(module.exports | exports, ...)`;
- `Object.freeze(<such a chain>)`;
- `Object.defineProperty(module.exports | exports, 'X', ...)`;
- TypeScript's `export = ...`,

possibly joined by commas. Every other reference to `exports`, `module`, a
load-time `this` or the module's `arguments` must be a read: a property read,
`typeof`, or an equality test. A write anywhere else (in a condition, a loop,
a `switch`, a function body, a computed key, an assignment target's
sub-expression, `exports` passed or aliased), `eval`, `with` or an early
`return` leaves the module's exports unknowable here, so nothing is recorded
(Greptile and CodeRabbit, PR #3355).

A published value names a binding only when it is a bare name that resolves,
through the scopes this subset allows, to exactly one declaration at the
module's top level that nothing reassigns and nothing else of its kind
shadows in the registry or the import map; any other value drops its name.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import NamedTuple

from tree_sitter import Node

from ... import constants as cs
from ..utils import safe_decode_text

_EXPORTS = cs.JS_EXPORTS_KEYWORD.encode()
_MODULE = cs.JS_MODULE_KEYWORD.encode()
_REQUIRE = cs.JS_REQUIRE_KEYWORD.encode()
_ARGUMENTS = cs.JS_ARGUMENTS_NAME.encode()
_EVAL = cs.JS_EVAL_NAME.encode()


def _object_call(method: str) -> bytes:
    return (cs.JS_OBJECT_NAME + cs.SEPARATOR_DOT + method).encode()


_OBJECT_ASSIGN = _object_call(cs.JS_OBJECT_ASSIGN_METHOD)
_OBJECT_FREEZE = _object_call(cs.JS_OBJECT_FREEZE_METHOD)
_OBJECT_DEFINE_PROPERTY = _object_call(cs.JS_OBJECT_DEFINE_PROPERTY_METHOD)
# Wrappers that pass their operand's value through unchanged.
_WRAPPERS = cs.TS_CAST_WRAPPER_TYPES | {cs.TS_PARENTHESIZED_EXPRESSION}
_FUNCTIONS = frozenset(
    {
        cs.TS_FUNCTION_DECLARATION,
        cs.TS_GENERATOR_FUNCTION_DECLARATION,
        cs.TS_FUNCTION_EXPRESSION,
        cs.TS_GENERATOR_FUNCTION,
        cs.TS_ARROW_FUNCTION,
        cs.TS_METHOD_DEFINITION,
    }
)
# A function invoked where it is written, directly or through `.call` /
# `.apply`, runs its body at once.
_INVOKABLE = frozenset({cs.TS_FUNCTION_EXPRESSION, cs.TS_ARROW_FUNCTION})
_INVOKE_METHODS = frozenset({cs.JS_METHOD_CALL, cs.JS_METHOD_APPLY})
_SCOPES = _FUNCTIONS | {
    cs.TS_STATEMENT_BLOCK,
    cs.TS_JS_SWITCH_BODY,
    cs.TS_JS_CATCH_CLAUSE,
    cs.TS_JS_FOR_STATEMENT,
    cs.TS_JS_FOR_IN_STATEMENT,
}
_PATTERNS = frozenset(
    {
        cs.TS_OBJECT_PATTERN,
        cs.TS_ARRAY_PATTERN,
        cs.TS_PAIR_PATTERN,
        cs.TS_ASSIGNMENT_PATTERN,
        cs.TS_OBJECT_ASSIGNMENT_PATTERN,
        cs.TS_REST_PATTERN,
    }
)
_ASSIGNMENTS = frozenset(
    {cs.TS_JS_ASSIGNMENT_EXPRESSION, cs.TS_JS_AUGMENTED_ASSIGNMENT_EXPRESSION}
)
_NAMED_DECLARATIONS = frozenset(
    {
        cs.TS_FUNCTION_DECLARATION,
        cs.TS_GENERATOR_FUNCTION_DECLARATION,
        cs.TS_CLASS_DECLARATION,
    }
)
# A value the registry may register under the name it is declared as.
_CALLABLE_VALUES = frozenset(
    {
        cs.TS_CLASS_EXPRESSION,
        cs.TS_FUNCTION_EXPRESSION,
        cs.TS_GENERATOR_FUNCTION,
        cs.TS_ARROW_FUNCTION,
    }
)
_NAME_TYPES = frozenset({cs.TS_IDENTIFIER, cs.TS_SHORTHAND_PROPERTY_IDENTIFIER})
_HOISTED_FUNCTIONS = frozenset(
    {cs.TS_FUNCTION_DECLARATION, cs.TS_GENERATOR_FUNCTION_DECLARATION}
)
_EXPORTS_NAMES = frozenset({cs.JS_EXPORTS_KEYWORD, cs.JS_MODULE_KEYWORD})

# The kinds of declaration that decide whether a published name is certain.
_IMPORT = "import"
_CALLABLE = "callable"
_VALUE = "value"


class CommonjsBinding(NamedTuple):
    """The module binding a published name stands for."""

    binding: str
    # Declared at the top level by `require` or `import`: the name stands for
    # what that import names, whatever else shares its spelling.
    imported: bool


class _Declaration(NamedTuple):
    top: bool
    kind: str


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
    counts when it runs at load: the text `module.exports` in a string, a
    comment, a read or a deferred function body does not.
    """
    if any(
        statement.type == cs.TS_EXPORT_STATEMENT
        and not _is_export_assignment(statement)
        and not _is_type_only_export(statement)
        for statement in root.children
    ):
        return False
    return _Module(root).publishes_at_load()


def commonjs_export_bindings(root: Node) -> dict[str, CommonjsBinding]:
    """Each name a CommonJS module publishes, with the binding it stands for.

    Empty when the module writes its exports outside the modelled subset;
    see the module docstring.
    """
    module = _Module(root)
    publications = module.publications()
    if publications is None:
        return {}
    exports = _Exports(module)
    for publication in publications:
        exports.apply(publication)
    published: dict[str, CommonjsBinding] = {}
    for name, value in exports.published().items():
        if value is not None and (binding := module.resolve(value)) is not None:
            published[name] = binding
    return published


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


class _Module:
    """One module's tree, read for its CommonJS publications."""

    __slots__ = ("_root", "_bound", "_declarations", "_reassigned")

    def __init__(self, root: Node) -> None:
        self._root = root
        self._bound: dict[tuple[int, int, str], frozenset[str]] = {}
        self._declarations: dict[str, list[_Declaration]] | None = None
        self._reassigned: set[str] = set()

    def publishes_at_load(self) -> bool:
        # Classification only: any publication that runs while the module
        # loads, supported or not.
        for statement in self._root.children:
            if statement.type == cs.TS_EXPORT_STATEMENT:
                if _is_export_assignment(statement):
                    return True
            elif _EXPORTS in (statement.text or b"") and any(
                self._is_publication(node) for node in _load_time_nodes(statement)
            ):
                return True
        return False

    def publications(self) -> list[Node] | None:
        """The modelled publications in the order they run, or None when the
        module writes its exports some other way."""
        if self._hides_writes():
            return None
        found: list[Node] = []
        allowed: set[tuple[int, int, str]] = set()
        containers = [self._root]
        while containers:
            container = containers.pop()
            for statement in container.named_children:
                containers.extend(self._collect(statement, found, allowed))
        if not self._only_reads_besides(allowed):
            return None
        return sorted(found, key=lambda node: node.start_byte)

    def chain(self, head: Node) -> tuple[list[Node], Node | None] | None:
        """The targets `a = b = value` assigns, outermost first, and the value
        they all receive; None unless every target is the module's exports."""
        targets: list[Node] = []
        node: Node | None = head
        while node is not None and node.type == cs.TS_JS_ASSIGNMENT_EXPRESSION:
            target = _unwrapped(node.child_by_field_name(cs.FIELD_LEFT))
            if target is None or not self.is_exports_target(target):
                return None
            targets.append(target)
            node = _unwrapped(node.child_by_field_name(cs.FIELD_RIGHT))
        return targets, node

    def is_exports_target(self, target: Node) -> bool:
        base = _exports_base(target)
        return base is not None and not self.is_shadowed(base)

    def is_shadowed(self, identifier: Node) -> bool:
        name = safe_decode_text(identifier)
        scope = identifier.parent
        while scope is not None and scope != self._root:
            if scope.type in _SCOPES and name in self._names_bound_by(scope):
                return True
            scope = scope.parent
        return False

    def resolve(self, value: Node) -> CommonjsBinding | None:
        """The top-level binding a published value certainly names, if any."""
        name = safe_decode_text(value)
        if value.type not in _NAME_TYPES or not name or self.is_shadowed(value):
            return None
        declarations = self._declarations_of(name)
        tops = [declaration for declaration in declarations if declaration.top]
        if len(tops) != 1 or name in self._reassigned:
            return None
        imported = tops[0].kind == _IMPORT
        # A name that leads through the import map must not be imported
        # anywhere else; one that leads through the registry must not be
        # declared as a class or function anywhere else either (Greptile,
        # PR #3355).
        rivals = {_IMPORT} if imported else {_IMPORT, _CALLABLE}
        if any(not d.top and d.kind in rivals for d in declarations):
            return None
        return CommonjsBinding(name, imported)

    def _collect(
        self, statement: Node, found: list[Node], allowed: set[tuple[int, int, str]]
    ) -> list[Node]:
        # A supported statement's publications; returns the bodies of the
        # functions it calls on the spot, whose statements are supported too.
        if statement.type == cs.TS_EXPORT_STATEMENT:
            if statement.parent == self._root and _is_export_assignment(statement):
                found.append(statement)
                self._allow_value(statement, allowed)
            return []
        if statement.type != cs.TS_EXPRESSION_STATEMENT or not statement.named_children:
            return []
        bodies: list[Node] = []
        for part in _sequence(statement.named_children[0]):
            if (body := _supported_iife_body(part)) is not None:
                bodies.append(body)
            elif (publication := self._publication(part, allowed)) is not None:
                found.append(publication)
        return bodies

    def _publication(
        self, expression: Node, allowed: set[tuple[int, int, str]]
    ) -> Node | None:
        if expression.type == cs.TS_CALL_EXPRESSION:
            callee = expression.child_by_field_name(cs.FIELD_FUNCTION)
            arguments = _arguments(expression)
            if (
                callee is not None
                and callee.text == _OBJECT_FREEZE
                and len(arguments) == 1
            ):
                expression = _unwrapped(arguments[0]) or expression
            elif self._is_object_call(expression):
                allowed.add(_key(_unwrapped(arguments[0]) or expression))
                return expression
        if expression.type != cs.TS_JS_ASSIGNMENT_EXPRESSION:
            return None
        chain = self.chain(expression)
        if chain is None:
            return None
        for target in chain[0]:
            allowed.add(_key(_alias_of(target)))
        self._allow_value(expression, allowed)
        return expression

    def _is_object_call(self, call: Node) -> bool:
        # `Object.assign(<exports>, ...)` or
        # `Object.defineProperty(<exports>, 'X', ...)`.
        callee = call.child_by_field_name(cs.FIELD_FUNCTION)
        arguments = _arguments(call)
        if callee is None or not arguments:
            return False
        target = _unwrapped(arguments[0])
        if target is None or not _is_exports_object(target):
            return False
        if not self.is_exports_target(target):
            return False
        if callee.text == _OBJECT_ASSIGN:
            return True
        return (
            callee.text == _OBJECT_DEFINE_PROPERTY
            and len(arguments) > 1
            and _string_content(_unwrapped(arguments[1])) is not None
        )

    def _allow_value(
        self, publication: Node, allowed: set[tuple[int, int, str]]
    ) -> None:
        # `module.exports = exports` and `exports = module.exports` name the
        # exports object as a whole value.
        value = _final_value(publication)
        if (
            value is not None
            and _is_exports_object(value)
            and self.is_exports_target(value)
        ):
            allowed.add(_key(value))

    def _only_reads_besides(self, allowed: set[tuple[int, int, str]]) -> bool:
        # Every reference to the module's exports outside the modelled
        # publications reads it, and changes nothing.
        for node in _subtree(self._root):
            alias = self._alias_reference(node)
            if alias is not None and _key(alias) not in allowed and not _is_read(alias):
                return False
        return True

    def _alias_reference(self, node: Node) -> Node | None:
        # `exports`, `module.exports` or `module`, as the module's own.
        if node.type not in _NAME_TYPES or node.text not in (_EXPORTS, _MODULE):
            return None
        if self.is_shadowed(node):
            return None
        parent = node.parent
        if (
            node.text == _MODULE
            and parent is not None
            and parent.type == cs.TS_MEMBER_EXPRESSION
            and parent.child_by_field_name(cs.FIELD_OBJECT) == node
        ):
            property_ = parent.child_by_field_name(cs.FIELD_PROPERTY)
            # `module.id` and the like read nothing exports can change.
            return (
                parent if property_ is not None and property_.text == _EXPORTS else None
            )
        return node

    def _hides_writes(self) -> bool:
        return any(_hides_write(node) for node in _subtree(self._root))

    def _is_publication(self, node: Node) -> bool:
        if node.type == cs.TS_JS_ASSIGNMENT_EXPRESSION:
            target = _unwrapped(node.child_by_field_name(cs.FIELD_LEFT))
            return (
                target is not None
                and not _is_bare_exports(target)
                and self.is_exports_target(target)
            )
        return node.type == cs.TS_CALL_EXPRESSION and self._is_object_call(node)

    def _names_bound_by(self, scope: Node) -> frozenset[str]:
        key = _key(scope)
        if (names := self._bound.get(key)) is None:
            names = self._bound[key] = frozenset(_declared_names(scope))
        return names

    def _declarations_of(self, name: str) -> list[_Declaration]:
        if self._declarations is None:
            self._declarations = {}
            for node in _subtree(self._root):
                for found, declaration in _declarations(node, self._root):
                    self._declarations.setdefault(found, []).append(declaration)
                self._reassigned.update(_reassigned_names(node, self._root))
        return self._declarations.get(name, [])


class _Exports:
    """What `module.exports` holds once the modelled publications have run.

    `module.exports` and `exports` are two references to objects (Greptile,
    PR #3355): replacing `module.exports` drops what the old object held
    (CodeRabbit, PR #3355) while `exports` keeps pointing at it;
    `exports = module.exports` re-points `exports` at the current object, and
    `module.exports = exports` publishes the object `exports` holds, names
    and all. An assignment chain fixes every target's object before any of
    its assignments runs, then assigns its innermost target first. A name
    holds the value node it was given, or None when that value is opaque.
    """

    __slots__ = ("_module", "_objects", "_module_ref", "_exports_ref")

    def __init__(self, module: _Module) -> None:
        self._module = module
        self._objects: list[dict[str, Node | None]] = [{}]
        self._module_ref = 0
        self._exports_ref = 0

    def published(self) -> dict[str, Node | None]:
        return self._objects[self._module_ref]

    def apply(self, publication: Node) -> None:
        if publication.type == cs.TS_EXPORT_STATEMENT:
            # TypeScript's `export = value` is `module.exports = value`.
            self._module_ref = self._object_of(_final_value(publication))
        elif publication.type == cs.TS_CALL_EXPRESSION:
            self._call(publication)
        else:
            self._assign(publication)

    def _assign(self, head: Node) -> None:
        chain = self._module.chain(head)
        if chain is None:
            return
        targets, value = chain
        owners = [self._owner(target) for target in targets]
        held: int | None = None
        for target, owner in zip(reversed(targets), reversed(owners)):
            if owner is not None:
                self._set(owner, _export_name(target), _named_value(value))
                continue
            held = self._object_of(value) if held is None else held
            if _is_module_exports(target):
                self._module_ref = held
            else:
                self._exports_ref = held

    def _call(self, call: Node) -> None:
        callee = call.child_by_field_name(cs.FIELD_FUNCTION)
        arguments = _arguments(call)
        owner = self._owner_of(_unwrapped(arguments[0]))
        if owner is None or callee is None:
            return
        if callee.text == _OBJECT_DEFINE_PROPERTY:
            # A property defined by descriptor holds no value the reader sees.
            self._set(owner, _string_content(_unwrapped(arguments[1])), None)
            return
        for source in arguments[1:]:
            names = _object_names(_unwrapped(source))
            if names is None:
                # An opaque source may overwrite any name.
                self._objects[owner].clear()
                continue
            for name, value in names.items():
                self._set(owner, name, value)

    def _object_of(self, value: Node | None) -> int:
        # The object a value evaluates to: the one `module.exports` or
        # `exports` holds, else a new one holding an object literal's names.
        if value is not None and _is_exports_object(value):
            if (owner := self._owner_of(value)) is not None:
                return owner
        self._objects.append(_object_names(value) or {})
        return len(self._objects) - 1

    def _owner(self, target: Node) -> int | None:
        # The object a property target (`exports.X`) writes to.
        if _export_name(target) is None:
            return None
        return self._owner_of(_unwrapped(target.child_by_field_name(cs.FIELD_OBJECT)))

    def _owner_of(self, reference: Node | None) -> int | None:
        if reference is not None and _is_module_exports(reference):
            return self._module_ref
        return self._exports_ref if _is_bare_exports(reference) else None

    def _set(self, owner: int, name: str | None, value: Node | None) -> None:
        if name is not None:
            self._objects[owner][name] = value


def _hides_write(node: Node) -> bool:
    # A construct that can write the exports without naming them where the
    # reader looks: `with`, a direct `eval`, an early `return`, the load-time
    # `this` and `arguments`, which hold the exports object, and a
    # declaration of `exports` or `module` of the module's own, which the
    # scope model does not follow (a parameter or a `catch` binding it does).
    if node.type == cs.TS_JS_WITH_STATEMENT:
        return True
    if node.type == cs.TS_CALL_EXPRESSION:
        callee = node.child_by_field_name(cs.FIELD_FUNCTION)
        return callee is not None and callee.text == _EVAL
    if node.type == cs.TS_RETURN_STATEMENT:
        return not _is_deferred(node)
    if node.type == cs.TS_JS_THIS:
        return _this_is_exports(node)
    if node.type == cs.TS_IDENTIFIER and node.text == _ARGUMENTS:
        return _nearest_function(node) is None
    return any(name in _EXPORTS_NAMES for name in _declared_here(node))


def _declared_here(node: Node) -> Iterator[str]:
    if node.type == cs.TS_VARIABLE_DECLARATOR or node.type in _NAMED_DECLARATIONS:
        yield from _pattern_names(node.child_by_field_name(cs.FIELD_NAME))
    elif node.type in _CALLABLE_VALUES:
        yield from _pattern_names(node.child_by_field_name(cs.FIELD_NAME))
    elif node.type == cs.TS_IMPORT_STATEMENT:
        yield from _imported_names(node)


def _load_time_nodes(statement: Node) -> Iterator[Node]:
    pending = [statement]
    while pending:
        node = pending.pop()
        if node.type in _FUNCTIONS and not _is_invoked_on_the_spot(node):
            continue
        yield node
        pending.extend(reversed(node.named_children))


def _subtree(root: Node) -> Iterator[Node]:
    pending = [root]
    while pending:
        node = pending.pop()
        yield node
        pending.extend(reversed(node.named_children))


def _sequence(expression: Node) -> list[Node]:
    # The parts of `a, b, c` in the order they run, each looked through
    # parentheses and a prefix operator (`!function () {...}()`).
    parts: list[Node] = []
    pending: list[Node | None] = [expression]
    while pending:
        node = _unwrapped(pending.pop())
        while node is not None and node.type == cs.TS_JS_UNARY_EXPRESSION:
            node = _unwrapped(node.child_by_field_name(cs.TS_FIELD_ARGUMENT))
        if node is None:
            continue
        if node.type == cs.TS_JS_SEQUENCE_EXPRESSION:
            pending.extend(reversed(node.named_children))
        else:
            parts.append(node)
    return parts


def _supported_iife_body(call: Node) -> Node | None:
    # The body of a synchronous function called on the spot, at statement
    # level: its statements run in order while the module loads.
    if call.type != cs.TS_CALL_EXPRESSION:
        return None
    callee = _unwrapped(call.child_by_field_name(cs.FIELD_FUNCTION))
    if callee is not None and callee.type == cs.TS_MEMBER_EXPRESSION:
        method = callee.child_by_field_name(cs.FIELD_PROPERTY)
        if method is None or safe_decode_text(method) not in _INVOKE_METHODS:
            return None
        callee = _unwrapped(callee.child_by_field_name(cs.FIELD_OBJECT))
    if callee is None or callee.type not in _INVOKABLE:
        return None
    if any(child.type == cs.TS_JS_ASYNC_KEYWORD for child in callee.children):
        return None
    body = callee.child_by_field_name(cs.FIELD_BODY)
    return body if body is not None and body.type == cs.TS_STATEMENT_BLOCK else None


def _is_invoked_on_the_spot(function: Node) -> bool:
    if function.type not in _INVOKABLE:
        return False
    callee, holder = _outermost(function)
    if (
        holder is not None
        and holder.type == cs.TS_MEMBER_EXPRESSION
        and holder.child_by_field_name(cs.FIELD_OBJECT) == callee
        and safe_decode_text(holder.child_by_field_name(cs.FIELD_PROPERTY))
        in _INVOKE_METHODS
    ):
        callee, holder = holder, holder.parent
    return (
        holder is not None
        and holder.type == cs.TS_CALL_EXPRESSION
        and holder.child_by_field_name(cs.FIELD_FUNCTION) == callee
    )


def _is_deferred(node: Node) -> bool:
    # Inside a function that is not called on the spot: it runs later, if
    # ever, not while the module loads.
    current = node.parent
    while current is not None:
        if current.type in _FUNCTIONS and not _is_invoked_on_the_spot(current):
            return True
        current = current.parent
    return False


def _nearest_function(node: Node) -> Node | None:
    # The function whose `this` and `arguments` `node` sees: arrows have none.
    current = node.parent
    while current is not None:
        if current.type in _FUNCTIONS and current.type != cs.TS_ARROW_FUNCTION:
            return current
        current = current.parent
    return None


def _this_is_exports(node: Node) -> bool:
    # The module's `this` is its exports object, and so is the `this` of a
    # function called on the spot through `.call(this)`.
    function = _nearest_function(node)
    if function is None:
        return not _is_call_receiver_argument(node)
    if not _is_invoked_on_the_spot(function):
        return False
    _callee, holder = _outermost(function)
    return holder is not None and holder.type == cs.TS_MEMBER_EXPRESSION


def _is_call_receiver_argument(node: Node) -> bool:
    # The `this` handed to `(function () {...}).call(this)`; the function's
    # own `this` is checked where it is used.
    arguments = node.parent
    call = arguments.parent if arguments is not None else None
    if call is None or call.type != cs.TS_CALL_EXPRESSION:
        return False
    callee = _unwrapped(call.child_by_field_name(cs.FIELD_FUNCTION))
    return (
        callee is not None
        and callee.type == cs.TS_MEMBER_EXPRESSION
        and _supported_iife_body(call) is not None
        and arguments is not None
        and arguments.named_children[:1] == [node]
    )


def _is_read(alias: Node) -> bool:
    # A property read, `typeof`, or an equality test leaves the exports as
    # they are; a property read used as a call while the module loads would
    # hand the callee the exports as its `this`.
    parent = alias.parent
    if parent is None:
        return False
    if parent.type in (cs.TS_MEMBER_EXPRESSION, cs.TS_SUBSCRIPT_EXPRESSION):
        if parent.child_by_field_name(cs.FIELD_OBJECT) != alias:
            return False
        return not _is_written(parent) and not (
            _is_callee(parent) and not _is_deferred(parent)
        )
    if parent.type == cs.TS_JS_UNARY_EXPRESSION:
        operator = parent.child_by_field_name(cs.FIELD_OPERATOR)
        return operator is not None and operator.type == cs.JS_TYPEOF_OPERATOR
    if parent.type == cs.TS_BINARY_EXPRESSION:
        operator = parent.child_by_field_name(cs.FIELD_OPERATOR)
        return operator is not None and operator.type in cs.JS_EQUALITY_OPERATORS
    return False


def _is_written(node: Node) -> bool:
    # Assigned, updated, deleted, or bound by a `for` head or a pattern.
    child, parent = _outermost(node)
    while parent is not None and parent.type in _PATTERNS:
        child, parent = parent, parent.parent
    if parent is None:
        return False
    if parent.type in _ASSIGNMENTS or parent.type == cs.TS_JS_FOR_IN_STATEMENT:
        return parent.child_by_field_name(cs.FIELD_LEFT) == child
    if parent.type == cs.TS_JS_UPDATE_EXPRESSION:
        return True
    if parent.type == cs.TS_JS_UNARY_EXPRESSION:
        operator = parent.child_by_field_name(cs.FIELD_OPERATOR)
        return operator is not None and operator.type == cs.JS_DELETE_OPERATOR
    return False


def _is_callee(node: Node) -> bool:
    callee, holder = _outermost(node)
    return (
        holder is not None
        and holder.type == cs.TS_CALL_EXPRESSION
        and holder.child_by_field_name(cs.FIELD_FUNCTION) == callee
    )


def _declared_names(scope: Node) -> Iterator[str]:
    # The names `scope` itself binds, exactly. A block (or a `switch` body)
    # binds its own `let`, `const`, `class` and function declarations, and
    # keeps them (Greptile, PR #3355). A function binds its name, its
    # parameters, every `var` in its body and the function declarations
    # directly in it. `catch` binds its parameter and a `for` head its
    # declarations.
    if scope.type in (cs.TS_STATEMENT_BLOCK, cs.TS_JS_SWITCH_BODY):
        yield from _block_names(scope)
        return
    if scope.type in (cs.TS_JS_CATCH_CLAUSE, cs.TS_JS_FOR_IN_STATEMENT):
        field = (
            cs.FIELD_PARAMETER if scope.type == cs.TS_JS_CATCH_CLAUSE else cs.FIELD_LEFT
        )
        yield from _pattern_names(scope.child_by_field_name(field))
        return
    if scope.type == cs.TS_JS_FOR_STATEMENT:
        initializer = scope.child_by_field_name(cs.TS_FIELD_INITIALIZER)
        if initializer is not None:
            yield from _declarator_names(initializer)
        return
    yield from _function_names(scope)


def _function_names(function: Node) -> Iterator[str]:
    if function.type in (cs.TS_FUNCTION_EXPRESSION, cs.TS_GENERATOR_FUNCTION):
        yield from _pattern_names(function.child_by_field_name(cs.FIELD_NAME))
    yield from _pattern_names(function.child_by_field_name(cs.FIELD_PARAMETER))
    yield from _pattern_names(function.child_by_field_name(cs.FIELD_PARAMETERS))
    body = function.child_by_field_name(cs.FIELD_BODY)
    if body is None or body.type != cs.TS_STATEMENT_BLOCK:
        return
    # `var`, a `for (var ...)` head, and a function declaration even in a
    # nested block (sloppy mode binds it in the function too) all bind in the
    # function; counting the last one there too can only drop a name.
    pending = list(body.named_children)
    while pending:
        node = pending.pop()
        if node.type in _HOISTED_FUNCTIONS:
            yield from _pattern_names(node.child_by_field_name(cs.FIELD_NAME))
        elif node.type == cs.TS_VARIABLE_DECLARATION:
            yield from _declarator_names(node)
        elif node.type == cs.TS_JS_FOR_IN_STATEMENT and _declares_var(node):
            yield from _pattern_names(node.child_by_field_name(cs.FIELD_LEFT))
        if node.type not in _FUNCTIONS:
            pending.extend(node.named_children)


def _declares_var(loop: Node) -> bool:
    kind = loop.child_by_field_name(cs.FIELD_KIND)
    return kind is not None and kind.type == cs.TS_JS_VAR_KIND


def _block_names(block: Node) -> Iterator[str]:
    statements = (
        [
            statement
            for case in block.named_children
            for statement in case.children_by_field_name(cs.FIELD_BODY)
        ]
        if block.type == cs.TS_JS_SWITCH_BODY
        else block.named_children
    )
    for statement in statements:
        if statement.type == cs.TS_LEXICAL_DECLARATION:
            yield from _declarator_names(statement)
        elif statement.type in _NAMED_DECLARATIONS:
            yield from _pattern_names(statement.child_by_field_name(cs.FIELD_NAME))


def _declarator_names(declaration: Node) -> Iterator[str]:
    for declarator in declaration.named_children:
        if declarator.type == cs.TS_VARIABLE_DECLARATOR:
            yield from _pattern_names(declarator.child_by_field_name(cs.FIELD_NAME))


def _pattern_names(pattern: Node | None) -> Iterator[str]:
    # The names a binding pattern binds, exactly: a default value's names
    # and a parameter's type are not bound.
    pending = [pattern] if pattern is not None else []
    while pending:
        node = pending.pop()
        if node.type in (
            cs.TS_IDENTIFIER,
            cs.TS_SHORTHAND_PROPERTY_IDENTIFIER_PATTERN,
            cs.TS_TYPE_IDENTIFIER,
        ):
            if text := safe_decode_text(node):
                yield text
        elif node.type in (cs.TS_ASSIGNMENT_PATTERN, cs.TS_OBJECT_ASSIGNMENT_PATTERN):
            pending.extend(_present(node.child_by_field_name(cs.FIELD_LEFT)))
        elif node.type == cs.TS_PAIR_PATTERN:
            pending.extend(_present(node.child_by_field_name(cs.FIELD_VALUE)))
        elif node.type in (cs.TS_REQUIRED_PARAMETER, cs.TS_OPTIONAL_PARAMETER):
            pending.extend(_present(node.child_by_field_name(cs.TS_FIELD_PATTERN)))
        elif node.type in (
            cs.TS_JS_FORMAL_PARAMETERS,
            cs.TS_OBJECT_PATTERN,
            cs.TS_ARRAY_PATTERN,
            cs.TS_REST_PATTERN,
        ):
            pending.extend(node.named_children)


def _present(node: Node | None) -> list[Node]:
    return [node] if node is not None else []


def _declarations(node: Node, root: Node) -> Iterator[tuple[str, _Declaration]]:
    # The declarations `node` makes, each marked top-level or not and with
    # the kind that decides whether a published name can rely on it.
    if node.type == cs.TS_VARIABLE_DECLARATOR:
        statement = node.parent
        top = statement is not None and statement.parent == root
        kind = _value_kind(node.child_by_field_name(cs.FIELD_VALUE))
        for name in _pattern_names(node.child_by_field_name(cs.FIELD_NAME)):
            yield name, _Declaration(top, kind)
    elif node.type == cs.TS_IMPORT_STATEMENT:
        for name in _imported_names(node):
            yield name, _Declaration(node.parent == root, _IMPORT)
    else:
        yield from _callable_declarations(node, root)
        yield from _parameter_declarations(node)


def _parameter_declarations(node: Node) -> Iterator[tuple[str, _Declaration]]:
    if node.type in _FUNCTIONS or node.type in (
        cs.TS_JS_CATCH_CLAUSE,
        cs.TS_JS_FOR_IN_STATEMENT,
    ):
        for name in _bound_here(node):
            yield name, _Declaration(False, _VALUE)


def _callable_declarations(
    node: Node, root: Node
) -> Iterator[tuple[str, _Declaration]]:
    # A class or function declaration, or a named class or function
    # expression, which the registry may hold under its name.
    if node.type not in _NAMED_DECLARATIONS and node.type not in _CALLABLE_VALUES:
        return
    top = node.type in _NAMED_DECLARATIONS and node.parent == root
    for name in _pattern_names(node.child_by_field_name(cs.FIELD_NAME)):
        yield name, _Declaration(top, _CALLABLE)


def _bound_here(node: Node) -> Iterator[str]:
    # A function's parameters, a `catch` parameter, a `for ... of` head.
    if node.type in _FUNCTIONS:
        yield from _pattern_names(node.child_by_field_name(cs.FIELD_PARAMETER))
        yield from _pattern_names(node.child_by_field_name(cs.FIELD_PARAMETERS))
    elif node.type == cs.TS_JS_CATCH_CLAUSE:
        yield from _pattern_names(node.child_by_field_name(cs.FIELD_PARAMETER))
    elif node.child_by_field_name(cs.FIELD_KIND) is not None:
        yield from _pattern_names(node.child_by_field_name(cs.FIELD_LEFT))


def _value_kind(value: Node | None) -> str:
    value = _unwrapped(value)
    if value is not None and value.type in _CALLABLE_VALUES:
        return _CALLABLE
    return _IMPORT if _is_require(value) else _VALUE


def _is_require(value: Node | None) -> bool:
    # `require('./m')`, or a property read off one (`require('./m').X`).
    while value is not None and value.type in (
        cs.TS_MEMBER_EXPRESSION,
        cs.TS_SUBSCRIPT_EXPRESSION,
    ):
        value = _unwrapped(value.child_by_field_name(cs.FIELD_OBJECT))
    if value is None or value.type != cs.TS_CALL_EXPRESSION:
        return False
    callee = value.child_by_field_name(cs.FIELD_FUNCTION)
    return callee is not None and callee.text == _REQUIRE


def _imported_names(statement: Node) -> Iterator[str]:
    # `import D, * as ns, { x as y } from '...'` binds D, ns and y.
    for clause in statement.named_children:
        if clause.type != cs.TS_IMPORT_CLAUSE:
            continue
        for part in _subtree(clause):
            local = _import_local(part)
            if local is not None and (text := safe_decode_text(local)):
                yield text


def _import_local(part: Node) -> Node | None:
    if part.type == cs.TS_IMPORT_SPECIFIER:
        return part.child_by_field_name(cs.FIELD_ALIAS) or part.child_by_field_name(
            cs.FIELD_NAME
        )
    parent = part.parent
    if (
        part.type == cs.TS_IDENTIFIER
        and parent is not None
        and parent.type in (cs.TS_IMPORT_CLAUSE, cs.TS_NAMESPACE_IMPORT)
    ):
        return part
    return None


def _reassigned_names(node: Node, root: Node) -> Iterator[str]:
    # Names written after their declaration: by an assignment, an update or a
    # `for` head with no declaration, and a `var` or function declaration in
    # a module-level block, which rebinds the module's own name.
    if node.type in _ASSIGNMENTS:
        yield from _written_names(node.child_by_field_name(cs.FIELD_LEFT))
    elif node.type == cs.TS_JS_UPDATE_EXPRESSION:
        yield from _written_names(node.child_by_field_name(cs.TS_FIELD_ARGUMENT))
    elif (
        node.type == cs.TS_JS_FOR_IN_STATEMENT
        and node.child_by_field_name(cs.FIELD_KIND) is None
    ):
        yield from _written_names(node.child_by_field_name(cs.FIELD_LEFT))
    elif _rebinds_module_name(node, root):
        if node.type == cs.TS_VARIABLE_DECLARATION:
            yield from _declarator_names(node)
        else:
            yield from _pattern_names(node.child_by_field_name(cs.FIELD_NAME))


def _rebinds_module_name(node: Node, root: Node) -> bool:
    if node.parent == root or node.type not in (
        cs.TS_VARIABLE_DECLARATION,
        cs.TS_FUNCTION_DECLARATION,
        cs.TS_GENERATOR_FUNCTION_DECLARATION,
    ):
        return False
    return _nearest_function(node) is None and not any(
        ancestor.type == cs.TS_ARROW_FUNCTION for ancestor in _ancestors(node)
    )


def _ancestors(node: Node) -> Iterator[Node]:
    current = node.parent
    while current is not None:
        yield current
        current = current.parent


def _written_names(target: Node | None) -> Iterator[str]:
    target = _unwrapped(target)
    if target is None:
        return
    if target.type == cs.TS_IDENTIFIER:
        if text := safe_decode_text(target):
            yield text
    elif target.type in _PATTERNS:
        yield from _pattern_names(target)


def _arguments(call: Node) -> list[Node]:
    arguments = call.child_by_field_name(cs.FIELD_ARGUMENTS)
    return arguments.named_children if arguments is not None else []


def _final_value(publication: Node) -> Node | None:
    # The value an `export =` or an assignment chain ends in.
    if publication.type == cs.TS_EXPORT_STATEMENT:
        return (
            _unwrapped(publication.named_children[0])
            if publication.named_children
            else None
        )
    node: Node | None = publication
    while node is not None and node.type == cs.TS_JS_ASSIGNMENT_EXPRESSION:
        node = _unwrapped(node.child_by_field_name(cs.FIELD_RIGHT))
    return node


def _named_value(value: Node | None) -> Node | None:
    # A value that can name a binding: a bare name, else opaque.
    return value if value is not None and value.type in _NAME_TYPES else None


def _object_names(value: Node | None) -> dict[str, Node | None] | None:
    # The names an object literal holds, each with its value node (None when
    # the value names no binding); None when the literal is opaque, as a
    # spread or a computed key can overwrite any name.
    if value is None or value.type != cs.TS_OBJECT:
        return None
    names: dict[str, Node | None] = {}
    for child in value.named_children:
        if child.type == cs.TS_SHORTHAND_PROPERTY_IDENTIFIER:
            key, held = safe_decode_text(child), child
        elif child.type == cs.TS_PAIR:
            key = _property_key(child.child_by_field_name(cs.FIELD_KEY))
            if key is None:
                return None
            held = _named_value(_unwrapped(child.child_by_field_name(cs.FIELD_VALUE)))
        elif child.type == cs.TS_METHOD_DEFINITION:
            key, held = _property_key(child.child_by_field_name(cs.FIELD_NAME)), None
        else:
            return None
        if key is None:
            return None
        names[key] = held
    return names


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


def _unwrapped(node: Node | None) -> Node | None:
    while node is not None and node.type in _WRAPPERS and node.named_children:
        node = node.named_children[0]
    return node


def _outermost(node: Node) -> tuple[Node, Node | None]:
    # `node` with the wrappers around it, and the node holding that.
    parent = node.parent
    while parent is not None and parent.type in _WRAPPERS:
        node = parent
        parent = node.parent
    return node, parent


def _key(node: Node) -> tuple[int, int, str]:
    return (node.start_byte, node.end_byte, node.type)


def _is_bare_exports(node: Node | None) -> bool:
    return node is not None and node.type == cs.TS_IDENTIFIER and node.text == _EXPORTS


def _is_module_exports(node: Node | None) -> bool:
    if node is None or node.type != cs.TS_MEMBER_EXPRESSION:
        return False
    owner = node.child_by_field_name(cs.FIELD_OBJECT)
    property_ = node.child_by_field_name(cs.FIELD_PROPERTY)
    return (
        owner is not None
        and property_ is not None
        and owner.type == cs.TS_IDENTIFIER
        and owner.text == _MODULE
        and property_.text == _EXPORTS
    )


def _is_exports_object(node: Node | None) -> bool:
    return _is_bare_exports(node) or _is_module_exports(node)


def _export_name(target: Node | None) -> str | None:
    # The X of `exports.X`, `module.exports.X` or `exports['X']`.
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


def _exports_base(target: Node) -> Node | None:
    # The `exports` or `module` identifier an exports target starts from, or
    # None for anything else.
    if _is_bare_exports(target):
        return target
    if _is_module_exports(target):
        return target.child_by_field_name(cs.FIELD_OBJECT)
    if _export_name(target) is None:
        return None
    owner = _unwrapped(target.child_by_field_name(cs.FIELD_OBJECT))
    return _exports_base(owner) if owner is not None else None


def _alias_of(target: Node) -> Node:
    # The exports object a target names or writes a property of.
    if _is_exports_object(target):
        return target
    owner = _unwrapped(target.child_by_field_name(cs.FIELD_OBJECT))
    return owner if owner is not None else target
