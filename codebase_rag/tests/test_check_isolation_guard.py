"""The isolated check's capture and restore against a scripted store (#1718).

The end-to-end cases replay a real re-ingest on the eval emulator; these pin
the guard's own contract with nothing else in the loop: which rows the
capture keeps, and the exact writes the restore issues, in order, for a
scope whose re-ingest added a node, a finding, a shared node and a key.
"""

from __future__ import annotations

from pathlib import Path

from codebase_rag import constants as cs
from codebase_rag import cypher_queries as cq
from codebase_rag.check_isolation import IsolationGuard
from codebase_rag.types_defs import PropertyDict, PropertyValue, ResultRow
from codebase_rag.utils.path_utils import (
    cached_file_identity_posix,
    cached_resolve_posix,
)

PROJECT = "proj"
MODULE_QN = "proj.pkg.mod"
FUNC_QN = "proj.pkg.mod.run"
SMELL_QN = "proj.pkg.mod.smell"
KEPT_EXT = "requests"
NEW_EXT = "httpx"
ORPHAN_EXT = "unused"

_MODULE = cs.NodeLabel.MODULE.value
_FUNCTION = cs.NodeLabel.FUNCTION.value
_FILE = cs.NodeLabel.FILE.value
_FOLDER = cs.NodeLabel.FOLDER.value
_EXT = cs.NodeLabel.EXTERNAL_MODULE.value
_SMELL = cs.NodeLabel.CODE_SMELL.value


class _ScriptedStore:
    """Answers each read from a per-query script and records every write.

    A query answers with its scripted rows in call order (the last answer
    repeats), and anything unscripted answers with no rows.
    """

    def __init__(self) -> None:
        self.answers: dict[str, list[list[ResultRow]]] = {}
        self.writes: list[tuple[str, PropertyDict | None]] = []
        self.nodes: list[tuple[str, PropertyDict]] = []
        self.rels: list[
            tuple[
                tuple[str, str, PropertyValue],
                str,
                tuple[str, str, PropertyValue],
                PropertyDict | None,
            ]
        ] = []
        self.flushes = 0

    def script(self, query: str, *answers: list[ResultRow]) -> None:
        self.answers[query] = list(answers)

    def fetch_all(
        self, query: str, params: PropertyDict | None = None
    ) -> list[ResultRow]:
        answers = self.answers.get(query)
        if not answers:
            return []
        return answers.pop(0) if len(answers) > 1 else answers[0]

    def execute_write(self, query: str, params: PropertyDict | None = None) -> None:
        self.writes.append((query, params))

    def ensure_node_batch(self, label: str, properties: PropertyDict) -> None:
        self.nodes.append((label, dict(properties)))

    def ensure_relationship_batch(
        self,
        from_spec: tuple[str, str, PropertyValue],
        rel_type: str,
        to_spec: tuple[str, str, PropertyValue],
        properties: PropertyDict | None = None,
    ) -> None:
        self.rels.append((from_spec, rel_type, to_spec, properties))

    def flush_all(self) -> None:
        self.flushes += 1


def _edge_row(
    near_label: str,
    near_qn: str,
    rel: str,
    far_label: object,
    far_qn: object,
    *,
    outgoing: bool = True,
    props: object = None,
    far_props: object = None,
) -> ResultRow:
    return {
        cs.KEY_LABEL: near_label,
        cs.KEY_QUALIFIED_NAME: near_qn,
        cs.KEY_REL: rel,
        cs.KEY_OUTGOING: outgoing,
        cs.KEY_PROPS: props,
        cs.KEY_FAR_LABEL: far_label,
        cs.FAR_END_PREFIX + cs.KEY_QUALIFIED_NAME: far_qn,
        cs.KEY_FAR_PROPS: far_props,
    }


def _file_props(root: Path) -> PropertyDict:
    return {
        cs.KEY_ABSOLUTE_PATH: cached_file_identity_posix(root / "pkg" / "mod.py"),
        cs.KEY_PATH: "pkg/mod.py",
    }


def _captured(root: Path) -> tuple[IsolationGuard, _ScriptedStore]:
    """A guard that captured one module, its function, its File and edges."""
    store = _ScriptedStore()
    module = {cs.KEY_QUALIFIED_NAME: MODULE_QN, cs.KEY_PATH: "pkg/mod.py"}
    function = {cs.KEY_QUALIFIED_NAME: FUNC_QN, cs.KEY_NAME: "run"}
    smell = {cs.KEY_QUALIFIED_NAME: SMELL_QN, cs.KEY_PATH: "pkg/mod.py"}
    kept = {cs.KEY_QUALIFIED_NAME: KEPT_EXT}
    store.script(
        cq.CYPHER_CHECK_SCOPE_NODES,
        [
            {cs.KEY_LABEL: _MODULE, cs.KEY_PROPS: module},
            {cs.KEY_LABEL: _FUNCTION, cs.KEY_PROPS: function},
            {cs.KEY_LABEL: _FILE, cs.KEY_PROPS: _file_props(root)},
            # Rows the capture cannot use are skipped, not raised on.
            {cs.KEY_LABEL: None, cs.KEY_PROPS: module},
            {cs.KEY_LABEL: _MODULE, cs.KEY_PROPS: "not a map"},
        ],
    )
    defines = cs.RelationshipType.DEFINES.value
    imports = cs.RelationshipType.IMPORTS.value
    store.script(
        cq.CYPHER_CHECK_SCOPE_EDGES,
        [
            _edge_row(_MODULE, MODULE_QN, defines, _FUNCTION, FUNC_QN),
            # The same edge seen from its other end is kept once.
            _edge_row(_FUNCTION, FUNC_QN, defines, _MODULE, MODULE_QN, outgoing=False),
            _edge_row(
                _MODULE,
                MODULE_QN,
                imports,
                _EXT,
                KEPT_EXT,
                props={cs.KEY_LINE: 1},
                far_props=kept,
            ),
            _edge_row(_SMELL, SMELL_QN, "HAS_SMELL", _MODULE, MODULE_QN),
            _edge_row(
                _MODULE,
                MODULE_QN,
                "HAS_SMELL",
                _SMELL,
                SMELL_QN,
                far_props=smell,
            ),
            # Unaddressable ends: no label, a label without a unique key, a
            # key value that is not a scalar, a relationship with no type.
            _edge_row(_MODULE, MODULE_QN, imports, None, "x"),
            _edge_row(_MODULE, MODULE_QN, imports, "NoSuchLabel", "x"),
            _edge_row(_MODULE, MODULE_QN, imports, _EXT, None),
            {
                **_edge_row(_MODULE, MODULE_QN, imports, _EXT, KEPT_EXT),
                cs.KEY_REL: None,
            },
        ],
    )
    store.script(
        cq.CYPHER_CHECK_SHARED_NODES,
        [
            {cs.KEY_LABEL: _EXT, cs.KEY_PROPS: kept, cs.KEY_INBOUND: 1},
            {
                cs.KEY_LABEL: _EXT,
                cs.KEY_PROPS: {cs.KEY_QUALIFIED_NAME: ORPHAN_EXT},
                cs.KEY_INBOUND: 0,
            },
            {cs.KEY_LABEL: _EXT, cs.KEY_PROPS: None, cs.KEY_INBOUND: 0},
        ],
    )
    guard = IsolationGuard(store, PROJECT, root)
    guard.capture(["pkg/mod.py", "pkg/mod.py"])
    return guard, store


def test_a_restore_without_a_capture_writes_nothing(tmp_path: Path) -> None:
    store = _ScriptedStore()
    guard = IsolationGuard(store, PROJECT, tmp_path)

    guard.restore()

    assert not guard.captured
    assert (store.writes, store.nodes, store.rels, store.flushes) == ([], [], [], 0)


def test_the_capture_reads_the_scope_at_every_ancestor_directory(
    tmp_path: Path,
) -> None:
    guard, _store = _captured(tmp_path)

    params = guard._params()

    assert guard.captured
    assert params[cs.CYPHER_PARAM_PATHS] == ["pkg/mod.py"]
    assert params[cs.KEY_PROJECT_NAME] == PROJECT
    assert params[cs.KEY_PROJECT_PREFIX] == PROJECT + cs.SEPARATOR_DOT
    assert params[cs.CYPHER_PARAM_ABSOLUTE_PATHS] == sorted(
        {
            cached_file_identity_posix(tmp_path / "pkg" / "mod.py"),
            cached_resolve_posix(tmp_path / "pkg"),
            cached_resolve_posix(tmp_path),
        }
    )


def test_the_restore_undoes_each_kind_of_write_the_reingest_made(
    tmp_path: Path,
) -> None:
    """A new File, a new finding, a new ExternalModule and an added key.

    After the capture the store answers as the graph reads once the
    re-ingest ran: the scope now reaches a new ExternalModule beside the
    captured one, a new file sits beside the captured File, and the
    captured File carries a key it did not have.
    """
    guard, store = _captured(tmp_path)
    imports = cs.RelationshipType.IMPORTS.value
    new_file = cached_file_identity_posix(tmp_path / "pkg" / "new.py")
    store.script(
        cq.CYPHER_CHECK_SCOPE_EDGES,
        [
            _edge_row(_MODULE, MODULE_QN, imports, _EXT, KEPT_EXT),
            _edge_row(_MODULE, MODULE_QN, imports, _EXT, NEW_EXT),
            _edge_row(_MODULE, MODULE_QN, imports, _EXT, NEW_EXT),
            _edge_row(_MODULE, MODULE_QN, imports, _EXT, ORPHAN_EXT),
            _edge_row(_MODULE, MODULE_QN, imports, _EXT, None),
            _edge_row(
                _MODULE,
                MODULE_QN,
                cs.RelationshipType.DEFINES.value,
                _FUNCTION,
                FUNC_QN,
            ),
        ],
    )
    store.script(
        cq.CYPHER_CHECK_SCOPE_NODES,
        [
            {cs.KEY_LABEL: _FILE, cs.KEY_PROPS: _file_props(tmp_path)},
            {cs.KEY_LABEL: _FILE, cs.KEY_PROPS: {cs.KEY_ABSOLUTE_PATH: new_file}},
            {cs.KEY_LABEL: _MODULE, cs.KEY_PROPS: {cs.KEY_QUALIFIED_NAME: MODULE_QN}},
            {cs.KEY_LABEL: _FOLDER, cs.KEY_PROPS: None},
        ],
    )
    file_key = cs.NODE_UNIQUE_CONSTRAINTS[_FILE]
    file_id = _file_props(tmp_path)[file_key]
    store.script(
        cq.build_node_props_query(_FILE, file_key),
        [{cs.KEY_PROPS: {**_file_props(tmp_path), "added": 1}}],
    )

    guard.restore()

    project = {
        cs.KEY_PROJECT_NAME: PROJECT,
        cs.KEY_PROJECT_PREFIX: PROJECT + cs.SEPARATOR_DOT,
    }
    assert store.writes == [
        (
            cq.CYPHER_CHECK_DELETE_FINDINGS,
            {
                cs.CYPHER_PARAM_PATHS: ["pkg/mod.py"],
                cs.CYPHER_PARAM_KEEP: [SMELL_QN],
                **project,
            },
        ),
        (
            cs.CYPHER_DELETE_MODULE,
            {cs.KEY_PATH: "pkg/mod.py", **project, cs.KEY_NESTED_PROJECTS: []},
        ),
        (cs.CYPHER_DELETE_FILE, {cs.KEY_PATH: new_file}),
        (
            cq.CYPHER_CHECK_DELETE_SHARED_NODES,
            {
                cs.CYPHER_PARAM_LABELS: [_EXT],
                cs.CYPHER_PARAM_QUALIFIED_NAMES: [NEW_EXT],
            },
        ),
        (
            cq.build_remove_node_keys_query(_FILE, file_key, {"added"}),
            {cs.KEY_ID: file_id},
        ),
    ]
    restored = {
        (label, props.get(cs.KEY_QUALIFIED_NAME)) for label, props in store.nodes
    }
    assert {
        (_MODULE, MODULE_QN),
        (_FUNCTION, FUNC_QN),
        (_EXT, KEPT_EXT),
        (_EXT, ORPHAN_EXT),
        (_SMELL, SMELL_QN),
    } <= restored
    assert (_EXT, NEW_EXT) not in restored
    module = (_MODULE, cs.KEY_QUALIFIED_NAME, MODULE_QN)
    assert sorted(store.rels, key=repr) == sorted(
        [
            (
                module,
                cs.RelationshipType.DEFINES.value,
                (_FUNCTION, cs.KEY_QUALIFIED_NAME, FUNC_QN),
                None,
            ),
            (
                module,
                imports,
                (_EXT, cs.KEY_QUALIFIED_NAME, KEPT_EXT),
                {cs.KEY_LINE: 1},
            ),
            ((_SMELL, cs.KEY_QUALIFIED_NAME, SMELL_QN), "HAS_SMELL", module, None),
            (module, "HAS_SMELL", (_SMELL, cs.KEY_QUALIFIED_NAME, SMELL_QN), None),
        ],
        key=repr,
    )
    assert store.flushes == 1


def test_a_survivor_without_added_keys_or_no_longer_present_is_left_alone(
    tmp_path: Path,
) -> None:
    guard, store = _captured(tmp_path)
    file_key = cs.NODE_UNIQUE_CONSTRAINTS[_FILE]
    store.script(
        cq.build_node_props_query(_FILE, file_key),
        [{cs.KEY_PROPS: _file_props(tmp_path)}],
    )
    store.script(cq.build_node_props_query(_EXT, cs.NODE_UNIQUE_CONSTRAINTS[_EXT]), [])

    guard.restore()

    assert not [q for q, _p in store.writes if " REMOVE " in q]
    assert not [q for q, _p in store.writes if q == cq.CYPHER_CHECK_DELETE_SHARED_NODES]


def test_a_survivor_without_its_key_is_not_read_back(tmp_path: Path) -> None:
    store = _ScriptedStore()
    store.script(
        cq.CYPHER_CHECK_SCOPE_NODES,
        [{cs.KEY_LABEL: _FILE, cs.KEY_PROPS: {cs.KEY_PATH: "pkg/mod.py"}}],
    )
    guard = IsolationGuard(store, PROJECT, tmp_path)
    guard.capture(["pkg/mod.py"])
    store.script(
        cq.build_node_props_query(_FILE, cs.NODE_UNIQUE_CONSTRAINTS[_FILE]),
        [{cs.KEY_PROPS: {"added": 1}}],
    )

    guard.restore()

    assert not [q for q, _p in store.writes if " REMOVE " in q]


class _BufferedStore(_ScriptedStore):
    def __init__(self) -> None:
        super().__init__()
        self.node_buffer: list[tuple[str, PropertyDict]] = [("Function", {})]
        self._rel_groups: dict[str, list[object]] = {"p": [object()]}
        self._rel_count = 3


def test_the_restore_drops_what_a_failed_reingest_left_queued(tmp_path: Path) -> None:
    store = _BufferedStore()
    guard = IsolationGuard(store, PROJECT, tmp_path)
    guard.capture(["pkg/mod.py"])

    guard.restore()

    assert (store.node_buffer, store._rel_groups, store._rel_count) == ([], {}, 0)
