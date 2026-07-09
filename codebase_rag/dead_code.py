# (H) Dead-code reachability engine. Roots (entry points, framework hooks,
# (H) module-load callees, test code) are expanded over CALLS/REFERENCES edges;
# (H) whatever is never reached is reported. Reachability runs client-side in
# (H) Python: the per-root *BFS Cypher formulation is O(roots x graph) and hit
# (H) memgraph's 600s query timeout on big projects (django: 31k roots, 101k
# (H) CALLS edges), while a multi-source walk over the fetched edge list is
# (H) linear and finishes in milliseconds.
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
# (H) Rounds of override-reachability expansion. Covers depth-N override/callee
# (H) interleaving (a base revived via an override's callee, whose own overrides
# (H) then need reviving); a round that adds nothing breaks out early.
_OVERRIDE_EXPANSION_ROUNDS = 3

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
    # (H) Drop '@', take the text before '(', then the last dotted segment,
    # (H) lowercased -> `@app.route(...)` becomes `route`.
    head = decorator.replace(cs.DECORATOR_AT, "").split(cs.CHAR_PAREN_OPEN)[0]
    return head.split(cs.SEPARATOR_DOT)[-1].lower()


def _is_dunder(name: str) -> bool:
    # (H) A __dunder__ method is invoked by the Python runtime (async with, iteration,
    # (H) operators, ...), never by an explicit call the call graph can see, so it is a
    # (H) reachability root rather than dead code.
    return (
        len(name) > len(cs.PY_NAME_DUNDER) * 2
        and name.startswith(cs.PY_NAME_DUNDER)
        and name.endswith(cs.PY_NAME_DUNDER)
    )


def _is_rust_runtime_root(name: str, is_method: bool, path: str) -> bool:
    # (H) A Rust `.rs` symbol the language/runtime invokes with no call site: `fn
    # (H) main()` (entry) or a trait-impl method (Display::fmt, Iterator::next, ...).
    # (H) Name-scoped like Python dunders; trait methods must be methods.
    if not path.endswith(cs.EXT_RS):
        return False
    # (H) `main` is only the entry point as a receiverless `fn main()`; a method
    # (H) named main is not, so gate it to non-methods. Trait methods are the reverse.
    if name in cs.RUST_ROOT_FUNCTION_NAMES:
        return not is_method
    return is_method and name in cs.RUST_TRAIT_METHOD_NAMES


def _is_cpp_operator_root(name: str, path: str) -> bool:
    # (H) A C++ operator overload / user-defined literal (`operator==`, `operator[]`,
    # (H) `operator""_json`) is invoked by operator/literal SYNTAX, not a named call the
    # (H) graph can see, so it is a reachability root (like Python dunders / Rust trait
    # (H) methods). `operator` heads every such definition (member or free), so the name
    # (H) prefix on a C++ file identifies them uniquely.
    return name.startswith(cs.CPP_OPERATOR_PREFIX) and path.endswith(cs.CPP_EXTENSIONS)


def _is_java_serialization_root(name: str, is_method: bool, path: str) -> bool:
    # (H) A Java serialization hook (`readObject`/`writeObject`/`writeReplace`/
    # (H) `readResolve`/`readObjectNoData`) is invoked reflectively by the java.io
    # (H) serialization runtime, never by a named call the graph can see, so it is a
    # (H) reachability root (like Python dunders / Rust trait methods). Gated to methods
    # (H) on a .java file; `name` is the bare method name (signature stripped by caller).
    return (
        is_method
        and path.endswith(cs.EXT_JAVA)
        and name in cs.JAVA_SERIALIZATION_METHOD_NAMES
    )


def _matches_test_path(path: str, patterns: tuple[str, ...]) -> bool:
    # (H) Match test-path patterns against a leading-slash-normalized path so a dir
    # (H) pattern like `/tests/` also matches a ROOT `tests/` dir (Rust integration
    # (H) tests, a top-level tests/ folder) -- not just a nested `src/tests/`. The
    # (H) leading slash keeps `contests/` from matching `/tests/` (no false segment).
    normalized = (
        path if path.startswith(cs.SEPARATOR_SLASH) else cs.SEPARATOR_SLASH + path
    )
    return any(pattern in normalized for pattern in patterns)


def _has_root_decorator(props: PropertyDict, root_decorators: frozenset[str]) -> bool:
    decorators = props.get(cs.KEY_DECORATORS)
    if not isinstance(decorators, list):
        return False
    return any(_norm_decorator(str(d)) in root_decorators for d in decorators)


def _walk(frontier: set[str], adjacency: dict[str, set[str]], live: set[str]) -> None:
    stack = list(frontier)
    while stack:
        current = stack.pop()
        for nxt in adjacency.get(current, ()):
            if nxt not in live:
                live.add(nxt)
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
            # (H) With tests excluded, a test-file symbol's only callers are
            # (H) excluded as roots, so reporting it is unconditional noise (test
            # (H) helpers and mocks are infrastructure, not dead production code).
            if not config.include_tests and _matches_test_path(
                str(props.get(cs.KEY_PATH) or ""), config.test_patterns
            ):
                continue
            candidates.add(str(uid))
            props_by_qn[str(uid)] = props
            if label == _METHOD:
                method_qns.add(str(uid))

    roots: set[str] = set()
    # (H) A method of a typing.Protocol subclass is an interface stub whose callers
    # (H) resolve to the implementations, and DEFINES edges from functions/methods
    # (H) feed the live-owner registration round below.
    defines_pairs: list[tuple[str, str]] = []
    protocol_classes: set[str] = set()
    class_methods: list[tuple[str, str]] = []
    for from_label, from_val, rel_type, _to_label, to_val in rels:
        if rel_type == _DEFINES and from_label in (_FUNCTION, _METHOD):
            defines_pairs.append((str(from_val), str(to_val)))
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
        if _has_root_decorator(props, config.root_decorators):
            roots.add(qn)
        elif props.get(cs.KEY_IS_EXPORTED) is True:
            roots.add(qn)
        # (H) A method overriding an EXTERNAL stdlib base's method (click's
        # (H) textwrap.TextWrapper subclass) is invoked by the base's machinery,
        # (H) never by a first-party call -- a root.
        elif props.get(cs.KEY_OVERRIDES_EXTERNAL) is True:
            roots.add(qn)
        elif qn in protocol_stubs:
            roots.add(qn)
        elif (
            qn in method_qns
            and _is_dunder(qn.rsplit(cs.SEPARATOR_DOT, 1)[-1])
            and str(props.get(cs.KEY_PATH, "")).endswith(cs.EXT_PY)
        ):
            roots.add(qn)
        elif (
            qn not in method_qns
            and qn.rsplit(cs.SEPARATOR_DOT, 1)[-1] in cs.GO_ROOT_FUNCTION_NAMES
            and str(props.get(cs.KEY_PATH, "")).endswith(cs.EXT_GO)
        ):
            roots.add(qn)
        elif _is_rust_runtime_root(
            qn.rsplit(cs.SEPARATOR_DOT, 1)[-1],
            qn in method_qns,
            str(props.get(cs.KEY_PATH, "")),
        ):
            roots.add(qn)
        elif _is_cpp_operator_root(
            qn.rsplit(cs.SEPARATOR_DOT, 1)[-1], str(props.get(cs.KEY_PATH, ""))
        ):
            roots.add(qn)
        elif _is_java_serialization_root(
            qn.rsplit(cs.SEPARATOR_DOT, 1)[-1].split(cs.CHAR_PAREN_OPEN, 1)[0],
            qn in method_qns,
            str(props.get(cs.KEY_PATH, "")),
        ):
            roots.add(qn)
        elif any(qn.endswith(entry) for entry in config.entry_points):
            roots.add(qn)
        elif config.include_tests and _matches_test_path(
            str(props.get(cs.KEY_PATH, "")), config.test_patterns
        ):
            roots.add(qn)

    adjacency: dict[str, set[str]] = defaultdict(set)
    # (H) OVERRIDES is recorded overrider -> overridden; keep the REVERSE mapping
    # (H) (overridden -> overriders) to expand virtual-dispatch targets below.
    override_rev: dict[str, set[str]] = defaultdict(set)
    for from_label, from_val, rel_type, _to_label, to_val in rels:
        if rel_type in traversal:
            adjacency[str(from_val)].add(str(to_val))
        elif rel_type == _OVERRIDES:
            override_rev[str(to_val)].add(str(from_val))

    live = set(roots)
    _walk(roots, adjacency, live)

    # (H) Second expansion: a decorated function DEFINED by a LIVE owner is
    # (H) framework-registered when the owner runs, so it and its callees are
    # (H) live; the closure of a DEAD owner never registers and stays in the
    # (H) reported cluster. ponytail: one round, so a registration chain nested
    # (H) two closures deep is missed; iterate to fixed point if real code ever
    # (H) registers closures from inside registered closures.
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

    # (H) Override expansion: a call to a base or interface method dispatches at
    # (H) runtime to any override, so every (transitive) override of a LIVE method
    # (H) is a reachable dispatch target, as is its callee closure. `override_rev`
    # (H) walks all multi-level overriders (Base<-Sub<-SubSub); an override of a
    # (H) DEAD base stays dead. Run several rounds because a base can go live only
    # (H) via a revived override's CALLEE (depth-2+ interleaving); one pass would
    # (H) miss those. A round that adds nothing is a no-op.
    for _ in range(_OVERRIDE_EXPANSION_ROUNDS):
        override_roots: set[str] = set()
        stack = list(live)
        while stack:
            for overrider in override_rev.get(stack.pop(), ()):
                if overrider not in live and overrider not in override_roots:
                    override_roots.add(overrider)
                    stack.append(overrider)
        if not override_roots:
            break
        live |= override_roots
        _walk(override_roots, adjacency, live)

    dead = candidates - live
    # (H) Suppress generated files (openapi-ts client/core, routeTree.gen.ts) from
    # (H) the REPORT only, after reachability: they stay full participants as roots
    # (H) and callers, so a real function invoked only from generated glue is not
    # (H) newly flagged -- excluding earlier would drop those live edges.
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
    # (H) Coalesce NULL column values at the fetch boundary so the engine never
    # (H) sees None where a str/list/bool is expected. Only the properties the
    # (H) engine reads are kept; the report is built from the raw rows.
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
