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
_EXTERNAL_MODULE = cs.NodeLabel.EXTERNAL_MODULE.value
_FILE = cs.NodeLabel.FILE.value
_FUNCTION = cs.NodeLabel.FUNCTION.value
_QN = cs.KEY_QUALIFIED_NAME
_DEFINES = cs.RelationshipType.DEFINES.value
_CALLS = cs.RelationshipType.CALLS.value
_IMPORTS = cs.RelationshipType.IMPORTS.value
_CONTAINS_FILE = cs.RelationshipType.CONTAINS_FILE.value

# The inbound call edge issue #532 drops: caller.use() calls callee.target().
_INBOUND_CALL = (_FUNCTION, "proj.caller.use", _CALLS, _FUNCTION, "proj.callee.target")


def _node(store: _StatefulIngestor, label: str, **props: object) -> None:
    store.ensure_node_batch(label, props)


def _module_subtree() -> _StatefulIngestor:
    # Two modules: callee.py defines target(); caller.py defines use() which
    # CALLS target(). Mirrors the real graph shape captured from cgr.
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
        # The caller subtree is untouched.
        assert (_FUNCTION, "proj.caller.use") in s.nodes
        # DETACH removes the inbound CALLS edge incident on the deleted target.
        assert not any(e[2] == _CALLS for e in s.edges)
        # The caller's own DEFINES edge survives.
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
        _node(s, _EXTERNAL_MODULE, qualified_name="ext", path="ext")

        files = s.fetch_all(cs.CYPHER_ALL_FILE_PATHS)
        assert {r[cs.KEY_PATH] for r in files} == {"a.py"}
        mods = s.fetch_all(cs.CYPHER_ALL_MODULE_PATHS_INTERNAL)
        assert {r[cs.KEY_PATH] for r in mods} == {"a.py"}

    def test_delete_orphan_external_modules(self) -> None:
        s = _StatefulIngestor()
        _node(s, _EXTERNAL_MODULE, qualified_name="ext.orphan", path="ext")
        _node(s, _EXTERNAL_MODULE, qualified_name="ext.used", path="ext2")
        _node(s, _MODULE, qualified_name="proj.m", path="m.py")
        s.ensure_relationship_batch(
            (_MODULE, _QN, "proj.m"), _IMPORTS, (_EXTERNAL_MODULE, _QN, "ext.used")
        )
        s.execute_write(cs.CYPHER_DELETE_ORPHAN_EXTERNAL_MODULES)

        assert (_EXTERNAL_MODULE, "ext.orphan") not in s.nodes
        assert (_EXTERNAL_MODULE, "ext.used") in s.nodes

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
        # Baseline: a clean forced index resolves caller.use -> callee.target.
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
        # Issue #532: editing the callee deletes its module subtree (and the
        # inbound CALLS incident on it). The fix must rebuild that inbound edge
        # from the unchanged caller, so the incremental graph equals a clean
        # re-index.
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
        # Editing the caller must rebuild its outbound call to the unchanged
        # callee. This requires the function registry to know definitions in
        # unchanged files (rehydrated from the persisted graph), not just the
        # changed file.
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
        # The real cgr source carries its own .cgr-hash-cache.json from prior
        # indexing. If the scenario copies it, a future-dated cache makes every
        # file look unchanged and the baseline index skips them, so the diff
        # against a clean re-index becomes meaningless. The runner must purge
        # any copied cache so the baseline is a true full index.
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
        # other.py was never edited; it must still be indexed in the baseline.
        assert (_FUNCTION, "proj.other.helper") in clean.nodes
        assert (_FUNCTION, "proj.other.helper") in incr.nodes

    def test_incremental_preserves_property_dispatch_editing_caller(
        self, tmp_path: Path, parsers_queries: tuple[object, object]
    ) -> None:
        # Issue #532 residual (full-parity): editing client.py re-parses only it,
        # so the property status of Factory.dep (a @property in the unchanged
        # factory.py) is not re-marked. cgr resolves the attribute access
        # `self.d.dep` to that property via its property-name set, which the
        # incremental run must rehydrate from the graph (not just the function
        # registry). Without it the property-dispatch edge drops vs a clean index.
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

    def test_incremental_preserves_protocol_dispatch_editing_caller(
        self, tmp_path: Path, parsers_queries: tuple[object, object]
    ) -> None:
        # Issue #532 residual: editing client.py re-parses only it, so
        # class_inheritance no longer records that GreeterProtocol subclasses
        # Protocol (iface.py was not re-parsed). Protocol dispatch keys off that
        # hierarchy: a clean index redirects g.greet() from the Protocol stub to
        # the concrete Greeter.greet, but without rehydrating class_inheritance
        # the incremental run leaves the call on the stub. The fix rehydrates the
        # hierarchy from persisted INHERITS edges before Pass 3.
        method = cs.NodeLabel.METHOD.value
        concrete = (
            _FUNCTION,
            "proj.client.use",
            _CALLS,
            method,
            "proj.impl.Greeter.greet",
        )
        stub = (
            _FUNCTION,
            "proj.client.use",
            _CALLS,
            method,
            "proj.iface.GreeterProtocol.greet",
        )
        src = tmp_path / "proj"
        src.mkdir(parents=True)
        (src / "__init__.py").write_text("", encoding="utf-8")
        (src / "iface.py").write_text(
            "from typing import Protocol\n\n\n"
            "class GreeterProtocol(Protocol):\n    def greet(self):\n        ...\n",
            encoding="utf-8",
        )
        (src / "impl.py").write_text(
            "class Greeter:\n    def greet(self):\n        return 1\n", encoding="utf-8"
        )
        (src / "client.py").write_text(
            "from proj.iface import GreeterProtocol\n\n\n"
            "def use(g: GreeterProtocol):\n    return g.greet()\n",
            encoding="utf-8",
        )
        parsers, queries = parsers_queries
        incr, clean = run_neutral_edit_scenario(
            src, "proj", "client.py", parsers, queries, tmp_path / "scn"
        )
        assert concrete in clean.edges
        assert stub not in clean.edges
        assert concrete in incr.edges
        assert incr == clean

    def test_all_inherits_query_returns_bases_in_base_index_order(self) -> None:
        # Rehydration replays INHERITS edges into class_inheritance; multiple
        # inheritance is order-sensitive (method resolution and override
        # attribution walk the base list first-match-wins). The persisted
        # base_index must restore source order regardless of the order the edges
        # were stored in. Insert five bases in reverse index order; the query
        # must return them by base_index (the store is a set, so without the
        # base_index ordering the returned order would not be base0..base4).
        klass = cs.NodeLabel.CLASS.value
        s = _StatefulIngestor()
        child = "proj.combined.Combined"
        want = [f"proj.mixins.Base{i}" for i in range(5)]
        for index in reversed(range(5)):
            s.ensure_relationship_batch(
                (klass, _QN, child),
                cs.RelationshipType.INHERITS.value,
                (klass, _QN, want[index]),
                {cs.KEY_BASE_INDEX: index},
            )
        rows = s.fetch_all(cs.CYPHER_ALL_INHERITS)
        bases = [r[cs.KEY_BASE_QN] for r in rows if r[cs.KEY_CHILD_QN] == child]
        assert bases == want

    def test_rehydrate_skips_multi_base_class_with_missing_base_index(self) -> None:
        # Greptile #572: an INHERITS edge written by an older index has no
        # base_index, so a multi-inheritance class's base order cannot be
        # trusted after an upgrade. Such a class must NOT be rehydrated (it would
        # risk binding a call/override to the wrong base); a single-base class is
        # order-free and is still rehydrated; a fully-ordered class is rehydrated
        # in base_index order; and a class already parsed locally is left alone.
        from codebase_rag.graph_updater import GraphUpdater

        rows: list[dict[str, object]] = [
            # Ordered multi-base -> rehydrated in index order.
            {cs.KEY_CHILD_QN: "p.Ok", cs.KEY_BASE_QN: "p.B", cs.KEY_BASE_INDEX: 1},
            {cs.KEY_CHILD_QN: "p.Ok", cs.KEY_BASE_QN: "p.A", cs.KEY_BASE_INDEX: 0},
            # Multi-base with a missing index -> skipped entirely.
            {cs.KEY_CHILD_QN: "p.Old", cs.KEY_BASE_QN: "p.X", cs.KEY_BASE_INDEX: None},
            {cs.KEY_CHILD_QN: "p.Old", cs.KEY_BASE_QN: "p.Y", cs.KEY_BASE_INDEX: 1},
            # Single base with a missing index -> still safe to rehydrate.
            {cs.KEY_CHILD_QN: "p.Solo", cs.KEY_BASE_QN: "p.Z", cs.KEY_BASE_INDEX: None},
            # Already parsed locally -> not touched.
            {cs.KEY_CHILD_QN: "p.Local", cs.KEY_BASE_QN: "p.W", cs.KEY_BASE_INDEX: 0},
        ]
        result = GraphUpdater._rehydrated_bases_by_child(rows, {"p.Local": ["p.W"]})
        assert result == {"p.Ok": ["p.A", "p.B"], "p.Solo": ["p.Z"]}

    def test_incremental_preserves_multiple_inheritance_dispatch(
        self, tmp_path: Path, parsers_queries: tuple[object, object]
    ) -> None:
        # Issue #532 residual (Greptile #572): editing the caller rehydrates
        # class_inheritance for the unchanged Combined(MixinA, MixinB). Python
        # MRO resolves c.shared() to MixinA.shared (first base). If rehydration
        # lost base order, an incremental run could bind it to MixinB.shared,
        # diverging from a clean index. base_index ordering keeps them equal.
        method = cs.NodeLabel.METHOD.value
        first_base = (
            _FUNCTION,
            "proj.client.use",
            _CALLS,
            method,
            "proj.mixins.MixinA.shared",
        )
        src = tmp_path / "proj"
        src.mkdir(parents=True)
        (src / "__init__.py").write_text("", encoding="utf-8")
        (src / "mixins.py").write_text(
            "class MixinA:\n    def shared(self):\n        return 1\n\n\n"
            "class MixinB:\n    def shared(self):\n        return 2\n",
            encoding="utf-8",
        )
        (src / "combined.py").write_text(
            "from proj.mixins import MixinA, MixinB\n\n\n"
            "class Combined(MixinA, MixinB):\n    pass\n",
            encoding="utf-8",
        )
        (src / "client.py").write_text(
            "from proj.combined import Combined\n\n\n"
            "def use(c: Combined):\n    return c.shared()\n",
            encoding="utf-8",
        )
        parsers, queries = parsers_queries
        incr, clean = run_neutral_edit_scenario(
            src, "proj", "client.py", parsers, queries, tmp_path / "scn"
        )
        assert first_base in clean.edges
        assert incr == clean

    def test_compare_states_flags_missing_edge(self) -> None:
        from evals.types_defs import GraphState

        clean = GraphState(frozenset({("Module", "proj")}), frozenset({_INBOUND_CALL}))
        incr = GraphState(frozenset({("Module", "proj")}), frozenset())
        result = compare_states(incr, clean)
        calls_row = next(r for r in result.rows if r["label"] == _CALLS)
        assert calls_row["fn"] == 1
        assert calls_row["recall"] == 0.0
