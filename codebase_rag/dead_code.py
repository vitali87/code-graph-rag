# Dead-code reachability engine. Roots (entry points, framework hooks,
# module-load callees, test code) expand over CALLS/REFERENCES edges;
# whatever is never reached is reported. Reachability runs client-side in
# Python: the per-root *BFS Cypher formulation is O(roots x graph) and hit
# memgraph's 600s timeout on big projects (django: 31k roots, 101k CALLS
# edges), whereas a multi-source walk over the fetched edges is linear and
# finishes in milliseconds.
from collections import defaultdict
from fnmatch import fnmatch

from . import constants as cs
from . import cypher_queries as cq
from .types_defs import (
    DeadCodeConfig,
    GraphQueryClient,
    PropertyDict,
    PropertyValue,
    ResultRow,
    ResultValue,
)

_MODULE = cs.NodeLabel.MODULE.value
_FUNCTION = cs.NodeLabel.FUNCTION.value
_METHOD = cs.NodeLabel.METHOD.value
_CLASS = cs.NodeLabel.CLASS.value
_CALLS = cs.RelationshipType.CALLS.value
_REFERENCES = cs.RelationshipType.REFERENCES.value
_INSTANTIATES = cs.RelationshipType.INSTANTIATES.value
_INHERITS = cs.RelationshipType.INHERITS.value
_DEFINES = cs.RelationshipType.DEFINES.value
_DEFINES_METHOD = cs.RelationshipType.DEFINES_METHOD.value
_OVERRIDES = cs.RelationshipType.OVERRIDES.value
_NodeId = tuple[str, PropertyValue]
_RelTuple = tuple[str, PropertyValue, str, str, PropertyValue]


def default_dead_code_config(
    include_tests: bool,
    include_classes: bool,
    exclude_patterns: tuple[str, ...] = (),
) -> DeadCodeConfig:
    return DeadCodeConfig(
        include_tests=include_tests,
        include_classes=include_classes,
        root_decorators=frozenset(d.lower() for d in cs.DEFAULT_ROOT_DECORATORS),
        entry_points=(),
        test_patterns=tuple(cs.TEST_PATH_PATTERNS),
        exclude_patterns=exclude_patterns,
    )


def _norm_decorator(decorator: str) -> str:
    # Drop '@' and any surrounding attribute brackets, take the text before
    # '(', then the last dotted segment, lowercased -> `@app.route(...)` and a
    # C# `[Route("x")]` both become `route`. Bracket-stripping keeps the
    # normalization robust to whatever a highlight query captures.
    cleaned = decorator.replace(cs.DECORATOR_AT, "").strip("[] ")
    head = cleaned.split(cs.CHAR_PAREN_OPEN)[0]
    return head.split(cs.SEPARATOR_DOT)[-1].strip("[]").lower()


def _is_dunder(name: str) -> bool:
    # A __dunder__ method is invoked by the Python runtime (async with,
    # iteration, operators), never by an explicit call the graph can see, so it
    # is a reachability root, not dead code.
    return (
        len(name) > len(cs.PY_NAME_DUNDER) * 2
        and name.startswith(cs.PY_NAME_DUNDER)
        and name.endswith(cs.PY_NAME_DUNDER)
    )


def _is_rust_runtime_root(name: str, is_method: bool, path: str) -> bool:
    # A Rust `.rs` symbol the language/runtime invokes with no call site: `fn
    # main()` (entry) or a trait-impl method (Display::fmt, Iterator::next).
    # Name-scoped like Python dunders; trait methods must be methods.
    if not path.endswith(cs.EXT_RS):
        return False
    # `main` is only the entry point as a receiverless `fn main()`; a method
    # named main is not, so gate it to non-methods. Trait methods are the reverse.
    if name in cs.RUST_ROOT_FUNCTION_NAMES:
        return not is_method
    return is_method and name in cs.RUST_TRAIT_METHOD_NAMES


def _is_c_cpp_entry_root(name: str, is_method: bool, path: str) -> bool:
    # A C/C++ program entry (`main`, Windows' `wWinMain`/`WinMain`/`wmain`, a
    # DLL's `DllMain`) is invoked by the OS runtime, never by a call the graph
    # sees, so it roots its whole call tree (an unrooted wWinMain reported all
    # 34 windows/runner symbols of a Flutter desktop shim dead). Free
    # functions only; a method named main is not an entry.
    return (
        not is_method
        and name in cs.C_CPP_ENTRY_FUNCTION_NAMES
        and (path.endswith(cs.CPP_EXTENSIONS) or path.endswith(cs.EXT_C))
    )


def _is_cpp_operator_root(name: str, path: str) -> bool:
    # A C++ operator overload / user-defined literal (`operator==`, `operator[]`,
    # `operator""_json`) is invoked by operator/literal SYNTAX, not a named call
    # the graph sees, so it is a reachability root (like Python dunders / Rust
    # trait methods). `operator` heads every such definition (member or free),
    # so the name prefix on a C++ file identifies them.
    return name.startswith(cs.CPP_OPERATOR_PREFIX) and path.endswith(cs.CPP_EXTENSIONS)


def _is_java_serialization_root(name: str, is_method: bool, path: str) -> bool:
    # A Java serialization hook (`readObject`/`writeObject`/`writeReplace`/
    # `readResolve`/`readObjectNoData`) is invoked reflectively by the java.io
    # runtime, never by a named call the graph sees, so it is a reachability root
    # (like Python dunders / Rust trait methods). Gated to methods on a .java
    # file; `name` is the bare method name (signature stripped by caller).
    return (
        is_method
        and path.endswith(cs.EXT_JAVA)
        and name in cs.JAVA_SERIALIZATION_METHOD_NAMES
    )


def _is_csharp_attribute_root(props: PropertyDict, path: str) -> bool:
    # A C# method carrying a framework/runtime attribute ([Fact], [HttpGet],
    # [OnDeserialized]) is invoked reflectively, never by a call the graph sees,
    # so it is a reachability root. Gated to .cs; the decorator set matches via
    # the normalized (lowercased, arg-stripped) form.
    return path.endswith(cs.EXT_CS) and _has_root_decorator(
        props, cs.CSHARP_ROOT_ATTRIBUTES
    )


def _is_csharp_dispose_root(name: str, is_method: bool, path: str) -> bool:
    # `Dispose`/`DisposeAsync` are invoked by a `using` block's teardown, not
    # a named call; a reachability root on a .cs method (like the Java hooks).
    return (
        is_method
        and path.endswith(cs.EXT_CS)
        and name in cs.CSHARP_DISPOSE_METHOD_NAMES
    )


def _is_csharp_operator_or_finalizer_root(name: str, path: str) -> bool:
    # An operator overload is invoked by operator SYNTAX (`a + b`) and a
    # finalizer (`~Foo`) by the GC, never a named call the graph sees, so both
    # are reachability roots on a .cs file (cf. the C++ operator root). The
    # synthesized leaf carries the `operator_`/`~` prefix.
    return path.endswith(cs.EXT_CS) and (
        name.startswith(cs.TS_CSHARP_OPERATOR_NAME_PREFIX)
        or name.startswith(cs.TS_CSHARP_DESTRUCTOR_NAME_PREFIX)
    )


def _matches_test_path(path: str, patterns: tuple[str, ...]) -> bool:
    # Match test-path patterns against a leading-slash-normalized path so a dir
    # pattern like `/tests/` also matches a ROOT `tests/` dir (Rust integration
    # tests, a top-level tests/ folder), not just a nested `src/tests/`. The
    # leading slash keeps `contests/` from matching `/tests/`.
    normalized = (
        path if path.startswith(cs.SEPARATOR_SLASH) else cs.SEPARATOR_SLASH + path
    )
    return any(pattern in normalized for pattern in patterns)


def _has_root_decorator(props: PropertyDict, root_decorators: frozenset[str]) -> bool:
    decorators = props.get(cs.KEY_DECORATORS)
    if not isinstance(decorators, list):
        return False
    return any(_norm_decorator(str(d)) in root_decorators for d in decorators)


def _walk(
    frontier: set[str],
    adjacency: dict[str, set[str]],
    live: set[str],
    added: set[str] | None = None,
) -> None:
    stack = list(frontier)
    while stack:
        current = stack.pop()
        for nxt in adjacency.get(current, ()):
            if nxt not in live:
                live.add(nxt)
                if added is not None:
                    added.add(nxt)
                stack.append(nxt)


def dead_code_from_graph(
    nodes: dict[_NodeId, PropertyDict],
    rels: list[_RelTuple],
    project_prefix: str,
    config: DeadCodeConfig,
) -> set[str]:
    labels = {_FUNCTION, _METHOD}
    traversal = {_CALLS, _REFERENCES}
    module_rels = {_CALLS, _REFERENCES}
    if config.include_classes:
        labels.add(_CLASS)
        traversal |= {_INSTANTIATES, _INHERITS}
        module_rels.add(_INSTANTIATES)

    candidates: set[str] = set()
    props_by_qn: dict[str, PropertyDict] = {}
    method_qns: set[str] = set()
    module_path: dict[str, str] = {}
    for (label, uid), props in nodes.items():
        if label == _MODULE:
            module_path[str(uid)] = str(props.get(cs.KEY_PATH, ""))
        elif label in labels and str(uid).startswith(project_prefix):
            # With tests excluded, a test-file symbol's only callers are
            # excluded as roots, so reporting it is noise (test helpers and
            # mocks are infrastructure, not dead production code).
            if not config.include_tests and _matches_test_path(
                str(props.get(cs.KEY_PATH) or ""), config.test_patterns
            ):
                continue
            candidates.add(str(uid))
            props_by_qn[str(uid)] = props
            if label == _METHOD:
                method_qns.add(str(uid))

    roots: set[str] = set()
    # A method of a typing.Protocol subclass is an interface stub whose callers
    # resolve to the implementations; DEFINES edges from functions/methods feed
    # the live-owner registration round below.
    defines_pairs: list[tuple[str, str]] = []
    protocol_classes: set[str] = set()
    class_methods: list[tuple[str, str]] = []
    nested_class_pairs: list[tuple[str, str]] = []
    for from_label, from_val, rel_type, to_label, to_val in rels:
        if rel_type == _DEFINES and from_label in (_FUNCTION, _METHOD):
            defines_pairs.append((str(from_val), str(to_val)))
            if to_label == _CLASS:
                nested_class_pairs.append((str(from_val), str(to_val)))
        elif rel_type == _INHERITS and str(to_val) in cs.PROTOCOL_BASE_QNS:
            protocol_classes.add(str(from_val))
        elif rel_type == _DEFINES_METHOD:
            class_methods.append((str(from_val), str(to_val)))
        if from_label != _MODULE or rel_type not in module_rels:
            continue
        target_qn = str(to_val)
        if target_qn not in candidates:
            continue
        path = module_path.get(str(from_val), "")
        is_test = _matches_test_path(path, config.test_patterns)
        if config.include_tests or not is_test:
            roots.add(target_qn)
    protocol_stubs = {m for c, m in class_methods if c in protocol_classes}

    for qn in candidates:
        if qn in roots:
            continue
        props = props_by_qn[qn]
        # The duplicate-qn marker (`init@51`, a SECOND Go init() in one file)
        # is a registration artifact, never part of the written name; strip it
        # so every name-scoped root rule sees the real leaf (kubernetes
        # pkg.apis.abac register.init@51 reported dead).
        leaf = qn.rsplit(cs.SEPARATOR_DOT, 1)[-1].split(cs.DUP_QN_MARKER, 1)[0]
        path = str(props.get(cs.KEY_PATH, ""))
        if _has_root_decorator(props, config.root_decorators):
            roots.add(qn)
        elif props.get(cs.KEY_IS_EXPORTED) is True:
            roots.add(qn)
        # A method overriding an EXTERNAL stdlib base's method (click's
        # textwrap.TextWrapper subclass) is invoked by the base's machinery,
        # never by a first-party call, so it is a root.
        elif props.get(cs.KEY_OVERRIDES_EXTERNAL) is True:
            roots.add(qn)
        elif qn in protocol_stubs:
            roots.add(qn)
        elif qn in method_qns and _is_dunder(leaf) and path.endswith(cs.EXT_PY):
            roots.add(qn)
        # Python Enum protocol hooks (_generate_next_value_, _missing_) are
        # invoked by the enum machinery by NAME, like dunders: roots, not
        # dead code (django's TextChoices._generate_next_value_).
        elif (
            qn in method_qns
            and leaf in cs.PY_ENUM_HOOK_METHOD_NAMES
            and path.endswith(cs.EXT_PY)
        ):
            roots.add(qn)
        elif (
            qn not in method_qns
            and leaf in cs.GO_ROOT_FUNCTION_NAMES
            and path.endswith(cs.EXT_GO)
        ):
            roots.add(qn)
        elif _is_rust_runtime_root(leaf, qn in method_qns, path):
            roots.add(qn)
        elif _is_cpp_operator_root(leaf, path):
            roots.add(qn)
        elif _is_c_cpp_entry_root(leaf, qn in method_qns, path):
            roots.add(qn)
        elif _is_java_serialization_root(
            leaf.split(cs.CHAR_PAREN_OPEN, 1)[0], qn in method_qns, path
        ):
            roots.add(qn)
        elif _is_csharp_attribute_root(props, path):
            roots.add(qn)
        elif _is_csharp_dispose_root(
            leaf.split(cs.CHAR_PAREN_OPEN, 1)[0], qn in method_qns, path
        ):
            roots.add(qn)
        elif _is_csharp_operator_or_finalizer_root(leaf, path):
            roots.add(qn)
        elif any(qn.endswith(entry) for entry in config.entry_points):
            roots.add(qn)
        elif config.include_tests and _matches_test_path(path, config.test_patterns):
            roots.add(qn)

    adjacency: dict[str, set[str]] = defaultdict(set)
    # OVERRIDES is recorded overrider -> overridden; keep the REVERSE mapping
    # (overridden -> overriders) to expand virtual-dispatch targets below.
    override_rev: dict[str, set[str]] = defaultdict(set)
    for from_label, from_val, rel_type, _to_label, to_val in rels:
        if rel_type in traversal:
            adjacency[str(from_val)].add(str(to_val))
        elif rel_type == _OVERRIDES:
            override_rev[str(to_val)].add(str(from_val))

    live = set(roots)
    _walk(roots, adjacency, live)

    # Second expansion: a decorated function DEFINED by a LIVE owner is
    # framework-registered when the owner runs, so it and its callees are
    # live; the closure of a DEAD owner never registers and stays in the
    # reported cluster. ponytail: one round, so a registration chain nested
    # two closures deep is missed; iterate to fixed point if real code ever
    # registers closures from inside registered closures.
    closure_roots = {
        c
        for o, c in defines_pairs
        if o in live
        and c not in live
        and c in props_by_qn
        and props_by_qn[c].get(cs.KEY_DECORATORS)
    }
    live |= closure_roots
    _walk(closure_roots, adjacency, live)

    # Factory-class and override expansions, iterated together to a fixed
    # point because they feed each other (a factory revived only via an
    # override's callee still needs its class rooted, and vice versa).
    #
    # Factory-class rule: a class defined inside a LIVE function
    # (django's create_reverse_many_to_one_manager) escapes via its return
    # value or arguments, so no call edge lands on its methods. Treat them as
    # dispatch surface and revive their callee closure; a DEAD factory's
    # class stays dead.
    #
    # Override rule: a call to a base or interface method dispatches at
    # runtime to any override, so every transitive override of a LIVE method
    # is a reachable dispatch target, as is its callee closure. `override_rev`
    # walks all multi-level overriders (Base<-Sub<-SubSub); an override of a
    # DEAD base stays dead.
    #
    # Each round scans only nodes revived since the last (the pair maps and
    # override_rev are static, so a rescanned node yields nothing new),
    # keeping the loop O(live) total; a round that adds nothing ends it.
    classes_by_owner: dict[str, set[str]] = defaultdict(set)
    for owner, cls in nested_class_pairs:
        classes_by_owner[owner].add(cls)
    methods_by_class: dict[str, set[str]] = defaultdict(set)
    for cls, m in class_methods:
        methods_by_class[cls].add(m)

    frontier = set(live)
    while frontier:
        added: set[str] = set()

        factory_method_roots: set[str] = set()
        for owner in frontier:
            for cls in classes_by_owner.get(owner, ()):
                if cls not in live:
                    live.add(cls)
                    added.add(cls)
                factory_method_roots |= methods_by_class[cls] - live
        live |= factory_method_roots
        added |= factory_method_roots
        _walk(factory_method_roots, adjacency, live, added=added)

        override_roots: set[str] = set()
        stack = list(frontier | added)
        while stack:
            for overrider in override_rev.get(stack.pop(), ()):
                if overrider not in live and overrider not in override_roots:
                    override_roots.add(overrider)
                    stack.append(overrider)
        live |= override_roots
        added |= override_roots
        _walk(override_roots, adjacency, live, added=added)

        frontier = added

    dead = candidates - live
    # Suppress generated files (openapi-ts client/core, routeTree.gen.ts) from
    # the REPORT only, after reachability: they stay full participants as roots
    # and callers, so a real function invoked only from generated glue is not
    # newly flagged; excluding earlier would drop those live edges.
    if config.exclude_patterns:
        dead = {
            qn
            for qn in dead
            if not any(
                fnmatch(str(props_by_qn[qn].get(cs.KEY_PATH) or ""), pattern)
                for pattern in config.exclude_patterns
            )
        }
    return dead


def _as_str_list(value: ResultValue | None) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


def _node_props(row: ResultRow) -> PropertyDict:
    # Coalesce NULL column values at the fetch boundary so the engine never
    # sees None where a str/list/bool is expected. Only properties the engine
    # reads are kept; the report is built from the raw rows.
    return {
        cs.KEY_PATH: str(row.get(cs.KEY_PATH) or ""),
        cs.KEY_DECORATORS: _as_str_list(row.get(cs.KEY_DECORATORS)),
        cs.KEY_IS_EXPORTED: row.get(cs.KEY_IS_EXPORTED) is True,
        cs.KEY_OVERRIDES_EXTERNAL: row.get(cs.KEY_OVERRIDES_EXTERNAL) is True,
    }


def _row_qn(row: ResultRow) -> str:
    return str(row.get(cs.KEY_QUALIFIED_NAME) or "")


def collect_dead_code(
    ingestor: GraphQueryClient, project_name: str, config: DeadCodeConfig
) -> list[ResultRow]:
    prefix = project_name + cs.SEPARATOR_DOT
    params: dict[str, PropertyValue] = {cs.KEY_PROJECT_PREFIX: prefix}

    node_rows = ingestor.fetch_all(cq.CYPHER_DEAD_CODE_NODES, params)
    nodes: dict[_NodeId, PropertyDict] = {
        (str(row.get(cs.KEY_LABEL) or ""), _row_qn(row)): _node_props(row)
        for row in node_rows
    }

    rels: list[_RelTuple] = [
        (
            str(row.get(cs.KEY_FROM_LABEL) or ""),
            str(row.get(cs.KEY_FROM_QN) or ""),
            str(row.get(cs.KEY_REL_TYPE) or ""),
            str(row.get(cs.KEY_TO_LABEL) or ""),
            str(row.get(cs.KEY_TO_QN) or ""),
        )
        for row in ingestor.fetch_all(cq.CYPHER_DEAD_CODE_RELS, params)
    ]

    dead = dead_code_from_graph(nodes, rels, prefix, config)
    rows = [row for row in node_rows if _row_qn(row) in dead]
    rows.sort(key=_row_qn)
    return rows
