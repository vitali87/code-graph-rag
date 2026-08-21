# Regression tests for issue #1: incremental rebuild used to leave
# stale Function/DEFINES/IMPORTS/CALLS entities when a symbol was renamed
# across files, because the incremental path was additive-only. After the
# fix, an incremental rebuild after a rename must yield exactly the same
# graph as a fresh full rebuild of the renamed tree.
from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.types_defs import PropertyDict, PropertyValue, ResultRow

PROJECT_NAME = "testproj"

NodeId = tuple[str, PropertyValue]
RelTuple = tuple[str, str, PropertyValue, str, str, str, PropertyValue]

_DEFINES_EDGES = (cs.RelationshipType.DEFINES, cs.RelationshipType.DEFINES_METHOD)


class InMemoryGraph:
    """Minimal in-memory ingestor that applies the exact node/relationship
    writes and the DETACH-DELETE queries the updater issues, so final graph
    state can be compared between incremental and full rebuilds."""

    def __init__(self) -> None:
        self.nodes: dict[NodeId, PropertyDict] = {}
        self.rels: set[RelTuple] = set()

    # IngestorProtocol
    def ensure_node_batch(self, label: str, properties: PropertyDict) -> None:
        uid = properties[NODE_UNIQUE_KEYS[label]]
        self.nodes[(str(label), uid)] = dict(properties)

    def ensure_relationship_batch(
        self,
        from_spec: tuple[str, str, PropertyValue],
        rel_type: str,
        to_spec: tuple[str, str, PropertyValue],
        properties: PropertyDict | None = None,
    ) -> None:
        fl, fk, fv = from_spec
        tl, tk, tv = to_spec
        self.rels.add((str(fl), str(fk), fv, str(rel_type), str(tl), str(tk), tv))

    def flush_all(self) -> None:
        return None

    # QueryProtocol
    def fetch_all(
        self, query: str, params: PropertyDict | None = None
    ) -> list[ResultRow]:
        if query == cs.CYPHER_AFFECTED_CALLER_PATHS:
            request = params or {}
            return self._affected_caller_rows(
                request.get("paths") or [], request.get(cs.KEY_PROJECT_PREFIX)
            )
        if query == cs.CYPHER_ALL_MODULE_QNS:
            return self._module_qn_rows((params or {}).get(cs.KEY_PROJECT_PREFIX))
        if query == cs.CYPHER_ALL_DEFINITION_QNS:
            return self._definition_rows((params or {}).get(cs.KEY_PROJECT_PREFIX))
        return []

    def _definition_rows(self, prefix: PropertyValue) -> list[ResultRow]:
        # Mirrors production registry rehydration: definitions in UNCHANGED
        # files (a base class, an overridden method) must be resolvable when
        # a re-parsed subclass or caller references them.
        if not isinstance(prefix, str):
            return []
        definition_labels = {
            "Function",
            "Method",
            "Class",
            "Interface",
            "Enum",
            "Type",
            "Union",
        }
        rows: list[ResultRow] = []
        for (label, _uid), props in self.nodes.items():
            if label not in definition_labels:
                continue
            qn = props.get(cs.KEY_QUALIFIED_NAME)
            if not isinstance(qn, str) or not qn.startswith(prefix):
                continue
            rows.append(
                {
                    cs.KEY_QUALIFIED_NAME: qn,
                    cs.KEY_LABEL: label,
                    cs.KEY_IS_PROPERTY: props.get(cs.KEY_IS_PROPERTY),
                    "is_macro": props.get("is_macro"),
                    cs.KEY_PATH: props.get(cs.KEY_PATH),
                    cs.KEY_START_LINE: props.get(cs.KEY_START_LINE),
                    cs.KEY_END_LINE: props.get(cs.KEY_END_LINE),
                }
            )
        return rows

    def _module_qn_rows(self, prefix: PropertyValue) -> list[ResultRow]:
        # Mirrors production module-qn rehydration: without it, a re-parsed
        # file's import of an UNCHANGED internal module fails deferred
        # verification and grows a phantom ExternalModule node.
        if not isinstance(prefix, str):
            return []
        rows: list[ResultRow] = []
        for (label, _uid), props in self.nodes.items():
            if label not in ("Module", "ModuleInterface"):
                continue
            qn = props.get(cs.KEY_QUALIFIED_NAME)
            if isinstance(qn, str) and qn.startswith(prefix):
                rows.append({cs.KEY_QUALIFIED_NAME: qn, cs.KEY_LABEL: label})
        return rows

    def _affected_caller_rows(
        self, paths: PropertyValue, prefix: PropertyValue
    ) -> list[ResultRow]:
        # Answers the phase-4 dependency query from the stored graph: callers
        # with an edge into any target whose path is being re-indexed, both
        # endpoints scoped to the requesting project.
        if not isinstance(prefix, str):
            return []
        path_set = {str(p) for p in paths} if isinstance(paths, list) else set()
        dependency_rels = {"CALLS", "REFERENCES", "INSTANTIATES", "IMPORTS", "INHERITS"}
        out: set[str] = set()
        for fl, fk, fv, rel, tl, tk, tv in self.rels:
            if rel not in dependency_rels:
                continue
            target = self.nodes.get((tl, tv))
            caller = self.nodes.get((fl, fv))
            if target is None or caller is None:
                continue
            target_path = target.get(cs.KEY_PATH)
            caller_path = caller.get(cs.KEY_PATH)
            target_qn = target.get(cs.KEY_QUALIFIED_NAME)
            caller_qn = caller.get(cs.KEY_QUALIFIED_NAME)
            if (
                isinstance(target_path, str)
                and target_path in path_set
                and isinstance(caller_path, str)
                and caller_path
                and caller_path not in path_set
                and isinstance(target_qn, str)
                and target_qn.startswith(prefix)
                and isinstance(caller_qn, str)
                and caller_qn.startswith(prefix)
            ):
                out.add(caller_path)
        return [{cs.KEY_CALLER_PATH: path} for path in sorted(out)]

    def execute_write(self, query: str, params: PropertyDict | None = None) -> None:
        params = params or {}
        path = params.get(cs.KEY_PATH)
        match query:
            case cs.CYPHER_DELETE_MODULE:
                self._delete_module_subtree(path)
            case cs.CYPHER_DELETE_FILE:
                self._delete_node_by_path(cs.NodeLabel.FILE, path)
            case cs.CYPHER_DELETE_FOLDER:
                self._delete_node_by_path(cs.NodeLabel.FOLDER, path)
            case _:
                return None

    def _find_nodes(self, label: str, key: str, val: PropertyValue) -> list[NodeId]:
        return [
            nid
            for nid, props in self.nodes.items()
            if nid[0] == label and props.get(key) == val
        ]

    def _delete_module_subtree(self, path: PropertyValue) -> None:
        seeds = [
            nid
            for nid, props in self.nodes.items()
            if nid[0] == cs.NodeLabel.MODULE and props.get(cs.KEY_PATH) == path
        ]
        to_delete: set[NodeId] = set()
        stack = list(seeds)
        while stack:
            nid = stack.pop()
            if nid in to_delete:
                continue
            to_delete.add(nid)
            props = self.nodes[nid]
            for fl, fk, fv, rt, tl, tk, tv in self.rels:
                if rt in _DEFINES_EDGES and fl == nid[0] and props.get(fk) == fv:
                    for child in self._find_nodes(tl, tk, tv):
                        if child not in to_delete:
                            stack.append(child)
        self._purge_nodes(to_delete)

    def _delete_node_by_path(self, label: str, path: PropertyValue) -> None:
        # Mirrors the real query: File/Folder deletes key on the absolute
        # path (issue #897).
        self._purge_nodes(set(self._find_nodes(label, cs.KEY_ABSOLUTE_PATH, path)))

    def _purge_nodes(self, to_delete: set[NodeId]) -> None:
        deleted_props = {nid: self.nodes[nid] for nid in to_delete}
        for nid in to_delete:
            self.nodes.pop(nid, None)

        def touches(label: str, key: str, val: PropertyValue) -> bool:
            return any(
                nid[0] == label and props.get(key) == val
                for nid, props in deleted_props.items()
            )

        self.rels = {
            (fl, fk, fv, rt, tl, tk, tv)
            for (fl, fk, fv, rt, tl, tk, tv) in self.rels
            if not touches(fl, fk, fv) and not touches(tl, tk, tv)
        }

    def snapshot(self) -> tuple[frozenset[NodeId], frozenset[RelTuple]]:
        # File/Folder identity is the absolute path, which differs between
        # the golden and incremental tmp roots; normalise those ids back to
        # the relative path so the two graphs stay comparable.
        alias: dict[PropertyValue, PropertyValue] = {
            props[cs.KEY_ABSOLUTE_PATH]: props[cs.KEY_PATH]
            for (label, _uid), props in self.nodes.items()
            if label in (cs.NodeLabel.FILE, cs.NodeLabel.FOLDER)
            and cs.KEY_ABSOLUTE_PATH in props
        }
        nodes = frozenset(
            (label, alias.get(uid, uid)) for (label, uid) in self.nodes.keys()
        )
        rels = frozenset(
            (
                fl,
                cs.KEY_PATH if fk == cs.KEY_ABSOLUTE_PATH else fk,
                alias.get(fv, fv) if fk == cs.KEY_ABSOLUTE_PATH else fv,
                rel,
                tl,
                cs.KEY_PATH if tk == cs.KEY_ABSOLUTE_PATH else tk,
                alias.get(tv, tv) if tk == cs.KEY_ABSOLUTE_PATH else tv,
            )
            for (fl, fk, fv, rel, tl, tk, tv) in self.rels
        )
        return nodes, rels


NODE_UNIQUE_KEYS = cs.NODE_UNIQUE_CONSTRAINTS


def _write_tree(root: Path, new_name: str) -> None:
    (root / "__init__.py").touch()
    (root / "a.py").write_text(f"def {new_name}():\n    return 1\n")
    (root / "b.py").write_text(
        f"from .a import {new_name}\n\n\ndef caller():\n    return {new_name}()\n"
    )


def _make_updater(root: Path, ingestor: InMemoryGraph) -> GraphUpdater:
    parsers, queries = load_parsers()
    return GraphUpdater(
        ingestor=ingestor,
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name=PROJECT_NAME,
    )


class TestIncrementalRenameStaleEntities:
    def test_incremental_rename_matches_full_rebuild(self, tmp_path: Path) -> None:
        # Golden: a fresh full rebuild of the already-renamed tree.
        golden_root = tmp_path / "golden"
        golden_root.mkdir()
        _write_tree(golden_root, "new_name")
        golden_graph = InMemoryGraph()
        _make_updater(golden_root, golden_graph).run(force=True)

        # Sanity: golden truly contains the renamed symbol and not the old one.
        golden_funcs = {
            uid for (label, uid) in golden_graph.nodes if label == cs.NodeLabel.FUNCTION
        }
        assert any(str(qn).endswith(".new_name") for qn in golden_funcs)
        assert not any(str(qn).endswith(".old_name") for qn in golden_funcs)

        # Incremental: build original tree, then rename across both files
        # and rebuild incrementally (force=False).
        incr_root = tmp_path / "incr"
        incr_root.mkdir()
        _write_tree(incr_root, "old_name")
        incr_graph = InMemoryGraph()
        _make_updater(incr_root, incr_graph).run(force=True)

        _write_tree(incr_root, "new_name")
        _make_updater(incr_root, incr_graph).run(force=False)

        # The stale old_name Function and its edges must be gone.
        incr_nodes, incr_rels = incr_graph.snapshot()
        golden_nodes, golden_rels = golden_graph.snapshot()

        assert incr_nodes == golden_nodes, {
            "stale_extra_nodes": sorted(map(str, incr_nodes - golden_nodes)),
            "missing_nodes": sorted(map(str, golden_nodes - incr_nodes)),
        }
        assert incr_rels == golden_rels, {
            "stale_extra_rels": sorted(map(str, incr_rels - golden_rels)),
            "missing_rels": sorted(map(str, golden_rels - incr_rels)),
        }


if __name__ == "__main__":
    pytest.main([__file__, "-v"])


def _write_override_tree(root: Path, with_override: bool) -> None:
    (root / "base.py").write_text(
        "class A:\n    def m(self):\n        return 1\n", encoding="utf-8"
    )
    override = "    def m(self):\n        return 2\n" if with_override else "    pass\n"
    (root / "derived.py").write_text(
        f"from base import A\n\nclass B(A):\n{override}", encoding="utf-8"
    )
    (root / "caller.py").write_text(
        "from derived import B\n\n\ndef use():\n    b = B()\n    b.m()\n",
        encoding="utf-8",
    )


class TestAffectedCallerReprocessing:
    def test_new_override_rebinds_the_unchanged_callers_call(
        self, tmp_path: Path
    ) -> None:
        # Issue #1229 phase 4: only derived.py changes (B gains an override of
        # m), so caller.py is not re-parsed by the hash diff; its old edge to
        # A.m would be restored verbatim while the correct binding is now B.m.
        # Dependency expansion re-parses caller.py and the graphs converge.
        golden_root = tmp_path / "golden"
        golden_root.mkdir()
        _write_override_tree(golden_root, with_override=True)
        golden_graph = InMemoryGraph()
        _make_updater(golden_root, golden_graph).run(force=True)
        golden_nodes, golden_rels = golden_graph.snapshot()

        incr_root = tmp_path / "incr"
        incr_root.mkdir()
        _write_override_tree(incr_root, with_override=False)
        incr_graph = InMemoryGraph()
        _make_updater(incr_root, incr_graph).run(force=True)

        _write_override_tree(incr_root, with_override=True)
        _make_updater(incr_root, incr_graph).run(force=False)
        incr_nodes, incr_rels = incr_graph.snapshot()

        assert incr_nodes == golden_nodes, {
            "stale_extra_nodes": sorted(map(str, incr_nodes - golden_nodes)),
            "missing_nodes": sorted(map(str, golden_nodes - incr_nodes)),
        }
        assert incr_rels == golden_rels, {
            "stale_extra_rels": sorted(map(str, incr_rels - golden_rels)),
            "missing_rels": sorted(map(str, golden_rels - incr_rels)),
        }
