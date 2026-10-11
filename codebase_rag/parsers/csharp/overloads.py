"""Ranking same-arity C# overloads by the types of a call's arguments.

Arity alone cannot choose between `Validate(T)`, `Validate(Ctx<T>)` and
`Validate(Ctx)`, so a member call used to take whichever came first (issue
#2619). Each argument the syntax can type is matched against each candidate's
parameter, after the receiver's closed type arguments are substituted for the
declaring type's parameters (`T := Person` on `PersonValidator :
Inline<Person>`). C#'s betterness order decides between the fits: an
identity beats a type parameter, which beats a conversion. A candidate the
argument provably cannot bind drops out; what is left undecided stays a set,
for the caller to fan out over.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Collection, Mapping, Sequence
from enum import IntEnum
from typing import NamedTuple, Protocol

from ... import constants as cs
from ...types_defs import CSharpGenericShape
from ...utils import qn_markers
from .utils import (
    generic_arity_of_type_text,
    leaf_type_segment,
    signature_type_name,
    split_type_ref,
    strip_generic_arguments,
)

_TYPE_IDENTIFIER = re.compile(cs.CSHARP_TYPE_IDENTIFIER_PATTERN)
_TYPE_SUFFIX = re.compile(cs.CSHARP_TYPE_SUFFIX_PATTERN)
_TYPE_MODIFIER = re.compile(cs.CSHARP_TYPE_MODIFIER_PATTERN)
_TUPLE_ELEMENT_NAME = re.compile(cs.CSHARP_TUPLE_ELEMENT_NAME_PATTERN)


class ArgumentType(NamedTuple):
    """What the syntax says about one argument: a literal's C# type and,
    for an integral one, its value, or a type's written name, its arity and,
    where the source spells them (`new Ctx<Person>()`), its type arguments."""

    literal: str | None
    name: str | None
    arity: int
    arguments: tuple[str, ...] | None
    value: int | None = None


class Fit(IntEnum):
    # Ordered as C#'s betterness rules rank conversions. UNKNOWN is an
    # argument of a type not known: it ties two candidates whose parameter
    # there is one type, and compares no others, since either may be the
    # better conversion or not apply (PR 2814's ground-truth harness).
    # UNDECIDED is a narrowing constant conversion of a value that may be a
    # constant not read (a field): it applies or not by that value, so the
    # candidate neither beats another nor loses to one (PR 2814 review).
    UNKNOWN = 0
    OTHER = 1
    CONVERTIBLE = 2
    OPEN = 3
    EXACT = 4
    UNDECIDED = 5


class Bound(IntEnum):
    """How inference bounds a type parameter (C#'s exact, lower-bound and
    upper-bound inferences): to one type exactly, from below (the type
    converts to it), or from above (it converts to the type). A class's or
    struct's type argument is inferred into exactly, an interface's or a
    delegate's as its parameter varies (PR 2814 review). CANDIDATE is one
    of the three, not known which (a generic type of no known kind): its
    type is still one the parameter may be fixed to, and nothing more."""

    EXACT = 0
    LOWER = 1
    UPPER = 2
    CANDIDATE = 3


class TypeKinds(NamedTuple):
    """What declarations tell inference of types: the variance of each
    type parameter of the generic type a type names, "out", "in" or "" (None
    when its kind is not known), and whether a type is a reference type
    (None when that is not known)."""

    variances: Callable[[str], tuple[str, ...] | None]
    reference: Callable[[str], bool | None]


# The bounds inference gave each open type parameter: (how, the type).
type Inferred = dict[str, list[tuple[Bound, str]]]


class _Inference(NamedTuple):
    # One argument's inference: the names it infers, the bounds it found,
    # and what declarations tell of types.
    open_names: frozenset[str]
    found: Inferred
    kinds: TypeKinds | None


# (written type name, its type-argument count) -> the first-party type qn it
# names in one declaring file, None when it names none there.
type Resolver = Callable[[str, int], str | None]


class Scope(NamedTuple):
    """One file's view of the type names written in a declaration there:
    `resolve` finds the first-party type a name names in that file, and
    `type_parameters` are the names in scope at the declaration that are
    type parameters (its own, its type's and every enclosing type's), which
    name no type and stay as written (PR 2814 review)."""

    resolve: Resolver
    type_parameters: frozenset[str] = frozenset()


def with_type_parameters(scope: Scope | None, names: Collection[str]) -> Scope | None:
    """`scope` with a member's own type parameters in scope too."""
    if scope is None or not names:
        return scope
    return Scope(scope.resolve, scope.type_parameters | frozenset(names))


class TypeScopes(NamedTuple):
    """Where each side of a member comparison resolves the types it writes:
    the given member's file and the wanted member's (PR 2814 review)."""

    given: Scope
    wanted: Scope


class TypeJudge(Protocol):
    """How one candidate's parameters relate to a call's arguments, each side
    resolved in its own file (the argument in the caller's, the parameter in
    the method's) and with closed type arguments compared alias-blind. Each
    answer is True, False, or None when it cannot be told; never a guess."""

    def identical(
        self, argument: ArgumentType, parameter: str, open_names: Collection[str]
    ) -> bool | None:
        """Whether the argument's type IS the parameter's."""
        ...

    def converts(
        self, argument: ArgumentType, parameter: str, open_names: Collection[str]
    ) -> bool | None:
        """Whether the argument's type converts implicitly to the parameter's."""
        ...

    def is_enum(self, parameter: str) -> bool:
        """Whether the parameter's type, or the value its nullable wraps, is
        a first-party enum."""
        ...


def predefined_keyword(core: str) -> str | None:
    """The keyword of the predefined type a type with no shape after it
    names: `int` for `int`, `Int32`, `System.Int32` and `global::System.Int32`.
    None for any other type, a first-party namesake among them: a name its
    file resolved (`.proj.N.Int32`) or another namespace qualifies
    (`N.Int32`) only shares the framework type's name (PR 2814 review)."""
    if core.startswith(cs.CSHARP_RESOLVED_PREFIX) or cs.CHAR_ANGLE_OPEN in core:
        return None
    path = core.strip().rpartition(cs.SEPARATOR_DOUBLE_COLON)[2]
    if path in cs.CSHARP_PREDEFINED_TYPE_NAMES:
        return path
    return cs.CSHARP_PREDEFINED_TYPE_KEYWORDS.get(
        path.removeprefix(cs.CSHARP_SYSTEM_PREFIX)
    )


def mentioned_names(type_text: str) -> set[str]:
    """Every name a type spells that no dot qualifies:
    `Dictionary<T, N.Widget>` -> {Dictionary, T, N}."""
    return set(_TYPE_IDENTIFIER.findall(type_text))


def substitute(type_text: str, bindings: Mapping[str, str]) -> str:
    """`Ctx<T>` with T := Person -> `Ctx<Person>`. Only a whole simple
    name is replaced, never a qualified segment that happens to match."""
    if not bindings:
        return type_text
    return _TYPE_IDENTIFIER.sub(
        lambda match: bindings.get(match.group(0), match.group(0)), type_text
    )


def declared_type(
    written: str, scope: Scope | None, bindings: Mapping[str, str]
) -> str:
    """A type a member's declaration writes, as a use through a closed
    receiver sees it: resolved once in `scope`, the declaring file, and
    `bindings` (each resolved where it was written) substituted after. Its
    parameters and its `where` bounds are read alike, so on a `C<Person>`
    `where T : U` is `where T : Person` (PR 2814 review)."""
    return substitute(canonical_type(written, scope), bindings)


def unshadowed(
    bindings: Mapping[str, str], declared: Collection[str]
) -> dict[str, str]:
    """`bindings` less the type parameters a generic method `declared`
    itself: in `Handler<T>.Handle<T>(T)` the method's own `T` hides the
    class's, so a binding of the class's never reaches it."""
    return {name: bound for name, bound in bindings.items() if name not in declared}


def bindings_for_base(
    shapes: Mapping[str, CSharpGenericShape],
    generic_arity: Mapping[str, int],
    class_qn: str,
    bindings: Mapping[str, str],
    base_qn: str,
    scope: Scope | None = None,
) -> dict[str, str]:
    """What `class_qn`'s base list passes `base_qn`'s type parameters, in
    `class_qn`'s own bound terms: `Derived : Base<int>` gives Base's T
    `int`. An argument naming one of `class_qn`'s still unbound parameters
    binds nothing: `Inline<T> : Validator<T>` says Validator's T is
    Inline's T, which only a subclass can close. With `scope`, `class_qn`'s
    file, each argument is resolved there before the deeper bindings are
    substituted in, so a chain binds every hop in its own file."""
    shape = shapes.get(class_qn)
    base_shape = shapes.get(base_qn)
    if shape is None or base_shape is None or not base_shape.parameters:
        return {}
    leaf = qn_markers.strip_dup_marker(base_qn.rsplit(cs.SEPARATOR_DOT, 1)[-1])
    name = split_type_ref(leaf)[0]
    arity = generic_arity.get(base_qn, 0)
    written = next(
        (b for b in shape.bases if b.name == name and len(b.arguments) == arity),
        None,
    )
    if written is None:
        return {}
    unbound = set(shape.parameters) - bindings.keys()
    return {
        parameter: declared_type(argument, scope, bindings)
        for parameter, argument in zip(
            base_shape.parameters, written.arguments, strict=False
        )
        if not mentioned_names(argument) & unbound
    }


def written_bindings(
    written: str, shape: CSharpGenericShape | None, scope: Scope | None = None
) -> dict[str, str]:
    """What an explicit implementation's spelling of its interface binds:
    `IMap<Ctx<Person>, int>` for `IMap<K, V>` is K := Ctx<Person> and
    V := int, each resolved in `scope`, the implementing type's file.
    Nothing for a non-generic interface, or for a spelling whose argument
    count is not the interface's."""
    parameters = shape.parameters if shape is not None else ()
    arguments = type_arguments(written)
    if not parameters or len(arguments) != len(parameters):
        return {}
    return {
        parameter: canonical_type(signature_type_name(argument), scope)
        for parameter, argument in zip(parameters, arguments, strict=True)
    }


# Each side's own type parameters, as (the member's, the interface or base
# member's), each None when not known.
type OwnParameters = tuple[Sequence[str] | None, Sequence[str] | None]


def spells_member(
    member: str,
    interface_member: str,
    bindings: Mapping[str, str],
    scopes: TypeScopes | None = None,
    own: OwnParameters | None = None,
) -> bool:
    """Whether `member` (`Validate(Person)`) is `interface_member`
    (`Validate(T)`) once `bindings` (T := Person) are substituted into the
    interface member's parameters, each parameter compared as a type, so
    `Go(System.Int32)` is `Go(int)` (PR 2814 review). Erasing the arguments
    cannot pair the two up beside a `Validate(string)`, nor can counting
    them (issue #2619). With `scopes`, each side's types resolve in its own
    file first, and with `own`, each side's own type parameters match by
    their position (see `member_verdict`). Explicit implementations and
    overrides both use it."""
    return member_verdict(member, interface_member, bindings, scopes, own) is True


def member_verdict(
    member: str,
    interface_member: str,
    bindings: Mapping[str, str],
    scopes: TypeScopes | None,
    own: OwnParameters | None = None,
) -> bool | None:
    """`spells_member` with its reason: True on a match; False when the two
    provably differ, a parameter naming two different resolved types; None
    when they differ only in spelling, which may be one type.

    With `scopes`, every type name each side writes is resolved once, in the
    file that wrote it (`canonical_type`): the member's in its own, the
    interface member's in the interface's, its type parameters left as
    written. `bindings` come resolved already, each in the file whose base
    list wrote it, and are substituted after, so nothing is resolved twice
    or in another file. A name neither side resolves keeps its spelling.

    With `own`, a method's own type parameters are one by declaration
    position, whatever each side names them (Greptile review of PR 2814):
    `M<U>(U)` is `M<T>(T)` and `M<X, Y>(Y, X)` is `M<A, B>(B, A)`, never
    `M<A, B>(A, B)`. Two members declaring as many is a match condition,
    and one's own type parameter is no other type, the other's class type
    parameter `T` among them.
    """
    name = member.partition(cs.CHAR_PAREN_OPEN)[0]
    wanted_name, paren, wanted_params = interface_member.partition(cs.CHAR_PAREN_OPEN)
    given = parameter_types(member)
    if name != wanted_name or len(given) != len(parameter_types(interface_member)):
        return False
    given_own, wanted_own = _matched_own(own)
    if given_own is None or wanted_own is None:
        return False
    given_scope, wanted_scope = scopes if scopes is not None else (None, None)
    given = _by_position([canonical_type(p, given_scope) for p in given], given_own)
    wanted = _by_position(
        [
            declared_type(p, wanted_scope, bindings)
            for p in parameter_types(f"{wanted_name}{paren}{wanted_params}")
        ],
        wanted_own,
    )
    if all(same_type(g, w) for g, w in zip(given, wanted, strict=True)):
        return True
    return (
        False
        if any(
            _proven_apart(g, w) or _apart_by_position(g, w)
            for g, w in zip(given, wanted, strict=True)
        )
        else None
    )


def _matched_own(
    own: OwnParameters | None,
) -> tuple[Sequence[str] | None, Sequence[str] | None]:
    # Each side's own type parameters to name by position: none when either
    # side's are not known; (None, None) when both are and their counts
    # differ, which no match survives.
    given_own, wanted_own = own if own is not None else (None, None)
    if given_own is None or wanted_own is None:
        return (), ()
    if len(given_own) != len(wanted_own):
        return None, None
    return given_own, wanted_own


def _by_position(types: list[str], own: Sequence[str] | None) -> list[str]:
    # Each of a method's own type parameters named by its declaration
    # position (`!!0`), a name no source type spells.
    if not own:
        return types
    renamed = {
        name: f"{cs.CSHARP_METHOD_TYPE_PARAMETER_MARK}{index}"
        for index, name in enumerate(own)
    }
    return [substitute(t, renamed) for t in types]


def _apart_by_position(given: str, wanted: str) -> bool:
    # Whether two types differ where one names a method's own type
    # parameter by position: that is no other type, nor another position.
    if given == wanted:
        return False
    (given_core, given_shape), (wanted_core, wanted_shape) = (
        type_shape(given),
        type_shape(wanted),
    )
    mark = cs.CSHARP_METHOD_TYPE_PARAMETER_MARK
    if given_core.startswith(mark) or wanted_core.startswith(mark):
        return not same_type(given, wanted)
    given_arguments, wanted_arguments = (
        type_arguments(given_core),
        type_arguments(wanted_core),
    )
    if given_shape != wanted_shape or len(given_arguments) != len(wanted_arguments):
        return False
    return any(
        _apart_by_position(g, w)
        for g, w in zip(given_arguments, wanted_arguments, strict=True)
    )


def canonical_type(written: str, scope: Scope | None, arity: int | None = None) -> str:
    """`written` with each first-party type it names spelled as its qn behind
    a leading dot, resolved once, in `scope`, the file that wrote it:
    `List<Widget>[]` -> `List<.proj.A.Widget>[]`, and so are `global::A.Widget`,
    `A.Widget` and an alias of it. Only names are resolved: the shape built
    on them (modifiers, array ranks, nullable and pointer marks, a tuple's
    elements, type arguments) is kept as written, each part canonical in
    turn (PR 2814 review). A type parameter in scope, a name the file does
    not resolve (external, predefined) and a name already resolved keep
    their spelling. `arity` stands for type arguments the text leaves out
    (`Box` of `Box<int>`). Without a scope nothing is resolved."""
    if scope is None:
        return written.strip()
    modifiers = _TYPE_MODIFIER.match(written.strip())
    lead = modifiers.group(0) if modifiers else ""
    core, suffix = type_shape(written.strip().removeprefix(lead))
    return f"{lead}{_canonical_core(core, scope, arity)}{suffix}"


def _canonical_core(core: str, scope: Scope, arity: int | None) -> str:
    # A type with no shape after it: a tuple, or a name and its arguments.
    if core.startswith(cs.CSHARP_RESOLVED_PREFIX) or core in scope.type_parameters:
        return core
    if is_tuple(core):
        elements = cs.SEPARATOR_COMMA_SPACE.join(
            canonical_type(e, scope) for e in _tuple_elements(core)
        )
        return f"{cs.CHAR_PAREN_OPEN}{elements}{cs.CHAR_PAREN_CLOSE}"
    arguments = type_arguments(core)
    name = strip_generic_arguments(core)
    qn = scope.resolve(name, len(arguments) if arity is None else arity)
    head = f"{cs.CSHARP_RESOLVED_PREFIX}{qn}" if qn else name
    if not arguments:
        return head
    inner = cs.SEPARATOR_COMMA_SPACE.join(canonical_type(a, scope) for a in arguments)
    return f"{head}{cs.CHAR_ANGLE_OPEN}{inner}{cs.CHAR_ANGLE_CLOSE}"


def resolved_qn(canonical: str) -> str | None:
    """The qn of the first-party type a `canonical_type` spelling IS, or
    None when it kept a written spelling or builds a shape on the type: an
    array, nullable or pointer of `Widget` is not `Widget` (PR 2814 review)."""
    core, suffix = type_shape(canonical)
    if suffix or not core.startswith(cs.CSHARP_RESOLVED_PREFIX):
        return None
    return strip_generic_arguments(core).removeprefix(cs.CSHARP_RESOLVED_PREFIX)


def type_shape(written: str) -> tuple[str, str]:
    """`List<T>[ ]?` -> ("List<T>", "[]?"): a type and the shape built on it
    after its name (array ranks, nullable and pointer marks), spaced alike
    for every spelling, so two shapes compare as text."""
    text = written.strip()
    match = _TYPE_SUFFIX.search(text)
    if match is None:
        return text, ""
    return text[: match.start()].rstrip(), "".join(match.group(0).split())


def is_tuple(core: str) -> bool:
    """Whether a type with no shape after it is a tuple: `(Widget, int)`."""
    return core.startswith(cs.CHAR_PAREN_OPEN)


def _tuple_elements(core: str) -> list[str]:
    # `(Widget w, int)` -> ["Widget", "int"]: element names are no part of
    # the tuple's type.
    return [_TUPLE_ELEMENT_NAME.sub("", e) for e in parameter_types(core)]


def _array_shape(suffix: str) -> str:
    # A shape less its nullable marks, which a signature may leave out.
    return suffix.replace(cs.CSHARP_NULLABLE_SUFFIX, "")


def _known_type(core: str) -> str | None:
    # What a canonical type with no shape after it is known to name: a
    # resolved first-party type's qn, or a predefined type's keyword, which
    # no qn is. None for a spelling that may be either, or neither.
    return resolved_qn(core) or predefined_keyword(core)


def _proven_apart(given: str, wanted: str) -> bool:
    # Whether two canonical types name, at some matching position, two
    # different known types (first-party or predefined: the project's
    # `Int32` is no `int`, nor `string` one), or one in two array shapes.
    (given_core, given_suffix), (wanted_core, wanted_suffix) = (
        type_shape(given),
        type_shape(wanted),
    )
    if is_tuple(given_core) and is_tuple(wanted_core):
        given_elements, wanted_elements = (
            _tuple_elements(given_core),
            _tuple_elements(wanted_core),
        )
        return (
            len(given_elements) != len(wanted_elements)
            or _array_shape(given_suffix) != _array_shape(wanted_suffix)
            or _any_apart(given_elements, wanted_elements)
        )
    given_qn, wanted_qn = _known_type(given_core), _known_type(wanted_core)
    if given_qn is not None and wanted_qn is not None:
        if given_qn != wanted_qn:
            return True
        if _array_shape(given_suffix) != _array_shape(wanted_suffix):
            return True
    given_arguments, wanted_arguments = (
        type_arguments(given_core),
        type_arguments(wanted_core),
    )
    return _any_apart(given_arguments, wanted_arguments)


def _any_apart(given: Sequence[str], wanted: Sequence[str]) -> bool:
    # Position by position, whether any pair of as many types is proven apart.
    return len(given) == len(wanted) and any(
        _proven_apart(g, w) for g, w in zip(given, wanted, strict=True)
    )


def parameter_types(signature: str) -> list[str]:
    """The depth-0 parameter types of a signatured leaf or qn:
    `M(int, Dictionary<K, V>)` -> ["int", "Dictionary<K, V>"]."""
    open_idx = signature.find(cs.CHAR_PAREN_OPEN)
    if open_idx < 0:
        return []
    inner = signature[open_idx + 1 : signature.rfind(cs.CHAR_PAREN_CLOSE)]
    if not inner.strip():
        return []
    types: list[str] = []
    depth = 0
    start = 0
    for idx, ch in enumerate(inner):
        if ch in cs.CSHARP_SIGNATURE_OPENERS:
            depth += 1
        elif ch in cs.CSHARP_SIGNATURE_CLOSERS:
            depth -= 1
        elif ch == cs.CHAR_COMMA and depth == 0:
            types.append(inner[start:idx].strip())
            start = idx + 1
    types.append(inner[start:].strip())
    return types


def type_arguments(type_text: str) -> tuple[str, ...]:
    """The top-level type arguments of a type's last segment:
    `Map<K, List<V>>` -> ("K", "List<V>"), `Ctx` -> (); a tuple's
    elements are no type arguments."""
    if is_tuple(type_text.strip()):
        return ()
    leaf = leaf_type_segment(type_text)
    open_idx = leaf.find(cs.CHAR_ANGLE_OPEN)
    if open_idx < 0:
        return ()
    arguments: list[str] = []
    depth = 0
    start = open_idx + 1
    for index in range(start, len(leaf)):
        char = leaf[index]
        if char == cs.CHAR_ANGLE_OPEN or char in cs.CSHARP_NESTED_OPEN:
            depth += 1
        elif char in cs.CSHARP_NESTED_CLOSE:
            depth -= 1
        elif char == cs.CHAR_ANGLE_CLOSE:
            if depth == 0:
                arguments.append(leaf[start:index].strip())
                break
            depth -= 1
        elif char == cs.CHAR_COMMA and depth == 0:
            arguments.append(leaf[start:index].strip())
            start = index + 1
    return tuple(arguments)


def _simple_name(type_text: str) -> str:
    return strip_generic_arguments(leaf_type_segment(type_text))


def literal_fit(
    parameter: str, literal: str, value: int | None = None, enum: bool = False
) -> Fit | None:
    """How a literal of C# type `literal` (and, for an integral one, of
    `value`) binds `parameter`, None when it cannot. A predefined type, or
    its nullable, is judged by the keyword it names (`predefined_keyword`),
    so `System.Int32` is an int's identity and a first-party `Int32` is not
    judged that way; an `enum` parameter as C# converts a constant to one.
    A narrowing constant conversion of a constant whose value is not known
    is UNDECIDED, so it never wins or loses alone."""
    core, suffix = type_shape(parameter)
    # An array or pointer (`int[]`) is not judged.
    if suffix.removesuffix(cs.CSHARP_NULLABLE_SUFFIX):
        return Fit.OTHER
    keyword = predefined_keyword(core)
    if keyword is None:
        return _enum_literal_fit(literal, value) if enum else Fit.OTHER
    converts = constant_converts(literal, value, keyword)
    if converts is None:
        return Fit.UNDECIDED
    if not converts:
        return None
    # `int?` takes an int through a nullable conversion, which `int` beats.
    return Fit.EXACT if keyword == literal and not suffix else Fit.CONVERTIBLE


def _enum_literal_fit(literal: str, value: int | None) -> Fit | None:
    # An integral constant zero converts to any enum (C#'s implicit
    # enumeration conversion), and no other literal does: an enum declares
    # no conversion operator. A value not known is not judged.
    if literal not in cs.CSHARP_INTEGRAL_MAX:
        return None
    if value is None:
        return Fit.UNDECIDED
    return Fit.CONVERTIBLE if value == 0 else None


def constant_converts(literal: str, value: int | None, keyword: str) -> bool | None:
    """Whether a constant of predefined type `literal` and, when integral
    and known, `value` converts implicitly to the predefined type `keyword`:
    by identity, to `object`, by an implicit numeric conversion, or by an
    implicit constant conversion its value fits (`300` is no byte, `-1` no
    uint; PR 2814 review). None when only a value not known could tell."""
    if keyword in (literal, cs.CSHARP_OBJECT_KEYWORD):
        return True
    if keyword in cs.CSHARP_IMPLICIT_NUMERIC_CONVERSIONS.get(literal, ()):
        return True
    fits = cs.CSHARP_CONSTANT_CONVERSIONS.get((literal, keyword))
    if fits is None:
        return False
    return None if value is None else fits[0] <= value <= fits[1]


def signed_preference(first: str, second: str) -> int:
    """C#'s better conversion target between two integral types neither
    converts to: +1 when `first` is the signed one of such a pair (`sbyte`
    beside `byte`, `int` beside `uint`), -1 when `second` is, else 0."""
    pair = (predefined_keyword(first) or "", predefined_keyword(second) or "")
    if pair in cs.CSHARP_SIGNED_OVER_UNSIGNED:
        return 1
    return -1 if pair[::-1] in cs.CSHARP_SIGNED_OVER_UNSIGNED else 0


def arguments_match(
    given: Sequence[str] | None,
    wanted: Sequence[str],
    open_names: Collection[str],
    inferred: Inferred | None = None,
    owner: tuple[str, TypeKinds] | None = None,
) -> bool | None:
    """Whether closed type arguments agree, position by position and alias-
    blind (`Box<System.Int32>` is `Box<int>`). An `open_names` parameter in
    a wanted argument is inferred from the given one, never a wildcard for
    the structure around it: `List<T>` takes `List<int>` but no `int`, and
    `Pair<T, T>` no `Pair<int, string>` (PR 2814 review). None when `given`
    is not known, or only a shape inference might absorb tells them apart.

    On a match, the bounds it inferred go into `inferred`, for the call's
    other arguments to agree with (`merged_inferences`). They are exact,
    as a class's type arguments are, unless `owner` names the generic type
    an argument passes them to, with what declarations tell of types: then
    each is bounded as C# infers into that type (`Bound`)."""
    if given is None:
        return None
    if len(given) != len(wanted):
        return False
    kinds = owner[1] if owner is not None else None
    state = _Inference(frozenset(open_names), {}, kinds)
    contexts = (
        _contexts(given, Bound.LOWER, owner[0], kinds)
        if owner is not None
        else [Bound.EXACT] * len(given)
    )
    return _committed(_infer_all(given, wanted, state, contexts), state, inferred)


def infer_into(
    given: str,
    parameter: str,
    open_names: Collection[str],
    inferred: Inferred | None,
    kinds: TypeKinds,
) -> bool | None:
    """Whether an argument of type `given` is `parameter` for some binding
    of `open_names`, inferred from below as C# infers an argument into its
    parameter: `int[]` is `T[]` with `int` bounding T (PR 2814 review). On a
    match, its bounds go into `inferred`."""
    state = _Inference(frozenset(open_names), {}, kinds)
    return _committed(_infer(given, parameter, state, Bound.LOWER), state, inferred)


def _committed(
    verdict: bool | None, state: _Inference, inferred: Inferred | None
) -> bool | None:
    # `verdict`, with the bounds `state` found added to `inferred` on a match.
    if verdict and inferred is not None:
        for name, bounds in state.found.items():
            inferred.setdefault(name, []).extend(bounds)
    return verdict


def merged_inferences(inferences: Sequence[Inferred]) -> Inferred | None:
    """Every bound a call's arguments gave each type parameter; None when
    two exact ones differ: `M<T>(Box<T>, Box<T>)` takes no `(Box<int>,
    Box<string>)` (PR 2814 review). Lower and upper bounds are reconciled
    when the parameter is fixed."""
    merged: Inferred = {}
    for inferred in inferences:
        for name, bounds in inferred.items():
            merged.setdefault(name, []).extend(bounds)
    return merged if all(map(_exact_agree, merged.values())) else None


def _exact_agree(bounds: Sequence[tuple[Bound, str]]) -> bool:
    # Whether the exact bounds among `bounds` are one type.
    exact = [type_text for kind, type_text in bounds if kind is Bound.EXACT]
    return all(t == exact[0] or same_type(t, exact[0]) for t in exact[1:])


def framework_generic(core: str) -> tuple[bool, tuple[str, ...]] | None:
    """(whether it is a reference type, each type parameter's variance) of
    the framework generic type a type with no shape after it names
    (`CSHARP_FRAMEWORK_GENERICS`); None for any other. Only a bare spelling
    or one its namespace qualifies names it, never a resolved one."""
    if core.startswith(cs.CSHARP_RESOLVED_PREFIX):
        return None
    path = strip_generic_arguments(core.rpartition(cs.SEPARATOR_DOUBLE_COLON)[2])
    namespace, _, name = path.rpartition(cs.SEPARATOR_DOT)
    entry = cs.CSHARP_FRAMEWORK_GENERICS.get((name, generic_arity_of_type_text(core)))
    if entry is None or namespace not in ("", entry[0]):
        return None
    return entry[1], entry[2]


def predefined_implements(keyword: str, bound: str) -> bool:
    """Whether the predefined type `keyword` is known to implement the
    framework interface `bound`, closed as written: every one of
    `CSHARP_SELF_INTERFACE_KEYWORDS` implements `IComparable` and
    `IConvertible`, and `IComparable<>` and `IEquatable<>` of itself. False
    says only that the tables do not tell."""
    if keyword not in cs.CSHARP_SELF_INTERFACE_KEYWORDS:
        return False
    path = strip_generic_arguments(bound).rpartition(cs.SEPARATOR_DOUBLE_COLON)[2]
    name = path.removeprefix(cs.CSHARP_SYSTEM_PREFIX)
    arguments = type_arguments(bound)
    if not arguments:
        return name in cs.CSHARP_PREDEFINED_INTERFACES
    return (
        name in cs.CSHARP_PREDEFINED_SELF_INTERFACES
        and len(arguments) == 1
        and predefined_keyword(arguments[0]) == keyword
    )


def framework_reference(core: str) -> bool | None:
    """Whether a framework type with no shape after it is a reference type,
    as far as the tables tell: a known generic one's entry, or a known value
    type (`DateTime`, `Guid`); None for any other."""
    if (generic := framework_generic(core)) is not None:
        return generic[0]
    path = core.rpartition(cs.SEPARATOR_DOUBLE_COLON)[2]
    if path.removeprefix(cs.CSHARP_SYSTEM_PREFIX) in cs.CSHARP_NULLABLE_VALUE_TYPES:
        return False
    return None


def predefined_converts(source: str, target: str) -> bool | None:
    """Whether a value of one type converts implicitly to another, as far
    as predefined types tell: every type converts to `object`, `object` to
    nothing else but `dynamic` (no operator may convert from it), and one
    predefined type to another only by an implicit numeric conversion.
    None when either is another type, which only its declarations tell."""
    origin, destination = predefined_keyword(source), predefined_keyword(target)
    if destination == cs.CSHARP_OBJECT_KEYWORD:
        return True
    if origin == cs.CSHARP_OBJECT_KEYWORD and destination is None:
        return None if target.strip() == cs.CSHARP_DYNAMIC_KEYWORD else False
    if origin is None or destination is None:
        return None
    widened = cs.CSHARP_IMPLICIT_NUMERIC_CONVERSIONS.get(origin, frozenset())
    return origin == destination or destination in widened


def _varied(context: Bound, variance: str, reference: bool | None) -> Bound:
    """How a type argument bounds its parameter where its generic type is
    inferred into from below or above: as the parameter varies (an `out`
    keeps the direction, an `in` turns it), when the argument is a
    reference type; exactly when it is a value type or the parameter does
    not vary. A candidate when whether it is a reference type is not known."""
    if not variance or reference is False:
        return Bound.EXACT
    if reference is None:
        return Bound.CANDIDATE
    if variance == cs.CSHARP_VARIANCE_OUT:
        return context
    return Bound.UPPER if context is Bound.LOWER else Bound.LOWER


def _contexts(
    given: Sequence[str], context: Bound, owner: str, kinds: TypeKinds | None
) -> list[Bound]:
    # How each of the type arguments `given` passes the generic type `owner`
    # bounds, inferred into in `context`. Inside an exact one every position
    # is exact, inside a candidate one a candidate, and a type of no known
    # kind makes each a candidate.
    if context in (Bound.EXACT, Bound.CANDIDATE):
        return [context] * len(given)
    variances = kinds.variances(owner) if kinds is not None else None
    if kinds is None or variances is None or len(variances) != len(given):
        return [Bound.CANDIDATE] * len(given)
    return [
        _varied(context, variance, kinds.reference(argument))
        for variance, argument in zip(variances, given, strict=True)
    ]


def _infer_all(
    given: Sequence[str],
    wanted: Sequence[str],
    state: _Inference,
    contexts: Sequence[Bound],
) -> bool | None:
    # As many types, position by position, under one set of inferences.
    verdict: bool | None = True
    for g, w, context in zip(given, wanted, contexts, strict=True):
        found = _infer(g, w, state, context)
        if found is False:
            return False
        if found is None:
            verdict = None
    return verdict


def _infer(given: str, wanted: str, state: _Inference, context: Bound) -> bool | None:
    # Whether a binding of the open names, agreeing with what this argument
    # inferred exactly so far, makes `wanted` the type `given`. A wanted
    # type with no open name is compared as it is; one with some is taken
    # apart as far as it goes, its bounds recorded as `context` says.
    if not mentioned_names(wanted) & state.open_names:
        return same_type(given, wanted)
    (given_core, given_suffix), (wanted_core, wanted_suffix) = (
        type_shape(given),
        type_shape(wanted),
    )
    if wanted_core in state.open_names:
        return _infer_parameter(
            wanted_core, (given_core, given_suffix), wanted_suffix, (state, context)
        )
    if given_suffix != wanted_suffix:
        return False
    if is_tuple(wanted_core):
        return _infer_tuple(given_core, wanted_core, state, context)
    if not same_type_name(given_core, wanted_core):
        return False
    arguments = type_arguments(given_core)
    return _infer_all(
        arguments,
        type_arguments(wanted_core),
        state,
        _contexts(arguments, context, wanted_core, state.kinds),
    )


def _infer_tuple(
    given: str, wanted: str, state: _Inference, context: Bound
) -> bool | None:
    # A tuple is a struct, so its elements are inferred into exactly.
    given_elements = _tuple_elements(given) if is_tuple(given) else []
    wanted_elements = _tuple_elements(wanted)
    if len(given_elements) != len(wanted_elements):
        return False
    element = Bound.CANDIDATE if context is Bound.CANDIDATE else Bound.EXACT
    return _infer_all(
        given_elements, wanted_elements, state, [element] * len(given_elements)
    )


def _infer_parameter(
    name: str,
    given: tuple[str, str],
    wanted_suffix: str,
    at: tuple[_Inference, Bound],
) -> bool | None:
    # `name` is the given type, its shape and all (`T` of `int[]`), or the
    # given type's core when both build one shape on it (`T[]` of `int[]`),
    # and the same type wherever this argument fixed it exactly before. A
    # shape the given type does not build on (`T[]` of `int`) is no fit;
    # another one may be the parameter's own (`T[]` of `int?[]`), which is
    # not told.
    state, context = at
    core, suffix = given
    if not wanted_suffix:
        core = f"{core}{suffix}"
    elif suffix != wanted_suffix:
        rank = cs.CSHARP_ARRAY_RANK_OPEN
        return None if suffix.count(rank) >= wanted_suffix.count(rank) else False
    else:
        context = _element_bound(context, suffix, core, state.kinds)
    return _bound(name, core, state, context)


def _element_bound(
    context: Bound, suffix: str, element: str, kinds: TypeKinds | None
) -> Bound:
    # How the element of a shape both sides build bounds its parameter: an
    # array's as an `out` type argument does (a reference element varies,
    # a value one is exact), a nullable value's exactly.
    if context in (Bound.EXACT, Bound.CANDIDATE):
        return context
    if cs.CSHARP_ARRAY_RANK_OPEN not in suffix:
        return Bound.EXACT
    reference = kinds.reference(element) if kinds is not None else None
    return _varied(context, cs.CSHARP_VARIANCE_OUT, reference)


def _bound(name: str, type_text: str, state: _Inference, context: Bound) -> bool:
    # Records `type_text` as a `context` bound of `name`: False when it is
    # exact and another exact one this argument gave differs.
    bounds = state.found.setdefault(name, [])
    if context is Bound.EXACT and not _exact_agree([*bounds, (context, type_text)]):
        return False
    bounds.append((context, type_text))
    return True


def written_type(argument: ArgumentType) -> str:
    """An argument's type as written, its type arguments included when the
    syntax spells them: `Box<int>`, or `Box` when they are not known."""
    name = argument.name or ""
    if not argument.arguments:
        return name
    core, suffix = type_shape(name)
    return (
        f"{core}{cs.CHAR_ANGLE_OPEN}"
        f"{cs.SEPARATOR_COMMA_SPACE.join(argument.arguments)}{cs.CHAR_ANGLE_CLOSE}"
        f"{suffix}"
    )


def same_type_name(given: str, wanted: str, given_arity: int | None = None) -> bool:
    """Whether two written types name one class, arguments aside: one
    predefined type however spelled (`int`, `System.Int32`), or the same
    simple name with as many type arguments (`given_arity` when `given`
    does not spell them) and no qualification that sets them apart
    (`A.Widget`, `B.Widget`), in one shape: `Widget[]` is not `Widget`, nor
    `(Widget, int)` `(Widget, string)`. A first-party `N.Int32` is not
    `int` (PR 2814 review)."""
    (given_core, given_suffix), (wanted_core, wanted_suffix) = (
        type_shape(given),
        type_shape(wanted),
    )
    if given_suffix != wanted_suffix:
        return False
    if is_tuple(given_core) or is_tuple(wanted_core):
        return _same_tuple(given_core, wanted_core)
    arity = (
        generic_arity_of_type_text(given_core) if given_arity is None else given_arity
    )
    if arity != generic_arity_of_type_text(wanted_core):
        return False
    if not arity:
        # Where either names a predefined type, both must name that one.
        keywords = {predefined_keyword(given_core), predefined_keyword(wanted_core)}
        if keywords != {None}:
            return len(keywords) == 1
    if _simple_name(given_core) != _simple_name(wanted_core):
        return False
    return not _qualified_apart(given_core, wanted_core)


def _same_tuple(given: str, wanted: str) -> bool:
    # Two tuples are one type when each element is, names aside.
    if not (is_tuple(given) and is_tuple(wanted)):
        return False
    given_elements, wanted_elements = _tuple_elements(given), _tuple_elements(wanted)
    return len(given_elements) == len(wanted_elements) and all(
        same_type(g, w) for g, w in zip(given_elements, wanted_elements, strict=True)
    )


def _type_path(type_text: str) -> list[str]:
    # `global::A.Widget<T>` -> ["A", "Widget"]: the segments a type's
    # spelling qualifies its name with, alias and arguments dropped.
    path = strip_generic_arguments(type_text).rpartition(cs.SEPARATOR_DOUBLE_COLON)[2]
    return path.split(cs.SEPARATOR_DOT)


def _qualified_apart(argument_name: str, parameter: str) -> bool:
    """Whether two spellings of one simple name name two types: `A.Widget`
    and `B.Widget` each qualify it, and neither path ends the other. A bare
    `Widget` may be either, so it never tells them apart (PR 2814 review)."""
    given, wanted = _type_path(argument_name), _type_path(parameter)
    shorter, longer = sorted((given, wanted), key=len)
    return longer[-len(shorter) :] != shorter


def same_type(given: str, wanted: str) -> bool:
    """Whether two written types name one type: `Target<int>` twice,
    `Lib.Person` and `Person`, or `System.Int32` and `int` (PR 2814 review);
    never `Target<int>` and `Target<string>`, nor `A.Widget` and
    `B.Widget`, nor a first-party `N.Int32` and `int`. The one alias-blind
    comparison every path uses."""
    if not same_type_name(given, wanted):
        return False
    return bool(arguments_match(type_arguments(given), type_arguments(wanted), ()))


def fit(
    parameter: str,
    argument: ArgumentType | None,
    open_names: Collection[str],
    judge: TypeJudge,
) -> Fit | None:
    """How well `argument` binds `parameter`, or None when it cannot.

    `open_names` are the type parameters still unbound for this candidate:
    one standing alone takes any argument, as inference would make it, and
    one inside a type argument is inferred from what the argument passes
    there, the structure around it compared (`arguments_match`).
    """
    if argument is None:
        return Fit.UNKNOWN
    if parameter in open_names:
        return Fit.OPEN
    if argument.literal is not None:
        return _literal(parameter, argument, argument.literal, (open_names, judge))
    if argument.name is None:
        return Fit.UNKNOWN
    identical = judge.identical(argument, parameter, open_names)
    if identical:
        return Fit.EXACT
    if identical is None:
        # One class whose type arguments are not known on one side.
        return Fit.OTHER
    related = judge.converts(argument, parameter, open_names)
    if related is None:
        return Fit.OTHER
    return Fit.CONVERTIBLE if related else None


def _literal(
    parameter: str,
    argument: ArgumentType,
    literal: str,
    judged: tuple[Collection[str], TypeJudge],
) -> Fit | None:
    # A literal binds a predefined type or an enum by C#'s constant rules,
    # and any other type only through a user-defined implicit conversion
    # from its type, judged as a typed argument's is (`judge.converts`; PR
    # 2814 review); a better target then weighs it as any conversion.
    open_names, judge = judged
    fitted = literal_fit(parameter, literal, argument.value, judge.is_enum(parameter))
    if fitted is not Fit.OTHER:
        return fitted
    related = judge.converts(argument, parameter, open_names)
    if related is None:
        return Fit.OTHER
    return Fit.CONVERTIBLE if related else None


class Scored(NamedTuple):
    """One candidate as C# weighs it against another (PR 2814 review): how
    each argument binds, the parameter type it binds it to with every fixed
    type argument in, and what breaks a tie between two candidates binding
    the arguments to one parameter types: whether it is a generic method,
    whether it takes them in its expanded form, and its declared parameter
    types with the type parameters in scope where they are written. A
    candidate whose own type parameter inference could not settle is not
    `settled`: its fits hold for some type, not the one every argument must
    agree on, so it neither beats another candidate nor loses to one."""

    qn: str
    fits: Sequence[Fit | None]
    parameters: Sequence[str]
    generic: bool = False
    expanded: bool = False
    declared: Sequence[str] = ()
    type_parameters: frozenset[str] = frozenset()
    settled: bool = True


# (a parameter type, another) an argument converts to both -> +1 when the
# first is C#'s better conversion target, -1 when the second is, 0 when
# neither is known to be.
type TargetOrder = Callable[[str, str], int]


def best_candidates(
    scored: Sequence[Scored], target_order: TargetOrder | None = None
) -> list[str]:
    """The applicable candidates no other applicable one is better than, as
    C# finds a better function member, in their given order; empty when
    none applies. `target_order` ranks two types two known conversions
    reach; without it, such conversions tie."""
    viable = [c for c in scored if None not in c.fits]
    return [
        c.qn
        for c in viable
        if not any(_better(o, c, target_order) for o in viable if o is not c)
    ]


def _better(p: Scored, q: Scored, target_order: TargetOrder | None) -> bool:
    # Whether `p` is the better function member: better for some argument
    # and worse for none, or, binding every argument to one type as `q`
    # does, better by C#'s tie-breaks. Candidates scored on different
    # arguments, or either unsettled or undecided, are never compared.
    if not (p.settled and q.settled):
        return False
    if len(p.fits) != len(q.fits) or len(p.parameters) != len(q.parameters):
        return False
    verdicts = {
        _better_conversion(conversion, target_order)
        for conversion in zip(p.fits, q.fits, p.parameters, q.parameters, strict=True)
    }
    if None in verdicts or -1 in verdicts:
        return False
    if 1 in verdicts:
        return True
    return _equivalent(p.parameters, q.parameters) and _tie_break(p, q) > 0


def _better_conversion(
    conversion: tuple[Fit | None, Fit | None, str, str],
    target_order: TargetOrder | None,
) -> int | None:
    # Which of two conversions of one argument is better: the better fit,
    # or, between two known conversions, the better target. None when one
    # is undecided, which compares with nothing, or the argument's type is
    # not known and the two parameter types differ.
    p_fit, q_fit, p_type, q_type = conversion
    if Fit.UNDECIDED in (p_fit, q_fit):
        return None
    if Fit.UNKNOWN in (p_fit, q_fit):
        return 0 if p_type == q_type or same_type(p_type, q_type) else None
    if p_fit != q_fit:
        return 1 if (p_fit or 0) > (q_fit or 0) else -1
    if p_fit is Fit.CONVERTIBLE and target_order is not None:
        return target_order(p_type, q_type)
    return 0


def _equivalent(given: Sequence[str], wanted: Sequence[str]) -> bool:
    # Whether two parameter type lists are one type, position by position.
    return all(g == w or same_type(g, w) for g, w in zip(given, wanted, strict=True))


def _tie_break(p: Scored, q: Scored) -> int:
    # C#'s tie-breaks, in order: a non-generic method beats a generic one, a
    # normal form an expanded one, and more specific declared parameter
    # types less specific ones. +1 for `p`, -1 for `q`, 0 for neither.
    return (
        int(q.generic) - int(p.generic)
        or int(q.expanded) - int(p.expanded)
        or _more_specific(
            p.declared, q.declared, (p.type_parameters, q.type_parameters)
        )
    )


def _more_specific(
    first: Sequence[str],
    second: Sequence[str],
    type_parameters: tuple[frozenset[str], frozenset[str]],
) -> int:
    # +1 when `first` is more specific, no type less specific and one more,
    # -1 when `second` is, 0 when neither or their lengths differ.
    if len(first) != len(second):
        return 0
    verdicts = {
        _specificity(f, s, type_parameters) for f, s in zip(first, second, strict=True)
    }
    if 1 in verdicts and -1 not in verdicts:
        return 1
    if -1 in verdicts and 1 not in verdicts:
        return -1
    return 0


def _specificity(
    first: str, second: str, type_parameters: tuple[frozenset[str], frozenset[str]]
) -> int:
    # A type parameter is less specific than any other type; an array is
    # as specific as its element, and a constructed type or a tuple more
    # specific by its type arguments or elements (`_more_specific`).
    first_open = first.strip() in type_parameters[0]
    second_open = second.strip() in type_parameters[1]
    if first_open or second_open:
        return int(second_open) - int(first_open)
    (first_core, first_suffix), (second_core, second_suffix) = (
        type_shape(first),
        type_shape(second),
    )
    if first_suffix != second_suffix:
        return 0
    if first_suffix:
        return _specificity(first_core, second_core, type_parameters)
    if is_tuple(first_core) and is_tuple(second_core):
        parts = (_tuple_elements(first_core), _tuple_elements(second_core))
    elif same_type_name(first_core, second_core):
        parts = (list(type_arguments(first_core)), list(type_arguments(second_core)))
    else:
        return 0
    return _more_specific(*parts, type_parameters)
