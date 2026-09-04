from pathlib import Path

from codebase_rag import constants as cs
from codebase_rag import graph_updater as gu
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.types_defs import PropertyDict, PropertyValue, ResultRow

from . import constants as ec
from .ignore_rules import ignore_rules
from .types_defs import DefNode, EdgeKey, GraphData, NameEdge, NodeKey

_RelTuple = tuple[str, PropertyValue, str, str, PropertyValue]
_NodeId = tuple[str, PropertyValue]


class _CapturingIngestor:
    def __init__(self) -> None:
        self.nodes: dict[_NodeId, PropertyDict] = {}
        self.rels: list[_RelTuple] = []

    def ensure_node_batch(self, label: str, properties: PropertyDict) -> None:
        # Production writes are MERGE (n {qn}) SET n += props: additive. Some
        # passes deliberately re-emit a node with a PARTIAL row (the Rust
        # external-trait-override flag carries only qualified_name +
        # overrides_external), so replacing the dict would wipe path/span/
        # fingerprint off the already-captured definition.
        uid = properties[cs.NODE_UNIQUE_CONSTRAINTS[label]]
        self.nodes.setdefault((str(label), uid), {}).update(properties)

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
_EXTERNAL_MODULE_LABEL = cs.NodeLabel.EXTERNAL_MODULE.value
_FILE_LABEL = cs.NodeLabel.FILE.value
_FOLDER_LABEL = cs.NodeLabel.FOLDER.value
_PACKAGE_LABEL = cs.NodeLabel.PACKAGE.value
_DEFINES_RELS = frozenset(
    {
        cs.RelationshipType.DEFINES.value,
        cs.RelationshipType.DEFINES_METHOD.value,
    }
)
# Labels the C# partial-join and Go col-keyed rehydration queries select on.
_CSHARP_TYPE_LABELS = frozenset(
    {
        cs.NodeLabel.CLASS.value,
        cs.NodeLabel.INTERFACE.value,
        cs.NodeLabel.ENUM.value,
    }
)
_GO_TYPE_LABELS = _CSHARP_TYPE_LABELS | {
    cs.NodeLabel.TYPE.value,
    cs.NodeLabel.UNION.value,
}
# Queries this double deliberately does NOT model, so `case _` can raise for
# everything else without pretending these are graph reads (issue #1716).
#
# `CYPHER_QUERY_EMBEDDINGS` drives Pass 4, which reads function bodies and
# writes vectors to Qdrant. The double models the graph, not the vector store,
# and its caller already treats an empty result as "no functions to embed" and
# returns -- verified at graph_updater's embeddings pass, not assumed. An
# emulation would invite the double into work it cannot represent.
_NOT_MODELLED: frozenset[str] = frozenset({cs.CYPHER_QUERY_EMBEDDINGS})
_MODULE_QN_LABELS = frozenset(
    {
        _MODULE_LABEL,
        cs.NodeLabel.MODULE_INTERFACE.value,
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
        cs.RelationshipType.REFERENCES.value,
        cs.RelationshipType.INSTANTIATES.value,
        cs.RelationshipType.IMPORTS.value,
        cs.RelationshipType.INHERITS.value,
        cs.RelationshipType.IMPLEMENTS.value,
        cs.RelationshipType.OVERRIDES.value,
        cs.RelationshipType.RETURNS.value,
        cs.RelationshipType.ACCEPTS.value,
    }
)
# The dependency relations CYPHER_AFFECTED_CALLER_PATHS walks: a file holding
# one of these into a re-indexed file is re-parsed rather than restored
# verbatim (issue #1229 phase 4). REFERENCES is in, OVERRIDES is out, exactly
# as in the production query; IMPLEMENTS joined both lists with issue #1565.
_AFFECTED_CALLER_RELS = frozenset(
    {
        cs.RelationshipType.CALLS.value,
        cs.RelationshipType.REFERENCES.value,
        cs.RelationshipType.INSTANTIATES.value,
        cs.RelationshipType.IMPORTS.value,
        cs.RelationshipType.INHERITS.value,
        cs.RelationshipType.IMPLEMENTS.value,
    }
)
# Labels CYPHER_PROJECT_ROUTE_HANDLERS selects on.
_ROUTE_HANDLER_LABELS = frozenset(
    {cs.NodeLabel.FUNCTION.value, cs.NodeLabel.METHOD.value}
)
_INHERITS_REL = cs.RelationshipType.INHERITS.value


def _text(value: PropertyValue) -> str | None:
    # path / qualified_name / absolute_path are always textual; narrow the
    # general PropertyValue (which includes list[str]) to the ResultValue
    # shape the prune query consumer expects.
    return value if isinstance(value, str) else None


def _int(value: PropertyValue) -> int | None:
    # start_line / end_line are always integral; narrow the general
    # PropertyValue (whose list[str] member clashes with ResultValue) to the
    # ResultValue shape. bool is an int subclass but line numbers are never
    # bool, so the guard is exact.
    return value if isinstance(value, int) else None


class _StatefulIngestor:
    # A faithful in-memory stand-in for the persistent graph store. Unlike
    # _CapturingIngestor it implements the QueryProtocol delete/fetch Cypher
    # the incremental updater issues, so an incrementally mutated graph can be
    # compared against a clean re-index. Only the exact queries cgr emits are
    # emulated (matched by identity).
    def __init__(self) -> None:
        self.nodes: dict[_NodeId, PropertyDict] = {}
        self.edges: set[_RelTuple] = set()
        self.edge_props: dict[_RelTuple, PropertyDict] = {}
        # Adjacency indexes so a subtree delete costs the subtree, not a
        # scan of every edge per node (the real store has indexes too).
        self._out: dict[_NodeId, set[_RelTuple]] = {}
        self._in: dict[_NodeId, set[_RelTuple]] = {}

    def ensure_node_batch(self, label: str, properties: PropertyDict) -> None:
        # Same += merge semantics as _CapturingIngestor: partial re-emissions
        # must never wipe the properties of an already-ingested node.
        uid = properties[cs.NODE_UNIQUE_CONSTRAINTS[label]]
        self.nodes.setdefault((str(label), uid), {}).update(properties)

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
        self._out.setdefault((str(from_label), from_val), set()).add(edge)
        self._in.setdefault((str(to_label), to_val), set()).add(edge)
        if properties:
            self.edge_props[edge] = dict(properties)

    def reset_edges(self) -> None:
        self.edges = set()
        self.edge_props = {}
        self._out = {}
        self._in = {}

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
            case cs.CYPHER_ALL_PACKAGE_PATHS:
                return self._path_rows(_PACKAGE_LABEL)
            case cs.CYPHER_INBOUND_EDGES:
                raw_paths = params.get(cs.CYPHER_PARAM_PATHS) if params else None
                changed: set[str] = (
                    set(raw_paths) if isinstance(raw_paths, list) else set()
                )
                inbound: list[ResultRow] = []
                for edge in self._edges_into(changed):
                    from_label, from_val, rel_type, to_label, to_val = edge
                    if rel_type not in _INBOUND_DEPENDENT_RELS:
                        continue
                    caller = self.nodes.get((from_label, from_val))
                    if caller is None:
                        continue
                    caller_path = caller.get(cs.KEY_PATH)
                    if caller_path in changed:
                        continue
                    inbound.append(
                        {
                            cs.KEY_CALLER_LABEL: from_label,
                            cs.KEY_CALLER_QN: _text(from_val),
                            cs.KEY_REL: rel_type,
                            cs.KEY_TARGET_LABEL: to_label,
                            cs.KEY_TARGET_QN: _text(to_val),
                            cs.KEY_CALLER_PATH: _text(caller_path),
                            # The restore re-emits the edge with its own
                            # properties (its site, issue #1522).
                            cs.KEY_PROPS: dict(self.edge_props.get(edge, {})),
                        }
                    )
                return inbound
            case cs.CYPHER_AFFECTED_CALLER_PATHS:
                raw_paths = params.get(cs.CYPHER_PARAM_PATHS) if params else None
                prefix = str(params.get(cs.KEY_PROJECT_PREFIX, "")) if params else ""
                reindexed: set[str] = (
                    set(raw_paths) if isinstance(raw_paths, list) else set()
                )
                callers: set[str] = set()
                for (
                    from_label,
                    from_val,
                    rel_type,
                    to_label,
                    to_val,
                ) in self._edges_into(reindexed):
                    if rel_type not in _AFFECTED_CALLER_RELS:
                        continue
                    caller = self.nodes.get((from_label, from_val))
                    if caller is None:
                        continue
                    caller_path = caller.get(cs.KEY_PATH)
                    if (
                        not isinstance(caller_path, str)
                        or caller_path in reindexed
                        or not str(_text(from_val)).startswith(prefix)
                        or not str(_text(to_val)).startswith(prefix)
                    ):
                        continue
                    callers.add(caller_path)
                return [{cs.KEY_CALLER_PATH: path} for path in sorted(callers)]
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
                        cs.KEY_IS_MACRO: bool(props.get(cs.KEY_IS_MACRO)),
                        cs.KEY_PATH: _text(props.get(cs.KEY_PATH)),
                        cs.KEY_START_LINE: _int(props.get(cs.KEY_START_LINE)),
                        cs.KEY_END_LINE: _int(props.get(cs.KEY_END_LINE)),
                        cs.KEY_RETURN_TYPE: _text(props[cs.KEY_RETURN_TYPE])
                        if cs.KEY_RETURN_TYPE in props
                        else None,
                        cs.KEY_PARAM_TYPES: [_text(p) for p in raw_param_types]
                        if isinstance(
                            raw_param_types := props.get(cs.KEY_PARAM_TYPES), list
                        )
                        else None,
                    }
                    defs.append(row)
                return defs
            case cs.CYPHER_ALL_INHERITS:
                inherits: list[tuple[str, int, ResultRow]] = []
                for edge in self.edges:
                    _from_label, from_val, rel_type, _to_label, to_val = edge
                    if rel_type != _INHERITS_REL:
                        continue
                    raw_index = self.edge_props.get(edge, {}).get(cs.KEY_BASE_INDEX)
                    index = raw_index if isinstance(raw_index, int) else None
                    inherits.append(
                        (
                            str(_text(from_val)),
                            index if index is not None else 0,
                            {
                                cs.KEY_CHILD_QN: _text(from_val),
                                cs.KEY_BASE_QN: _text(to_val),
                                cs.KEY_BASE_INDEX: index,
                            },
                        )
                    )
                inherits.sort(key=lambda item: (item[0], item[1]))
                return [row for _child, _index, row in inherits]
            case cs.CYPHER_ALL_MODULE_QNS:
                module_rows: list[ResultRow] = []
                for (label, _uid), props in self.nodes.items():
                    if label not in _MODULE_QN_LABELS:
                        continue
                    module_row: ResultRow = {
                        cs.KEY_QUALIFIED_NAME: _text(props.get(cs.KEY_QUALIFIED_NAME)),
                        cs.KEY_LABEL: label,
                    }
                    module_rows.append(module_row)
                return module_rows
            case cs.CYPHER_PROJECT_MODULE_PATHS:
                project_name = (
                    _text(params.get(cs.KEY_PROJECT_NAME)) if params else None
                )
                prefix = _text(params.get(cs.KEY_PROJECT_PREFIX)) if params else None
                project_rows: list[ResultRow] = []
                for (label, _uid), props in self.nodes.items():
                    if label != _MODULE_LABEL:
                        continue
                    qn = _text(props.get(cs.KEY_QUALIFIED_NAME)) or ""
                    if qn == project_name or (prefix and qn.startswith(prefix)):
                        project_rows.append(
                            {cs.KEY_PATH: _text(props.get(cs.KEY_PATH))}
                        )
                return project_rows
            case cs.CYPHER_ALL_MODULE_PATHS_INTERNAL:
                rows: list[ResultRow] = []
                for (label, _uid), props in self.nodes.items():
                    if label != _MODULE_LABEL:
                        continue
                    row: ResultRow = {
                        cs.KEY_PATH: _text(props.get(cs.KEY_PATH)),
                        cs.KEY_QUALIFIED_NAME: _text(props.get(cs.KEY_QUALIFIED_NAME)),
                    }
                    rows.append(row)
                return rows
            case gu.CYPHER_PROJECT_MODULES | gu.CYPHER_PROJECT_PY_MODULES:
                # Route rehydration. These MUST be emulated rather than left to
                # the refusal below: their readers wrap the call in
                # `except Exception: return []`, so a raise is caught and
                # turned straight back into an empty result -- the fail-closed
                # default defeated one layer down, invisibly (raised on #1716).
                prefix = _text(params.get(cs.KEY_PROJECT_PREFIX)) if params else None
                raw_exts = params.get("extensions") if params else None
                # CYPHER_PROJECT_MODULES filters on a passed extension list;
                # the PY variant hardcodes `.py` in its Cypher.
                suffixes = (
                    tuple(e for e in raw_exts if isinstance(e, str))
                    if isinstance(raw_exts, list)
                    else (".py",)
                )
                return [
                    {cs.KEY_QUALIFIED_NAME: qn, cs.KEY_PATH: path}
                    for (label, _uid), props in self.nodes.items()
                    if label == _MODULE_LABEL
                    and (qn := _text(props.get(cs.KEY_QUALIFIED_NAME)) or "")
                    and prefix
                    and qn.startswith(prefix)
                    and (path := _text(props.get(cs.KEY_PATH)) or "").endswith(suffixes)
                ]
            case gu.CYPHER_PROJECT_ROUTE_HANDLERS:
                prefix = _text(params.get(cs.KEY_PROJECT_PREFIX)) if params else None
                return [
                    {
                        cs.KEY_LABELS: [label],
                        cs.KEY_QUALIFIED_NAME: qn,
                        "decorators": [d for d in decorators if isinstance(d, str)],
                    }
                    for (label, _uid), props in self.nodes.items()
                    if label in _ROUTE_HANDLER_LABELS
                    and (qn := _text(props.get(cs.KEY_QUALIFIED_NAME)) or "")
                    and prefix
                    and qn.startswith(prefix)
                    and isinstance(decorators := props.get("decorators"), list)
                    and decorators
                ]
            case cs.CYPHER_UNRESOLVED_IMPORTER_PATHS:
                # Importers whose IMPORTS edge points at an UNRESOLVED target
                # named after one of the given modules (issue #1682). Emulated
                # for the same reason as the specifier query below: falling
                # through returned [], which reads as "no waiters" and is why
                # that issue's tests could only cover the query layer.
                names = params.get(cs.CYPHER_PARAM_MODULE_NAMES) if params else None
                prefix = _text(params.get(cs.KEY_PROJECT_PREFIX)) if params else None
                wanted_names = (
                    [n for n in names if isinstance(n, str)]
                    if isinstance(names, list)
                    else []
                )
                importer_rows: list[ResultRow] = []
                if not wanted_names or not prefix:
                    return importer_rows
                seen_paths: set[str] = set()
                for from_label, from_val, rel_type, _to_label, to_val in self.edges:
                    if rel_type != cs.RelationshipType.IMPORTS.value:
                        continue
                    importer = self.nodes.get((from_label, from_val))
                    if importer is None:
                        continue
                    importer_path = _text(importer.get(cs.KEY_PATH))
                    importer_qn = _text(importer.get(cs.KEY_QUALIFIED_NAME)) or ""
                    if not importer_path or not importer_qn.startswith(prefix):
                        continue
                    target_qn = _text(to_val) or ""
                    # The target must be UNRESOLVED: a first-party one is a
                    # real module and its importer is found by the inbound
                    # edge lookup instead.
                    if target_qn.startswith(prefix):
                        continue
                    if any(
                        target_qn == name
                        or target_qn.startswith(f"{name}{cs.SEPARATOR_DOT}")
                        for name in wanted_names
                    ):
                        if importer_path not in seen_paths:
                            seen_paths.add(importer_path)
                            importer_rows.append({cs.KEY_CALLER_PATH: importer_path})
                return importer_rows
            case cs.CYPHER_UNRESOLVED_SPECIFIER_IMPORTERS:
                # Modules carrying a dropped relative specifier (issue #1714).
                # Emulated rather than left to fall through: an unanswered
                # query returns [] here, which is indistinguishable from "no
                # waiters" and would let an end-to-end test pass against a
                # lookup that never ran.
                prefix = _text(params.get(cs.KEY_PROJECT_PREFIX)) if params else None
                waiter_rows: list[ResultRow] = []
                for (label, _uid), props in self.nodes.items():
                    if label != _MODULE_LABEL:
                        continue
                    path = _text(props.get(cs.KEY_PATH))
                    qn = _text(props.get(cs.KEY_QUALIFIED_NAME)) or ""
                    specifiers = props.get(cs.KEY_UNRESOLVED_SPECIFIERS)
                    if not path or not (prefix and qn.startswith(prefix)):
                        continue
                    if not isinstance(specifiers, list) or not specifiers:
                        continue
                    waiter_rows.append(
                        {
                            cs.KEY_CALLER_PATH: path,
                            cs.CYPHER_KEY_SPECIFIERS: list(specifiers),
                        }
                    )
                return waiter_rows
            case cs.CYPHER_COUNT_PROJECT_MODULES:
                # A COUNT query yields exactly ONE row. Falling through to []
                # was the sharpest of the gaps: its caller reads `rows[0]
                # ["count"]`, so an empty list raises IndexError, the handler
                # returns, and the orphaned-hash-cache check never runs at all
                # (issue #1716).
                project_name = (
                    _text(params.get(cs.KEY_PROJECT_NAME)) if params else None
                )
                prefix = _text(params.get(cs.KEY_PROJECT_PREFIX)) if params else None
                total = sum(
                    1
                    for (label, _uid), props in self.nodes.items()
                    if label == _MODULE_LABEL
                    and (
                        (qn := _text(props.get(cs.KEY_QUALIFIED_NAME)) or "")
                        == project_name
                        or (prefix and qn.startswith(prefix))
                    )
                )
                return [{cs.KEY_COUNT: total}]
            case cs.CYPHER_ALL_CSHARP_TYPE_LOCATIONS:
                prefix = _text(params.get(cs.KEY_PROJECT_PREFIX)) if params else None
                return [
                    {
                        cs.KEY_QUALIFIED_NAME: qn,
                        cs.KEY_PATH: path,
                        cs.KEY_START_LINE: _int(props.get(cs.KEY_START_LINE)),
                    }
                    for (label, _uid), props in self.nodes.items()
                    if label in _CSHARP_TYPE_LABELS
                    and (qn := _text(props.get(cs.KEY_QUALIFIED_NAME)) or "")
                    and prefix
                    and qn.startswith(prefix)
                    and (path := _text(props.get(cs.KEY_PATH)) or "").endswith(
                        cs.EXT_CS
                    )
                ]
            case cs.CYPHER_ALL_GO_TYPE_LOCATIONS:
                prefix = _text(params.get(cs.KEY_PROJECT_PREFIX)) if params else None
                return [
                    {
                        cs.KEY_LABEL: label,
                        cs.KEY_QUALIFIED_NAME: qn,
                        cs.KEY_PATH: _text(props.get(cs.KEY_PATH)),
                        cs.KEY_START_LINE: _int(props.get(cs.KEY_START_LINE)),
                        cs.KEY_START_COL: _int(props.get(cs.KEY_START_COL)),
                    }
                    for (label, _uid), props in self.nodes.items()
                    if label in _GO_TYPE_LABELS
                    and (qn := _text(props.get(cs.KEY_QUALIFIED_NAME)) or "")
                    and prefix
                    and qn.startswith(prefix)
                ]
            case cs.CYPHER_ALL_FUNCTION_LOCATIONS:
                prefix = _text(params.get(cs.KEY_PROJECT_PREFIX)) if params else None
                return self._definition_location_rows(
                    prefix, cs.NodeLabel.FUNCTION.value
                )
            case cs.CYPHER_ALL_METHOD_LOCATIONS:
                prefix = _text(params.get(cs.KEY_PROJECT_PREFIX)) if params else None
                return self._definition_location_rows(prefix, cs.NodeLabel.METHOD.value)
            case _:
                if query in _NOT_MODELLED:
                    return []
                # Fail CLOSED. An unemulated query answered [] is
                # indistinguishable from a genuinely empty result, so a test
                # can pass because a lookup silently returned nothing -- which
                # is how #1682's waiting-importer path went uncovered for a
                # whole release (issue #1716). A new query must be emulated
                # here, or named in _NOT_MODELLED with a reason.
                raise AssertionError(
                    f"{type(self).__name__} does not emulate this query, and it "
                    f"is not in _NOT_MODELLED. Add a case or a reason:\n{query}"
                )

    def _definition_location_rows(
        self, prefix: str | None, target_label: str
    ) -> list[ResultRow]:
        """Rows for the Function/Method location rehydration queries.

        Both walk Module -[:DEFINES]-> ... to the definition; the Method form
        has one more hop through its container and returns `container_qn`.
        Shared so the two cannot drift apart in what counts as "in project".
        """
        if not prefix:
            return []
        rows: list[ResultRow] = []
        for from_label, from_val, rel, to_label, to_val in self.edges:
            if rel != cs.RelationshipType.DEFINES.value:
                continue
            if from_label != _MODULE_LABEL:
                continue
            module_qn = _text(from_val) or ""
            if not module_qn.startswith(prefix):
                continue
            if target_label == cs.NodeLabel.FUNCTION.value:
                if to_label != target_label:
                    continue
                pairs = [(None, (to_label, to_val))]
            else:
                # Module -DEFINES-> container -DEFINES_METHOD-> Method.
                pairs = [
                    (to_val, (m_label, m_val))
                    for (c_label, c_val, m_rel, m_label, m_val) in self.edges
                    if m_rel == cs.RelationshipType.DEFINES_METHOD.value
                    and (c_label, c_val) == (to_label, to_val)
                    and m_label == target_label
                ]
            for container_qn, node_id in pairs:
                props = self.nodes.get(node_id)
                if props is None:
                    continue
                row: ResultRow = {
                    cs.KEY_LABEL: node_id[0],
                    cs.KEY_QUALIFIED_NAME: _text(props.get(cs.KEY_QUALIFIED_NAME)),
                    cs.KEY_MODULE_QN: module_qn,
                    cs.KEY_START_LINE: _int(props.get(cs.KEY_START_LINE)),
                    cs.KEY_START_COL: _int(props.get(cs.KEY_START_COL)),
                    cs.KEY_NAME_START_LINE: _int(props.get(cs.KEY_NAME_START_LINE)),
                    cs.KEY_NAME_START_COL: _int(props.get(cs.KEY_NAME_START_COL)),
                }
                if container_qn is not None:
                    row[cs.KEY_CONTAINER_QN] = _text(container_qn)
                rows.append(row)
        return rows

    def execute_write(self, query: str, params: PropertyDict | None = None) -> None:
        path = params.get(cs.KEY_PATH) if params else None
        match query:
            case cs.CYPHER_DELETE_MODULE:
                self._delete_module_subtree(path)
            case cs.CYPHER_DELETE_FILE:
                # Mirrors the real query: File/Folder delete keys on the
                # absolute path (issue #897).
                self._detach_delete(
                    self._nodes_at_path(_FILE_LABEL, path, key=cs.KEY_ABSOLUTE_PATH)
                )
            case cs.CYPHER_DELETE_FOLDER:
                self._detach_delete(
                    self._nodes_at_path(_FOLDER_LABEL, path, key=cs.KEY_ABSOLUTE_PATH)
                )
            case cs.CYPHER_DELETE_PACKAGE:
                self._detach_delete(
                    self._nodes_at_path(_PACKAGE_LABEL, path, key=cs.KEY_ABSOLUTE_PATH)
                )
            case cs.CYPHER_DELETE_ORPHAN_EXTERNAL_MODULES:
                self._delete_orphan_external_modules()
            case _:
                return None

    def _edges_into(self, paths: set[str]) -> list[_RelTuple]:
        # Every edge whose TARGET node lives at one of `paths`, through the
        # inbound index: the store answers a path-scoped query with an index
        # lookup, not a scan of every edge.
        found: list[_RelTuple] = []
        for node_id, props in self.nodes.items():
            if props.get(cs.KEY_PATH) in paths:
                found.extend(self._in.get(node_id, ()))
        return found

    def _path_rows(self, label: str) -> list[ResultRow]:
        rows: list[ResultRow] = []
        for (node_label, _uid), props in self.nodes.items():
            if node_label != label:
                continue
            row: ResultRow = {
                cs.KEY_PATH: _text(props.get(cs.KEY_PATH)),
                cs.KEY_ABSOLUTE_PATH: _text(props.get(cs.KEY_ABSOLUTE_PATH)),
                cs.KEY_QUALIFIED_NAME: _text(props.get(cs.KEY_QUALIFIED_NAME)),
            }
            rows.append(row)
        return rows

    def _nodes_at_path(
        self, label: str, path: PropertyValue, key: str = cs.KEY_PATH
    ) -> set[_NodeId]:
        return {
            (node_label, uid)
            for (node_label, uid), props in self.nodes.items()
            if node_label == label and props.get(key) == path
        }

    def _delete_module_subtree(self, path: PropertyValue) -> None:
        doomed: set[_NodeId] = set()
        frontier = list(self._nodes_at_path(_MODULE_LABEL, path))
        while frontier:
            node = frontier.pop()
            if node in doomed:
                continue
            doomed.add(node)
            for _fl, _fv, rel_type, to_label, to_val in self._out.get(node, ()):
                if rel_type in _DEFINES_RELS:
                    child = (to_label, to_val)
                    if child not in doomed:
                        frontier.append(child)
        self._detach_delete(doomed)

    def _delete_orphan_external_modules(self) -> None:
        doomed = {
            (label, uid)
            for (label, uid), props in self.nodes.items()
            if label == _EXTERNAL_MODULE_LABEL and not self._in.get((label, uid))
        }
        self._detach_delete(doomed)

    def _detach_delete(self, doomed: set[_NodeId]) -> None:
        if not doomed:
            return
        for node in doomed:
            self.nodes.pop(node, None)
            touched = self._out.pop(node, set()) | self._in.pop(node, set())
            for edge in touched:
                self.edges.discard(edge)
                self.edge_props.pop(edge, None)
                self._out.get((edge[0], edge[1]), set()).discard(edge)
                self._in.get((edge[3], edge[4]), set()).discard(edge)


def _capture(target: Path, project_name: str) -> _CapturingIngestor:
    parsers, queries = load_parsers()
    ingestor = _CapturingIngestor()
    exclude_paths, unignore_paths = ignore_rules(target)
    GraphUpdater(
        ingestor=ingestor,
        repo_path=target,
        parsers=parsers,
        queries=queries,
        exclude_paths=exclude_paths,
        unignore_paths=unignore_paths,
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
    # Resolve any node (incl. the per-file Module, which has no start_line)
    # to a NodeKey so containment edges can join on it. cgr keys module-level
    # DEFINES parents at the module node; mirror the ast oracle by placing the
    # module at MODULE_START_LINE.
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
        # The per-file module carries no start line (keyed at line 0); an
        # inline module (Rust `mod`) carries its declaration line, keeping it
        # distinct from the file module so nested containment can join.
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
            # Inheritance is graded by the base's SIMPLE NAME (cgr's to-value
            # is the resolved base qn, or the bare name when unresolved).
            source = by_uid.get((from_label, from_val))
            if source is not None:
                # Base simple name: cgr's resolved target may be a dotted qn
                # (`module.Base`) or a Rust path (`std::io::Read`), so split on
                # both `.` and `::`. A same-scope collision registers the base
                # as a DUP_QN_MARKER variant (`ITtl@3`, issue #764); the oracle
                # grades by the written name, so strip the marker.
                flat = str(to_val).replace(cs.SEPARATOR_DOUBLE_COLON, cs.SEPARATOR_DOT)
                target_name = flat.rsplit(cs.SEPARATOR_DOT, 1)[-1].split(
                    cs.DUP_QN_MARKER, 1
                )[0]
                name_edges.add(NameEdge(rel_type, source, target_name))
    return GraphData(nodes=nodes, edges=edges, name_edges=name_edges)


def restrict_to_files(graph: GraphData, files: set[str]) -> GraphData:
    # Scope a graph to a file universe. A compile_commands.json oracle only
    # "sees" files its compiled TUs reach, while cgr indexes the whole tree
    # (bundled test deps, uncompiled sources), so restrict cgr to the files
    # the oracle parsed before scoring. Drops only false positives: no oracle
    # node lives outside its universe, so recall is untouched.
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


def extract_cgr_csharp_nodes(target: Path, project_name: str) -> dict[NodeKey, DefNode]:
    return extract_cgr_lang_nodes(
        target, project_name, ec.CS_SUFFIX, ec.CSHARP_SCORED_NODE_KIND_VALUES
    )


def extract_cgr_csharp_graph(target: Path, project_name: str) -> GraphData:
    return extract_cgr_lang_graph(
        target, project_name, ec.CS_SUFFIX, ec.CSHARP_SCORED_NODE_KIND_VALUES
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
        # Match the oracle: real .ts/.tsx sources, excluding .d.ts type stubs.
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
    # Only real in-repo Python modules count as internal import targets. cgr
    # also emits placeholder MODULE nodes for unresolved imports keyed by the
    # dotted import name (e.g. "thrift.TTornado", "std.set"); requiring a .py
    # path excludes those so IMPORTS is graded against real files only.
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
            # Same DUP_QN_MARKER strip as the multi-language reducer: a base
            # registered as a duplicate variant grades by its written name.
            target = (
                str(to_val)
                .rsplit(cs.SEPARATOR_DOT, 1)[-1]
                .split(cs.DUP_QN_MARKER, 1)[0]
            )
            name_edges.add(NameEdge(rel_type, source, target))
        elif rel_type == cs.RelationshipType.IMPORTS.value:
            target_path = _internal_target_file(str(to_val), internal_modules)
            if target_path is not None:
                name_edges.add(NameEdge(rel_type, source, target_path))

    return GraphData(nodes=nodes, edges=edges, name_edges=name_edges)
