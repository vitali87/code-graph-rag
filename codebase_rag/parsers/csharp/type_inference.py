from __future__ import annotations

import re
from collections import deque
from collections.abc import Callable, Collection, Iterator, Mapping, Sequence
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

from tree_sitter import Node

from ... import constants as cs
from ...types_defs import (
    CSharpCallShape,
    CSharpGenericShape,
    FunctionLocation,
    FunctionRegistryTrieProtocol,
    FunctionSpanKey,
    LanguageQueries,
    NodeType,
    SimpleNameLookup,
)
from ...utils import qn_markers
from ..csharp_frontend import CallSiteKey
from ..frontends.protocol import ResolvedCallSite
from ..import_processor import ImportProcessor
from ..semantic_call_join import call_site_key, declared_location
from ..utils import direct_methods_by_name, safe_decode_text
from .arity import CSharpArityIndex
from .arity import spellings as arity_spellings
from .name_lookup import (
    Lookup,
    LookupKind,
    member_of,
    member_type,
    simple_name,
    simple_type_name,
    type_body,
)
from .overloads import (
    ArgumentType,
    Bound,
    Fit,
    Inferred,
    Scope,
    Scored,
    TypeKinds,
    arguments_match,
    best_candidates,
    bindings_for_base,
    by_position,
    canonical_type,
    certainly_applies,
    constant_converts,
    declared_type,
    fit,
    framework_generic,
    framework_reference,
    infer_into,
    is_tuple,
    mentioned_names,
    merged_inferences,
    parameter_types,
    predefined_converts,
    predefined_implements,
    predefined_keyword,
    resolved_qn,
    same_type,
    same_type_name,
    signed_preference,
    substitute,
    type_arguments,
    type_shape,
    unannotated,
    unshadowed,
    written_type,
)
from .utils import (
    _normalize_type_name,
    annotate_type_ref,
    binds_local,
    declared_namespace,
    generic_arity_of_type_text,
    leaf_type_segment,
    local_binder,
    signature_type_name,
    split_type_ref,
    strip_generic_arguments,
    type_parameter_constraints,
    type_parameter_names,
    unique_carrier,
)

if TYPE_CHECKING:
    from ..factory import ASTCacheProtocol

_TYPE_DECLS = (NodeType.CLASS, NodeType.INTERFACE, NodeType.ENUM)
# Sentinel: the call's target is provably EXTERNAL (a BCL/base member, a
# static call on an unregistered type). The dispatcher must emit nothing
# and must NOT fall back to the name-only trie, which would fabricate an
# edge onto an unrelated same-name first-party member.
CSHARP_EXTERNAL_TARGET: tuple[str, str] = ("", "")


def _arity(leaf: str) -> int:
    # Parameter count of a (possibly signatured) method leaf: `M(int, string)`
    # -> 2, `M` / `M()` -> 0. Only depth-0 commas separate parameters, so a
    # qualified/array type never inflates the count.
    open_idx = leaf.find(cs.CHAR_PAREN_OPEN)
    if open_idx < 0:
        return 0
    inner = leaf[open_idx + 1 : leaf.rfind(cs.CHAR_PAREN_CLOSE)]
    if not inner.strip():
        return 0
    depth = 0
    count = 1
    for ch in inner:
        if ch in "<([":
            depth += 1
        elif ch in ">)]":
            depth -= 1
        elif ch == cs.CHAR_COMMA and depth == 0:
            count += 1
    return count


def _given[T](value: T | None, empty: Callable[[], T]) -> T:
    # The caller's container, shared by reference and filled after
    # construction; a fresh one only when none was given (`or` would swap an
    # empty shared one for a new one and lose the later writes).
    return value if value is not None else empty()


def _param_types(leaf: str) -> list[str]:
    # The depth-0 parameter types of a signatured leaf, split as _arity
    # counts them: `M(int, Dictionary<K, V>)` -> ["int", "Dictionary<K, V>"].
    return parameter_types(leaf)


def _parameter_type_at(method_qn: str, position: int) -> str | None:
    # The declared type of one parameter from the qn's signature, which
    # spells types generic-free; an array (a `params` tail) is not a class
    # a `new(...)` argument could construct.
    types = _param_types(method_qn)
    if position >= len(types) or types[position].endswith(cs.CSHARP_ARRAY_SUFFIX):
        return None
    return types[position] or None


def _accepts_arg_count(
    leaf: str, arg_count: int, shape: CSharpCallShape | None
) -> bool:
    # Can a call with `arg_count` arguments bind this overload without an
    # exact arity match? FEWER arguments than parameters compiles only when
    # the omitted ones have defaults, and MORE only through a `params` tail;
    # `shape`, recorded at ingest, says which (CodeRabbit, PR #2036). A method
    # this run did not parse has no record and is judged from its signature
    # alone: the call site vouches for the defaults, and a trailing array is
    # the only tail that can be `params`.
    types = _param_types(leaf)
    if arg_count < len(types):
        return shape is None or arg_count >= shape.required
    if shape is not None:
        return shape.variadic
    return bool(types) and types[-1].endswith(cs.CSHARP_ARRAY_SUFFIX)


class _Constant(NamedTuple):
    """A literal argument as C# types it: its predefined type and, for an
    integral one, its value (`-1` is an int of -1, `300u` a uint of 300)."""

    type: str
    value: int | None = None


def _integer_constant(text: str) -> _Constant | None:
    # An integer literal's type and value: of the types its suffix allows,
    # the first the value fits (`3000000000` is a uint, `300L` a long). None
    # for one no type holds, or a spelling not read.
    body = text.lower().replace(cs.CSHARP_DIGIT_SEPARATOR, "")
    digits = body.rstrip(cs.CSHARP_INTEGER_SUFFIX_CHARS)
    candidates = cs.CSHARP_INTEGER_SUFFIX_CANDIDATES.get(body[len(digits) :], ())
    prefix, base = next(
        (
            (prefix, base)
            for prefix, base in cs.CSHARP_INTEGER_BASE_PREFIXES.items()
            if digits.startswith(prefix)
        ),
        ("", cs.CSHARP_DECIMAL_BASE),
    )
    try:
        value = int(digits.removeprefix(prefix), base)
    except ValueError:
        return None
    kind = next((k for k in candidates if value <= cs.CSHARP_INTEGRAL_MAX[k]), None)
    return _Constant(kind, value) if kind is not None else None


def _real_constant(text: str) -> _Constant:
    # A real literal's type, by its suffix: `1.5f` float, `1.5m` decimal,
    # else double.
    body = text.lower()
    suffix = body[-1:] if body[-1:] in cs.CSHARP_REAL_SUFFIX_TYPES else ""
    return _Constant(cs.CSHARP_REAL_SUFFIX_TYPES[suffix])


def _negated(constant: _Constant, written: str) -> _Constant | None:
    # A literal under a unary minus: an int stays an int, a uint becomes a
    # long, a long stays a long and a ulong has none, except the unsuffixed
    # decimal `2147483648` and `9223372036854775808`, which a minus makes the
    # least int and long. A real literal keeps its type.
    if constant.value is None:
        return constant
    if written.isdigit() and constant.value in cs.CSHARP_NEGATED_MINIMUM_TYPES:
        return _Constant(
            cs.CSHARP_NEGATED_MINIMUM_TYPES[constant.value], -constant.value
        )
    kind = cs.CSHARP_NEGATED_TYPES.get(constant.type)
    return _Constant(kind, -constant.value) if kind is not None else None


def _literal_constant(value: Node) -> _Constant | None:
    # The constant a literal argument is, a unary minus or plus on one
    # folded in; None for any other expression, whose value is not read.
    if value.type == cs.TS_CSHARP_PREFIX_UNARY_EXPRESSION:
        return _signed_constant(value)
    text = safe_decode_text(value) or ""
    if value.type == cs.TS_CSHARP_INTEGER_LITERAL:
        return _integer_constant(text)
    if value.type == cs.TS_CSHARP_REAL_LITERAL:
        return _real_constant(text)
    literal = cs.CSHARP_LITERAL_ARG_TYPES.get(value.type)
    return _Constant(literal) if literal is not None else None


def _signed_constant(value: Node) -> _Constant | None:
    # `-1`, `+1`, `-1.5`: a sign written on a numeric literal.
    sign = value.children[0].type if value.children else ""
    operand = value.named_children[0] if value.named_children else None
    if operand is None or operand.type not in (
        cs.TS_CSHARP_INTEGER_LITERAL,
        cs.TS_CSHARP_REAL_LITERAL,
    ):
        return None
    constant = _literal_constant(operand)
    if constant is None or sign == cs.CSHARP_UNARY_PLUS:
        return constant
    if sign != cs.CSHARP_UNARY_MINUS:
        return None
    written = (safe_decode_text(operand) or "").replace(cs.CSHARP_DIGIT_SEPARATOR, "")
    return _negated(constant, written)


class _Sentinel:
    """Distinct from every real result, including None."""

    __slots__ = ()


# A miss, as distinct from a cached None: None is a legitimate answer
# ("unresolved"), and caching it is most of the win on a chain whose links
# do not resolve.
_MISSING = _Sentinel()
# Set while a node's own resolution is on the stack; see the docstring on
# resolve_csharp_method_call.
_IN_PROGRESS = _Sentinel()
# A candidate a member call's level walk leaves out (`_stand_for`).
_PASSED_OVER = _Sentinel()

# What the memo stores: a resolved (label, qn), an unresolved None, or the
# in-progress marker.
type _MemoEntry = tuple[str, str] | None | _Sentinel


def _extension_receiver_matches(
    receiver_type_name: str,
    recv_type: str,
    ext_namespace: str,
    ambiguous_unqualified: bool,
) -> bool:
    # Namespace consistency between the call receiver and the stored
    # `this` type, by qualification:
    #  - both qualified: require the SAME fully-qualified name
    #    (`N1.Widget` binds `this N1.Widget`, never `this N2.Widget`);
    #  - recv qualified, cand not: resolve the ext's unqualified
    #    `this Widget` to `<ext-namespace>.Widget` and require equality
    #    (`N.Widget` binds a same-namespace `this Widget`);
    #  - recv unqualified, cand qualified: the receiver's namespace is
    #    unknown without a semantic model, so don't guess;
    #  - both unqualified: match by simple name unless it's ambiguous.
    recv_qualified = cs.SEPARATOR_DOT in receiver_type_name
    cand_qualified = cs.SEPARATOR_DOT in recv_type
    if recv_qualified and cand_qualified:
        return recv_type == receiver_type_name
    if recv_qualified:
        cand_qualified_name = (
            f"{ext_namespace}{cs.SEPARATOR_DOT}{recv_type}"
            if ext_namespace
            else recv_type
        )
        return cand_qualified_name == receiver_type_name
    if cand_qualified:
        return False
    return not ambiguous_unqualified


def _member_type(member_qn: str) -> str:
    # `proj.M.N.Widget.Put(List<N.Item>)` -> `proj.M.N.Widget`.
    return member_qn.split(cs.CHAR_PAREN_OPEN, 1)[0].rpartition(cs.SEPARATOR_DOT)[0]


def _canonical_argument(
    argument: ArgumentType | None, scope: Scope
) -> ArgumentType | None:
    # An argument's type, resolved once in the caller's file: its name with
    # its arity, its shape kept (`Widget[]`, a tuple's elements), and each
    # type argument the syntax spells. A value of a type parameter whose
    # `where` clauses the scope knows carries the types they name.
    if argument is None or argument.name is None:
        return argument
    arguments = (
        tuple(canonical_type(a, scope) for a in argument.arguments)
        if argument.arguments is not None
        else None
    )
    bounds = dict(scope.bounds).get(argument.name) if not arguments else None
    return ArgumentType(
        argument.literal,
        canonical_type(argument.name, scope, argument.arity),
        argument.arity,
        arguments,
        bounds=None
        if bounds is None
        else tuple(
            canonical_type(bound, scope)
            for bound in bounds
            if bound not in cs.CSHARP_KIND_CONSTRAINTS
        ),
    )


def _written_argument(raw: str) -> ArgumentType:
    # An argument typed by a type the source writes: its name, arity and
    # type arguments, canonically spelled, with the shape built on it after
    # its arguments (`Box<int>[]` is `Box[]` of int); a tuple as it is.
    written = signature_type_name(raw)
    core, suffix = type_shape(written)
    if is_tuple(core):
        return ArgumentType(None, written, 0, ())
    return ArgumentType(
        None,
        f"{strip_generic_arguments(core)}{suffix}",
        generic_arity_of_type_text(core),
        type_arguments(core),
    )


def _passed_type(argument: ArgumentType | None) -> str | None:
    # The type an argument passes, when it is known whole: a literal's, or
    # a type's with its type arguments (none known for a generic is none).
    if argument is None:
        return None
    if argument.literal is not None:
        return argument.literal
    if argument.name is None or (argument.arity and argument.arguments is None):
        return None
    return written_type(argument)


def _lower_bounds(
    parameters: Sequence[str],
    arguments: Sequence[ArgumentType | None],
    open_names: Collection[str],
) -> Inferred:
    # The type each argument passes a parameter that is a type parameter
    # alone (`M<T>(T x)`): a lower bound of that parameter.
    lower: Inferred = {}
    for parameter, argument in zip(parameters, arguments, strict=True):
        if parameter in open_names and (passed := _passed_type(argument)):
            lower.setdefault(parameter, []).append((Bound.LOWER, passed))
    return lower


class _Origin(NamedTuple):
    """A value of a predefined type: the type, its value when a literal or
    a `const` spells it, and whether it is a constant (C#'s constant
    conversions take only a constant) or may be one."""

    type: str
    value: int | None
    constant: bool


def _predefined_origin(argument: ArgumentType) -> _Origin | None:
    # The predefined type an argument's value has, when it has one: a
    # constant's (a literal, a `const`, a value that may be one), or a typed
    # variable's spelled with a predefined name.
    if argument.literal is not None:
        return _Origin(argument.literal, argument.value, True)
    if argument.name is None or argument.arity:
        return None
    core, suffix = type_shape(argument.name)
    keyword = None if suffix else predefined_keyword(core)
    return _Origin(keyword, None, False) if keyword is not None else None


def _takes(origin: _Origin, keyword: str) -> bool | None:
    # Whether a predefined value converts to the predefined type `keyword`
    # by a standard implicit conversion: a constant by C#'s constant rules
    # (a narrowing only when its value fits), a variable by its type alone.
    if origin.constant:
        return constant_converts(origin.type, origin.value, keyword)
    return predefined_converts(origin.type, keyword)


def _takes_any_value(parameter: str) -> bool:
    # Whether a parameter is `object` or `dynamic`, with no shape on it.
    core, shape = type_shape(parameter)
    return not shape and (
        predefined_keyword(core) == cs.CSHARP_OBJECT_KEYWORD
        or core == cs.CSHARP_DYNAMIC_KEYWORD
    )


def _type_parameter_bounds_at(node: Node) -> dict[str, tuple[str, ...]]:
    # The type parameters in scope where `node` is written, those of every
    # type, method and local function around it, the nearest of a name
    # first, each with the types and kinds its `where` clauses write. They
    # shadow a type of the same name in the file (Greptile review of PR
    # 2814).
    found: dict[str, tuple[str, ...]] = {}
    current = node.parent
    while current is not None:
        if current.type in cs.CSHARP_GENERIC_DECLARATIONS:
            for name, bounds in _declared_bounds(current).items():
                found.setdefault(name, bounds)
        current = current.parent
    return found


def _declared_bounds(declaration: Node) -> dict[str, tuple[str, ...]]:
    # The type parameters a declaration declares, each with the types and
    # kinds its `where` clauses write.
    names = [
        name
        for child in declaration.children
        if child.type == cs.TS_CSHARP_TYPE_PARAMETER_LIST
        for name in type_parameter_names(child)
    ]
    if not names:
        return {}
    constraints = type_parameter_constraints(declaration)
    return {
        name: tuple(bound for of, bound in constraints if of == name) for name in names
    }


def _type_parameters_at(node: Node) -> frozenset[str]:
    # The names of the type parameters in scope where `node` is written.
    return frozenset(_type_parameter_bounds_at(node))


def _argument_list(node: Node) -> Node | None:
    # The arguments a call writes, or the index an element access writes,
    # which binds an indexer as a call's arguments bind a method.
    return node.child_by_field_name(cs.FIELD_ARGUMENTS) or node.child_by_field_name(
        cs.TS_CSHARP_FIELD_SUBSCRIPT
    )


def _argument_names(call_node: Node) -> tuple[str | None, ...]:
    # Each argument's name (`M(y: 1)`), None for a positional one.
    arg_list = _argument_list(call_node)
    if arg_list is None:
        return ()
    return tuple(
        safe_decode_text(argument.child_by_field_name(cs.FIELD_NAME))
        if argument.child_by_field_name(cs.FIELD_NAME) is not None
        else None
        for argument in arg_list.children
        if argument.type == cs.TS_CSHARP_ARGUMENT
    )


# The modes an argument may be passed in besides by value.
_ARGUMENT_MODES = frozenset({cs.CSHARP_MODE_REF, cs.CSHARP_MODE_OUT, cs.CSHARP_MODE_IN})


def _argument_modes(call_node: Node) -> tuple[str, ...]:
    # How each argument is passed: the `ref`, `out` or `in` written before
    # it, "" for by value.
    arg_list = _argument_list(call_node)
    if arg_list is None:
        return ()
    return tuple(
        next(
            (c.type for c in argument.children if c.type in _ARGUMENT_MODES),
            "",
        )
        for argument in arg_list.children
        if argument.type == cs.TS_CSHARP_ARGUMENT
    )


class _CallSite(NamedTuple):
    """What a call writes of its arguments beside their types: each one's
    name (None for a positional one) and how each is passed."""

    names: tuple[str | None, ...] = ()
    modes: tuple[str, ...] = ()


class _IndexSite(NamedTuple):
    """An element access whose indexer is being chosen, and where its
    index's arguments are typed: the caller's locals, file and method."""

    access: Node
    local_var_types: dict[str, str]
    module_qn: str
    caller_qn: str | None


class _Index(NamedTuple):
    """An element access's index as indexers are scored against it: its
    arguments' types, what it writes of them beside their types, and the
    type parameters in scope where it is written."""

    arguments: list[ArgumentType | None]
    site: _CallSite
    type_parameters: frozenset[str]


class _Form(NamedTuple):
    """One way a call's arguments bind a candidate's parameters: the type
    each binds, the declared parameter each binds (None when the
    parameters are not known by name), whether a `params` tail takes them
    expanded, whether a defaulted parameter takes none, and whether the
    candidate is known to take them so."""

    parameters: list[str]
    slots: Sequence[int] | None
    expanded: bool
    omits: bool = False
    known: bool = True


def _slot(
    shape: CSharpCallShape, position: int, name: str | None, tail: int | None
) -> int:
    # The declared parameter one argument binds: a named one its name's, a
    # positional one its position's, or, past an expanded `tail`, the tail.
    if name is not None:
        return _index_of(shape.names, name)
    return position if tail is None else min(position, tail)


def _slots(
    shape: CSharpCallShape, count: int, names: Sequence[str | None], expanded: bool
) -> list[int] | None:
    # The declared parameter each argument binds in one form: a positional
    # one the parameter at its position, or, expanded, the `params` tail
    # past it; a named one the parameter of its name (Greptile review of PR
    # 2814: `M(x: 1, 2)` passes 2 to `params int[] xs` as an int). None
    # when the form takes no such call: a name no parameter has, one
    # parameter bound twice, a required one left out, or the array of a
    # `params` tail in its normal form.
    last = count - 1
    slots: list[int] = []
    named: set[int] = set()
    for position, name in enumerate(names):
        index = _slot(shape, position, name, last if expanded else None)
        if name is not None:
            named.add(index)
        # Only positional arguments share an expanded tail: a named one
        # binds its parameter alone (`M(1, r: 2)` passes `params object[]
        # r` twice).
        shared = expanded and index == last and index not in named
        if not 0 <= index < count or (index in slots and not shared):
            return None
        slots.append(index)
    needed = set(range(shape.required))
    if shape.variadic and not expanded:
        needed.add(last)
    return slots if needed.issubset(slots) else None


def _form_of(
    types: Sequence[str], slots: list[int], expanded: bool, variadic: bool
) -> _Form:
    # The form binding each argument to its slot: an expanded tail's
    # element type for one past it, and whether a defaulted parameter
    # before the tail takes no argument.
    last = len(types) - 1
    return _Form(
        [
            types[i].removesuffix(cs.CSHARP_ARRAY_SUFFIX)
            if expanded and i == last
            else types[i]
            for i in slots
        ],
        slots,
        expanded,
        any(i not in slots for i in range(len(types) - int(variadic))),
    )


# A stored indexer parameter's name, marked as `indexer_entry` marks it.
_STORED_PARAMETER_NAME = re.compile(cs.CSHARP_STORED_PARAMETER_NAME_PATTERN)
_ANNOTATED_NAME = re.compile(cs.CSHARP_ANNOTATED_NAME_PATTERN)
# A parameter only an argument passed in its mode binds.
_BY_REFERENCE = frozenset({cs.CSHARP_MODE_REF, cs.CSHARP_MODE_OUT})
# The read-only parameter modes an argument passed by reference binds too.
_READ_ONLY_TAKES: dict[str, tuple[str, ...]] = {
    cs.CSHARP_MODE_REF: (cs.CSHARP_MODE_IN, cs.CSHARP_MODE_REF_READONLY),
    cs.CSHARP_MODE_IN: (cs.CSHARP_MODE_REF_READONLY,),
}


# How an extension method's `this` parameter receives the call's receiver,
# which the call writes no mode for: a variable (a local, a parameter, a
# field, an element), which a `this ref` parameter takes by reference; a
# value (a literal, a computed or constructed one), which it cannot; or
# not known (a call's result, which may be a reference). Any of them binds a
# by-value, `this in` or `this ref readonly` parameter (PR 2814 review).
_RECEIVER_VARIABLE = "receiver variable"
_RECEIVER_VALUE = "receiver value"
_RECEIVER_UNSURE = "receiver"
_RECEIVER_PASSING = frozenset({_RECEIVER_VARIABLE, _RECEIVER_VALUE, _RECEIVER_UNSURE})
# What a receiver is, by its expression's syntax.
_VARIABLE_RECEIVERS = frozenset(
    {
        cs.TS_CSHARP_IDENTIFIER,
        cs.TS_CSHARP_MEMBER_ACCESS_EXPRESSION,
        cs.TS_CSHARP_ELEMENT_ACCESS_EXPRESSION,
        cs.TS_CSHARP_THIS,
    }
)


def _receiver_passing(receiver: Node) -> str:
    # How a member call passes its receiver to an extension's `this`.
    while receiver.type == cs.TS_PARENTHESIZED_EXPRESSION and receiver.named_children:
        receiver = receiver.named_children[0]
    if receiver.type in _VARIABLE_RECEIVERS:
        return _RECEIVER_VARIABLE
    if receiver.type == cs.TS_CSHARP_INVOCATION_EXPRESSION:
        return _RECEIVER_UNSURE
    return _RECEIVER_VALUE


# Where C# finds a type a file names by its simple name, nearest first
# (`_visibility`).
_ENCLOSING = 0
_GLOBAL_NAMESPACE = 1
_IMPORTED = 2
_NOT_VISIBLE = 3

# How deep an operand chain is typed (`(a + b) * c`).
_MAX_VALUE_DEPTH = 8


def _promoted(left: str | None, right: str | None) -> str | None:
    # C#'s binary numeric promotion of two predefined operand types; None
    # when either is not numeric or the pair has no common type.
    if left is None or right is None:
        return None
    pair = {left, right}
    if not pair <= cs.CSHARP_NUMERIC_KEYWORDS:
        return None
    wide = next((w for w in cs.CSHARP_PROMOTION_ORDER if w in pair), None)
    if wide is None:
        return cs.CSHARP_INT_KEYWORD
    other = next(iter(pair - {wide}), wide)
    if other in cs.CSHARP_PROMOTION_CLASHES.get(wide, frozenset()):
        return None
    return cs.CSHARP_PROMOTION_WIDENS.get(wide, {}).get(other, wide)


def _shifted(left: str | None, right: str | None) -> str | None:
    # A shift is of its left operand's type after unary promotion (`u << 1`
    # is a uint, `b << 1` an int), its count of a type converting to int.
    if right not in cs.CSHARP_SHIFT_COUNTS:
        return None
    return cs.CSHARP_SHIFTED.get(left or "")


def _operated(operator: str, left: str | None, right: str | None) -> str | None:
    # The type an arithmetic or bitwise operator gives two predefined
    # operands: their promotion (`bool & bool` is a bool).
    if (
        operator in cs.CSHARP_BITWISE_OPERATORS
        and left == right == cs.CSHARP_BOOL_KEYWORD
    ):
        return cs.CSHARP_BOOL_KEYWORD
    return _promoted(left, right)


def _passed(fit: Fit | None, given: str, wanted: str) -> Fit | None:
    # How an argument passed `given` binds a parameter passed `wanted`. By
    # value it binds a by-value, `in` or `ref readonly` parameter as its
    # type does and never a `ref` or `out` one; passed `ref`, `out` or `in`
    # it binds only a parameter of its mode (a `ref` one an `in` or `ref
    # readonly` one too, an `in` one a `ref readonly` one), and only by
    # identity (PR 2814's Roslyn oracle).
    if given in _RECEIVER_PASSING:
        return None if given == _RECEIVER_VALUE and wanted in _BY_REFERENCE else fit
    if not given:
        return None if wanted in _BY_REFERENCE else fit
    if wanted != given and wanted not in _READ_ONLY_TAKES.get(given, ()):
        return None
    return None if fit in (Fit.CONVERTIBLE, Fit.UNDECIDED) else fit


def _passed_in_modes(
    fits: list[Fit | None],
    form: _Form,
    modes: tuple[Sequence[str], Sequence[str]],
    last: int,
) -> list[Fit | None]:
    # Each fit as the way its argument is passed lets it stand (`_passed`)
    # against the mode its parameter declares (`modes`: the declared ones,
    # then the given ones); an expanded `params` element, past the `last`
    # parameter, is passed by value.
    declared, given = modes
    if form.slots is None or not (declared or any(given)):
        return fits
    return [
        _passed(
            fit,
            mode,
            declared[slot]
            if slot < len(declared) and not (form.expanded and slot == last)
            else "",
        )
        for fit, mode, slot in zip(
            fits, given or ("",) * len(fits), form.slots, strict=True
        )
    ]


def _to_type_parameter(
    fitted: Fit | None,
    parameter: str,
    argument: ArgumentType | None,
    in_scope: frozenset[str],
) -> Fit | None:
    # A parameter of a type parameter in scope where the index is written
    # (`Box<T>`'s `this[U u]` read as `this[T u]`) takes a value of that
    # type parameter, or a literal, as judged; a value of any other type
    # known does not convert to it.
    if (
        fitted is None
        or parameter not in in_scope
        or argument is None
        or argument.name is None
        or argument.literal is not None
        or argument.bounds is not None
    ):
        return fitted
    return None


def _index_of(names: Sequence[str], name: str) -> int:
    return names.index(name) if name in names else -1


def _initializer(declarator: Node) -> Node | None:
    # The expression a variable declarator initializes its variable with:
    # its last part, bare or in an `equals_value_clause` by grammar version.
    parts = declarator.named_children
    if len(parts) < 2:
        return None
    value = parts[-1]
    if value.type == cs.TS_CSHARP_EQUALS_VALUE_CLAUSE:
        return value.named_children[0] if value.named_children else None
    return value


def _stored_parameter(text: str) -> tuple[str, str, str]:
    # (marked name, passing mode, type) of one stored parameter, `in int a`
    # -> ("a", "in", "int"); a bare type (`Dictionary<int, string>`) is
    # one of no name known.
    head, _, name = text.rpartition(" ")
    if not head or not _STORED_PARAMETER_NAME.fullmatch(name):
        return "", "", text
    words = head.split()
    passing = 0
    while passing < len(words) and words[passing] in cs.CSHARP_PASSING_MODIFIERS:
        passing += 1
    return name, " ".join(words[:passing]), " ".join(words[passing:])


def _indexer_shapes(indexers: Sequence[str]) -> list[tuple[str, CSharpCallShape]]:
    # Each indexer `indexer_entry` stored: its written return type, and how
    # it is called, as a method's call shape records it.
    return [_indexer_shape(entry) for entry in indexers]


def _indexer_shape(entry: str) -> tuple[str, CSharpCallShape]:
    # `Left=>in int a, string b?` -> ("Left", a shape taking `in int a` and an
    # optional `string b`). An entry naming no parameters (an older graph's
    # `Left=>int`) gives their types alone, every one required.
    returned, _, written = entry.partition(cs.CSHARP_STORED_PAIR_SEPARATOR)
    names: list[str] = []
    types: list[str] = []
    modes: list[str] = []
    for parameter in parameter_types(f"{cs.CSHARP_INDEXER_NAME}({written})"):
        name, mode, written_type = _stored_parameter(parameter)
        names.append(name)
        modes.append(mode)
        types.append(written_type)
    named = all(names)
    optional = [n.endswith(cs.POSITIONAL_OPTIONAL_SUFFIX) for n in names]
    variadic = named and bool(names) and names[-1].startswith(cs.POSITIONAL_REST_PREFIX)
    return returned, CSharpCallShape(
        required=sum(not o for o in optional) - int(variadic),
        variadic=variadic,
        is_static=False,
        names=tuple(
            n.removeprefix(cs.POSITIONAL_REST_PREFIX).removesuffix(
                cs.POSITIONAL_OPTIONAL_SUFFIX
            )
            for n in names
        )
        if named
        else (),
        modes=tuple(modes) if any(modes) else (),
        types=tuple(types),
    )


def _reads_as_sum(cast: Node) -> bool:
    # `(x) + 1` and `(x) - y` parse as a cast of a signed operand to a type
    # `x`, but C# reads a parenthesized name before `+` or `-` as a sum
    # (`(E)-1` does not compile), of whatever type its operator gives.
    cast_to = cast.child_by_field_name(cs.FIELD_TYPE)
    operand = cast.child_by_field_name(cs.FIELD_VALUE)
    return (
        cast_to is not None
        and cast_to.type == cs.TS_CSHARP_IDENTIFIER
        and operand is not None
        and operand.type == cs.TS_CSHARP_PREFIX_UNARY_EXPRESSION
        and bool(operand.children)
        and bool(operand.named_children)
        and safe_decode_text(operand.children[0])
        in (cs.CSHARP_UNARY_PLUS, cs.CSHARP_UNARY_MINUS)
    )


def _declared_constness(
    statement: Node, declarator: Node
) -> tuple[bool | None, int | None]:
    # A local is a variable unless declared `const`, whose value its literal
    # initializer writes.
    if not any(
        child.type == cs.TS_CSHARP_MODIFIER
        and safe_decode_text(child) == cs.CSHARP_CONST_MODIFIER
        for child in statement.children
    ):
        return False, None
    constant = _literal_constant(declarator.named_children[-1])
    value = constant.value if constant is not None else None
    return True, value if isinstance(value, int) else None


def _spelled_type(value: Node) -> str | None:
    # The type an expression spells as its own: a construction's, a cast's
    # or an `as`'s. Any other expression's type is its result's, which only
    # resolving it tells, so a construction among a call's arguments is never
    # the type of the call (`var x = Factory.Make(new Input())` is no
    # Input; Greptile review of PR 2814).
    while value.type == cs.TS_PARENTHESIZED_EXPRESSION and value.named_children:
        value = value.named_children[0]
    if value.type == cs.TS_CSHARP_OBJECT_CREATION_EXPRESSION or (
        value.type == cs.TS_CSHARP_CAST_EXPRESSION and not _reads_as_sum(value)
    ):
        return safe_decode_text(value.child_by_field_name(cs.FIELD_TYPE))
    if value.type == cs.TS_CSHARP_AS_EXPRESSION:
        return safe_decode_text(value.child_by_field_name(cs.FIELD_RIGHT))
    return None


def _const_constant(statement: Node, decl: Node, declarator: Node) -> _Constant | None:
    # The constant a `const` local of a predefined type is: `const int k =
    # 5` is an int of 5, one with a value not read an int of value unknown.
    # None for a local that is not `const`.
    if not any(
        child.type == cs.TS_CSHARP_MODIFIER
        and safe_decode_text(child) == cs.CSHARP_CONST_MODIFIER
        for child in statement.children
    ):
        return None
    written = safe_decode_text(decl.child_by_field_name(cs.FIELD_TYPE)) or ""
    keyword = predefined_keyword(signature_type_name(written))
    if keyword is None:
        return None
    parts = declarator.named_children
    spelled = _literal_constant(parts[-1]) if len(parts) > 1 else None
    return _Constant(keyword, spelled.value if spelled is not None else None)


def _bound_keyword(bound: str) -> str | None:
    # The predefined type a bound is, `dynamic` among them, when it is one.
    core, suffix = type_shape(bound)
    if suffix:
        return None
    return (
        cs.CSHARP_DYNAMIC_KEYWORD
        if core == cs.CSHARP_DYNAMIC_KEYWORD
        else (predefined_keyword(core))
    )


def _meets_predefined(keyword: str, given: str) -> bool | None:
    # A bound a type parameter was fixed to a predefined type: every type
    # converts to `object` (and `dynamic`) by a reference conversion or
    # boxing; any other is a value type or sealed, met by itself alone,
    # which `given` is not, when its type is known.
    if keyword in (cs.CSHARP_OBJECT_KEYWORD, cs.CSHARP_DYNAMIC_KEYWORD):
        return True
    return False if _known_type(given) else None


def _known_type(given: str) -> bool:
    # Whether a resolved type is one this resolver tells apart from any
    # other: predefined, first-party, a tuple, or a shape built on a type.
    core, suffix = type_shape(given)
    return (
        predefined_keyword(core) is not None
        or core.startswith(cs.CSHARP_RESOLVED_PREFIX)
        or is_tuple(core)
        or bool(suffix)
    )


def _framework_name(bound: str) -> str:
    # A framework bound's simple name: `Enum` of `System.Enum`.
    return strip_generic_arguments(type_shape(bound)[0]).rpartition(cs.SEPARATOR_DOT)[2]


def _external_spelling(written: str) -> str:
    # An external type as spelled, its `global::` and `System.` aside:
    # `System.IDisposable` and `IDisposable` under `using System;` are one.
    path = written.strip().rpartition(cs.SEPARATOR_DOUBLE_COLON)[2]
    return path.removeprefix(cs.CSHARP_SYSTEM_PREFIX)


def _built_in_meets(given: str, bound: str, first_party_bound: bool) -> bool | None:
    # Whether a type no file declares meets a bound, None when that is not
    # known: no predefined type, array, nullable or tuple derives from or
    # implements a first-party type or is an enum, and a predefined type
    # implements the framework interfaces `predefined_implements` lists.
    core, suffix = type_shape(given)
    keyword = None if suffix else predefined_keyword(core)
    if first_party_bound or _framework_name(bound) == cs.CSHARP_ENUM_BASE_TYPE:
        # Nor is any of them an enum.
        built_in = predefined_keyword(core) is not None or is_tuple(core)
        return False if built_in or suffix else None
    return True if keyword and predefined_implements(keyword, bound) else None


def _property_argument(declaration: Node, name: str) -> ArgumentType | None:
    # What a property declaration passes for `name`, when it declares it: a
    # variable of its written type.
    if safe_decode_text(declaration.child_by_field_name(cs.FIELD_NAME)) != name:
        return None
    written = safe_decode_text(declaration.child_by_field_name(cs.FIELD_TYPE))
    return _written_argument(written) if written else None


def _enclosing_body(node: Node) -> Node | None:
    # The body of the type a node is written in.
    current = node.parent
    while current is not None and current.type != cs.TS_CSHARP_DECLARATION_LIST:
        current = current.parent
    return current


class _Unread:
    """A value whose member C#'s lookup would read is not known here."""


_UNREAD = _Unread()


def _named_member(value: Node) -> tuple[Node, str] | None:
    # The field or property a bare name reads, as C#'s lookup finds it
    # among what the file declares, when no local or parameter has it.
    name = safe_decode_text(value) or ""
    found = None if binds_local(value, name) else simple_name(value, name)
    if found is None or found.kind is not LookupKind.MEMBER or found.node is None:
        return None
    return found.node, name


def _owner_body(owner: Node, local_type: str | None) -> Node | _Unread | None:
    # The body of the type a member access's owner names: `this`'s; for a
    # local or parameter, the type it is declared with (`local_type`); else
    # the type `_owner_type` finds. `_UNREAD` when a base the file does not
    # declare may hold a name on the way; None for a name or type the file
    # does not declare.
    if owner.type == cs.TS_CSHARP_THIS:
        return _enclosing_body(owner)
    name = safe_decode_text(owner) or ""
    if owner.type == cs.TS_CSHARP_MEMBER_ACCESS_EXPRESSION:
        found = _this_member_type(owner)
    elif owner.type != cs.TS_CSHARP_IDENTIFIER:
        return None
    elif binds_local(owner, name):
        type_name = simple_type_name(local_type) if local_type else None
        found = simple_name(owner, type_name, types_only=True) if type_name else None
    else:
        found = _owner_type(owner, simple_name(owner, name), name)
    if found is None or isinstance(found, _Unread):
        return found
    return _UNREAD if found.kind is LookupKind.UNKNOWN else type_body(found)


def _this_member_type(owner: Node) -> Lookup | _Unread | None:
    # The type `this.K` is, `K` a member of the enclosing type or one it
    # inherits; None for any other member access.
    target = owner.child_by_field_name(cs.TS_CSHARP_FIELD_EXPRESSION)
    name = safe_decode_text(owner.child_by_field_name(cs.FIELD_NAME)) or ""
    body = _enclosing_body(owner)
    if target is None or target.type != cs.TS_CSHARP_THIS or body is None or not name:
        return None
    holder = body.parent
    return _owner_type(owner, member_of(holder, name), name) if holder else None


def _binder_type(binder: Node) -> str | None:
    # The type a local's or parameter's binder is declared with, None for
    # one written `var` or with none.
    holder = binder.parent
    if holder is not None and holder.type == cs.TS_CSHARP_VARIABLE_DECLARATOR:
        holder = holder.parent
    type_node = holder.child_by_field_name(cs.FIELD_TYPE) if holder else None
    if type_node is None or type_node.type == cs.TS_CSHARP_IMPLICIT_TYPE:
        return None
    return safe_decode_text(type_node)


def _owner_type(
    owner: Node, found: Lookup | None, name: str
) -> Lookup | _Unread | None:
    # The type a name `found` as a member access's owner is: the type it
    # found, or the type of a field or property it found, even one sharing
    # the member's name ("Color Color", whose static and instance readings
    # both read that type's members), looked up where that member is
    # declared: an inherited `Holder K` is its base's `Holder` (Greptile
    # review of PR 2814). `_UNREAD` for a method or event of the name; None
    # for a member whose type this file's lookup cannot name.
    if found is None or found.kind is not LookupKind.MEMBER or found.node is None:
        return found
    written = member_type(found.node, name)
    if not written:
        return _UNREAD
    type_name = simple_type_name(written)
    return simple_name(found.node, type_name, types_only=True) if type_name else None


def _reads_alike(member: Node, site: Node, name: str) -> bool:
    # Whether every name the member `name`'s declared type spells finds the
    # same declaration where the member is declared as at `site`, where its
    # type is read (Greptile review of PR 2814).
    # A predefined type's keyword names no declaration anywhere.
    written = member_type(member, name)
    return not written or all(
        simple_name(member, spelled, types_only=True)
        == simple_name(site, spelled, types_only=True)
        for spelled in mentioned_names(written)
        if spelled not in cs.CSHARP_PREDEFINED_TYPE_NAMES
    )


def _declares_value(member: Node, name: str) -> bool:
    # Whether a type member is the field or property `name`.
    if member.type == cs.TS_CSHARP_PROPERTY_DECLARATION:
        return safe_decode_text(member.child_by_field_name(cs.FIELD_NAME)) == name
    return (
        member.type == cs.TS_CSHARP_FIELD_DECLARATION
        and member_type(member, name) is not None
    )


def _exact(bound: Inferred) -> dict[str, str]:
    # The type each parameter was inferred exactly to.
    return {
        name: type_text
        for name, bounds in bound.items()
        for kind, type_text in bounds
        if kind is Bound.EXACT
    }


def _all_hold(verdicts: Collection[bool | None]) -> bool | None:
    # False when one fails, else None when one is not known, else True.
    if False in verdicts:
        return False
    return None if None in verdicts else True


def _distinct(types: Sequence[str]) -> list[str]:
    # One of each type, however many ways it is spelled.
    out: list[str] = []
    for type_text in types:
        if not any(same_type(type_text, seen) for seen in out):
            out.append(type_text)
    return out


class _Judged(NamedTuple):
    """What inference made of one candidate: each argument's fit, the type
    fixed for each type parameter, and whether every type parameter of its
    own that the arguments bound was fixed."""

    fits: list[Fit | None]
    fixed: dict[str, str]
    settled: bool = True


class _Reading(NamedTuple):
    """How one candidate's declaration reads at a call: the file its types
    resolve in, the bindings it sees (the receiver's and the call's), the
    type parameters it declares itself, every type parameter still open,
    and the type arguments the call spells for its own."""

    scope: Scope | None
    visible: Mapping[str, str]
    own: frozenset[str]
    unbound: frozenset[str]
    spelled: Mapping[str, str]


class _ScopedJudge:
    """The `TypeJudge` ranking one candidate: its arguments resolve in the
    caller's file and its parameters in the file declaring the candidate."""

    __slots__ = ("_converts", "_identical", "_is_enum")

    def __init__(
        self,
        identical: Callable[[ArgumentType, str, Collection[str]], bool | None],
        converts: Callable[[ArgumentType, str, Collection[str]], bool | None],
        is_enum: Callable[[str], bool],
    ) -> None:
        self._identical = identical
        self._converts = converts
        self._is_enum = is_enum

    def identical(
        self, argument: ArgumentType, parameter: str, open_names: Collection[str]
    ) -> bool | None:
        return self._identical(argument, parameter, open_names)

    def converts(
        self, argument: ArgumentType, parameter: str, open_names: Collection[str]
    ) -> bool | None:
        return self._converts(argument, parameter, open_names)

    def is_enum(self, parameter: str) -> bool:
        return self._is_enum(parameter)


class _MemberCall(NamedTuple):
    """A member call being bound, as overload ranking reads it."""

    node: Node
    receiver: Node
    method_name: str
    arg_count: int
    local_var_types: dict[str, str]
    module_qn: str
    caller_qn: str | None
    # The type arguments the call spells (`c.M<long>(1)`), as written.
    type_arguments: tuple[str, ...] = ()


class _ExtendedReceiver(NamedTuple):
    """A member call's receiver as extension methods are matched to it: its
    type's name as written, the written arity (None when not known), and
    the first-party class it is, when known."""

    name: str
    arity: int | None
    qn: str | None
    # Where the receiver's type name resolves: the call's file, with the
    # type parameters around it; None when not known.
    scope: Scope | None = None


# How a use reads a declaring type's members: the type arguments its
# receiver binds, its type parameters left open, and the call's own type
# arguments.
type _ReceiverView = tuple[Mapping[str, str], frozenset[str], tuple[str, ...]]


class _Standing(NamedTuple):
    """An override waiting for the virtual method it stands for: its
    parameter types as the receiver reads them, its qn, that reading and
    the type declaring it."""

    signature: Sequence[str]
    qn: str
    receiver: _ReceiverView
    declaring: str


class _Hierarchy(NamedTuple):
    """The types a receiver reaches, each before every type it derives
    from, the reached types deriving from each one directly, and each
    one's ancestors among them. C# drops a base's methods once a type
    deriving from it holds one that applies, and sibling bases (two
    interfaces) drop none of each other's, so every type deriving from a
    base is weighed before it (PR 2814 review)."""

    order: list[str]
    derived: Mapping[str, list[str]]
    ancestors: Mapping[str, frozenset[str]]


class _Gathering:
    """What a member call's walk of the receiver's types gathered: every
    candidate scored, the parameter types each type declares (which hide a
    base's method of those types), the overrides waiting for their virtual
    methods, the types holding a candidate known to apply, and their
    ancestors, whose methods C# drops."""

    __slots__ = ("blocked", "declared", "picks", "placed", "settled", "standing")

    def __init__(self) -> None:
        self.picks: list[Scored] = []
        self.declared: list[tuple[str, Sequence[str]]] = []
        self.standing: list[_Standing] = []
        self.placed: set[str] = set()
        self.settled: set[str] = set()
        self.blocked: set[str] = set()


# An extension candidate's owner, by the scope it is found in.
_EXTENSION_SCOPE = "<extension scope>"


def _outer_scope(owner: str, other: str) -> bool:
    # Whether `owner` is an extension scope C# searches after `other`'s,
    # whose methods it never beats: they apply or it is not searched.
    return int(owner[len(_EXTENSION_SCOPE) :]) > int(other[len(_EXTENSION_SCOPE) :])


def _received(scored: Scored, receiving: Fit) -> Scored:
    # An extension candidate with its receiver's fit as `_receiving` judged
    # it, unless its passing mode already ruled the receiver out.
    if not scored.fits or scored.fits[0] is None:
        return scored
    return scored._replace(fits=[receiving, *scored.fits[1:]])


def _enclosing_namespaces(node: Node) -> list[str]:
    # The namespaces a node is declared in, innermost first: `A.B` is in
    # `A.B` and `A`.
    declared = declared_namespace(node)
    if not declared:
        return []
    parts = declared.split(cs.SEPARATOR_DOT)
    return [cs.SEPARATOR_DOT.join(parts[:i]) for i in range(len(parts), 0, -1)]


# The owner of an override whose virtual method is in a base no walk
# reached: a base of every type reached.
_UNPLACED = "<base>"


def _outranked(ancestors: Mapping[str, frozenset[str]], owner: str, other: str) -> bool:
    # Whether `owner`'s methods are dropped once `other`'s apply: it is one
    # of `other`'s bases.
    if not owner or not other or owner == other:
        return False
    return owner == _UNPLACED or owner in ancestors.get(other, frozenset())


def _derived_first(
    reached: Sequence[str], derived: Mapping[str, list[str]]
) -> list[str]:
    # Each type once every type deriving from it is placed, ties in walk
    # order; a cycle (a malformed base list) leaves the rest in walk order.
    placed: list[str] = []
    done: set[str] = set()
    pending = list(reached)
    while pending:
        ready = next((t for t in pending if done.issuperset(derived[t])), None)
        if ready is None:
            return placed + pending
        placed.append(ready)
        done.add(ready)
        pending.remove(ready)
    return placed


def _ancestors_within(
    reached: Sequence[str], inheritance: Mapping[str, list[str]]
) -> dict[str, frozenset[str]]:
    # Each reached type's ancestors among the reached types.
    inside = set(reached)
    found: dict[str, frozenset[str]] = {}
    for start in reached:
        seen: set[str] = set()
        stack = [b for b in inheritance.get(start, []) if b in inside]
        while stack:
            base = stack.pop()
            if base in seen or base == start:
                continue
            seen.add(base)
            stack.extend(b for b in inheritance.get(base, []) if b in inside)
        found[start] = frozenset(seen)
    return found


def _same_signature(first: Sequence[str], second: Sequence[str]) -> bool:
    # Whether two methods take one parameter type list.
    return len(first) == len(second) and all(
        a == b or same_type(a, b) for a, b in zip(first, second, strict=True)
    )


class _Ranking(NamedTuple):
    """The overloads a member call may bind, best first; whether one is
    known to apply, which leaves extension methods out; and whether any
    overload was scored at all."""

    picks: list[Scored]
    certain: bool
    scored: bool


class CSharpTypeInferenceEngine:
    __slots__ = (
        "import_processor",
        "function_registry",
        "repo_path",
        "project_name",
        "ast_cache",
        "queries",
        "module_qn_to_file_path",
        "class_inheritance",
        "simple_name_lookup",
        "class_field_types",
        "csharp_partial_groups",
        "csharp_extension_methods",
        "csharp_call_sites",
        "csharp_external_sites",
        "csharp_local_functions",
        "csharp_generic_methods",
        "csharp_call_shapes",
        "csharp_class_generic_arity",
        "csharp_class_namespaced",
        "csharp_namespaced_qns",
        "csharp_method_return_types",
        "csharp_generic_shapes",
        "csharp_implicit_conversions",
        "csharp_class_owner_module",
        "method_return_types",
        "function_locations",
        "_rel_to_module",
        "_call_memo",
        "_overload_families",
        "_call_picks",
        "_guessed_calls",
        "_typing_initializers",
        "_methods_by_name",
        "_explicit_by_member",
    )

    def __init__(
        self,
        import_processor: ImportProcessor,
        function_registry: FunctionRegistryTrieProtocol,
        repo_path: Path,
        project_name: str,
        ast_cache: ASTCacheProtocol,
        queries: Mapping[cs.SupportedLanguage, LanguageQueries],
        module_qn_to_file_path: dict[str, Path],
        class_inheritance: dict[str, list[str]],
        simple_name_lookup: SimpleNameLookup,
        class_field_types: dict[str, dict[str, str]],
        csharp_partial_groups: dict[str, list[str]] | None = None,
        csharp_extension_methods: dict[str, list[tuple[str, str, str, int]]]
        | None = None,
        csharp_call_sites: dict[CallSiteKey, ResolvedCallSite] | None = None,
        csharp_external_sites: set[CallSiteKey] | None = None,
        csharp_local_functions: dict[str, tuple[FunctionSpanKey, int]] | None = None,
        csharp_generic_methods: set[str] | None = None,
        csharp_call_shapes: dict[str, CSharpCallShape] | None = None,
        csharp_class_generic_arity: dict[str, int] | None = None,
        csharp_class_namespaced: dict[str, str] | None = None,
        csharp_namespaced_qns: dict[str, set[str]] | None = None,
        csharp_method_return_types: dict[str, tuple[str, int]] | None = None,
        method_return_types: dict[str, str] | None = None,
        function_locations: dict[FunctionSpanKey, FunctionLocation] | None = None,
        csharp_generic_shapes: dict[str, CSharpGenericShape] | None = None,
        csharp_implicit_conversions: dict[str, list[tuple[str, str]]] | None = None,
        csharp_class_owner_module: dict[str, str] | None = None,
    ):
        # Memo for `resolve_csharp_method_call` (issue #1800). A chained
        # invocation types its receiver by resolving it, and THREE branches do
        # that independently for the same receiver node (the class-qn path,
        # the type-name path and the arity path), so an n-link fluent chain
        # re-resolved its whole prefix 3^(n/2)-ish times: measured 58 calls at
        # 4 links rising to 3,587,219 at 14, and a real Aspire file wedged
        # Pass 3 at 100% CPU for over half an hour. Memoising the entry point
        # collapses all three branches at once and makes the walk linear.
        #
        # Keyed by byte span rather than `id(node)`: CPython recycles ids of
        # freed objects, and this repo has already been bitten by that (see
        # `reset_resolution_caches`, "wildcard entries keyed by dict id, which
        # recycles"). A span is unique within a file, and `module_qn` scopes it
        # to one; `caller_qn` is included because the same node resolves
        # differently per enclosing method (`this`, locals, imports).
        self._call_memo: dict[tuple[str, str | None, int, int], _MemoEntry] = {}
        # The spans of the `var` initializers being typed by their call's
        # result right now: a local named in its own initializer (invalid
        # C#, but parsed) must not resolve that call again.
        self._typing_initializers: set[tuple[int, int]] = set()
        # The overloads a member call could not choose between beyond the
        # one it returned, under the memo's key, so the call pass
        # can fan the call out to them (issue #2619). Kept beside the memo
        # rather than recomputed: the memo may answer the call node long
        # after the ranking ran for it as a chained receiver.
        self._overload_families: dict[tuple[str, str | None, int, int], list[str]] = {}
        # Under the same key, how the ranking bound each overload a member
        # call may take (the type arguments it fixed), which types what the
        # call returns; and the calls bound by name alone, which no ranking
        # chose (PR 2814 review).
        self._call_picks: dict[tuple[str, str | None, int, int], dict[str, Scored]] = {}
        self._guessed_calls: set[tuple[str, str | None, int, int]] = set()
        # class qn -> method name -> the methods the class itself declares
        # under that name, in registry order; see `_direct_same_name_methods`.
        self._methods_by_name: dict[str, dict[str, tuple[str, ...]]] = {}
        # class qn -> member name -> the explicit interface implementations
        # the class declares of it (`IRun#Go(int)` under `Go`), off the same
        # index; see `explicit_methods`.
        self._explicit_by_member: dict[str, dict[str, tuple[str, ...]]] = {}
        self.import_processor = import_processor
        self.function_registry = function_registry
        self.repo_path = repo_path
        self.project_name = project_name
        self.ast_cache = ast_cache
        self.queries = queries
        self.module_qn_to_file_path = module_qn_to_file_path
        self.class_inheritance = class_inheritance
        self.simple_name_lookup = simple_name_lookup
        self.class_field_types = class_field_types
        self.csharp_partial_groups = (
            csharp_partial_groups if csharp_partial_groups is not None else {}
        )
        self.csharp_extension_methods = (
            csharp_extension_methods if csharp_extension_methods is not None else {}
        )
        # Shared references (populated by the Roslyn frontend / Pass 2 after
        # this engine is constructed), so `or {}` would lose them.
        self.csharp_call_sites = (
            csharp_call_sites if csharp_call_sites is not None else {}
        )
        self.csharp_external_sites = (
            csharp_external_sites if csharp_external_sites is not None else set()
        )
        self.csharp_local_functions = (
            csharp_local_functions if csharp_local_functions is not None else {}
        )
        self.csharp_generic_methods = (
            csharp_generic_methods if csharp_generic_methods is not None else set()
        )
        self.csharp_call_shapes = (
            csharp_call_shapes if csharp_call_shapes is not None else {}
        )
        self.csharp_class_generic_arity = (
            csharp_class_generic_arity if csharp_class_generic_arity is not None else {}
        )
        self.csharp_class_namespaced = (
            csharp_class_namespaced if csharp_class_namespaced is not None else {}
        )
        self.csharp_namespaced_qns = (
            csharp_namespaced_qns if csharp_namespaced_qns is not None else {}
        )
        self.csharp_method_return_types = (
            csharp_method_return_types if csharp_method_return_types is not None else {}
        )
        # Shared reference (as above): {method qn: normalized return type},
        # populated during ingestion, read by chained-receiver typing.
        self.method_return_types = (
            method_return_types if method_return_types is not None else {}
        )
        self.function_locations = (
            function_locations if function_locations is not None else {}
        )
        # Shared reference (as above): {class qn: its type parameters and the
        # arguments it passes its generic bases}, populated during ingestion.
        self.csharp_generic_shapes = _given(csharp_generic_shapes, dict)
        # Shared reference (as above): {class qn: (source, target) of each
        # `implicit operator` it declares, as written}.
        self.csharp_implicit_conversions = _given(csharp_implicit_conversions, dict)
        # Shared reference (as above): {class qn: its declaring module qn},
        # the scope a conversion operator's written types resolve in.
        self.csharp_class_owner_module = _given(csharp_class_owner_module, dict)
        self._rel_to_module: dict[str, str] = {}

    # --- variable/field/parameter type map -------------------------------

    def build_variable_type_map(self, scope_node: Node) -> dict[str, str]:
        # Parameters and locals only. Field types are looked up at resolve
        # time against class_field_types (keyed by class qn), which also
        # reaches fields inherited from a base class in another file; the
        # enclosing class qn is not known here, only at the call site.
        types: dict[str, str] = {}
        self._collect_parameters(scope_node, types)
        self._collect_locals(scope_node, types)
        return types

    def _collect_parameters(self, scope_node: Node, types: dict[str, str]) -> None:
        param_list = scope_node.child_by_field_name(cs.FIELD_PARAMETERS)
        if param_list is None:
            return
        prev_loose_type: str | None = None
        for child in param_list.children:
            # A `params string[] xs` tail is not wrapped in a `parameter`
            # node (grammar quirk, same as extract_parameter_type_names):
            # its array_type and identifier sit loose under the list.
            if child.type == cs.TS_CSHARP_ARRAY_TYPE:
                prev_loose_type = safe_decode_text(child)
                continue
            if child.type == cs.TS_CSHARP_IDENTIFIER and prev_loose_type:
                if name := safe_decode_text(child):
                    types[name] = annotate_type_ref(prev_loose_type)
                prev_loose_type = None
                continue
            if child.type != cs.TS_CSHARP_PARAMETER:
                continue
            name = safe_decode_text(child.child_by_field_name(cs.FIELD_NAME))
            type_text = safe_decode_text(child.child_by_field_name(cs.FIELD_TYPE))
            if name and type_text:
                types[name] = annotate_type_ref(type_text)

    def _collect_locals(self, scope_node: Node, types: dict[str, str]) -> None:
        # One type map per method (as every language engine here builds), so
        # sibling blocks are not distinguished: two `{ var x = ... }` blocks
        # declaring `x` as DIFFERENT types cannot both be modelled. Rather than
        # let the last declaration win and misbind the other block's calls, a
        # name seen with conflicting types is dropped so it falls back to
        # bare-name resolution. Full block-scoped precision needs the Roslyn
        # semantic model (follow-up).
        conflicted: set[str] = set()
        for decl in self._local_variable_declarations(scope_node):
            declared = self._declared_type_name(decl)
            for declarator in decl.children:
                if declarator.type == cs.TS_CSHARP_VARIABLE_DECLARATOR:
                    self._record_local(declarator, declared, types, conflicted)

    def _binds_local(self, node: Node, name: str) -> bool:
        """Whether a local in scope at `node` binds `name`.

        Two binders, both of which can leave the name out of
        `local_var_types` and so read as a TYPE: a `foreach (var name in
        ...)` loop, whose element type is not inferred, and a local declared
        in an enclosing block whose initializer is not inferred
        (`var Config = Ext.Make();`, Copilot, #2011).

        The foreach binding is in scope in the loop BODY only; in the
        collection expression `foreach (var Config in Config.All())` the
        name is still the class (CodeRabbit, #2011). Only its IMPLICIT form
        counts: an explicitly typed binding declares its type, reaches
        `local_var_types`, and must keep resolving normally.
        """
        child: Node | None = None
        current: Node | None = node
        while current is not None:
            if current.type == cs.TS_CSHARP_FOREACH_STATEMENT:
                body = current.child_by_field_name(cs.FIELD_BODY)
                bound = current.child_by_field_name(cs.FIELD_LEFT)
                declared = current.child_by_field_name(cs.FIELD_TYPE)
                implicit = declared is None or (
                    declared.type == cs.TS_CSHARP_IMPLICIT_TYPE
                )
                if (
                    child is not None
                    and body is not None
                    and child.id == body.id
                    and bound is not None
                    and implicit
                    and safe_decode_text(bound) == name
                ):
                    return True
            elif current.type == cs.TS_CSHARP_BLOCK and self._block_declares(
                current, name
            ):
                return True
            child, current = current, current.parent
        return False

    def _block_declares(self, block: Node, name: str) -> bool:
        # A local declared directly in this block; C# forbids a use before
        # its declaration, so its position within the block is no signal.
        for statement in block.named_children:
            for decl in statement.named_children:
                if decl.type != cs.TS_CSHARP_VARIABLE_DECLARATION:
                    continue
                for declarator in decl.named_children:
                    if (
                        declarator.type == cs.TS_CSHARP_VARIABLE_DECLARATOR
                        and safe_decode_text(
                            declarator.child_by_field_name(cs.FIELD_NAME)
                        )
                        == name
                    ):
                        return True
        return False

    def _declared_type_name(self, decl: Node) -> str | None:
        type_node = decl.child_by_field_name(cs.FIELD_TYPE)
        if type_node is None or type_node.type == cs.TS_CSHARP_IMPLICIT_TYPE:
            return None
        if type_text := safe_decode_text(type_node):
            return annotate_type_ref(type_text)
        return None

    def _record_local(
        self,
        declarator: Node,
        declared: str | None,
        types: dict[str, str],
        conflicted: set[str],
    ) -> None:
        var_name = safe_decode_text(declarator.child_by_field_name(cs.FIELD_NAME))
        if not var_name or var_name in conflicted:
            return
        var_type = declared or self._infer_initializer_type(declarator)
        if not var_type:
            return
        existing = types.get(var_name)
        if existing is not None and existing != var_type:
            del types[var_name]
            conflicted.add(var_name)
        else:
            types[var_name] = var_type

    @staticmethod
    def _infer_initializer_type(declarator: Node) -> str | None:
        # `var x = new T(...)` -> T, and `(T)e` or `e as T` -> T: the type the
        # initializer spells for itself. Any other initializer, a call above
        # all, is left untyped here, never typed by a construction among its
        # arguments (`var x = Factory.Make(new Input())` is no Input; Greptile
        # review of PR 2814); `_local_result_type` types a call's result
        # where the call resolves.
        initializer = _initializer(declarator)
        spelled = _spelled_type(initializer) if initializer is not None else None
        return annotate_type_ref(spelled) if spelled else None

    def _field_type(self, class_qn: str, field_name: str) -> str | None:
        # The declared type of `field_name` on class_qn or any base class,
        # read from the per-class maps recorded at ingestion (so it reaches a
        # field inherited from a base in another file). Seed the BFS with every
        # partial part of the class so a field declared on ANOTHER part
        # (`helper` on P1, used in a method on P2) is found; a visited guard
        # stops a malformed inheritance cycle looping.
        seen: set[str] = set()
        queue = deque(self.csharp_partial_groups.get(class_qn) or [class_qn])
        while queue:
            current = queue.popleft()
            if current in seen:
                continue
            seen.add(current)
            fields = self.class_field_types.get(current)
            if fields and field_name in fields:
                return fields[field_name]
            queue.extend(self.class_inheritance.get(current, []))
        return None

    # --- typed method-call resolution ------------------------------------

    def resolve_csharp_method_call(
        self,
        call_node: Node,
        local_var_types: dict[str, str] | None,
        module_qn: str,
        caller_qn: str | None = None,
    ) -> tuple[str, str] | None:
        """Memoising front for `_resolve_csharp_method_call` (issue #1800).

        Wrapping the entry point rather than editing the three recursive
        branches means every path in and every path back out shares one memo,
        including future callers.

        `_IN_PROGRESS` doubles as a cycle guard. A chain cannot be cyclic in
        valid C#, but a malformed or recovered parse tree can present one, and
        without this the recursion would not terminate. Returning None for a
        re-entered node degrades that site to an unresolved call, which is what
        the issue asks a pathological input to do instead of hanging.
        """
        key = self._call_key(call_node, module_qn, caller_qn)
        cached = self._call_memo.get(key, _MISSING)
        if cached is _IN_PROGRESS:
            return None
        if not isinstance(cached, _Sentinel):
            return cached
        self._call_memo[key] = _IN_PROGRESS
        result = self._resolve_csharp_method_call(
            call_node, local_var_types, module_qn, caller_qn
        )
        self._call_memo[key] = result
        return result

    @staticmethod
    def _call_key(
        call_node: Node, module_qn: str, caller_qn: str | None
    ) -> tuple[str, str | None, int, int]:
        return (module_qn, caller_qn, call_node.start_byte, call_node.end_byte)

    def clear_call_memo(self) -> None:
        # The ranked families answer the same keys as the memo, so they
        # expire with it.
        self._call_memo.clear()
        self._overload_families.clear()
        self._call_picks.clear()
        self._guessed_calls.clear()

    def csharp_member_overload_family(
        self, call_node: Node, module_qn: str, caller_qn: str | None
    ) -> list[str]:
        """The other overloads a member call may bind beside the one it
        resolved to, or [] when its arguments chose one (issue #2619)."""
        return self._overload_families.get(
            self._call_key(call_node, module_qn, caller_qn), []
        )

    def _resolve_csharp_method_call(
        self,
        call_node: Node,
        local_var_types: dict[str, str] | None,
        module_qn: str,
        caller_qn: str | None = None,
    ) -> tuple[str, str] | None:
        # A Roslyn call fact for this exact site wins over every heuristic: it
        # is the compiler's own overload resolution (argument types, not arity)
        # and covers receivers no syntax walk can type (chained returns) plus
        # reduced extension methods. Any key miss falls through to the
        # heuristics below.
        if semantic := self._semantic_call_target(call_node, module_qn):
            return semantic
        func = call_node.child_by_field_name(cs.TS_FIELD_FUNCTION)
        if func is None:
            return None
        if func.type != cs.TS_CSHARP_MEMBER_ACCESS_EXPRESSION:
            return self._resolve_non_member_call(func, call_node, module_qn, caller_qn)
        method_name = safe_decode_text(func.child_by_field_name(cs.FIELD_NAME))
        receiver = func.child_by_field_name(cs.TS_CSHARP_FIELD_EXPRESSION)
        if not method_name or receiver is None:
            return None
        # `Policy.Handle<TException>()`: a generic member's NAME field is a
        # generic_name; methods register generic-free, so strip the type
        # arguments or the fluent entry point never matches. They are kept
        # for overload ranking, which they fix the type parameters for.
        spelled = type_arguments(method_name)
        method_name = method_name.split(cs.CHAR_ANGLE_OPEN, 1)[0]
        arg_count = self._count_arguments(call_node)

        # `base.X()` binds the BASE chain only: a first-party base's member
        # when one exists, otherwise the base is external (object.Equals in
        # Polly's hide-object-members regions) and the call must emit nothing;
        # the trie fallback was self-looping it onto the caller's own override.
        if receiver.type == cs.TS_CSHARP_BASE_EXPRESSION:
            return self._resolve_base_call(method_name, arg_count, caller_qn)

        receiver_class_qn = self._resolve_receiver_class_qn(
            receiver, local_var_types or {}, module_qn, caller_qn
        ) or self._named_receiver_class(
            receiver, local_var_types or {}, module_qn, caller_qn
        )
        member_call = _MemberCall(
            call_node,
            receiver,
            method_name,
            arg_count,
            local_var_types or {},
            module_qn,
            caller_qn,
            spelled,
        )
        # C# weighs every instance method the arguments can bind, whether by
        # their count or through defaults and a `params` tail, as one set
        # (`M(int a, int b = 1, params int[] r)` beats `M(long)` for `M(1)`),
        # looks at extension methods only once no instance method applies
        # (`c.Foo()` binds `C.Foo(int value = 0)` over `Foo(this C)`;
        # CodeRabbit, PR 2814), and ranks those by their arguments too. One
        # that may or may not apply is never ruled out: while no instance
        # method is known to apply, the extensions stay candidates beside
        # them (PR 2814 review).
        ranking = self._rank_member_overloads(member_call, receiver_class_qn)
        picks = ranking.picks
        if not ranking.certain:
            picks = [*picks, *self._extension_picks(member_call, receiver_class_qn)]
        # A delegate-typed PROPERTY registers as a METHOD node with a bare
        # (arity-0) qn, so it may rank too: `entry.Callback()` is
        # Delegate.Invoke, not a call to the property.
        if picks:
            return self._method_or_property_target(self._settle(member_call, picks))
        return self._unranked_member_target(
            member_call, receiver_class_qn, bool(ranking.scored)
        )

    def _unranked_member_target(
        self, call: _MemberCall, receiver_class_qn: str | None, scored: bool
    ) -> tuple[str, str] | None:
        # What a member call no ranking settled binds. Only a call no
        # overload was scored for falls back on the name, and that is a
        # guess, never an exact edge, unless the name can name no other
        # method: one the ranking proved takes none of its arguments is not
        # bound at all.
        key = self._call_key(call.node, call.module_qn, call.caller_qn)
        if not scored and (
            guess := self._fallback_member_target(call, receiver_class_qn)
        ):
            if guess != CSHARP_EXTERNAL_TARGET and not (
                receiver_class_qn and self._names_alone(call, receiver_class_qn)
            ):
                self._guessed_calls.add(key)
            return guess
        if external := self._external_or_unresolved(
            call.receiver,
            call.method_name,
            receiver_class_qn,
            call.local_var_types,
            call.caller_qn,
        ):
            return external
        # A receiver of no type the index knows: every extension of the name
        # that takes the arguments may be what runs, a guess each.
        if receiver_class_qn is None and (
            guesses := self._untyped_extension_picks(call)
        ):
            self._guessed_calls.add(key)
            return self._method_or_property_target(self._settle(call, guesses))
        return None

    def _bare_target_in(
        self, bare: _MemberCall, class_qn: str, generic_call: bool
    ) -> tuple[str, str] | None:
        # A bare call's target among `class_qn`'s methods: ranked by argument
        # type as a member call on `this` is (PR 2814's Roslyn oracle); the
        # arity match only when no overload was scored.
        ranking = self._rank_member_overloads(bare, class_qn)
        if ranking.picks:
            return self._method_or_property_target(self._settle(bare, ranking.picks))
        matches = self._find_bare_call_matches(
            class_qn, bare.method_name, bare.arg_count, generic_call
        )
        if not matches:
            return None
        preferred = [
            m for m in matches if (m in self.csharp_generic_methods) == generic_call
        ]
        return cs.NodeLabel.METHOD.value, (preferred or matches)[0]

    def _settle(self, call: _MemberCall, picks: Sequence[Scored]) -> str:
        # The first of the overloads a call may bind; the rest, kept for the
        # call pass to fan out over, and how each was bound, for the type
        # its call returns.
        key = self._call_key(call.node, call.module_qn, call.caller_qn)
        qns = list(dict.fromkeys(pick.qn for pick in picks))
        if len(qns) > 1:
            self._overload_families[key] = qns[1:]
        bound: dict[str, Scored] = {}
        for pick in picks:
            bound.setdefault(pick.qn, pick)
        self._call_picks[key] = bound
        return qns[0]

    def _fallback_member_target(
        self, member_call: _MemberCall, receiver_class_qn: str | None
    ) -> tuple[str, str] | None:
        """The receiver's class member a call binds to by its argument count
        or its name alone, which no ranking chose."""
        if receiver_class_qn is None:
            return None
        if name_hit := self._find_arity_across_parts(
            receiver_class_qn, member_call.method_name, member_call.arg_count
        ) or self._find_name_across_parts(receiver_class_qn, member_call.method_name):
            # A delegate-typed PROPERTY invoked with call syntax
            # (`options.ShouldHandle(args)`) is Delegate.Invoke, not a method
            # call; binding the property as a METHOD fabricates an edge. Its
            # reachability comes from the read pass.
            return self._method_or_property_target(name_hit)
        return None

    def _names_alone(self, call: _MemberCall, receiver_class_qn: str) -> bool:
        # Whether a call no overload takes still names one method alone: its
        # receiver's whole first-party ancestry declares the name with one
        # signature, and nothing else can take the call, as no extension
        # method takes a type name and none of the name is indexed. C# binds
        # the call to that method and reports the error against it (a `cgr
        # check` edit dropping a default), which is no guess.
        ancestry = self._class_ancestry(receiver_class_qn)
        if not self._all_indexed(ancestry):
            return False
        named = [
            qn
            for owner in ancestry
            for qn in self.declared_methods(owner).get(call.method_name, ())
        ]
        signatures = {qn.partition(cs.CHAR_PAREN_OPEN)[2] for qn in named}
        # Type arguments the method does not declare as many of leave it out
        # of the member group (`c.G<int>(1)` on a plain `G(int)`).
        spelled = len(call.type_arguments)
        if spelled and any(
            len(self._declared_type_parameters(qn)) != spelled for qn in named
        ):
            return False
        return len(signatures) == 1 and (
            self._names_type(call)
            or not self.csharp_extension_methods.get(call.method_name)
        )

    def _names_type(self, call: _MemberCall) -> bool:
        # Whether a call's receiver is a type's name (`Util.Fmt(1)`), not a
        # value: no local, parameter or field of the name is in scope.
        receiver = call.receiver
        name = safe_decode_text(receiver)
        return (
            receiver.type == cs.TS_CSHARP_IDENTIFIER
            and bool(name)
            and not self._binds_local(receiver, name or "")
            and not self._receiver_type_name(
                receiver, call.local_var_types, call.module_qn, call.caller_qn
            )
        )

    def csharp_call_ranked(
        self, call_node: Node, module_qn: str, caller_qn: str | None
    ) -> bool:
        """Whether a call's overloads were ranked by argument type, which
        left the candidates it may bind in its family (PR 2814 review)."""
        return self._call_key(call_node, module_qn, caller_qn) in self._call_picks

    def csharp_call_guessed(
        self, call_node: Node, module_qn: str, caller_qn: str | None
    ) -> bool:
        """Whether a member call was bound by name alone, which no ranking of
        its arguments chose: a heuristic edge, never an exact one."""
        return self._call_key(call_node, module_qn, caller_qn) in self._guessed_calls

    def _external_or_unresolved(
        self,
        receiver: Node,
        method_name: str,
        receiver_class_qn: str | None,
        local_var_types: dict[str, str],
        caller_qn: str | None,
    ) -> tuple[str, str] | None:
        """The last word: an external target, or nothing.

        An object-virtual miss on a TYPED receiver (`severity.ToString()` on an
        enum, `options.GetType()`) resolves to System.Object/Enum; falling to
        the trie instead lands on whatever unrelated hide-object-members
        override exists (Polly's PolicyBuilder). An UNTYPED receiver is
        external when `_externally_targeted` says so; otherwise the call is
        simply unresolved.
        """
        if method_name in cs.CSHARP_OBJECT_VIRTUALS:
            return CSHARP_EXTERNAL_TARGET
        if receiver_class_qn is None and self._externally_targeted(
            receiver, method_name, local_var_types, caller_qn
        ):
            return CSHARP_EXTERNAL_TARGET
        return None

    def _resolve_non_member_call(
        self,
        func: Node,
        call_node: Node,
        module_qn: str,
        caller_qn: str | None,
    ) -> tuple[str, str] | None:
        """Resolve a call whose callee is not a member access.

        A bare `Foo(...)`/`Foo<T>(...)` follows C# simple-name lookup: an
        in-scope local function first (it shadows same-name method overloads),
        then an arity-matched member of the enclosing type. A miss falls to the
        generic simple-name path. Anything else (an invoked expression, say) is
        not resolvable here.
        """
        if func.type in (cs.TS_CSHARP_IDENTIFIER, cs.TS_CSHARP_GENERIC_NAME):
            return self._resolve_bare_call(func, call_node, module_qn, caller_qn)
        return None

    def _method_or_property_target(self, hit: str) -> tuple[str, str]:
        """A resolved qn as a call target, or external when it is a property.

        Both the arity and the name lookup end this way, so the property check
        lives here once rather than twice in the caller.
        """
        if self.function_registry.is_property(hit):
            return CSHARP_EXTERNAL_TARGET
        return cs.NodeLabel.METHOD.value, hit

    def _resolve_base_call(
        self, method_name: str, arg_count: int, caller_qn: str | None
    ) -> tuple[str, str] | None:
        """Resolve `base.X()` against the BASE chain only.

        Split out of `_resolve_csharp_method_call` to keep that function under
        the cognitive-complexity limit; the two nested walks below are its
        densest part and are self-contained.

        A first-party base's member wins when one exists; otherwise the base is
        external (`object.Equals` in Polly's hide-object-members regions) and
        the call must emit nothing, because the trie fallback was self-looping
        it onto the caller's own override. Arity-exact matches are tried across
        every partial part first, then name-only, mirroring the order the
        instance path uses.
        """
        class_qn = self._containing_class_qn(caller_qn)
        if class_qn is None:
            return CSHARP_EXTERNAL_TARGET
        seen: set[str] = set()
        for root in self._partial_roots(class_qn):
            for base_qn in self.class_inheritance.get(root, []):
                if hit := self._find_method_by_arity(
                    base_qn, method_name, arg_count, seen
                ):
                    return cs.NodeLabel.METHOD.value, hit
        seen = set()
        for root in self._partial_roots(class_qn):
            for base_qn in self.class_inheritance.get(root, []):
                if hit := self._find_method_by_name(base_qn, method_name, seen):
                    return cs.NodeLabel.METHOD.value, hit
        return CSHARP_EXTERNAL_TARGET

    def _externally_targeted(
        self,
        receiver: Node,
        method_name: str,
        local_var_types: dict[str, str],
        caller_qn: str | None,
    ) -> bool:
        # Only for UNTYPED receivers (a typed miss keeps today's trie
        # rescue): (a) a PascalCase identifier/dotted path that is no
        # local, no field, and no registered type is an external TYPE
        # (`Console`, `System.Console`); (b) an object-virtual member name
        # on an untyped receiver resolves to System.Object.
        if method_name in cs.CSHARP_OBJECT_VIRTUALS:
            return True
        # A `foreach (var x in ...)` binding is a LOCAL whose element type is
        # not inferred, so it never reaches `local_var_types`. Left to the
        # checks below it read as an external TYPE when PascalCase and fell
        # to the name trie otherwise, which bound the call to a registered
        # class of the same name: `foreach (var Config in items) {
        # Config.Each(); }` emitted an edge to `N.Config.Each` (Copilot,
        # PR #1998). A local owns the name whatever its type, so the call is
        # external to this graph, not a static call on that class.
        unwrapped = self._unwrap_receiver(receiver)
        if unwrapped is None:
            return False
        if self._is_untyped_local(unwrapped, local_var_types):
            return True
        if unwrapped.type not in (
            cs.TS_CSHARP_IDENTIFIER,
            cs.TS_CSHARP_MEMBER_ACCESS_EXPRESSION,
        ):
            return False
        text = safe_decode_text(unwrapped)
        if not text:
            return False
        segments = text.split(cs.SEPARATOR_DOT)
        head = segments[0]
        if head in local_var_types:
            # A local/param (any casing) whose DECLARED type resolves to
            # no registered type (`string[]`, reflection FieldInfo) is
            # external; its members cannot be attributed by name
            # (`names.Contains(x)` bound Context.Contains).
            return not self._registered_type_declares(
                local_var_types[head], method_name
            )
        if not all(seg[:1].isupper() for seg in segments):
            return False
        if class_qn := self._containing_class_qn(caller_qn):
            verdict = self._enclosing_member_external(
                class_qn, head, method_name, caller_qn
            )
            if verdict is not None:
                return verdict
        # Any registered type with this simple name means the receiver may
        # be first-party (even when twin ambiguity kept it untyped).
        if any(
            self.function_registry.get(qn) in _TYPE_DECLS
            for qn in self.simple_name_lookup.get(head, set())
        ):
            return False
        return True

    def _is_untyped_local(self, bound: Node, local_var_types: dict[str, str]) -> bool:
        # Only an IMPLICIT binding: `foreach (Item it in ...)` declares a
        # type, reaches local_var_types, and resolves normally -- this
        # branch is never reached for it. Guarding on the `var` form
        # keeps that path working.
        if bound.type != cs.TS_CSHARP_IDENTIFIER:
            return False
        name = safe_decode_text(bound)
        return bool(
            name and name not in local_var_types and self._binds_local(bound, name)
        )

    def _enclosing_member_external(
        self,
        class_qn: str,
        head: str,
        method_name: str,
        caller_qn: str | None,
    ) -> bool | None:
        # Tri-state: True/False decide externality from what the enclosing
        # type knows about the receiver head; None leaves it to the
        # registered-simple-name sweep.
        if member_type := self._field_type(class_qn, head):
            # A field/property receiver whose declared type is external
            # (`Wrapper` of BCL RateLimiter) makes the call external (the
            # trie self-looped `Wrapper.DisposeAsync()` onto the enclosing
            # class).
            return not self._registered_type_declares(member_type, method_name)
        if prop_qn := self.resolve_property_read(head, caller_qn):
            if entry := self.csharp_method_return_types.get(prop_qn):
                return not self._registered_type_declares(entry[0], method_name)
        # A PascalCase receiver is very often a PROPERTY or member of
        # the enclosing type (`Pipeline.Execute(...)`); anything the
        # enclosing type declares by that name is first-party, not an
        # external type.
        if self._find_name_across_parts(class_qn, head) is not None:
            return False
        return None

    def _resolve_bare_call(
        self,
        func: Node,
        call_node: Node,
        module_qn: str,
        caller_qn: str | None,
    ) -> tuple[str, str] | None:
        name = safe_decode_text(func)
        if not name:
            return None
        # `Handle<TException>(...)`: the callee name is the identifier
        # without its type arguments (matching how methods register).
        name = name.split(cs.CHAR_ANGLE_OPEN, 1)[0]
        arg_count = self._count_arguments(call_node)
        if local := self._find_in_scope_local_function(
            name, arg_count, caller_qn, module_qn
        ):
            return cs.NodeLabel.FUNCTION.value, local
        # Same-arity twins (`M(X) => M<Void>(x)` beside `M<T>(X)` on another
        # partial part, Polly's ResiliencePipeline Get*/Initialize*Context):
        # parameter arity cannot tell them apart, so prefer the overload
        # whose GENERICNESS matches the callee shape (`M<TResult>(...)` is a
        # generic_name, `M(...)` a plain identifier).
        generic_call = func.type == cs.TS_CSHARP_GENERIC_NAME
        bare = _MemberCall(
            call_node,
            func,
            name,
            arg_count,
            {},
            module_qn,
            caller_qn,
            type_arguments(safe_decode_text(func) or ""),
        )
        for class_qn in self._caller_class_candidates(caller_qn, module_qn):
            if target := self._bare_target_in(bare, class_qn, generic_call):
                return target
        # A bare object-virtual with no local declaration is
        # `this.GetType()` -> System.Object (Polly's PolicyBase.PolicyKey);
        # a bare name that IS a delegate-typed member of the enclosing type
        # (`Callback();` on a record positional property) is
        # Delegate.Invoke. Neither may fall to the bare-name trie.
        if name in cs.CSHARP_OBJECT_VIRTUALS:
            return CSHARP_EXTERNAL_TARGET
        if self.resolve_property_read(name, caller_qn) is not None:
            return CSHARP_EXTERNAL_TARGET
        if (class_qn := self._containing_class_qn(caller_qn)) and self._field_type(
            class_qn, name
        ):
            return CSHARP_EXTERNAL_TARGET
        # `using static N.T;` puts T's members in bare-call scope, but LAST:
        # a member of the enclosing type beats a static import in C#, so this
        # runs after the checks above that claim the name for the enclosing
        # type (a delegate-typed field's `Callback()` is Delegate.Invoke).
        return self._resolve_via_static_imports(bare)

    def _resolve_via_static_imports(self, bare: _MemberCall) -> tuple[str, str] | None:
        # The imported types' same-name static methods form ONE method group,
        # ranked by the call's arguments as a member call's overloads are: by
        # name, type and mode, in normal and expanded form (PR 2814 review:
        # `F(b: "s", a: 1)` binds `F(int a, string b)`, and `F(i, s)` one
        # overload, not every one of its arity). The best are bound, the call
        # fanning out over several of one type; None when none can take the
        # call, and when two types' are left, which is C#'s ambiguity
        # (CS0121) and would differ per run, the imports being a set (#2005).
        caller = self._caller_scope(bare)
        spelled = tuple(canonical_type(a, caller) for a in bare.type_arguments)
        candidates = {
            qn: type_qn
            for type_qn in self._static_import_types(bare.module_qn, bare.caller_qn)
            for qn in self._static_member_matches(
                type_qn, bare.method_name, bare.arg_count
            )
            if not spelled or len(self._declared_type_parameters(qn)) == len(spelled)
        }
        if not candidates:
            return None
        arguments, site = self._call_scoring(bare, caller)
        best = best_candidates(
            [
                scored._replace(owner=type_qn)
                for qn, type_qn in candidates.items()
                for scored in self._scores(
                    qn, arguments, ({}, frozenset(), spelled), site
                )
            ],
            self._better_target,
        )
        if not best or len({scored.owner for scored in best}) > 1:
            return None
        return cs.NodeLabel.METHOD.value, self._settle(bare, best)

    def _static_import_types(self, module_qn: str, caller_qn: str | None) -> list[str]:
        # The types a call's file brings into bare-call scope with `using
        # static`, its own and the project's global ones.
        type_qns: list[str] = []
        for static_type, context_qn in self._static_import_scope(module_qn, caller_qn):
            # The directive stores the C# type path (`Helpers.MathHelpers`);
            # the registry keys types by project-qualified qn, so resolve it
            # the same way a written type reference is resolved, from the
            # file that wrote it.
            type_qn = self._type_name_to_qn(static_type, context_qn)
            if type_qn and type_qn not in type_qns:
                type_qns.append(type_qn)
        return type_qns

    def _static_member_matches(
        self, type_qn: str, name: str, arg_count: int
    ) -> list[str]:
        # `using static T` imports the static members T itself declares:
        # not its instance methods, and not what it inherits (CodeRabbit,
        # PR #2036). A method with no recorded shape (not parsed this run)
        # cannot be checked for `static` and is kept.
        out: list[str] = []
        for root in self._partial_roots(type_qn):
            prefix = f"{root}{cs.SEPARATOR_DOT}"
            for qn in self._direct_same_name_methods(root, name):
                shape = self.csharp_call_shapes.get(qn)
                if shape is not None and not shape.is_static:
                    continue
                leaf = qn[len(prefix) :]
                if _arity(leaf) == arg_count or _accepts_arg_count(
                    leaf, arg_count, shape
                ):
                    out.append(qn)
        return out

    def _static_import_scope(
        self, module_qn: str, caller_qn: str | None
    ) -> list[tuple[str, str]]:
        # (type path, module that wrote it): the file's own `using static`
        # directives, then every `global using static` in the project, which
        # C# puts in scope in every file of the compilation. A directive
        # written inside `namespace N { ... }` is in scope only there, which
        # the caller's qn shows: it embeds the namespace after the module qn
        # (CodeRabbit, PR #2036).
        imports = self.import_processor
        scope = [
            (path, module_qn)
            for namespace, path in imports.csharp_static_imports.get(module_qn, ())
            if not namespace
            or (
                caller_qn is not None
                and caller_qn.startswith(
                    f"{module_qn}{cs.SEPARATOR_DOT}{namespace}{cs.SEPARATOR_DOT}"
                )
            )
        ]
        scope.extend(
            (path, declaring_qn)
            for declaring_qn, paths in imports.csharp_global_static_imports.items()
            for path in paths
        )
        return scope

    def _find_in_scope_local_function(
        self, name: str, arg_count: int, caller_qn: str | None, module_qn: str
    ) -> str | None:
        # Walk the caller's scope chain probing for a registered local
        # function. Each level is probed both as-is and with the overload
        # signature suffix stripped, because local functions register under the
        # BARE method scope name while the caller_qn carries the host overload's
        # signatured identity (`Handle(System.Func)` hosts `Handle.Handle`). A
        # hit must match the call's arity AND be declared in a host the caller
        # sits inside (C# scoping), without which the parameterless sibling
        # overload would capture the local fn textually nested under its own
        # bare qn.
        if not caller_qn:
            return None
        scope = caller_qn
        while len(scope) > len(module_qn):
            stripped = scope.split(cs.CHAR_PAREN_OPEN, 1)[0]
            for probe_scope in dict.fromkeys((scope, stripped)):
                candidate = f"{probe_scope}{cs.SEPARATOR_DOT}{name}"
                # Same-name local fns in SIBLING BLOCKS flatten to one scope
                # qn; later declarations carry an `@line` duplicate suffix,
                # so probe every registered variant, not just the natural qn
                # (else an arity-matched later declaration is missed and the
                # arity-blind fallback's duplicate fan-out fabricates a
                # phantom edge onto the uncalled sibling).
                for variant in self.function_registry.variants(candidate):
                    entry = self.csharp_local_functions.get(variant)
                    if (
                        entry is not None
                        and entry[1] == arg_count
                        and self._caller_within_host(caller_qn, entry[0])
                    ):
                        return variant
            if cs.SEPARATOR_DOT not in stripped:
                return None
            scope = stripped.rsplit(cs.SEPARATOR_DOT, 1)[0]
        return None

    def csharp_local_function_group(
        self, name: str, caller_qn: str | None, module_qn: str
    ) -> list[str]:
        # Every in-scope local function with this name, arity-blind: a
        # method GROUP argument (`return new(..., Dispose)` handing
        # Serilog's CreateLogger locals to the Logger ctor) carries no call
        # arity, so the whole registered group at the nearest declaring
        # scope is the referenced set. Locals shadow members, matching
        # _find_in_scope_local_function's scope-chain discipline.
        if not caller_qn:
            return []
        scope = caller_qn
        while len(scope) > len(module_qn):
            stripped = scope.split(cs.CHAR_PAREN_OPEN, 1)[0]
            if matches := self._local_function_group_at_scope(
                name, caller_qn, scope, stripped
            ):
                return matches
            if cs.SEPARATOR_DOT not in stripped:
                return []
            scope = stripped.rsplit(cs.SEPARATOR_DOT, 1)[0]
        return []

    def _local_function_group_at_scope(
        self, name: str, caller_qn: str, scope: str, stripped: str
    ) -> list[str]:
        matches: list[str] = []
        for probe_scope in dict.fromkeys((scope, stripped)):
            candidate = f"{probe_scope}{cs.SEPARATOR_DOT}{name}"
            for variant in self.function_registry.variants(candidate):
                entry = self.csharp_local_functions.get(variant)
                if entry is not None and self._caller_within_host(caller_qn, entry[0]):
                    matches.append(variant)
        return matches

    def _caller_within_host(self, caller_qn: str, host_key: FunctionSpanKey) -> bool:
        # True when the caller IS the local function's host scope or a local
        # function transitively hosted inside it (a sibling or nested local
        # fn calling across/into its own nest). Spans join lazily against
        # function_locations because the host's signatured identity was not
        # registered yet when the local function was pinned.
        host_loc = self.function_locations.get(host_key)
        if host_loc is None:
            return False
        host_qn = host_loc.qualified_name
        seen: set[str] = set()
        current = caller_qn
        while current not in seen:
            seen.add(current)
            if current == host_qn:
                return True
            entry = self.csharp_local_functions.get(current)
            if entry is None:
                return False
            next_loc = self.function_locations.get(entry[0])
            if next_loc is None:
                return False
            current = next_loc.qualified_name
        return False

    def _caller_class_candidates(
        self, caller_qn: str | None, module_qn: str
    ) -> Iterator[str]:
        # Enclosing-type candidates for a bare member call, outermost last:
        # strip the overload signature (only the leaf carries one, and its
        # qualified parameter types contain dots that would break a plain
        # rsplit), then peel scope segments down to the module boundary.
        if not caller_qn:
            return
        scope = caller_qn.split(cs.CHAR_PAREN_OPEN, 1)[0]
        while cs.SEPARATOR_DOT in scope:
            scope = scope.rsplit(cs.SEPARATOR_DOT, 1)[0]
            if len(scope) <= len(module_qn):
                return
            yield scope

    def resolve_property_read(self, name: str, caller_qn: str | None) -> str | None:
        # A bare-identifier read (`WrappedDictionary.Keys`) targets a
        # property of the caller's enclosing type (implicit this); resolve
        # across partial parts and base classes and accept ONLY a
        # registered property, so same-name methods stay out of the read
        # pass.
        class_qn = self._containing_class_qn(caller_qn)
        if class_qn is None:
            return None
        qn = self._find_name_across_parts(class_qn, name)
        if qn is not None and self.function_registry.is_property(qn):
            return qn
        return None

    def resolve_member_property_read(
        self, receiver_type: str, name: str, module_qn: str
    ) -> str | None:
        # `Cfg.Value` / `w.Inner`: the NAME field read resolved against the
        # receiver's type (a class name for a static read, an inferred
        # local/parameter type for an instance read). Accepts ONLY a
        # registered property; an unresolvable receiver yields nothing, so
        # unrelated `x.Value` chains never fabricate an edge.
        class_qn = self._type_name_to_qn(receiver_type, module_qn)
        if class_qn is None:
            return None
        qn = self._find_name_across_parts(class_qn, name)
        if qn is not None and self.function_registry.is_property(qn):
            return qn
        return None

    def semantic_fact_resolved(self, call_node: Node, module_qn: str) -> bool:
        # True when a Roslyn call fact pinned this exact site: the target is
        # the compiler's own overload choice, so arity-based widening (the
        # same-arity family fan-out) must stay off for it.
        return self._semantic_call_target(call_node, module_qn) is not None

    def _semantic_call_target(
        self, call_node: Node, module_qn: str
    ) -> tuple[str, str] | None:
        if not (self.csharp_call_sites or self.csharp_external_sites):
            return None
        key = self._call_site_key(call_node, module_qn)
        if key is None:
            return None
        fact = self.csharp_call_sites.get(key)
        if fact is not None:
            return declared_location(
                fact.target_file,
                fact.target_line,
                fact.target_col,
                self.function_locations,
                self.module_qn_to_file_path,
                self.repo_path,
                self._rel_to_module,
            )
        if key in self.csharp_external_sites:
            # Roslyn resolved this site to a METADATA method: the call
            # provably leaves the repo, so return the external sentinel and
            # keep the name trie from fabricating a first-party edge (the
            # untypeable-receiver fp class the pure frontend cannot see).
            return CSHARP_EXTERNAL_TARGET
        return None

    def _call_site_key(self, call_node: Node, module_qn: str) -> CallSiteKey | None:
        name_node = self._callee_name_node(call_node)
        if name_node is None:
            return None
        name = safe_decode_text(name_node)
        if not name:
            return None
        # Generic arguments stripped to match Roslyn's symbol name.
        return call_site_key(
            name_node,
            name.split(cs.CHAR_ANGLE_OPEN, 1)[0],
            module_qn,
            self.module_qn_to_file_path,
            self.repo_path,
        )

    def _callee_name_node(self, call_node: Node) -> Node | None:
        func = call_node.child_by_field_name(cs.TS_FIELD_FUNCTION)
        if func is None:
            return None
        if func.type == cs.TS_CSHARP_MEMBER_ACCESS_EXPRESSION:
            return func.child_by_field_name(cs.FIELD_NAME)
        if func.type in (cs.TS_CSHARP_IDENTIFIER, cs.TS_CSHARP_GENERIC_NAME):
            return func
        if func.type == cs.TS_CSHARP_CONDITIONAL_ACCESS_EXPRESSION:
            # `recv?.Method(...)`: the name lives on the member_binding child
            # (same token Roslyn keys its MemberBindingExpressionSyntax fact
            # on).
            binding = next(
                (
                    child
                    for child in func.children
                    if child.type == cs.TS_CSHARP_MEMBER_BINDING_EXPRESSION
                ),
                None,
            )
            if binding is not None:
                return binding.child_by_field_name(cs.FIELD_NAME)
        return None

    def _extension_picks(
        self, call: _MemberCall, receiver_qn: str | None
    ) -> list[Scored]:
        # The extension methods a member call may bind: those whose `this`
        # type takes the receiver (`_receiving`) that take its arguments,
        # ranked by them as instance methods are, the receiver passed first
        # (PR 2814's Roslyn oracle: `c.M("s")` binds `M(this C, string)`
        # beside `M(this C, int)`).
        args = (call.receiver, call.local_var_types, call.module_qn, call.caller_qn)
        type_name = self._receiver_type_name(*args)
        if not type_name:
            return []
        receiving = dict(
            self._extension_candidates(
                _ExtendedReceiver(
                    type_name,
                    self._receiver_type_arity(*args),
                    receiver_qn,
                    self._caller_scope(call),
                ),
                call.method_name,
                call.arg_count,
            )
        )
        return self._rank_extensions(call, receiving)

    def _untyped_extension_picks(self, call: _MemberCall) -> list[Scored]:
        # The extension methods of the name in scope that take the
        # arguments, when the receiver is of no type the index knows: any
        # may take it. One no `using` the index sees brings in is left out,
        # as it would bind a receiver of any type anywhere.
        qns = [
            entry[0]
            for entry in self.csharp_extension_methods.get(call.method_name, ())
            if self._takes_after_receiver(entry[0], call.arg_count)
        ]
        outside = len(_enclosing_namespaces(call.node)) + 1
        receiving = {
            qn: Fit.UNKNOWN
            for level, level_qns in self._extension_levels(call, qns)
            if level < outside
            for qn in level_qns
        }
        return self._rank_extensions(call, receiving)

    def _rank_extensions(
        self, call: _MemberCall, receiving: Mapping[str, Fit]
    ) -> list[Scored]:
        # Extension candidates ranked by the call's arguments, the receiver
        # first, taken as `receiving` says. C# looks for them scope by scope
        # (`_extension_levels`): a scope holding one known to apply decides.
        caller = self._caller_scope(call)
        spelled = tuple(canonical_type(a, caller) for a in call.type_arguments)
        qns = [
            qn
            for qn in receiving
            if not spelled or len(self._declared_type_parameters(qn)) == len(spelled)
        ]
        if not qns:
            return []
        arguments: list[ArgumentType | None] = [
            None,
            *(
                _canonical_argument(argument, caller)
                for argument in self._argument_types(
                    call.node, call.local_var_types, call.module_qn, call.caller_qn
                )
            ),
        ]
        names, modes = _argument_names(call.node), _argument_modes(call.node)
        # The receiver is passed as its `this` parameter takes it.
        site = _CallSite(
            (None, *names) if any(names) else (),
            (_receiver_passing(call.receiver), *(modes or ("",) * call.arg_count)),
        )
        picks: list[Scored] = []
        for level, level_qns in self._extension_levels(call, qns):
            scored = [
                _received(s, receiving[qn])._replace(owner=f"{_EXTENSION_SCOPE}{level}")
                for qn in level_qns
                for s in self._scores(qn, arguments, ({}, frozenset(), spelled), site)
            ]
            picks.extend(scored)
            if any(certainly_applies(s) for s in scored):
                break
        return best_candidates(picks, self._better_target, _outer_scope)

    def _extension_levels(
        self, call: _MemberCall, qns: Sequence[str]
    ) -> list[tuple[int, list[str]]]:
        # Extension candidates by the scope C# finds them in: each namespace
        # enclosing the call, innermost first, then the file's (the global
        # namespace's, and those its own and every global `using` imports),
        # then any other, which an import the index does not see (a project
        # file's `<Using>`) may still bring in (PR 2814's Roslyn oracle).
        enclosing = _enclosing_namespaces(call.node)
        imports = self.import_processor
        imported = set(imports.csharp_usings(call.module_qn).values())
        for usings in imports.csharp_global_usings.values():
            imported |= usings
        statics = {
            path for _ns, path in imports.csharp_static_imports.get(call.module_qn, ())
        }
        for paths in imports.csharp_global_static_imports.values():
            statics |= paths
        namespaces = {
            entry[0]: entry[2]
            for entry in self.csharp_extension_methods.get(call.method_name, ())
        }
        levels: dict[int, list[str]] = {}
        for qn in qns:
            namespace = namespaces.get(qn, "")
            if namespace in enclosing:
                level = enclosing.index(namespace)
            elif (
                not namespace
                or namespace in imported
                or self._namespaced_owner(qn) in statics
            ):
                level = len(enclosing)
            else:
                level = len(enclosing) + 1
            levels.setdefault(level, []).append(qn)
        return sorted(levels.items())

    def _namespaced_owner(self, method_qn: str) -> str | None:
        # The namespace-qualified name of a method's declaring class.
        owner = method_qn.split(cs.CHAR_PAREN_OPEN, 1)[0].rpartition(cs.SEPARATOR_DOT)[
            0
        ]
        return self.csharp_class_namespaced.get(owner)

    def _value_keyword(
        self, value: Node, depth: int = 0, module_qn: str | None = None
    ) -> str | None:
        # The type a literal, a local or parameter of a predefined type, or
        # an operator over them is: C#'s binary numeric promotion (`x + 1L`
        # is long), string concatenation and comparisons, or, read in
        # `module_qn`, the result of a user-defined operator an operand's
        # first-party type declares (its qn: `s1 && s2` on a struct with
        # `operator &`). None for anything else.
        while value.type == cs.TS_PARENTHESIZED_EXPRESSION and value.named_children:
            value = value.named_children[0]
        if (constant := _literal_constant(value)) is not None:
            return predefined_keyword(constant.type) if constant.type else None
        if value.type == cs.TS_CSHARP_CAST_EXPRESSION:
            return self._cast_keyword(value, depth, module_qn)
        if value.type == cs.TS_CSHARP_IDENTIFIER:
            written = self._written_receiver_type(value)
            return predefined_keyword(written) if written else None
        if value.type == cs.TS_CSHARP_PREFIX_UNARY_EXPRESSION:
            return self._unary_keyword(value, depth, module_qn)
        if value.type != cs.TS_BINARY_EXPRESSION or depth > _MAX_VALUE_DEPTH:
            return None
        left = value.child_by_field_name(cs.FIELD_LEFT)
        right = value.child_by_field_name(cs.FIELD_RIGHT)
        operator = safe_decode_text(value.child_by_field_name(cs.FIELD_OPERATOR))
        if left is None or right is None or operator is None:
            return None
        if operator == cs.CSHARP_COALESCE_OPERATOR:
            return self._coalesced(left, right, depth)
        return self._binary_keyword(operator, left, right, depth, module_qn)

    def _unary_keyword(
        self, value: Node, depth: int, module_qn: str | None
    ) -> str | None:
        # What a user-defined prefix operator gives (`-s` on a struct
        # declaring `operator -(S)`). None for a predefined operator's
        # result, which is not read here, and while one may apply.
        operator = safe_decode_text(value.children[0]) if value.children else None
        operand = value.named_children[0] if value.named_children else None
        if (
            operator not in cs.CSHARP_OVERLOADABLE_UNARY_OPERATORS
            or operand is None
            or depth > _MAX_VALUE_DEPTH
        ):
            return None
        typed = self._value_keyword(operand, depth + 1, module_qn)
        user = self._user_operator(operator, (operand,), (typed,), module_qn)
        return None if isinstance(user, _Sentinel) else user

    def _binary_keyword(
        self,
        operator: str,
        left: Node,
        right: Node,
        depth: int,
        module_qn: str | None = None,
    ) -> str | None:
        # What `left operator right` gives. A user-defined operator an
        # operand's type declares decides (Greptile review of PR 2814: `&&`
        # on a type with `operator &` is of that type); while one may, the
        # result is not known. Otherwise a comparison is a bool, a
        # concatenation a string, a shift its left operand's promotion, and
        # C#'s binary numeric promotion the rest, where a constant the
        # unsigned operand's type holds takes that type (`u + 1` is a uint,
        # `u + i` a long). None while an operand may be a constant of unknown
        # value.
        operands = (
            self._value_keyword(left, depth + 1, module_qn),
            self._value_keyword(right, depth + 1, module_qn),
        )
        user = self._user_operator(operator, (left, right), operands, module_qn)
        if not isinstance(user, _Sentinel):
            return user
        if operator in cs.CSHARP_COMPARISON_OPERATORS:
            return cs.CSHARP_BOOL_KEYWORD
        if operator == cs.CHAR_PLUS and cs.CSHARP_STRING_KEYWORD in operands:
            return cs.CSHARP_STRING_KEYWORD
        if operator in cs.CSHARP_SHIFT_OPERATORS:
            return _shifted(*operands)
        unsigned = self._unsigned_result(operands, (left, right), depth)
        if not isinstance(unsigned, _Sentinel):
            return unsigned
        return _operated(operator, *operands)

    def _unsigned_result(
        self,
        operands: tuple[str | None, str | None],
        nodes: tuple[Node, Node],
        depth: int,
    ) -> str | _Sentinel | None:
        # An unsigned operand's type, where the other is a constant it holds
        # (`u + 1` is a uint); None while that constant's value is not known;
        # `_PASSED_OVER` for the operands' promotion.
        left, right = nodes
        for unsigned, other, node in (
            (operands[0], operands[1], right),
            (operands[1], operands[0], left),
        ):
            if other not in cs.CSHARP_UNSIGNED_CONSTANT_SOURCES.get(unsigned or "", ()):
                continue
            constant, value = self._constness(node, depth + 1)
            if constant is None or (constant and value is None):
                return None
            if constant and value is not None and value >= 0:
                return unsigned
        return _PASSED_OVER

    def _constness(self, value: Node, depth: int) -> tuple[bool | None, int | None]:
        # Whether an operand is a constant, with the value a literal writes,
        # a variable (False), or either (None): an integral constant converts
        # to an unsigned type that holds it.
        while value.type == cs.TS_PARENTHESIZED_EXPRESSION and value.named_children:
            value = value.named_children[0]
        if (constant := _literal_constant(value)) is not None:
            return True, constant.value if isinstance(constant.value, int) else None
        if value.type == cs.TS_CSHARP_IDENTIFIER:
            return self._local_constness(value)
        # A cast of a constant is one: `(int)1`, not `(int)x`.
        if value.type == cs.TS_CSHARP_CAST_EXPRESSION and (
            operand := value.child_by_field_name(cs.FIELD_VALUE)
        ):
            return self._constness(operand, depth + 1)
        if value.type != cs.TS_BINARY_EXPRESSION or depth > _MAX_VALUE_DEPTH:
            return None, None
        return self._operands_constness(value, depth)

    def _operands_constness(
        self, value: Node, depth: int
    ) -> tuple[bool | None, int | None]:
        # An operator over a variable is a variable; over two constants, a
        # constant of a value not read.
        parts = [
            self._constness(part, depth + 1)
            for part in (
                value.child_by_field_name(cs.FIELD_LEFT),
                value.child_by_field_name(cs.FIELD_RIGHT),
            )
            if part is not None
        ]
        if any(constant is False for constant, _ in parts):
            return False, None
        both = len(parts) == 2 and all(constant for constant, _ in parts)
        return (True, None) if both else (None, None)

    def _local_constness(self, name_node: Node) -> tuple[bool | None, int | None]:
        # A parameter or a local is a variable unless declared `const`, whose
        # value its literal initializer writes; anything else (a field) may
        # be a constant.
        name = safe_decode_text(name_node) or ""
        current = name_node.parent
        while current is not None and current.type != cs.TS_CSHARP_DECLARATION_LIST:
            if current.type == cs.TS_CSHARP_BLOCK and (
                found := self._block_declaration(current, name)
            ):
                return _declared_constness(found[0], found[2])
            if self._parameter_type(current, name):
                return False, None
            current = current.parent
        return None, None

    def _coalesced(self, left: Node, right: Node, depth: int) -> str | None:
        # `n ?? 1` on an `int? n`: the nullable's value type, when the right
        # operand is of that type too.
        if left.type != cs.TS_CSHARP_IDENTIFIER:
            return None
        written = self._written_receiver_type(left) or ""
        underlying = predefined_keyword(written.removesuffix(cs.CHAR_QUESTION_MARK))
        if not written.endswith(cs.CHAR_QUESTION_MARK) or underlying is None:
            return None
        return (
            underlying if self._value_keyword(right, depth + 1) == underlying else None
        )

    def _cast_keyword(
        self, value: Node, depth: int, module_qn: str | None = None
    ) -> str | None:
        # A cast to a predefined type is of that type. `(x) + 1` parses as a
        # cast of `+1` to a type `x`; C# reads it as a sum when `x` is a
        # variable, not a type. None for a cast to any other type.
        cast_to = value.child_by_field_name(cs.FIELD_TYPE)
        operand = value.child_by_field_name(cs.FIELD_VALUE)
        if cast_to is not None and cast_to.type == cs.TS_CSHARP_PREDEFINED_TYPE:
            return predefined_keyword(safe_decode_text(cast_to) or "")
        if (
            cast_to is None
            or operand is None
            or not _reads_as_sum(value)
            or self._value_keyword(cast_to, depth + 1) is None
        ):
            return None
        sign = safe_decode_text(operand.children[0]) or ""
        return self._binary_keyword(
            sign, cast_to, operand.named_children[-1], depth, module_qn
        )

    def _user_operator(
        self,
        operator: str,
        nodes: tuple[Node, ...],
        operands: tuple[str | None, ...],
        module_qn: str | None,
    ) -> str | _Sentinel | None:
        # The type of the user-defined operator C# binds the operands to
        # (§12.4.4, §12.4.5): of the operators their types declare, those
        # that take them, ranked as a method call's overloads are, the best
        # one known to apply. `_PASSED_OVER` when none can take them, so a
        # predefined operator applies (Greptile review of PR 2814: `+(S, S)`
        # takes no `string`, so `text + s` concatenates); None when that is
        # not known: an operand of no known type, or no single best.
        symbol = (
            cs.CSHARP_CONDITIONAL_OPERATORS.get(operator, operator)
            if len(nodes) == 2
            else operator
        )
        found = self._operator_candidates(symbol, nodes, operands, module_qn)
        if found is None:
            return None
        if not found:
            return _PASSED_OVER
        best = best_candidates(found, self._better_target)
        if len(best) != 1 or not certainly_applies(best[0]):
            return None
        return self._operator_return(best[0].qn)

    def _operator_candidates(
        self,
        symbol: str,
        nodes: tuple[Node, ...],
        operands: tuple[str | None, ...],
        module_qn: str | None,
    ) -> list[Scored] | None:
        # The `operator symbol` overloads the operands' types declare that
        # may take them, scored as a call's overloads are; None when an
        # operand's type, or one of its bases, is not known.
        owners: list[str] = []
        for node, typed in zip(nodes, operands, strict=True):
            owner = self._operand_owner(node, typed, module_qn)
            if isinstance(owner, _Sentinel):
                return None
            if owner is not None and owner not in owners:
                owners.append(owner)
        if not owners:
            return []
        if module_qn is None:
            return None
        caller = self._scope_at(nodes[0], module_qn)
        arguments = [
            _canonical_argument(self._operand_argument(node, typed), caller)
            for node, typed in zip(nodes, operands, strict=True)
        ]
        found: dict[str, Scored] = {}
        for owner in owners:
            declared = self._operator_overloads(owner, symbol, arguments)
            if declared is None:
                return None
            for scored in declared:
                found.setdefault(scored.qn, scored)
        return list(found.values())

    def _operator_overloads(
        self, owner: str, symbol: str, arguments: list[ArgumentType | None]
    ) -> list[Scored] | None:
        # The `operator symbol` overloads a type and its bases declare that
        # may take the operands, scored in each: a base's are candidates only
        # while none of a type deriving from it is known to take them
        # (§12.4.6). None when a base is not indexed, which may declare more.
        hierarchy = self._hierarchy(owner)
        if not self._all_indexed(hierarchy.order):
            return None
        name = f"{cs.TS_CSHARP_OPERATOR_NAME_PREFIX}{symbol}"
        found: list[Scored] = []
        blocked: set[str] = set()
        for declaring in hierarchy.order:
            if declaring in blocked:
                continue
            shape = self.csharp_generic_shapes.get(declaring)
            receiver = ({}, frozenset(shape.parameters if shape else ()), ())
            viable = [
                scored._replace(owner=declaring)
                for qn in self.declared_methods(declaring).get(name, ())
                for scored in self._scores(qn, arguments, receiver, _CallSite())
                if None not in scored.fits
            ]
            found.extend(viable)
            if any(certainly_applies(scored) for scored in viable):
                blocked |= hierarchy.ancestors.get(declaring, frozenset())
        return found

    def _operand_argument(self, node: Node, typed: str | None) -> ArgumentType | None:
        # An operand as an operator's parameter takes it: a literal or a
        # `const` local as its constant, any other local as its declared
        # type, else the type its typing gave (a keyword, or a user-defined
        # operator's result) or the one it is written with (a cast, a `new`).
        while node.type == cs.TS_PARENTHESIZED_EXPRESSION and node.named_children:
            node = node.named_children[0]
        if (constant := _literal_constant(node)) is not None:
            return ArgumentType(constant.type, None, 0, None, constant.value)
        if node.type == cs.TS_CSHARP_IDENTIFIER and (
            declared := self._written_receiver_type(node)
        ):
            return self._local_argument(node, declared)
        written = typed or _spelled_type(node)
        return _written_argument(written) if written else None

    def _operand_bound(
        self, written: str, node: Node, module_qn: str
    ) -> str | _Sentinel | None:
        # The type whose operators an operand of a written type brings in:
        # itself; for a type parameter in scope, the one type its `where`
        # clauses name, None for one they name no type of, `_PASSED_OVER`
        # for several or one not known.
        bounds = _type_parameter_bounds_at(node)
        core = strip_generic_arguments(written)
        if core not in bounds:
            return written
        if all(bound in cs.CSHARP_KIND_CONSTRAINTS for bound in bounds[core]):
            return None
        return self._named_at(written, node, module_qn) or _PASSED_OVER

    def _operator_return(self, operator_qn: str) -> str | None:
        # The type a user-defined operator is declared to return: a keyword,
        # or the first-party type's qn; None when that is not known.
        declared_return = self._declared_return(operator_qn)
        if declared_return is None or not declared_return[1]:
            return None
        canonical = declared_return[0]
        return predefined_keyword(canonical) or self._first_party_type(canonical)

    def _operand_owner(
        self, node: Node, typed: str | None, module_qn: str | None
    ) -> str | _Sentinel | None:
        # The first-party type whose operators an operand brings in; None
        # for a literal or a predefined type, which declares none;
        # `_PASSED_OVER` for an operand of no type known.
        if typed is not None and predefined_keyword(typed) is not None:
            return None
        if typed is not None and self.function_registry.get(typed) in _TYPE_DECLS:
            return typed
        while node.type == cs.TS_PARENTHESIZED_EXPRESSION and node.named_children:
            node = node.named_children[0]
        if _literal_constant(node) is not None:
            return None
        written = _spelled_type(node) or (
            self._written_receiver_type(node)
            if node.type == cs.TS_CSHARP_IDENTIFIER
            else None
        )
        if not written or module_qn is None:
            return _PASSED_OVER
        bound = self._operand_bound(written, node, module_qn)
        if not isinstance(bound, str):
            return bound
        written = bound
        if predefined_keyword(written) is not None:
            return None
        qn = self._resolve_in(
            module_qn,
            strip_generic_arguments(written),
            generic_arity_of_type_text(written),
        )
        return qn if qn is not None else _PASSED_OVER

    def _receiver_type_name(
        self,
        receiver: Node,
        local_var_types: dict[str, str],
        module_qn: str,
        caller_qn: str | None,
    ) -> str | None:
        # The receiver's declared type NAME (not its class qn), needed for the
        # extension-method fallback: extensions frequently target BCL types
        # (`string`, `int`) that are never registered as classes, so the raw
        # type name is all we can match on. Mirrors _resolve_receiver_class_qn's
        # branches but stops at the name.
        unwrapped = self._unwrap_receiver(receiver)
        if unwrapped is None:
            return None
        receiver = unwrapped
        # A literal or a computed value of a predefined type: `1.Touch()`,
        # `(x + 1).Touch()` (PR 2814's Roslyn oracle), or of the type a
        # user-defined operator gives.
        if (keyword := self._value_keyword(receiver, 0, module_qn)) is not None:
            return keyword
        # `((Widget)o).Ext()`: the cast target IS the receiver type, so an
        # extension-only method still binds on a cast receiver; same for a
        # `new Widget(...)` receiver. `(w) + 1` is a sum of no type known.
        if receiver.type == cs.TS_CSHARP_CAST_EXPRESSION and _reads_as_sum(receiver):
            return None
        if receiver.type in (
            cs.TS_CSHARP_CAST_EXPRESSION,
            cs.TS_CSHARP_OBJECT_CREATION_EXPRESSION,
        ):
            return self._annotated_type_field(receiver)
        # Same chained-receiver typing as the instance path, stopping at the
        # (arity-annotated) type name; extensions often target unregistered BCL
        # types like `string`, and both sides of the matcher carry the
        # annotation consistently.
        if receiver.type == cs.TS_CSHARP_INVOCATION_EXPRESSION:
            return self._invocation_return_type_name(
                receiver, local_var_types, module_qn, caller_qn
            )
        if receiver.type == cs.TS_CSHARP_THIS:
            return self._this_receiver_type(module_qn, caller_qn)
        if receiver.type == cs.TS_CSHARP_MEMBER_ACCESS_EXPRESSION:
            return self._this_field_receiver_type(
                receiver, caller_qn
            ) or self._member_field_type(
                receiver, local_var_types, module_qn, caller_qn
            )
        if receiver.type in (
            cs.TS_CSHARP_ELEMENT_ACCESS_EXPRESSION,
            cs.TS_CSHARP_IDENTIFIER,
        ):
            return self._held_type_name(receiver, local_var_types, module_qn, caller_qn)
        return None

    def _held_type_name(
        self,
        receiver: Node,
        local_var_types: dict[str, str],
        module_qn: str,
        caller_qn: str | None,
    ) -> str | None:
        # The type name an element or a local, parameter or field receiver
        # is declared with, read where it is written (`_named_at`).
        if receiver.type == cs.TS_CSHARP_ELEMENT_ACCESS_EXPRESSION:
            held = self._element_type(receiver, local_var_types, module_qn, caller_qn)
        else:
            held = self._local_result_type(
                receiver, local_var_types, module_qn, caller_qn
            ) or self._identifier_receiver_type(receiver, local_var_types, caller_qn)
        return self._named_at(held, receiver, module_qn) if held else None

    def _named_at(self, type_name: str, node: Node, module_qn: str) -> str | None:
        # A type a receiver is of, by the name it is written with where
        # `node` is: a type parameter in scope there stands for the one
        # first-party type its `where` clauses name, whose members it has,
        # and for no type of the file of its name (Greptile review of PR
        # 2814); None when they name none or several.
        bounds = _type_parameter_bounds_at(node)
        core = split_type_ref(type_name)[0].removesuffix(cs.CHAR_QUESTION_MARK)
        if core not in bounds:
            return type_name
        named = [
            bound
            for bound in bounds[core]
            if bound not in cs.CSHARP_KIND_CONSTRAINTS
            and strip_generic_arguments(bound) not in bounds
            and self._bound_qn(bound, module_qn) is not None
        ]
        return named[0] if len(named) == 1 else None

    def _bound_qn(self, bound: str, module_qn: str) -> str | None:
        # The first-party type a `where` clause names, closed or not.
        return self._resolve_in(
            module_qn,
            strip_generic_arguments(bound),
            generic_arity_of_type_text(bound),
        )

    def _type_at(self, type_name: str, node: Node, module_qn: str) -> str | None:
        # The first-party type a receiver of a written type is of
        # (`_named_at`).
        named = self._named_at(type_name, node, module_qn)
        if named is None:
            return None
        if named != type_name:
            return self._bound_qn(named, module_qn)
        return self._type_name_to_qn(named, module_qn)

    def _named_receiver_class(
        self,
        receiver: Node,
        local_var_types: dict[str, str],
        module_qn: str,
        caller_qn: str | None,
    ) -> str | None:
        # The first-party class a receiver's type name names, when only the
        # extension lookup's typing reads it (an array element, an operator's
        # result): its instance methods come before any extension (PR 2814's
        # oracle).
        unwrapped = self._unwrap_receiver(receiver)
        if unwrapped is None or unwrapped.type not in (
            cs.TS_CSHARP_ELEMENT_ACCESS_EXPRESSION,
            cs.TS_BINARY_EXPRESSION,
            cs.TS_CSHARP_PREFIX_UNARY_EXPRESSION,
        ):
            return None
        name = self._receiver_type_name(receiver, local_var_types, module_qn, caller_qn)
        return self._type_name_to_qn(name, module_qn) if name else None

    def _member_field_type(
        self,
        receiver: Node,
        local_var_types: dict[str, str],
        module_qn: str,
        caller_qn: str | None,
    ) -> str | None:
        # `Holder.F` or `holder.F`: the type the field's declaration records,
        # on the class the expression before the dot is or holds.
        expr = receiver.child_by_field_name(cs.TS_CSHARP_FIELD_EXPRESSION)
        field = safe_decode_text(receiver.child_by_field_name(cs.FIELD_NAME))
        if expr is None or not field:
            return None
        owner = self._resolve_receiver_class_qn(
            expr, local_var_types, module_qn, caller_qn
        )
        return self._field_type(owner, field) if owner else None

    def _element_type(
        self,
        receiver: Node,
        local_var_types: dict[str, str],
        module_qn: str,
        caller_qn: str | None,
    ) -> str | None:
        # `arr[0]`, `Holder.Arr[0]` or `list[0]`: the element of an array, a
        # string's char, or what the indexed type's indexer returns.
        expr = receiver.child_by_field_name(cs.TS_CSHARP_FIELD_EXPRESSION)
        if expr is None:
            return None
        if expr.type == cs.TS_CSHARP_IDENTIFIER:
            written = self._written_receiver_type(expr)
        elif expr.type == cs.TS_CSHARP_MEMBER_ACCESS_EXPRESSION:
            written = self._member_field_type(
                expr, local_var_types, module_qn, caller_qn
            )
        else:
            return None
        if not written:
            return None
        if written.endswith(cs.CSHARP_ARRAY_SUFFIX):
            return written.removesuffix(cs.CSHARP_ARRAY_SUFFIX)
        # A string's indexer returns its chars.
        if predefined_keyword(written.removesuffix(cs.CHAR_QUESTION_MARK)) == (
            cs.CSHARP_STRING_KEYWORD
        ):
            return cs.CSHARP_CHAR_KEYWORD
        return self._indexed_type(
            written, _IndexSite(receiver, local_var_types, module_qn, caller_qn)
        )

    def _indexed_type(self, written: str, site: _IndexSite) -> str | None:
        # What indexing a value of a written type gives. A first-party type
        # the file names by it gives what its indexer is declared to return
        # (Greptile review of PR 2814: a project's `List<T>` may index to
        # anything); a framework collection its type argument, only where
        # the name is the framework's: the file sees no first-party type of
        # it, and imports the framework's namespace or no first-party type
        # has the name at all.
        # A type parameter's indexers are its constraint's.
        named = self._named_at(written, site.access, site.module_qn)
        if named is None:
            return None
        name = strip_generic_arguments(named)
        arguments = type_arguments(named)
        owner = self._resolve_in(site.module_qn, name, len(arguments))
        if owner is not None:
            return self._indexer_result(owner, arguments, site)
        return self._framework_element(name, arguments, site.module_qn)

    def _framework_element(
        self, name: str, arguments: Sequence[str], module_qn: str
    ) -> str | None:
        # The element a framework collection a spelling names indexes to,
        # where the spelling names the framework's type: by its namespace
        # (an alias of it too), or bare in a file that sees no first-party
        # type of the name and imports the framework's namespace, or when no
        # first-party type has the name at all.
        namespace, _, simple = name.rpartition(cs.SEPARATOR_DOT)
        entry = cs.CSHARP_INDEXED_ELEMENT.get(simple)
        if entry is None or entry[1] >= len(arguments):
            return None
        framework, index = entry
        namespace = self.import_processor.csharp_usings(module_qn).get(
            namespace, namespace
        )
        if namespace not in ("", framework) or not (
            namespace
            or framework in self._imported_namespaces(module_qn)
            or not self._names_first_party(name, len(arguments))
        ):
            return None
        return arguments[index]

    def _imported_namespaces(self, module_qn: str) -> set[str]:
        # The namespaces a file's `using`s and every global `using` import.
        imports = self.import_processor
        imported = set(imports.csharp_usings(module_qn).values())
        for usings in imports.csharp_global_usings.values():
            imported |= usings
        return imported

    def _indexer_result(
        self, owner: str, arguments: Sequence[str], site: _IndexSite
    ) -> str | None:
        # The type the indexer C# binds an index to returns (§12.8.12.3): of
        # the indexers a first-party type and its bases declare, those the
        # index may bind, ranked as a call's overloads are (Greptile review
        # of PR 2814: `xs[b: "s", a: 0]` binds `this[int a, string b]`). Once
        # one a type declares is known to take the index, every indexer its
        # bases declare drops out, however else they are reached (`IC : IB,
        # IA` binds `IB`'s, which hides `IA`'s). The index and the written
        # type arguments resolve where the access is written, the type
        # parameters around it in scope; each type's parameters are bound by
        # them and by those each type passes its bases. A type deriving from
        # a framework collection indexes as it where no indexer of its own
        # takes the index. None when no indexer is known or several may
        # apply.
        caller = self._scope_at(site.access, site.module_qn)
        shape = self.csharp_generic_shapes.get(owner)
        parameters = shape.parameters if shape is not None else ()
        bindings = {
            p: canonical_type(a, caller)
            for p, a in zip(parameters, arguments, strict=False)
        }
        index = _Index(
            [
                _canonical_argument(argument, caller)
                for argument in self._argument_types(
                    site.access, site.local_var_types, site.module_qn, site.caller_qn
                )
            ],
            _CallSite(_argument_names(site.access), _argument_modes(site.access)),
            caller.type_parameters,
        )
        walk, bases_of = self._indexer_walk(owner, bindings)
        ancestors = _ancestors_within([current for current, _ in walk], bases_of)
        picks: list[Scored] = []
        returns: dict[str, str | None] = {}
        deciding: set[str] = set()
        frameworks: list[tuple[str, str]] = []
        for current, bound in walk:
            level = self._indexer_level(current, bound, index, returns)
            picks.extend(level)
            if any(certainly_applies(scored) for scored in level):
                deciding.add(current)
            elif (element := self._framework_base_element(current, bound)) is not None:
                frameworks.append((current, element))
        hidden = set().union(*(ancestors[current] for current in deciding))
        return self._picked_index(
            [scored for scored in picks if scored.owner not in hidden],
            returns,
            next((e for current, e in frameworks if current not in hidden), None),
        )

    def _indexer_walk(
        self, owner: str, bindings: Mapping[str, str]
    ) -> tuple[list[tuple[str, Mapping[str, str]]], dict[str, list[str]]]:
        # The types an index into `owner` looks up indexers in, nearest
        # first, each with the type arguments its parameters are bound to,
        # and the bases each one passes them on to (`_indexer_bases`).
        walk: list[tuple[str, Mapping[str, str]]] = []
        bases_of: dict[str, list[str]] = {}
        pending: list[tuple[str, Mapping[str, str]]] = [(owner, bindings)]
        while pending:
            current, bound = pending.pop(0)
            if current in bases_of:
                continue
            walk.append((current, bound))
            bases_of[current] = self._indexer_bases(current)
            pending.extend(
                (
                    base,
                    bindings_for_base(
                        self.csharp_generic_shapes,
                        self.csharp_class_generic_arity,
                        current,
                        dict(bound),
                        base,
                        self.scope_of(current),
                    ),
                )
                for base in bases_of[current]
            )
        return walk, bases_of

    def _indexer_level(
        self,
        declaring: str,
        bindings: Mapping[str, str],
        index: _Index,
        returns: dict[str, str | None],
    ) -> list[Scored]:
        # `declaring`'s indexers the index may bind, scored, each keyed in
        # `returns` to the type it returns read through `bindings`.
        shape = self.csharp_generic_shapes.get(declaring)
        if shape is None or not shape.indexers:
            return []
        scope = self.scope_of(declaring)
        open_names = frozenset(shape.parameters) - bindings.keys()
        level: list[Scored] = []
        for position, (returned, taken) in enumerate(_indexer_shapes(shape.indexers)):
            key = f"{declaring}{cs.CSHARP_STORED_PAIR_SEPARATOR}{position}"
            result = declared_type(returned, scope, bindings)
            returns[key] = (
                None
                if mentioned_names(result) & open_names
                else self._first_party_type(result) or result
            )
            level.extend(
                scored._replace(owner=declaring)
                for scored in self._indexer_scores(
                    key, taken, index, (scope, bindings, open_names)
                )
                if None not in scored.fits
            )
        return level

    def _indexer_scores(
        self,
        key: str,
        shape: CSharpCallShape,
        index: _Index,
        reading: tuple[Scope | None, Mapping[str, str], frozenset[str]],
    ) -> list[Scored]:
        # One indexer scored in each form its index binds it in, as `_scores`
        # scores a method: by name, expanded into a `params` tail, leaving
        # defaults out, each argument passed in its mode. One whose
        # parameters are not known by name (an older graph's) takes a named
        # index as not known.
        arguments, site, in_scope = index
        scope, bindings, open_names = reading
        types = list(shape.types)
        if len(shape.names) != len(types) and any(site.names):
            return [
                Scored(
                    key, [Fit.UNKNOWN] * len(arguments), [""] * len(arguments)
                )._replace(settled=False)
            ]
        judge = _ScopedJudge(self._identical, self._converts, self._names_enum)
        declared = [canonical_type(t, scope) for t in types]
        names = site.names or (None,) * len(arguments)
        scored: list[Scored] = []
        for expanded in (False, True) if shape.variadic else (False,):
            slots = _slots(shape, len(types), names, expanded)
            if slots is None:
                continue
            form = _form_of(types, slots, expanded, shape.variadic)
            seen = [declared_type(p, scope, bindings) for p in form.parameters]
            fits = [
                _to_type_parameter(fit(p, a, open_names, judge), p, a, in_scope)
                for p, a in zip(seen, arguments, strict=True)
            ]
            scored.append(
                Scored(
                    key,
                    _passed_in_modes(
                        fits, form, (shape.modes, site.modes), len(types) - 1
                    ),
                    seen,
                    expanded=expanded,
                    declared=[declared[i] for i in slots],
                    type_parameters=scope.type_parameters if scope else frozenset(),
                    omits=form.omits,
                    count=len(types),
                    open=open_names,
                )
            )
        return scored

    def _indexer_bases(self, type_qn: str) -> list[str]:
        # The bases whose indexers a type's index may bind: a class's base
        # classes, an interface's base interfaces (C# looks a class type's
        # members up in its classes alone, §12.5).
        interface = self.function_registry.get(type_qn) == NodeType.INTERFACE
        return [
            base
            for base in self._written_bases(type_qn)
            if (self.function_registry.get(base) == NodeType.INTERFACE) == interface
        ]

    def _picked_index(
        self,
        picks: list[Scored],
        returns: Mapping[str, str | None],
        framework: str | None,
    ) -> str | None:
        # The type the best indexer returns, where one is known to take the
        # index; the framework collection's element where no indexer of its
        # own may take it; None otherwise.
        if not picks:
            return framework
        best = best_candidates(picks, self._better_target)
        if framework is not None or not best:
            return None
        if not all(certainly_applies(scored) for scored in best):
            return None
        results = {returns[scored.qn] for scored in best}
        return results.pop() if len(results) == 1 else None

    def _written_bases(self, type_qn: str) -> list[str]:
        # The first-party types a type's base list names, each resolved as
        # C# resolves it in the type's own file: a namesake elsewhere that
        # the file cannot see is not a base.
        shape = self.csharp_generic_shapes.get(type_qn)
        module_qn = self.csharp_class_owner_module.get(type_qn)
        if shape is None or module_qn is None or not shape.base_types:
            return list(self.class_inheritance.get(type_qn, []))
        resolved = (
            self._resolve_in(
                module_qn,
                strip_generic_arguments(written),
                len(type_arguments(written)),
            )
            for written in shape.base_types
        )
        return [base for base in resolved if base is not None]

    def _framework_base_element(
        self, type_qn: str, bindings: Mapping[str, str]
    ) -> str | None:
        # The element a framework collection a first-party type derives
        # from indexes to (`MyList : List<Widget>`), read in the type's file
        # with its own type parameters bound.
        shape = self.csharp_generic_shapes.get(type_qn)
        module_qn = self.csharp_class_owner_module.get(type_qn)
        if shape is None or module_qn is None:
            return None
        scope = self.scope_of(type_qn)
        for written in shape.base_types:
            name = strip_generic_arguments(written)
            arguments = type_arguments(written)
            if self._resolve_in(module_qn, name, len(arguments)) is not None:
                continue
            element = self._framework_element(name, arguments, module_qn)
            if element is not None:
                result = declared_type(element, scope, bindings)
                if mentioned_names(result) & (set(shape.parameters) - bindings.keys()):
                    return None
                return self._first_party_type(result) or result
        return None

    def _local_result_type(
        self,
        identifier: Node,
        local_var_types: dict[str, str],
        module_qn: str,
        caller_qn: str | None,
    ) -> str | None:
        # A `var` local initialized by a call, which the variable map leaves
        # untyped: the type the method it calls is declared to return, where
        # the call resolves (Greptile review of PR 2814). None otherwise.
        return self._call_result(
            self._invocation_return_type_name,
            identifier,
            local_var_types,
            (module_qn, caller_qn),
        )

    def _call_result[T](
        self,
        typed: Callable[[Node, dict[str, str], str, str | None], T | None],
        identifier: Node,
        local_var_types: dict[str, str],
        context: tuple[str, str | None],
    ) -> T | None:
        # `typed` of the call a `var` local `identifier` reads is initialized
        # with, once at a time: a local named in its own initializer (invalid
        # C#, but parsed) does not resolve that call again.
        name = safe_decode_text(identifier)
        if not name or name in local_var_types:
            return None
        initializer = self._var_initializer(identifier, name)
        if (
            initializer is None
            or initializer.type != cs.TS_CSHARP_INVOCATION_EXPRESSION
        ):
            return None
        span = (initializer.start_byte, initializer.end_byte)
        if span in self._typing_initializers:
            return None
        self._typing_initializers.add(span)
        try:
            if not self._one_result(initializer, local_var_types, *context):
                return None
            return typed(initializer, local_var_types, *context)
        finally:
            self._typing_initializers.discard(span)

    def _one_result(
        self,
        call: Node,
        local_var_types: dict[str, str],
        module_qn: str,
        caller_qn: str | None,
    ) -> bool:
        # Whether a call returns one type: it binds one method, or every
        # overload its arguments leave open returns the same type. Where two
        # open ones return two types, either may run (Greptile review of PR
        # 2814), so the call's result is of no known type.
        inner = self.resolve_csharp_method_call(
            call, local_var_types, module_qn, caller_qn
        )
        if inner is None:
            return False
        family = self.csharp_member_overload_family(call, module_qn, caller_qn)
        returns = {self._declared_return(qn) for qn in (inner[1], *family)}
        return len(returns) == 1

    def _result_qn(
        self,
        call: Node,
        local_var_types: dict[str, str],
        module_qn: str,
        caller_qn: str | None,
    ) -> str | None:
        # The first-party type a call returns, its declared return type
        # resolved where its method is declared (Greptile review of PR 2814):
        # `L.Factory.Make()` returns L's `Output` whatever the caller's file
        # calls `Output`. Read in the caller's file, as a chained call reads
        # it, only when the method's file is not known.
        found = self._declared_result(call, local_var_types, module_qn, caller_qn)
        if found is None:
            return None
        canonical, scoped = found
        if scoped:
            return self._first_party_type(canonical)
        rtype, rarity = split_type_ref(canonical)
        return self._type_name_to_qn(rtype, module_qn, rarity)

    def _declared_result(
        self,
        call: Node,
        local_var_types: dict[str, str],
        module_qn: str,
        caller_qn: str | None,
    ) -> tuple[str, bool] | None:
        # A call's declared return type, resolved where its method is
        # declared (`canonical_type`) and True; or, that file not known, as
        # the return type is recorded (`Box`1`) and False. None when the call
        # resolves to no method of a recorded return type.
        inner = self.resolve_csharp_method_call(
            call, local_var_types, module_qn, caller_qn
        )
        return self._declared_return(inner[1]) if inner else None

    def _declared_return(self, method_qn: str) -> tuple[str, bool] | None:
        # A method's return type as written, its shape and type arguments in
        # (`Widget[]`, `List<Widget>`), resolved where it is declared and
        # True; or, that file not known, as recorded (`Box`1`) and False.
        # None for a method of no return type known.
        shape = self.csharp_call_shapes.get(method_qn)
        written = signature_type_name(shape.returns) if shape and shape.returns else ""
        owner, _member = qn_markers.owner_and_member(method_qn)
        scope = self.scope_of(owner, self._method_type_parameters(method_qn))
        if scope is not None and written:
            return canonical_type(written, scope), True
        entry = self.csharp_method_return_types.get(method_qn)
        if entry is None:
            return None
        rtype, rarity = entry
        if scope is None:
            return (f"{rtype}`{rarity}" if rarity else rtype), False
        return canonical_type(rtype, scope, rarity), True

    def _result_argument(
        self,
        call: Node,
        local_var_types: dict[str, str],
        module_qn: str,
        caller_qn: str | None,
    ) -> ArgumentType | _Unread | None:
        # What a call-initialized local passes: its call's first-party result
        # type, resolved where the method is declared, with the type
        # arguments the call bound in (`Make<T>(T)` called with 1 returns a
        # `Box<int>`). `_UNREAD` for a result naming a type parameter the
        # call does not bind for certain, whose type is not known (Greptile
        # review of PR 2814), for overloads the call may bind that return
        # different types, and for a name its own file leaves unresolved that
        # the caller's file would read as one of its types; None for any
        # other, which is read by name.
        inner = self.resolve_csharp_method_call(
            call, local_var_types, module_qn, caller_qn
        )
        if inner is None:
            return None
        key = self._call_key(call, module_qn, caller_qn)
        results = {
            self._bound_return(qn, self._call_picks.get(key, {}).get(qn))
            for qn in (inner[1], *self._overload_families.get(key, []))
        }
        if len(results) != 1:
            return _UNREAD
        (canonical,) = results
        if canonical is None or isinstance(canonical, _Unread):
            return canonical
        # A name the method's own file leaves unresolved (external, or not
        # one type there) must not be read as the caller's file reads it.
        if any(
            predefined_keyword(name) is None
            and self._type_name_to_qn(name, module_qn) is not None
            for name in mentioned_names(canonical)
        ):
            return _UNREAD
        return _written_argument(canonical)

    def _bound_return(
        self, method_qn: str, pick: Scored | None
    ) -> str | _Unread | None:
        # A method's declared return type with the type arguments a call
        # bound in: those its receiver passes and those the ranking inferred
        # for it, settled. `_UNREAD` when one it names is still unbound;
        # None when the return type is not known where it is declared.
        found = self._declared_return(method_qn)
        if found is None or not found[1]:
            return None
        canonical = found[0]
        owner, _member = qn_markers.owner_and_member(method_qn)
        scope = self.scope_of(owner, self._method_type_parameters(method_qn))
        names = (scope.type_parameters if scope else frozenset()) | (
            self._method_type_parameters(method_qn)
        )
        if pick is not None and pick.settled:
            bound = dict(pick.binds)
            canonical = substitute(canonical, bound)
            names = names - bound.keys()
        if names & mentioned_names(canonical):
            return _UNREAD
        return canonical

    def _var_initializer(self, identifier: Node, name: str) -> Node | None:
        # The initializer of the `var` local `name` an identifier reads,
        # searched outward as `_written_receiver_type` searches; None when
        # the name is a parameter or a typed local, or no block declares it.
        current = identifier.parent
        while current is not None and current.type != cs.TS_CSHARP_DECLARATION_LIST:
            if current.type == cs.TS_CSHARP_BLOCK and (
                found := self._block_declaration(current, name)
            ):
                _statement, decl, declarator = found
                implicit = decl.child_by_field_name(cs.FIELD_TYPE)
                if implicit is None or implicit.type != cs.TS_CSHARP_IMPLICIT_TYPE:
                    return None
                return _initializer(declarator)
            if self._parameter_type(current, name):
                return None
            current = current.parent
        return None

    def _annotated_type_field(self, receiver: Node) -> str | None:
        type_node = receiver.child_by_field_name(cs.FIELD_TYPE)
        raw = safe_decode_text(type_node) if type_node else None
        return annotate_type_ref(raw) if raw else None

    def _invocation_return_type_name(
        self,
        receiver: Node,
        local_var_types: dict[str, str],
        module_qn: str,
        caller_qn: str | None,
    ) -> str | None:
        inner = self.resolve_csharp_method_call(
            receiver, local_var_types, module_qn, caller_qn
        )
        if inner is None:
            return None
        if entry := self.csharp_method_return_types.get(inner[1]):
            rname, rarity = entry
            return f"{rname}`{rarity}" if rarity else rname
        return None

    def _unwrap_receiver(self, receiver: Node) -> Node | None:
        # Peel interleaved parens and null-forgiving postfix wrappers
        # (`((Component)s)!` puts the `!` OUTSIDE the parens) to a fixpoint.
        while receiver.type in (
            cs.TS_PARENTHESIZED_EXPRESSION,
            cs.TS_CSHARP_POSTFIX_UNARY_EXPRESSION,
        ):
            inner = receiver.named_children[0] if receiver.named_children else None
            if inner is None:
                return None
            receiver = inner
        return receiver

    def _this_receiver_type(self, module_qn: str, caller_qn: str | None) -> str | None:
        qn = self._containing_class_qn(caller_qn)
        if qn is None:
            return None
        # `this` names the exact containing class, so keep its
        # namespace-qualified form (`N1.Widget`) rather than the bare simple
        # name: that lets the matcher bind an exact `this N1.Widget` extension
        # even when another `N2.Widget` exists. The form is recorded at
        # ingest from the declaration, because a namespace the module's
        # directory spells is no longer in the qn (issue #1629); the
        # prefix-strip below is the fallback for a class ingested without it.
        if (namespaced := self.csharp_class_namespaced.get(qn)) is not None:
            return namespaced
        if qn.startswith(f"{module_qn}{cs.SEPARATOR_DOT}"):
            return qn[len(module_qn) + 1 :]
        return qn.rsplit(cs.SEPARATOR_DOT, 1)[-1]

    def _this_field_receiver_type(
        self, receiver: Node, caller_qn: str | None
    ) -> str | None:
        expr = receiver.child_by_field_name(cs.TS_CSHARP_FIELD_EXPRESSION)
        field = safe_decode_text(receiver.child_by_field_name(cs.FIELD_NAME))
        if expr is not None and expr.type == cs.TS_CSHARP_THIS and field:
            if class_qn := self._containing_class_qn(caller_qn):
                return self._field_type(class_qn, field)
        return None

    def _identifier_receiver_type(
        self, receiver: Node, local_var_types: dict[str, str], caller_qn: str | None
    ) -> str | None:
        name = safe_decode_text(receiver)
        if not name:
            return None
        if (type_name := local_var_types.get(name)) is not None:
            return type_name
        if class_qn := self._containing_class_qn(caller_qn):
            if ftype := self._field_type(class_qn, name):
                return ftype
        # An unknown bare identifier is a TYPE name (a static call
        # `Widget.M()`), not an instance; extension methods bind on instances
        # only, so do NOT treat it as an extension receiver (else `Widget.Poke()`
        # would wrongly bind `static Poke(this Widget)`, invalid in C#).
        return None

    def _receiver_type_arity(
        self,
        receiver: Node,
        local_var_types: dict[str, str],
        module_qn: str,
        caller_qn: str | None,
    ) -> int | None:
        # The receiver's WRITTEN generic arity where it is knowable
        # (object-creation/cast text, a chained call's recorded return, or
        # `this` inside a generic type); None when unknowable (an untyped
        # identifier), which skips the arity gate rather than guessing.
        unwrapped = self._unwrap_receiver(receiver)
        if unwrapped is None:
            return None
        receiver = unwrapped
        if receiver.type == cs.TS_CSHARP_OBJECT_CREATION_EXPRESSION or (
            receiver.type == cs.TS_CSHARP_CAST_EXPRESSION
            and not _reads_as_sum(receiver)
        ):
            type_node = receiver.child_by_field_name(cs.FIELD_TYPE)
            raw = safe_decode_text(type_node) if type_node else None
            return generic_arity_of_type_text(raw) if raw else None
        if receiver.type == cs.TS_CSHARP_INVOCATION_EXPRESSION:
            return self._invocation_return_arity(
                receiver, local_var_types, module_qn, caller_qn
            )
        if receiver.type == cs.TS_CSHARP_THIS:
            class_qn = self._containing_class_qn(caller_qn)
            if class_qn is not None:
                return self.csharp_class_generic_arity.get(class_qn, 0)
        return None

    def _invocation_return_arity(
        self,
        invocation: Node,
        local_var_types: dict[str, str],
        module_qn: str,
        caller_qn: str | None,
    ) -> int | None:
        # Full caller context: locals and imports participate in the inner
        # resolution exactly as they did for the instance path.
        inner = self.resolve_csharp_method_call(
            invocation, local_var_types, module_qn, caller_qn
        )
        if inner is not None and (
            entry := self.csharp_method_return_types.get(inner[1])
        ):
            return entry[1]
        return None

    def _registered_type_declares(self, type_name: str, method_name: str) -> bool:
        # A registered type merely SHARING the receiver type's simple name
        # (Polly's Snippets.Docs.RateLimiter demo class vs BCL RateLimiter)
        # must not defeat the external gate: the candidate counts only if
        # it actually DECLARES the called member somewhere in its
        # parts/bases.
        simple = split_type_ref(type_name)[0].rsplit(cs.SEPARATOR_DOT, 1)[-1]
        for qn in self.simple_name_lookup.get(simple, set()):
            if self.function_registry.get(qn) in _TYPE_DECLS:
                if self._find_name_across_parts(qn, method_name) is not None:
                    return True
        return False

    def _extension_candidates(
        self, receiver: _ExtendedReceiver, method_name: str, arg_count: int
    ) -> list[tuple[str, Fit]]:
        # The extension methods of the name whose `this` type takes the
        # receiver, and how (`_receiving`), that take `arg_count` arguments
        # after it.
        candidates = self.csharp_extension_methods.get(method_name)
        if not candidates:
            return []
        recv_simple = receiver.name.rsplit(cs.SEPARATOR_DOT, 1)[-1]
        recv_qualified = cs.SEPARATOR_DOT in receiver.name
        # An UNqualified receiver whose simple name maps to more than one
        # registered first-party type (`N1.Widget` vs `N2.Widget`) is
        # genuinely ambiguous, since we can't tell which one it is, so an
        # unqualified-vs-unqualified match must not guess. A qualified receiver
        # or a BCL name (not registered) is not affected.
        ambiguous_unqualified = not recv_qualified and self._same_arity_type_twins(
            recv_simple
        )
        found: list[tuple[str, Fit]] = []
        for entry in candidates:
            if not self._takes_after_receiver(entry[0], arg_count):
                continue
            receiving = self._receiving(receiver, entry, ambiguous_unqualified)
            if receiving is not None:
                found.append((entry[0], receiving))
        return found

    def _receiving(
        self,
        receiver: _ExtendedReceiver,
        entry: tuple[str, str, str, int],
        ambiguous_unqualified: bool,
    ) -> Fit | None:
        # How an extension's `this` parameter takes the receiver, which C#
        # converts to it only by identity, a reference or a boxing
        # conversion (PR 2814's Roslyn oracle): the receiver's own type is
        # an identity (one of no type arguments; with them, of no known
        # closing), `object` a conversion, a type parameter of the method an
        # inference. A first-party class takes its first-party bases, and an
        # external type only through a base the index does not hold; a
        # predefined value or an external type takes no first-party or other
        # predefined type. An external `this` type such a receiver may
        # implement is not known, and never ruled out.
        qn, recv_type, _namespace, arity = entry
        written = strip_generic_arguments(recv_type)
        if written in self._method_type_parameters(qn):
            return Fit.UNKNOWN
        keyword = predefined_keyword(written)
        if keyword in (cs.CSHARP_OBJECT_KEYWORD, cs.CSHARP_DYNAMIC_KEYWORD):
            return Fit.CONVERTIBLE
        if self._extends(receiver, entry, ambiguous_unqualified):
            return (
                self._closed_receiving(receiver, entry, Fit.EXACT)
                if arity
                else Fit.EXACT
            )
        given = predefined_keyword(receiver.name)
        if given is not None and keyword is not None:
            return Fit.EXACT if keyword == given else None
        if receiver.qn is not None and keyword is None:
            return self._class_receiving(receiver, entry)
        # A predefined value or an external type converts to no first-party
        # or other predefined type, and maybe to an external one.
        if keyword is not None or self._extended_type(qn, recv_type, arity):
            return None
        return Fit.UNKNOWN

    def _class_receiving(
        self, receiver: _ExtendedReceiver, entry: tuple[str, str, str, int]
    ) -> Fit | None:
        # How a first-party receiver converts to a `this` type its own name
        # does not match: by reference or boxing up its bases and
        # interfaces, closed as written (`_closed_receiving`); where the
        # `this` type is not read, a first-party base by its name, and
        # anything else only through a base the index does not hold.
        qn, recv_type, _namespace, arity = entry
        if self._this_type(qn) is not None:
            # `_extends` judged a non-generic `this` type's identity by its
            # name; a generic one's it cannot spell, so its own closing counts.
            return self._closed_receiving(
                receiver, entry, Fit.CONVERTIBLE, bases_only=not arity
            )
        receiver_qn = receiver.qn or ""
        ancestry = self._class_ancestry(receiver_qn)
        bases = ancestry - set(self._partial_roots(receiver_qn))
        if self._extended_type(qn, recv_type, arity) in bases:
            return Fit.UNKNOWN if arity else Fit.CONVERTIBLE
        return None if self._all_indexed(ancestry) else Fit.UNKNOWN

    def _closed_receiving(
        self,
        receiver: _ExtendedReceiver,
        entry: tuple[str, str, str, int],
        taken: Fit,
        bases_only: bool = False,
    ) -> Fit | None:
        # How a first-party receiver takes a `this` type closed as the
        # extension writes it (Greptile review of PR 2814: a `Two :
        # IBox<int>, IBox<string>` is an `IBox<string>`; a receiver `where T
        # : IBox<int>` is none): `taken` when it converts, None when it
        # cannot, unknown when that is not known.
        target = self._this_type(entry[0])
        if target is None or receiver.qn is None:
            return Fit.UNKNOWN
        given = (
            canonical_type(receiver.name, receiver.scope)
            if receiver.scope is not None
            else strip_generic_arguments(receiver.name)
        )
        verdict = self._constructed_converts(
            (given, receiver.qn),
            target,
            self._method_type_parameters(entry[0]),
            bases_only,
        )
        if verdict is None:
            return Fit.UNKNOWN
        return taken if verdict else None

    def _this_type(self, method_qn: str) -> tuple[str, str] | None:
        # The first-party type an extension method's `this` parameter is,
        # closed as written and resolved where the method is declared, with
        # its qn; None when it is not one, or not read.
        scope = self.scope_of(
            _member_type(method_qn), self._method_type_parameters(method_qn)
        )
        types = self._ranked_types(method_qn)
        if scope is None or not types:
            return None
        written = canonical_type(types[0], scope)
        qn = resolved_qn(written)
        return (written, qn) if qn is not None else None

    def _takes_after_receiver(self, method_qn: str, arg_count: int) -> bool:
        # Whether an extension method takes `arg_count` arguments after its
        # receiver. `_arity` reads the first `(`/last `)`, so it is passed the
        # whole qn; a leaf-split on `.` would land inside a qualified type.
        return _arity(method_qn) == arg_count + 1 or _accepts_arg_count(
            method_qn, arg_count + 1, self.csharp_call_shapes.get(method_qn)
        )

    def _extends(
        self,
        receiver: _ExtendedReceiver,
        entry: tuple[str, str, str, int],
        ambiguous_unqualified: bool,
    ) -> bool:
        # Whether an extension method's `this` type is the receiver's.
        qn, recv_type, ext_namespace, cand_recv_arity = entry
        # Both unqualified, a receiver whose class is known binds an
        # extension whose `this` type is that class where the extension is
        # declared, however many first-party types share its name (PR 2814's
        # oracle).
        # A receiver typed by its qn (an operator's result) is that class.
        unqualified = cs.SEPARATOR_DOT not in receiver.name + recv_type
        if receiver.qn is not None and (unqualified or receiver.name == receiver.qn):
            extended = self._extended_type(qn, recv_type, cand_recv_arity)
            if extended is not None:
                return extended == receiver.qn
        # A receiver of KNOWN written arity never binds an extension
        # declared for the other generic twin (`new Builder<int>()` cannot
        # take a `this Builder` extension).
        if receiver.arity is not None and cand_recv_arity != receiver.arity:
            return False
        recv_simple = receiver.name.rsplit(cs.SEPARATOR_DOT, 1)[-1]
        if recv_type.rsplit(cs.SEPARATOR_DOT, 1)[-1] != recv_simple:
            return False
        return _extension_receiver_matches(
            receiver.name, recv_type, ext_namespace, ambiguous_unqualified
        )

    def _extended_type(self, method_qn: str, written: str, arity: int) -> str | None:
        # The first-party type an extension method's `this` parameter names,
        # resolved where the method is declared; None when it names none.
        scope = self.scope_of(
            _member_type(method_qn), self._method_type_parameters(method_qn)
        )
        if scope is None:
            return None
        return resolved_qn(canonical_type(written, scope, arity))

    def _count_arguments(self, call_node: Node) -> int:
        arg_list = call_node.child_by_field_name(cs.FIELD_ARGUMENTS)
        if arg_list is None:
            return 0
        return sum(1 for c in arg_list.children if c.type == cs.TS_CSHARP_ARGUMENT)

    def _resolve_receiver_class_qn(
        self,
        receiver: Node,
        local_var_types: dict[str, str],
        module_qn: str,
        caller_qn: str | None,
    ) -> str | None:
        # A cast receiver `((Component)s!).Reload()` (Polly's
        # CancellationToken.Register callback): the cast TYPE is the
        # receiver's type by construction (mirrors the Java cast-receiver
        # handling).
        unwrapped = self._unwrap_receiver(receiver)
        if unwrapped is None:
            return None
        receiver = unwrapped
        if receiver.type == cs.TS_CSHARP_CAST_EXPRESSION:
            if _reads_as_sum(receiver):
                return None
            return self._cast_receiver_qn(receiver, module_qn)
        # `new Builder().Add()`: an object-creation receiver IS its type.
        if receiver.type == cs.TS_CSHARP_OBJECT_CREATION_EXPRESSION:
            return self._object_creation_receiver_qn(receiver, module_qn)
        # `Policy.Handle<T>().Wrap(...)`: an invocation receiver types the
        # next hop via the resolved inner call's recorded return type
        # (Polly's whole fluent surface). Depth is bounded by chain length.
        if receiver.type == cs.TS_CSHARP_INVOCATION_EXPRESSION:
            return self._invocation_receiver_qn(
                receiver, local_var_types, module_qn, caller_qn
            )
        if receiver.type == cs.TS_CSHARP_THIS:
            return self._containing_class_qn(caller_qn)
        # An explicit `this.field` receiver: the field's (possibly inherited)
        # type on the enclosing class.
        if receiver.type == cs.TS_CSHARP_MEMBER_ACCESS_EXPRESSION:
            return self._member_access_receiver_qn(
                receiver, local_var_types, module_qn, caller_qn
            )
        if receiver.type == cs.TS_CSHARP_ALIAS_QUALIFIED_NAME:
            if raw := safe_decode_text(receiver):
                return self._qualified_type_name_to_qn(raw, module_qn)
            return None
        if receiver.type == cs.TS_CSHARP_IDENTIFIER:
            return self._identifier_receiver_qn(
                receiver, local_var_types, module_qn, caller_qn
            )
        # `PB<int>.Make()`: a generic name in receiver position is a type, and
        # its written arity picks it from a same-named twin (issue #2579).
        if receiver.type == cs.TS_CSHARP_GENERIC_NAME:
            return self._written_type_qn(receiver, module_qn)
        return None

    def _cast_receiver_qn(self, receiver: Node, module_qn: str) -> str | None:
        type_node = receiver.child_by_field_name(cs.FIELD_TYPE)
        return self._written_type_qn(type_node, module_qn) if type_node else None

    def _written_type_qn(self, type_node: Node, module_qn: str) -> str | None:
        raw = safe_decode_text(type_node)
        if raw:
            # The WRITTEN arity picks between simple-name twins (`(Opt<int>)o`
            # names the generic Opt<T>, never plain Opt).
            return self._type_name_to_qn(
                _normalize_type_name(raw),
                module_qn,
                generic_arity_of_type_text(raw),
            )
        return None

    def _object_creation_receiver_qn(
        self, receiver: Node, module_qn: str
    ) -> str | None:
        type_node = receiver.child_by_field_name(cs.FIELD_TYPE)
        if type_text := safe_decode_text(type_node) if type_node else None:
            return self._type_name_to_qn(
                _normalize_type_name(type_text),
                module_qn,
                generic_arity_of_type_text(type_text),
            )
        return None

    def _invocation_receiver_qn(
        self,
        receiver: Node,
        local_var_types: dict[str, str],
        module_qn: str,
        caller_qn: str | None,
    ) -> str | None:
        inner = self.resolve_csharp_method_call(
            receiver, local_var_types, module_qn, caller_qn
        )
        if inner is None:
            return None
        if entry := self.csharp_method_return_types.get(inner[1]):
            rtype, rarity = entry
            return self._type_name_to_qn(rtype, module_qn, rarity)
        return None

    def _member_access_receiver_qn(
        self,
        receiver: Node,
        local_var_types: dict[str, str],
        module_qn: str,
        caller_qn: str | None,
    ) -> str | None:
        expr = receiver.child_by_field_name(cs.TS_CSHARP_FIELD_EXPRESSION)
        field = safe_decode_text(receiver.child_by_field_name(cs.FIELD_NAME))
        if expr is not None and expr.type == cs.TS_CSHARP_THIS and field:
            if class_qn := self._containing_class_qn(caller_qn):
                if ftype := self._field_type(class_qn, field):
                    return self._type_name_to_qn(ftype, module_qn)
        raw = safe_decode_text(receiver)
        return (
            self._dotted_receiver_qn(raw, local_var_types, module_qn) if raw else None
        )

    def _dotted_receiver_qn(
        self, raw: str, local_var_types: dict[str, str], module_qn: str
    ) -> str | None:
        # `local.Field`, a qualified type name, or `Holder.Field`: a static
        # field of a type the head names.
        head, separator, tail = raw.partition(cs.SEPARATOR_DOT)
        chained = bool(separator) and cs.SEPARATOR_DOUBLE_COLON not in raw
        if chained and head in local_var_types:
            return self._field_chain_qn(local_var_types[head], tail, module_qn)
        if qualified := self._qualified_type_name_to_qn(raw, module_qn):
            return qualified
        return self._field_chain_qn(head, tail, module_qn) if chained else None

    def _field_chain_qn(self, head_type: str, tail: str, module_qn: str) -> str | None:
        current_qn = self._type_name_to_qn(head_type, module_qn)
        for field_name in tail.split(cs.SEPARATOR_DOT):
            if current_qn is None:
                break
            field_type = self._field_type(current_qn, field_name)
            if field_type is None:
                current_qn = None
                break
            current_qn = self._type_name_to_qn(field_type, module_qn)
        return current_qn

    def _identifier_receiver_qn(
        self,
        receiver: Node,
        local_var_types: dict[str, str],
        module_qn: str,
        caller_qn: str | None,
    ) -> str | None:
        name = safe_decode_text(receiver)
        if not name:
            return None
        # A local/parameter of a known type resolves via its type; else a
        # bare (possibly inherited) field of the enclosing class; else the
        # receiver may itself be a type name (a static call `Foo.Bar()`).
        type_name = local_var_types.get(name)
        if type_name is not None:
            return self._type_at(type_name, receiver, module_qn)
        # A `var` local a call initializes is of the type the call returns,
        # as a chained call's receiver is (Greptile review of PR 2814).
        if result := self._call_result(
            self._result_qn,
            receiver,
            local_var_types,
            (module_qn, caller_qn),
        ):
            return result
        # A `foreach (var x in ...)` binding is a LOCAL whose type comes
        # from the sequence and is not inferred, so it never reaches
        # `local_var_types`. Falling through to the static branch below
        # read the name as a TYPE: `foreach (var Config in items) {
        # Config.Each(); }` emitted an edge to the registered class
        # `N.Config` (Copilot, PR #1998). A local owns the name here
        # whatever its type, so the receiver is unresolved, not static.
        if self._binds_local(receiver, name):
            return None
        if class_qn := self._containing_class_qn(caller_qn):
            if ftype := self._field_type(class_qn, name):
                return self._type_at(ftype, receiver, module_qn)
        return self._type_name_to_qn(name, module_qn)

    def _qualified_type_name_to_qn(
        self,
        type_name: str,
        module_qn: str,
        generic_arity: int | None = None,
    ) -> str | None:
        type_name, annotated_arity = split_type_ref(type_name)
        if generic_arity is None:
            generic_arity = annotated_arity or generic_arity_of_type_text(type_name)
        type_name = _normalize_type_name(type_name)
        expanded = type_name.replace("::", cs.SEPARATOR_DOT)
        global_prefix = f"global{cs.SEPARATOR_DOT}"
        is_global = expanded.startswith(global_prefix)
        if is_global:
            expanded = expanded[len(global_prefix) :]
        import_map = self.import_processor.csharp_usings(module_qn)
        first, separator, rest = expanded.partition(cs.SEPARATOR_DOT)
        if not is_global and import_map and (mapped := import_map.get(first)):
            expanded = f"{mapped}{separator}{rest}" if separator else mapped

        if self.function_registry.get(expanded) in _TYPE_DECLS:
            return self.arity_twin(expanded, generic_arity)
        # The written path names the DECLARED form (`Zeta.Widget`), which a
        # folded qn no longer ends with, so it is looked up in the
        # declared-form index; the leading alias was expanded above, so
        # `Z.Widget` under `using Z = Zeta;` lands here as `Zeta.Widget`
        # (issues #2000, #2004).
        # `Widget` and `Widget<T>` share the declared form, so the written
        # arity picks between them first (bot review).
        carriers = self.csharp_namespaced_qns.get(expanded)
        if carriers and len(carriers) > 1 and generic_arity is not None:
            carriers = {
                qn
                for qn in carriers
                if self.csharp_class_generic_arity.get(qn, 0) == generic_arity
            } or carriers
        if declared := unique_carrier(carriers, self.csharp_partial_groups):
            return declared
        leaf = expanded.rsplit(cs.SEPARATOR_DOT, 1)[-1]
        candidates = [
            qn
            for spelled in arity_spellings(leaf, generic_arity)
            for qn in self.simple_name_lookup.get(spelled, set())
            if self.function_registry.get(qn) in _TYPE_DECLS
            and self._csharp_qualified_qn_matches(qn, expanded, module_qn)
        ]
        return self._disambiguate_type_candidates(candidates, generic_arity, module_qn)

    def _csharp_qualified_qn_matches(
        self, candidate_qn: str, expanded: str, module_qn: str
    ) -> bool:
        """Accept a complete type path rooted at a known repository module."""
        # The written path never spells a generic twin's CLR arity.
        natural_qn = qn_markers.strip_arity_marker(qn_markers.natural_qn(candidate_qn))
        if natural_qn == expanded:
            return True
        suffix = f"{cs.SEPARATOR_DOT}{expanded}"
        if not natural_qn.endswith(suffix):
            return False
        prefix = natural_qn[: -len(suffix)]
        return prefix in self.module_qn_to_file_path or prefix in (
            self.project_name,
            module_qn,
        )

    def _containing_class_qn(self, caller_qn: str | None) -> str | None:
        if not caller_qn:
            return None
        # Strip any parameter signature before splitting off the method leaf,
        # so a qualified param type (`M(System.String)`) does not fool rsplit.
        base = caller_qn.split(cs.CHAR_PAREN_OPEN, 1)[0]
        class_qn = base.rsplit(cs.SEPARATOR_DOT, 1)[0]
        return class_qn if self.function_registry.get(class_qn) in _TYPE_DECLS else None

    def _type_name_to_qn(
        self,
        type_name: str,
        module_qn: str,
        generic_arity: int | None = None,
    ) -> str | None:
        # Stored type refs carry their written generic arity CLR-style
        # (`Options`0` is implicit: plain means arity 0); parse it so twin
        # filtering works for every map-sourced reference.
        if "::" in type_name or cs.SEPARATOR_DOT in type_name:
            return self._qualified_type_name_to_qn(type_name, module_qn, generic_arity)
        if generic_arity is None:
            type_name, generic_arity = split_type_ref(type_name)
        # An already-qualified name that IS a registered type resolves directly,
        # skipping the ambiguous simple-name sweep.
        if self.function_registry.get(type_name) in _TYPE_DECLS:
            return type_name
        simple = type_name.rsplit(cs.SEPARATOR_DOT, 1)[-1]
        import_map = self.import_processor.csharp_usings(module_qn)
        if import_map and (mapped := import_map.get(simple)):
            if self.function_registry.get(mapped) in _TYPE_DECLS:
                return self.arity_twin(mapped, generic_arity)
            # A type alias (`using W = Zeta.Widget;`) maps to a WRITTEN path,
            # not a qn: resolve it as one (issue #2002). An alias takes no
            # type arguments, so the arity is its target's: `using R =
            # N.IRun<int>;` names `IRun<T>`, not a plain `IRun` beside it
            # (PR 2814 review).
            if mapped != simple and (
                aliased := self._qualified_type_name_to_qn(mapped, module_qn)
            ):
                return aliased
        candidates = [
            qn
            for spelled in arity_spellings(simple, generic_arity)
            for qn in self.simple_name_lookup.get(spelled, set())
            if self.function_registry.get(qn) in _TYPE_DECLS
        ]
        return self._disambiguate_type_candidates(candidates, generic_arity, module_qn)

    def arity_index(self) -> CSharpArityIndex:
        return CSharpArityIndex(
            self.function_registry,
            self.csharp_class_generic_arity,
            self.csharp_class_namespaced,
            self.csharp_namespaced_qns,
            self.csharp_partial_groups,
        )

    def arity_twin(self, type_qn: str, generic_arity: int) -> str:
        """The declaration a reference written with `generic_arity` type
        arguments means, given the type its name resolved to (issue #2579)."""
        return self.arity_index().twin(type_qn, generic_arity)

    def _disambiguate_type_candidates(
        self,
        candidates: list[str],
        generic_arity: int | None,
        module_qn: str,
    ) -> str | None:
        # `Builder` vs `Builder<TResult>` share a simple name; when the
        # reference's WRITTEN generic arity is known, keep only the
        # declarations with that type-parameter count (Polly's dual
        # builders, where the ambiguity killed every fluent second hop).
        if generic_arity is not None and len(candidates) > 1:
            arity_matched = [
                qn
                for qn in candidates
                if self.csharp_class_generic_arity.get(qn, 0) == generic_arity
            ]
            if arity_matched:
                candidates = arity_matched
        if len(candidates) == 1:
            return candidates[0]
        if not candidates:
            return None
        # Several candidates that are all parts of ONE partial class are a
        # single logical type, not a real ambiguity; return one part (method
        # resolution then spans the whole group).
        if part := self._single_partial_group_member(candidates):
            return part
        # Prefer a candidate in the calling file's module, then the one C#
        # finds nearest from the file (`_visibility`): a type of the file's
        # own namespace in another file beside a same-named one elsewhere.
        # Ambiguity C# would not resolve is left unresolved, not guessed.
        same_module = [q for q in candidates if q.startswith(f"{module_qn}.")]
        if len(same_module) == 1:
            return same_module[0]
        return self._nearest_visible(candidates, module_qn)

    def _nearest_visible(self, candidates: list[str], module_qn: str) -> str | None:
        # The one candidate C# finds nearest from the file, if it is one.
        levels: dict[str, int] = {}
        for qn in candidates:
            if (level := self._visibility(module_qn, qn)) is None:
                return None
            levels[qn] = level
        nearest = min(levels.values())
        found = [q for q, level in levels.items() if level == nearest]
        return found[0] if nearest != _NOT_VISIBLE and len(found) == 1 else None

    def _same_arity_type_twins(self, simple_name: str) -> bool:
        # An UNqualified receiver whose simple name maps to more than one
        # registered first-party type (`N1.Widget` vs `N2.Widget`) is
        # genuinely ambiguous, since we can't tell which one it is, so an
        # unqualified-vs-unqualified match must not guess. Same-name
        # declarations that all differ by GENERIC ARITY (`Builder` beside
        # `Builder<TResult>`, Polly's dual pipeline builders) are not that
        # namespace ambiguity: a compilable call binds the unique matching
        # extension regardless of which twin the receiver is. Only same-arity
        # twins (true `N1.Widget`/`N2.Widget` namespace splits) stay ambiguous.
        same_name_decls = [
            qn
            for qn in self.simple_name_lookup.get(simple_name, set())
            if self.function_registry.get(qn) in _TYPE_DECLS
        ]
        distinct_arities = {
            self.csharp_class_generic_arity.get(qn, 0) for qn in same_name_decls
        }
        return len(same_name_decls) > 1 and len(distinct_arities) != len(
            same_name_decls
        )

    def _single_partial_group_member(self, candidates: list[str]) -> str | None:
        # If every candidate belongs to the SAME partial-class group, they are
        # one logical type; return its lexicographically-first part (stable
        # across runs). A candidate outside the group (its group is None) means
        # a genuine cross-type ambiguity, left unresolved.
        group = self.csharp_partial_groups.get(candidates[0])
        if group is None:
            return None
        if all(self.csharp_partial_groups.get(c) is group for c in candidates):
            return min(group)
        return None

    # --- overloads by argument type (issue #2619) ------------------------

    def _rank_member_overloads(
        self, call: _MemberCall, class_qn: str | None
    ) -> _Ranking:
        """The overloads a member call may bind, best first.

        Every overload taking the argument count, by its arity, defaults or
        a `params` tail, competes, from every type the receiver reaches,
        ranked as one set. A type holding one known to apply drops every
        method of its ancestors, as C# drops a base's methods; one that may
        or may not apply drops none, and sibling bases (two interfaces)
        drop none of each other's (PR 2814 review). A base's method of the
        parameter types a derived type declares is that one's, overridden
        or hidden. Each type is seen with the type arguments its subclasses
        pass it, so `Validator<T>.Validate(T)` takes a Person on a
        `PersonValidator : Inline<Person>`, and the receiver's own with the
        ones its written type passes (`new Handler<Person>()`).
        """
        if class_qn is None:
            return _Ranking([], False, False)
        # Every type the call site writes (its arguments', its receiver's
        # and its own type arguments) is resolved once, in the caller's file.
        caller = self._caller_scope(call)
        spelled = tuple(canonical_type(a, caller) for a in call.type_arguments)
        hierarchy = self._hierarchy(class_qn)
        views = self._views(
            hierarchy, self._receiver_bindings(call.receiver, class_qn, caller), spelled
        )
        gathering = _Gathering()
        scoring: tuple[list[ArgumentType | None], _CallSite] | None = None
        for declaring in hierarchy.order:
            if declaring in gathering.blocked:
                continue
            view = views(declaring)
            qns = self._candidate_methods(declaring, call, len(spelled))
            kept, stands = self._level(qns, view, (declaring, hierarchy), gathering)
            if not kept:
                continue
            scoring = scoring or self._call_scoring(call, caller)
            self._weigh(kept, stands, (declaring, view, hierarchy), gathering, scoring)
        if scoring is None:
            return _Ranking([], False, False)
        self._weigh_unplaced(gathering, scoring)
        best = best_candidates(
            gathering.picks,
            self._better_target,
            partial(_outranked, hierarchy.ancestors),
        )
        return _Ranking(best, bool(gathering.settled), True)

    def _call_scoring(
        self, call: _MemberCall, caller: Scope
    ) -> tuple[list[ArgumentType | None], _CallSite]:
        # The call's argument types, resolved in the caller's file, and what
        # it writes of them beside their types.
        arguments = [
            _canonical_argument(argument, caller)
            for argument in self._argument_types(
                call.node, call.local_var_types, call.module_qn, call.caller_qn
            )
        ]
        return arguments, _CallSite(
            _argument_names(call.node), _argument_modes(call.node)
        )

    def _weigh(
        self,
        qns: Sequence[str],
        stands: Mapping[str, str],
        where: tuple[str, _ReceiverView, _Hierarchy],
        gathering: _Gathering,
        scoring: tuple[list[ArgumentType | None], _CallSite],
    ) -> None:
        # Scores one type's candidates into the gathering. A virtual's place
        # goes to the override standing for it, which is what runs; how the
        # virtual's types bind says nothing of the override's own. A
        # candidate known to apply drops every method of the type's
        # ancestors.
        declaring, view, hierarchy = where
        arguments, site = scoring
        scored = [
            s._replace(owner=declaring)
            for qn in qns
            for s in self._scores(qn, arguments, view, site)
        ]
        gathering.picks.extend(
            s._replace(qn=stands[s.qn], binds=()) if s.qn in stands else s
            for s in scored
        )
        if any(certainly_applies(s) for s in scored):
            gathering.settled.add(declaring)
            gathering.blocked |= hierarchy.ancestors.get(declaring, frozenset())
            return
        # Read only when a base may still be weighed.
        gathering.declared.extend(
            (declaring, self._signature(qn, view))
            for qn in qns
            if not self._overrides(qn)
        )

    def _weigh_unplaced(
        self,
        gathering: _Gathering,
        scoring: tuple[list[ArgumentType | None], _CallSite],
    ) -> None:
        # An override no virtual method was found for stands in its
        # virtual's place, in a base of its type: one a type holding a
        # candidate known to apply derives from, which drops it, or a base
        # no first-party type is, which every class in the chain derives
        # from. So it is weighed only while no candidate is known to apply.
        if gathering.settled:
            return
        arguments, site = scoring
        for stand in gathering.standing:
            if stand.qn not in gathering.placed:
                gathering.picks.extend(
                    s._replace(owner=_UNPLACED)
                    for s in self._scores(stand.qn, arguments, stand.receiver, site)
                )

    def _hierarchy(self, class_qn: str) -> _Hierarchy:
        # The types the receiver's parts reach through their base lists,
        # each before every type it derives from.
        reached: list[str] = []
        seen: set[str] = set()
        stack = list(reversed(self._partial_roots(class_qn)))
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            reached.append(current)
            stack.extend(reversed(self.class_inheritance.get(current, [])))
        derived: dict[str, list[str]] = {t: [] for t in reached}
        for t in reached:
            for base in dict.fromkeys(self.class_inheritance.get(t, [])):
                if base in derived and base != t:
                    derived[base].append(t)
        return _Hierarchy(
            _derived_first(reached, derived),
            derived,
            _ancestors_within(reached, self.class_inheritance),
        )

    def _views(
        self,
        hierarchy: _Hierarchy,
        receiver_bindings: dict[str, str],
        spelled: tuple[str, ...],
    ) -> Callable[[str], _ReceiverView]:
        # How the call reads each type's members: the receiver's parts with
        # the type arguments its written type passes, each base with those
        # the first type deriving from it passes it, in that type's terms.
        bound: dict[str, dict[str, str]] = {}

        def view(declaring: str) -> _ReceiverView:
            # Asked once per type, every type deriving from it asked first.
            via = next((d for d in hierarchy.derived[declaring] if d in bound), None)
            bindings = bound[declaring] = (
                receiver_bindings
                if via is None
                else bindings_for_base(
                    self.csharp_generic_shapes,
                    self.csharp_class_generic_arity,
                    via,
                    bound[via],
                    declaring,
                    self.scope_of(via),
                )
            )
            shape = self.csharp_generic_shapes.get(declaring)
            parameters = shape.parameters if shape is not None else ()
            return (
                bindings,
                frozenset(p for p in parameters if p not in bindings),
                spelled,
            )

        return view

    def _level(
        self,
        qns: Sequence[str],
        receiver: _ReceiverView,
        where: tuple[str, _Hierarchy],
        gathering: _Gathering,
    ) -> tuple[list[str], dict[str, str]]:
        # One type's candidates, less those a type deriving from it hides
        # and the overrides, which wait for their virtual; each virtual an
        # override stands for.
        kept: list[str] = []
        stands: dict[str, str] = {}
        for qn in qns:
            stand = self._stand_for(qn, receiver, where, gathering)
            if isinstance(stand, _Sentinel):
                continue
            if stand is not None:
                stands[qn] = stand
                gathering.placed.add(stand)
            kept.append(qn)
        return kept, stands

    def _stand_for(
        self,
        qn: str,
        receiver: _ReceiverView,
        where: tuple[str, _Hierarchy],
        gathering: _Gathering,
    ) -> str | _Sentinel | None:
        # The override standing for a virtual candidate, None for one no
        # override stands for; `_PASSED_OVER` for one a type deriving from
        # its own hides, and for an override, which then waits for its
        # virtual.
        declaring, hierarchy = where
        hiding = [
            sig
            for owner, sig in gathering.declared
            if declaring in hierarchy.ancestors.get(owner, frozenset())
        ]
        standing = [
            o
            for o in gathering.standing
            if declaring in hierarchy.ancestors.get(o.declaring, frozenset())
        ]
        overrides = self._overrides(qn)
        if not (hiding or standing or overrides):
            return None
        signature = self._signature(qn, receiver)
        if any(_same_signature(signature, other) for other in hiding):
            return _PASSED_OVER
        stand = next(
            (o.qn for o in standing if _same_signature(signature, o.signature)), None
        )
        if not overrides:
            return stand
        if stand is None:
            gathering.standing.append(_Standing(signature, qn, receiver, declaring))
        return _PASSED_OVER

    def _overrides(self, method_qn: str) -> bool:
        shape = self.csharp_call_shapes.get(method_qn)
        return shape is not None and shape.overrides

    def _signature(self, method_qn: str, receiver: _ReceiverView) -> list[str]:
        # A method's declared parameter types, as a use through the receiver
        # reads them, its own type parameters named by position: an override
        # or a hiding member may rename them (`M<T>(T)`, `M<U>(U)`; Greptile
        # review of PR 2814).
        # A nullable reference annotation is no part of a signature (`V(string?)`
        # overrides `V(string)`).
        reading = self._reading(method_qn, receiver)
        return by_position(
            [
                unannotated(
                    declared_type(p, reading.scope, reading.visible),
                    self._is_reference,
                )[0]
                for p in self._ranked_types(method_qn)
            ],
            self._declared_type_parameters(method_qn),
        )

    def _best_overloads(
        self,
        qns: Sequence[str],
        arguments: list[ArgumentType | None],
        receiver: tuple[Mapping[str, str], frozenset[str], tuple[str, ...]],
        site: _CallSite,
    ) -> tuple[list[Scored], bool]:
        # The overloads of one declaring type no other one is better than,
        # each scored in every form C# may call it in, and whether one of
        # them is known to apply.
        scored = [
            scored
            for qn in qns
            for scored in self._scores(qn, arguments, receiver, site)
        ]
        return (
            best_candidates(scored, self._better_target),
            any(certainly_applies(candidate) for candidate in scored),
        )

    def _scores(
        self,
        method_qn: str,
        arguments: list[ArgumentType | None],
        receiver: tuple[Mapping[str, str], frozenset[str], tuple[str, ...]],
        site: _CallSite,
    ) -> list[Scored]:
        # A candidate scored in each form its arguments bind it in: one no
        # form takes cannot apply. A call naming arguments to a method whose
        # parameter names are not known is scored on its positional ones
        # alone, and compared with nothing.
        forms = self._binding_forms(method_qn, site.names, len(arguments))
        if forms is None:
            unnamed = [
                a if n is None else None
                for a, n in zip(arguments, site.names, strict=True)
            ]
            form = self._unshaped_forms(method_qn, len(arguments))[0]
            return [
                self._score_overload(
                    method_qn, form._replace(known=False), unnamed, receiver, site
                )
            ]
        if not forms:
            return [Scored(method_qn, [None] * len(arguments), [""] * len(arguments))]
        return [
            self._score_overload(method_qn, form, arguments, receiver, site)
            for form in forms
        ]

    def _binding_forms(
        self, method_qn: str, names: Sequence[str | None], arg_count: int
    ) -> list[_Form] | None:
        # Every form C# may call `method_qn` in with `arg_count` arguments of
        # these names: normal, and expanded for a `params` tail, each binding
        # every argument to a declared parameter (`_slots`). None when the
        # call names an argument and the parameters' names are not known.
        types = self._ranked_types(method_qn)
        shape = self.csharp_call_shapes.get(method_qn)
        if shape is None or len(shape.names) != len(types):
            return None if any(names) else self._unshaped_forms(method_qn, arg_count)
        given = names or (None,) * arg_count
        return [
            _form_of(types, slots, expanded, shape.variadic)
            for expanded in ((False, True) if shape.variadic else (False,))
            if (slots := _slots(shape, len(types), given, expanded)) is not None
        ]

    def _unshaped_forms(self, method_qn: str, arg_count: int) -> list[_Form]:
        # The form of a method whose parameters are not known by name (no
        # call shape records them), read off its signature: a trailing array
        # takes the arguments past the others as elements, and defaults leave
        # the rest out, which only the call vouches for, so it is not known to
        # apply unless it takes exactly its declared parameters.
        types = self._ranked_types(method_qn)
        variadic = self._is_variadic(method_qn, types)
        return [
            _Form(
                self._call_parameters(method_qn, arg_count),
                None,
                variadic,
                arg_count < len(types) - int(variadic),
                arg_count == len(types) and not variadic,
            )
        ]

    def _candidate_methods(
        self, declaring: str, call: _MemberCall, spelled: int
    ) -> list[str]:
        # The overloads of `declaring` the call can bind by its argument
        # count, and, when it spells type arguments, only the methods that
        # declare as many type parameters: `c.M<long>(1)` binds no `M(int)`.
        qns = self._applicable_methods(declaring, call.method_name, call.arg_count)
        if not spelled:
            return qns
        return [qn for qn in qns if len(self._declared_type_parameters(qn)) == spelled]

    def _declared_type_parameters(self, method_qn: str) -> tuple[str, ...]:
        # A generic method's own type parameters, in declared order.
        shape = self.csharp_call_shapes.get(method_qn)
        return shape.type_parameters if shape is not None else ()

    def _score_overload(
        self,
        method_qn: str,
        form: _Form,
        arguments: list[ArgumentType | None],
        receiver: tuple[Mapping[str, str], frozenset[str], tuple[str, ...]],
        site: _CallSite,
    ) -> Scored:
        # How each argument binds its parameter of `method_qn` in one form,
        # given the type arguments the receiver binds, the parameters it
        # leaves open and the type arguments the call spells, with what C#
        # compares it by (`Scored`). A type parameter the method declares
        # itself shadows its class's of the same name
        # (`Handler<T>.Handle<T>(T)`), so the receiver's binding of the
        # class's never reaches it. The parameters and the bounds are both
        # read through the receiver and the call (`declared_type`), and what
        # they leave unbound is open in either.
        reading = self._reading(method_qn, receiver)
        seen = [
            declared_type(parameter, reading.scope, reading.visible)
            for parameter in form.parameters
        ]
        inferred = self._inferred_fits(method_qn, seen, arguments, reading)
        declared = [
            canonical_type(p, reading.scope) for p in self._ranked_types(method_qn)
        ]
        return Scored(
            method_qn,
            self._passed_fits(method_qn, form, inferred.fits, site.modes),
            [substitute(parameter, inferred.fixed) for parameter in seen],
            bool(reading.own),
            form.expanded,
            # Compared by the declared type each argument binds.
            [declared[i] for i in form.slots] if form.slots is not None else declared,
            reading.scope.type_parameters if reading.scope else reading.own,
            inferred.settled and form.known,
            form.omits,
            len(declared),
            reading.unbound - inferred.fixed.keys(),
            tuple({**reading.visible, **inferred.fixed}.items()),
        )

    def _passed_fits(
        self,
        method_qn: str,
        form: _Form,
        fits: list[Fit | None],
        modes: Sequence[str],
    ) -> list[Fit | None]:
        # Each fit as the way its argument is passed lets it stand
        # (`_passed_in_modes`).
        shape = self.csharp_call_shapes.get(method_qn)
        declared = shape.modes if shape is not None else ()
        return _passed_in_modes(
            fits, form, (declared, modes), len(_param_types(method_qn)) - 1
        )

    def _reading(
        self,
        method_qn: str,
        receiver: tuple[Mapping[str, str], frozenset[str], tuple[str, ...]],
    ) -> _Reading:
        # How `method_qn`'s declaration reads through the receiver and the
        # type arguments the call spells, which fix its own type parameters.
        bindings, open_names, spelled = receiver
        own = self._method_type_parameters(method_qn)
        declared = self._declared_type_parameters(method_qn)
        fixed = dict(zip(declared, spelled, strict=True)) if spelled else {}
        return _Reading(
            self.scope_of(_member_type(method_qn), own),
            {**unshadowed(bindings, own), **fixed},
            own,
            (open_names | own) - fixed.keys(),
            fixed,
        )

    def _inferred_fits(
        self,
        method_qn: str,
        parameters: list[str],
        arguments: list[ArgumentType | None],
        reading: _Reading,
    ) -> _Judged:
        # Each argument is scored on its own, then, as C# infers before it
        # judges applicability, what inference took from all of them must
        # fix one type per type parameter, that type must meet its `where`
        # clause, and every argument must still bind its parameter with the
        # fixed types in, or the candidate does not apply. It is then ranked
        # by those fits, against the parameter types C# compares (PR 2814
        # review); a type parameter left unfixed stays open in them, and one
        # of its own the arguments bound leaves it unsettled.
        if self._uninferable(parameters, reading):
            return _Judged([None] * len(arguments), {})
        found: list[Inferred] = []
        fits = [
            fit(parameter, argument, reading.unbound, self._recording_judge(found))
            for parameter, argument in zip(parameters, arguments, strict=True)
        ]
        if None in fits:
            return _Judged(fits, {})
        merged = merged_inferences(
            [*found, _lower_bounds(parameters, arguments, reading.unbound)]
        )
        inferred = (
            self._fixed_types(merged, reading.own) if merged is not None else None
        )
        fixed = {**reading.spelled, **inferred} if inferred is not None else None
        if merged is None or fixed is None:
            return _Judged([None] * len(fits), {})
        # A `where` clause the fixed types break drops the candidate; one not
        # known to hold leaves it unsettled, as a type parameter of its own
        # the arguments bound but did not fix does.
        breaks = self._breaks_constraints(
            fixed, merged, self._where_bounds(method_qn, reading), reading.unbound
        )
        if breaks:
            return _Judged([None] * len(fits), {})
        if fixed:
            fits = self._fits_once_fixed(parameters, arguments, fixed, reading.unbound)
        unfixed = (merged.keys() & reading.own) - fixed.keys()
        return _Judged(fits, fixed, breaks is False and not unfixed)

    @staticmethod
    def _uninferable(parameters: Sequence[str], reading: _Reading) -> bool:
        # Whether a type parameter the method declares is one the call
        # neither spells nor passes an argument for a parameter naming it,
        # which C# cannot infer: `M<T>(int x)` takes no `M(1)`.
        named = set().union(*(mentioned_names(p) for p in parameters))
        return bool(reading.own - reading.spelled.keys() - named)

    def _where_bounds(self, method_qn: str, reading: _Reading) -> dict[str, list[str]]:
        # Each constrained type parameter's bounds, read through the receiver;
        # a kind (`struct`, `class`) names no type and is kept as written.
        return {
            name: [
                b
                if b in cs.CSHARP_KIND_CONSTRAINTS
                else declared_type(b, reading.scope, reading.visible)
                for b in written
            ]
            for name, written in self._constraint_bounds(method_qn).items()
        }

    def _better_target(self, first: str, second: str) -> int:
        # C#'s better conversion target between two parameter types an
        # argument converts to: the one that converts to the other where the
        # other does not convert back. +1, -1, or 0 when that is not known.
        # Between two integral types neither converts to, the signed one.
        forward = self._implicitly_converts(first, second)
        backward = self._implicitly_converts(second, first)
        if forward is True and backward is False:
            return 1
        if backward is True and forward is False:
            return -1
        return signed_preference(first, second) if forward is backward is False else 0

    def _recording_judge(self, found: list[Inferred]) -> _ScopedJudge:
        # A judge for one argument, whose matches add the bounds they
        # inferred inside a type argument as that argument's entry.
        inferred: Inferred = {}
        found.append(inferred)
        return _ScopedJudge(
            partial(self._identical, inferred=inferred),
            partial(self._converts, inferred=inferred),
            self._names_enum,
        )

    def _fixed_types(
        self, merged: Inferred, own: frozenset[str]
    ) -> dict[str, str] | None:
        # The type fixed for each type parameter its bounds fix, None when
        # no type meets them. A receiver's parameter is given, not inferred:
        # its bounds only constrain it, so only an exact one fixes it, which
        # every other must then allow.
        fixed: dict[str, str] = {}
        for name, bounds in merged.items():
            exact = next((t for kind, t in bounds if kind is Bound.EXACT), None)
            if exact is None and name not in own:
                continue
            fails, fixed_type = self._fixing(bounds, exact)
            if fails:
                return None
            if fixed_type is not None:
                fixed[name] = fixed_type
        return fixed

    def _fits_once_fixed(
        self,
        parameters: Sequence[str],
        arguments: Sequence[ArgumentType | None],
        fixed: Mapping[str, str],
        open_names: frozenset[str],
    ) -> list[Fit | None]:
        # How every argument binds its parameter with the fixed types in: a
        # conversion judged with a type parameter open (an operator to
        # `Box<string>` for `Box<T>`) may not hold once T is int, and C#
        # compares the conversions to these types, so `M<T>(T)` with T := int
        # ties `M(int)`, which then wins as the non-generic method.
        judge = _ScopedJudge(self._identical, self._converts, self._names_enum)
        still_open = open_names - fixed.keys()
        return [
            fit(substitute(parameter, fixed), argument, still_open, judge)
            for parameter, argument in zip(parameters, arguments, strict=True)
        ]

    def _fixing(
        self, bounds: Sequence[tuple[Bound, str]], exact: str | None
    ) -> tuple[bool, str | None]:
        # C#'s fixing of one type parameter: of its bounds' types (its exact
        # one alone, when it has one), those every bound allows remain, and
        # it is fixed to the one every other remaining one converts to.
        # (whether none can remain, the type fixed when that is known) An
        # exact type is the only one it can be fixed to; a candidate bound
        # keeps it from that one only when it allows it as no kind of bound.
        candidates = [exact] if exact is not None else _distinct([t for _, t in bounds])
        verdicts = [
            (c, _all_hold({self._allows(b, c) for b in bounds})) for c in candidates
        ]
        if any(verdict is None for _, verdict in verdicts):
            return False, None
        remaining = [candidate for candidate, verdict in verdicts if verdict]
        return not remaining, self._widest(remaining)

    def _allows(self, bound: tuple[Bound, str], candidate: str) -> bool | None:
        # Whether a type parameter bounded so may be `candidate`: its exact
        # type, a type its lower bound converts to, or one that converts to
        # its upper bound. A candidate bound, of a kind not known, allows its
        # own type, no type it allows as no kind (one it neither is nor
        # converts to nor from: PR 2814's ground-truth harness), and of any
        # other it is not known.
        kind, type_text = bound
        if kind in (Bound.EXACT, Bound.CANDIDATE):
            same = type_text == candidate or same_type(type_text, candidate)
            if kind is Bound.EXACT or same:
                return same
            return self._candidate_allows(type_text, candidate)
        if kind is Bound.LOWER:
            return self._implicitly_converts(type_text, candidate)
        return self._implicitly_converts(candidate, type_text)

    def _candidate_allows(self, bound: str, candidate: str) -> bool | None:
        # A candidate bound of another type: False when it allows `candidate`
        # neither as a lower bound nor as an upper one (nor, so, as exact).
        lower = self._implicitly_converts(bound, candidate)
        upper = self._implicitly_converts(candidate, bound)
        return False if lower is False and upper is False else None

    def _widest(self, remaining: Sequence[str]) -> str | None:
        # The one type every other of `remaining` converts to, if known.
        return next(
            (
                v
                for v in remaining
                if all(self._implicitly_converts(o, v) is True for o in remaining)
            ),
            None,
        )

    def _implicitly_converts(self, source: str, target: str) -> bool | None:
        # Whether a value of `source` converts implicitly to `target`, each
        # a resolved type: one type, a predefined conversion, a variant
        # interface's or delegate's, or one first-party classes declare.
        # None when that is not known.
        if same_type(source, target):
            return True
        predefined = predefined_converts(source, target)
        if predefined is not None:
            return predefined
        if type_shape(source)[1] or type_shape(target)[1]:
            return None
        return self._converts(_written_argument(source), target, ())

    def _breaks_constraints(
        self,
        fixed: Mapping[str, str],
        merged: Inferred,
        bounds: Mapping[str, list[str]],
        open_names: frozenset[str],
    ) -> bool | None:
        # Whether a type parameter's fixed type breaks its `where` clause,
        # read with the other parameters' fixed types (`where T : U`), or,
        # where none was fixed, what its bounds prove does (`_unfixed_breaks`).
        # None when no clause is broken but one is not known to hold.
        verdict: bool | None = False
        for name, written in bounds.items():
            closed = [substitute(bound, fixed) for bound in written]
            if name in fixed:
                breaks = self._breaks_constraint(closed, fixed[name], open_names)
            else:
                breaks = self._unfixed_breaks(closed, merged.get(name, ()), open_names)
            if breaks:
                return True
            if breaks is None:
                verdict = None
        return verdict

    def _unfixed_breaks(
        self,
        closed: Sequence[str],
        bounds: Sequence[tuple[Bound, str]],
        open_names: frozenset[str],
    ) -> bool:
        # Whether a type parameter no type was fixed for breaks its `where`
        # clause however it is fixed: its exact or a lower bound breaks it (a
        # type that derives from no bound has no base that does), or every
        # type its bounds name does, one of which C# fixes it to.
        if any(
            self._breaks_constraint(closed, t, open_names)
            for kind, t in bounds
            if kind in (Bound.EXACT, Bound.LOWER)
        ):
            return True
        return bool(bounds) and all(
            self._breaks_constraint(closed, t, open_names) for _, t in bounds
        )

    def _constraint_bounds(self, method_qn: str) -> dict[str, list[str]]:
        # Each constrained type parameter of `method_qn`, with its types.
        shape = self.csharp_call_shapes.get(method_qn)
        bounds: dict[str, list[str]] = {}
        for name, bound in shape.constraints if shape is not None else ():
            bounds.setdefault(name, []).append(bound)
        return bounds

    def _breaks_constraint(
        self, bounds: Sequence[str], given: str, open_names: Collection[str]
    ) -> bool | None:
        # Whether a type parameter fixed to `given` breaks a bound of its
        # `where` clause, which drops the generic overload (`M<T>(T) where T
        # : Person` takes no Circle; PR 2814 review). None when none is
        # broken but one is not known to hold. Both come resolved, the type
        # in the caller's file, the bound in the method's and closed by the
        # receiver.
        verdict: bool | None = False
        for bound in bounds:
            meets = self._meets(bound, given, frozenset(open_names))
            if meets is False:
                return True
            if meets is None:
                verdict = None
        return verdict

    def _meets(self, bound: str, given: str, open_names: frozenset[str]) -> bool | None:
        # Whether `given` meets one bound, None when that is not known. A
        # kind is met by its kind of type, a bound by the type itself, and a
        # bound or a type of a shape as `_shape_meets` tells: a type
        # parameter fixed to `object` (`where T : U`) by every type
        # (Greptile review of PR 2814). A type no file declares meets a
        # bound as `_built_in_meets` tells. A first-party type meets a
        # first-party bound by its bases, through the bases each writes (a
        # user-defined conversion never meets one), and an external bound
        # only through an external base, so not at all when every base it
        # reaches is first-party (`_meets_external`). A bound naming a type
        # parameter still open (the method's own, or one the receiver leaves
        # unbound) is open there.
        if bound in cs.CSHARP_KIND_CONSTRAINTS:
            return self._meets_kind(bound, given)
        if given == bound or same_type(given, bound):
            return True
        decided, verdict = self._shape_meets(bound, given)
        if decided:
            return verdict
        bound_qn = self._first_party_type(bound)
        given_qn = self._first_party_type(given)
        if given_qn is None:
            return _built_in_meets(given, bound, bound_qn is not None)
        if bound_qn is not None:
            return self._meets_first_party(
                (given, given_qn), (bound, bound_qn), open_names
            )
        if mentioned_names(bound) & open_names:
            return None
        return self._meets_external(bound, given_qn)

    def _shape_meets(self, bound: str, given: str) -> tuple[bool, bool | None]:
        # (whether the bound's or the type's shape decides, how): a bound of
        # a predefined type as `_meets_predefined` tells; a nullable bound,
        # a value type, by itself alone; an array bound by no type that is
        # not an array; `System.ValueType` by a value type alone; and a
        # nullable value, which implements no interface, by no other bound.
        if (keyword := _bound_keyword(bound)) is not None:
            return True, _meets_predefined(keyword, given)
        bound_shape, given_shape = type_shape(bound)[1], type_shape(given)[1]
        if cs.CSHARP_NULLABLE_SUFFIX in bound_shape or (
            cs.CSHARP_ARRAY_RANK_OPEN in bound_shape
            and cs.CSHARP_ARRAY_RANK_OPEN not in given_shape
        ):
            return True, False if _known_type(given) else None
        reference = self._is_reference(given)
        if _framework_name(bound) == cs.CSHARP_VALUE_TYPE_BASE:
            return True, None if reference is None else not reference
        if reference is False and cs.CSHARP_NULLABLE_SUFFIX in given_shape:
            return True, False
        return False, None

    def _meets_kind(self, kind: str, given: str) -> bool | None:
        # `class` takes a reference type alone; `struct` a value type that is
        # no nullable, `unmanaged` one whose every field is unmanaged too: a
        # predefined value type's and an enum's are.
        reference = self._is_reference(given)
        if reference is None or kind == cs.CSHARP_CLASS_CONSTRAINT:
            return reference
        core, suffix = type_shape(given)
        if reference or cs.CSHARP_NULLABLE_SUFFIX in suffix:
            return False
        if kind == cs.CSHARP_STRUCT_CONSTRAINT:
            return True
        return True if predefined_keyword(core) or self._names_enum(core) else None

    def _meets_first_party(
        self,
        given: tuple[str, str],
        bound: tuple[str, str],
        open_names: frozenset[str],
    ) -> bool | None:
        # A first-party class bound by the class hierarchy, closed as the
        # bound is; a first-party interface by every base the type reaches
        # (a closing of a generic one is not judged).
        kind = self.function_registry.get(bound[1])
        if kind == NodeType.CLASS:
            return self._class_conversion(given, bound, open_names)
        if kind == NodeType.ENUM:
            # An enum is a value type: met by itself alone.
            return given[1] == bound[1]
        if kind != NodeType.INTERFACE:
            return None
        reached, unindexed = self._ancestor_closure(given[1])
        if bound[1] not in reached:
            # An unindexed base may implement it (Greptile review of PR 2814).
            return None if unindexed else False
        return None if type_arguments(bound[0]) else True

    def _meets_external(self, bound: str, given_qn: str) -> bool | None:
        # An external bound: `System.Enum` is met by an enum alone; any other
        # by a first-party type only through an external base: one the type
        # or a base of it writes as the bound meets it (`Held : Disposer :
        # IDisposable`), and none when every base is first-party. An enum
        # implements the framework's interfaces, and a record `IEquatable<>`
        # of itself, which its bases record.
        is_enum = self.function_registry.get(given_qn) == NodeType.ENUM
        if _framework_name(bound) == cs.CSHARP_ENUM_BASE_TYPE:
            return is_enum
        if is_enum:
            return None
        external = self._ancestor_closure(given_qn)[1]
        if _external_spelling(bound) in {_external_spelling(e) for e in external}:
            return True
        return None if external else False

    def _ancestor_closure(self, type_qn: str) -> tuple[set[str], set[str]]:
        # Every first-party type `type_qn` is, derives from or implements,
        # through the bases each part of each writes, and every base on the
        # way that is not first-party, as resolved: one may implement
        # anything.
        seen: set[str] = set()
        external: set[str] = set()
        queue = deque(self._partial_roots(type_qn))
        while queue:
            current = queue.popleft()
            if current in seen:
                continue
            seen.add(current)
            for base, spelled in self._first_party_bases(current):
                if base is None:
                    external.add(spelled)
                else:
                    queue.extend(self._partial_roots(base))
        return seen, external

    def _first_party_bases(self, type_qn: str) -> list[tuple[str | None, str]]:
        # Each base of `type_qn`, resolved in its file, as (the first-party
        # type it is or None, its spelling): those it writes (interfaces too)
        # and those the inheritance pass linked.
        shape = self.csharp_generic_shapes.get(type_qn)
        scope = self.scope_of(type_qn)
        written = [
            (self._first_party_type(canonical), canonical)
            for base in (shape.base_types if shape is not None else ())
            if (canonical := canonical_type(base, scope))
        ]
        linked = [
            (base if self.function_registry.get(base) in _TYPE_DECLS else None, base)
            for base in self.class_inheritance.get(type_qn, ())
        ]
        return written + linked

    def _is_variadic(self, method_qn: str, types: list[str]) -> bool:
        shape = self.csharp_call_shapes.get(method_qn)
        if shape is not None:
            return shape.variadic
        return bool(types) and types[-1].endswith(cs.CSHARP_ARRAY_SUFFIX)

    def _ranked_types(self, method_qn: str) -> list[str]:
        # A method's parameter types as written, which keep the `?` the qn's
        # signature drops off a struct or a type parameter (`M<T>(T? x)
        # where T : struct` takes `Nullable<T>`); the qn's when the
        # declaration was not read.
        types = _param_types(method_qn)
        shape = self.csharp_call_shapes.get(method_qn)
        if shape is not None and len(shape.types) == len(types):
            return list(shape.types)
        return types

    def _call_parameters(self, method_qn: str, arg_count: int) -> list[str]:
        # The parameter each of `arg_count` arguments binds, one per argument,
        # so every overload is scored on every argument. A call that expands
        # a `params T[]` tail passes its trailing arguments as `T`; one that
        # leaves defaulted parameters out binds the leading ones. A position
        # no parameter takes (an overload that cannot apply) is unknown.
        types = self._ranked_types(method_qn)
        if self._is_variadic(method_qn, types) and arg_count != len(types):
            element = types[-1].removesuffix(cs.CSHARP_ARRAY_SUFFIX)
            types = types[:-1] + [element] * max(arg_count - len(types) + 1, 0)
        return (types + [""] * arg_count)[:arg_count]

    def _receiver_bindings(
        self, receiver: Node, class_qn: str, caller: Scope | None
    ) -> dict[str, str]:
        # The type arguments the receiver's written type passes its class's
        # parameters: `new Handler<Person>()`, or `Handler<Person> h`, each
        # resolved in the caller's file, which wrote it. Empty when the
        # syntax spells none, or not one per parameter.
        shape = self.csharp_generic_shapes.get(class_qn)
        if shape is None or not shape.parameters:
            return {}
        written = self._written_receiver_type(receiver)
        if not written:
            return {}
        arguments = type_arguments(signature_type_name(written))
        if len(arguments) != len(shape.parameters):
            return {}
        return {
            parameter: canonical_type(argument, caller)
            for parameter, argument in zip(shape.parameters, arguments, strict=True)
        }

    def _written_receiver_type(self, receiver: Node) -> str | None:
        # The type a receiver is written with: a construction's own type, or
        # the declared type of the parameter or local it names (a `var`
        # local's construction). Searched outward through the enclosing
        # blocks and callables, stopping at the type body.
        while (
            receiver.type == cs.TS_PARENTHESIZED_EXPRESSION and receiver.named_children
        ):
            receiver = receiver.named_children[0]
        if receiver.type == cs.TS_CSHARP_OBJECT_CREATION_EXPRESSION:
            return safe_decode_text(receiver.child_by_field_name(cs.FIELD_TYPE))
        if receiver.type != cs.TS_CSHARP_IDENTIFIER:
            return None
        name = safe_decode_text(receiver)
        current = receiver.parent
        while name and current is not None:
            if current.type == cs.TS_CSHARP_DECLARATION_LIST:
                return None
            if current.type == cs.TS_CSHARP_BLOCK and (
                declared := self._block_local_type(current, name)
            ):
                return declared
            if declared := self._parameter_type(current, name):
                return declared
            current = current.parent
        return None

    def _block_local_type(self, block: Node, name: str) -> str | None:
        found = self._block_declaration(block, name)
        return self._declarator_written_type(*found[1:]) if found else None

    def _block_declaration(
        self, block: Node, name: str
    ) -> tuple[Node, Node, Node] | None:
        # (statement, declaration, declarator) of the local `name` a block
        # declares.
        for statement in block.named_children:
            for decl in statement.named_children:
                if decl.type != cs.TS_CSHARP_VARIABLE_DECLARATION:
                    continue
                declarator = self._declarator_named(decl, name)
                if declarator is not None:
                    return statement, decl, declarator
        return None

    @staticmethod
    def _declarator_named(decl: Node, name: str) -> Node | None:
        for declarator in decl.named_children:
            if (
                declarator.type == cs.TS_CSHARP_VARIABLE_DECLARATOR
                and safe_decode_text(declarator.child_by_field_name(cs.FIELD_NAME))
                == name
            ):
                return declarator
        return None

    @staticmethod
    def _declarator_written_type(decl: Node, declarator: Node) -> str | None:
        # The declaration's own type, or, for a `var` local, the type its
        # initializer spells for itself.
        type_node = decl.child_by_field_name(cs.FIELD_TYPE)
        if type_node is not None and type_node.type != cs.TS_CSHARP_IMPLICIT_TYPE:
            return safe_decode_text(type_node)
        initializer = _initializer(declarator)
        return _spelled_type(initializer) if initializer is not None else None

    @staticmethod
    def _parameter_type(scope: Node, name: str) -> str | None:
        param_list = scope.child_by_field_name(cs.FIELD_PARAMETERS)
        if param_list is None:
            return None
        for param in param_list.named_children:
            if (
                param.type == cs.TS_CSHARP_PARAMETER
                and safe_decode_text(param.child_by_field_name(cs.FIELD_NAME)) == name
            ):
                return safe_decode_text(param.child_by_field_name(cs.FIELD_TYPE))
        return None

    def _typed_hierarchy(
        self, class_qn: str, receiver_bindings: dict[str, str]
    ) -> Iterator[tuple[str, dict[str, str], frozenset[str]]]:
        # (type, the type arguments bound to its parameters, its parameters
        # left unbound) from the receiver's parts up through every base, in
        # the depth-first order `_find_method_by_arity` walks.
        seen: set[str] = set()
        stack: list[tuple[str, dict[str, str]]] = [
            (root, receiver_bindings)
            for root in reversed(self._partial_roots(class_qn))
        ]
        while stack:
            current, bindings = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            shape = self.csharp_generic_shapes.get(current)
            parameters = shape.parameters if shape is not None else ()
            yield (
                current,
                bindings,
                frozenset(p for p in parameters if p not in bindings),
            )
            stack.extend(
                (
                    base,
                    bindings_for_base(
                        self.csharp_generic_shapes,
                        self.csharp_class_generic_arity,
                        current,
                        bindings,
                        base,
                        self.scope_of(current),
                    ),
                )
                for base in reversed(self.class_inheritance.get(current, []))
            )

    def _applicable_methods(
        self, class_qn: str, method_name: str, arg_count: int
    ) -> list[str]:
        # The methods a call of `arg_count` arguments can bind by their
        # arity, defaults or a `params` tail. A line-suffixed twin
        # (`M(int)@12`, a `#if` branch) is the same overload, which the call
        # pass reaches through the registry variants, unless it takes its
        # arguments in other modes or types (`M(out int)` beside `M(int)`,
        # `M(Point?)` beside `M(Point)`).
        prefix = f"{class_qn}{cs.SEPARATOR_DOT}"
        found = [
            qn
            for qn in self._direct_same_name_methods(class_qn, method_name)
            if _arity(qn[len(prefix) :]) == arg_count
            or _accepts_arg_count(
                qn[len(prefix) :], arg_count, self.csharp_call_shapes.get(qn)
            )
        ]
        spelled = set(found)
        return [
            qn
            for qn in found
            if (natural := qn_markers.strip_dup_marker(qn)) == qn
            or natural not in spelled
            or self._declared_as(natural) != self._declared_as(qn)
        ]

    def _declared_as(self, method_qn: str) -> tuple[tuple[str, ...], ...]:
        # How a method takes its parameters beyond what its qn spells: their
        # modes and their types as written, its own type parameters named by
        # position.
        # A nullable reference annotation tells no two overloads apart
        # (`M(string?)` in one `#if` branch is `M(string)` in the other).
        shape = self.csharp_call_shapes.get(method_qn)
        if shape is None:
            return (), ()
        types = [unannotated(t, self._is_reference)[0] for t in shape.types]
        return shape.modes, tuple(by_position(types, shape.type_parameters))

    def _method_type_parameters(self, method_qn: str) -> frozenset[str]:
        # The type parameters a generic method declares (`M<U>(U item)`),
        # as its declaration spells them.
        shape = self.csharp_call_shapes.get(method_qn)
        return frozenset(shape.type_parameters) if shape is not None else frozenset()

    def _argument_types(
        self,
        call_node: Node,
        local_var_types: dict[str, str],
        module_qn: str,
        caller_qn: str | None,
    ) -> list[ArgumentType | None]:
        arg_list = _argument_list(call_node)
        if arg_list is None:
            return []
        arguments = [c for c in arg_list.children if c.type == cs.TS_CSHARP_ARGUMENT]
        # A named argument is matched to its parameter by its name, per
        # candidate (`_slots`), not here.
        return [
            self._argument_type(a, local_var_types, module_qn, caller_qn)
            for a in arguments
        ]

    def _argument_type(
        self,
        argument: Node,
        local_var_types: dict[str, str],
        module_qn: str,
        caller_qn: str | None,
    ) -> ArgumentType | None:
        value = argument.named_children[-1] if argument.named_children else None
        if value is None:
            return None
        if (constant := _literal_constant(value)) is not None:
            return ArgumentType(constant.type, None, 0, None, constant.value)
        if value.type == cs.TS_CSHARP_OBJECT_CREATION_EXPRESSION or (
            value.type == cs.TS_CSHARP_CAST_EXPRESSION and not _reads_as_sum(value)
        ):
            # The written type keeps its arguments: `new Ctx<Person>()` is
            # an identity for `Ctx<T>` with T := Person, not for `Ctx<Order>`.
            raw = safe_decode_text(value.child_by_field_name(cs.FIELD_TYPE))
            return _written_argument(raw) if raw else None
        # A parameter or local carries the type it was declared with, type
        # arguments included (`Box<int> b`), so its closing is known too.
        # A `const` local passes its constant, so C#'s constant conversions
        # take it as they take the literal.
        if value.type == cs.TS_CSHARP_IDENTIFIER and (
            declared := self._written_receiver_type(value)
        ):
            return self._local_argument(value, declared)
        operated = self._operator_argument(value, module_qn)
        if not isinstance(operated, _Sentinel):
            return operated
        return self._member_argument(value, local_var_types, module_qn, caller_qn)

    def _operator_argument(
        self, value: Node, module_qn: str
    ) -> ArgumentType | _Sentinel | None:
        # An operator's result passed as an argument. An operator over a
        # variable is no constant: its value is of the type the operator
        # gives (`u + 1` a uint), as a typed local's is; so is a user-defined
        # prefix operator's (`-s`). `_PASSED_OVER` for any other expression.
        if (
            value.type == cs.TS_BINARY_EXPRESSION
            and self._constness(value, 0)[0] is False
        ):
            keyword = self._value_keyword(value, 0, module_qn)
            return _written_argument(keyword) if keyword else None
        if value.type == cs.TS_CSHARP_PREFIX_UNARY_EXPRESSION and (
            result := self._unary_keyword(value, 0, module_qn)
        ):
            return _written_argument(result)
        return _PASSED_OVER

    def _local_argument(self, value: Node, declared: str) -> ArgumentType:
        # A `const` local passes its constant; any other its declared type.
        constant = self._local_constant(value)
        if constant is not None:
            return ArgumentType(constant.type, None, 0, None, constant.value)
        return _written_argument(declared)

    def _member_argument(
        self,
        value: Node,
        local_var_types: dict[str, str],
        module_qn: str,
        caller_qn: str | None,
    ) -> ArgumentType | None:
        # A field or property read: off its declaration when the caller's
        # file declares it, else as its annotation tells; nothing when what
        # it reads is not known.
        member = self._file_member_argument(value)
        if isinstance(member, _Unread):
            return None
        if member is not None:
            return member
        result = self._call_result(
            self._result_argument, value, local_var_types, (module_qn, caller_qn)
        )
        if result is not None:
            return None if isinstance(result, _Unread) else result
        annotated = self._receiver_type_name(
            value, local_var_types, module_qn, caller_qn
        )
        if not annotated:
            return None
        name, arity = split_type_ref(annotated)
        # A field or property of a predefined type may be a constant whose
        # value is not read: a narrowing it takes stays undecided.
        if not arity and (keyword := predefined_keyword(name)) is not None:
            return ArgumentType(keyword, None, 0, None)
        return ArgumentType(None, name, arity, None)

    def _file_member_argument(self, value: Node) -> ArgumentType | _Unread | None:
        # A field or property the caller's own file declares, read off its
        # declaration, which is parsed whenever the call is (PR 2814 review):
        # a `const` field passes its constant, any other member is a variable
        # of its written type, type arguments included. Found as C#'s
        # simple-name lookup finds it (`name_lookup`); `_UNREAD` when what the
        # name reads is not known there, or its type names another type
        # where the member is declared than where it is read (an inherited
        # `Holder K` beside the reader's own `Holder`), None for one this
        # file does not declare, which stays as its annotation tells.
        found = self._read_member(value)
        if found is None or isinstance(found, _Unread):
            return found
        member, name = found
        if not _reads_alike(member, value, name):
            return _UNREAD
        if member.type == cs.TS_CSHARP_PROPERTY_DECLARATION:
            return _property_argument(member, name)
        if member.type == cs.TS_CSHARP_FIELD_DECLARATION:
            return self._field_argument(member, name)
        return None

    def _read_member(self, value: Node) -> tuple[Node, str] | _Unread | None:
        # The member declaration a value reads, with its name: one a bare
        # name finds, or one the body of the type a member access's owner
        # names declares.
        if value.type == cs.TS_CSHARP_IDENTIFIER:
            return _named_member(value)
        if value.type != cs.TS_CSHARP_MEMBER_ACCESS_EXPRESSION:
            return None
        owner = value.child_by_field_name(cs.TS_CSHARP_FIELD_EXPRESSION)
        name = safe_decode_text(value.child_by_field_name(cs.FIELD_NAME)) or ""
        body = (
            _owner_body(owner, self._local_written_type(owner))
            if owner is not None and name
            else None
        )
        if body is None or isinstance(body, _Unread):
            return body
        return next(
            (
                (member, name)
                for member in body.named_children
                if _declares_value(member, name)
            ),
            None,
        )

    def _local_written_type(self, owner: Node) -> str | None:
        # The type a local or parameter an owner names is declared with: a
        # block's local (a `var` one's construction), a parameter, or any
        # other binder in scope (`foreach`, `catch`, a pattern, `out var`).
        if owner.type != cs.TS_CSHARP_IDENTIFIER:
            return None
        if written := self._written_receiver_type(owner):
            return written
        binder = local_binder(owner, safe_decode_text(owner) or "")
        return _binder_type(binder) if binder is not None else None

    def _field_argument(self, field: Node, name: str) -> ArgumentType | None:
        # What a field declaration passes for `name`, when it declares it.
        decl = next(
            (
                c
                for c in field.named_children
                if c.type == cs.TS_CSHARP_VARIABLE_DECLARATION
            ),
            None,
        )
        declarator = self._declarator_named(decl, name) if decl is not None else None
        if decl is None or declarator is None:
            return None
        constant = _const_constant(field, decl, declarator)
        if constant is not None:
            return ArgumentType(constant.type, None, 0, None, constant.value)
        written = self._declarator_written_type(decl, declarator)
        return _written_argument(written) if written else None

    def _local_constant(self, identifier: Node) -> _Constant | None:
        # The constant a `const` local an identifier names is, found as
        # `_written_receiver_type` finds its declaration; None for a variable
        # or a parameter.
        name = safe_decode_text(identifier)
        current = identifier.parent
        while name and current is not None:
            if current.type == cs.TS_CSHARP_DECLARATION_LIST:
                return None
            if current.type == cs.TS_CSHARP_BLOCK and (
                found := self._block_declaration(current, name)
            ):
                return _const_constant(*found)
            if self._parameter_type(current, name):
                return None
            current = current.parent
        return None

    def _names_enum(self, parameter: str) -> bool:
        # Whether a resolved parameter type, or the value its nullable
        # wraps, is a first-party enum.
        qn = self._first_party_type(type_shape(parameter)[0])
        return qn is not None and self.function_registry.get(qn) == NodeType.ENUM

    def _first_party_type(self, canonical: str) -> str | None:
        # The first-party type a resolved spelling IS (`canonical_type`), or
        # None for a spelling no file resolved or a shape built on the type.
        qn = resolved_qn(canonical)
        return (
            qn
            if qn is not None and self.function_registry.get(qn) in _TYPE_DECLS
            else None
        )

    def _wraps_nullable(
        self,
        given: tuple[str, str],
        wanted: tuple[str, str],
        arguments: tuple[str, ...] | None,
        open_names: Collection[str],
    ) -> bool | None:
        # `Widget` to `Widget?`: the implicit nullable conversion of a value,
        # of one closing to the same closing: `G<int>` wraps into no
        # `G<string>?` (Greptile review of PR 2814). None when the value's
        # type arguments are not known.
        nullable = f"{type_shape(given[0])[1]}{cs.CSHARP_NULLABLE_SUFFIX}"
        if type_shape(wanted[0])[1] != nullable or not self._same_class(
            given[1], wanted[1]
        ):
            return False
        expected = type_arguments(type_shape(wanted[0])[0])
        if not expected:
            return True
        return arguments_match(arguments, expected, open_names)

    def _judgeable(self, given_qn: str, wanted_qn: str) -> bool:
        # Whether the hierarchy can settle a conversion between the two: the
        # wanted type a first-party class, the given one a class or enum. An
        # interface is implemented outside `class_inheritance`.
        return self.function_registry.get(wanted_qn) == NodeType.CLASS and (
            self.function_registry.get(given_qn) in (NodeType.CLASS, NodeType.ENUM)
        )

    def _same_class(self, given_qn: str, wanted_qn: str) -> bool:
        return given_qn == wanted_qn or wanted_qn in self._partial_roots(given_qn)

    def _identical(
        self,
        argument: ArgumentType,
        parameter: str,
        open_names: Collection[str],
        inferred: Inferred | None = None,
    ) -> bool | None:
        return self._annotation_blind(
            self._identical_as_written, (argument, parameter), open_names, inferred
        )

    def _converts(
        self,
        argument: ArgumentType,
        parameter: str,
        open_names: Collection[str],
        inferred: Inferred | None = None,
    ) -> bool | None:
        return self._annotation_blind(
            self._converts_as_written, (argument, parameter), open_names, inferred
        )

    def _annotation_blind(
        self,
        judged: Callable[
            [ArgumentType, str, Collection[str], Inferred | None], bool | None
        ],
        pair: tuple[ArgumentType, str],
        open_names: Collection[str],
        inferred: Inferred | None,
    ) -> bool | None:
        # `judged` of the argument and the parameter less their nullable
        # reference annotations, which no type's identity holds (Greptile
        # review of PR 2814). Where a `?` marks a type of no known kind, a
        # `False` it alone may cause is not known.
        (argument, parameter), certain = self._unannotated(pair, lenient=False)
        verdict = judged(argument, parameter, open_names, inferred)
        if verdict is not False or certain:
            return verdict
        (argument, parameter), _ = self._unannotated(pair, lenient=True)
        lenient = judged(argument, parameter, open_names, None)
        return None if lenient is not False else False

    def _unannotated(
        self, pair: tuple[ArgumentType, str], lenient: bool
    ) -> tuple[tuple[ArgumentType, str], bool]:
        # An argument and a parameter without their reference annotations,
        # and whether every `?` either holds was told apart.
        argument, parameter = pair
        texts = [argument.name or "", *(argument.arguments or ()), parameter]
        erased = [unannotated(text, self._is_reference, lenient) for text in texts]
        name, *arguments, wanted = (text for text, _certain in erased)
        return (
            (
                argument._replace(
                    name=name if argument.name is not None else None,
                    arguments=(
                        tuple(arguments) if argument.arguments is not None else None
                    ),
                ),
                wanted,
            ),
            all(certain for _text, certain in erased),
        )

    def _names_first_party(self, written: str, arity: int | None) -> bool:
        # Whether a spelling's simple name is some first-party type's, which
        # an unresolved spelling of it may or may not mean.
        simple = strip_generic_arguments(written).rsplit(cs.SEPARATOR_DOT, 1)[-1]
        return any(
            self.function_registry.get(qn) in _TYPE_DECLS
            for spelled in arity_spellings(
                simple, generic_arity_of_type_text(written) or arity
            )
            for qn in self.simple_name_lookup.get(spelled, ())
        )

    def _identical_as_written(
        self,
        argument: ArgumentType,
        parameter: str,
        open_names: Collection[str],
        inferred: Inferred | None = None,
    ) -> bool | None:
        # Whether the argument's type IS the parameter's: one class, each
        # side resolved in its own file (or, unresolved, spelled alike up to
        # aliases), in one shape (`Widget[]` is no `Widget`), closed with the
        # same type arguments. None when they are not known on the argument's
        # side. What a match infers goes into `inferred`. A parameter that is
        # a type parameter in a shape (`T[]`) is the argument's type when its
        # element is (`int[]`, T := int).
        (given_core, given_shape), (wanted_core, wanted_shape) = (
            type_shape(argument.name or ""),
            type_shape(parameter),
        )
        if wanted_core in open_names and (passed := _passed_type(argument)):
            kinds = TypeKinds(self._variances, self._is_reference)
            return infer_into(passed, parameter, open_names, inferred, kinds)
        if argument.bounds is not None:
            # A type parameter is itself alone, never a type of its name.
            return argument.name == parameter
        if given_shape != wanted_shape:
            return False
        given_qn = self._first_party_type(given_core)
        wanted_qn = self._first_party_type(wanted_core)
        if given_qn is not None and wanted_qn is not None:
            if not self._same_class(given_qn, wanted_qn):
                return False
        elif not same_type_name(argument.name or "", parameter, argument.arity):
            return False
        elif (
            given_qn is not None
            or wanted_qn is not None
            or self._names_first_party(wanted_core, argument.arity)
        ):
            # A first-party type against a spelling of its name its file does
            # not resolve to it, or two such spellings: that type or another
            # of the name (`Outer2.In` against `Outer.In` written `In`).
            return None
        wanted = type_arguments(parameter)
        if not wanted:
            return True
        return arguments_match(
            argument.arguments,
            wanted,
            open_names,
            inferred,
            (wanted_core, TypeKinds(self._variances, self._is_reference)),
        )

    def _converts_as_written(
        self,
        argument: ArgumentType,
        parameter: str,
        open_names: Collection[str],
        inferred: Inferred | None = None,
    ) -> bool | None:
        # Whether the argument converts implicitly to the parameter: up its
        # class hierarchy, or through a declared `implicit operator`, closed
        # as the parameter is either way, or a value wrapped as nullable.
        # Known only between first-party classes, each in any shape; anything
        # else is not told, but a variant interface's or delegate's
        # conversion between two closings of it, and a predefined type's
        # value, a literal's above all, through an operator (`_from_predefined`).
        # Only a conversion up the hierarchy infers (into `inferred`): C#
        # infers no type parameter through an operator.
        if (origin := _predefined_origin(argument)) is not None:
            return self._from_predefined(origin, parameter)
        given_text = written_type(argument)
        if _takes_any_value(parameter) and "*" not in type_shape(given_text)[1]:
            # Every type converts to `object` (boxing a value), and to
            # `dynamic`: a known conversion, ranked as any other.
            return True
        if argument.bounds is not None:
            return self._bound_converts(argument.bounds, parameter, open_names)
        varies = self._variance_converts(given_text, parameter, open_names)
        if varies is not None:
            return varies
        given_qn = self._first_party_type(type_shape(given_text)[0])
        wanted_qn = self._first_party_type(type_shape(parameter)[0])
        if given_qn is not None and wanted_qn is None:
            return self._to_predefined((given_text, given_qn), parameter)
        if given_qn is None or wanted_qn is None:
            return None
        return self._first_party_converts(
            argument,
            (given_text, given_qn),
            (parameter, wanted_qn),
            (open_names, inferred),
        )

    def _first_party_converts(
        self,
        argument: ArgumentType,
        given: tuple[str, str],
        wanted: tuple[str, str],
        reading: tuple[Collection[str], Inferred | None],
    ) -> bool | None:
        # A conversion between two first-party types: one an interface takes
        # part in (`_interface_conversion`), else, between classes, up the
        # hierarchy, by a declared `implicit operator`, or wrapping a value
        # as nullable. None when it is not told.
        open_names, inferred = reading
        interfaced = self._interface_conversion(given, wanted, open_names)
        if not isinstance(interfaced, _Sentinel):
            return interfaced
        if not self._judgeable(given[1], wanted[1]):
            return None
        wraps = self._wraps_nullable(given, wanted, argument.arguments, open_names)
        if wraps is not False:
            return wraps
        derived = self._class_conversion(given, wanted, open_names, inferred)
        if derived:
            return True
        implicit = self._converts_implicitly(given, wanted, open_names)
        if implicit:
            return True
        return None if None in (derived, implicit) else False

    def _bound_converts(
        self, bounds: Sequence[str], parameter: str, open_names: Collection[str]
    ) -> bool | None:
        # Whether a value of a type parameter converts to the parameter
        # through one of the types its `where` clauses name; besides
        # `object`, it converts to nothing else.
        verdicts = {
            self._one_bound_converts(bound, parameter, open_names) for bound in bounds
        }
        if True in verdicts:
            return True
        return None if None in verdicts else False

    def _one_bound_converts(
        self, bound: str, parameter: str, open_names: Collection[str]
    ) -> bool | None:
        # Whether a type parameter's value converts to the parameter through
        # one bound. An interface bound gives it that interface and its base
        # interfaces alone: no user-defined conversion is from an interface,
        # and none from it to a class is implicit.
        bound_qn = self._first_party_type(strip_generic_arguments(bound))
        wanted_core, wanted_shape = type_shape(parameter)
        wanted_qn = self._first_party_type(wanted_core)
        if (
            bound_qn is not None
            and wanted_qn is not None
            and not wanted_shape
            and self.function_registry.get(bound_qn) == NodeType.INTERFACE
        ):
            return self._constructed_converts(
                (bound, bound_qn), (parameter, wanted_qn), open_names
            )
        given = ArgumentType(
            None,
            strip_generic_arguments(bound),
            generic_arity_of_type_text(bound),
            type_arguments(bound),
        )
        identical = self._identical_as_written(given, parameter, open_names)
        converts = self._converts_as_written(given, parameter, open_names)
        if identical or converts:
            return True
        return None if None in (identical, converts) else False

    def _variances(self, core: str) -> tuple[str, ...] | None:
        # Each type parameter's variance, "out", "in" or "", of the generic
        # type `core` names: a first-party interface's as declared, a
        # class's or struct's none, a framework type's from its table. None
        # for a type of no known kind, an external one or a delegate, which
        # the project does not register as a type.
        qn = self._first_party_type(core)
        if qn is None:
            generic = framework_generic(core)
            return generic[1] if generic is not None else None
        shape = self.csharp_generic_shapes.get(qn)
        if shape is not None and shape.variances:
            return shape.variances
        return ("",) * generic_arity_of_type_text(core)

    def _is_reference(self, type_text: str) -> bool | None:
        # Whether a resolved type is a reference type, None when not known:
        # an array is, as are `object`, `string`, a first-party class or
        # interface and the framework's reference types; a predefined value
        # type, a tuple, an enum, a struct and a pointer are not, nor is a
        # nullable value, while a `?` on a reference type changes nothing.
        core, suffix = type_shape(type_text)
        if cs.CSHARP_ARRAY_RANK_OPEN in suffix:
            return True
        if suffix.strip(cs.CSHARP_NULLABLE_SUFFIX):
            return False
        if is_tuple(core):
            return False
        if (keyword := predefined_keyword(core)) is not None:
            return keyword in cs.CSHARP_REFERENCE_KEYWORDS
        qn = self._first_party_type(core)
        if qn is None:
            return framework_reference(core)
        shape = self.csharp_generic_shapes.get(qn)
        value_type = shape is not None and shape.value_type
        return not value_type and self.function_registry.get(qn) != NodeType.ENUM

    def _variance_converts(
        self, given: str, parameter: str, open_names: Collection[str]
    ) -> bool | None:
        # Whether one closing of a variant interface or delegate converts to
        # another (`IBox<Employee>` to `IBox<Person>` for `IBox<out X>`), by
        # each type argument as its parameter varies. None for two other
        # types, or a parameter still naming an open type parameter.
        (given_core, given_shape), (wanted_core, wanted_shape) = (
            type_shape(given),
            type_shape(parameter),
        )
        if (
            given_shape
            or wanted_shape
            or mentioned_names(parameter) & set(open_names)
            or not same_type_name(given_core, wanted_core)
        ):
            return None
        variances = self._variances(wanted_core) or ()
        given_arguments = type_arguments(given_core)
        if not any(variances) or len(given_arguments) != len(variances):
            return None
        return _all_hold(
            {
                self._varies_to(variance, g, w)
                for variance, g, w in zip(
                    variances, given_arguments, type_arguments(wanted_core), strict=True
                )
            }
        )

    def _varies_to(self, variance: str, given: str, wanted: str) -> bool | None:
        # Whether a type argument converts as its parameter varies: the same
        # type, or, by an implicit reference conversion, one to the other
        # (an `out`'s argument to the wanted one, an `in`'s back). A value
        # type does not vary.
        if same_type(given, wanted):
            return True
        if not variance:
            return False
        source, target = (
            (given, wanted) if variance == cs.CSHARP_VARIANCE_OUT else (wanted, given)
        )
        reference = self._is_reference(source)
        if reference is False:
            return False
        converts = self._reference_converts(source, target)
        return converts if reference or converts is False else None

    def _reference_converts(self, source: str, target: str) -> bool | None:
        # An implicit reference conversion between two resolved reference
        # types: to `object`, up a first-party class hierarchy, or between
        # two closings of a variant type. None when it is not told.
        if predefined_keyword(target) == cs.CSHARP_OBJECT_KEYWORD:
            return True
        source_qn = self._first_party_type(source)
        target_qn = self._first_party_type(target)
        if source_qn and target_qn:
            interfaced = self._interface_conversion(
                (source, source_qn), (target, target_qn), ()
            )
            if not isinstance(interfaced, _Sentinel):
                return interfaced
        if source_qn and target_qn and self._judgeable(source_qn, target_qn):
            return self._class_conversion((source, source_qn), (target, target_qn), ())
        return self._variance_converts(source, target, ())

    def _interface_conversion(
        self,
        given: tuple[str, str],
        wanted: tuple[str, str],
        open_names: Collection[str],
    ) -> bool | _Sentinel | None:
        # A conversion an interface takes part in: to one, by reference or
        # boxing up the given type's bases and interfaces, closed as written
        # (`_constructed_converts`); from one to a class, none, as no
        # user-defined conversion is to or from an interface. `_PASSED_OVER`
        # when neither side is an interface.
        if type_shape(given[0])[1] or type_shape(wanted[0])[1]:
            return _PASSED_OVER
        if self.function_registry.get(wanted[1]) == NodeType.INTERFACE:
            return self._constructed_converts(given, wanted, open_names)
        if self.function_registry.get(given[1]) == NodeType.INTERFACE:
            return False
        return _PASSED_OVER

    def _constructed_converts(
        self,
        given: tuple[str, str],
        wanted: tuple[str, str],
        open_names: Collection[str],
        bases_only: bool = False,
    ) -> bool | None:
        # Whether a first-party type, closed as written, converts by
        # reference or boxing to a first-party base or interface, closed as
        # the parameter is: some way up its bases and interfaces closes the
        # wanted type with type arguments the wanted ones take, each the
        # same type or, for a variant interface's or delegate's parameter, a
        # reference conversion its way (Greptile review of PR 2814: a type
        # parameter `where T : IBox<int>` is no `IBox<string>`). None when
        # a way is not known: a base the index does not resolve, a closing
        # not bound, or a type argument whose conversion is not told.
        # `bases_only` leaves the given type itself out, for a caller that
        # judged its identity apart.
        wanted_text, wanted_qn = wanted
        closings, complete = self._constructed_ancestry(given)
        own = set(self._partial_roots(given[1])) if bases_only else set()
        verdicts = {
            self._closing_converts(closing, wanted_text, open_names)
            for qn, closing in closings
            if qn not in own and self._same_class(qn, wanted_qn)
        }
        if True in verdicts:
            return True
        return None if None in verdicts or not complete else False

    def _constructed_ancestry(
        self, given: tuple[str, str]
    ) -> tuple[list[tuple[str, tuple[str, ...] | None]], bool]:
        # Every first-party type a type converts to by reference up its
        # written base lists, interfaces too, each with the type arguments
        # that way closes it (None where one is not bound), and whether the
        # walk met only bases it resolved. A type implementing one generic
        # interface twice (`IBox<int>, IBox<string>`) is closed both ways.
        written, type_qn = given
        found: list[tuple[str, tuple[str, ...] | None]] = []
        complete = True
        pending: list[tuple[str, tuple[str, ...] | None]] = [
            (type_qn, type_arguments(type_shape(written)[0]) or None)
        ]
        seen: set[tuple[str, tuple[str, ...] | None]] = set()
        while pending:
            current, closing = pending.pop()
            if (current, closing) in seen:
                continue
            seen.add((current, closing))
            found.append((current, closing))
            bases, resolved = self._closed_bases(current, closing)
            complete = complete and resolved
            pending.extend(bases)
        return found, complete

    def _closed_bases(
        self, type_qn: str, closing: tuple[str, ...] | None
    ) -> tuple[list[tuple[str, tuple[str, ...] | None]], bool]:
        # The first-party bases and interfaces a type's base lists write,
        # each closed through the type's own closing, and whether every one
        # resolved (an external base may lead anywhere).
        bases: list[tuple[str, tuple[str, ...] | None]] = []
        resolved = True
        for part in self._partial_roots(type_qn):
            part_bases = self._part_bases(part, closing)
            if part_bases is None:
                resolved = resolved and not self.class_inheritance.get(part)
                continue
            for base in part_bases:
                if base is None:
                    resolved = False
                else:
                    bases.append(base)
        return bases, resolved

    def _part_bases(
        self, part: str, closing: tuple[str, ...] | None
    ) -> list[tuple[str, tuple[str, ...] | None] | None] | None:
        # Each base one part of a type writes, as (its qn, the type arguments
        # it is closed with through `closing`, None when one is not bound),
        # None for one its file does not resolve; None when the part's shape
        # or file is not known.
        shape = self.csharp_generic_shapes.get(part)
        module_qn = self.csharp_class_owner_module.get(part)
        if shape is None or module_qn is None:
            return None
        parameters = shape.parameters
        bindings = (
            dict(zip(parameters, closing, strict=True))
            if closing is not None and len(closing) == len(parameters)
            else None
        )
        scope = self.scope_of(part)
        found: list[tuple[str, tuple[str, ...] | None] | None] = []
        for written in shape.base_types:
            arguments = type_arguments(written)
            base = self._resolve_in(
                module_qn, strip_generic_arguments(written), len(arguments)
            )
            if base is None:
                found.append(None)
                continue
            known = bindings is not None or not parameters
            closed = (
                self._closed_arguments(arguments, scope, bindings or {})
                if arguments and known
                else None
            )
            found.append((base, closed))
        return found

    def _closed_arguments(
        self,
        arguments: Sequence[str],
        scope: Scope | None,
        bindings: Mapping[str, str],
    ) -> tuple[str, ...] | None:
        # A base list's type arguments with the deriving type's bound in.
        # A `?` on one of its type parameters is `Nullable<U>` under a
        # `struct` constraint and an annotation otherwise, which the stored
        # shape does not tell: bound to a type of no known reference kind,
        # the closing is not known (None).
        for argument in arguments:
            for name in _ANNOTATED_NAME.findall(argument):
                if name in bindings and not self._is_reference(bindings[name]):
                    return None
        return tuple(declared_type(a, scope, bindings) for a in arguments)

    def _closing_converts(
        self,
        closing: tuple[str, ...] | None,
        wanted_text: str,
        open_names: Collection[str],
    ) -> bool | None:
        # Whether a closing of a generic type takes the wanted closing's
        # place: each type argument the same type, or a reference conversion
        # the parameter's variance allows. None for a closing not bound, or
        # a wanted argument naming a type parameter still to infer.
        wanted_core, wanted_shape = type_shape(wanted_text)
        expected = type_arguments(wanted_core)
        if wanted_shape:
            return False
        if not expected:
            return True
        if closing is None or len(closing) != len(expected):
            return None
        if any(mentioned_names(w) & set(open_names) for w in expected):
            return None
        variances = self._variances(wanted_core) or ("",) * len(expected)
        return self._closing_varies(variances, closing, expected)

    def _closing_varies(
        self,
        variances: Sequence[str],
        closing: Sequence[str],
        expected: Sequence[str],
    ) -> bool | None:
        # Whether each type argument of a closing takes the expected one's
        # place as its parameter varies, both read without the `?` marks
        # that only annotate a reference type (Greptile review of PR 2814:
        # `IBox<string?>` is an `IBox<string>`, `IBox<int?>` no `IBox<int>`).
        # A `?` on a type of no known kind may be either, so a mismatch it
        # alone may cause is not known.
        verdicts: set[bool | None] = set()
        for variance, given, wanted in zip(variances, closing, expected, strict=True):
            plain_given, given_told = unannotated(given, self._is_reference)
            plain_wanted, wanted_told = unannotated(wanted, self._is_reference)
            verdict = self._varies_to(variance, plain_given, plain_wanted)
            told = given_told and wanted_told
            verdicts.add(None if verdict is False and not told else verdict)
        return _all_hold(verdicts)

    def _class_conversion(
        self,
        given: tuple[str, str],
        wanted: tuple[str, str],
        open_names: Collection[str],
        inferred: Inferred | None = None,
    ) -> bool | None:
        # A conversion up the class hierarchy, each side as (written, qn):
        # the wanted class is the given one or a base of it, and the given
        # type closes it as the wanted one is (`Derived : Base<int>` is no
        # `Base<string>`; PR 2814 review). None when that closing is not
        # known. No such conversion changes a shape: a `Widget` is no
        # `Widget[]`, and arrays convert only by their elements (covariance).
        wanted_text, wanted_qn = wanted
        if type_shape(given[0])[1] != type_shape(wanted_text)[1]:
            return False
        ancestry = self._class_ancestry(given[1])
        if wanted_qn not in ancestry:
            # A base the index does not hold may derive from it (Greptile
            # review of PR 2814): only a whole first-party ancestry says no.
            return False if self._all_indexed(ancestry) else None
        expected = type_arguments(wanted_text)
        if not expected:
            return True
        return arguments_match(
            self._closing_through(given, wanted_qn), expected, open_names, inferred
        )

    def _converts_implicitly(
        self,
        given: tuple[str, str],
        wanted: tuple[str, str],
        open_names: Collection[str],
    ) -> bool | None:
        # A user-defined implicit conversion is declared on its source or its
        # target type, or a base of either, and its written types resolve in
        # its declaring file. True when one applies, None when one might.
        ancestry = self._class_ancestry(given[1]) | self._class_ancestry(wanted[1])
        return self._any_operator(
            ancestry,
            lambda operator, declaring: self._operator_applies(
                operator, declaring, given, (wanted, open_names)
            ),
            False if self._all_indexed(ancestry) else None,
        )

    def _any_operator(
        self,
        declaring_types: Collection[str],
        applies: Callable[[tuple[str, str], str], bool | None],
        none_found: bool | None,
    ) -> bool | None:
        # True when an implicit operator one of `declaring_types` declares
        # applies, None when one might, else `none_found`.
        verdict = none_found
        for declaring in declaring_types:
            for operator in self._declared_operators(declaring):
                found = applies(operator, declaring)
                if found:
                    return True
                if found is None:
                    verdict = None
        return verdict

    def _declared_operators(self, declaring: str) -> list[tuple[str, str]]:
        # The (source, target) of each `implicit operator` a type declares,
        # resolved in its declaring file.
        scope = self.scope_of(declaring)
        return [
            (canonical_type(source, scope), canonical_type(target, scope))
            for source, target in self.csharp_implicit_conversions.get(declaring, ())
        ]

    def _from_predefined(self, origin: _Origin, parameter: str) -> bool | None:
        # Whether a value of a predefined type converts to the parameter, or
        # the value its nullable wraps: to a predefined type by a standard
        # implicit conversion (`_takes`), to a first-party class or struct
        # only through an implicit operator it or a base declares (a
        # predefined type declares none to it), from a type the value takes.
        # None for any other parameter.
        core, suffix = type_shape(parameter)
        if suffix.strip(cs.CSHARP_NULLABLE_SUFFIX):
            return None
        if (keyword := predefined_keyword(core)) is not None:
            return _takes(origin, keyword)
        wanted_qn = self._first_party_type(core)
        if wanted_qn is None or self.function_registry.get(wanted_qn) != NodeType.CLASS:
            return None
        return self._any_operator(
            self._class_ancestry(wanted_qn),
            lambda operator, declaring: self._operator_from_predefined(
                origin, operator, declaring, (core, wanted_qn)
            ),
            False,
        )

    def _to_predefined(self, given: tuple[str, str], parameter: str) -> bool | None:
        # Whether a first-party value converts to a predefined type other
        # than `object`: only through an implicit operator it or a base
        # declares, from a type it converts to, to a predefined type that
        # converts to the parameter's (a predefined type declares none from
        # it). With every base first-party, none applying means none exists;
        # an external base may declare one. None for any other parameter.
        core, shape = type_shape(parameter)
        keyword = None if shape else predefined_keyword(core)
        if (
            keyword is None
            or keyword == cs.CSHARP_OBJECT_KEYWORD
            or type_shape(given[0])[1]
        ):
            return None
        ancestry = self._class_ancestry(given[1])
        return self._any_operator(
            ancestry,
            lambda operator, declaring: self._operator_to_predefined(
                given, operator, declaring, keyword
            ),
            False if self._all_indexed(ancestry) else None,
        )

    def _operator_to_predefined(
        self,
        given: tuple[str, str],
        operator: tuple[str, str],
        declaring: str,
        keyword: str,
    ) -> bool | None:
        # Whether `implicit operator Target(Source)` takes a first-party value
        # to the predefined type `keyword`: the value converts to its source
        # (that class or a base, closed as written), and its target, with
        # the declaring type's own parameters bound from the value
        # (`operator T(Box<T>)` gives a Box<int> an int), is a predefined
        # type converting to `keyword` by a standard implicit conversion.
        source, target = operator
        source_qn = self._first_party_type(type_shape(source)[0])
        if source_qn is None:
            return False
        owned = self._owned_parameters(declaring)
        takes = self._class_conversion(given, (source, source_qn), owned)
        if takes is False:
            return False
        bound = substitute(
            target, self._operator_bindings(given, (source, source_qn), owned)
        )
        target_core, target_shape = type_shape(bound)
        target_keyword = predefined_keyword(target_core)
        if target_keyword is None or target_shape:
            return None if mentioned_names(bound) & owned else False
        return takes if predefined_converts(target_keyword, keyword) else False

    def _operator_from_predefined(
        self,
        origin: _Origin,
        operator: tuple[str, str],
        declaring: str,
        wanted: tuple[str, str],
    ) -> bool | None:
        # Whether `implicit operator Target(Source)` takes a predefined value
        # to the parameter: its target is the parameter's type or derives
        # from it, closed as the parameter is (binding the declaring type's
        # own parameters: `Box<X>(X)` for a `Box<int>` takes an int), and the
        # value converts to its source by a standard implicit conversion, a
        # constant only when it fits (`ByteTarget(byte)` takes 5, not 300).
        source, target = operator
        owned = self._owned_parameters(declaring)
        matched, bindings = self._target_bindings(target, wanted, owned)
        if not matched:
            return matched
        source_core, source_suffix = type_shape(substitute(source, bindings))
        keyword = predefined_keyword(source_core)
        if keyword is None or source_suffix.strip(cs.CSHARP_NULLABLE_SUFFIX):
            return None if mentioned_names(source_core) & owned else False
        return _takes(origin, keyword)

    def _operator_applies(
        self,
        operator: tuple[str, str],
        declaring: str,
        given: tuple[str, str],
        wanted: tuple[tuple[str, str], Collection[str]],
    ) -> bool | None:
        # Whether `implicit operator Target(Source)` takes the argument and
        # gives the parameter: the argument converts to its source, closed as
        # the source is written (`Source<int>` takes no `Source<string>`), and
        # its target converts to the parameter. The declaring type's own
        # parameters are bound before either is judged, from the parameter
        # its target closes (`operator Wrap<T>(T)` takes a Widget to a
        # Wrap<Base> as a Base) and from the argument its source closes
        # (`operator T(Box<T>)` gives a Box<Widget> a Widget).
        source, target = operator
        owned = self._owned_parameters(declaring)
        matched, bindings = self._target_bindings(target, wanted[0], owned)
        bindings = bindings if matched else {}
        source = substitute(source, bindings)
        source_qn = self._first_party_type(type_shape(source)[0])
        if source_qn is None:
            return (
                None if matched is None and mentioned_names(source) & owned else False
            )
        takes = self._class_conversion(given, (source, source_qn), owned)
        if takes is False:
            return False
        bound = substitute(
            target,
            {**bindings, **self._operator_bindings(given, (source, source_qn), owned)},
        )
        gives = self._operator_gives(bound, wanted, owned)
        if takes and gives:
            return True
        return False if gives is False else None

    def _operator_gives(
        self,
        target: str,
        wanted: tuple[tuple[str, str], Collection[str]],
        owned: frozenset[str],
    ) -> bool | None:
        # Whether an operator's target, its declaring type's parameters bound,
        # converts to the parameter; one still naming them is open.
        target_qn = self._first_party_type(type_shape(target)[0])
        if target_qn is None:
            return None if mentioned_names(target) & owned else False
        wanted_type, open_names = wanted
        gives = self._class_conversion((target, target_qn), wanted_type, open_names)
        if gives is False and mentioned_names(target) & owned:
            return None if wanted_type[1] in self._class_ancestry(target_qn) else False
        return gives

    def _owned_parameters(self, declaring: str) -> frozenset[str]:
        # The type parameters an operator's declaring type declares.
        shape = self.csharp_generic_shapes.get(declaring)
        return frozenset(shape.parameters) if shape is not None else frozenset()

    def _target_bindings(
        self, target: str, wanted: tuple[str, str], owned: frozenset[str]
    ) -> tuple[bool | None, dict[str, str]]:
        # Whether an operator's target is the parameter's type or derives
        # from it, closed as the parameter is (None when that closing is not
        # known), with the declaring type's own parameters that closing binds:
        # `Wrap<T>` for a `Wrap<Base>` binds T := Base.
        target_qn = self._first_party_type(type_shape(target)[0])
        if target_qn is None or wanted[1] not in self._class_ancestry(target_qn):
            return False, {}
        closing = self._closing_through((target, target_qn), wanted[1])
        if closing is None:
            return None, {}
        bound: Inferred = {}
        matched = arguments_match(type_arguments(wanted[0]), closing, owned, bound)
        return matched, _exact(bound)

    def _operator_bindings(
        self, given: tuple[str, str], source: tuple[str, str], owned: frozenset[str]
    ) -> dict[str, str]:
        # The declaring type's own parameters an operator's source spells
        # bare (`Source<U>`), bound to what the argument passes there.
        closing = self._closing_through(given, source[1]) or ()
        return {
            written: passed
            for written, passed in zip(type_arguments(source[0]), closing, strict=False)
            if written in owned
        }

    def _closing_through(
        self, target: tuple[str, str], param_qn: str
    ) -> tuple[str, ...] | None:
        # The type arguments a target type passes `param_qn`'s parameters:
        # its own written ones when it is that class, otherwise what its base
        # lists pass down (`Derived : Base<int>`, or `Deep : Mid<int>` with
        # `Mid<U> : Base<U>`; PR 2814 review). None when one is not bound.
        written, target_qn = target
        shape = self.csharp_generic_shapes.get(target_qn)
        parameters = shape.parameters if shape is not None else ()
        arguments = type_arguments(written)
        bindings = (
            dict(zip(parameters, arguments, strict=True))
            if len(parameters) == len(arguments)
            else {}
        )
        for current, bound, _open in self._typed_hierarchy(target_qn, bindings):
            if current != param_qn:
                continue
            base = self.csharp_generic_shapes.get(current)
            closed = base.parameters if base is not None else ()
            if not all(p in bound for p in closed):
                return None
            return tuple(bound[p] for p in closed)
        return None

    def scope_of(
        self, type_qn: str, member_parameters: Collection[str] = ()
    ) -> Scope | None:
        """Where a declaration in `type_qn` (a member declaring
        `member_parameters` itself, or the type's own base list) resolves the
        type names it writes: the type's file, with every type parameter in
        scope there left as written (PR 2814 review). None when that file is
        not recorded, which leaves its spellings unresolved."""
        module_qn = self.csharp_class_owner_module.get(type_qn)
        if module_qn is None:
            return None
        return Scope(
            partial(self._resolve_in, module_qn),
            self._type_parameters_in_scope(type_qn) | frozenset(member_parameters),
            self._is_reference,
        )

    def _type_parameters_in_scope(self, type_qn: str) -> frozenset[str]:
        # The type parameters of `type_qn` and of every type it is nested in.
        names: set[str] = set()
        current = type_qn
        while self.function_registry.get(current) in _TYPE_DECLS:
            if (shape := self.csharp_generic_shapes.get(current)) is not None:
                names.update(shape.parameters)
            current = current.rpartition(cs.SEPARATOR_DOT)[0]
        return frozenset(names)

    def _caller_scope(self, call: _MemberCall) -> Scope:
        # Where a call site resolves the types it writes: the caller's file,
        # with the caller's own type parameters and its type's in scope.
        in_scope = self._method_type_parameters(call.caller_qn or "")
        if (class_qn := self._containing_class_qn(call.caller_qn)) is not None:
            in_scope |= self._type_parameters_in_scope(class_qn)
        # The declarations around the call add the `where` clauses, and a
        # local function's own type parameters.
        bounds = _type_parameter_bounds_at(call.node)
        return Scope(
            partial(self._resolve_in, call.module_qn),
            in_scope | frozenset(bounds),
            self._is_reference,
            tuple(bounds.items()),
        )

    def _scope_at(self, node: Node, module_qn: str) -> Scope:
        # Where an expression resolves the types it writes: its file, with
        # the type parameters around it in scope, which a type of the file
        # of the same name does not stand for, and their `where` clauses.
        bounds = _type_parameter_bounds_at(node)
        return Scope(
            partial(self._resolve_in, module_qn),
            frozenset(bounds),
            self._is_reference,
            tuple(bounds.items()),
        )

    def _resolve_in(self, module_qn: str, name: str, arity: int) -> str | None:
        # The first-party type `name` names in `module_qn`, one partial part
        # standing for every part. A simple name the file cannot see the type
        # by names another (PR 2814's Roslyn oracle: `Widget` in a file that
        # imports an unindexed namespace, beside a `Widget` it does not
        # import), whose type is not known.
        qn = self._type_name_to_qn(name, module_qn, arity)
        if qn is None or not self._named_from(module_qn, name, qn):
            return None
        return min(self._partial_roots(qn))

    def _named_from(self, module_qn: str, written: str, type_qn: str) -> bool:
        # Whether a file's spelling names the type it resolved to: a
        # qualified or imported spelling says where the type is, a simple
        # one only when C# finds the type from the file.
        simple = split_type_ref(written)[0]
        if (
            cs.SEPARATOR_DOT in simple
            or cs.SEPARATOR_DOUBLE_COLON in simple
            or simple in self.import_processor.csharp_usings(module_qn)
        ):
            return True
        return self._visibility(module_qn, type_qn) != _NOT_VISIBLE

    def _visibility(self, module_qn: str, type_qn: str) -> int | None:
        # Where C# finds a type a file names by its simple name, nearest
        # first: in the file or a namespace enclosing it, in the global
        # namespace, in a namespace a `using` of the file or a global one
        # imports, or not at all (`_NOT_VISIBLE`). None when the index cannot
        # tell: a nested type, a file whose namespaces were not scanned, or a
        # file of several namespaces where the type's encloses one of them.
        if any(
            part.startswith(f"{module_qn}{cs.SEPARATOR_DOT}")
            for part in self._partial_roots(type_qn)
        ):
            return _ENCLOSING
        imports = self.import_processor
        namespaced = self.csharp_class_namespaced.get(type_qn)
        declared = imports.csharp_declared_namespaces(module_qn)
        if namespaced is None or declared is None:
            return None
        namespace = namespaced.rpartition(cs.SEPARATOR_DOT)[0]
        if namespace in self.csharp_namespaced_qns:
            return None
        if not namespace:
            return _GLOBAL_NAMESPACE
        if any(d == namespace or d.startswith(f"{namespace}.") for d in declared):
            # A file of several namespaces: which one encloses the name is
            # not known here.
            return _ENCLOSING if len(declared) == 1 else None
        imported = set(imports.csharp_usings(module_qn).values())
        for usings in imports.csharp_global_usings.values():
            imported |= usings
        return _IMPORTED if namespace in imported else _NOT_VISIBLE

    def _all_indexed(self, ancestry: Collection[str]) -> bool:
        # Whether every type of an ancestry is one the index declares: one it
        # only names (an external base, one excluded from indexing) may hold
        # what the walk is looking for.
        return all(self.function_registry.get(qn) in _TYPE_DECLS for qn in ancestry)

    def _class_ancestry(self, class_qn: str) -> set[str]:
        seen: set[str] = set()
        queue = deque(self._partial_roots(class_qn))
        while queue:
            current = queue.popleft()
            if current in seen:
                continue
            seen.add(current)
            for base in self.class_inheritance.get(current, []):
                queue.extend(self._partial_roots(base))
        return seen

    # An exact-arity match ANYWHERE up the hierarchy (_find_arity_across_parts)
    # wins before any same-name fallback, so an inherited correct-arity overload
    # (`Base.Foo(int, int)`) is not lost to a wrong-arity same-name method
    # (`Derived.Foo(int)`). resolve_csharp_method_call sequences the two around
    # the extension-method lookup so an arity-correct extension beats a
    # lone-same-name instance fallback. Both phases span every part of a partial
    # class (and each part's bases), so a member/base on another part binds.
    def _partial_roots(self, class_qn: str) -> list[str]:
        return self.csharp_partial_groups.get(class_qn) or [class_qn]

    def _find_arity_across_parts(
        self, class_qn: str, method_name: str, arg_count: int
    ) -> str | None:
        seen: set[str] = set()
        for root in self._partial_roots(class_qn):
            if resolved := self._find_method_by_arity(
                root, method_name, arg_count, seen
            ):
                return resolved
        return None

    def _find_arity_matches_across_parts(
        self, class_qn: str, method_name: str, arg_count: int
    ) -> list[str]:
        # ALL exact-arity same-name overloads across partial parts and
        # bases (the single-hit variant returns the first, which is
        # arbitrary when same-arity twins exist).
        seen: set[str] = set()
        out: list[str] = []
        for root in self._partial_roots(class_qn):
            self._collect_arity_matches(root, method_name, arg_count, seen, out)
        return out

    def _find_bare_call_matches(
        self, class_qn: str, method_name: str, arg_count: int, generic_call: bool
    ) -> list[str]:
        # A bare call no longer falls to the name-wide trie (#2005), so an
        # overload bound through a default or a `params` tail must be found
        # here; exact arity still wins when the type has one. An explicit
        # type argument (`M<int>(1)`) cannot bind a non-generic method, so a
        # generic overload of either tier is taken before a non-generic exact
        # match (CodeRabbit, PR #2036); with no known generic overload the
        # tiers stand, since genericness is only recorded for parsed methods.
        exact = self._find_arity_matches_across_parts(class_qn, method_name, arg_count)
        compatible = self._find_compatible_matches(class_qn, method_name, arg_count)
        if generic_call:
            for tier in (exact, compatible):
                if generic := [m for m in tier if m in self.csharp_generic_methods]:
                    return generic
        return exact or compatible

    def _find_compatible_matches(
        self, class_qn: str, method_name: str, arg_count: int
    ) -> list[str]:
        # Nearest parameter count first, the overload C#'s betterness rules
        # favour (fewest defaults filled, fewest `params` elements).
        seen: set[str] = set()
        out: list[str] = []
        for root in self._partial_roots(class_qn):
            self._collect_arity_matches(
                root, method_name, arg_count, seen, out, compatible=True
            )
        return sorted(out, key=lambda qn: abs(_arity(qn) - arg_count))

    def _collect_arity_matches(
        self,
        class_qn: str,
        method_name: str,
        arg_count: int,
        seen: set[str],
        out: list[str],
        compatible: bool = False,
    ) -> None:
        if class_qn in seen:
            return
        seen.add(class_qn)
        prefix = f"{class_qn}{cs.SEPARATOR_DOT}"
        out.extend(
            qn
            for qn in self._direct_same_name_methods(class_qn, method_name)
            if (
                _accepts_arg_count(
                    qn[len(prefix) :], arg_count, self.csharp_call_shapes.get(qn)
                )
                if compatible
                else _arity(qn[len(prefix) :]) == arg_count
            )
        )
        for base_qn in self.class_inheritance.get(class_qn, []):
            self._collect_arity_matches(
                base_qn, method_name, arg_count, seen, out, compatible
            )

    def csharp_same_arity_family(self, method_qn: str) -> list[str]:
        # Signature-suffixed siblings of a resolved bare call that differ
        # only in parameter TYPES: a switch-arm dispatch
        # (`FormatExact(i, o)` / `FormatExact(s, o)`) is untypeable by
        # arity alone, so the whole same-arity family stays reachable.
        if cs.CHAR_PAREN_OPEN not in method_qn:
            return []
        base = method_qn.split(cs.CHAR_PAREN_OPEN, 1)[0]
        if cs.SEPARATOR_DOT not in base:
            return []
        class_qn, name = base.rsplit(cs.SEPARATOR_DOT, 1)
        return [
            qn
            for qn in self._find_arity_matches_across_parts(
                class_qn, name, _arity(method_qn)
            )
            if qn != method_qn
        ]

    def argument_parameter_type(
        self,
        argument: Node,
        local_var_types: dict[str, str] | None,
        module_qn: str,
        caller_qn: str | None,
    ) -> str | None:
        """The declared type of the parameter a positional argument binds.

        C# types a target-typed `new(...)` passed as an argument from the
        parameter of the overload the call resolves to (issue #2579). Only a
        type every same-arity overload agrees on is an answer. A named
        argument, an extension method (whose receiver shifts the positions)
        or an unresolved call gives None.
        """
        arg_list = argument.parent
        invocation = arg_list.parent if arg_list is not None else None
        if (
            argument.child_by_field_name(cs.FIELD_NAME) is not None
            or arg_list is None
            or arg_list.type != cs.TS_CSHARP_ARGUMENT_LIST
            or invocation is None
            or invocation.type != cs.TS_CSHARP_INVOCATION_EXPRESSION
        ):
            return None
        arguments = [c for c in arg_list.children if c.type == cs.TS_CSHARP_ARGUMENT]
        position = next(
            (i for i, arg in enumerate(arguments) if arg.id == argument.id), None
        )
        callee = self.resolve_csharp_method_call(
            invocation, local_var_types, module_qn, caller_qn
        )
        if position is None or callee is None or callee == CSHARP_EXTERNAL_TARGET:
            return None
        method_qn = callee[1]
        if any(
            entry[0] == method_qn
            for entries in self.csharp_extension_methods.values()
            for entry in entries
        ):
            return None
        declared = {
            _parameter_type_at(qn, position)
            for qn in (method_qn, *self.csharp_same_arity_family(method_qn))
        }
        return declared.pop() if len(declared) == 1 else None

    def csharp_method_group_family(self, name: str, caller_qn: str | None) -> list[str]:
        # Every same-name METHOD of the caller's enclosing type (across
        # partial parts and base classes): a bare method-group pass binds
        # the enclosing type's method group, and which overload the
        # delegate selects is invisible to syntax, so the whole family is
        # referenced. Properties are excluded (a method group never names
        # a property).
        class_qn = self._containing_class_qn(caller_qn)
        if class_qn is None:
            return []
        return self._method_group_on(class_qn, name)

    def csharp_member_group_argument(
        self,
        arg_node: Node,
        local_var_types: dict[str, str],
        module_qn: str,
        caller_qn: str | None,
    ) -> list[str]:
        """The methods a `recv.Name` argument names as a method group.

        Only on a receiver the engine can type, and only METHODS: a property
        read is a value, and an untyped receiver (a foreach variable over a
        BCL collection, a BCL value) names nothing, where the simple-name
        fallback bound whichever first-party `Name` sat nearest (issue #1998).
        """
        if arg_node.type != cs.TS_CSHARP_MEMBER_ACCESS_EXPRESSION:
            return []
        receiver = arg_node.child_by_field_name(cs.TS_CSHARP_FIELD_EXPRESSION)
        name = safe_decode_text(arg_node.child_by_field_name(cs.FIELD_NAME))
        if receiver is None or not name:
            return []
        name = name.split(cs.CHAR_ANGLE_OPEN, 1)[0]
        if receiver.type == cs.TS_CSHARP_BASE:
            # `base.Handle`: the method group on the base chain (bot review).
            own = self._containing_class_qn(caller_qn)
            return sorted(
                {
                    qn
                    for root in self._partial_roots(own or "")
                    for base in self.class_inheritance.get(root, [])
                    for qn in self._method_group_on(base, name)
                }
            )
        class_qn = self._resolve_receiver_class_qn(
            receiver, local_var_types, module_qn, caller_qn
        )
        if class_qn is None and receiver.type == cs.TS_CSHARP_MEMBER_ACCESS_EXPRESSION:
            # `Outer.Inner.Go`, `Lib.Util.Helper`: a dotted receiver the
            # receiver typing does not cover is a TYPE written with its
            # enclosing type or namespace, but only when a registered type
            # sits at exactly that written path. The last segment alone
            # would also bind `Config.Default.Handle`, a property value
            # chain, to an unrelated `Default` type (bot review).
            class_qn = self._dotted_type_path_qn(
                safe_decode_text(receiver) or "", module_qn
            )
        if class_qn is None:
            return []
        return self._method_group_on(class_qn, name)

    def _dotted_type_path_qn(self, dotted: str, module_qn: str) -> str | None:
        # A written type path (`Outer.Inner`, `myLib.Util.Helper`, generic
        # arguments stripped from EVERY segment, so `Lib.Util<int>.Helper`
        # keeps its leaf) names a registered type whose qn ends with the
        # WHOLE path at a segment boundary; a namespace's case is no signal.
        written = strip_generic_arguments(dotted)
        if not written:
            return None
        if self.function_registry.get(written) in _TYPE_DECLS:
            return written
        simple = written.rsplit(cs.SEPARATOR_DOT, 1)[-1]
        suffix = f"{cs.SEPARATOR_DOT}{written}"
        candidates = [
            qn
            for qn in self.simple_name_lookup.get(simple, set())
            if self.function_registry.get(qn) in _TYPE_DECLS
            # A same-file twin carries a duplicate marker (`Helper@12`)
            # after the path; the leaf's arity then picks between them.
            # Matched as `@<digits>`, never a bare `@`: a C# verbatim
            # identifier (`Lib.@Helper`) carries a leading `@` that IS part
            # of the name, and splitting at the first one truncated the
            # candidate to `Lib.`, rejecting the real type (Copilot, #1998).
            and qn_markers.strip_all_markers(qn).endswith(suffix)
        ]
        # The leaf's own arity picks between same-name twins; the leaf is
        # cut at the last dot outside generic arguments, so a qualified type
        # argument (`Helper<System.String>`) keeps its arity (bot review).
        return self._disambiguate_type_candidates(
            candidates,
            generic_arity_of_type_text(leaf_type_segment(dotted)),
            module_qn,
        )

    def _method_group_on(self, class_qn: str, name: str) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        queue = deque(self._partial_roots(class_qn))
        while queue:
            current = queue.popleft()
            if current in seen:
                continue
            seen.add(current)
            out.extend(
                qn
                for qn in self._direct_same_name_methods(current, name)
                if not self.function_registry.is_property(qn)
            )
            queue.extend(self.class_inheritance.get(current, []))
        return sorted(set(out))

    def _find_name_across_parts(self, class_qn: str, method_name: str) -> str | None:
        seen: set[str] = set()
        for root in self._partial_roots(class_qn):
            if resolved := self._find_method_by_name(root, method_name, seen):
                return resolved
        return None

    def reset_member_index(self) -> None:
        # Read off the registry, so it expires when a re-parse changes that.
        self._methods_by_name.clear()
        self._explicit_by_member.clear()

    def _direct_same_name_methods(self, class_qn: str, method_name: str) -> list[str]:
        # Every bare call asked this, and answering it enumerated every member
        # of the class through the registry trie before filtering on the
        # name, so a class's calls cost O(calls x members) (issue #2453). The
        # class is enumerated once per pass and grouped by name instead.
        return list(self.declared_methods(class_qn).get(method_name, ()))

    def declared_methods(self, class_qn: str) -> Mapping[str, tuple[str, ...]]:
        """The methods `class_qn` itself declares, by name, indexed once per
        pass (issue #2453); an explicit interface implementation is under
        its `IRun#Go` name."""
        by_name = self._methods_by_name.get(class_qn)
        if by_name is None:
            by_name = self._methods_by_name[class_qn] = direct_methods_by_name(
                class_qn, self.function_registry
            )
        return by_name

    def explicit_methods(self, class_qn: str, member: str) -> tuple[str, ...]:
        """The explicit interface implementations `class_qn` declares of a
        member named `member` (`IRun#Go(int)` for `Go`), every interface's,
        grouped once per pass so a call asks for its member alone."""
        by_member = self._explicit_by_member.get(class_qn)
        if by_member is None:
            grouped: dict[str, list[str]] = {}
            for name, qns in self.declared_methods(class_qn).items():
                interface, separator, leaf = name.rpartition(
                    cs.CSHARP_EXPLICIT_IMPL_SEPARATOR
                )
                if separator and interface:
                    grouped.setdefault(leaf, []).extend(qns)
            by_member = self._explicit_by_member[class_qn] = {
                leaf: tuple(qns) for leaf, qns in grouped.items()
            }
        return by_member.get(member, ())

    def _find_method_by_arity(
        self, class_qn: str, method_name: str, arg_count: int, seen: set[str]
    ) -> str | None:
        if class_qn in seen:
            return None
        seen.add(class_qn)
        prefix = f"{class_qn}{cs.SEPARATOR_DOT}"
        for qn in self._direct_same_name_methods(class_qn, method_name):
            if _arity(qn[len(prefix) :]) == arg_count:
                return qn
        for base_qn in self.class_inheritance.get(class_qn, []):
            if resolved := self._find_method_by_arity(
                base_qn, method_name, arg_count, seen
            ):
                return resolved
        return None

    def _find_method_by_name(
        self, class_qn: str, method_name: str, seen: set[str]
    ) -> str | None:
        if class_qn in seen:
            return None
        seen.add(class_qn)
        same_name = self._direct_same_name_methods(class_qn, method_name)
        if len(same_name) == 1:
            return same_name[0]
        for base_qn in self.class_inheritance.get(class_qn, []):
            if resolved := self._find_method_by_name(base_qn, method_name, seen):
                return resolved
        return None

    # --- ast helpers ------------------------------------------------------

    def _local_variable_declarations(self, scope_node: Node) -> list[Node]:
        # Every variable_declaration lexically in this method's own scope,
        # pruning nested callables (lambdas, local functions, anonymous
        # methods): their locals belong to a separate scope and must not leak
        # into or shadow the enclosing method's type map.
        found: list[Node] = []
        stack = list(scope_node.children)
        while stack:
            current = stack.pop()
            if current.type in cs.TS_CSHARP_NESTED_SCOPE_TYPES:
                continue
            if current.type == cs.TS_CSHARP_VARIABLE_DECLARATION:
                found.append(current)
            stack.extend(current.children)
        return found
