from __future__ import annotations

from collections import deque
from itertools import chain
from typing import TYPE_CHECKING

from loguru import logger

from ... import constants as cs
from ... import logs
from ...types_defs import NodeType

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
) -> None:
    logger.info(logs.CLASS_PASS_4)

    implemented_interfaces = _invert_implementers(interface_implementers or {})
    for method_qn in function_registry.keys():
        if (
            function_registry[method_qn] == NodeType.METHOD
            and cs.SEPARATOR_DOT in method_qn
        ):
            parts = method_qn.rsplit(cs.SEPARATOR_DOT, 1)
            if len(parts) == 2:
                class_qn, method_name = parts
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
    # reverse post-order (a C3-compatible MRO stand-in) and link each
    # method name's FIRST provider (the runtime dispatch target here)
    # to every later provider; dead-code override expansion then revives
    # the shadowing method when the shadowed one has live callers.
    # Interfaces are not walked: default-method shadowing is rare and
    # Java resolves it differently. ponytail: name-exact matching only, so
    # a Java generic type-var rename across branches is not linked.
    method_names_cache: dict[str, list[str]] = {}
    ancestor_cache: dict[str, set[str]] = {}
    emitted: set[tuple[str, str]] = set()
    for class_qn in sorted(class_inheritance):
        providers: dict[str, list[str]] = {}
        for ancestor_qn in _linearized_ancestors(class_qn, class_inheritance):
            if ancestor_qn not in method_names_cache:
                method_names_cache[ancestor_qn] = _direct_method_names(
                    ancestor_qn, function_registry
                )
            for name in method_names_cache[ancestor_qn]:
                providers.setdefault(name, []).append(ancestor_qn)
        for name, classes in providers.items():
            if len(classes) < 2:
                continue
            # Same-branch pairs (the provider inherits the shadowed class)
            # are the per-method walk's territory and already linked; this
            # pass adds only cross-branch sibling shadows.
            if classes[0] not in ancestor_cache:
                ancestor_cache[classes[0]] = set(
                    _linearized_ancestors(classes[0], class_inheritance)[1:]
                )
            first_qn = f"{classes[0]}{cs.SEPARATOR_DOT}{name}"
            for shadowed_class in classes[1:]:
                if shadowed_class in ancestor_cache[classes[0]]:
                    continue
                pair = (first_qn, f"{shadowed_class}{cs.SEPARATOR_DOT}{name}")
                if pair in emitted:
                    continue
                emitted.add(pair)
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


def _invert_implementers(
    interface_implementers: dict[str, set[str]],
) -> dict[str, list[str]]:
    # class_inheritance holds only superclasses (an `implements` clause or a
    # Rust `impl Trait for Type` never enters it), so the override walk needs
    # the implementer -> interfaces direction too, or no interface/trait
    # implementation ever gets an OVERRIDES edge. Both loops sorted: the
    # map and its sets are hash-ordered and edge emission must be
    # deterministic (each implementer's interface list follows the outer
    # sort; the inner sort makes the dict order deterministic too).
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
    csharp_gated = (
        csharp_methods is not None
        and method_qn in csharp_methods
        and (
            csharp_override_methods is None or method_qn not in csharp_override_methods
        )
    )

    queue = deque([class_qn])
    visited = {class_qn}

    while queue:
        current_class = queue.popleft()

        if current_class != class_qn:
            parent_method_qn = f"{current_class}.{method_name}"
            if parent_method_qn not in function_registry:
                # Fall back to name+arity so a generic type-var rename in the
                # override signature still matches the base method.
                parent_method_qn = (
                    _find_override_by_arity(
                        current_class, method_name, function_registry
                    )
                    or parent_method_qn
                )

            # The parent member must BE a method: a ctor of a nested class
            # that inherits its encloser (node::inner_node : node) shares
            # its name with the nested CLASS registered at parent.name, and
            # an OVERRIDES onto that class qn is a label-mismatched phantom.
            if function_registry.get(parent_method_qn) == NodeType.METHOD:
                # Skip a gated C# member's CLASS-parent match, but keep
                # walking: it may still implement an interface member deeper.
                parent_is_interface = (
                    function_registry.get(current_class) == NodeType.INTERFACE
                )
                if not (csharp_gated and not parent_is_interface):
                    ingestor.ensure_relationship_batch(
                        (cs.NodeLabel.METHOD, cs.KEY_QUALIFIED_NAME, method_qn),
                        cs.RelationshipType.OVERRIDES,
                        (
                            cs.NodeLabel.METHOD,
                            cs.KEY_QUALIFIED_NAME,
                            parent_method_qn,
                        ),
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
        for parent_class_qn in chain(
            class_inheritance.get(current_class, ()),
            implemented.get(current_class, ()),
        ):
            if parent_class_qn not in visited:
                visited.add(parent_class_qn)
                queue.append(parent_class_qn)
