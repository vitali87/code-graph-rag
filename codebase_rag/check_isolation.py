"""Capture and restore of the subgraph a scoped re-ingest replaces (issue #1718).

`cgr check` measures the working tree by re-ingesting the files that differ
from the base, and that re-ingest is also what brings the shared graph up
to the tree: a second run of the same check reports nothing. `--isolated`
keeps the measurement and drops the write. The guard reads, in full,
exactly the set the re-ingest is about to delete and re-parse -- the module
subtrees the delete query walks, the File nodes at those paths, the
containers above them and every relationship touching any of it -- and
puts that back once the delta has been computed.

The capture runs inside the re-ingest's own prologue (`before_write`), so
its scope is the updater's, dependents and same-stem survivors included,
rather than a guess made from the diff. The restore is the inverse of the
writes the re-ingest issues: the subtrees are deleted again, the nodes the
check created beside them (a new file's File node, a flipped container, a
new finding, an ExternalModule for a new import) are removed, and the
captured nodes and edges are re-emitted through the batch API the parsers
write with. Nodes outside the scope that the check can prune or re-grade
(ExternalModule, Resource, Gloss) come back with their captured properties;
a property the check ADDED to such a node is not removed, since the batch
write merges properties rather than replacing them.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

from . import constants as cs
from . import cypher_queries as cq
from .services.resource_cleanup import prune_unanchored_resources
from .types_defs import PropertyDict, PropertyValue, ResultRow
from .utils.path_utils import cached_file_identity_posix, cached_resolve_posix


class GraphStore(Protocol):
    """Both halves of an ingestor: the batch writes and the query surface."""

    def fetch_all(
        self, query: str, params: PropertyDict | None = None
    ) -> list[ResultRow]: ...

    def execute_write(self, query: str, params: PropertyDict | None = None) -> None: ...

    def ensure_node_batch(self, label: str, properties: PropertyDict) -> None: ...

    def ensure_relationship_batch(
        self,
        from_spec: tuple[str, str, PropertyValue],
        rel_type: str,
        to_spec: tuple[str, str, PropertyValue],
        properties: PropertyDict | None = None,
    ) -> None: ...

    def flush_all(self) -> None: ...


NodeSpec = tuple[str, str, PropertyValue]
_Edge = tuple[NodeSpec, str, NodeSpec, PropertyDict]
_EdgeKey = tuple[NodeSpec, str, NodeSpec, str]

# The nodes the scope reaches by path rather than by the subtree walk, with
# the delete each one answers to (all keyed on the absolute path).
_PATH_NODE_DELETES: dict[str, str] = {
    cs.NodeLabel.FILE.value: cs.CYPHER_DELETE_FILE,
    cs.NodeLabel.FOLDER.value: cs.CYPHER_DELETE_FOLDER,
    cs.NodeLabel.PACKAGE.value: cs.CYPHER_DELETE_PACKAGE,
}
_FINDING_LABELS = frozenset(
    {
        cs.NodeLabel.CODE_SMELL.value,
        cs.NodeLabel.SECURITY_ISSUE.value,
        cs.NodeLabel.PATTERN.value,
    }
)


def _node_spec(label: object, row: ResultRow, prefix: str = "") -> NodeSpec | None:
    """The (label, key, value) the batch API addresses a row's node by.

    None for a label without a unique key or a row missing its value: such
    an edge cannot be re-emitted and is left out, the way the inbound-edge
    restore leaves out what it cannot address.
    """
    if not isinstance(label, str):
        return None
    key = cs.NODE_UNIQUE_CONSTRAINTS.get(label)
    if key is None:
        return None
    value = row.get(prefix + key)
    if not isinstance(value, str | int):
        return None
    return (label, key, value)


def _edge_key(
    source: NodeSpec, rel: str, target: NodeSpec, props: PropertyDict
) -> _EdgeKey:
    # An edge between two scoped nodes is reported from both ends; the
    # properties join the key because per-site edges (issue #1522) share
    # their endpoints and differ only there.
    return (source, rel, target, repr(sorted(props.items(), key=lambda kv: kv[0])))


class IsolationGuard:
    """One capture, one restore, for the scope one re-ingest call names."""

    def __init__(self, store: GraphStore, project_name: str, repo_root: Path) -> None:
        self._store = store
        self._project_name = project_name
        self._repo_root = repo_root
        self._keys: tuple[str, ...] = ()
        self._nodes: list[tuple[str, PropertyDict]] = []
        self._far_nodes: dict[NodeSpec, PropertyDict] = {}
        self._edges: dict[_EdgeKey, _Edge] = {}
        self._path_nodes: set[tuple[str, str]] = set()
        self._finding_qns: set[str] = set()
        self._captured = False

    @property
    def captured(self) -> bool:
        """Whether the re-ingest reached its write point and was captured."""
        return self._captured

    # --- capture ------------------------------------------------------------

    def capture(self, keys: Sequence[str]) -> None:
        """Read the scope's nodes and edges; `keys` are repo-relative paths."""
        self._keys = tuple(sorted(set(keys)))
        params = self._params()
        for row in self._store.fetch_all(cq.CYPHER_CHECK_SCOPE_NODES, params):
            label = row.get(cs.KEY_LABEL)
            props = row.get(cs.KEY_PROPS)
            if not isinstance(label, str) or not isinstance(props, dict):
                continue
            self._nodes.append((label, dict(props)))
            if label in _PATH_NODE_DELETES:
                self._path_nodes.add((label, str(props.get(cs.KEY_ABSOLUTE_PATH))))
        for row in self._store.fetch_all(cq.CYPHER_CHECK_SCOPE_EDGES, params):
            self._capture_edge(row)
        self._captured = True

    def _capture_edge(self, row: ResultRow) -> None:
        rel = row.get(cs.KEY_REL)
        near = _node_spec(row.get(cs.KEY_LABEL), row)
        far_label = row.get(cs.KEY_FAR_LABEL)
        far = _node_spec(far_label, row, prefix=cs.FAR_END_PREFIX)
        if not isinstance(rel, str) or near is None or far is None:
            return
        raw_props = row.get(cs.KEY_PROPS)
        props: PropertyDict = dict(raw_props) if isinstance(raw_props, dict) else {}
        source, target = (near, far) if row.get(cs.KEY_OUTGOING) else (far, near)
        self._edges.setdefault(
            _edge_key(source, rel, target, props), (source, rel, target, props)
        )
        far_props = row.get(cs.KEY_FAR_PROPS)
        if isinstance(far_props, dict):
            self._far_nodes.setdefault(far, dict(far_props))
        if far_label in _FINDING_LABELS:
            self._finding_qns.add(str(far[2]))

    def _params(self) -> PropertyDict:
        return {
            cs.CYPHER_PARAM_PATHS: list(self._keys),
            cs.KEY_PROJECT_NAME: self._project_name,
            cs.KEY_PROJECT_PREFIX: self._project_name + cs.SEPARATOR_DOT,
            cs.CYPHER_PARAM_ABSOLUTE_PATHS: self._absolute_paths(),
        }

    def _absolute_paths(self) -> list[str]:
        # File identity as the prune reads it, container identity as the
        # flip prune reads it; every ancestor directory of a key is in, the
        # root included, because a package indicator appearing or vanishing
        # flips the container of the directory above the file.
        files = {
            cached_file_identity_posix(self._repo_root / key) for key in self._keys
        }
        directories = {
            cached_resolve_posix(self._repo_root / parent)
            for key in self._keys
            for parent in Path(key).parents
        }
        return sorted(files | directories)

    # --- restore ------------------------------------------------------------

    def restore(self) -> None:
        """Put the capture back; a no-op when the re-ingest never reached its write."""
        if not self._captured:
            return
        store = self._store
        params = self._params()
        store.execute_write(
            cq.CYPHER_CHECK_DELETE_FINDINGS,
            {
                cs.CYPHER_PARAM_PATHS: list(self._keys),
                cs.CYPHER_PARAM_KEEP: sorted(self._finding_qns),
            },
        )
        for key in self._keys:
            store.execute_write(
                cs.CYPHER_DELETE_MODULE,
                {
                    cs.KEY_PATH: key,
                    cs.KEY_PROJECT_NAME: self._project_name,
                    cs.KEY_PROJECT_PREFIX: self._project_name + cs.SEPARATOR_DOT,
                },
            )
        self._delete_new_path_nodes(params)
        # What the check created beside the subtrees and the deletes above
        # just orphaned; the captured ones are re-emitted below.
        store.execute_write(cs.CYPHER_DELETE_ORPHAN_EXTERNAL_MODULES)
        prune_unanchored_resources(store)
        for label, props in self._nodes:
            store.ensure_node_batch(label, props)
        for (label, _key, _value), props in self._far_nodes.items():
            store.ensure_node_batch(label, props)
        for source, rel, target, props in self._edges.values():
            store.ensure_relationship_batch(
                source, rel, target, properties=props or None
            )
        store.flush_all()

    def _delete_new_path_nodes(self, params: PropertyDict) -> None:
        # File and container nodes now at the scope's paths that were not
        # there before: a new file's File node, a new directory's Folder,
        # the other kind of a flipped container.
        for row in self._store.fetch_all(cq.CYPHER_CHECK_SCOPE_NODES, params):
            label = row.get(cs.KEY_LABEL)
            props = row.get(cs.KEY_PROPS)
            delete = _PATH_NODE_DELETES.get(str(label))
            if delete is None or not isinstance(props, dict):
                continue
            absolute = props.get(cs.KEY_ABSOLUTE_PATH)
            if (str(label), str(absolute)) in self._path_nodes:
                continue
            self._store.execute_write(delete, {cs.KEY_PATH: absolute})
