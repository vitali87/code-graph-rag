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


def extract_cgr_go_nodes(target: Path, project_name: str) -> dict[NodeKey, DefNode]:
    return extract_cgr_lang_nodes(
        target, project_name, ec.GO_SUFFIX, ec.GO_SCORED_NODE_KIND_VALUES
    )


def extract_cgr_rust_nodes(target: Path, project_name: str) -> dict[NodeKey, DefNode]:
    return extract_cgr_lang_nodes(
        target, project_name, ec.RS_SUFFIX, ec.RS_SCORED_NODE_KIND_VALUES
    )


def extract_cgr_java_nodes(target: Path, project_name: str) -> dict[NodeKey, DefNode]:
    return extract_cgr_lang_nodes(
        target, project_name, ec.JAVA_SUFFIX, ec.JAVA_SCORED_NODE_KIND_VALUES
    )


def extract_cgr_js_nodes(target: Path, project_name: str) -> dict[NodeKey, DefNode]:
    return extract_cgr_lang_nodes(
        target, project_name, ec.JS_SUFFIXES, ec.JS_SCORED_NODE_KIND_VALUES
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
