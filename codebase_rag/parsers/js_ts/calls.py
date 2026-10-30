"""JavaScript/TypeScript's share of the call pass: the callees a call site
binds by its own span or lexical binding rather than by name (`fn.call(...)`,
a named IIFE, a branch-valued callee, a symbol-keyed member), the member
pick a name lookup made, judged against the receiver's declared type, the
`this.m()` dispatch and member twins a resolved call adds, the getters a
member read invokes, and the class a value constructs, read through the
symbol index and the factories that return it.

`CallProcessor` owns what every language shares (the scan of a caller's call
nodes, the site and resolution each edge carries, the name-based resolver
and the reference walkers) and hands JS/TS's part to the
`JsTsCallResolution` it builds. The value peels and call-name shapes the
shared walkers apply to JS/TS syntax are the module functions here."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, NamedTuple, Protocol

from tree_sitter import Node

from ... import constants as cs
from ...language_spec import LanguageSpec
from ...node_text import decode_node_text, safe_decode_text
from ...types_defs import FunctionLocation, LanguageQueries, PropertyDict
from ..callable_params import js_ts_parameter_names
from ..definition_docstring import extract_definition_docstring
from ..edge_site import EdgeSiteHost, host_site_scoped
from ..field_nodes import declared_fields
from ..language_calls import LanguageCalls
from . import bindings as js_bindings
from . import utils as js_ts_utils

if TYPE_CHECKING:
    from ..call_resolver import CallResolver


class JsTsCallHost(EdgeSiteHost, Protocol):
    """What JS/TS call resolution reads and sets on the call pass that builds
    it: the edge emitter, a function node's own name and the location the
    definition pass recorded for it, the reference an inline callback
    argument gets, the reference walkers JS/TS shares with other languages
    and the scope boundaries they stop at, and `_verdict`, how surely the
    call node being scanned binds its callee, which a member pick its
    receiver's declared type confirms raises to exact."""

    _verdict: str

    def _emit_rel(
        self,
        from_spec: tuple[str, str, str],
        rel_type: str,
        to_spec: tuple[str, str, str],
        properties: PropertyDict | None = None,
    ) -> None: ...

    def _get_node_name(self, node: Node, field: str = cs.FIELD_NAME) -> str | None: ...

    def _recorded_caller(
        self, func_node: Node, module_qn: str
    ) -> FunctionLocation | None: ...

    def _emit_inline_arg_function_ref(
        self,
        arg_node: Node,
        source_spec: tuple[str, str, str],
        ensure_rel: Callable[..., None],
        caller_qn: str | None = None,
        rel_type: cs.RelationshipType = cs.RelationshipType.REFERENCES,
        module_qn: str | None = None,
    ) -> None: ...

    @staticmethod
    def _flow_scope_boundaries(lang_config: LanguageSpec) -> frozenset[str]: ...

    def _ingest_py_js_module_references(
        self,
        root_node: Node,
        module_qn: str,
        language: cs.SupportedLanguage,
        queries: Mapping[cs.SupportedLanguage, LanguageQueries],
        class_field_assignments: bool = False,
    ) -> None: ...

    def _ingest_jsx_component_references(
        self,
        caller_node: Node,
        caller_spec: tuple[str, str, str],
        module_qn: str,
        local_var_types: dict[str, str] | None,
        class_context: str | None,
        boundary_types: frozenset[str],
        caller_qn: str | None = None,
    ) -> None: ...

    def _ingest_assignment_function_references(
        self,
        caller_node: Node,
        caller_spec: tuple[str, str, str],
        module_qn: str,
        local_var_types: dict[str, str] | None,
        class_context: str | None,
        boundary_types: frozenset[str],
        caller_qn: str | None = None,
        language: cs.SupportedLanguage | None = None,
    ) -> None: ...

    def _ingest_default_param_references(
        self,
        caller_node: Node,
        caller_spec: tuple[str, str, str],
        module_qn: str,
        local_var_types: dict[str, str] | None,
        class_context: str | None,
        caller_qn: str | None,
    ) -> None: ...

    def _ingest_returned_function_references(
        self,
        caller_node: Node,
        caller_spec: tuple[str, str, str],
        module_qn: str,
        local_var_types: dict[str, str] | None,
        class_context: str | None,
        boundary_types: frozenset[str],
        caller_qn: str | None = None,
        language: cs.SupportedLanguage | None = None,
    ) -> None: ...

    def _ingest_collection_function_references(
        self,
        caller_node: Node,
        caller_spec: tuple[str, str, str],
        module_qn: str,
        local_var_types: dict[str, str] | None,
        class_context: str | None,
        boundary_types: frozenset[str],
        language: cs.SupportedLanguage | None = None,
    ) -> None: ...


class JsTsCallScan(Protocol):
    """The caller a JS/TS call node is scanned in."""

    caller_spec: tuple[str, str, str]
    caller_qn: str
    module_qn: str
    language: cs.SupportedLanguage
    class_context: str | None


# Every class shape whose body a `this.f` read can belong to.
_ANY_CLASS_NODES = frozenset({*cs.JS_TS_CLASS_NODES, cs.TS_ABSTRACT_CLASS_DECLARATION})

# The class members whose `static` keyword decides what `this` is inside.
_CLASS_MEMBER_TYPES = frozenset(
    {cs.TS_METHOD_DEFINITION, cs.TS_PUBLIC_FIELD_DEFINITION, cs.TS_JS_FIELD_DEFINITION}
)

# Expressions that write their target.
_FIELD_WRITE_TYPES = frozenset(
    {
        cs.TS_JS_ASSIGNMENT_EXPRESSION,
        cs.TS_JS_AUGMENTED_ASSIGNMENT_EXPRESSION,
        cs.TS_JS_UPDATE_EXPRESSION,
    }
)


class _ReceiverDeclaration(NamedTuple):
    """What a JS/TS call site declares its receiver to be (issue #2609).

    `foreign` means the declaration names a type the project does not define
    (a built-in, a library type, `any`); otherwise `qn` is the first-party
    type whose inheritance chain may confirm a method binding.
    """

    qn: str
    foreign: bool


# Transparent wrappers a bound arrow may sit behind in its declarator:
# parens and TS casts (`const f = ((x) => ...) as T`). Climbed when
# recovering the arrow's binding name so it is not treated as anonymous.
_BINDING_WRAPPER_TYPES = cs.TS_CAST_WRAPPER_TYPES | {cs.TS_PARENTHESIZED_EXPRESSION}

# JS/TS function scopes that REBIND `this` per call (a plain function /
# generator / function expression), as opposed to an arrow_function (inherits
# `this` lexically) or a method_definition / class-field arrow (binds `this` to
# the instance). Used to tell whether a `this.M()` call site lexically refers to
# the enclosing class instance.
_THIS_REBINDING_FUNC_TYPES: frozenset[str] = frozenset(
    t
    for t in cs.JS_TS_FUNCTION_NODES
    if t not in (cs.TS_ARROW_FUNCTION, cs.TS_METHOD_DEFINITION)
)


def _symbol_member_call_parts(call_node: Node) -> tuple[str, str] | None:
    # `recv[kSym].method(...)` -> ("kSym", "method"); None for other shapes.
    func = call_node.child_by_field_name(cs.FIELD_FUNCTION)
    if func is None or func.type != cs.TS_MEMBER_EXPRESSION:
        return None
    obj = func.child_by_field_name(cs.FIELD_OBJECT)
    prop = func.child_by_field_name(cs.FIELD_PROPERTY)
    if obj is None or prop is None or obj.type != cs.TS_SUBSCRIPT_EXPRESSION:
        return None
    index = obj.child_by_field_name(cs.TS_FIELD_INDEX)
    if index is None or index.type != cs.TS_IDENTIFIER:
        return None
    sym_name = safe_decode_text(index)
    method = safe_decode_text(prop)
    if not sym_name or not method:
        return None
    return sym_name, method


def _iife_wrapper_inner(target: Node) -> Node | None:
    # One value-transparent wrapper layer: parens and casts wrap their
    # operand, the comma operator yields its LAST operand. None stops the
    # peel (no wrapper, or an empty one).
    if target.type == cs.TS_PARENTHESIZED_EXPRESSION:
        return next(
            (child for child in target.named_children if child.type != cs.TS_COMMENT),
            None,
        )
    if target.type == cs.TS_JS_SEQUENCE_EXPRESSION:
        return target.named_children[-1] if target.named_children else None
    if target.type in cs.TS_CAST_WRAPPER_TYPES:
        return next(iter(target.named_children), None)
    return None


def _peel_getter_receiver(recv: Node | None) -> tuple[Node | None, str | None]:
    """Peel type-transparent wrappers off a getter read's receiver.

    Parens, `x!`, and `x satisfies T` keep the operand's type, so they are
    peeled; an `as` cast ASSERTS a type, so the receiver resolves from that
    target type directly (`(box as Base).thing` links even when box is
    untyped). Returns (receiver, None), or (None, cast type text) for a cast,
    with "" when the cast's type has no text.
    """
    while recv is not None:
        if recv.type in (
            cs.TS_PARENTHESIZED_EXPRESSION,
            cs.TS_NON_NULL_EXPRESSION,
            cs.TS_SATISFIES_EXPRESSION,
        ):
            recv = recv.named_child(0)
        elif recv.type == cs.TS_AS_EXPRESSION:
            type_node = recv.named_children[-1] if recv.named_children else None
            return None, (safe_decode_text(type_node) or "") if type_node else ""
        else:
            break
    return recv, None


def this_is_class_instance(call_node: Node) -> bool:
    # True when `this` at this JS/TS call site lexically refers to the
    # enclosing class instance: walk up treating an arrow_function as
    # transparent (it inherits `this`), stopping at the first scope that
    # BINDS `this` -- a method_definition or the class itself (a class-field
    # arrow) means the instance (True); a plain function / generator /
    # function expression rebinds `this` per call, so `this` is NOT the
    # instance (False). Prevents a false self-dispatch edge for `this.M()`
    # inside a plain nested `function(){}` (issue #971).
    current = call_node.parent
    while current is not None:
        node_type = current.type
        if node_type == cs.TS_METHOD_DEFINITION or node_type in cs.JS_TS_CLASS_NODES:
            return True
        if node_type in _THIS_REBINDING_FUNC_TYPES:
            return False
        current = current.parent
    return False


def iife_target_name(parenthesized_expr: Node) -> str | None:
    for child in parenthesized_expr.children:
        match child.type:
            case cs.TS_FUNCTION_EXPRESSION | cs.TS_GENERATOR_FUNCTION:
                # A named IIFE registers under its own name, not the
                # positional iife_* name; its edge comes from the
                # span-resolved path, never a bare-name lookup (a
                # same-named sibling would capture it).
                if iife_callee_name(child):
                    return None
                return f"{cs.IIFE_FUNC_PREFIX}{child.start_point[0]}_{child.start_point[1]}"
            case cs.TS_ARROW_FUNCTION:
                return f"{cs.IIFE_ARROW_PREFIX}{child.start_point[0]}_{child.start_point[1]}"
    return None


def iife_callee_name(func_node: Node) -> str | None:
    name_node = func_node.child_by_field_name(cs.FIELD_NAME)
    if name_node is not None and name_node.text is not None:
        return name_node.text.decode(cs.ENCODING_UTF8)
    return None


def _peel_iife_callee_wrappers(func_child: Node) -> tuple[Node, int, int]:
    # Peel to a FIXPOINT (wrappers nest: `((fn))()`, `(0, (fn))()`, cast
    # forms), the same discipline as peel_bound_callable. Parens and
    # casts are value-transparent; the comma operator yields its LAST
    # operand. Anything else (ternary, logicals) has no single static
    # callee and stops the peel.
    target = func_child
    paren_peels = 0
    other_peels = 0
    while (inner := _iife_wrapper_inner(target)) is not None:
        if target.type == cs.TS_PARENTHESIZED_EXPRESSION:
            paren_peels += 1
        else:
            other_peels += 1
        target = inner
    return target, paren_peels, other_peels


def _wrapper_operands(node: Node, stack: list[Node]) -> bool:
    # Value-transparent wrappers: push what they yield; True when handled.
    node_type = node.type
    if (
        node_type == cs.TS_PARENTHESIZED_EXPRESSION
        or node_type in cs.TS_CAST_WRAPPER_TYPES
    ):
        stack.extend(
            child for child in node.named_children if child.type != cs.TS_COMMENT
        )
        return True
    if node_type == cs.TS_JS_SEQUENCE_EXPRESSION:
        if node.named_children:
            stack.append(node.named_children[-1])
        return True
    return False


def _branch_operands(node: Node, stack: list[Node]) -> bool:
    # Push a branch expression's candidate operands; True only for a
    # genuine BRANCH (ternary / short-circuit), which is what licenses
    # the candidate-set references.
    if _wrapper_operands(node, stack):
        return False
    node_type = node.type
    if node_type == cs.TS_JS_TERNARY_EXPRESSION:
        for field in (cs.FIELD_CONSEQUENCE, cs.FIELD_ALTERNATIVE):
            if (child := node.child_by_field_name(field)) is not None:
                stack.append(child)
        return True
    if node_type == cs.TS_BINARY_EXPRESSION and is_logical_binary(node):
        for field in (cs.FIELD_LEFT, cs.FIELD_RIGHT):
            if (child := node.child_by_field_name(field)) is not None:
                stack.append(child)
        return True
    return False


def is_logical_binary(node: Node) -> bool:
    # `||`, `??` and `&&` evaluate to ONE OF their operands; every other
    # binary operator produces a fresh value. Callers gate to JS/TS: Go
    # spells boolean `||`/`&&` with the same node and field, where a
    # named operand is a bool, never a callable.
    operator = node.child_by_field_name(cs.FIELD_OPERATOR)
    return (
        operator is not None
        and safe_decode_text(operator) in cs.JS_SHORT_CIRCUIT_OPERATORS
    )


def prefixed_generic_callee(func_child: Node) -> Node | None:
    # `await client.call<number>(x)` and `!isKind<string>(v)` parse as a
    # call of `await client.call` / `!isKind`: the grammar binds the
    # operator tighter than a call with type arguments, and the call is
    # what the operator applies to (issue #2930). Only that misparse puts
    # an operator in the `function` field, so the call must carry type
    # arguments; the operand is the callee, possibly under another
    # operator (`!await f<T>()`).
    call = func_child.parent
    if call is None or call.child_by_field_name(cs.TS_JS_FIELD_TYPE_ARGUMENTS) is None:
        return None
    operand: Node | None = func_child
    while operand is not None and operand.type in cs.TS_JS_PREFIXED_CALLEES:
        operand = (
            operand.child_by_field_name(cs.TS_JS_FIELD_ARGUMENT)
            if operand.type == cs.TS_JS_UNARY_EXPRESSION
            else next(reversed(operand.named_children), None)
        )
    return operand


def bound_function_call_name(func_child: Node) -> str | None:
    if _unwrap_bound_function(func_child) is not None:
        peeled = peel_bound_callable(func_child)
        if peeled.text is not None:
            return decode_node_text(peeled.text)
    return None


def unwrap_ts_value(node: Node) -> Node:
    # Peel TS casts AND parens, interleaved (`((s) => {...}) as SetState`),
    # down to the wrapped value.
    current = node
    while current.type in _BINDING_WRAPPER_TYPES:
        inner = current.named_child(0)
        if inner is None:
            break
        current = inner
    return current


def unwrap_ts_cast(node: Node) -> Node:
    # Peel TS cast wrappers (`x as T`, `x satisfies T`, `x!`) to the wrapped
    # value; they are transparent for reference resolution. The wrapped value
    # is the first named child; casts nest (`x as unknown as T`), so loop.
    current = node
    while current.type in cs.TS_CAST_WRAPPER_TYPES:
        inner = current.named_child(0)
        if inner is None:
            break
        current = inner
    return current


def peel_bound_callable(node: Node, peel_parens: bool = False) -> Node:
    # Iterate cast (and optionally paren) unwraps with the bound-call
    # unwrap to a FIXPOINT: `(handler as any).bind(null)` interleaves a
    # cast INSIDE the bind receiver and `h.bind(a).bind(b)` chains, so
    # a single pass of either unwrap leaves a wrapper behind.
    while True:
        node = unwrap_ts_value(node) if peel_parens else unwrap_ts_cast(node)
        bound = _unwrap_bound_function(node)
        if bound is None:
            return node
        node = bound


def _unwrap_bound_function(node: Node) -> Node | None:
    # For `fn.bind(ctx)` (a call_expression whose function is `fn.bind`),
    # return the bound function `fn` (the member object) so the value is
    # referenced as `fn`, not the Function.prototype builtin. call/apply use
    # the function the same way. Returns None when the node is not such a call.
    if node.type != cs.TS_CALL_EXPRESSION:
        return None
    fn = node.child_by_field_name(cs.TS_FIELD_FUNCTION)
    if fn is None or fn.type != cs.TS_MEMBER_EXPRESSION:
        return None
    prop = fn.child_by_field_name(cs.FIELD_PROPERTY)
    if prop is None or safe_decode_text(prop) not in cs.JS_FUNCTION_PROTOTYPE_METHODS:
        return None
    return fn.child_by_field_name(cs.FIELD_OBJECT)


def _callable_params(callable_node: Node) -> list[Node]:
    # `(a, b) => ...` lists them under `parameters`; a bare `a => ...`
    # holds its one identifier under `parameter`.
    params: list[Node] = []
    for field in (cs.FIELD_PARAMETERS, cs.FIELD_PARAMETER):
        node = callable_node.child_by_field_name(field)
        if node is None:
            continue
        if node.type == cs.TS_IDENTIFIER:
            params.append(node)
        else:
            params.extend(node.named_children)
    return params


def _param_binds_name(param: Node, name: str) -> bool:
    pattern = param.child_by_field_name(cs.TS_FIELD_PATTERN)
    return (
        pattern is not None
        and pattern.type == cs.TS_IDENTIFIER
        and safe_decode_text(pattern) == name
    )


def _param_annotation(callable_node: Node, name: str) -> Node | None:
    for param in _callable_params(callable_node):
        if (
            param.type in (cs.TS_REQUIRED_PARAMETER, cs.TS_OPTIONAL_PARAMETER)
            and _param_binds_name(param, name)
            and (annotation := param.child_by_field_name(cs.FIELD_TYPE)) is not None
        ):
            return annotation
    return None


def _jsdoc_param_type(
    callable_node: Node, name: str, language: cs.SupportedLanguage
) -> str | None:
    doc = extract_definition_docstring(callable_node, language)
    if not doc:
        return None
    templates = {
        template.strip()
        for names in re.findall(cs.JSDOC_TEMPLATE_TAG_PATTERN, doc)
        for template in names.split(cs.CHAR_COMMA)
    }
    for type_text, param_name in re.findall(cs.JSDOC_PARAM_TAG_PATTERN, doc):
        if param_name != name:
            continue
        # An `@template T` names no type the project or the platform
        # defines, so `{T}` is left unread rather than taken as foreign.
        bare = type_text.strip().strip(cs.JSDOC_TYPE_MODIFIER_CHARS)
        return None if bare in templates else type_text.strip()
    return None


def _declared_field_parts(
    class_node: Node, field: str, language: cs.SupportedLanguage
) -> tuple[Node | None, list[Node]]:
    # The instance field's type annotation, which is declared truth and
    # ends the search, else the initialisers its declarations give it.
    initials: list[Node] = []
    for declared in declared_fields(class_node, language):
        if declared.name != field or declared.is_static:
            continue
        annotation = declared.node.child_by_field_name(cs.FIELD_TYPE)
        if annotation is not None:
            return annotation, initials
        if (initial := declared.node.child_by_field_name(cs.FIELD_VALUE)) is not None:
            initials.append(initial)
    return None, initials


def _this_field_writes(class_node: Node, field: str) -> list[Node] | None:
    # Every value instance code in the class body assigns to `this.f`, or
    # None when some write to it gives no single value to type: a
    # computed key (`this[k] = ...`, which may be `f`), a destructuring
    # target, a compound operator or an update.
    body = class_node.child_by_field_name(cs.FIELD_BODY)
    values: list[Node] = []
    stack: list[Node] = list(body.children) if body is not None else []
    while stack:
        node = stack.pop()
        if node.type in _ANY_CLASS_NODES:
            continue
        if node.type in _FIELD_WRITE_TYPES:
            written = _field_write_values(node, field)
            if written is None:
                return None
            values.extend(written)
        stack.extend(node.children)
    return values


def _field_write_values(node: Node, field: str) -> list[Node] | None:
    # What one write expression assigns to `this.f`: nothing when it
    # writes elsewhere, its right-hand side for a plain `=`, and None
    # when it may write `f` with no value to type (see the caller).
    target = node.child_by_field_name(
        cs.TS_JS_FIELD_ARGUMENT
        if node.type == cs.TS_JS_UPDATE_EXPRESSION
        else cs.FIELD_LEFT
    )
    writes = _writes_this_field(target, field)
    if writes is None:
        return None
    if not writes:
        return []
    value = node.child_by_field_name(cs.TS_FIELD_RIGHT)
    if node.type != cs.TS_JS_ASSIGNMENT_EXPRESSION or value is None:
        return None
    return [value]


def _writes_this_field(target: Node | None, field: str) -> bool | None:
    # Whether an assignment target writes the instance field `field`:
    # None when it may or may not (a computed key, or the field inside a
    # destructuring pattern, whose value is not the right-hand side).
    if target is None:
        return False
    if target.type in (cs.TS_MEMBER_EXPRESSION, cs.TS_SUBSCRIPT_EXPRESSION):
        return _this_target_field(target, field)
    stack = [target]
    while stack:
        node = stack.pop()
        if node.type in (cs.TS_MEMBER_EXPRESSION, cs.TS_SUBSCRIPT_EXPRESSION):
            if _this_target_field(node, field) is not False:
                return None
            continue
        stack.extend(node.named_children)
    return False


def _this_target_field(target: Node, field: str) -> bool | None:
    if not _instance_this(target.child_by_field_name(cs.FIELD_OBJECT)):
        return False
    if target.type == cs.TS_MEMBER_EXPRESSION:
        prop = target.child_by_field_name(cs.FIELD_PROPERTY)
        return prop is not None and safe_decode_text(prop) == field
    index = target.child_by_field_name(cs.TS_FIELD_INDEX)
    if index is None or index.type != cs.TS_STRING:
        return None
    parts = index.named_children
    if any(part.type != cs.TS_STRING_FRAGMENT for part in parts):
        return None
    return "".join(safe_decode_text(part) or "" for part in parts) == field


def _instance_this(node: Node | None) -> bool:
    # `this` naming an instance of the enclosing class: in an instance
    # method or instance field (arrows inherit it), never in a static
    # method, static field or static block, where it is the class.
    if (
        node is None
        or node.type != cs.TS_THIS
        or not _this_binds_to_enclosing_class(node)
    ):
        return False
    current = node.parent
    while current is not None:
        if current.type == cs.TS_CLASS_STATIC_BLOCK:
            return False
        if current.type in _CLASS_MEMBER_TYPES:
            return not any(child.type == cs.TS_STATIC for child in current.children)
        current = current.parent
    return True


def _lexical_type(anchor: Node, name: str) -> bool | None:
    # Whether an enclosing scope declares the type `name`: True when the
    # module itself does, False when only a nested scope (a function, a
    # namespace) or a type-parameter list does, None when none does.
    current = anchor.parent
    while current is not None:
        params = current.child_by_field_name(cs.TS_FIELD_TYPE_PARAMETERS)
        if params is not None and any(
            (param_name := param.child_by_field_name(cs.FIELD_NAME)) is not None
            and safe_decode_text(param_name) == name
            for param in params.named_children
        ):
            return False
        for child in current.named_children:
            declaration = (
                child.child_by_field_name(cs.TS_DECLARATION)
                if child.type == cs.TS_EXPORT_STATEMENT
                else child
            )
            if (
                declaration is not None
                and declaration.type in cs.TS_TYPE_DECLARATION_NODES
                and (decl_name := declaration.child_by_field_name(cs.FIELD_NAME))
                is not None
                and safe_decode_text(decl_name) == name
            ):
                return current.parent is None
        current = current.parent
    return None


def _member_is_write_target(node: Node) -> bool:
    # True when a member read is the WRITE target of a plain assignment
    # (`this.thing = v`, and destructuring forms `[this.thing] = a`,
    # `({x: this.thing} = o)` where the member is nested in the left pattern
    # but is not its direct `left` child); those invoke the SETTER, not the
    # getter. An augmented assignment (`this.thing += 1`) reads first, so it
    # is NOT a write target. The walk stops at the nearest assignment or at a
    # function/class boundary so an outer assignment cannot misjudge a member
    # that actually sits in a nested scope or on the right-hand side.
    current = node.parent
    while current is not None:
        node_type = current.type
        if node_type == cs.TS_JS_ASSIGNMENT_EXPRESSION:
            left = current.child_by_field_name(cs.FIELD_LEFT)
            return (
                left is not None
                and left.start_byte <= node.start_byte
                and node.end_byte <= left.end_byte
            )
        if (
            node_type
            in (
                cs.TS_JS_AUGMENTED_ASSIGNMENT_EXPRESSION,
                cs.TS_METHOD_DEFINITION,
                cs.TS_ARROW_FUNCTION,
            )
            or node_type in _THIS_REBINDING_FUNC_TYPES
            or node_type in cs.JS_TS_CLASS_NODES
        ):
            return False
        current = current.parent
    return False


def _this_binds_to_enclosing_class(read_node: Node) -> bool:
    # True when `this`/`super` at read_node lexically refers to the enclosing
    # class instance: walk up, arrows are transparent (they inherit `this`),
    # a method_definition binds `this` to the class ONLY when it is a class
    # member (parent is a class_body) -- an object-literal shorthand method
    # binds `this` to the object; a class node itself is a class-field arrow
    # context; a plain function / function-expression / generator rebinds.
    current = read_node.parent
    while current is not None:
        node_type = current.type
        if node_type == cs.TS_METHOD_DEFINITION:
            parent = current.parent
            return parent is not None and parent.type == cs.TS_CLASS_BODY
        if node_type in cs.JS_TS_CLASS_NODES:
            return True
        if node_type in _THIS_REBINDING_FUNC_TYPES:
            return False
        current = current.parent
    return False


def _is_symbol_mint(value: Node | None) -> bool:
    # `Symbol('x')` or `Symbol.for('x')`.
    if value is None or value.type != cs.TS_CALL_EXPRESSION:
        return False
    func = value.child_by_field_name(cs.FIELD_FUNCTION)
    if func is None:
        return False
    if func.type == cs.TS_IDENTIFIER:
        return safe_decode_text(func) == cs.JS_SYMBOL_GLOBAL
    if func.type == cs.TS_MEMBER_EXPRESSION:
        obj = func.child_by_field_name(cs.FIELD_OBJECT)
        return (
            obj is not None
            and obj.type == cs.TS_IDENTIFIER
            and safe_decode_text(obj) == cs.JS_SYMBOL_GLOBAL
        )
    return False


def _inside_callable(node: Node) -> bool:
    current = node.parent
    while current is not None:
        if current.type in cs.JS_TS_FUNCTION_NODES:
            return True
        current = current.parent
    return False


def _prototype_assignment_matches(node: Node, fn_leaf: str, member: str | None) -> bool:
    left = node.child_by_field_name(cs.FIELD_LEFT)
    if (
        left is None
        or left.type != cs.TS_MEMBER_EXPRESSION
        or (inner := left.child_by_field_name(cs.FIELD_OBJECT)) is None
        or inner.type != cs.TS_MEMBER_EXPRESSION
        or (obj := inner.child_by_field_name(cs.FIELD_OBJECT)) is None
        or obj.type != cs.TS_IDENTIFIER
        or safe_decode_text(obj) != fn_leaf
        or (prop := inner.child_by_field_name(cs.FIELD_PROPERTY)) is None
        or safe_decode_text(prop) != cs.JS_PROTOTYPE_KEYWORD
    ):
        return False
    if member is None:
        return True
    outer_prop = left.child_by_field_name(cs.FIELD_PROPERTY)
    return outer_prop is not None and safe_decode_text(outer_prop) == member


def _own_returned_objects(fn_node: Node) -> list[Node]:
    objects: list[Node] = []
    stack: list[Node] = list(fn_node.children)
    while stack:
        node = stack.pop()
        if node.type in cs.JS_TS_FUNCTION_NODES:
            continue
        if node.type == cs.TS_RETURN_STATEMENT:
            for child in node.named_children:
                if child.type == cs.TS_OBJECT:
                    objects.append(child)
        stack.extend(node.children)
    return objects


def _direct_return_ctor(fn_node: Node) -> str | None:
    stack: list[Node] = list(fn_node.children)
    while stack:
        node = stack.pop()
        if node.type in cs.JS_TS_FUNCTION_NODES:
            # A nested callable's returns are its own.
            continue
        if node.type == cs.TS_RETURN_STATEMENT:
            for child in node.named_children:
                if child.type == cs.TS_NEW_EXPRESSION:
                    return js_ts_utils.extract_constructor_name(child)
        stack.extend(node.children)
    return None


def _innermost_introduction(ident: Node, name: str) -> tuple[str, Node | None] | None:
    # The (kind, value) of the introduction of `name` that `ident` reads.
    # Scopes are visited innermost first and the first one introducing
    # the name around `ident` decides: an outer scope's introductions
    # span all of the inner scope, so they can never be the innermost,
    # and the module's introductions are read only for a module-level name.
    pos = ident.start_byte
    scope = ident.parent
    while scope is not None:
        if scope.type in cs.JS_TS_FUNCTION_NODES or scope.parent is None:
            intros = js_bindings.scope_introductions(scope).get(name, ())
            decided, intro = js_bindings.innermost_enclosing(intros, pos)
            if decided:
                return intro
        scope = scope.parent
    return None


def _collect_file_bindings(
    root: Node,
) -> dict[str, list[tuple[Node | None, Node | None]]]:
    return js_bindings.JsFileBindingCollector(unwrap_ts_value).collect(root)


class JsTsCallResolution(LanguageCalls):
    """Binds the JS/TS call sites a name lookup cannot, judges a member pick
    against the receiver's declared type, writes the dispatch and getter
    edges JS/TS's rules add, and keeps the per-run indexes they read.

    Built and owned by `CallProcessor`. The resolver is the pass's own, held
    by reference: the definition pass fills its registry as it registers
    each file."""

    __slots__ = (
        "_host",
        "_resolver",
        "_file_receiver_bindings",
        "symbol_constants",
        "_symbol_assignments",
        "symbol_member_types",
        "_proto_evidence_cache",
        "_factory_classes",
    )

    flows_callables = True
    references_arguments = True
    # A method's inline callback (`run() { return () => 1; }`) gets its
    # REFERENCES edge only when the class pass runs (issue #970).
    scans_callers_without_calls = True
    anonymous_calls_reach_module = True
    keys_members_by_symbol = True
    types_locals = True

    def __init__(self, host: JsTsCallHost, resolver: CallResolver) -> None:
        self._host = host
        self._resolver = resolver
        # Per-module map of identifier -> introductions as (function node,
        # container) for the #988 lexical receiver resolution; built once per
        # file on first use.
        self._file_receiver_bindings: dict[
            str, dict[str, list[tuple[Node | None, Node | None]]]
        ] = {}
        # Symbol-keyed member dispatch (issue #989): qns of symbol CONSTANTS
        # (`kX = Symbol(...)`, `kX: Symbol(...)`), raw `[kX]: value` /
        # `obj[kX] = value` records as (module_qn, symbol_name, class_qn),
        # and the finalised symbol qn -> assigned class qns map. Strings
        # only: holding Node values would pin whole trees.
        self.symbol_constants: set[str] = set()
        self._symbol_assignments: list[tuple[str, str, str]] = []
        self.symbol_member_types: dict[str, set[str]] = {}
        self._proto_evidence_cache: dict[tuple[str, str | None], bool] = {}
        # factory qn -> the class its returns construct, read once per pass
        # (issue #2453). Strings only, so no tree is pinned.
        self._factory_classes: dict[str, str | None] = {}

    def caller_name(self, func_node: Node, module_qn: str) -> str | None:
        return super().caller_name(func_node, module_qn) or self._bound_name(
            func_node, module_qn
        )

    def method_name(self, method_node: Node, module_qn: str) -> str | None:
        # A nameless function expression the definition pass registered
        # under a name (`x: function () {}` in a method's object literal,
        # `this.h = function () {}` in a constructor) has a real node, named
        # as the module pass names it. Since #1903 handed every class-scoped
        # function to the class pass, skipping it there would leave that
        # node with no outgoing edge.
        return self.caller_name(method_node, module_qn)

    def _bound_name(self, func_node: Node, module_qn: str) -> str | None:
        if name := js_ts_utils.arrow_binding_name(func_node):
            return name
        # A nameless JS/TS function expression that a NAMED pass
        # registered (`exports.f = function`, `x: function`) has a
        # real node; its body's calls belong to that node, not the
        # module, so adopt the record's simple name (the recorded-caller
        # branch of the caller pass reuses the registered qn). A GENERATED
        # record (anonymous callback, IIFE) keeps the historical
        # bubble-to-module attribution.
        recorded = self._host._recorded_caller(func_node, module_qn)
        if recorded is not None and recorded.is_named:
            return recorded.qualified_name.rsplit(cs.SEPARATOR_DOT, 1)[-1]
        return None

    def class_name(self, class_node: Node) -> str | None:
        # An anonymous class expression (`static Proxy = class {...}`)
        # has no `name`; recover its field/declarator binding name so
        # its methods get a caller pass (issue #970). Matches the qn the
        # definition pass builds via _js_get_name.
        return super().class_name(class_node) or js_ts_utils.class_binding_name(
            class_node
        )

    def attributable(self, func_nodes: list[Node], module_qn: str) -> list[Node]:
        # A nameless function expression the definition pass registered under
        # a name (`x: function () {}` in an object literal, `this.h = function
        # () {}`) gets its own caller pass, so it must own its calls too, or
        # the enclosing function keeps a second copy of every edge.
        return [
            n
            for n in func_nodes
            if self._host._get_node_name(n)
            or js_ts_utils.arrow_binding_name(n)
            or (
                (recorded := self._host._recorded_caller(n, module_qn)) is not None
                and recorded.is_named
            )
        ]

    def scope_name(self, function_node: Node) -> str | None:
        # An arrow-const ancestor (`getQueryString = () => {...}`) has no
        # `name` field (the name lives on the parent declarator), so it would be
        # dropped, flattening a nested callee's qn (request.encodePair instead
        # of request.getQueryString.encodePair). Recover the binding name the
        # definition pass used so the qns agree; else the callee's own
        # inline-arg/object callbacks never match and report dead.
        return js_ts_utils.arrow_binding_name(function_node) or None

    def ingest_module_references(
        self,
        root_node: Node,
        module_qn: str,
        language: cs.SupportedLanguage,
        queries: Mapping[cs.SupportedLanguage, LanguageQueries],
        combined_captures: dict[str, list[Node]],
        sorted_func_nodes: list[Node] | None,
    ) -> None:
        # A class field's initializer runs at construction, so the
        # assignment scan descends into class bodies too.
        self._host._ingest_py_js_module_references(
            root_node, module_qn, language, queries, class_field_assignments=True
        )
        # A module-scope JSX element (export default <App />) can sit
        # in a file with no call expressions; scan before the early
        # return like the assignment pass.
        self._host._ingest_jsx_component_references(
            root_node,
            (cs.NodeLabel.MODULE, cs.KEY_QUALIFIED_NAME, module_qn),
            module_qn,
            None,
            None,
            self._host._flow_scope_boundaries(queries[language][cs.QUERY_CONFIG]),
        )

    def parameter_names(self, caller_node: Node) -> list[str] | None:
        return js_ts_parameter_names(caller_node)

    def ingest_caller_references(
        self,
        caller_node: Node,
        caller_spec: tuple[str, str, str],
        caller_qn: str,
        caller_type: str,
        module_qn: str,
        language: cs.SupportedLanguage,
        queries: Mapping[cs.SupportedLanguage, LanguageQueries],
        class_context: str | None,
        local_var_types: dict[str, str] | None,
    ) -> None:
        host = self._host
        boundaries = host._flow_scope_boundaries(queries[language][cs.QUERY_CONFIG])
        host._ingest_assignment_function_references(
            caller_node,
            caller_spec,
            module_qn,
            local_var_types,
            class_context,
            boundaries,
            caller_qn,
            language=language,
        )
        host._ingest_jsx_component_references(
            caller_node,
            caller_spec,
            module_qn,
            local_var_types,
            class_context,
            boundaries,
            caller_qn,
        )
        host._ingest_default_param_references(
            caller_node,
            caller_spec,
            module_qn,
            local_var_types,
            class_context,
            caller_qn,
        )
        host._ingest_returned_function_references(
            caller_node,
            caller_spec,
            module_qn,
            local_var_types,
            class_context,
            boundaries,
            caller_qn,
            language,
        )
        # Dispatch-table handler references. Module-scope literals are
        # scanned before the no-calls early return, so only nested scopes
        # here.
        if caller_type != cs.NodeLabel.MODULE:
            host._ingest_collection_function_references(
                caller_node,
                caller_spec,
                module_qn,
                local_var_types,
                class_context,
                boundaries,
                language,
            )

    def forget_file_bindings(self, module_qn: str) -> None:
        self._file_receiver_bindings.pop(module_qn, None)

    def reset_factory_classes(self) -> None:
        self._factory_classes = {}

    def ingest_call_site(
        self,
        ctx: JsTsCallScan,
        call_node: Node,
        call_name: str | None,
        ensure_rel: Callable[..., None],
    ) -> str | None:
        self._inline_arg_references(
            call_node, ctx.caller_spec, ensure_rel, ctx.caller_qn, ctx.module_qn
        )
        # `fn.call(this, ...)` / `fn.apply(this, ...)` invokes fn, but
        # the callee names Function.prototype.call, so the resolve
        # below fails (or, cast-wrapped, never even names the site)
        # and a function invoked ONLY this way in statement position
        # reports dead (issue #988; value positions link through the
        # bound-callable peel). The receiver is resolved by LEXICAL
        # BINDING, never by name lookup: only a binding that IS a
        # function (a hoisted declaration or a declarator holding an
        # inline function) emits, resolved by its own source span.
        # Everything else about the site (name resolution, builtin
        # handling, argument references) proceeds unchanged below.
        if (
            loc := self._fn_call_receiver_binding(call_node, ctx.module_qn)
        ) is not None:
            ensure_rel(
                ctx.caller_spec,
                cs.RelationshipType.CALLS,
                (loc.label, cs.KEY_QUALIFIED_NAME, loc.qualified_name),
            )
        # A NAMED IIFE yields no call name (a bare-name lookup would
        # bind a same-named sibling); its edge is emitted here from
        # the callee node's own span.
        if (iife_loc := self._named_iife_callee(call_node, ctx.module_qn)) is not None:
            ensure_rel(
                ctx.caller_spec,
                cs.RelationshipType.CALLS,
                (iife_loc.label, cs.KEY_QUALIFIED_NAME, iife_loc.qualified_name),
            )
        for branch_loc in self._branch_callee_functions(call_node, ctx.module_qn):
            ensure_rel(
                ctx.caller_spec,
                cs.RelationshipType.REFERENCES,
                (branch_loc.label, cs.KEY_QUALIFIED_NAME, branch_loc.qualified_name),
            )
        # `recv[kSym].method(...)`: the callee text contains brackets
        # and resolves nowhere useful; the symbol index carries the
        # edges, and the generic path (with its phantom-prone
        # bare-name fallback) is suppressed for the recognised shape.
        if (
            sym_targets := self._symbol_member_targets(call_node, ctx.module_qn)
        ) is not None:
            targets, unique = sym_targets
            sym_rel = (
                cs.RelationshipType.CALLS if unique else cs.RelationshipType.REFERENCES
            )
            for sym_label, sym_qn_target in targets:
                for variant in self._resolver.function_registry.variants(sym_qn_target):
                    ensure_rel(
                        ctx.caller_spec,
                        sym_rel,
                        (sym_label, cs.KEY_QUALIFIED_NAME, variant),
                    )
            call_name = None
        return call_name

    def _inline_arg_references(
        self,
        call_node: Node,
        caller_spec: tuple[str, str, str],
        ensure_rel: Callable[..., None],
        caller_qn: str | None,
        module_qn: str | None = None,
    ) -> None:
        args = call_node.child_by_field_name(cs.FIELD_ARGUMENTS)
        if args is None:
            return
        for arg in args.named_children:
            value = unwrap_ts_value(arg)
            if value.type in js_bindings.INLINE_FUNC_VALUE_TYPES:
                self._host._emit_inline_arg_function_ref(
                    value,
                    caller_spec,
                    ensure_rel,
                    caller_qn,
                    cs.RelationshipType.REFERENCES,
                    module_qn=module_qn,
                )

    def _named_iife_callee(
        self, call_node: Node, module_qn: str
    ) -> FunctionLocation | None:
        # A NAMED IIFE (`(function build () {...})()`, `function build ()
        # {...}()`) registers under its own name, minted as a duplicate
        # variant when a sibling shares it; a sequence IIFE
        # (`(0, function () {...})()`, the indirect-this idiom) registers as
        # an anonymous node. Neither the positional iife_* names nor
        # bare-name resolution can reach them safely, so resolve the callee
        # NODE by its own source span to the exact minted qn. The plain
        # anonymous parenthesised/direct forms keep their positional names
        # and are excluded here.
        func_child = call_node.child_by_field_name(cs.FIELD_FUNCTION)
        if func_child is None:
            return None
        target, paren_peels, other_peels = _peel_iife_callee_wrappers(func_child)
        if target.type not in (cs.TS_FUNCTION_EXPRESSION, cs.TS_GENERATOR_FUNCTION):
            return None
        # The plain anonymous forms (bare callee, or exactly one paren) keep
        # their positional iife_direct_/iife_func_ names; everything else
        # registers under its own name or as an anonymous node and resolves
        # only by span.
        if other_peels == 0 and paren_peels <= 1 and not iife_callee_name(target):
            return None
        return self._host._recorded_caller(target, module_qn)

    def _branch_callee_functions(
        self, call_node: Node, module_qn: str
    ) -> list[FunctionLocation]:
        # A BRANCH-valued callee (`(c ? f : g)()`, `(f || g)()`) may invoke
        # ANY of its inline function operands, and no single node can be the
        # span-resolved callee. Reference each operand so a function whose
        # only use is such a call (and everything only it calls) stays live:
        # a sound liveness over-approximation (issue #1002's family).
        func_child = call_node.child_by_field_name(cs.FIELD_FUNCTION)
        if func_child is None:
            return []
        locations: list[FunctionLocation] = []
        branched = False
        stack: list[Node] = [func_child]
        while stack:
            node = stack.pop()
            node_type = node.type
            if (
                node_type
                in (
                    cs.TS_FUNCTION_EXPRESSION,
                    cs.TS_GENERATOR_FUNCTION,
                    cs.TS_ARROW_FUNCTION,
                )
                and (loc := self._host._recorded_caller(node, module_qn)) is not None
            ):
                locations.append(loc)
            elif _branch_operands(node, stack):
                branched = True
        return locations if branched else []

    def judge_member_pick(
        self, ctx: JsTsCallScan, call_node: Node, callee_info: tuple[str, str]
    ) -> tuple[str, str] | None:
        # The resolver binds `recv.m()` on a receiver it could not type to the
        # one visible class defining `m`, and calls that a heuristic (issue
        # #2609). What the receiver is declared as here decides further: a
        # first-party type owning `m` (itself or a base) makes the binding
        # the one the type implies, so it is exact; a type the project does
        # not define (`Map`, a library class, `any`) cannot be that class, so
        # there is no edge, as for a local `new Map()`. Anything else stays
        # the heuristic it is.
        if self._host._verdict != cs.EdgeResolution.HEURISTIC:
            return callee_info
        func = call_node.child_by_field_name(cs.FIELD_FUNCTION)
        if func is not None and func.type in cs.TS_JS_PREFIXED_CALLEES:
            # `await recv.m<T>(x)`: the receiver sits under the operator.
            func = prefixed_generic_callee(func)
        if func is None or func.type != cs.TS_MEMBER_EXPRESSION:
            return callee_info
        declared = self._receiver_declaration(
            ctx, func.child_by_field_name(cs.FIELD_OBJECT)
        )
        if declared is None:
            return callee_info
        if declared.foreign:
            return None
        owner_qn = callee_info[1].rpartition(cs.SEPARATOR_DOT)[0]
        if owner_qn in self._resolver._mro(
            self._resolver._follow_reexports(declared.qn)
        ):
            self._host._verdict = cs.EdgeResolution.EXACT
        return callee_info

    def _receiver_declaration(
        self, ctx: JsTsCallScan, receiver: Node | None
    ) -> _ReceiverDeclaration | None:
        # Wrappers that keep the operand's type are looked through; an `as`
        # cast asserts its own type, which is then the declaration.
        while receiver is not None and receiver.type in (
            cs.TS_PARENTHESIZED_EXPRESSION,
            cs.TS_NON_NULL_EXPRESSION,
            cs.TS_SATISFIES_EXPRESSION,
        ):
            receiver = receiver.named_child(0)
        if receiver is None:
            return None
        if receiver.type == cs.TS_AS_EXPRESSION:
            cast = receiver.named_children[-1] if receiver.named_children else None
            return self._type_declaration(cast, ctx.module_qn) if cast else None
        if receiver.type == cs.TS_THIS:
            if ctx.class_context and _instance_this(receiver):
                return _ReceiverDeclaration(ctx.class_context, foreign=False)
            return None
        if receiver.type == cs.TS_IDENTIFIER:
            return self._identifier_declaration(ctx, receiver)
        if receiver.type == cs.TS_MEMBER_EXPRESSION:
            return self._this_field_declaration(ctx, receiver)
        return None

    def _identifier_declaration(
        self, ctx: JsTsCallScan, ident: Node
    ) -> _ReceiverDeclaration | None:
        name = safe_decode_text(ident)
        if not name:
            return None
        intro = _innermost_introduction(ident, name)
        if intro is None:
            return None
        kind, value = intro
        if kind == js_bindings.DECL_PARAM and value is not None:
            return self._param_declaration(ctx, value, name)
        if kind == js_bindings.DECL_PLAIN and value is not None:
            declarator = value.parent
            annotation = (
                declarator.child_by_field_name(cs.FIELD_TYPE)
                if declarator is not None
                and declarator.type == cs.TS_VARIABLE_DECLARATOR
                else None
            )
            if annotation is not None:
                return self._annotation_declaration(annotation, ctx.module_qn)
        class_qn = self._introduction_class_qn(
            kind, value, name, ctx.module_qn, depth=0
        )
        return _ReceiverDeclaration(class_qn, foreign=False) if class_qn else None

    def _param_declaration(
        self, ctx: JsTsCallScan, callable_node: Node, name: str
    ) -> _ReceiverDeclaration | None:
        annotation = _param_annotation(callable_node, name)
        if annotation is not None:
            return self._annotation_declaration(annotation, ctx.module_qn)
        jsdoc_type = _jsdoc_param_type(callable_node, name, ctx.language)
        if jsdoc_type is None:
            return None
        return self._jsdoc_type_declaration(jsdoc_type, callable_node, ctx.module_qn)

    def _jsdoc_type_declaration(
        self, type_text: str, anchor: Node, module_qn: str
    ) -> _ReceiverDeclaration | None:
        # Only the plain forms are read: `{A}`, `{?A}`, `{A=}`, `{ns.A}`, and
        # `{*}` for any. A union, a record or a function type stays unread.
        if type_text in cs.JSDOC_ANY_TYPES:
            return _ReceiverDeclaration(type_text, foreign=True)
        bare = type_text.strip(cs.JSDOC_TYPE_MODIFIER_CHARS)
        if not re.fullmatch(cs.JSDOC_TYPE_NAME_PATTERN, bare):
            return None
        return self._type_name_declaration(bare, anchor, module_qn)

    def _this_field_declaration(
        self, ctx: JsTsCallScan, member: Node
    ) -> _ReceiverDeclaration | None:
        # `this.f` on an instance: the field's declared type when it has one;
        # otherwise the class every value instance code gives it (initialiser,
        # `this.f = ...` in the class's own methods) constructs, when they all
        # agree. A static member's `this` is the class, whose `f` is another
        # property altogether.
        obj = member.child_by_field_name(cs.FIELD_OBJECT)
        prop = member.child_by_field_name(cs.FIELD_PROPERTY)
        if (
            not _instance_this(obj)
            or prop is None
            or not (field := safe_decode_text(prop))
        ):
            return None
        class_node = member.parent
        while class_node is not None and class_node.type not in _ANY_CLASS_NODES:
            class_node = class_node.parent
        if class_node is None:
            return None
        annotation, values = _declared_field_parts(class_node, field, ctx.language)
        if annotation is not None:
            return self._annotation_declaration(annotation, ctx.module_qn)
        writes = _this_field_writes(class_node, field)
        if writes is None:
            return None
        return self._agreed_class(values + writes, ctx.module_qn)

    def _agreed_class(
        self, values: list[Node], module_qn: str
    ) -> _ReceiverDeclaration | None:
        # The one class every value constructs; none, or any disagreement or
        # untypable value, proves nothing.
        class_qns = {
            self._value_type_class_qn(value, module_qn, depth=0) for value in values
        }
        if len(class_qns) != 1 or (class_qn := class_qns.pop()) is None:
            return None
        return _ReceiverDeclaration(class_qn, foreign=False)

    def _annotation_declaration(
        self, annotation: Node, module_qn: str
    ) -> _ReceiverDeclaration | None:
        inner = next(iter(annotation.named_children), None)
        return self._type_declaration(inner, module_qn) if inner else None

    def _type_declaration(
        self, node: Node, module_qn: str
    ) -> _ReceiverDeclaration | None:
        node_type = node.type
        if node_type in (cs.TS_PREDEFINED_TYPE, cs.TS_ARRAY_TYPE, cs.TS_TUPLE_TYPE):
            # `any`, `unknown`, `string`, `A[]`: nothing the project defines.
            return _ReceiverDeclaration(safe_decode_text(node) or "", foreign=True)
        if node_type == cs.TS_PARENTHESIZED_TYPE:
            inner = next(iter(node.named_children), None)
            return self._type_declaration(inner, module_qn) if inner else None
        if node_type == cs.TS_UNION_TYPE:
            return self._union_declaration(node, module_qn)
        if node_type == cs.TS_TYPE_IDENTIFIER:
            name = safe_decode_text(node)
            return self._type_name_declaration(name, node, module_qn) if name else None
        if node_type == cs.TS_GENERIC_TYPE:
            return self._generic_declaration(node, module_qn)
        if node_type == cs.TS_NESTED_TYPE_IDENTIFIER:
            name = safe_decode_text(node)
            return self._type_name_declaration(name, node, module_qn) if name else None
        return None

    def _union_declaration(
        self, node: Node, module_qn: str
    ) -> _ReceiverDeclaration | None:
        # `A | null` declares A. A wider union declares nothing usable unless
        # no member is first-party (`string | number`).
        members = [
            child
            for child in node.named_children
            if (safe_decode_text(child) or "") not in cs.TS_NULLISH_TYPE_TEXTS
        ]
        if len(members) == 1:
            return self._type_declaration(members[0], module_qn)
        declared = [self._type_declaration(member, module_qn) for member in members]
        if declared and all(d is not None and d.foreign for d in declared):
            return _ReceiverDeclaration(safe_decode_text(node) or "", foreign=True)
        return None

    def _generic_declaration(
        self, node: Node, module_qn: str
    ) -> _ReceiverDeclaration | None:
        name_node = node.child_by_field_name(cs.FIELD_NAME)
        name = safe_decode_text(name_node) if name_node is not None else None
        if not name:
            return None
        declared = self._type_name_declaration(name, node, module_qn)
        # TypeScript's own utility types are no value type of their own and
        # are read through. A name the project defines or a package exports
        # (`Wrapper<Cache>`, or an imported `Readonly`) is that type, whatever
        # its arguments, so only a name resolving to nothing is a utility.
        if declared is not None or name in cs.TS_UNSETTLED_UTILITY_TYPES:
            return declared
        if name in cs.TS_MEMBER_PRESERVING_UTILITY_TYPES:
            # `Readonly<A>` and friends keep A's members: A is the declaration.
            arguments = node.child_by_field_name(cs.TS_FIELD_TYPE_ARGUMENTS)
            first = next(iter(arguments.named_children), None) if arguments else None
            return self._type_declaration(first, module_qn) if first else None
        return None

    def _type_name_declaration(
        self, name: str, anchor: Node, module_qn: str
    ) -> _ReceiverDeclaration | None:
        # A type name is first-party when an enclosing scope declares it, an
        # import supplies it from the project, or the module registers it.
        # A type parameter or a nested declaration is first-party too but has
        # no qn to compare, so it declares nothing usable. Foreign takes
        # proof: an import from a package, or a platform global (`Map`,
        # `URLSearchParams`, `HTMLElement`). Any other name may be a type
        # this pass cannot see (a JSDoc `@typedef`, a class-level `@template`,
        # an ambient declaration), so it declares nothing usable either.
        head, _, rest = name.partition(cs.SEPARATOR_DOT)
        top_level = _lexical_type(anchor, head)
        if top_level is not None:
            return (
                _ReceiverDeclaration(
                    f"{module_qn}{cs.SEPARATOR_DOT}{name}", foreign=False
                )
                if top_level
                else None
            )
        import_map = self._resolver.import_processor.import_mapping.get(module_qn) or {}
        if head in import_map:
            if self._resolver._is_external_import(head, module_qn):
                return _ReceiverDeclaration(name, foreign=True)
            head_qn = self._resolver._resolve_class_qn_from_type(
                head, import_map, module_qn
            )
            qn = f"{head_qn}{cs.SEPARATOR_DOT}{rest}" if rest else head_qn
            return _ReceiverDeclaration(qn, foreign=False) if qn else None
        local_qn = f"{module_qn}{cs.SEPARATOR_DOT}{name}"
        if local_qn in self._resolver.function_registry:
            return _ReceiverDeclaration(local_qn, foreign=False)
        if name in cs.JS_TS_GLOBAL_RECEIVER_TYPES:
            return _ReceiverDeclaration(name, foreign=True)
        return None

    def emit_this_dispatch(
        self,
        caller_spec: tuple[str, str, str],
        class_context: str,
        call_name: str,
        ensure_rel: Callable[..., None],
    ) -> None:
        _, _, self_method = call_name.partition(cs.SEPARATOR_DOT)
        if self_method and cs.SEPARATOR_DOT not in self_method:
            for target_type, target_qn in self._resolver.self_dispatch_targets(
                class_context, self_method
            ):
                for variant in self._resolver.function_registry.variants(target_qn):
                    ensure_rel(
                        caller_spec,
                        cs.RelationshipType.CALLS,
                        (target_type, cs.KEY_QUALIFIED_NAME, variant),
                    )

    def emit_member_twins(
        self,
        caller_spec: tuple[str, str, str],
        callee_qn: str,
        ensure_rel: Callable[..., None],
    ) -> None:
        # A JS member call that bound one twin of a double-registered
        # prototype method (`this.lookup()` -> module-flat `view.lookup`,
        # leaving `View.lookup` dead) edges the same-module same-name
        # member twin too (duplicate-QN keep-both design; revive-only).
        for twin_type, twin_qn in sorted(
            self._resolver.js_ts.js_member_twin_targets(callee_qn)
        ):
            for variant in self._resolver.function_registry.variants(twin_qn):
                ensure_rel(
                    caller_spec,
                    cs.RelationshipType.CALLS,
                    (twin_type, cs.KEY_QUALIFIED_NAME, variant),
                )

    def _getter_read_class_qn(
        self,
        member_node: Node,
        class_context: str | None,
        local_var_types: dict[str, str] | None,
        module_qn: str,
    ) -> str | None:
        # The class that owns a getter read `recv.getter`, resolved from the
        # RECEIVER's type only: `this`/`super` is the enclosing class, and a
        # plain identifier is typed via local_var_types then resolved to its
        # class qn. Any other receiver (an unknown local, a chained expression)
        # yields None so no edge is fabricated. Resolving by receiver type (not
        # the name-global trie) is what keeps a same-named getter on an
        # unrelated class from being falsely revived.
        recv, cast_type = _peel_getter_receiver(
            member_node.child_by_field_name(cs.FIELD_OBJECT)
        )
        if cast_type is not None:
            return (
                self._resolver._resolve_class_name(cast_type, module_qn)
                if cast_type
                else None
            )
        if recv is None:
            return None
        if recv.type == cs.TS_THIS:
            # `this` binds to the class instance only when the read is lexically
            # inside a CLASS method or class-field (arrows are transparent);
            # inside a nested plain `function`/function-expression, or an
            # object-literal method, `this` rebinds, so it is NOT the class and
            # must not link (issue #971's rebinding rule, extended to reject an
            # object-literal method whose method_definition binds `this` to the
            # object, not the class). `super.getter` is deliberately NOT handled:
            # it reads the BASE class's getter, and class_context is the current
            # class, so mapping it here would target the wrong (or a missing)
            # method; a conservative miss is preferred to a wrong edge.
            return (
                class_context if _this_binds_to_enclosing_class(member_node) else None
            )
        if recv.type == cs.TS_IDENTIFIER and (recv_name := safe_decode_text(recv)):
            # A typed local already stores its resolved class qn; a bare
            # class-name receiver (a static getter read, `Box.thing`) resolves
            # through the class registry. An unknown receiver (an untyped
            # parameter) matches neither and yields None, so the name-global
            # trie never fires and an unrelated same-named getter is not revived.
            if (recv_type := (local_var_types or {}).get(recv_name)) is not None:
                return recv_type
            return self._resolver._resolve_class_name(recv_name, module_qn)
        return None

    @host_site_scoped
    def ingest_property_reads(
        self,
        caller_node: Node,
        caller_spec: tuple[str, str, str],
        caller_qn: str,
        module_qn: str,
        local_var_types: dict[str, str] | None,
        class_context: str | None,
        lang_config: LanguageSpec,
        prop_names: set[str],
    ) -> None:
        # A getter read is a member_expression (`this.relation`,
        # `loadMap.mainLoadMapItem`), never an invocation (issue #984). The
        # return/assignment reference passes only inspect the direct value, so
        # a getter read nested in a ternary, member chain, `if` condition, or
        # binary expression emitted nothing and dead-code flagged it
        # (TypeORM's JoinAttribute.relation, LoadMap.mainLoadMapItem).
        # Emit a REFERENCES edge (a read invokes the getter but is not a call, so
        # the call graph stays invocation-only) for every member_expression whose
        # property resolves to a getter ON THE RECEIVER'S OWN CLASS, wherever it
        # appears in the body. The walk is position-agnostic, so a getter read
        # inside a ternary, member chain (`loadMap.mainLoadMapItem.entity` visits
        # the inner `loadMap.mainLoadMapItem`), or `if` condition links the same
        # as a bare read. The candidate qn is built from the receiver's class and
        # emitted only when it is a registered property, so a same-named data
        # field, a same-named regular method, or a same-named getter on an
        # unrelated class never fabricates an edge.
        function_types = lang_config.function_node_types
        class_types = lang_config.class_node_types
        seen: set[str] = set()
        stack = list(caller_node.children)
        while stack:
            node = self._host._site_node = stack.pop()
            node_type = node.type
            # A nested class or NON-arrow function owns its own reads (and
            # rebinds `this`), so it is processed as its own caller; do not
            # descend. An arrow is transparent (it captures the enclosing `this`
            # and shares the outer locals), so descend into it or getter reads in
            # `.map`/`.find`/`.forEach` callbacks are missed.
            if (
                node_type in function_types and node_type != cs.TS_ARROW_FUNCTION
            ) or node_type in class_types:
                continue
            if node_type == cs.TS_MEMBER_EXPRESSION:
                self._emit_getter_read(
                    node,
                    caller_spec,
                    caller_qn,
                    module_qn,
                    local_var_types,
                    class_context,
                    prop_names,
                    seen,
                )
            stack.extend(node.children)

    def _emit_getter_read(
        self,
        node: Node,
        caller_spec: tuple[str, str, str],
        caller_qn: str | None,
        module_qn: str,
        local_var_types: dict[str, str] | None,
        class_context: str | None,
        prop_names: set[str],
        seen: set[str],
    ) -> None:
        registry = self._resolver.function_registry
        prop = node.child_by_field_name(cs.FIELD_PROPERTY)
        if (
            not _member_is_write_target(node)
            and prop is not None
            and (name := safe_decode_text(prop)) in prop_names
            and (
                class_qn := self._getter_read_class_qn(
                    node, class_context, local_var_types, module_qn
                )
            )
        ):
            # Resolve the getter on the receiver's class OR an ancestor
            # (an inherited getter still invokes real code); emit only
            # when the resolved target is a registered property, so a
            # same-named data field or regular method never links.
            resolved = self._resolver._try_resolve_method(class_qn, name)
            if resolved is not None and registry.is_property(resolved[1]):
                res_label, res_qn = resolved
                for target_qn in registry.variants(res_qn):
                    if target_qn != caller_qn and target_qn not in seen:
                        seen.add(target_qn)
                        self._host._emit_rel(
                            caller_spec,
                            cs.RelationshipType.REFERENCES,
                            (res_label, cs.KEY_QUALIFIED_NAME, target_qn),
                        )

    def is_unowned_scope(self, node: Node, module_qn: str) -> bool:
        # An anonymous arrow/function/generator expression that gets no caller
        # node of its own (no name, no binding name): a `.map()`/`cell`/
        # forwardRef callback. Its calls bubble up to the enclosing named
        # scope (_attributable_func_nodes), and so must its JSX references:
        # the enclosing JSX walk continues through it.
        if node.type not in (
            cs.TS_ARROW_FUNCTION,
            cs.TS_FUNCTION_EXPRESSION,
            cs.TS_GENERATOR_FUNCTION,
        ):
            return False
        if self._host._get_node_name(node) or js_ts_utils.arrow_binding_name(node):
            return False
        # A nameless FUNCTION EXPRESSION the definition pass registered under
        # a name (`x: function () {}`) has neither, but IS a node with its own
        # walk, so a reference inside it belongs to that node alone. Without
        # this the enclosing scope emitted a second copy of every REFERENCES
        # edge (issue #1932).
        #
        # Deliberately NOT extended to arrows, and the reason is narrower
        # than it looks. An arrow that is an object value in a config array
        # (`{ cell: ({row}) => <CopyId/> }`, TanStack columns) is registered
        # under its key too, so owning it would MOVE the edge to `cols.cell`
        # rather than drop it -- the component stays reachable either way.
        # What it would break is the edge's SOURCE, which
        # test_jsx_component_in_config_callback_is_referenced asserts is the
        # module. Changing that is a behaviour change for consumers, so
        # arrows keep bubbling here.
        #
        # A generator expression has no such consumer and duplicates exactly
        # as a function expression does, so it is included.
        #
        # All three types are identical on both underlying signals
        # (_attributable_func_nodes says unattributable, the recorded name
        # says is_named), so this split is an enumeration, not a property.
        if node.type not in (
            cs.TS_FUNCTION_EXPRESSION,
            cs.TS_GENERATOR_FUNCTION,
        ):
            return True
        recorded = self._host._recorded_caller(node, module_qn)
        return recorded is None or not recorded.is_named

    def reset_run_state(self) -> None:
        self._file_receiver_bindings.clear()
        self.symbol_constants.clear()
        self._symbol_assignments.clear()
        self.symbol_member_types.clear()
        self._proto_evidence_cache.clear()

    def collect_symbol_bindings(self, root_node: Node, module_qn: str) -> None:
        # One walk per JS/TS file collecting (a) symbol CONSTANT definitions
        # (`kX = Symbol('...')` declarators, `kX: Symbol('...')` pairs) and
        # (b) raw symbol-keyed installations (`{ [kX]: value }` pairs,
        # `obj[kX] = value` assignments) with the value's class resolved
        # immediately (strings only). Whether a key actually names a symbol
        # constant is judged in finalize, once every file's constants are in.
        stack: list[Node] = [root_node]
        while stack:
            node = stack.pop()
            if node.type == cs.TS_VARIABLE_DECLARATOR:
                self._collect_symbol_declarator(node, module_qn)
            elif node.type in (
                cs.TS_PAIR,
                cs.TS_PUBLIC_FIELD_DEFINITION,
                cs.TS_JS_FIELD_DEFINITION,
            ):
                self._collect_symbol_pair(node, module_qn)
            elif node.type == cs.TS_JS_ASSIGNMENT_EXPRESSION:
                self._collect_symbol_subscript_assignment(node, module_qn)
            stack.extend(node.children)

    def _collect_symbol_declarator(self, node: Node, module_qn: str) -> None:
        # Only a MODULE-scoped binding mints a shared symbol constant; a
        # function-local `k = Symbol(...)` is a local value whose name must
        # not hijack same-spelled reads elsewhere in the module.
        target = node.child_by_field_name(cs.FIELD_NAME)
        if (
            target is not None
            and target.type == cs.TS_IDENTIFIER
            and _is_symbol_mint(node.child_by_field_name(cs.FIELD_VALUE))
            and not _inside_callable(node)
            and (name := safe_decode_text(target))
        ):
            self.symbol_constants.add(f"{module_qn}{cs.SEPARATOR_DOT}{name}")

    def _collect_symbol_pair(self, node: Node, module_qn: str) -> None:
        # An object-literal pair OR a class field: the key sits in `key`,
        # `name` (TS field) or `property` (JS field), the value in `value`.
        key = (
            node.child_by_field_name(cs.FIELD_KEY)
            or node.child_by_field_name(cs.FIELD_NAME)
            or node.child_by_field_name(cs.FIELD_PROPERTY)
        )
        value = node.child_by_field_name(cs.FIELD_VALUE)
        if key is None or value is None:
            return
        if key.type == cs.TS_PROPERTY_IDENTIFIER and _is_symbol_mint(value):
            if not _inside_callable(node) and (name := safe_decode_text(key)):
                self.symbol_constants.add(f"{module_qn}{cs.SEPARATOR_DOT}{name}")
            return
        if key.type == cs.TS_JS_COMPUTED_PROPERTY_NAME:
            self._collect_computed_symbol_pair(key, value, module_qn)

    def _collect_computed_symbol_pair(
        self, key: Node, value: Node, module_qn: str
    ) -> None:
        # `[kSym]: new Impl()` installs a class under a symbol key.
        index = next((c for c in key.named_children if c.type != cs.TS_COMMENT), None)
        if (
            index is not None
            and index.type == cs.TS_IDENTIFIER
            and (sym_name := safe_decode_text(index))
            and (class_qn := self._symbol_value_class_qn(value, module_qn))
        ):
            self._symbol_assignments.append((module_qn, sym_name, class_qn))

    def _collect_symbol_subscript_assignment(self, node: Node, module_qn: str) -> None:
        left = node.child_by_field_name(cs.FIELD_LEFT)
        value = node.child_by_field_name(cs.TS_FIELD_RIGHT)
        if left is None or value is None or left.type != cs.TS_SUBSCRIPT_EXPRESSION:
            return
        index = left.child_by_field_name(cs.TS_FIELD_INDEX)
        if (
            index is not None
            and index.type == cs.TS_IDENTIFIER
            and (sym_name := safe_decode_text(index))
            and (class_qn := self._symbol_value_class_qn(value, module_qn))
        ):
            self._symbol_assignments.append((module_qn, sym_name, class_qn))

    def _symbol_value_class_qn(self, value: Node, module_qn: str) -> str | None:
        # Resolve the INSTALLED value to a class qn, entirely at sweep time
        # so only strings are kept.
        return self._value_type_class_qn(value, module_qn, depth=0)

    def _value_type_class_qn(
        self, value: Node | None, module_qn: str, depth: int
    ) -> str | None:
        # Type a VALUE expression to a registered class qn: a direct
        # construction, a factory call (bare, member/static, or one whose
        # returned OBJECT the caller destructures), or an identifier chased
        # to its declarator. Depth-capped: each hop crosses at most one
        # binding or one function return.
        if value is None or depth > 3:
            return None
        node_type = value.type
        if (
            node_type == cs.TS_PARENTHESIZED_EXPRESSION
            or node_type in cs.TS_CAST_WRAPPER_TYPES
        ):
            inner = next(
                (c for c in value.named_children if c.type != cs.TS_COMMENT), None
            )
            return self._value_type_class_qn(inner, module_qn, depth)
        if node_type == cs.TS_NEW_EXPRESSION:
            ctor = js_ts_utils.extract_constructor_name(value)
            if not ctor:
                return None
            return self._registered_class(
                ctor, module_qn, allow_constructor_function=True
            )
        if node_type == cs.TS_IDENTIFIER:
            return self._identifier_class_qn(value, module_qn, depth)
        if node_type == cs.TS_CALL_EXPRESSION:
            return self._call_result_class_qn(value, module_qn)
        return None

    def _registered_class(
        self, name: str, module_qn: str, allow_constructor_function: bool = False
    ) -> str | None:
        # allow_constructor_function: a `new X(...)` target may be an
        # old-style CONSTRUCTOR FUNCTION with prototype methods (fastify's
        # ContentTypeParser), registered as Function; accepting it lets its
        # prototype methods resolve. Never allowed for name hints, where a
        # function qn is a factory, not a type.
        registry = self._resolver.function_registry
        candidates = [name, *self._name_candidates(name, module_qn)]
        for candidate in candidates:
            if candidate not in registry:
                continue
            node_type = registry[candidate]
            if node_type == cs.NodeLabel.CLASS:
                return candidate
            if (
                allow_constructor_function
                and node_type == cs.NodeLabel.FUNCTION
                and self._has_prototype_members(candidate)
            ):
                return candidate
        return None

    def _member_is_exposed(self, class_qn: str, method: str) -> bool:
        # A CLASS's registry members are its real methods. A CONSTRUCTOR
        # FUNCTION shares its `module.fn.name` namespace with its NESTED
        # private helpers, so each member needs its own
        # `Fn.prototype.<method> = ...` evidence before the qn may be
        # treated as callable from outside.
        registry = self._resolver.function_registry
        if class_qn in registry and registry[class_qn] == cs.NodeLabel.CLASS:
            return True
        return self._has_prototype_members(class_qn, member=method)

    def _has_prototype_members(self, fn_qn: str, member: str | None = None) -> bool:
        cache_key = (fn_qn, member)
        if cache_key in self._proto_evidence_cache:
            return self._proto_evidence_cache[cache_key]
        result = self._scan_prototype_members(fn_qn, member)
        self._proto_evidence_cache[cache_key] = result
        return result

    def _scan_prototype_members(self, fn_qn: str, member: str | None = None) -> bool:
        # Constructor EVIDENCE: at least one `Fn.prototype.x = ...` in the
        # function's module (any member), or, when `member` is given, an
        # assignment for that SPECIFIC member. Without it, `module.fn.name`
        # registry entries under the function are its NESTED helpers, not
        # methods, and accepting them would manufacture edges.
        fn_module, _, fn_leaf = fn_qn.rpartition(cs.SEPARATOR_DOT)
        type_inference = self._resolver.type_inference
        file_path = type_inference.module_qn_to_file_path.get(fn_module)
        if file_path is None or not (entry := type_inference.ast_cache.load(file_path)):
            return False
        root_node, _language = entry
        stack: list[Node] = [root_node]
        while stack:
            node = stack.pop()
            if node.type == cs.TS_JS_ASSIGNMENT_EXPRESSION and (
                _prototype_assignment_matches(node, fn_leaf, member)
            ):
                return True
            stack.extend(node.children)
        return False

    def _name_candidates(self, name: str, module_qn: str) -> list[str]:
        # The qns a bare name may denote: the imported member, the imported
        # MODULE's same-named export (`const P = require('./p.js')` where
        # p.js exports its constructor), or the module-local binding.
        candidates: list[str] = []
        import_map = self._resolver.import_processor.import_mapping.get(module_qn)
        if import_map is not None and name in import_map:
            imported = import_map[name]
            candidates.append(imported)
            if not imported.endswith(f"{cs.SEPARATOR_DOT}{name}"):
                candidates.append(f"{imported}{cs.SEPARATOR_DOT}{name}")
        candidates.append(f"{module_qn}{cs.SEPARATOR_DOT}{name}")
        return candidates

    def _identifier_class_qn(
        self, ident: Node, module_qn: str, depth: int
    ) -> str | None:
        # Chase the identifier to the INTRODUCTION it actually reads: every
        # binding of the name visible from this position (parameters, catch
        # and loop bindings, lexical and var declarators) is collected with
        # the byte span of the scope it governs, and the innermost enclosing
        # one wins. A parameter or other opaque introduction is statically
        # unknowable and refuses; name-match-anywhere resolution leaks (the
        # #988 lesson) and is never used.
        name = safe_decode_text(ident)
        if not name:
            return None
        intro = _innermost_introduction(ident, name)
        if intro is None:
            return None
        kind, value = intro
        return self._introduction_class_qn(kind, value, name, module_qn, depth)

    def _introduction_class_qn(
        self, kind: str, value: Node | None, name: str, module_qn: str, depth: int
    ) -> str | None:
        if kind == js_bindings.DECL_PLAIN:
            return self._value_type_class_qn(value, module_qn, depth + 1)
        if kind == js_bindings.DECL_DESTRUCTURED and value is not None:
            return self._destructured_return_class(value, name, module_qn, depth + 1)
        return None

    def _call_result_class_qn(self, call: Node, module_qn: str) -> str | None:
        func = call.child_by_field_name(cs.FIELD_FUNCTION)
        if func is None:
            return None
        if func.type == cs.TS_IDENTIFIER:
            fn_name = safe_decode_text(func)
            if not fn_name:
                return None
            return self._factory_return_class(self._symbol_name_qn(fn_name, module_qn))
        if func.type == cs.TS_MEMBER_EXPRESSION:
            return self._member_call_result_class(func, module_qn)
        return None

    def _member_call_result_class(self, func: Node, module_qn: str) -> str | None:
        obj = func.child_by_field_name(cs.FIELD_OBJECT)
        prop = func.child_by_field_name(cs.FIELD_PROPERTY)
        if (
            obj is None
            or prop is None
            or obj.type != cs.TS_IDENTIFIER
            or not (owner := safe_decode_text(obj))
            or not (method := safe_decode_text(prop))
        ):
            return None
        js = self._resolver.type_inference.js_type_inference
        hint = js._infer_js_method_return_type(
            f"{owner}{cs.SEPARATOR_DOT}{method}", module_qn
        )
        if hint and (resolved := self._registered_class(hint, module_qn)):
            return resolved
        # A static assigned OUTSIDE the class body (`C.build = build`;
        # fastify's SchemaController.buildSchemaController) is invisible
        # to the class-body method finder. Only a VERIFIED assignment in
        # the class's module may redirect; a same-named free function
        # alone must not (it can be unrelated).
        class_qn = js._resolve_js_class_name(owner, module_qn)
        if class_qn is None:
            return None
        assigned = self._static_assigned_factory(class_qn, method)
        if assigned is None:
            return None
        class_module = class_qn.rsplit(cs.SEPARATOR_DOT, 1)[0]
        if assigned.type == cs.TS_IDENTIFIER:
            fn_name = safe_decode_text(assigned)
            if not fn_name:
                return None
            return self._factory_return_class(
                f"{class_module}{cs.SEPARATOR_DOT}{fn_name}"
            )
        if assigned.type in cs.JS_TS_FUNCTION_NODES:
            return self._function_return_class(
                assigned,
                f"{class_module}{cs.SEPARATOR_DOT}{method}",
                class_module,
            )
        return None

    def _static_assigned_factory(self, class_qn: str, method: str) -> Node | None:
        # The right-hand side of `ClassLeaf.method = ...` in the class's
        # module, or None when no such assignment exists.
        class_module, _, class_leaf = class_qn.rpartition(cs.SEPARATOR_DOT)
        type_inference = self._resolver.type_inference
        file_path = type_inference.module_qn_to_file_path.get(class_module)
        if file_path is None or not (entry := type_inference.ast_cache.load(file_path)):
            return None
        root_node, _language = entry
        stack: list[Node] = [root_node]
        while stack:
            node = stack.pop()
            if node.type == cs.TS_JS_ASSIGNMENT_EXPRESSION:
                left = node.child_by_field_name(cs.FIELD_LEFT)
                if (
                    left is not None
                    and left.type == cs.TS_MEMBER_EXPRESSION
                    and (obj := left.child_by_field_name(cs.FIELD_OBJECT)) is not None
                    and obj.type == cs.TS_IDENTIFIER
                    and safe_decode_text(obj) == class_leaf
                    and (prop := left.child_by_field_name(cs.FIELD_PROPERTY))
                    is not None
                    and safe_decode_text(prop) == method
                ):
                    return node.child_by_field_name(cs.TS_FIELD_RIGHT)
            stack.extend(node.children)
        return None

    def _factory_return_class(self, fn_qn: str | None) -> str | None:
        # Once per factory per pass: every call-result receiver a factory
        # types asked again, re-reading its returns each time (issue #2453).
        # The answer depends on the registry and the parsed trees only, and
        # `reset_resolution_caches` drops it when either may have changed.
        if fn_qn is None:
            return None
        memo = self._factory_classes
        if fn_qn not in memo:
            memo[fn_qn] = self._read_factory_return_class(fn_qn)
        return memo[fn_qn]

    def _read_factory_return_class(self, fn_qn: str) -> str | None:
        registry = self._resolver.function_registry
        if fn_qn not in registry:
            return None
        fn_node = self._function_node_by_qn(fn_qn)
        if fn_node is None:
            return None
        factory_module = fn_qn.rsplit(cs.SEPARATOR_DOT, 1)[0]
        return self._function_return_class(fn_node, fn_qn, factory_module)

    def _function_return_class(
        self, fn_node: Node, fn_qn: str, factory_module: str
    ) -> str | None:
        if ctor := _direct_return_ctor(fn_node):
            return self._registered_class(ctor, factory_module)
        js = self._resolver.type_inference.js_type_inference
        returned = js._analyze_return_statements(fn_node, fn_qn)
        if returned is None:
            return None
        return self._registered_class(returned, factory_module)

    def _destructured_return_class(
        self, call_value: Node, prop_name: str, module_qn: str, depth: int
    ) -> str | None:
        # `const { name } = f(...)`: the binding's type is the type of the
        # `name` property in f's returned object literal.
        if depth > 3 or call_value.type != cs.TS_CALL_EXPRESSION:
            return None
        func = call_value.child_by_field_name(cs.FIELD_FUNCTION)
        if func is None or func.type != cs.TS_IDENTIFIER:
            return None
        fn_name = safe_decode_text(func)
        if not fn_name:
            return None
        fn_qn = self._symbol_name_qn(fn_name, module_qn)
        registry = self._resolver.function_registry
        if fn_qn is None or fn_qn not in registry:
            return None
        fn_node = self._function_node_by_qn(fn_qn)
        if fn_node is None:
            return None
        factory_module = fn_qn.rsplit(cs.SEPARATOR_DOT, 1)[0]
        # Every returned object contributes its `name` property's class;
        # divergent returns are ambiguous and refuse (never guess one path).
        candidates: set[str | None] = set()
        for obj in _own_returned_objects(fn_node):
            candidates.add(
                self._returned_object_property_class(
                    obj, prop_name, factory_module, depth
                )
            )
        candidates.discard(None)
        if len(candidates) == 1:
            return candidates.pop()
        return None

    def _returned_object_property_class(
        self, obj: Node, prop_name: str, factory_module: str, depth: int
    ) -> str | None:
        for child in obj.named_children:
            if child.type == cs.TS_PAIR:
                key = child.child_by_field_name(cs.FIELD_KEY)
                if (
                    key is not None
                    and key.type == cs.TS_PROPERTY_IDENTIFIER
                    and safe_decode_text(key) == prop_name
                ):
                    return self._value_type_class_qn(
                        child.child_by_field_name(cs.FIELD_VALUE),
                        factory_module,
                        depth + 1,
                    )
            elif (
                child.type == cs.TS_SHORTHAND_PROPERTY_IDENTIFIER
                and safe_decode_text(child) == prop_name
            ):
                return self._identifier_class_qn(child, factory_module, depth + 1)
        return None

    def _function_node_by_qn(self, fn_qn: str) -> Node | None:
        # Locate a top-level FUNCTION's definition node from its qn (the
        # class.method finder cannot: it splits qns as module.Class.method).
        module_qn, _, fn_name = fn_qn.rpartition(cs.SEPARATOR_DOT)
        type_inference = self._resolver.type_inference
        file_path = type_inference.module_qn_to_file_path.get(module_qn)
        if file_path is None or not (entry := type_inference.ast_cache.load(file_path)):
            return None
        root_node, _language = entry
        return js_bindings.named_functions(root_node).get(fn_name)

    def _symbol_name_qn(self, name: str, module_qn: str) -> str | None:
        # The qn a bare name denotes in this module: the imported member's qn
        # when the name is imported, else the module-local qn.
        import_map = self._resolver.import_processor.import_mapping.get(module_qn)
        if import_map is not None and name in import_map:
            imported = import_map[name]
            if imported.endswith(f"{cs.SEPARATOR_DOT}{name}"):
                return imported
            return f"{imported}{cs.SEPARATOR_DOT}{name}"
        return f"{module_qn}{cs.SEPARATOR_DOT}{name}"

    def finalize_symbol_bindings(self) -> None:
        # Keep only installations whose key resolves to a REGISTERED symbol
        # constant: an arbitrary computed key (`obj[i]`) must never join the
        # index, or unrelated subscript reads would type by it.
        for module_qn, sym_name, class_qn in self._symbol_assignments:
            sym_qn = self._symbol_name_qn(sym_name, module_qn)
            if sym_qn is not None and sym_qn in self.symbol_constants:
                self.symbol_member_types.setdefault(sym_qn, set()).add(class_qn)
        self._symbol_assignments.clear()

    def _symbol_member_targets(
        self, call_node: Node, module_qn: str
    ) -> tuple[list[tuple[str, str]], bool] | None:
        # `recv[kSym].method(...)` where kSym resolves to a symbol constant
        # with recorded installations: exactly ONE installed class binds the
        # call (unique=True, CALLS); several installed classes are a sound
        # candidate set (unique=False, the caller references each class's
        # matching method so liveness holds without guessing). Returns None
        # for every other shape, leaving the generic path untouched. The
        # bare-name trie fallback is SUPPRESSED for the recognised shapes:
        # the issue #989 receiver is statically unknowable by name and the
        # trie falsely revives same-named methods on unrelated classes.
        parts = _symbol_member_call_parts(call_node)
        if parts is None:
            return None
        sym_name, method = parts
        sym_qn = self._symbol_name_qn(sym_name, module_qn)
        if sym_qn is None or sym_qn not in self.symbol_constants:
            return None
        # The shape is recognised from here on: a symbol-keyed receiver is
        # never resolvable by NAME, so the generic path (whose trie fallback
        # guesses one same-named method) is suppressed. With typed
        # installations the index provides the targets; without any, the
        # site degrades to REFERENCES over EVERY method of that name, so
        # liveness survives while no wrong-class CALLS is fabricated.
        registry = self._resolver.function_registry
        classes = self.symbol_member_types.get(sym_qn)
        if classes:
            targets: list[tuple[str, str]] = [
                (registry[method_qn], method_qn)
                for class_qn in sorted(classes)
                if (method_qn := f"{class_qn}{cs.SEPARATOR_DOT}{method}") in registry
                and self._member_is_exposed(class_qn, method)
            ]
            return targets, len(classes) == 1
        name_matches = self._resolver.type_inference.simple_name_lookup.get(
            method, set()
        )
        fan: list[tuple[str, str]] = [
            (registry[qn], qn)
            for qn in sorted(name_matches)
            if qn in registry
            and registry[qn] in (cs.NodeLabel.FUNCTION, cs.NodeLabel.METHOD)
        ]
        return fan, False

    def _fn_call_receiver_binding(
        self, call_node: Node, module_qn: str
    ) -> FunctionLocation | None:
        # For `fn.call(...)` / `fn.apply(...)` (only: `.bind` produces a value
        # without invoking), peel casts/parens off the receiver and, when it is
        # a bare identifier, resolve what that identifier DENOTES at this site
        # by lexical scoping. `.bind`-style member receivers and computed
        # receivers never qualify.
        fn_child = call_node.child_by_field_name(cs.TS_FIELD_FUNCTION)
        if fn_child is None or fn_child.type != cs.TS_MEMBER_EXPRESSION:
            return None
        prop = fn_child.child_by_field_name(cs.FIELD_PROPERTY)
        if prop is None or safe_decode_text(prop) not in (
            cs.JS_METHOD_CALL,
            cs.JS_METHOD_APPLY,
        ):
            return None
        obj = fn_child.child_by_field_name(cs.FIELD_OBJECT)
        if obj is None:
            return None
        obj = unwrap_ts_value(obj)
        if obj.type != cs.TS_IDENTIFIER:
            return None
        name = safe_decode_text(obj)
        if not name:
            return None
        return self._lexical_function_binding(call_node, name, module_qn)

    def _lexical_function_binding(
        self, site: Node, name: str, module_qn: str
    ) -> FunctionLocation | None:
        # UNIQUE-RELEVANT-BINDING resolution: collect every INTRODUCTION of
        # every name in the file once (cached), each with the container whose
        # span bounds where it is usable, then keep only the introductions
        # whose container encloses THIS site (a file-wide container always
        # qualifies). The receiver resolves only when exactly ONE relevant
        # introduction remains and it IS a function registered by the
        # definition pass; two or more relevant introductions, or a
        # non-function one, mean the runtime value is unknowable and nothing
        # is emitted. Shadowing, `var` hoisting, switch/for/catch scoping and
        # duplicate declarations all collapse into "a second relevant
        # introduction", with no scope simulation; an introduction confined
        # to a SIBLING scope is irrelevant here and cannot suppress.
        root = site
        while root.parent is not None:
            root = root.parent
        bindings = self._file_receiver_bindings.get(module_qn)
        if bindings is None:
            bindings = _collect_file_bindings(root)
            self._file_receiver_bindings[module_qn] = bindings
        entries = bindings.get(name)
        if not entries:
            return None
        relevant = [
            (fn_node, container)
            for fn_node, container in entries
            if container is None
            or container.start_byte <= site.start_byte < container.end_byte
        ]
        if len(relevant) != 1:
            return None
        fn_node, _container = relevant[0]
        if fn_node is None:
            return None
        return self._host._recorded_caller(fn_node, module_qn)
