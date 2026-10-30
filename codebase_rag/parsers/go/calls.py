"""Go's share of the call pass: which declaration a Go call binds, the edges
Go's build variants and interface dispatch add to it, and the constructions
and function values a Go composite literal writes.

`CallProcessor` owns what every language shares (the scan of a caller's call
nodes, the site and resolution each edge carries, the name-based resolver
fallback) and hands Go's part to the `GoCallResolution` it builds."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple, Protocol

from tree_sitter import Node

from ... import constants as cs
from ...language_spec import LanguageSpec
from ...node_text import decode_node_text, safe_decode_text
from ...types_defs import LanguageQueries, PropertyDict
from ...utils import qn_markers
from ..definition_facts import DefinitionFacts
from ..edge_site import EdgeSiteHost, host_site_scoped
from ..language_calls import LanguageCalls
from . import utils as go_utils
from .type_inference import GO_EXTERNAL_TARGET

if TYPE_CHECKING:
    from ..call_resolver import CallResolver


class GoCallHost(EdgeSiteHost, Protocol):
    """What Go call resolution reads and sets on the call pass that builds it.

    Beside the edge site, which the fan-outs set per edge and put back as
    the pass's own emitters do: `_verdict`, how surely the last resolve
    bound its callee, which `_resolve_noting` notes and a chain root's
    binding is judged by; and the assignment walker Go shares with other
    languages, with the scope boundaries it stops at."""

    _verdict: str

    def _emit_rel(
        self,
        from_spec: tuple[str, str, str],
        rel_type: str,
        to_spec: tuple[str, str, str],
        properties: PropertyDict | None = None,
    ) -> None: ...

    def _get_call_target_name(
        self, call_node: Node, language: cs.SupportedLanguage | None = None
    ) -> str | None: ...

    def _resolve_noting(
        self,
        call_name: str,
        module_qn: str,
        local_var_types: dict[str, str] | None = None,
        class_context: str | None = None,
        caller_qn: str | None = None,
        language: cs.SupportedLanguage | None = None,
        call_point: int | None = None,
    ) -> tuple[str, str] | None: ...

    def _emit_value_function_ref(
        self,
        node: Node,
        caller_spec: tuple[str, str, str],
        module_qn: str,
        local_var_types: dict[str, str] | None,
        class_context: str | None,
        resolve_func: Callable[..., tuple[str, str] | None],
        ensure_rel: Callable[..., None],
        caller_qn: str | None = None,
    ) -> None: ...

    @staticmethod
    def _flow_scope_boundaries(lang_config: LanguageSpec) -> frozenset[str]: ...

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


class _ValueReceiverCallee(NamedTuple):
    # What a Go method call on a call result or a composite literal binds to.
    # `decided` False leaves the call to the name-based resolver; True means
    # `callee` is the answer, and None there is a known non-edge.
    decided: bool
    callee: tuple[str, str] | None


class _PackageFunctionLookup(NamedTuple):
    # A Go chain root that calls a package's function: the function's name
    # and the (package, is the caller's own) pairs to look it up in, in
    # order. No packages means a call owned here that types nothing.
    name: str
    packages: tuple[tuple[str, bool], ...]


_SCOPE_TYPES = frozenset(
    {
        cs.TS_GO_FUNCTION_DECLARATION,
        cs.TS_GO_METHOD_DECLARATION,
        cs.TS_GO_FUNC_LITERAL,
    }
)
# Each is an implicit block for a declaration inside it: a `case` clause's
# body is its own scope without braces, so a type declared under `case 0:`
# is invisible to the `default:` arm (#1747).
_BLOCK_TYPES = frozenset(
    {
        cs.TS_GO_BLOCK,
        cs.TS_GO_EXPRESSION_CASE,
        cs.TS_GO_TYPE_CASE,
        cs.TS_GO_COMMUNICATION_CASE,
        cs.TS_GO_DEFAULT_CASE,
    }
)
Point = tuple[int, int]
Span = tuple[Point, Point]


def passes_every_result(args_node: Node, positional: list[Node]) -> bool:
    """Go's `f(pair())`: a lone call argument passes all of `pair`'s results.

    Go is the one grammar with an `argument_list` under a `source_file` root
    (Rust's list is `arguments`, C's root a `translation_unit`), and only
    this shape is walked up to its root.
    """
    if (
        args_node.type != cs.TS_ARGUMENT_LIST
        or len(positional) != 1
        or positional[0].type != cs.TS_GO_CALL_EXPRESSION
    ):
        return False
    root = args_node
    while root.parent is not None:
        root = root.parent
    return root.type == cs.TS_GO_SOURCE_FILE


def _innermost_block_end(node: Node, block_end: Point | None) -> Point | None:
    """The innermost block end in force below `node`: its own end for a
    function, or for a block-like node already inside one; else inherited."""
    if node.type in _SCOPE_TYPES or (
        block_end is not None and node.type in _BLOCK_TYPES
    ):
        return (node.end_point[0], node.end_point[1])
    return block_end


def type_declarations_by_name(
    root: Node,
) -> dict[str, list[tuple[int, Span | None]]]:
    """(1-based line, visible span or None) per `type` declaration, by name.

    One walk answers every name in the file. Each composite literal of a
    shadowed type used to walk the whole file for its own name, which cost
    O(literals x file size) per file (issue #2393). Sorted by line, the
    order the definition pass registered the variants in.
    """
    found: dict[str, list[tuple[int, Span | None]]] = {}
    # The second item is the end point of the innermost block, or None above
    # every function.
    stack: list[tuple[Node, Point | None]] = [(root, None)]
    while stack:
        node, block_end = stack.pop()
        block_end = _innermost_block_end(node, block_end)
        if node.type == cs.TS_GO_TYPE_SPEC and (
            name := safe_decode_text(node.child_by_field_name(cs.FIELD_NAME))
        ):
            start: Point = (node.start_point[0], node.start_point[1])
            found.setdefault(name, []).append(
                (start[0] + 1, None if block_end is None else (start, block_end))
            )
        stack.extend((child, block_end) for child in node.children)
    for declarations in found.values():
        declarations.sort(key=lambda item: item[0])
    return found


def variant_spans(
    variants: list[str], declarations: list[tuple[int, Span | None]]
) -> list[Span | None] | None:
    """One visible span (None for package level) per registry variant.

    The natural qn is the first declaration in document order; a `@line`
    variant names its declaration by line. None when the two cannot be put
    in correspondence, which keeps the caller's fan-out.
    """
    if len(declarations) != len(variants):
        return None
    by_line = dict(declarations)
    spans: list[Span | None] = []
    for index, variant in enumerate(variants):
        marker = variant.rsplit(cs.SEPARATOR_DOT, 1)[-1]
        # The LAST `@`, not the first: a C# verbatim identifier opens with
        # one that is part of the name, so `@event@12` names line 12 while
        # a first-`@` split reads `event@12` and gives up (issue #2017).
        # The no-marker branch tests the stripped form for the same reason.
        if qn_markers.strip_dup_marker(marker) == marker:
            if index != 0:
                return None
            spans.append(declarations[0][1])
            continue
        line = qn_markers.marker_line(marker)
        if line is None or line not in by_line:
            return None
        spans.append(by_line[line])
    return spans


def _composite_type_name(type_node: Node | None) -> str | None:
    """The type a Go composite literal constructs, as written: `Error`, `pkg.Error`.

    A `generic_type` (`Box[int]{...}`) constructs its base type. Anything
    else -- a slice, map, array or struct type literal -- is a container or
    an anonymous type and has no class to instantiate.
    """
    if type_node is None:
        return None
    if type_node.type == cs.TS_GO_GENERIC_TYPE:
        return _composite_type_name(type_node.child_by_field_name(cs.FIELD_TYPE))
    if type_node.type == cs.TS_GO_TYPE_IDENTIFIER:
        return safe_decode_text(type_node)
    if type_node.type == cs.TS_GO_QUALIFIED_TYPE:
        package = type_node.child_by_field_name(cs.FIELD_GO_PACKAGE)
        name = type_node.child_by_field_name(cs.FIELD_NAME)
        if package is None or name is None:
            return None
        return f"{safe_decode_text(package)}{cs.SEPARATOR_DOT}{safe_decode_text(name)}"
    return None


def _written_type(value: Node) -> Node | None:
    # The type a composite literal (`Box{}`) or a type assertion (`a.(*Dog)`)
    # writes for its value. An asserted pointer is peeled to its pointee,
    # whose methods a call on it binds.
    type_node = value.child_by_field_name(cs.FIELD_TYPE)
    if value.type != cs.TS_GO_TYPE_ASSERTION_EXPRESSION:
        return type_node
    while type_node is not None and type_node.type == cs.TS_GO_POINTER_TYPE:
        type_node = type_node.named_child(0)
    return type_node


def asserted_receiver_call_name(selector: Node) -> str | None:
    # `a.(Dog).Fetch()` and `x.(*Cat).Purr()` name the receiver's concrete
    # type at the call site, so the call is `Dog.Fetch`, which the resolver
    # binds as it binds a typed receiver (and as it already did once the
    # assertion was hoisted into a local); the selector's own text was a call
    # name it could not read (issue #2891).
    operand = selector.child_by_field_name(cs.FIELD_OPERAND)
    method = selector.child_by_field_name(cs.FIELD_FIELD)
    while operand is not None and operand.type == cs.TS_PARENTHESIZED_EXPRESSION:
        operand = operand.named_child(0)
    if (
        operand is None
        or operand.type != cs.TS_GO_TYPE_ASSERTION_EXPRESSION
        or method is None
        or method.text is None
    ):
        return None
    type_node = _written_type(operand)
    if type_node is None:
        return None
    # `other.Dog` keeps its package: read as `Dog` alone it named the
    # caller's own `Dog` (PR #2961).
    type_name = (
        decode_node_text(type_node.text)
        if type_node.type == cs.TS_GO_QUALIFIED_TYPE and type_node.text
        else go_utils.type_identifier_text(type_node)
    )
    if not type_name:
        return None
    return f"{type_name}{cs.SEPARATOR_DOT}{decode_node_text(method.text)}"


def _literal_element_inner(element: Node) -> Node | None:
    # {keyed_element(value=literal_element) | literal_element}: the element
    # wraps the bare value one level down.
    value = (
        element.child_by_field_name(cs.FIELD_VALUE)
        if element.type == cs.TS_GO_KEYED_ELEMENT
        else element
    )
    if value is None or value.type != cs.TS_GO_LITERAL_ELEMENT:
        return None
    return value.named_children[0] if value.named_children else None


def factory_bindings(node: Node) -> list[tuple[str, str]]:
    # Go `a, b := f(), g()`: pair each left identifier with the call in the
    # same position of the right expression list.
    left = node.child_by_field_name(cs.TS_FIELD_LEFT)
    right = node.child_by_field_name(cs.TS_FIELD_RIGHT)
    if left is None or right is None:
        return []
    names = [c for c in left.named_children if c.type == cs.TS_PY_IDENTIFIER]
    calls = [c for c in right.named_children if c.type == cs.TS_CALL_EXPRESSION]
    bindings: list[tuple[str, str]] = []
    for name_node, call in zip(names, calls):
        if (
            (var := safe_decode_text(name_node))
            and (fn := call.child_by_field_name(cs.TS_FIELD_FUNCTION)) is not None
            and (fn_name := safe_decode_text(fn))
        ):
            bindings.append((var, fn_name))
    return bindings


class GoCallResolution(LanguageCalls):
    """Binds Go calls and writes the edges Go's own rules add to them.

    Built and owned by `CallProcessor`. The resolver, the module paths and
    the definition facts are the pass's own, held by reference: the
    definition pass fills them as it registers each file, and the call pass
    resolves against them as they stand."""

    __slots__ = (
        "_host",
        "_resolver",
        "_module_qn_to_file_path",
        "_facts",
        "_package_index",
        "_package_index_size",
        "_type_declarations",
    )

    flows_callables = True
    references_arguments = True

    def __init__(
        self,
        host: GoCallHost,
        resolver: CallResolver,
        module_qn_to_file_path: dict[str, Path],
        facts: DefinitionFacts,
    ) -> None:
        self._host = host
        self._resolver = resolver
        self._module_qn_to_file_path = module_qn_to_file_path
        self._facts = facts
        # Package-prefix index over module_qn_to_file_path (issue #930),
        # rebuilt lazily whenever the underlying map grows.
        self._package_index: dict[Path, list[str]] = {}
        self._package_index_size = -1
        self._type_declarations: (
            tuple[Node, dict[str, list[tuple[int, Span | None]]]] | None
        ) = None

    def reset_package_index(self) -> None:
        self._package_index = {}
        self._package_index_size = -1

    def ingest_module_references(
        self,
        root_node: Node,
        module_qn: str,
        language: cs.SupportedLanguage,
        queries: Mapping[cs.SupportedLanguage, LanguageQueries],
        combined_captures: dict[str, list[Node]],
        sorted_func_nodes: list[Node] | None,
    ) -> None:
        module_spec = (cs.NodeLabel.MODULE, cs.KEY_QUALIFIED_NAME, module_qn)
        boundaries = self._host._flow_scope_boundaries(
            queries[language][cs.QUERY_CONFIG]
        )
        # A module-scope Go func map/slice (var funcMap = map[...]{...})
        # keeps its function entries reachable; scan before the no-calls
        # early return so a file defining only funcs and a table counts.
        self.ingest_composite_function_references(
            root_node, module_spec, module_qn, None, None, boundaries
        )
        # A module-scope `var e = &Error{}` constructs too (issue #1642).
        self.ingest_composite_literal_instantiations(
            root_node, module_spec, module_qn, boundaries
        )
        # A module-scope Go var bound to a bare function value
        # (var preExecHookFn = preExecHook) references it even in a file
        # with no calls, so scan before the no-calls early return.
        self._host._ingest_assignment_function_references(
            root_node, module_spec, module_qn, None, None, boundaries, language=language
        )

    def record_local_names(
        self, caller_node: Node, caller_qn: str, caller_type: str
    ) -> None:
        # A file's package-level names cannot collide with its functions'
        # (the compiler rejects it), so only a function or method binds
        # names a bare call might mean instead of the package's (#2571),
        # and only where each is in scope (#2616).
        scopes = (
            {}
            if caller_type == cs.NodeLabel.MODULE
            else go_utils.local_binding_scopes(caller_node)
        )
        if scopes:
            self._resolver.go_local_scopes[caller_qn] = scopes
        else:
            self._resolver.go_local_scopes.pop(caller_qn, None)

    def bound_method_caller(
        self, func_node: Node, func_name: str, module_qn: str
    ) -> tuple[str, str] | None:
        # A receiver method (`func (t T) m()`) is declared at file scope, but
        # the definition pass binds it to its receiver type's node (qn
        # `module.T.m`, not the receiver-dropping `module.m`). Resolve it to
        # its (method_qn, container_qn), mirroring that binding. The receiver
        # type resolves to its node qn (same or sibling file in the package),
        # and the registry check ensures the method node exists before
        # overriding the default attribution.
        if not go_utils.is_receiver_method(func_node):
            return None
        receiver_type = go_utils.extract_receiver_type_name(func_node)
        if not receiver_type:
            return None
        # The receiver struct is by Go definition in the METHOD'S OWN
        # package; same-package siblings outrank the global class-name
        # resolver, whose leaf lookup collides across packages on common
        # names like `Server` and dangles every edge (issue #930).
        package_hit = self._package_receiver_qn(receiver_type, func_name, module_qn)
        if package_hit is not None:
            return package_hit
        container_qn = self._resolver._resolve_class_name(receiver_type, module_qn) or (
            f"{module_qn}{cs.SEPARATOR_DOT}{receiver_type}"
        )
        caller_qn = f"{container_qn}{cs.SEPARATOR_DOT}{func_name}"
        if caller_qn in self._resolver.function_registry:
            return caller_qn, container_qn
        return None

    def _package_receiver_qn(
        self, receiver_type: str, method_name: str, module_qn: str
    ) -> tuple[str, str] | None:
        # A unique registered `<package sibling>.<ReceiverType>.<method>` is
        # the definition pass's own binding; ambiguity falls back. Go package
        # membership is (directory, `package` clause), and a `_test.go`
        # sibling is additionally compiled only under `go test`, so it only
        # counts for test requesters.
        requester = self._module_qn_to_file_path.get(module_qn)
        requester_is_test = requester is not None and requester.stem.endswith(
            cs.GO_TEST_FILE_SUFFIX
        )
        requester_package = self._facts.go_package_names.get(module_qn)
        hits: list[tuple[str, str]] = []
        for sibling_qn in self._package_modules(module_qn):
            sibling_path = self._module_qn_to_file_path[sibling_qn]
            if not requester_is_test and sibling_path.stem.endswith(
                cs.GO_TEST_FILE_SUFFIX
            ):
                continue
            if (
                requester_package is not None
                and self._facts.go_package_names.get(sibling_qn) != requester_package
            ):
                continue
            container_qn = f"{sibling_qn}{cs.SEPARATOR_DOT}{receiver_type}"
            caller_qn = f"{container_qn}{cs.SEPARATOR_DOT}{method_name}"
            if caller_qn in self._resolver.function_registry:
                hits.append((caller_qn, container_qn))
        return hits[0] if len(hits) == 1 else None

    def _package_modules(self, module_qn: str) -> list[str]:
        # Lazily indexed from module_qn_to_file_path by the file's PARENT
        # DIRECTORY (extension disambiguation makes qn-prefix grouping split
        # `pkg.service.go` from its package); rebuilt when the map grew
        # (incremental runs register modules as they parse).
        requester = self._module_qn_to_file_path.get(module_qn)
        if requester is None:
            return []
        size = len(self._module_qn_to_file_path)
        if self._package_index_size != size:
            index: dict[Path, list[str]] = {}
            for qn, path in self._module_qn_to_file_path.items():
                index.setdefault(path.parent, []).append(qn)
            self._package_index = index
            self._package_index_size = size
        return self._package_index.get(requester.parent, [])

    def resolve_callee(
        self,
        call_node: Node,
        call_name: str,
        call_var_types: dict[str, str] | None,
        module_qn: str,
        class_context: str | None,
        caller_qn: str,
    ) -> tuple[str, str] | None:
        # The go/types frontend (issue #1179): a HIT is the compiler's
        # exact first-party callee (promotion, shadowing) the heuristic
        # cannot type; the external sentinel proves the call leaves the
        # module and suppresses trie fabrication. Any miss (incl. the
        # frontend being off, so both fact maps are empty) falls back to
        # today's Go resolver with call_point preserved.
        callee_info = self._resolver.go.resolve_go_call_site(call_node, module_qn)
        if callee_info == GO_EXTERNAL_TARGET:
            callee_info = None
        elif (
            callee_info is None
            and (
                chained := self._value_receiver_callee(
                    call_node, call_var_types, module_qn, class_context, caller_qn
                )
            ).decided
        ):
            callee_info = chained.callee
        elif callee_info is None:
            callee_info = self._host._resolve_noting(
                call_name,
                module_qn,
                call_var_types,
                class_context,
                caller_qn,
                cs.SupportedLanguage.GO,
                call_point=call_node.start_byte,
            )
        return callee_info

    def _value_receiver_callee(
        self,
        call_node: Node,
        call_var_types: dict[str, str] | None,
        module_qn: str,
        class_context: str | None,
        caller_qn: str,
    ) -> _ValueReceiverCallee:
        """`NewBox().With(1).Bump()`, `(&Box{}).Bump()`: bind through the
        struct the receiver VALUE has (issue #2467).

        `b := NewBox(); b.Bump()` typed `b` from NewBox's result, but the
        chained spelling reached the resolver as one string with no local to
        type, and a literal receiver had no name to type at all. The chain
        is walked on the tree instead, which also keeps gofmt's line breaks
        between hops out of the method names: the root value's struct, then
        each method's single result, each read in the file declaring it.

        Decided when the method is found on that struct. Otherwise the call
        is left to the name-based resolver, which answered these shapes
        before, except for two shapes it never typed, which are owned here:
        a free function's result, whose type name it would now look up
        project-wide (and find another package's same-named struct), and a
        literal of an external package's type, whose methods are that
        package's.
        """
        undecided = _ValueReceiverCallee(False, None)
        chain = go_utils.call_receiver_chain(call_node)
        if chain is None:
            return undecided
        root, methods = chain
        if root.type in (
            cs.TS_GO_COMPOSITE_LITERAL,
            cs.TS_GO_TYPE_ASSERTION_EXPRESSION,
        ):
            # A literal or a type assertion writes the receiver's type, read in
            # this file's imports: `a.(other.Dog).Fetch()` binds the imported
            # package's Dog, never this package's (PR #2961).
            class_qn, owned = self._literal_receiver_class(
                _written_type(root), module_qn
            )
        else:
            owned, class_qn = self._package_function_root(root, module_qn)
            if not owned:
                called = self._resolved_root_class(
                    root, call_var_types, module_qn, class_context, caller_qn
                )
                if called is None:
                    return undecided
                owned, class_qn = called
        *hops, method = methods
        for hop in hops:
            if class_qn is None:
                break
            class_qn = self._result_class(
                self._resolver._try_resolve_method(class_qn, hop)
            )
        # A struct lacking the method may still promote it from an embedded
        # field, which this walk does not model; that stays the resolver's
        # call, as it does for a typed variable
        # (`PythonResolver.typed_receiver_lacks_method`).
        callee = (
            self._resolver._try_resolve_method(class_qn, method) if class_qn else None
        )
        return _ValueReceiverCallee(owned or callee is not None, callee)

    def _resolved_root_class(
        self,
        root: Node,
        call_var_types: dict[str, str] | None,
        module_qn: str,
        class_context: str | None,
        caller_qn: str,
    ) -> tuple[bool, str | None] | None:
        # (owned, struct) for a root the resolver binds (`c.Root()`), None when
        # it binds no function or method. Only an EXACT binding types the
        # result: a callee matched by its name alone (an untyped receiver)
        # says nothing about the value it returns. A free function's result
        # stays owned either way, since the name-based resolver never typed
        # one before and would now look its type name up project-wide.
        root_name = self._host._get_call_target_name(root, cs.SupportedLanguage.GO)
        if not root_name:
            return None
        root_callee = self.resolve_callee(
            root, root_name, call_var_types, module_qn, class_context, caller_qn
        )
        if root_callee is None or root_callee[0] not in (
            cs.NodeLabel.FUNCTION,
            cs.NodeLabel.METHOD,
        ):
            return None
        owned = root_callee[0] == cs.NodeLabel.FUNCTION
        if self._host._verdict != cs.EdgeResolution.EXACT:
            return owned, None
        return owned, self._result_class(root_callee)

    def _package_function_root(
        self, root: Node, module_qn: str
    ) -> tuple[bool, str | None]:
        """(calls a package's function, the struct it returns) for a root.

        A bare `NewBox()` names a function of the caller's own package (or a
        dot-imported one) and `box.New()` one of the package the import binds
        `box` to. Both are looked up there and nowhere else: the name trie
        answered `bytes.NewBuffer` with the caller's own `NewBuffer` when the
        standard library's was not indexed, and that guess typed every hop
        after it. Any other root (a method on a variable) is not one.

        A bare name the function binds itself (`NewBox := func() *Other
        {...}`, a parameter) is that local, never the package's function, so
        it types nothing; a local named like an import (`box`) makes
        `box.New()` a method call on it, left to the resolver.
        """
        function = root.child_by_field_name(cs.TS_FIELD_FUNCTION)
        if function is None:
            return False, None
        import_map = self._resolver.import_processor.import_mapping.get(module_qn) or {}
        if function.type == cs.TS_GO_IDENTIFIER:
            lookup = self._bare_function_lookup(root, function, module_qn, import_map)
        elif function.type == cs.TS_GO_SELECTOR_EXPRESSION:
            lookup = self._imported_function_lookup(root, function, import_map)
        else:
            lookup = None
        if lookup is None:
            return False, None
        return True, self._package_function_class(lookup, module_qn)

    def _bare_function_lookup(
        self,
        root: Node,
        function: Node,
        module_qn: str,
        import_map: dict[str, str],
    ) -> _PackageFunctionLookup | None:
        # `NewBox()`: the caller's own package, then each dot-imported one.
        # None when the name is unreadable; a local of that name types nothing.
        name = safe_decode_text(function)
        if not name:
            return None
        if go_utils.binds_locally(root, name):
            return _PackageFunctionLookup(name, ())
        # (package, is the caller's own): a dot-import of the caller's own
        # directory (`package m_test` importing `m`) is still an import.
        packages = ((module_qn.rpartition(cs.SEPARATOR_DOT)[0], True),) + tuple(
            (path, False)
            for key, path in import_map.items()
            if key.startswith(cs.SEPARATOR_DOT)
        )
        return _PackageFunctionLookup(name, packages)

    def _imported_function_lookup(
        self, root: Node, function: Node, import_map: dict[str, str]
    ) -> _PackageFunctionLookup | None:
        # `box.New()`: the package the import binds `box` to. None when `box`
        # is no import (or a local shadows it); an external package's
        # function types nothing.
        operand = function.child_by_field_name(cs.FIELD_OPERAND)
        field = function.child_by_field_name(cs.FIELD_FIELD)
        if operand is None or field is None or operand.type != cs.TS_GO_IDENTIFIER:
            return None
        alias = safe_decode_text(operand)
        name = safe_decode_text(field)
        if not alias or not name or go_utils.binds_locally(root, alias):
            return None
        target = import_map.get(alias)
        if not target:
            return None
        if self._import_is_external(target):
            return _PackageFunctionLookup(name, ())
        return _PackageFunctionLookup(name, ((target, False),))

    def _package_function_class(
        self, lookup: _PackageFunctionLookup, module_qn: str
    ) -> str | None:
        # The struct the function returns, from the first package declaring
        # one `module_qn` can see.
        for package_qn, own_package in lookup.packages:
            # Build-variant files may each declare the function; they count as
            # one when every copy returns the same struct.
            results = {
                self._result_class((cs.NodeLabel.FUNCTION, qn))
                for qn in self._package_functions(package_qn, lookup.name)
                if self._function_visible_to(
                    qn.rpartition(cs.SEPARATOR_DOT)[0], module_qn, own_package
                )
            }
            if results:
                return results.pop() if len(results) == 1 else None
        return None

    def _function_visible_to(
        self, declaring_qn: str, module_qn: str, own_package: bool
    ) -> bool:
        # Whether a function declared in file `declaring_qn` is one `module_qn`
        # can call, either in its own package (a bare call) or through an
        # import. The rules `_package_receiver_qn` uses: a `_test.go` file
        # compiles only under `go test`, so only a test file of the same
        # package sees it, and in the caller's own package the `package`
        # clauses must agree (`package m_test` is another package). Through an
        # import, even of the caller's own directory (`package m_test`
        # importing `m`), only that package's non-test files count.
        declaring_path = self._module_qn_to_file_path.get(declaring_qn)
        if declaring_path is None:
            return True
        requester = self._module_qn_to_file_path.get(module_qn)
        requester_is_test = requester is not None and requester.stem.endswith(
            cs.GO_TEST_FILE_SUFFIX
        )
        if declaring_path.stem.endswith(cs.GO_TEST_FILE_SUFFIX) and not (
            requester_is_test and own_package
        ):
            return False
        if not own_package:
            return True
        requester_package = self._facts.go_package_names.get(module_qn)
        return (
            requester_package is None
            or self._facts.go_package_names.get(declaring_qn) == requester_package
        )

    def _package_functions(self, package_qn: str, name: str) -> list[str]:
        # The free functions `name` filed directly under a file of
        # `package_qn`: cgr files a package's functions by FILE, a segment the
        # source never writes, and anything deeper is a method.
        registry = self._resolver.function_registry
        depth = package_qn.count(cs.SEPARATOR_DOT) + 2
        return [
            qn
            for qn in registry.find_with_prefix_and_suffix(package_qn, name)
            if registry.get(qn) == cs.NodeLabel.FUNCTION
            and qn.count(cs.SEPARATOR_DOT) == depth
        ]

    def _import_is_external(self, target: str) -> bool:
        # Local import paths were rewritten to project qns at import time, so
        # a target outside the project's qn space is an external package.
        project = self._resolver.import_processor.project_name
        return not (
            target == project or target.startswith(f"{project}{cs.SEPARATOR_DOT}")
        )

    def _literal_receiver_class(
        self, type_node: Node | None, module_qn: str
    ) -> tuple[str | None, bool]:
        # (first-party struct, owned) for a receiver of the written type
        # `type_node`: a composite literal's, or a type assertion's. A type
        # of an EXTERNAL package (`bytes.Buffer{}`) is owned with no struct:
        # its methods are that package's, never a first-party one sharing
        # the name. Any other miss (a named slice type, an unknown name) is
        # not owned.
        type_name = _composite_type_name(type_node)
        if not type_name:
            return None, False
        if class_qn := self._struct_class(type_name, module_qn):
            return class_qn, False
        package_alias = type_name.rpartition(cs.SEPARATOR_DOT)[0]
        import_map = self._resolver.import_processor.import_mapping.get(module_qn) or {}
        target = import_map.get(package_alias) if package_alias else None
        if not target:
            return None, False
        return None, self._import_is_external(target)

    def _result_class(self, callee: tuple[str, str] | None) -> str | None:
        # The struct a Go function's or method's single result names. The
        # name is read in the DECLARING file: `New() *Box` in package box
        # returns box's Box, whatever the calling package calls `Box`. A
        # method sits under its receiver struct (`file.Box.With`), a
        # function directly under its file (`file.New`).
        if callee is None:
            return None
        callee_type, callee_qn = callee
        result = self._resolver.type_inference.facts.method_return_types.get(callee_qn)
        if not result:
            return None
        depth = 2 if callee_type == cs.NodeLabel.METHOD else 1
        declaring_module = callee_qn.rsplit(cs.SEPARATOR_DOT, depth)[0]
        return self._struct_class(result, declaring_module)

    def build_targets(
        self,
        module_qn: str,
        callee_type: str,
        callee_qn: str,
        targets: list[str],
    ) -> tuple[list[str], dict[str, bool]]:
        # A Go call reaches a declaration exactly only when every build
        # compiling the caller holds it. A method of an interface each build
        # declares again is each build's own (`go_variant_callees`); any
        # other target in a file some of those builds leave out (a method
        # `t_linux.go` declares again) is one candidate of several.
        resolver = self._resolver
        if callee_type == cs.NodeLabel.METHOD and (
            variants := resolver.go.go_variant_callees(callee_qn, module_qn)
        ):
            return [qn for qn, _ in variants], dict(variants)
        return targets, {
            target: resolver.go.go_reaches_everywhere(module_qn, (target,))
            for target in targets
        }

    def emit_callee_fanouts(
        self,
        caller_spec: tuple[str, str, str],
        module_qn: str,
        callee_type: str,
        callee_qn: str,
        ensure_rel: Callable[..., None],
    ) -> None:
        if callee_type == cs.NodeLabel.FUNCTION:
            self._emit_build_variant_siblings(
                caller_spec, module_qn, callee_qn, ensure_rel
            )
        if callee_type == cs.NodeLabel.METHOD:
            for method_qn, exact in self._resolver.go.go_variant_callees(
                callee_qn, module_qn
            ) or [(callee_qn, True)]:
                self._emit_interface_dispatch(
                    caller_spec, module_qn, method_qn, ensure_rel, guessed=not exact
                )

    def _emit_build_variant_siblings(
        self,
        caller_spec: tuple[str, str, str],
        module_qn: str,
        callee_qn: str,
        ensure_rel: Callable[..., None],
    ) -> None:
        # A bare Go call resolves to one file's copy of a package-level
        # function; same-package same-name siblings are mutually-exclusive
        # build-tag variants (gin's `validate`), so emit an edge to each so
        # no build variant is reported dead. sorted(): the target label is a
        # hash-randomized StrEnum, so sort for deterministic output.
        host = self._host
        call_resolution = host._resolution
        for sibling_type, sibling_qn in sorted(
            self._resolver.go.go_package_sibling_targets(callee_qn)
        ):
            for variant in self._resolver.function_registry.variants(sibling_qn):
                # None in a file no build compiles with the caller; exact only
                # where every build compiling the caller holds it.
                if self._resolver.go.go_apart_from_caller(module_qn, variant):
                    continue
                host._resolution = (
                    call_resolution
                    if self._resolver.go.go_reaches_everywhere(module_qn, (variant,))
                    else cs.EdgeResolution.HEURISTIC
                )
                ensure_rel(
                    caller_spec,
                    cs.RelationshipType.CALLS,
                    (sibling_type, cs.KEY_QUALIFIED_NAME, variant),
                )
        host._resolution = call_resolution

    def _emit_interface_dispatch(
        self,
        caller_spec: tuple[str, str, str],
        module_qn: str,
        callee_qn: str,
        ensure_rel: Callable[..., None],
        guessed: bool = False,
    ) -> None:
        # The interface method is the static callee and keeps its edge; each
        # implementation is one candidate of the value's dynamic type. A
        # callee the name-only fallback merely guessed says nothing about
        # the receiver, so it is not fanned out (the #2931 rule). One that
        # only some of the caller's builds hold (`guessed`) is still the
        # receiver's, in those builds: its implementations are candidates.
        host = self._host
        if host._resolution == cs.EdgeResolution.HEURISTIC:
            return
        edges = self._dispatch_edges(callee_qn, module_qn)
        prev_resolution = host._resolution
        proven = cs.EdgeResolution.OVERLOAD if len(edges) > 1 else prev_resolution
        try:
            for target_type, target_qn, target_guessed in edges:
                host._resolution = (
                    cs.EdgeResolution.HEURISTIC if guessed or target_guessed else proven
                )
                ensure_rel(
                    caller_spec,
                    cs.RelationshipType.CALLS,
                    (target_type, cs.KEY_QUALIFIED_NAME, target_qn),
                )
        finally:
            host._resolution = prev_resolution

    def _dispatch_edges(
        self, callee_qn: str, module_qn: str
    ) -> list[tuple[str, str, bool]]:
        # (label, method, guessed) per implementation. Each declaration a build
        # makes again is a target of its own. One go/types read for a pair it
        # proved is the compiler's, whatever the names say; any other is
        # matched by name, and one whose signature differs for certain runs
        # in no build where its type is an implementer. One that a build
        # compiling the caller leaves out, or whose type it does, is a
        # candidate in some of the caller's builds only.
        resolver = self._resolver
        edges: list[tuple[str, str, bool]] = []
        for (target_type, target_qn), implementers in sorted(
            resolver.go.interface_dispatch_targets(callee_qn).items()
        ):
            for variant in resolver.function_registry.variants(target_qn):
                if (
                    guessed := self._dispatch_guess(
                        callee_qn, module_qn, variant, implementers
                    )
                ) is not None:
                    edges.append((target_type, variant, guessed))
        return edges

    def _dispatch_guess(
        self,
        callee_qn: str,
        module_qn: str,
        variant: str,
        implementers: dict[str, bool],
    ) -> bool | None:
        # Whether a dispatch edge to `variant` is a guess, or None for none:
        # it runs only for an implementer a build compiling the caller holds
        # with it, and a proven one is exact only where every such build
        # does.
        resolver = self._resolver
        interface_qn = callee_qn.rpartition(cs.SEPARATOR_DOT)[0]
        builds = {
            implementer: (
                guessed,
                *resolver.go.go_dispatch_builds(
                    module_qn, interface_qn, implementer, variant
                ),
            )
            for implementer, guessed in implementers.items()
        }
        read = resolver.go.go_declaration_read(variant)
        reachable = {impl: found for impl, found in builds.items() if found[1]}
        proven = [impl for impl, found in reachable.items() if not found[0] and read]
        if not reachable or not (
            proven or resolver.go.go_declaration_satisfies(variant, callee_qn)
        ):
            return None
        return not any(reachable[implementer][2] for implementer in proven)

    @host_site_scoped
    def ingest_composite_literal_instantiations(
        self,
        caller_node: Node,
        caller_spec: tuple[str, str, str],
        module_qn: str,
        boundary_types: frozenset[str],
    ) -> None:
        # `Error{...}` and `&Error{...}` are how Go constructs a struct: there
        # is no constructor call, so the call pass never saw a construction
        # and no Go program produced an INSTANTIATES edge (issue #1642). The
        # composite literal is the construction site. Only a literal whose
        # `type` names a type is one: a slice, map or array literal builds a
        # container, and its element literals carry no type of their own.
        # Revive-only, like the C++ braced-return pass: nothing is emitted
        # unless the type resolves to a registered first-party Class.
        host = self._host
        registry = self._resolver.function_registry
        ensure_rel = host._emit_rel
        stack: list[Node] = list(caller_node.children)
        while stack:
            node = stack.pop()
            if node.type in boundary_types:
                continue
            stack.extend(node.children)
            if node.type != cs.TS_GO_COMPOSITE_LITERAL:
                continue
            type_name = _composite_type_name(node.child_by_field_name(cs.FIELD_TYPE))
            if not type_name:
                continue
            class_qn = self._struct_class(type_name, module_qn)
            if class_qn is None:
                continue
            host._site_node = node
            for class_variant in self._visible_class_variants(
                class_qn, module_qn, node
            ):
                if registry.get(class_variant) != cs.NodeLabel.CLASS:
                    continue
                ensure_rel(
                    caller_spec,
                    cs.RelationshipType.INSTANTIATES,
                    (cs.NodeLabel.CLASS, cs.KEY_QUALIFIED_NAME, class_variant),
                )

    def _struct_class(self, type_name: str, module_qn: str) -> str | None:
        """The registered Class a Go composite literal's type names, or None.

        Go types are PACKAGE-scoped: a bare `Name` can only be a type declared
        in the literal's own package (the files beside it), and `pkg.Name` one
        declared in the package the import map binds `pkg` to. Types are
        registered under the FILE that declares them (`proj.m.types.Error`),
        so the lookup crosses the file segment the source never names.

        Deliberately not `_resolve_class_name`: its last step is a repo-wide
        search by simple name, which bound `Error{}` in package `m` to a
        same-named struct in package `a`, or to a Python `class Error`, and
        dead-code then revived the wrong type while reporting the constructed
        one dead (#1642). The one Class named `Name` directly under a
        file of the right package is the answer; two candidates, or none,
        resolve to nothing rather than a guess.
        """
        package_alias, _, name = type_name.rpartition(cs.SEPARATOR_DOT)
        import_map = self._resolver.import_processor.import_mapping.get(module_qn) or {}
        if package_alias:
            packages = [import_map.get(package_alias)]
        else:
            # Own package first; a bare name can also be an exported type of a
            # DOT-imported package (`import . "proj/m"`), which the import
            # pass records under a `.`-prefixed key (#1747).
            packages = [module_qn.rpartition(cs.SEPARATOR_DOT)[0]] + [
                path
                for key, path in import_map.items()
                if key.startswith(cs.SEPARATOR_DOT)
            ]
        for package_qn in packages:
            if not package_qn:
                continue
            found = self._class_in_package(package_qn, name, module_qn)
            if found is not None:
                return found
        return None

    def _class_in_package(
        self, package_qn: str, name: str, module_qn: str
    ) -> str | None:
        """The one Class `name` declared directly under a file of `package_qn`.

        Two files of the package declaring the same name is Go's own error;
        the shape that does occur is a function-local type shadowing a
        package-level one, which the registry also files directly under the
        declaring module. The one declared in THIS file wins then, because a
        local type is what a bare literal in that file names (#1747).
        Anything still ambiguous resolves to nothing rather than a guess.
        """
        registry = self._resolver.function_registry
        depth = package_qn.count(cs.SEPARATOR_DOT) + 2
        candidates = [
            qn
            for qn in registry.find_with_prefix_and_suffix(package_qn, name)
            if registry.get(qn) == cs.NodeLabel.CLASS
            and qn.count(cs.SEPARATOR_DOT) == depth
            and self._resolver.go.go_declaration_is_visible(qn, module_qn)
        ]
        if len(candidates) > 1:
            own = [
                qn
                for qn in candidates
                if qn.startswith(f"{module_qn}{cs.SEPARATOR_DOT}")
            ]
            candidates = own if own else candidates
        return candidates[0] if len(candidates) == 1 else None

    def _visible_class_variants(
        self, class_qn: str, module_qn: str, literal: Node
    ) -> list[str]:
        """The registry variants of `class_qn` that `literal` can name.

        One file declaring `Local` at package level and again inside a
        function registers both under one qn, the second as a `@line`
        variant, and a literal fanned out to both. Go scoping says a local
        type is visible from its declaration to the end of its innermost
        block and shadows the package-level twin there; a literal before the
        declaration, or outside the nested block that holds it, names the
        package type (#1747). So choose by position
        against that span. Any shape this cannot settle keeps the fan-out.
        """
        variants = self._resolver.function_registry.variants(class_qn)
        if len(variants) < 2 or not class_qn.startswith(
            f"{module_qn}{cs.SEPARATOR_DOT}"
        ):
            return variants
        name = class_qn.rsplit(cs.SEPARATOR_DOT, 1)[-1]
        declarations = self._type_declaration_scopes(module_qn, name)
        spans = variant_spans(variants, declarations)
        if spans is None:
            return variants
        # A point, not a row: `x := Local{}; type Local struct{}` on one line
        # puts the literal BEFORE the declaration, and rows alone attributed
        # it to the later local type (#1747).
        point: Point = (literal.start_point[0], literal.start_point[1])
        local = [
            variant
            for variant, span in zip(variants, spans, strict=True)
            if span is not None and span[0] <= point <= span[1]
        ]
        package_level = [
            variant
            for variant, span in zip(variants, spans, strict=True)
            if span is None
        ]
        if len(local) == 1:
            return local
        if not local and len(package_level) == 1:
            return package_level
        return variants

    def _type_declaration_scopes(
        self, module_qn: str, name: str
    ) -> list[tuple[int, Span | None]]:
        """(1-based line, visible span or None) per `type name` declaration.

        The span of a function-local declaration runs from the declaration's
        own (row, column) to the end of its innermost enclosing block: a bare
        `{ }` block, a loop or branch body, a `case` clause's implicit body,
        or the function body itself, which is where Go makes it visible.
        None marks a package-level declaration, visible everywhere in the
        file. In document order, which is the order the definition pass
        registered them in, so the first is the natural qn and the rest are
        variants named by their line.
        """
        type_inference = self._resolver.type_inference
        file_path = type_inference.module_qn_to_file_path.get(module_qn)
        if file_path is None or not (entry := type_inference.ast_cache.load(file_path)):
            return []
        root_node, _ = entry
        # Keyed on the root node itself: literals of one file are resolved
        # together, and a re-parsed file comes back as a new root.
        cached = self._type_declarations
        if cached is None or cached[0] is not root_node:
            cached = (root_node, type_declarations_by_name(root_node))
            self._type_declarations = cached
        return list(cached[1].get(name, ()))

    def ingest_composite_function_references(
        self,
        caller_node: Node,
        caller_spec: tuple[str, str, str],
        module_qn: str,
        local_var_types: dict[str, str] | None,
        class_context: str | None,
        boundary_types: frozenset[str],
    ) -> None:
        # A Go function placed as a value in a composite literal, a func map
        # (`map[string]any{"rpad": rpad}`) or a func slice (`[]Handler{a, b}`), is
        # a dispatch table invoked later by key, never by a visible call, so its
        # entries look dead. Reference each from the enclosing scope. Go's literal
        # shape differs from the JS/Py pair form: composite_literal > literal_value >
        # {keyed_element(value=literal_element) | literal_element}, and the element
        # wraps the bare identifier one level down, so unwrap before resolving.
        host = self._host
        resolve_func = self._resolver.resolve_function_call
        ensure_rel = host._emit_rel
        stack: list[Node] = list(caller_node.children)
        while stack:
            node = host._site_node = stack.pop()
            if node.type in boundary_types:
                continue
            if node.type == cs.TS_GO_LITERAL_VALUE:
                for element in node.named_children:
                    host._site_node = element
                    if (inner := _literal_element_inner(element)) is None:
                        continue
                    host._emit_value_function_ref(
                        inner,
                        caller_spec,
                        module_qn,
                        local_var_types,
                        class_context,
                        resolve_func,
                        ensure_rel,
                    )
            stack.extend(node.children)
