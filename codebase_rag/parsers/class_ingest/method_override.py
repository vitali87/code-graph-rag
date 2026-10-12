from __future__ import annotations

from collections import deque
from collections.abc import Callable, Collection, Iterator, Mapping, Sequence
from dataclasses import dataclass
from functools import partial
from itertools import chain
from typing import TYPE_CHECKING

from loguru import logger

from ... import constants as cs
from ... import logs
from ...types_defs import (
    CSharpCallShape,
    CSharpGenericShape,
    NodeType,
    OverloadSignature,
)
from ...utils import qn_markers
from ...utils.cpp_signatures import (
    ClassLookups,
    parameter_types_may_match,
    pick_overload,
)
from ..csharp import utils as csharp_utils
from ..csharp.overloads import (
    DeclaredTypes,
    Scope,
    bindings_for_base,
    member_verdict,
    spells_interface,
    unshadowed,
    with_type_parameters,
    written_bindings,
)
from ..csharp.overloads import TypeScopes as CSharpScopes
from ..utils import direct_methods_by_name

# The graph's type nodes. Every one but a Type alias is a distinct type.
_TYPE_KINDS = frozenset(
    {
        NodeType.CLASS,
        NodeType.TYPE,
        NodeType.ENUM,
        NodeType.UNION,
        NodeType.INTERFACE,
    }
)

if TYPE_CHECKING:
    from ...services import IngestorProtocol
    from ...types_defs import FunctionRegistryTrieProtocol

# (type qn, a member's own type parameters) -> where that declaration
# resolves the type names it writes, or None when its file is not known.
type ScopeOf = Callable[[str, Collection[str]], Scope | None]
# (class qn, method name) -> the methods the class itself declares under it.
type MethodsNamed = Callable[[str, str], Sequence[str]]


@dataclass(frozen=True, slots=True)
class CSharpOverrideFacts:
    """What the override pass reads about C# code, as the class pass
    recorded it: the C# methods and those written `override`, each type's
    generic arity, generic shape and namespaced name, each method's call
    shape, and where a type's declarations resolve the names they write."""

    methods: set[str] | None = None
    override_methods: set[str] | None = None
    class_generic_arity: Mapping[str, int] | None = None
    generic_shapes: Mapping[str, CSharpGenericShape] | None = None
    class_namespaced: Mapping[str, str] | None = None
    call_shapes: Mapping[str, CSharpCallShape] | None = None
    type_scopes: ScopeOf | None = None


def methods_named(function_registry: FunctionRegistryTrieProtocol) -> MethodsNamed:
    """Each class's own methods by name, read off the registry once per class
    asked about (issue #2453): matching every method against each ancestor's
    members by enumerating them all made a pass O(methods x members)."""
    by_class: dict[str, dict[str, tuple[str, ...]]] = {}

    def named(class_qn: str, name: str) -> Sequence[str]:
        found = by_class.get(class_qn)
        if found is None:
            found = by_class[class_qn] = direct_methods_by_name(
                class_qn, function_registry
            )
        return found.get(name, ())

    return named


def process_all_method_overrides(
    function_registry: FunctionRegistryTrieProtocol,
    class_inheritance: dict[str, list[str]],
    ingestor: IngestorProtocol,
    interface_implementers: dict[str, set[str]] | None = None,
    csharp: CSharpOverrideFacts | None = None,
    impl_method_traits: dict[str, str] | None = None,
    inherent_impl_methods: set[str] | None = None,
    module_qns: Collection[str] = (),
    linked_apart: Collection[str] = (),
) -> None:
    # `linked_apart`: the types whose overrides another pass links (Go's
    # interfaces, link_go_interfaces), which neither walk below visits. A
    # walk per type up a chain of embeds was quadratic in its length.
    logger.info(logs.CLASS_PASS_4)

    facts = csharp or CSharpOverrideFacts()
    implemented_interfaces = _invert_implementers(interface_implementers or {})
    members = methods_named(function_registry)
    type_scopes = TypeScopes(function_registry, module_qns)
    for method_qn in function_registry.keys():
        if function_registry[method_qn] != NodeType.METHOD:
            continue
        if _emit_csharp_explicit_override(
            method_qn,
            function_registry,
            (class_inheritance, implemented_interfaces),
            ingestor,
            facts,
            members,
        ):
            continue
        class_qn, method_name = _override_class_and_leaf(method_qn, facts.methods)
        if not class_qn or class_qn in linked_apart:
            continue
        # Positive evidence first: a recorded trait binding names the parent
        # outright, so it outranks a classification drawn from absence,
        # whichever way the two ever disagree.
        if _emit_recorded_impl_override(
            method_qn,
            method_name,
            function_registry,
            ingestor,
            impl_method_traits,
        ):
            continue
        if inherent_impl_methods and method_qn in inherent_impl_methods:
            # Written in an inherent `impl Type` block, so it implements
            # nothing. Rust has no inheritance, and the walk below would
            # otherwise hand it the first trait the type implements that
            # happens to declare this name.
            continue
        check_method_overrides(
            method_qn,
            method_name,
            class_qn,
            function_registry,
            class_inheritance,
            ingestor,
            implemented_interfaces,
            facts,
            members,
            type_scopes,
        )
    _process_mro_shadow_overrides(
        function_registry, class_inheritance, ingestor, linked_apart
    )


def _emit_csharp_explicit_override(
    method_qn: str,
    function_registry: FunctionRegistryTrieProtocol,
    hierarchy: tuple[dict[str, list[str]], dict[str, list[str]]],
    ingestor: IngestorProtocol,
    csharp: CSharpOverrideFacts,
    members: MethodsNamed | None = None,
) -> bool:
    """Link a C# explicit interface implementation, if `method_qn` is one.
    `hierarchy` is each class's bases and the interfaces it implements."""
    if not csharp.methods or method_qn not in csharp.methods:
        return False
    explicit = _csharp_explicit_member(method_qn)
    if explicit is None:
        return False
    class_inheritance, implemented_interfaces = hierarchy
    # Its leaf names the interface it implements; matching that
    # leaf by name, as the walk below does, would find nothing.
    _emit_explicit_impl_override(
        method_qn,
        explicit,
        function_registry,
        _ancestors(explicit[0], class_inheritance, implemented_interfaces),
        csharp.class_generic_arity or {},
        csharp.class_namespaced or {},
        ingestor,
        csharp.generic_shapes or {},
        (csharp.call_shapes or {}, csharp.type_scopes, members),
    )
    return True


def _override_class_and_leaf(
    method_qn: str, csharp_methods: set[str] | None
) -> tuple[str, str]:
    if csharp_methods and method_qn in csharp_methods:
        # A C# signature may spell qualified types (`Put(List<System.
        # String>)`), whose dots are not the class separator.
        return _csharp_class_and_leaf(method_qn)
    # A dotless qn has no class to walk from; rpartition leaves
    # class_qn empty for it, which is the same set the membership
    # test used to skip.
    class_qn, _, method_name = method_qn.rpartition(cs.SEPARATOR_DOT)
    return class_qn, method_name


def _process_mro_shadow_overrides(
    function_registry: FunctionRegistryTrieProtocol,
    class_inheritance: dict[str, list[str]],
    ingestor: IngestorProtocol,
    linked_apart: Collection[str] = (),
) -> None:
    # A mixin's method can shadow a same-name method from a SIBLING base
    # branch only in a combining subclass's MRO: django's
    # SearchVector(SearchVectorCombinable, Func) dispatches Combinable's
    # `self._combine()` to SearchVectorCombinable._combine, yet the mixin
    # never inherits Combinable, so the per-method ancestor walk above
    # cannot see the relation. For every class, linearize its ancestry in
    # reverse post-order (a C3-compatible MRO stand-in) and link each method
    # name's FIRST provider (the runtime dispatch target) to every later
    # provider; dead-code override expansion then revives the shadowing
    # method when the shadowed one has live callers. Interfaces are not
    # walked: default-method shadowing is rare and Java resolves it
    # differently. ponytail: name-exact matching only, so a Java generic
    # type-var rename across branches is not linked.
    method_names_cache: dict[str, list[str]] = {}
    ancestor_cache: dict[str, set[str]] = {}
    emitted: set[tuple[str, str]] = set()
    for class_qn in sorted(class_inheritance):
        if class_qn in linked_apart:
            continue
        providers = _mro_method_providers(
            class_qn, class_inheritance, function_registry, method_names_cache
        )
        for name, classes in providers.items():
            if len(classes) >= 2:
                _emit_sibling_shadows(
                    name, classes, class_inheritance, ancestor_cache, emitted, ingestor
                )


def _emit_sibling_shadows(
    name: str,
    classes: list[str],
    class_inheritance: dict[str, list[str]],
    ancestor_cache: dict[str, set[str]],
    emitted: set[tuple[str, str]],
    ingestor: IngestorProtocol,
) -> None:
    # Same-branch pairs (the provider inherits the shadowed class) are the
    # per-method walk's territory and already linked; this pass adds only
    # cross-branch sibling shadows.
    first = classes[0]
    if first not in ancestor_cache:
        ancestor_cache[first] = set(_linearized_ancestors(first, class_inheritance)[1:])
    first_qn = f"{first}{cs.SEPARATOR_DOT}{name}"
    for shadowed_class in classes[1:]:
        if shadowed_class in ancestor_cache[first]:
            continue
        pair = (first_qn, f"{shadowed_class}{cs.SEPARATOR_DOT}{name}")
        if pair not in emitted:
            emitted.add(pair)
            _emit_shadow_override(ingestor, pair)


def _mro_method_providers(
    class_qn: str,
    class_inheritance: dict[str, list[str]],
    function_registry: FunctionRegistryTrieProtocol,
    method_names_cache: dict[str, list[str]],
) -> dict[str, list[str]]:
    # Method name -> the classes defining it, in the class's MRO order.
    providers: dict[str, list[str]] = {}
    for ancestor_qn in _linearized_ancestors(class_qn, class_inheritance):
        if ancestor_qn not in method_names_cache:
            method_names_cache[ancestor_qn] = _direct_method_names(
                ancestor_qn, function_registry
            )
        for name in method_names_cache[ancestor_qn]:
            providers.setdefault(name, []).append(ancestor_qn)
    return providers


def _emit_shadow_override(ingestor: IngestorProtocol, pair: tuple[str, str]) -> None:
    ingestor.ensure_relationship_batch(
        (cs.NodeLabel.METHOD, cs.KEY_QUALIFIED_NAME, pair[0]),
        cs.RelationshipType.OVERRIDES,
        (cs.NodeLabel.METHOD, cs.KEY_QUALIFIED_NAME, pair[1]),
    )
    logger.debug(
        logs.CLASS_METHOD_OVERRIDE,
        method_qn=pair[0],
        parent_method_qn=pair[1],
    )


def _linearized_ancestors(
    class_qn: str, class_inheritance: dict[str, list[str]]
) -> list[str]:
    # Reverse post-order over the ancestor DAG: a subclass always precedes
    # its bases and a diamond's common ancestor sinks below BOTH branches
    # (D(B, C) with B(A), C(A) linearizes [D, B, C, A], matching the C3
    # MRO), so a shadowed common base can never outrank the sibling branch
    # that shadows it. A plain depth-first preorder gets diamonds backwards
    # and would emit reversed OVERRIDES edges. The `expanded` guard also
    # keeps a malformed inheritance cycle from looping.
    order: list[str] = []
    expanded: set[str] = set()
    stack: list[tuple[str, bool]] = [(class_qn, False)]
    while stack:
        current, processed = stack.pop()
        if processed:
            order.append(current)
            continue
        if current in expanded:
            continue
        expanded.add(current)
        stack.append((current, True))
        stack.extend((base, False) for base in class_inheritance.get(current, []))
    return list(reversed(order))


def _direct_method_names(
    class_qn: str, function_registry: FunctionRegistryTrieProtocol
) -> list[str]:
    prefix = f"{class_qn}{cs.SEPARATOR_DOT}"
    names: list[str] = []
    for qn, node_type in function_registry.find_with_prefix(class_qn):
        if node_type != NodeType.METHOD or not qn.startswith(prefix):
            continue
        leaf = qn[len(prefix) :]
        if cs.SEPARATOR_DOT in leaf.split(cs.CHAR_PAREN_OPEN, 1)[0]:
            continue
        names.append(leaf)
    return names


def _emit_recorded_impl_override(
    method_qn: str,
    method_name: str,
    function_registry: FunctionRegistryTrieProtocol,
    ingestor: IngestorProtocol,
    impl_method_traits: dict[str, str] | None,
) -> bool:
    """Link a method to the trait its own impl block named, if one was recorded.

    The ancestry walk below takes the first implemented trait declaring this
    name, in sorted order. Two traits declaring one name make that a coin toss
    decided by spelling: reversing the impl blocks in the source keeps the same
    edge, now pointing at the trait the method does not implement.
    """
    trait_qn = (impl_method_traits or {}).get(method_qn)
    if trait_qn is None:
        return False
    # Only now, with an impl block vouching for this method, is the dedup
    # suffix safe to drop. Stripping it for every method instead would hand
    # the ancestry walk a name it could not match before, inventing an
    # override for an INHERENT method that merely shares a trait method's
    # name, and would empty out a C# verbatim identifier like `@event`.
    method_name = qn_markers.strip_dup_marker(method_name)
    parent_method_qn = f"{trait_qn}{cs.SEPARATOR_DOT}{method_name}"
    if function_registry.get(parent_method_qn) != NodeType.METHOD:
        # An inherent method, or one the trait declares nowhere: the walk has
        # nothing to be wrong about, so let it run.
        return False
    ingestor.ensure_relationship_batch(
        (cs.NodeLabel.METHOD, cs.KEY_QUALIFIED_NAME, method_qn),
        cs.RelationshipType.OVERRIDES,
        (cs.NodeLabel.METHOD, cs.KEY_QUALIFIED_NAME, parent_method_qn),
    )
    logger.debug(
        logs.CLASS_METHOD_OVERRIDE,
        method_qn=method_qn,
        parent_method_qn=parent_method_qn,
    )
    return True


def _csharp_class_and_leaf(method_qn: str) -> tuple[str, str]:
    # The class is cut before the parameter list, whose qualified types
    # hold dots too; ("", qn) for a qn with no class.
    return qn_markers.owner_and_member(method_qn)


def _csharp_explicit_member(method_qn: str) -> tuple[str, str, str] | None:
    # (class qn, interface as written, member leaf) for an explicit interface
    # implementation, `...Validator.IValidator#Validate(Ctx)`.
    class_qn, leaf = _csharp_class_and_leaf(method_qn)
    if not class_qn:
        return None
    split = csharp_utils.split_explicit_member(leaf)
    if split is None:
        return None
    return class_qn, split[0], split[1]


def _ancestors(
    class_qn: str,
    class_inheritance: dict[str, list[str]],
    implemented_interfaces: dict[str, list[str]],
) -> Iterator[str]:
    # Every base and implemented interface, nearest first, as the override
    # walk visits them.
    queue = deque([class_qn])
    visited = {class_qn}
    while queue:
        current = queue.popleft()
        for parent in chain(
            class_inheritance.get(current, ()), implemented_interfaces.get(current, ())
        ):
            if parent not in visited:
                visited.add(parent)
                queue.append(parent)
                yield parent


def _emit_explicit_impl_override(
    method_qn: str,
    explicit: tuple[str, str, str],
    function_registry: FunctionRegistryTrieProtocol,
    ancestors: Iterator[str],
    generic_arity: Mapping[str, int],
    namespaced: Mapping[str, str],
    ingestor: IngestorProtocol,
    generic_shapes: Mapping[str, CSharpGenericShape],
    lookup: tuple[Mapping[str, CSharpCallShape], ScopeOf | None, MethodsNamed | None],
) -> None:
    """Link `IValidator#Validate(Ctx)` to `IValidator.Validate(Ctx)`.

    The interface is the nearest one the class implements that the
    implementation's spelling names, every written namespace segment and
    the generic arity included (issue #2619). The spelling's type arguments
    bind a generic interface's parameters, so `IValidator<Person>
    #Validate(Person)` links to `Validate(T)` even beside a `Validate(string)`
    that erasure and arity cannot tell it from.
    """
    class_qn, interface, member = explicit
    call_shapes, scope_of, members = lookup
    # The member's types resolve in its class's file; so does the interface
    # spelling, which its class's declaration wrote.
    given = _member_scope(scope_of, class_qn, call_shapes, method_qn)
    spelled_in = _scope(scope_of, class_qn)
    types = DeclaredTypes(namespaced, generic_arity)
    for ancestor in ancestors:
        if function_registry.get(ancestor) != NodeType.INTERFACE:
            continue
        if not spells_interface(interface, ancestor, spelled_in, types):
            continue
        parent_method_qn = _parent_method_qn(
            ancestor,
            member,
            function_registry,
            erase_generics=True,
            bindings=written_bindings(
                interface, generic_shapes.get(ancestor), spelled_in
            ),
            call_shapes=call_shapes,
            scopes=_scopes(given, _scope(scope_of, ancestor)),
            own=_own_parameters(call_shapes, method_qn),
            members=members,
        )
        if parent_method_qn is None:
            continue
        ingestor.ensure_relationship_batch(
            (cs.NodeLabel.METHOD, cs.KEY_QUALIFIED_NAME, method_qn),
            cs.RelationshipType.OVERRIDES,
            (cs.NodeLabel.METHOD, cs.KEY_QUALIFIED_NAME, parent_method_qn),
        )
        logger.debug(
            logs.CLASS_METHOD_OVERRIDE,
            method_qn=method_qn,
            parent_method_qn=parent_method_qn,
        )
        return


def _invert_implementers(
    interface_implementers: dict[str, set[str]],
) -> dict[str, list[str]]:
    # class_inheritance holds only superclasses (an `implements` clause or a
    # Rust `impl Trait for Type` never enters it), so the override walk needs
    # the implementer -> interfaces direction too, or no interface/trait
    # implementation ever gets an OVERRIDES edge. Both loops sorted: the map
    # and its sets are hash-ordered and edge emission must be deterministic
    # (the inner sort makes the dict order deterministic too).
    inverted: dict[str, list[str]] = {}
    for interface_qn, implementer_qns in sorted(interface_implementers.items()):
        for implementer_qn in sorted(implementer_qns):
            inverted.setdefault(implementer_qn, []).append(interface_qn)
    return inverted


def _signature_arity(method_name: str) -> int | None:
    # Number of top-level parameters in a signatured method name
    # (`readField(A,JsonReader,BoundField)` -> 3, `create()` -> 0); None when the
    # name carries no signature (Python/JS methods). Commas inside generics
    # (`Map<K, V>`) are nested, so only depth-0 commas separate parameters.
    open_idx = method_name.find(cs.CHAR_PAREN_OPEN)
    if open_idx < 0:
        return None
    inner = method_name[open_idx + 1 : method_name.rfind(cs.CHAR_PAREN_CLOSE)]
    if not inner.strip():
        return 0
    depth = 0
    count = 1
    for ch in inner:
        if ch in "<([":
            depth += 1
        elif ch in ">)]":
            depth -= 1
        elif ch == "," and depth == 0:
            count += 1
    return count


def _find_override_by_arity(
    parent_class: str,
    method_name: str,
    function_registry: FunctionRegistryTrieProtocol,
    members: MethodsNamed | None = None,
) -> str | None:
    # Override matching by exact signature fails when a subclass renames a generic
    # type parameter (base `readField(A,...)` vs override `readField(T,...)`), which
    # is a distinct qn. Java overriding is by name + erased parameter types, so fall
    # back to a UNIQUE parent method with the same simple name and arity; ambiguous
    # overloads (>1 candidate) are left unmatched rather than guessed.
    arity = _signature_arity(method_name)
    if arity is None:
        return None
    base_name = method_name.split(cs.CHAR_PAREN_OPEN, 1)[0]
    named = members or methods_named(function_registry)
    matches = [
        qn
        for qn in named(parent_class, base_name)
        if _signature_arity(qn[len(parent_class) + 1 :]) == arity
    ]
    return matches[0] if len(matches) == 1 else None


def _find_override_by_erasure(
    parent_class: str,
    method_name: str,
    function_registry: FunctionRegistryTrieProtocol,
    members: MethodsNamed | None = None,
) -> str | None:
    # A C# signature keeps generic arguments, so an override in a closed
    # subclass (`Put(List<int>)` on `Derived : Base<int>`) no longer spells
    # its base's `Put(List<T>)`. With the arguments erased they agree again;
    # a UNIQUE erased match is taken, as the arity fallback takes one.
    erased = csharp_utils.strip_generic_arguments(method_name)
    named = members or methods_named(function_registry)
    matches = [
        qn
        for qn in named(parent_class, method_name.split(cs.CHAR_PAREN_OPEN, 1)[0])
        if csharp_utils.strip_generic_arguments(qn[len(parent_class) + 1 :]) == erased
    ]
    return matches[0] if len(matches) == 1 else None


def _find_override_by_substitution(
    parent_class: str,
    method_name: str,
    function_registry: FunctionRegistryTrieProtocol,
    context: tuple[Mapping[str, str], CSharpScopes | None, Sequence[str] | None],
    call_shapes: Mapping[str, CSharpCallShape],
    members: MethodsNamed | None = None,
) -> tuple[str | None, bool]:
    # `Base<T>.Put(List<T>)` is `Put(List<int>)` on a `Derived : Base<int>`:
    # with the type arguments the subclass passes substituted, the base
    # overload it overrides spells its signature exactly, where erasing the
    # arguments cannot tell `Put(List<T>)` from `Put(List<string>)`. A type
    # parameter the base method declares itself (`M<T>(T)`) shadows the
    # class's, so the subclass's binding never reaches it. With scopes,
    # each side's types resolve in its own file, and its own type
    # parameters match the candidate's by position (`member_verdict`).
    # (the one match, whether every same-named candidate provably differs)
    bindings, scopes, given_own = context
    base_name = method_name.split(cs.CHAR_PAREN_OPEN, 1)[0]
    named = members or methods_named(function_registry)
    matches: list[str] = []
    verdicts: list[bool | None] = []
    for qn in named(parent_class, base_name):
        leaf = qn[len(parent_class) + 1 :]
        own = _own_type_parameters(call_shapes, qn)
        visible = unshadowed(bindings, own)
        # The candidate's own type parameters are in scope where it is
        # declared, and name no type there.
        candidate_scopes = (
            CSharpScopes(
                scopes.given,
                with_type_parameters(
                    scopes.wanted,
                    own,
                    csharp_utils.call_shape_lifts(call_shapes.get(qn), qn),
                )
                or scopes.wanted,
            )
            if scopes is not None
            else None
        )
        verdict = member_verdict(
            method_name,
            leaf,
            visible,
            candidate_scopes,
            (given_own, _own_parameters(call_shapes, qn)),
        )
        verdicts.append(verdict)
        if verdict:
            matches.append(qn)
    # A `#if` twin (`Put(List<T>)@12`) is the same member; the one matched
    # is returned, unmarked first. Two that only spell alike (`M<T>(T)` and
    # `M(T)@4`) are told apart by the shadowing above.
    natural = {qn_markers.strip_dup_marker(qn) for qn in matches}
    found = (
        min(matches, key=lambda qn: (qn != qn_markers.strip_dup_marker(qn), qn))
        if len(natural) == 1
        else None
    )
    return found, bool(verdicts) and all(v is False for v in verdicts)


def _csharp_override_gated(
    method_qn: str,
    csharp_methods: set[str] | None,
    csharp_override_methods: set[str] | None,
) -> bool:
    # A C# method without the `override` modifier must not match a base CLASS
    # member (only interface members).
    if csharp_methods is None or method_qn not in csharp_methods:
        return False
    return csharp_override_methods is None or method_qn not in csharp_override_methods


def _signed_base_overloads(
    parent_class: str,
    method_name: str,
    function_registry: FunctionRegistryTrieProtocol,
) -> list[tuple[str, OverloadSignature]] | None:
    """The base's C++ overloads of this name with their signatures.

    Base overloads are one node each (issue #2455), and a lookup by name finds
    only the plain-named one, whatever its signature: `f(double) override`
    would land on `f(int)`, and an override named `f@13` would find nothing.
    None when there is nothing to choose by signature (no such member, or a
    graph written before signatures were stored), so the name lookup decides
    as it always did.
    """
    natural = (
        f"{parent_class}{cs.SEPARATOR_DOT}{qn_markers.strip_dup_marker(method_name)}"
    )
    overloads: list[tuple[str, OverloadSignature]] = []
    for candidate in function_registry.variants(natural):
        if function_registry.get(candidate) != NodeType.METHOD:
            continue
        known = function_registry.overload_signature(candidate)
        if known is None:
            return None
        overloads.append((candidate, known))
    return overloads or None


class TypeScopes:
    """Where a C++ parameter's type name is looked up, whatever file holds it.

    A qualified name starts with the module of the file that declares it
    (`proj.types.h.lib.Foo`), but C++ finds `Foo` by namespace, wherever the
    header is: a member of `lib::Derived` in derived.h names `lib::Foo` from
    types.h by its namespace path `lib.Foo`. So every type the graph holds is
    indexed by that path, once, on first use (issue #2455).
    """

    def __init__(
        self,
        function_registry: FunctionRegistryTrieProtocol,
        module_qns: Collection[str],
    ) -> None:
        self._registry = function_registry
        self._module_qns = frozenset(module_qns)
        self._kinds: dict[str, set[NodeType]] | None = None

    def namespace_path(self, qualified_name: str) -> str:
        # The qn without its module: the longest prefix naming a module. A
        # qn under no known module is kept whole, so it is at least
        # consistent with the other qns of its file.
        candidate = qualified_name
        while cs.SEPARATOR_DOT in candidate:
            candidate = candidate.rsplit(cs.SEPARATOR_DOT, 1)[0]
            if candidate in self._module_qns:
                return qualified_name[len(candidate) + 1 :]
        return qualified_name

    def is_class(self, name: str, class_qn: str) -> bool:
        """Whether `name`, written in a member of `class_qn`, names a class.

        Looked up from the class outward through each enclosing scope, as
        C++ does, and the innermost declaration decides: a class (or enum or
        union) there is no typedef, and a name found nowhere, or found as a
        Type alias, might be one. A class of the same name in an unrelated
        namespace is never on that path, so it decides nothing.
        """
        kinds_by_path = self._type_kinds()
        dotted = name.replace(cs.SEPARATOR_DOUBLE_COLON, cs.SEPARATOR_DOT)
        scope = self.namespace_path(class_qn)
        while True:
            path = f"{scope}{cs.SEPARATOR_DOT}{dotted}" if scope else dotted
            if (kinds := kinds_by_path.get(path)) is not None:
                return NodeType.TYPE not in kinds
            if not scope:
                return False
            scope = scope.rpartition(cs.SEPARATOR_DOT)[0]

    def _type_kinds(self) -> dict[str, set[NodeType]]:
        if self._kinds is None:
            self._kinds = {}
            for qualified_name, kind in self._registry.items():
                if kind in _TYPE_KINDS:
                    self._kinds.setdefault(
                        self.namespace_path(qualified_name), set()
                    ).add(kind)
        return self._kinds


def _overridden_overload(
    signature: OverloadSignature,
    overloads: list[tuple[str, OverloadSignature]],
    base_class: str,
    derived_class: str,
    type_scopes: TypeScopes,
) -> str | None:
    """The base overload `signature` overrides, if the types allow it at all.

    `pick_overload` settles a respelling by arity when nothing closer
    matches, which pairs a definition with its declaration because C++ makes
    a definition define a declared member. A derived member has no such
    duty: `f(double)` beside a lone base `f(int)` hides it. So a pick that is
    not verbatim stands only when the types could be one through a typedef,
    each side's names read in its own class's scope.
    """
    picked = pick_overload(signature, overloads)
    if picked is None:
        return None
    known = dict(overloads)[picked]
    classes = ClassLookups(
        left=partial(type_scopes.is_class, class_qn=base_class),
        right=partial(type_scopes.is_class, class_qn=derived_class),
    )
    if known.text == signature.text or parameter_types_may_match(
        known.text, signature.text, classes
    ):
        return picked
    return None


def _parent_method_qn(
    parent_class: str,
    method_name: str,
    function_registry: FunctionRegistryTrieProtocol,
    erase_generics: bool = False,
    bindings: Mapping[str, str] | None = None,
    call_shapes: Mapping[str, CSharpCallShape] | None = None,
    scopes: CSharpScopes | None = None,
    own: Sequence[str] | None = None,
    members: MethodsNamed | None = None,
    signature: OverloadSignature | None = None,
    class_qn: str = "",
    type_scopes: TypeScopes | None = None,
) -> str | None:
    """The METHOD on `parent_class` an override of `method_name` would target.

    `bindings` are the type arguments the overriding class passes
    `parent_class`'s type parameters, for a C# override, `call_shapes`
    name the type parameters each C# method declares itself, and `scopes`
    resolve each side's written types in its own file. `own` are the
    overriding member's own type parameters, which match a candidate's by
    position (None when not known). A match those scopes prove impossible
    is not made up by a fallback (PR 2814 review). `members` is the pass's
    index of each class's own methods by name. A C++ override's `signature`
    picks among the base's overloads, its types read in `class_qn` and the
    base through `type_scopes` (issue #2455).
    """
    signed, by_signature = _signed_parent(
        parent_class, method_name, function_registry, (signature, class_qn, type_scopes)
    )
    if signed:
        return by_signature
    named = members or methods_named(function_registry)
    if scopes is not None:
        scoped, proven = _find_override_by_substitution(
            parent_class,
            method_name,
            function_registry,
            (bindings or {}, scopes, own),
            call_shapes or {},
            named,
        )
        if scoped is not None or proven:
            return _method_only(scoped, function_registry)
    parent_method_qn = f"{parent_class}.{method_name}"
    if parent_method_qn not in function_registry:
        parent_method_qn = (
            _respelled_parent(
                parent_class,
                method_name,
                function_registry,
                (bindings, own),
                (erase_generics, call_shapes or {}, named),
            )
            or parent_method_qn
        )
    return _method_only(parent_method_qn, function_registry)


def _respelled_parent(
    parent_class: str,
    method_name: str,
    function_registry: FunctionRegistryTrieProtocol,
    substitution: tuple[Mapping[str, str] | None, Sequence[str] | None],
    lookup: tuple[bool, Mapping[str, CSharpCallShape], MethodsNamed],
) -> str | None:
    # The base member an override spelled otherwise targets: matched with
    # its type parameters bound (`substitution`: the base's bindings and the
    # override's own type parameters), with generics erased when the walk
    # asks for it, else by name and arity, so a generic type-var rename in
    # the override signature still matches the base method.
    bindings, own = substitution
    erase_generics, call_shapes, named = lookup
    if bindings:
        substituted = _find_override_by_substitution(
            parent_class,
            method_name,
            function_registry,
            (bindings, None, own),
            call_shapes,
            named,
        )[0]
        if substituted is not None:
            return substituted
    if erase_generics and (
        erased := _find_override_by_erasure(
            parent_class, method_name, function_registry, named
        )
    ):
        return erased
    return _find_override_by_arity(parent_class, method_name, function_registry, named)


def _signed_parent(
    parent_class: str,
    method_name: str,
    function_registry: FunctionRegistryTrieProtocol,
    cpp: tuple[OverloadSignature | None, str, TypeScopes | None],
) -> tuple[bool, str | None]:
    # (whether a C++ override's signature decides, the base overload it
    # picks). It decides whenever the base's overloads have signatures.
    signature, class_qn, type_scopes = cpp
    if signature is None:
        return False, None
    overloads = _signed_base_overloads(parent_class, method_name, function_registry)
    if not overloads:
        return False, None
    # No settled match is no edge: the plain-named overload is not a
    # default, it is whichever one happened to be seen first.
    return True, _overridden_overload(
        signature,
        overloads,
        parent_class,
        class_qn,
        type_scopes or TypeScopes(function_registry, ()),
    )


def _method_only(
    qn: str | None, function_registry: FunctionRegistryTrieProtocol
) -> str | None:
    # The parent member must BE a method: a ctor of a nested class that
    # inherits its encloser (node::inner_node : node) shares its name with the
    # nested CLASS registered at parent.name, and an OVERRIDES onto that class
    # qn is a label-mismatched phantom.
    if qn is not None and function_registry.get(qn) == NodeType.METHOD:
        return qn
    return None


def check_method_overrides(
    method_qn: str,
    method_name: str,
    class_qn: str,
    function_registry: FunctionRegistryTrieProtocol,
    class_inheritance: dict[str, list[str]],
    ingestor: IngestorProtocol,
    implemented_interfaces: dict[str, list[str]] | None = None,
    csharp: CSharpOverrideFacts | None = None,
    members: MethodsNamed | None = None,
    type_scopes: TypeScopes | None = None,
) -> None:
    implemented = implemented_interfaces or {}
    if class_qn not in class_inheritance and class_qn not in implemented:
        return

    facts = csharp or CSharpOverrideFacts()
    csharp_call_shapes = facts.call_shapes
    # A C# method overrides a base CLASS member only with the explicit
    # `override` modifier; a `new`/implicit-hide member must not. Interface
    # members need no modifier, so gate only class-parent matches.
    csharp_gated = _csharp_override_gated(
        method_qn, facts.methods, facts.override_methods
    )
    is_csharp = facts.methods is not None and method_qn in facts.methods

    queue = deque([class_qn])
    visited = {class_qn}
    # The type arguments each visited C# ancestor's parameters are bound to,
    # in the overriding class's terms (`Derived : Base<int>` binds Base's T).
    bindings_of: dict[str, dict[str, str]] = {class_qn: {}}
    shapes = facts.generic_shapes if is_csharp else None
    scope_of = facts.type_scopes if is_csharp else None
    # The overriding member's types resolve in its own class's file.
    given = _member_scope(scope_of, class_qn, csharp_call_shapes, method_qn)
    # A C++ override's signature, which picks among the base's overloads.
    signature = function_registry.overload_signature(method_qn)

    while queue:
        current_class = queue.popleft()

        parent_method_qn = (
            _parent_method_qn(
                current_class,
                method_name,
                function_registry,
                is_csharp,
                bindings_of.get(current_class),
                csharp_call_shapes,
                _scopes(given, _scope(scope_of, current_class)),
                _own_parameters(csharp_call_shapes, method_qn),
                members,
                signature,
                class_qn,
                type_scopes,
            )
            if current_class != class_qn
            else None
        )
        # Skip a gated C# member's CLASS-parent match, but keep walking: it
        # may still implement an interface member deeper.
        if parent_method_qn is not None and (
            not csharp_gated
            or function_registry.get(current_class) == NodeType.INTERFACE
        ):
            ingestor.ensure_relationship_batch(
                (cs.NodeLabel.METHOD, cs.KEY_QUALIFIED_NAME, method_qn),
                cs.RelationshipType.OVERRIDES,
                (cs.NodeLabel.METHOD, cs.KEY_QUALIFIED_NAME, parent_method_qn),
            )
            logger.debug(
                logs.CLASS_METHOD_OVERRIDE,
                method_qn=method_qn,
                parent_method_qn=parent_method_qn,
            )
            return

        fresh = _unvisited_parents(
            current_class, class_inheritance, implemented, visited
        )
        visited.update(fresh)
        queue.extend(fresh)
        _bind_fresh_ancestors(
            (shapes, facts.class_generic_arity, scope_of),
            current_class,
            fresh,
            bindings_of,
        )


def _unvisited_parents(
    current_class: str,
    class_inheritance: Mapping[str, list[str]],
    implemented: Mapping[str, list[str]],
    visited: set[str],
) -> list[str]:
    # Superclasses first: when both a base class and an interface declare
    # the method, the edge lands on the base, matching Java resolution.
    # chain() instead of list concat: this runs per BFS node per method.
    return [
        parent_class_qn
        for parent_class_qn in dict.fromkeys(
            chain(
                class_inheritance.get(current_class, ()),
                implemented.get(current_class, ()),
            )
        )
        if parent_class_qn not in visited
    ]


def _bind_fresh_ancestors(
    facts: tuple[
        Mapping[str, CSharpGenericShape] | None,
        Mapping[str, int] | None,
        ScopeOf | None,
    ],
    current_class: str,
    fresh: list[str],
    bindings_of: dict[str, dict[str, str]],
) -> None:
    # Each newly queued C# ancestor's parameters, bound in the overriding
    # class's terms through the class that reached it. Each hop's base-list
    # arguments resolve in that hop's own file (PR 2814 review).
    shapes, generic_arity, scope_of = facts
    if not shapes:
        return
    scope = _scope(scope_of, current_class)
    for parent_class_qn in fresh:
        bindings_of[parent_class_qn] = bindings_for_base(
            shapes,
            generic_arity or {},
            current_class,
            bindings_of.get(current_class, {}),
            parent_class_qn,
            scope,
        )


def _own_type_parameters(
    call_shapes: Mapping[str, CSharpCallShape] | None, method_qn: str
) -> tuple[str, ...]:
    return _own_parameters(call_shapes, method_qn) or ()


def _own_parameters(
    call_shapes: Mapping[str, CSharpCallShape] | None, method_qn: str
) -> tuple[str, ...] | None:
    # A C# method's own type parameters, () for none; None when its shape
    # is not known, which leaves them unmatched by position.
    shape = call_shapes.get(method_qn) if call_shapes else None
    return shape.type_parameters if shape is not None else None


def _scope(scope_of: ScopeOf | None, type_qn: str) -> Scope | None:
    # Where a declaration in `type_qn` resolves its types, when that is known.
    return scope_of(type_qn, ()) if scope_of else None


def _member_scope(
    scope_of: ScopeOf | None,
    type_qn: str,
    call_shapes: Mapping[str, CSharpCallShape] | None,
    method_qn: str,
) -> Scope | None:
    # Where a C# method's declaration resolves its types: its type's file,
    # with the type parameters it declares in scope, and what a `?` on each
    # makes it (`nullable_lifts`; Greptile review of PR 2814).
    shape = call_shapes.get(method_qn) if call_shapes else None
    return with_type_parameters(
        _scope(scope_of, type_qn),
        _own_type_parameters(call_shapes, method_qn),
        csharp_utils.call_shape_lifts(shape, method_qn),
    )


def _scopes(given: Scope | None, wanted: Scope | None) -> CSharpScopes | None:
    # Both sides' files, or None when either is not known.
    return CSharpScopes(given, wanted) if given and wanted else None
