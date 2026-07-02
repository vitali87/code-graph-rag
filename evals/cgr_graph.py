from pathlib import Path

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.types_defs import PropertyDict, PropertyValue, ResultRow

from . import constants as ec
from .types_defs import DefNode, EdgeKey, GraphData, NameEdge, NodeKey

_RelTuple = tuple[str, PropertyValue, str, str, PropertyValue]
_NodeId = tuple[str, PropertyValue]


class _CapturingIngestor:
    def __init__(self) -> None:
        self.nodes: dict[_NodeId, PropertyDict] = {}
        self.rels: list[_RelTuple] = []

    def ensure_node_batch(self, label: str, properties: PropertyDict) -> None:
        uid = properties[cs.NODE_UNIQUE_CONSTRAINTS[label]]
        self.nodes[(str(label), uid)] = dict(properties)

    def ensure_relationship_batch(
        self,
        from_spec: tuple[str, str, PropertyValue],
        rel_type: str,
        to_spec: tuple[str, str, PropertyValue],
        properties: PropertyDict | None = None,
    ) -> None:
        from_label, _from_key, from_val = from_spec
        to_label, _to_key, to_val = to_spec
        self.rels.append(
            (str(from_label), from_val, str(rel_type), str(to_label), to_val)
        )

    def flush_all(self) -> None:
        return None

    def fetch_all(
        self, query: str, params: PropertyDict | None = None
    ) -> list[ResultRow]:
        return []

    def execute_write(self, query: str, params: PropertyDict | None = None) -> None:
        return None


_MODULE_LABEL = cs.NodeLabel.MODULE.value
_FILE_LABEL = cs.NodeLabel.FILE.value
_FOLDER_LABEL = cs.NodeLabel.FOLDER.value
_DEFINES_RELS = frozenset(
    {
        cs.RelationshipType.DEFINES.value,
        cs.RelationshipType.DEFINES_METHOD.value,
    }
)
_DEFINITION_LABELS = frozenset(
    {
        cs.NodeLabel.FUNCTION.value,
        cs.NodeLabel.METHOD.value,
        cs.NodeLabel.CLASS.value,
        cs.NodeLabel.INTERFACE.value,
        cs.NodeLabel.ENUM.value,
        cs.NodeLabel.TYPE.value,
        cs.NodeLabel.UNION.value,
    }
)
_INBOUND_DEPENDENT_RELS = frozenset(
    {
        cs.RelationshipType.CALLS.value,
        cs.RelationshipType.INSTANTIATES.value,
        cs.RelationshipType.IMPORTS.value,
        cs.RelationshipType.INHERITS.value,
        cs.RelationshipType.OVERRIDES.value,
    }
)
_INHERITS_REL = cs.RelationshipType.INHERITS.value


def _text(value: PropertyValue) -> str | None:
    # (H) path / qualified_name / absolute_path are always textual; narrow the
    # (H) general PropertyValue (which includes list[str]) so the row matches the
    # (H) ResultValue shape the prune query consumer expects.
    return value if isinstance(value, str) else None


class _StatefulIngestor:
    # (H) A faithful in-memory stand-in for the persistent graph store. Unlike
    # (H) _CapturingIngestor it implements the QueryProtocol delete/fetch Cypher
    # (H) the incremental updater issues, so a graph mutated by an incremental run
    # (H) can be compared against a clean re-index. Only the exact queries cgr
    # (H) emits are emulated (matched by identity), nothing more.
    def __init__(self) -> None:
        self.nodes: dict[_NodeId, PropertyDict] = {}
        self.edges: set[_RelTuple] = set()
        self.edge_props: dict[_RelTuple, PropertyDict] = {}

    def ensure_node_batch(self, label: str, properties: PropertyDict) -> None:
        uid = properties[cs.NODE_UNIQUE_CONSTRAINTS[label]]
        self.nodes[(str(label), uid)] = dict(properties)

    def ensure_relationship_batch(
        self,
        from_spec: tuple[str, str, PropertyValue],
        rel_type: str,
        to_spec: tuple[str, str, PropertyValue],
        properties: PropertyDict | None = None,
    ) -> None:
        from_label, _from_key, from_val = from_spec
        to_label, _to_key, to_val = to_spec
        edge = (str(from_label), from_val, str(rel_type), str(to_label), to_val)
        self.edges.add(edge)
        if properties:
            self.edge_props[edge] = dict(properties)

    def flush_all(self) -> None:
        return None

    def fetch_all(
        self, query: str, params: PropertyDict | None = None
    ) -> list[ResultRow]:
        match query:
            case cs.CYPHER_ALL_FILE_PATHS:
                return self._path_rows(_FILE_LABEL)
            case cs.CYPHER_ALL_FOLDER_PATHS:
                return self._path_rows(_FOLDER_LABEL)
            case cs.CYPHER_INBOUND_EDGES:
                raw_paths = params.get(cs.CYPHER_PARAM_PATHS) if params else None
                changed: set[str] = (
                    set(raw_paths) if isinstance(raw_paths, list) else set()
                )
                inbound: list[ResultRow] = []
                for from_label, from_val, rel_type, to_label, to_val in self.edges:
                    if rel_type not in _INBOUND_DEPENDENT_RELS:
                        continue
                    target = self.nodes.get((to_label, to_val))
                    caller = self.nodes.get((from_label, from_val))
                    if target is None or caller is None:
                        continue
                    caller_path = caller.get(cs.KEY_PATH)
                    if target.get(cs.KEY_PATH) not in changed or caller_path in changed:
                        continue
                    inbound.append(
                        {
                            cs.KEY_CALLER_LABEL: from_label,
                            cs.KEY_CALLER_QN: _text(from_val),
                            cs.KEY_REL: rel_type,
                            cs.KEY_TARGET_LABEL: to_label,
                            cs.KEY_TARGET_QN: _text(to_val),
                        }
                    )
                return inbound
            case cs.CYPHER_ALL_DEFINITION_QNS:
                defs: list[ResultRow] = []
                for (label, uid), props in self.nodes.items():
                    if label not in _DEFINITION_LABELS:
                        continue
                    qn = props.get(cs.KEY_QUALIFIED_NAME, uid)
                    row: ResultRow = {
                        cs.KEY_QUALIFIED_NAME: _text(qn),
                        cs.KEY_LABEL: label,
                        cs.KEY_IS_PROPERTY: bool(props.get(cs.KEY_IS_PROPERTY)),
                    }
                    defs.append(row)
                return defs
            case cs.CYPHER_ALL_INHERITS:
                inherits: list[tuple[str, int, ResultRow]] = []
                for edge in self.edges:
                    _from_label, from_val, rel_type, _to_label, to_val = edge
                    if rel_type != _INHERITS_REL:
                        continue
                    raw_index = self.edge_props.get(edge, {}).get(cs.KEY_BASE_INDEX, 0)
                    index = raw_index if isinstance(raw_index, int) else 0
                    inherits.append(
                        (
                            str(_text(from_val)),
                            index,
                            {
                                cs.KEY_CHILD_QN: _text(from_val),
                                cs.KEY_BASE_QN: _text(to_val),
                            },
                        )
                    )
                inherits.sort(key=lambda item: (item[0], item[1]))
                return [row for _child, _index, row in inherits]
            case cs.CYPHER_ALL_MODULE_PATHS_INTERNAL:
                rows: list[ResultRow] = []
                for (label, _uid), props in self.nodes.items():
                    if label != _MODULE_LABEL or props.get(cs.KEY_IS_EXTERNAL) is True:
                        continue
                    row: ResultRow = {
                        cs.KEY_PATH: _text(props.get(cs.KEY_PATH)),
                        cs.KEY_QUALIFIED_NAME: _text(props.get(cs.KEY_QUALIFIED_NAME)),
                    }
                    rows.append(row)
                return rows
            case _:
                return []

    def execute_write(self, query: str, params: PropertyDict | None = None) -> None:
        path = params.get(cs.KEY_PATH) if params else None
        match query:
            case cs.CYPHER_DELETE_MODULE:
                self._delete_module_subtree(path)
            case cs.CYPHER_DELETE_FILE:
                self._detach_delete(self._nodes_at_path(_FILE_LABEL, path))
            case cs.CYPHER_DELETE_FOLDER:
                self._detach_delete(self._nodes_at_path(_FOLDER_LABEL, path))
            case cs.CYPHER_DELETE_ORPHAN_EXTERNAL_MODULES:
                self._delete_orphan_external_modules()
            case _:
                return None

    def _path_rows(self, label: str) -> list[ResultRow]:
        rows: list[ResultRow] = []
        for (node_label, _uid), props in self.nodes.items():
            if node_label != label:
                continue
            row: ResultRow = {
                cs.KEY_PATH: _text(props.get(cs.KEY_PATH)),
                cs.KEY_ABSOLUTE_PATH: _text(props.get(cs.KEY_ABSOLUTE_PATH)),
            }
            rows.append(row)
        return rows

    def _nodes_at_path(self, label: str, path: PropertyValue) -> set[_NodeId]:
        return {
            (node_label, uid)
            for (node_label, uid), props in self.nodes.items()
            if node_label == label and props.get(cs.KEY_PATH) == path
        }

    def _delete_module_subtree(self, path: PropertyValue) -> None:
        doomed: set[_NodeId] = set()
        frontier = list(self._nodes_at_path(_MODULE_LABEL, path))
        while frontier:
            node = frontier.pop()
            if node in doomed:
                continue
            doomed.add(node)
            for from_label, from_val, rel_type, to_label, to_val in self.edges:
                if rel_type in _DEFINES_RELS and (from_label, from_val) == node:
                    child = (to_label, to_val)
                    if child not in doomed:
                        frontier.append(child)
        self._detach_delete(doomed)

    def _delete_orphan_external_modules(self) -> None:
        incoming = {(to_label, to_val) for _f, _v, _r, to_label, to_val in self.edges}
        doomed = {
            (label, uid)
            for (label, uid), props in self.nodes.items()
            if label == _MODULE_LABEL
            and props.get(cs.KEY_IS_EXTERNAL) is True
            and (label, uid) not in incoming
        }
        self._detach_delete(doomed)

    def _detach_delete(self, doomed: set[_NodeId]) -> None:
        if not doomed:
            return
        for node in doomed:
            self.nodes.pop(node, None)
        self.edges = {
            edge
            for edge in self.edges
            if (edge[0], edge[1]) not in doomed and (edge[3], edge[4]) not in doomed
        }


def _capture(target: Path, project_name: str) -> _CapturingIngestor:
    parsers, queries = load_parsers()
    ingestor = _CapturingIngestor()
    GraphUpdater(
        ingestor=ingestor,
        repo_path=target,
        parsers=parsers,
        queries=queries,
        project_name=project_name,
    ).run(force=True)
    return ingestor


def extract_cgr_graph(target: Path, project_name: str) -> GraphData:
    return _to_graph_data(_capture(target, project_name), project_name)


def extract_cgr_calls(target: Path, project_name: str) -> set[tuple[str, str]]:
    ingestor = _capture(target, project_name)
    calls_value = cs.RelationshipType.CALLS.value
    return {
        (str(from_val), str(to_val))
        for from_label, from_val, rel_type, to_label, to_val in ingestor.rels
        if rel_type == calls_value
    }


def _lang_node_key(
    label: str, props: PropertyDict, suffix: str | tuple[str, ...]
) -> NodeKey | None:
    path = props.get(cs.KEY_PATH)
    if path is None:
        return None
    file = str(path)
    if not file.endswith(suffix):
        return None
    raw_start = props.get(cs.KEY_START_LINE)
    if not isinstance(raw_start, int | float):
        return None
    return NodeKey(label, file, int(raw_start))


def extract_cgr_lang_nodes(
    target: Path,
    project_name: str,
    suffix: str | tuple[str, ...],
    kind_values: frozenset[str],
) -> dict[NodeKey, DefNode]:
    ingestor = _capture(target, project_name)
    nodes: dict[NodeKey, DefNode] = {}
    for (label, _uid), props in ingestor.nodes.items():
        if label not in kind_values:
            continue
        key = _lang_node_key(label, props, suffix)
        if key is None:
            continue
        raw_end = props.get(cs.KEY_END_LINE)
        end_line = int(raw_end) if isinstance(raw_end, int | float) else 0
        nodes[key] = DefNode(key, str(props.get(cs.KEY_NAME, "")), end_line)
    return nodes


def _lang_endpoint_key(
    label: str,
    props: PropertyDict,
    suffix: str | tuple[str, ...],
    exclude_suffix: str | None = None,
) -> NodeKey | None:
    # (H) Resolve any node (incl. the per-file Module, which carries no
    # (H) start_line) to a NodeKey so containment edges can join on it. cgr keys
    # (H) module-level DEFINES parents at the module node; mirror the ast oracle
    # (H) by placing the module at MODULE_START_LINE.
    path = props.get(cs.KEY_PATH)
    if path is None:
        return None
    file = str(path)
    if not file.endswith(suffix):
        return None
    if exclude_suffix is not None and file.endswith(exclude_suffix):
        return None
    raw_start = props.get(cs.KEY_START_LINE)
    if label == cs.NodeLabel.MODULE.value:
        # (H) The per-file module carries no start line (keyed at line 0); an
        # (H) inline module (Rust `mod`) carries its declaration line, which keeps
        # (H) it distinct from the file module so nested containment can join.
        if isinstance(raw_start, int | float):
            return NodeKey(label, file, int(raw_start))
        return NodeKey(label, file, ec.MODULE_START_LINE)
    if not isinstance(raw_start, int | float):
        return None
    return NodeKey(label, file, int(raw_start))


def extract_cgr_lang_graph(
    target: Path,
    project_name: str,
    suffix: str | tuple[str, ...],
    kind_values: frozenset[str],
    exclude_suffix: str | None = None,
) -> GraphData:
    ingestor = _capture(target, project_name)
    nodes: dict[NodeKey, DefNode] = {}
    by_uid: dict[_NodeId, NodeKey] = {}
    for (label, uid), props in ingestor.nodes.items():
        endpoint = _lang_endpoint_key(label, props, suffix, exclude_suffix)
        if endpoint is None:
            continue
        by_uid[(label, uid)] = endpoint
        if label not in kind_values:
            continue
        raw_end = props.get(cs.KEY_END_LINE)
        end_line = int(raw_end) if isinstance(raw_end, int | float) else 0
        nodes[endpoint] = DefNode(endpoint, str(props.get(cs.KEY_NAME, "")), end_line)

    edges: set[EdgeKey] = set()
    name_edges: set[NameEdge] = set()
    for from_label, from_val, rel_type, to_label, to_val in ingestor.rels:
        if rel_type in ec.SCORED_EDGE_TYPE_VALUES:
            parent = by_uid.get((from_label, from_val))
            child = by_uid.get((to_label, to_val))
            if parent is not None and child is not None:
                edges.add(EdgeKey(rel_type, parent, child))
        elif rel_type in ec.INHERITANCE_NAME_EDGE_TYPE_VALUES:
            # (H) Inheritance is graded by the base's SIMPLE NAME (cgr's to-value
            # (H) is the resolved base qn, or the bare name when unresolved).
            source = by_uid.get((from_label, from_val))
            if source is not None:
                # (H) Base simple name: cgr's resolved target may be a dotted qn
                # (H) (`module.Base`) or a Rust path (`std::io::Read`), so split on
                # (H) both `.` and `::`.
                flat = str(to_val).replace(cs.SEPARATOR_DOUBLE_COLON, cs.SEPARATOR_DOT)
                target_name = flat.rsplit(cs.SEPARATOR_DOT, 1)[-1]
                name_edges.add(NameEdge(rel_type, source, target_name))
    return GraphData(nodes=nodes, edges=edges, name_edges=name_edges)


def restrict_to_files(graph: GraphData, files: set[str]) -> GraphData:
    # (H) Scope a graph to a file universe. A compile_commands.json oracle only
    # (H) "sees" files its compiled TUs reach, while cgr indexes the whole tree
    # (H) (bundled test deps, uncompiled sources). Grading cgr's out-of-universe
    # (H) nodes against that oracle is meaningless, so restrict cgr to the files
    # (H) the oracle actually parsed before scoring. Drops only false positives:
    # (H) no oracle node lives outside its own universe, so recall is untouched.
    nodes = {k: v for k, v in graph.nodes.items() if k.file in files}
    edges = {e for e in graph.edges if e.parent.file in files and e.child.file in files}
    name_edges = {n for n in graph.name_edges if n.source.file in files}
    return GraphData(nodes=nodes, edges=edges, name_edges=name_edges)


def extract_cgr_cpp_nodes(target: Path, project_name: str) -> dict[NodeKey, DefNode]:
    return extract_cgr_lang_nodes(
        target, project_name, ec.CPP_SUFFIXES, ec.CPP_SCORED_NODE_KIND_VALUES
    )


def extract_cgr_cpp_graph(target: Path, project_name: str) -> GraphData:
    return extract_cgr_lang_graph(
        target, project_name, ec.CPP_SUFFIXES, ec.CPP_SCORED_NODE_KIND_VALUES
    )


def extract_cgr_go_nodes(target: Path, project_name: str) -> dict[NodeKey, DefNode]:
    return extract_cgr_lang_nodes(
        target, project_name, ec.GO_SUFFIX, ec.GO_SCORED_NODE_KIND_VALUES
    )


def extract_cgr_go_graph(target: Path, project_name: str) -> GraphData:
    return extract_cgr_lang_graph(
        target, project_name, ec.GO_SUFFIX, ec.GO_SCORED_NODE_KIND_VALUES
    )


def extract_cgr_rust_nodes(target: Path, project_name: str) -> dict[NodeKey, DefNode]:
    return extract_cgr_lang_nodes(
        target, project_name, ec.RS_SUFFIX, ec.RS_SCORED_NODE_KIND_VALUES
    )


def extract_cgr_rust_graph(target: Path, project_name: str) -> GraphData:
    return extract_cgr_lang_graph(
        target, project_name, ec.RS_SUFFIX, ec.RS_SCORED_NODE_KIND_VALUES
    )


def extract_cgr_lua_nodes(target: Path, project_name: str) -> dict[NodeKey, DefNode]:
    return extract_cgr_lang_nodes(
        target, project_name, ec.LUA_SUFFIX, ec.LUA_SCORED_NODE_KIND_VALUES
    )


def extract_cgr_lua_graph(target: Path, project_name: str) -> GraphData:
    return extract_cgr_lang_graph(
        target, project_name, ec.LUA_SUFFIX, ec.LUA_SCORED_NODE_KIND_VALUES
    )


def extract_cgr_php_nodes(target: Path, project_name: str) -> dict[NodeKey, DefNode]:
    return extract_cgr_lang_nodes(
        target, project_name, ec.PHP_SUFFIX, ec.PHP_SCORED_NODE_KIND_VALUES
    )


def extract_cgr_php_graph(target: Path, project_name: str) -> GraphData:
    return extract_cgr_lang_graph(
        target, project_name, ec.PHP_SUFFIX, ec.PHP_SCORED_NODE_KIND_VALUES
    )


def extract_cgr_java_nodes(target: Path, project_name: str) -> dict[NodeKey, DefNode]:
    return extract_cgr_lang_nodes(
        target, project_name, ec.JAVA_SUFFIX, ec.JAVA_SCORED_NODE_KIND_VALUES
    )


def extract_cgr_java_graph(target: Path, project_name: str) -> GraphData:
    return extract_cgr_lang_graph(
        target, project_name, ec.JAVA_SUFFIX, ec.JAVA_SCORED_NODE_KIND_VALUES
    )


def extract_cgr_js_nodes(target: Path, project_name: str) -> dict[NodeKey, DefNode]:
    return extract_cgr_lang_nodes(
        target, project_name, ec.JS_SUFFIXES, ec.JS_SCORED_NODE_KIND_VALUES
    )


def extract_cgr_js_graph(target: Path, project_name: str) -> GraphData:
    return extract_cgr_lang_graph(
        target, project_name, ec.JS_SUFFIXES, ec.JS_SCORED_NODE_KIND_VALUES
    )


def extract_cgr_ts_graph(target: Path, project_name: str) -> GraphData:
    return extract_cgr_lang_graph(
        target,
        project_name,
        ec.TS_SUFFIXES,
        ec.TS_SCORED_NODE_KIND_VALUES,
        exclude_suffix=ec.TS_DTS_SUFFIX,
    )


def extract_cgr_ts_nodes(target: Path, project_name: str) -> dict[NodeKey, DefNode]:
    ingestor = _capture(target, project_name)
    nodes: dict[NodeKey, DefNode] = {}
    for (label, _uid), props in ingestor.nodes.items():
        if label not in ec.TS_SCORED_NODE_KIND_VALUES:
            continue
        path = props.get(cs.KEY_PATH)
        if path is None:
            continue
        file = str(path)
        # (H) Match the oracle: real .ts/.tsx sources, excluding .d.ts type stubs.
        if not file.endswith(ec.TS_SUFFIXES) or file.endswith(ec.TS_DTS_SUFFIX):
            continue
        raw_start = props.get(cs.KEY_START_LINE)
        if not isinstance(raw_start, int | float):
            continue
        key = NodeKey(label, file, int(raw_start))
        raw_end = props.get(cs.KEY_END_LINE)
        end_line = int(raw_end) if isinstance(raw_end, int | float) else 0
        nodes[key] = DefNode(key, str(props.get(cs.KEY_NAME, "")), end_line)
    return nodes


def _node_key(label: str, props: PropertyDict) -> NodeKey | None:
    path = props.get(cs.KEY_PATH)
    if path is None:
        return None
    file = str(path)
    if not file.endswith(ec.PY_SUFFIX):
        return None
    if label == cs.NodeLabel.MODULE.value:
        return NodeKey(label, file, ec.MODULE_START_LINE)
    raw_start = props.get(cs.KEY_START_LINE)
    if not isinstance(raw_start, int | float):
        return None
    return NodeKey(label, file, int(raw_start))


def _edge_allowed(rel_type: str, parent_kind: str) -> bool:
    if rel_type == cs.RelationshipType.DEFINES.value:
        return parent_kind == cs.NodeLabel.MODULE.value
    return parent_kind == cs.NodeLabel.CLASS.value


def _internal_target_file(qn: str, internal_modules: dict[str, str]) -> str | None:
    parts = qn.split(cs.SEPARATOR_DOT)
    while parts:
        candidate = cs.SEPARATOR_DOT.join(parts)
        if candidate in internal_modules:
            return internal_modules[candidate]
        parts = parts[:-1]
    return None


def _to_graph_data(ingestor: _CapturingIngestor, project_name: str) -> GraphData:
    nodes: dict[NodeKey, DefNode] = {}
    by_uid: dict[_NodeId, NodeKey] = {}
    for (label, uid), props in ingestor.nodes.items():
        if label not in ec.SCORED_NODE_KIND_VALUES:
            continue
        key = _node_key(label, props)
        if key is None:
            continue
        raw_end = props.get(cs.KEY_END_LINE)
        end_line = int(raw_end) if isinstance(raw_end, int | float) else 0
        name = str(props.get(cs.KEY_NAME, ""))
        nodes[key] = DefNode(key, name, end_line)
        by_uid[(label, uid)] = key

    edges: set[EdgeKey] = set()
    for from_label, from_val, rel_type, to_label, to_val in ingestor.rels:
        if rel_type not in ec.SCORED_EDGE_TYPE_VALUES:
            continue
        parent = by_uid.get((from_label, from_val))
        child = by_uid.get((to_label, to_val))
        if parent is None or child is None:
            continue
        if _edge_allowed(rel_type, parent.kind):
            edges.add(EdgeKey(rel_type, parent, child))

    prefix = project_name + cs.SEPARATOR_DOT
    # (H) Only real in-repo Python modules count as internal import targets. cgr
    # (H) also emits placeholder MODULE nodes for unresolved imports whose path is
    # (H) the dotted import name (e.g. "thrift.TTornado", "std.set"); requiring a
    # (H) .py path excludes those so IMPORTS is graded against real files only,
    # (H) consistent with the .py node filter and the ast oracle.
    internal_modules: dict[str, str] = {
        str(uid): str(props[cs.KEY_PATH])
        for (label, uid), props in ingestor.nodes.items()
        if label == cs.NodeLabel.MODULE.value
        and props.get(cs.KEY_PATH)
        and str(props[cs.KEY_PATH]).endswith(ec.PY_SUFFIX)
        and (str(uid) == project_name or str(uid).startswith(prefix))
    }

    name_edges: set[NameEdge] = set()
    for from_label, from_val, rel_type, _to_label, to_val in ingestor.rels:
        if rel_type not in ec.SCORED_NAME_EDGE_TYPE_VALUES:
            continue
        source = by_uid.get((from_label, from_val))
        if source is None:
            continue
        if rel_type == cs.RelationshipType.INHERITS.value:
            target = str(to_val).rsplit(cs.SEPARATOR_DOT, 1)[-1]
            name_edges.add(NameEdge(rel_type, source, target))
        elif rel_type == cs.RelationshipType.IMPORTS.value:
            target_path = _internal_target_file(str(to_val), internal_modules)
            if target_path is not None:
                name_edges.add(NameEdge(rel_type, source, target_path))

    return GraphData(nodes=nodes, edges=edges, name_edges=name_edges)
