from __future__ import annotations

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
from ..utils import safe_decode_text
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
    canonical_type,
    constant_converts,
    declared_type,
    fit,
    framework_generic,
    framework_reference,
    infer_into,
    is_tuple,
    literal_fit,
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
    generic_arity_of_type_text,
    leaf_type_segment,
    local_binder,
    signature_type_name,
    split_type_ref,
    strip_generic_arguments,
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


def _literals_fit(leaf: str, literals: list[_Constant | None]) -> bool:
    # Could each literal argument bind its parameter? A parameter that names
    # no predefined type (`literal_fit`), and a non-literal argument, are
    # not judged; a `params` tail beyond the declared count is not either.
    return all(
        literal is None
        or literal_fit(param_type, literal.type, literal.value) is not None
        for param_type, literal in zip(_param_types(leaf), literals, strict=False)
    )


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
    # type argument the syntax spells.
    if argument is None or argument.name is None:
        return argument
    arguments = (
        tuple(canonical_type(a, scope) for a in argument.arguments)
        if argument.arguments is not None
        else None
    )
    return ArgumentType(
        argument.literal,
        canonical_type(argument.name, scope, argument.arity),
        argument.arity,
        arguments,
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


def _spelled_type(value: Node) -> str | None:
    # The type an expression spells as its own: a construction's, a cast's
    # or an `as`'s. Any other expression's type is its result's, which only
    # resolving it tells, so a construction among a call's arguments is never
    # the type of the call (`var x = Factory.Make(new Input())` is no
    # Input; Greptile review of PR 2814).
    while value.type == cs.TS_PARENTHESIZED_EXPRESSION and value.named_children:
        value = value.named_children[0]
    if value.type in (
        cs.TS_CSHARP_OBJECT_CREATION_EXPRESSION,
        cs.TS_CSHARP_CAST_EXPRESSION,
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
    if owner.type == cs.TS_CSHARP_MEMBER_ACCESS_EXPRESSION:
        found = _this_member_type(owner)
    elif owner.type != cs.TS_CSHARP_IDENTIFIER:
        return None
    elif binds_local(owner, name := safe_decode_text(owner) or ""):
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
        "_typing_initializers",
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
        # The same-arity overloads a member call could not choose between
        # beyond the one it returned, under the memo's key, so the call pass
        # can fan the call out to them (issue #2619). Kept beside the memo
        # rather than recomputed: the memo may answer the call node long
        # after the ranking ran for it as a chained receiver.
        self._overload_families: dict[tuple[str, str | None, int, int], list[str]] = {}
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

    def csharp_member_overload_family(
        self, call_node: Node, module_qn: str, caller_qn: str | None
    ) -> list[str]:
        """The other overloads a member call fits as well as the one it
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
        # Resolution order matters: an EXACT-ARITY instance method wins, then
        # one that takes the arguments through defaults or a `params` tail
        # (C# looks at extensions only once no instance member applies, so
        # `c.Foo()` binds `C.Foo(int value = 0)` over `Foo(this C)`;
        # CodeRabbit, PR 2814), then an (always arity-exact) extension
        # method, and only then the instance name-only fallback. Trying the
        # name-only fallback before extensions would bind `c.Foo(1)` to a
        # lone `C.Foo()` and never reach the arity-correct
        # `static Foo(this C, int)` extension.
        if arity_target := self._arity_member_target(member_call, receiver_class_qn):
            return arity_target
        if compatible_target := self._compatible_member_target(
            member_call, receiver_class_qn
        ):
            return compatible_target
        # An extension method (`static M(this T x, ...)` on an unrelated static
        # class) whose `this` receiver type matches the call's receiver; the
        # only path that binds `x.M()` to a method not in x's hierarchy.
        if ext := self._try_extension_call(
            receiver,
            local_var_types or {},
            module_qn,
            caller_qn,
            method_name,
            arg_count,
        ):
            return cs.NodeLabel.METHOD.value, ext
        if fallback_target := self._fallback_member_target(
            member_call, receiver_class_qn
        ):
            return fallback_target
        return self._external_or_unresolved(
            receiver,
            method_name,
            receiver_class_qn,
            local_var_types or {},
            caller_qn,
        )

    def _arity_member_target(
        self, member_call: _MemberCall, receiver_class_qn: str | None
    ) -> tuple[str, str] | None:
        """An exact-arity instance method of the receiver's class, if any."""
        if receiver_class_qn is None:
            return None
        arity_hit = self._find_arity_across_parts(
            receiver_class_qn, member_call.method_name, member_call.arg_count
        )
        if not arity_hit:
            return None
        # Same-arity overloads are told apart by argument type; the first
        # arity match stands when the ranking scored none (issue #2619). One
        # that scored them all inapplicable (`Log(string)` for `x.Log(1)`)
        # leaves the call to the defaulted and extension tiers, as C# does
        # (CodeRabbit, PR 2814); an argument nothing types never proves that.
        ranked = self._rank_member_overloads(
            member_call, receiver_class_qn, compatible=False
        )
        if ranked is not None and not ranked:
            return None
        arity_hit = self._pick_ranked(member_call, ranked or []) or arity_hit
        # A delegate-typed PROPERTY registers as a METHOD node with a bare
        # (arity-0) qn, so a 0-arg invoke slips through the ARITY path too:
        # `entry.Callback()` is Delegate.Invoke, not a call to the property.
        return self._method_or_property_target(arity_hit)

    def _compatible_member_target(
        self, member_call: _MemberCall, receiver_class_qn: str | None
    ) -> tuple[str, str] | None:
        """The receiver's class member that takes a call no overload's
        arity matches, through defaulted parameters or a `params` tail."""
        if receiver_class_qn is None:
            return None
        # A call that leaves defaulted parameters out (`v.ValidateAsync(p)`
        # for `ValidateAsync(T, CancellationToken = default)`) matches no
        # arity exactly. Ranked by argument type among the overloads that
        # accept it, it is not left to a name-only guess, which used to land
        # on whatever shared the name, an explicit implementation included.
        if defaulted_hit := self._pick_member_overload(
            member_call, receiver_class_qn, compatible=True
        ):
            return self._method_or_property_target(defaulted_hit)
        return None

    def _fallback_member_target(
        self, member_call: _MemberCall, receiver_class_qn: str | None
    ) -> tuple[str, str] | None:
        """The receiver's class member a call binds to by name alone, once no
        instance overload applies and no extension method took it."""
        if receiver_class_qn is None:
            return None
        if name_hit := self._find_name_across_parts(
            receiver_class_qn, member_call.method_name
        ):
            # A delegate-typed PROPERTY invoked with call syntax
            # (`options.ShouldHandle(args)`) is Delegate.Invoke, not a method
            # call; binding the property as a METHOD fabricates an edge. Its
            # reachability comes from the read pass.
            return self._method_or_property_target(name_hit)
        return None

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
        for class_qn in self._caller_class_candidates(caller_qn, module_qn):
            matches = self._find_bare_call_matches(
                class_qn, name, arg_count, generic_call
            )
            if matches:
                preferred = [
                    m
                    for m in matches
                    if (m in self.csharp_generic_methods) == generic_call
                ]
                return cs.NodeLabel.METHOD.value, (preferred or matches)[0]
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
        return self._resolve_via_static_imports(
            name, call_node, arg_count, generic_call, module_qn, caller_qn
        )

    def _resolve_via_static_imports(
        self,
        name: str,
        call_node: Node,
        arg_count: int,
        generic_call: bool,
        module_qn: str,
        caller_qn: str | None,
    ) -> tuple[str, str] | None:
        # The imported types' same-name methods form ONE method group, so a
        # call is ambiguous only when overloads from two types still fit it.
        # Arity is checked first, then literal arguments (`M(1)` fits
        # `M(int)`, not `M(string)`, CodeRabbit, PR #2036); what remains
        # across two types is refused rather than picked from, since the
        # values are a set and an arbitrary pick would differ per run.
        # Same-arity overloads on ONE type are a family, not an ambiguity,
        # and are picked from as the enclosing-type tier above picks.
        type_qns: list[str] = []
        for static_type, context_qn in self._static_import_scope(module_qn, caller_qn):
            # The directive stores the C# type path (`Helpers.MathHelpers`);
            # the registry keys types by project-qualified qn, so resolve it
            # the same way a written type reference is resolved, from the
            # file that wrote it.
            type_qn = self._type_name_to_qn(static_type, context_qn)
            if type_qn and type_qn not in type_qns:
                type_qns.append(type_qn)
        literal_types = self._literal_argument_types(call_node)
        families = self._static_import_families(
            type_qns, name, arg_count, generic_call, literal_types
        )
        if len(families) == 1:
            (matches,) = families
            preferred = [
                m for m in matches if (m in self.csharp_generic_methods) == generic_call
            ]
            return cs.NodeLabel.METHOD.value, (preferred or matches)[0]
        return None

    def _static_import_families(
        self,
        type_qns: list[str],
        name: str,
        arg_count: int,
        generic_call: bool,
        literal_types: list[_Constant | None],
    ) -> list[list[str]]:
        # Exact arity first, as C# prefers the unexpanded form; only when no
        # imported type has one may a defaulted or `params` overload bind.
        for compatible in (False, True):
            families = [
                fitting
                for type_qn in type_qns
                if (
                    fitting := [
                        qn
                        for qn in self._static_member_matches(
                            type_qn, name, arg_count, compatible
                        )
                        if _literals_fit(self._method_leaf(qn), literal_types)
                    ]
                )
            ]
            if generic_call:
                generic = [
                    kept
                    for family in families
                    if (kept := [m for m in family if m in self.csharp_generic_methods])
                ]
                families = generic or families
            if families:
                return families
        return []

    def _static_member_matches(
        self, type_qn: str, name: str, arg_count: int, compatible: bool
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
                if (
                    _accepts_arg_count(leaf, arg_count, shape)
                    if compatible
                    else _arity(leaf) == arg_count
                ):
                    out.append(qn)
        return sorted(out, key=lambda qn: abs(_arity(qn) - arg_count))

    @staticmethod
    def _method_leaf(qn: str) -> str:
        # The signatured leaf of a method qn: dots inside the parameter list
        # (`M(System.Int32)`) are not separators.
        head, paren, params = qn.partition(cs.CHAR_PAREN_OPEN)
        return f"{head.rsplit(cs.SEPARATOR_DOT, 1)[-1]}{paren}{params}"

    def _literal_argument_types(self, call_node: Node) -> list[_Constant | None]:
        # The constant each argument that is a bare literal is, else None.
        arg_list = call_node.child_by_field_name(cs.FIELD_ARGUMENTS)
        if arg_list is None:
            return []
        out: list[_Constant | None] = []
        for arg in arg_list.children:
            if arg.type != cs.TS_CSHARP_ARGUMENT:
                continue
            value = arg.named_children[-1] if arg.named_children else None
            out.append(None if value is None else _literal_constant(value))
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

    def _try_extension_call(
        self,
        receiver: Node,
        local_var_types: dict[str, str],
        module_qn: str,
        caller_qn: str | None,
        method_name: str,
        arg_count: int,
    ) -> str | None:
        if not self.csharp_extension_methods:
            return None
        type_name = self._receiver_type_name(
            receiver, local_var_types, module_qn, caller_qn
        )
        if not type_name:
            return None
        return self._find_extension_method(
            type_name,
            method_name,
            arg_count,
            self._receiver_type_arity(receiver, local_var_types, module_qn, caller_qn),
        )

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
        # `((Widget)o).Ext()`: the cast target IS the receiver type, so an
        # extension-only method still binds on a cast receiver; same for a
        # `new Widget(...)` receiver.
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
            return self._this_field_receiver_type(receiver, caller_qn)
        if receiver.type == cs.TS_CSHARP_IDENTIFIER:
            return self._local_result_type(
                receiver, local_var_types, module_qn, caller_qn
            ) or self._identifier_receiver_type(receiver, local_var_types, caller_qn)
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
            return typed(initializer, local_var_types, *context)
        finally:
            self._typing_initializers.discard(span)

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
        if receiver.type in (
            cs.TS_CSHARP_OBJECT_CREATION_EXPRESSION,
            cs.TS_CSHARP_CAST_EXPRESSION,
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

    def _find_extension_method(
        self,
        receiver_type_name: str,
        method_name: str,
        arg_count: int,
        receiver_arity: int | None = None,
    ) -> str | None:
        candidates = self.csharp_extension_methods.get(method_name)
        if not candidates:
            return None
        recv_simple = receiver_type_name.rsplit(cs.SEPARATOR_DOT, 1)[-1]
        recv_qualified = cs.SEPARATOR_DOT in receiver_type_name
        # An UNqualified receiver whose simple name maps to more than one
        # registered first-party type (`N1.Widget` vs `N2.Widget`) is
        # genuinely ambiguous, since we can't tell which one it is, so an
        # unqualified-vs-unqualified match must not guess. A qualified receiver
        # or a BCL name (not registered) is not affected.
        ambiguous_unqualified = not recv_qualified and self._same_arity_type_twins(
            recv_simple
        )
        matches: list[str] = []
        for qn, recv_type, ext_namespace, cand_recv_arity in candidates:
            # A receiver of KNOWN written arity never binds an extension
            # declared for the other generic twin (`new Builder<int>()`
            # cannot take a `this Builder` extension).
            if receiver_arity is not None and cand_recv_arity != receiver_arity:
                continue
            # `_arity` reads the first `(`/last `)`, so pass the whole qn; a
            # leaf-split on `.` would land inside a qualified param type.
            if _arity(qn) != arg_count + 1:
                continue
            if recv_type.rsplit(cs.SEPARATOR_DOT, 1)[-1] != recv_simple:
                continue
            if _extension_receiver_matches(
                receiver_type_name, recv_type, ext_namespace, ambiguous_unqualified
            ):
                matches.append(qn)
        # Bind only on a unique match; an ambiguous name across static classes
        # is left unresolved rather than guessed.
        return matches[0] if len(matches) == 1 else None

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
        if raw := safe_decode_text(receiver):
            head, separator, tail = raw.partition(cs.SEPARATOR_DOT)
            if (
                separator
                and cs.SEPARATOR_DOUBLE_COLON not in raw
                and head in local_var_types
            ):
                return self._field_chain_qn(local_var_types[head], tail, module_qn)
            if qualified := self._qualified_type_name_to_qn(raw, module_qn):
                return qualified
        return None

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
            return self._type_name_to_qn(type_name, module_qn)
        # A `var` local a call initializes is of the type the call returns,
        # as a chained call's receiver is (Greptile review of PR 2814).
        if result := self._call_result(
            self._invocation_receiver_qn,
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
                return self._type_name_to_qn(ftype, module_qn)
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
        import_map = self.import_processor.import_mapping.get(module_qn)
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
        import_map = self.import_processor.import_mapping.get(module_qn)
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
        # Prefer a candidate in the calling file's module; ambiguity across
        # unrelated files is left unresolved rather than guessed.
        same_module = [q for q in candidates if q.startswith(f"{module_qn}.")]
        return same_module[0] if len(same_module) == 1 else None

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

    def _pick_member_overload(
        self, call: _MemberCall, class_qn: str, compatible: bool
    ) -> str | None:
        """The overload a member call binds, or None when none applies.

        Overloads its arguments fit equally well are kept for the call pass,
        which fans the call out to all of them.
        """
        return self._pick_ranked(
            call, self._rank_member_overloads(call, class_qn, compatible) or []
        )

    def _pick_ranked(self, call: _MemberCall, ranked: list[str]) -> str | None:
        # The best of `ranked`; the rest, fitting as well, are kept for the
        # call pass to fan out over.
        if len(ranked) > 1:
            self._overload_families[
                self._call_key(call.node, call.module_qn, call.caller_qn)
            ] = ranked[1:]
        return ranked[0] if ranked else None

    def _rank_member_overloads(
        self, call: _MemberCall, class_qn: str, compatible: bool
    ) -> list[str] | None:
        """The overloads a member call fits best, best first.

        Only exact-arity overloads compete unless `compatible`, which admits
        those that take the argument count through defaults or a `params`
        tail. Declaring types are visited from the receiver up, as the arity
        walk visits them, and the first holding an overload the arguments can
        bind decides: C# drops a base's methods once a derived one applies.
        Each type is seen with the type arguments its subclasses pass it, so
        `Validator<T>.Validate(T)` takes a Person on a `PersonValidator :
        Inline<Person>`, and the receiver's own with the ones its written
        type passes (`new Handler<Person>()`). Empty when every overload
        scored is provably inapplicable; None when none was scored at all.
        """
        arguments: list[ArgumentType | None] | None = None
        # Every type the call site writes (its arguments', its receiver's
        # and its own type arguments) is resolved once, in the caller's file.
        caller = self._caller_scope(call)
        spelled = tuple(canonical_type(a, caller) for a in call.type_arguments)
        for declaring, bindings, open_names in self._typed_hierarchy(
            class_qn, self._receiver_bindings(call.receiver, class_qn, caller)
        ):
            qns = self._candidate_methods(declaring, call, compatible, len(spelled))
            if not qns:
                continue
            if arguments is None:
                arguments = [
                    _canonical_argument(argument, caller)
                    for argument in self._argument_types(
                        call.node, call.local_var_types, call.module_qn, call.caller_qn
                    )
                ]
            if best := self._best_overloads(
                qns, arguments, (bindings, open_names, spelled)
            ):
                return best
        return None if arguments is None else []

    def _best_overloads(
        self,
        qns: Sequence[str],
        arguments: list[ArgumentType | None],
        receiver: tuple[Mapping[str, str], frozenset[str], tuple[str, ...]],
    ) -> list[str]:
        # The overloads of one declaring type no other one is better than,
        # each scored in every form C# may call it in; a method scored in
        # both its forms is one candidate.
        scored = [
            self._score_overload(qn, form, arguments, receiver)
            for qn in qns
            for form in self._call_parameter_forms(qn, len(arguments))
        ]
        return list(dict.fromkeys(best_candidates(scored, self._better_target)))

    def _candidate_methods(
        self, declaring: str, call: _MemberCall, compatible: bool, spelled: int
    ) -> list[str]:
        # The overloads of `declaring` the call can bind by its argument
        # count, and, when it spells type arguments, only the methods that
        # declare as many type parameters: `c.M<long>(1)` binds no `M(int)`.
        qns = self._applicable_methods(
            declaring, call.method_name, call.arg_count, compatible
        )
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
        form: tuple[list[str], bool],
        arguments: list[ArgumentType | None],
        receiver: tuple[Mapping[str, str], frozenset[str], tuple[str, ...]],
    ) -> Scored:
        # How each argument binds its parameter of `method_qn` in one form
        # (its parameters, whether expanded), given the type arguments the
        # receiver binds, the parameters it leaves open and the type
        # arguments the call spells, with what C# compares it by (`Scored`).
        # A type parameter the method declares itself shadows its class's of
        # the same name (`Handler<T>.Handle<T>(T)`), so the receiver's
        # binding of the class's never reaches it. The parameters and the
        # bounds are both read through the receiver and the call
        # (`declared_type`), and what they leave unbound is open in either.
        parameters, expanded = form
        reading = self._reading(method_qn, receiver)
        seen = [
            declared_type(parameter, reading.scope, reading.visible)
            for parameter in parameters
        ]
        inferred = self._inferred_fits(method_qn, seen, arguments, reading)
        return Scored(
            method_qn,
            inferred.fits,
            [substitute(parameter, inferred.fixed) for parameter in seen],
            bool(reading.own),
            expanded,
            [canonical_type(p, reading.scope) for p in _param_types(method_qn)],
            reading.scope.type_parameters if reading.scope else reading.own,
            inferred.settled,
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
        if bound[1] not in self._ancestor_closure(given[1])[0]:
            return False
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

    def _call_parameter_forms(
        self, method_qn: str, arg_count: int
    ) -> list[tuple[list[str], bool]]:
        # The parameters the arguments bind, in each form C# may call the
        # method in, and whether that form is the expanded one. One argument
        # for a `params T[]` tail is the array itself (normal form) or a
        # single `T` (expanded form): `M(1)` binds `M(params int[])` as an
        # int, better than boxing it to `M(object)` (PR 2814 review), where
        # `M(arr)` passes the array.
        types = _param_types(method_qn)
        expandable = bool(types) and self._is_variadic(method_qn, types)
        if not expandable or arg_count != len(types):
            return [(self._call_parameters(method_qn, arg_count), expandable)]
        expanded = [*types[:-1], types[-1].removesuffix(cs.CSHARP_ARRAY_SUFFIX)]
        return [(types, False), (expanded, True)]

    def _is_variadic(self, method_qn: str, types: list[str]) -> bool:
        shape = self.csharp_call_shapes.get(method_qn)
        if shape is not None:
            return shape.variadic
        return bool(types) and types[-1].endswith(cs.CSHARP_ARRAY_SUFFIX)

    def _call_parameters(self, method_qn: str, arg_count: int) -> list[str]:
        # The parameter each of `arg_count` arguments binds, one per argument,
        # so every overload is scored on every argument. A call that expands
        # a `params T[]` tail passes its trailing arguments as `T`; one that
        # leaves defaulted parameters out binds the leading ones. A position
        # no parameter takes (an overload that cannot apply) is unknown.
        types = _param_types(method_qn)
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
        self, class_qn: str, method_name: str, arg_count: int, compatible: bool
    ) -> list[str]:
        # A line-suffixed twin (`M(int)@12`, a `#if` branch) is the same
        # overload; the call pass reaches it through the registry variants.
        prefix = f"{class_qn}{cs.SEPARATOR_DOT}"
        found = [
            qn
            for qn in self._direct_same_name_methods(class_qn, method_name)
            if _arity(leaf := qn[len(prefix) :]) == arg_count
            or (
                compatible
                and _accepts_arg_count(leaf, arg_count, self.csharp_call_shapes.get(qn))
            )
        ]
        spelled = set(found)
        return [
            qn
            for qn in found
            if (natural := qn_markers.strip_dup_marker(qn)) == qn
            or natural not in spelled
        ]

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
        arg_list = call_node.child_by_field_name(cs.FIELD_ARGUMENTS)
        if arg_list is None:
            return []
        arguments = [c for c in arg_list.children if c.type == cs.TS_CSHARP_ARGUMENT]
        # A named argument may stand at any position, so with one present no
        # argument is matched to a parameter by where it is written.
        if any(a.child_by_field_name(cs.FIELD_NAME) is not None for a in arguments):
            return [None] * len(arguments)
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
        if value.type in (
            cs.TS_CSHARP_OBJECT_CREATION_EXPRESSION,
            cs.TS_CSHARP_CAST_EXPRESSION,
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
            constant = self._local_constant(value)
            if constant is not None:
                return ArgumentType(constant.type, None, 0, None, constant.value)
            return _written_argument(declared)
        return self._member_argument(value, local_var_types, module_qn, caller_qn)

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

    def _wraps_nullable(self, given: tuple[str, str], wanted: tuple[str, str]) -> bool:
        # `Widget` to `Widget?`: the implicit nullable conversion of a value.
        nullable = f"{type_shape(given[0])[1]}{cs.CSHARP_NULLABLE_SUFFIX}"
        return type_shape(wanted[0])[1] == nullable and self._same_class(
            given[1], wanted[1]
        )

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
        if given_shape != wanted_shape:
            return False
        given_qn = self._first_party_type(given_core)
        wanted_qn = self._first_party_type(wanted_core)
        if given_qn is not None and wanted_qn is not None:
            if not self._same_class(given_qn, wanted_qn):
                return False
        elif not same_type_name(argument.name or "", parameter, argument.arity):
            return False
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
        varies = self._variance_converts(given_text, parameter, open_names)
        if varies is not None:
            return varies
        given_qn = self._first_party_type(type_shape(given_text)[0])
        wanted_qn = self._first_party_type(type_shape(parameter)[0])
        if given_qn is not None and wanted_qn is None:
            return self._to_predefined((given_text, given_qn), parameter)
        if (
            given_qn is None
            or wanted_qn is None
            or not self._judgeable(given_qn, wanted_qn)
        ):
            return None
        given = (given_text, given_qn)
        wanted = (parameter, wanted_qn)
        if self._wraps_nullable(given, wanted):
            return True
        derived = self._class_conversion(given, wanted, open_names, inferred)
        if derived:
            return True
        implicit = self._converts_implicitly(given, wanted, open_names)
        if implicit:
            return True
        return None if None in (derived, implicit) else False

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
        if source_qn and target_qn and self._judgeable(source_qn, target_qn):
            return self._class_conversion((source, source_qn), (target, target_qn), ())
        return self._variance_converts(source, target, ())

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
        if wanted_qn not in self._class_ancestry(given[1]):
            return False
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
        return self._any_operator(
            self._class_ancestry(given[1]) | self._class_ancestry(wanted[1]),
            lambda operator, declaring: self._operator_applies(
                operator, declaring, given, (wanted, open_names)
            ),
            False,
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
        known = all(self.function_registry.get(q) in _TYPE_DECLS for q in ancestry)
        return self._any_operator(
            ancestry,
            lambda operator, declaring: self._operator_to_predefined(
                given, operator, declaring, keyword
            ),
            False if known else None,
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
        return Scope(
            partial(self._resolve_in, call.module_qn), in_scope, self._is_reference
        )

    def _resolve_in(self, module_qn: str, name: str, arity: int) -> str | None:
        # The first-party type `name` names in `module_qn`, one partial part
        # standing for every part.
        qn = self._type_name_to_qn(name, module_qn, arity)
        return min(self._partial_roots(qn)) if qn is not None else None

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

    def _direct_same_name_methods(self, class_qn: str, method_name: str) -> list[str]:
        prefix = f"{class_qn}{cs.SEPARATOR_DOT}"
        matches: list[str] = []
        for qn, node_type in self.function_registry.find_with_prefix(class_qn):
            if node_type != NodeType.METHOD or not qn.startswith(prefix):
                continue
            leaf = qn[len(prefix) :]
            base = leaf.split(cs.CHAR_PAREN_OPEN, 1)[0]
            # Directly on this class only (a nested class's method has an extra
            # dot in its name portion).
            if cs.SEPARATOR_DOT in base or base != method_name:
                continue
            matches.append(qn)
        return matches

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
