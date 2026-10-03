from __future__ import annotations

from collections import deque
from itertools import chain
from typing import TYPE_CHECKING

from loguru import logger

from ... import constants as cs
from ... import logs
from ...types_defs import NodeType, OverloadSignature
from ...utils import qn_markers
from ...utils.cpp_signatures import parameter_types_may_match, pick_overload

if TYPE_CHECKING:
    from ...services import IngestorProtocol
    from ...types_defs import FunctionRegistryTrieProtocol


def process_all_method_overrides(
    function_registry: FunctionRegistryTrieProtocol,
    class_inheritance: dict[str, list[str]],
    ingestor: IngestorProtocol,
    interface_implementers: dict[str, set[str]] | None = None,
    csharp_methods: set[str] | None = None,
    csharp_override_methods: set[str] | None = None,
    impl_method_traits: dict[str, str] | None = None,
    inherent_impl_methods: set[str] | None = None,
) -> None:
    logger.info(logs.CLASS_PASS_4)

    implemented_interfaces = _invert_implementers(interface_implementers or {})
    for method_qn in function_registry.keys():
        if function_registry[method_qn] != NodeType.METHOD:
            continue
        # A dotless qn has no class to walk from; rpartition leaves class_qn
        # empty for it, which is the same set the membership test used to skip.
        class_qn, _, method_name = method_qn.rpartition(cs.SEPARATOR_DOT)
        if not class_qn:
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
            csharp_methods,
            csharp_override_methods,
        )
    _process_mro_shadow_overrides(function_registry, class_inheritance, ingestor)


def _process_mro_shadow_overrides(
    function_registry: FunctionRegistryTrieProtocol,
    class_inheritance: dict[str, list[str]],
    ingestor: IngestorProtocol,
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
    prefix = f"{parent_class}{cs.SEPARATOR_DOT}"
    matches: list[str] = []
    for qn, node_type in function_registry.find_with_prefix(parent_class):
        if node_type != NodeType.METHOD or not qn.startswith(prefix):
            continue
        leaf = qn[len(prefix) :]
        if cs.SEPARATOR_DOT in leaf.split(cs.CHAR_PAREN_OPEN, 1)[0]:
            continue  # a method of a nested class, not directly on parent_class
        if leaf.split(cs.CHAR_PAREN_OPEN, 1)[0] == base_name and (
            _signature_arity(leaf) == arity
        ):
            matches.append(qn)
    return matches[0] if len(matches) == 1 else None


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


def _overridden_overload(
    signature: OverloadSignature,
    overloads: list[tuple[str, OverloadSignature]],
    function_registry: FunctionRegistryTrieProtocol,
) -> str | None:
    """The base overload `signature` overrides, if the types allow it at all.

    `pick_overload` settles a respelling by arity when nothing closer
    matches, which pairs a definition with its declaration because C++ makes
    a definition define a declared member. A derived member has no such
    duty: `f(double)` beside a lone base `f(int)` hides it. So a pick that is
    not verbatim stands only when the types could be one through a typedef.
    """
    picked = pick_overload(signature, overloads)
    if picked is None:
        return None
    known = dict(overloads)[picked]
    if known.text == signature.text or parameter_types_may_match(
        known.text,
        signature.text,
        lambda name: _is_known_class(name, function_registry),
    ):
        return picked
    return None


def _is_known_class(name: str, function_registry: FunctionRegistryTrieProtocol) -> bool:
    # A class the graph holds is no typedef; a name it does not hold, or one
    # it also holds as a Type alias, might be one.
    simple = name.rsplit(cs.SEPARATOR_DOUBLE_COLON, 1)[-1]
    matches = [
        function_registry.get(qn)
        for qn in function_registry.find_ending_with(simple)
        if qn.rsplit(cs.SEPARATOR_DOT, 1)[-1] == simple
    ]
    return bool(matches) and all(kind == NodeType.CLASS for kind in matches)


def _parent_method_qn(
    parent_class: str,
    method_name: str,
    function_registry: FunctionRegistryTrieProtocol,
    signature: OverloadSignature | None = None,
) -> str | None:
    """The METHOD on `parent_class` an override of `method_name` would target."""
    if signature is not None and (
        overloads := _signed_base_overloads(
            parent_class, method_name, function_registry
        )
    ):
        # No settled match is no edge: the plain-named overload is not a
        # default, it is whichever one happened to be seen first.
        return _overridden_overload(signature, overloads, function_registry)
    parent_method_qn = f"{parent_class}.{method_name}"
    if parent_method_qn not in function_registry:
        # Fall back to name+arity so a generic type-var rename in the override
        # signature still matches the base method.
        parent_method_qn = (
            _find_override_by_arity(parent_class, method_name, function_registry)
            or parent_method_qn
        )
    # The parent member must BE a method: a ctor of a nested class that
    # inherits its encloser (node::inner_node : node) shares its name with the
    # nested CLASS registered at parent.name, and an OVERRIDES onto that class
    # qn is a label-mismatched phantom.
    if function_registry.get(parent_method_qn) == NodeType.METHOD:
        return parent_method_qn
    return None


def check_method_overrides(
    method_qn: str,
    method_name: str,
    class_qn: str,
    function_registry: FunctionRegistryTrieProtocol,
    class_inheritance: dict[str, list[str]],
    ingestor: IngestorProtocol,
    implemented_interfaces: dict[str, list[str]] | None = None,
    csharp_methods: set[str] | None = None,
    csharp_override_methods: set[str] | None = None,
) -> None:
    implemented = implemented_interfaces or {}
    if class_qn not in class_inheritance and class_qn not in implemented:
        return

    # A C# method overrides a base CLASS member only with the explicit
    # `override` modifier; a `new`/implicit-hide member must not. Interface
    # members need no modifier, so gate only class-parent matches.
    csharp_gated = _csharp_override_gated(
        method_qn, csharp_methods, csharp_override_methods
    )

    queue = deque([class_qn])
    visited = {class_qn}
    signature = function_registry.overload_signature(method_qn)

    while queue:
        current_class = queue.popleft()

        parent_method_qn = (
            _parent_method_qn(current_class, method_name, function_registry, signature)
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

        # Superclasses first: when both a base class and an interface declare
        # the method, the edge lands on the base, matching Java resolution.
        # chain() instead of list concat: this runs per BFS node per method.
        fresh = [
            parent_class_qn
            for parent_class_qn in dict.fromkeys(
                chain(
                    class_inheritance.get(current_class, ()),
                    implemented.get(current_class, ()),
                )
            )
            if parent_class_qn not in visited
        ]
        visited.update(fresh)
        queue.extend(fresh)
