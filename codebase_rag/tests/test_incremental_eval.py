import json
import os
import shutil
from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.parser_loader import load_parsers
from evals.cgr_graph import _StatefulIngestor
from evals.incremental import (
    compare_states,
    run_neutral_edit_scenario,
    snapshot,
)

_MODULE = cs.NodeLabel.MODULE.value
_FILE = cs.NodeLabel.FILE.value
_FUNCTION = cs.NodeLabel.FUNCTION.value
_QN = cs.KEY_QUALIFIED_NAME
_DEFINES = cs.RelationshipType.DEFINES.value
_CALLS = cs.RelationshipType.CALLS.value
_IMPORTS = cs.RelationshipType.IMPORTS.value
_CONTAINS_FILE = cs.RelationshipType.CONTAINS_FILE.value

# (H) The inbound call edge issue #532 drops: caller.use() calls callee.target().
_INBOUND_CALL = (_FUNCTION, "proj.caller.use", _CALLS, _FUNCTION, "proj.callee.target")


def _node(store: _StatefulIngestor, label: str, **props: object) -> None:
    store.ensure_node_batch(label, props)


def _module_subtree() -> _StatefulIngestor:
    # (H) Two modules: callee.py defines target(); caller.py defines use() which
    # (H) CALLS target(). Mirrors the real graph shape captured from cgr.
    s = _StatefulIngestor()
    _node(s, _MODULE, qualified_name="proj.callee", path="callee.py")
    _node(s, _FUNCTION, qualified_name="proj.callee.target", path="callee.py")
    _node(s, _MODULE, qualified_name="proj.caller", path="caller.py")
    _node(s, _FUNCTION, qualified_name="proj.caller.use", path="caller.py")
    s.ensure_relationship_batch(
        (_MODULE, _QN, "proj.callee"), _DEFINES, (_FUNCTION, _QN, "proj.callee.target")
    )
    s.ensure_relationship_batch(
        (_MODULE, _QN, "proj.caller"), _DEFINES, (_FUNCTION, _QN, "proj.caller.use")
    )
    s.ensure_relationship_batch(
        (_FUNCTION, _QN, "proj.caller.use"),
        _CALLS,
        (_FUNCTION, _QN, "proj.callee.target"),
    )
    return s


class TestStatefulStore:
    def test_detach_delete_module_removes_subtree_and_incident_edges(self) -> None:
        s = _module_subtree()
        s.execute_write(cs.CYPHER_DELETE_MODULE, {cs.KEY_PATH: "callee.py"})

        assert (_MODULE, "proj.callee") not in s.nodes
        assert (_FUNCTION, "proj.callee.target") not in s.nodes
        # (H) The caller subtree is untouched.
        assert (_FUNCTION, "proj.caller.use") in s.nodes
        # (H) DETACH removes the inbound CALLS edge incident on the deleted target.
        assert not any(e[2] == _CALLS for e in s.edges)
        # (H) The caller's own DEFINES edge survives.
        assert any(e[2] == _DEFINES and e[1] == "proj.caller" for e in s.edges)

    def test_delete_file_detaches(self) -> None:
        s = _StatefulIngestor()
        _node(s, _FILE, path="callee.py")
        _node(s, _MODULE, qualified_name="proj", path="x")
        s.ensure_relationship_batch(
            (_MODULE, _QN, "proj"), _CONTAINS_FILE, (_FILE, cs.KEY_PATH, "callee.py")
        )
        s.execute_write(cs.CYPHER_DELETE_FILE, {cs.KEY_PATH: "callee.py"})

        assert (_FILE, "callee.py") not in s.nodes
        assert all(e[4] != "callee.py" for e in s.edges)

    def test_fetch_all_excludes_external_modules(self) -> None:
        s = _StatefulIngestor()
        _node(s, _FILE, path="a.py", absolute_path="/x/a.py")
        _node(s, _MODULE, qualified_name="proj.a", path="a.py")
        _node(s, _MODULE, qualified_name="ext", path="ext", is_external=True)

        files = s.fetch_all(cs.CYPHER_ALL_FILE_PATHS)
        assert {r[cs.KEY_PATH] for r in files} == {"a.py"}
        mods = s.fetch_all(cs.CYPHER_ALL_MODULE_PATHS_INTERNAL)
        assert {r[cs.KEY_PATH] for r in mods} == {"a.py"}

    def test_delete_orphan_external_modules(self) -> None:
        s = _StatefulIngestor()
        _node(s, _MODULE, qualified_name="ext.orphan", path="ext", is_external=True)
        _node(s, _MODULE, qualified_name="ext.used", path="ext2", is_external=True)
        _node(s, _MODULE, qualified_name="proj.m", path="m.py")
        s.ensure_relationship_batch(
            (_MODULE, _QN, "proj.m"), _IMPORTS, (_MODULE, _QN, "ext.used")
        )
        s.execute_write(cs.CYPHER_DELETE_ORPHAN_EXTERNAL_MODULES)

        assert (_MODULE, "ext.orphan") not in s.nodes
        assert (_MODULE, "ext.used") in s.nodes

    def test_edges_are_deduped(self) -> None:
        s = _StatefulIngestor()
        spec = (_MODULE, _QN, "proj")
        s.ensure_relationship_batch(spec, _DEFINES, spec)
        s.ensure_relationship_batch(spec, _DEFINES, spec)
        assert len(s.edges) == 1


@pytest.fixture(scope="module")
def parsers_queries() -> tuple[object, object]:
    return load_parsers()


def _make_repo(root: Path) -> None:
    root.mkdir(parents=True)
    (root / "__init__.py").write_text("", encoding="utf-8")
    (root / "callee.py").write_text("def target():\n    return 1\n", encoding="utf-8")
    (root / "caller.py").write_text(
        "from proj.callee import target\n\n\ndef use():\n    return target()\n",
        encoding="utf-8",
    )


class TestIncrementalScenario:
    def test_clean_reindex_sees_inbound_call(
        self, tmp_path: Path, parsers_queries: tuple[object, object]
    ) -> None:
        # (H) Baseline: a clean forced index resolves caller.use -> callee.target.
        src = tmp_path / "proj"
        _make_repo(src)
        parsers, queries = parsers_queries
        work = tmp_path / "work"
        shutil.copytree(src, work)
        store = _StatefulIngestor()
        from codebase_rag.graph_updater import GraphUpdater

        GraphUpdater(
            ingestor=store,
            repo_path=work,
            parsers=parsers,
            queries=queries,
            project_name="proj",
        ).run(force=True)
        assert _INBOUND_CALL in snapshot(store).edges

    def test_incremental_preserves_inbound_call_editing_callee(
        self, tmp_path: Path, parsers_queries: tuple[object, object]
    ) -> None:
        # (H) Issue #532: editing the callee deletes its module subtree (and the
        # (H) inbound CALLS incident on it). The fix must rebuild that inbound edge
        # (H) from the unchanged caller, so the incremental graph equals a clean
        # (H) re-index.
        src = tmp_path / "proj"
        _make_repo(src)
        parsers, queries = parsers_queries
        incr, clean = run_neutral_edit_scenario(
            src, "proj", "callee.py", parsers, queries, tmp_path / "scn"
        )
        assert _INBOUND_CALL in clean.edges
        assert _INBOUND_CALL in incr.edges
        assert incr == clean

    def test_incremental_preserves_cross_file_call_editing_caller(
        self, tmp_path: Path, parsers_queries: tuple[object, object]
    ) -> None:
        # (H) Editing the caller must rebuild its outbound call to the unchanged
        # (H) callee. This requires the function registry to know definitions in
        # (H) unchanged files (rehydrated from the persisted graph), not just the
        # (H) changed file.
        src = tmp_path / "proj"
        _make_repo(src)
        parsers, queries = parsers_queries
        incr, clean = run_neutral_edit_scenario(
            src, "proj", "caller.py", parsers, queries, tmp_path / "scn"
        )
        assert _INBOUND_CALL in clean.edges
        assert _INBOUND_CALL in incr.edges
        assert incr == clean

    def test_baseline_index_ignores_preexisting_cache(
        self, tmp_path: Path, parsers_queries: tuple[object, object]
    ) -> None:
        # (H) The real cgr source carries its own .cgr-hash-cache.json from prior
        # (H) indexing. If the scenario copies it, a future-dated cache makes every
        # (H) file look unchanged and the baseline index skips them, so the diff
        # (H) against a clean re-index becomes meaningless. The runner must purge
        # (H) any copied cache so the baseline is a true full index.
        src = tmp_path / "proj"
        _make_repo(src)
        (src / "other.py").write_text("def helper():\n    return 2\n", encoding="utf-8")
        cache = src / cs.HASH_CACHE_FILENAME
        cache.write_text(
            json.dumps(
                {
                    "callee.py": "x",
                    "caller.py": "x",
                    "other.py": "x",
                    "__init__.py": "x",
                }
            ),
            encoding="utf-8",
        )
        (src / cs.DIR_MTIMES_FILENAME).write_text(
            json.dumps({cs.ROOT_DIR_KEY: 0.0}), encoding="utf-8"
        )
        future = max(p.stat().st_mtime for p in src.glob("*.py")) + 1000
        os.utime(cache, (future, future))

        parsers, queries = parsers_queries
        incr, clean = run_neutral_edit_scenario(
            src, "proj", "callee.py", parsers, queries, tmp_path / "scn"
        )
        # (H) other.py was never edited; it must still be indexed in the baseline.
        assert (_FUNCTION, "proj.other.helper") in clean.nodes
        assert (_FUNCTION, "proj.other.helper") in incr.nodes

    def test_incremental_preserves_property_dispatch_editing_caller(
        self, tmp_path: Path, parsers_queries: tuple[object, object]
    ) -> None:
        # (H) Issue #532 residual (full-parity): editing client.py re-parses only it,
        # (H) so the property status of Factory.dep (a @property in the unchanged
        # (H) factory.py) is not re-marked. cgr resolves the attribute access
        # (H) `self.d.dep` to that property via its property-name set, which the
        # (H) incremental run must rehydrate from the graph (not just the function
        # (H) registry). Without it the property-dispatch edge drops vs a clean index.
        method = cs.NodeLabel.METHOD.value
        prop_call = (
            _FUNCTION,
            "proj.client.use",
            _CALLS,
            method,
            "proj.factory.Factory.dep",
        )
        src = tmp_path / "proj"
        src.mkdir(parents=True)
        (src / "__init__.py").write_text("", encoding="utf-8")
        (src / "factory.py").write_text(
            "class Factory:\n    @property\n    def dep(self):\n        return 1\n",
            encoding="utf-8",
        )
        (src / "client.py").write_text(
            "from proj.factory import Factory\n\n\n"
            "def use(f: Factory):\n    return f.dep\n",
            encoding="utf-8",
        )
        parsers, queries = parsers_queries
        incr, clean = run_neutral_edit_scenario(
            src, "proj", "client.py", parsers, queries, tmp_path / "scn"
        )
        assert prop_call in clean.edges
        assert prop_call in incr.edges
        assert incr == clean

    def test_compare_states_flags_missing_edge(self) -> None:
        from evals.types_defs import GraphState

        clean = GraphState(frozenset({("Module", "proj")}), frozenset({_INBOUND_CALL}))
        incr = GraphState(frozenset({("Module", "proj")}), frozenset())
        result = compare_states(incr, clean)
        calls_row = next(r for r in result.rows if r["label"] == _CALLS)
        assert calls_row["fn"] == 1
        assert calls_row["recall"] == 0.0
