# Issue #2620: a Python class body can bind a method to a second member name
# (`run = _plain`, `__call__ = _fast if FAST else _slow`). The assignment is a
# real use of the method and the alias is a real member, but the indexer
# recorded neither: no REFERENCES edge reached `_plain`, a call through the
# alias (`obj.run()`, `obj()`) bound to nothing, and `cgr dead-code` reported
# every private implementation that sits behind such an alias (networkx's
# `_dispatchable._call_if_no_backends_installed`, the default dispatch path of
# nearly every networkx algorithm).
#
# The class body runs when its enclosing scope runs (module import for a
# top-level class), so the reference is that scope's, the way a call written
# in the class body (`prop = property(_get, _set)`) already is. A call through
# the alias binds to the aliased method; a conditional alias binds to every
# branch as `overload`, because the branch is chosen at runtime.
#
# The negative tests pin what must stay as it was: names the alias cannot see,
# a later redefinition, the truthiness-tested condition, plain data attributes,
# construction of the class itself, an untyped callable, other classes'
# same-named members, `super()` and the call-argument form the indexer
# already handled.
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag import cypher_queries as cq
from codebase_rag.dead_code import collect_dead_code, default_dead_code_config
from codebase_rag.function_registry import FunctionRegistryTrie
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.tests.conftest import create_and_run_updater
from codebase_rag.types_defs import NodeType, ResultRow
from evals.cgr_graph import _StatefulIngestor

PROJECT = "aliasproj"
MODULE = f"{PROJECT}.pkg.disp"
CLS = f"{MODULE}.Dispatcher"

_REFERENCES = cs.RelationshipType.REFERENCES.value
_CALLS = cs.RelationshipType.CALLS.value
_MODULE = cs.NodeLabel.MODULE.value
_FUNCTION = cs.NodeLabel.FUNCTION.value
_METHOD = cs.NodeLabel.METHOD.value

# The issue's reproduction, verbatim.
_ISSUE_REPRO = (
    "import os\n"
    'FAST = bool(os.environ.get("FAST"))\n'
    "\n"
    "class Dispatcher:\n"
    "    def _fast(self, x):\n"
    "        return x\n"
    "    def _slow(self, x):\n"
    "        return x * 2\n"
    "    def _plain(self, x):\n"
    "        return x + 1\n"
    "    __call__ = _fast if FAST else _slow\n"
    "    run = _plain\n"
    "\n"
    "def use():\n"
    "    return Dispatcher()(1) + Dispatcher().run(2)\n"
)

# networkx/utils/backends.py: an annotated, parenthesised conditional alias.
_NETWORKX_SHAPE = (
    "import typing\n"
    "backends = {}\n"
    "\n"
    "class Dispatcher:\n"
    "    def _call_if_any_backends_installed(self, *args):\n"
    "        return args\n"
    "    def _call_if_no_backends_installed(self, *args):\n"
    "        return args\n"
    "    # Dispatch only if there exist any installed backend(s)\n"
    "    __call__: typing.Callable = (\n"
    "        _call_if_any_backends_installed if backends else "
    "_call_if_no_backends_installed\n"
    "    )\n"
)

_Edges = dict[tuple[str, str, str], str | None]


def _index(
    temp_repo: Path, mock_ingestor: MagicMock, source: str, project: str = PROJECT
) -> None:
    root = temp_repo / project
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (root / "pkg" / "disp.py").write_text(source, encoding="utf-8")
    create_and_run_updater(root, mock_ingestor, skip_if_missing="python")


def _edges(mock_ingestor: MagicMock, rel: str) -> _Edges:
    """`rel` edges as (source label, source qn, target qn) -> resolution."""
    edges: _Edges = {}
    for c in mock_ingestor.ensure_relationship_batch.call_args_list:
        if str(c.args[1]) != rel:
            continue
        props = c.kwargs.get("properties") or (c.args[3] if len(c.args) > 3 else {})
        resolution = (props or {}).get(cs.KEY_RESOLUTION)
        edges[(str(c.args[0][0]), str(c.args[0][2]), str(c.args[2][2]))] = (
            str(resolution) if resolution is not None else None
        )
    return edges


def _targets(edges: _Edges, source_qn: str) -> dict[str, str | None]:
    return {
        target: resolution
        for (_label, source, target), resolution in edges.items()
        if source == source_qn
    }


def _dead(mock_ingestor: MagicMock) -> set[str]:
    # The dead-code engine over the graph the indexer recorded, fetched the
    # way its two Cypher reads return it, so the verdict is the one
    # `cgr dead-code` prints for this source.
    labels = {_FUNCTION, _METHOD, cs.NodeLabel.CLASS.value, _MODULE}
    nodes: list[ResultRow] = []
    for c in mock_ingestor.ensure_node_batch.call_args_list:
        label, props = str(c.args[0]), c.args[1]
        if label not in labels:
            continue
        nodes.append(
            {
                cs.KEY_LABEL: label,
                cs.KEY_QUALIFIED_NAME: props.get(cs.KEY_QUALIFIED_NAME),
                cs.KEY_NAME: props.get(cs.KEY_NAME),
                cs.KEY_PATH: props.get(cs.KEY_PATH),
                cs.KEY_START_LINE: props.get(cs.KEY_START_LINE),
                cs.KEY_END_LINE: props.get(cs.KEY_END_LINE),
                cs.KEY_DECORATORS: props.get(cs.KEY_DECORATORS) or [],
            }
        )
    rels: list[ResultRow] = [
        {
            cs.KEY_FROM_LABEL: str(c.args[0][0]),
            cs.KEY_FROM_QN: str(c.args[0][2]),
            cs.KEY_REL_TYPE: str(c.args[1]),
            cs.KEY_TO_LABEL: str(c.args[2][0]),
            cs.KEY_TO_QN: str(c.args[2][2]),
            cs.KEY_RESOLUTION: (c.kwargs.get("properties") or {}).get(
                cs.KEY_RESOLUTION
            ),
        }
        for c in mock_ingestor.ensure_relationship_batch.call_args_list
        if str(c.args[0][0]) in labels and c.args[0][1] == cs.KEY_QUALIFIED_NAME
    ]
    graph = MagicMock()
    graph.fetch_all = MagicMock(
        side_effect=lambda query, params=None: (
            nodes
            if query == cq.CYPHER_DEAD_CODE_NODES
            else rels
            if query == cq.CYPHER_DEAD_CODE_RELS
            else []
        )
    )
    config = default_dead_code_config(include_tests=True, include_classes=False)
    return {
        str(row[cs.KEY_QUALIFIED_NAME])
        for row in collect_dead_code(graph, PROJECT, config)
    }


# ---------------------------------------------------------------------------
# The class body's assignment references the method it binds
# ---------------------------------------------------------------------------


class TestAliasReferences:
    def test_issue_repro_references_every_aliased_method(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        _index(temp_repo, mock_ingestor, _ISSUE_REPRO)

        refs = _targets(_edges(mock_ingestor, _REFERENCES), MODULE)

        for method in ("_fast", "_slow", "_plain"):
            assert f"{CLS}.{method}" in refs, refs
        assert (_MODULE, MODULE, f"{CLS}._plain") in _edges(mock_ingestor, _REFERENCES)

    def test_issue_repro_reports_no_aliased_method_dead(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        _index(temp_repo, mock_ingestor, _ISSUE_REPRO)

        dead = _dead(mock_ingestor)

        assert not {f"{CLS}._fast", f"{CLS}._slow", f"{CLS}._plain"} & dead, dead

    def test_networkx_annotated_conditional_alias(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        _index(temp_repo, mock_ingestor, _NETWORKX_SHAPE)

        refs = _targets(_edges(mock_ingestor, _REFERENCES), MODULE)
        any_qn = f"{CLS}._call_if_any_backends_installed"
        none_qn = f"{CLS}._call_if_no_backends_installed"

        assert {any_qn, none_qn} <= set(refs), refs
        assert not {any_qn, none_qn} & _dead(mock_ingestor)

    def test_boolean_operator_alias_references_both_operands(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        _index(
            temp_repo,
            mock_ingestor,
            "class Dispatcher:\n"
            "    def _a(self):\n"
            "        return 1\n"
            "    def _b(self):\n"
            "        return 2\n"
            "    run = _a or _b\n",
        )

        refs = _targets(_edges(mock_ingestor, _REFERENCES), MODULE)

        assert {f"{CLS}._a", f"{CLS}._b"} <= set(refs), refs

    def test_decorated_method_and_chained_alias(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        # `a = b = _impl` binds both names; an alias of an alias still names
        # the method underneath.
        _index(
            temp_repo,
            mock_ingestor,
            "class Dispatcher:\n"
            "    @staticmethod\n"
            "    def _impl(x):\n"
            "        return x\n"
            "    run = execute = _impl\n"
            "    go = run\n"
            "\n"
            "def use():\n"
            "    return Dispatcher().go(1) + Dispatcher().execute(2)\n",
        )

        refs = _targets(_edges(mock_ingestor, _REFERENCES), MODULE)
        calls = _targets(_edges(mock_ingestor, _CALLS), f"{MODULE}.use")

        assert f"{CLS}._impl" in refs, refs
        assert calls.get(f"{CLS}._impl") == cs.EdgeResolution.EXACT, calls

    def test_alias_in_a_conditional_block_binds_every_branch(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        _index(
            temp_repo,
            mock_ingestor,
            "FAST = True\n"
            "\n"
            "class Dispatcher:\n"
            "    def _fast(self):\n"
            "        return 1\n"
            "    def _slow(self):\n"
            "        return 2\n"
            "    if FAST:\n"
            "        run = _fast\n"
            "    else:\n"
            "        run = _slow\n"
            "\n"
            "def use():\n"
            "    return Dispatcher().run()\n",
        )

        refs = _targets(_edges(mock_ingestor, _REFERENCES), MODULE)
        calls = _targets(_edges(mock_ingestor, _CALLS), f"{MODULE}.use")

        assert {f"{CLS}._fast", f"{CLS}._slow"} <= set(refs), refs
        assert calls.get(f"{CLS}._fast") == cs.EdgeResolution.OVERLOAD, calls
        assert calls.get(f"{CLS}._slow") == cs.EdgeResolution.OVERLOAD, calls

    def test_class_inside_a_function_references_from_that_function(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        # The body of a function-local class runs when the function runs, so
        # the function holds the reference: a dead factory keeps its class's
        # implementation dead.
        _index(
            temp_repo,
            mock_ingestor,
            "def make():\n"
            "    class Local:\n"
            "        def _impl(self):\n"
            "            return 1\n"
            "        run = _impl\n"
            "    return Local\n",
        )

        refs = _edges(mock_ingestor, _REFERENCES)
        impl = f"{MODULE}.make.Local._impl"

        assert (_FUNCTION, f"{MODULE}.make", impl) in refs, refs
        assert (_MODULE, MODULE, impl) not in refs, refs


# ---------------------------------------------------------------------------
# A call through the alias reaches the method it names
# ---------------------------------------------------------------------------


class TestCallsThroughAlias:
    def test_member_call_through_alias(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        _index(
            temp_repo,
            mock_ingestor,
            _ISSUE_REPRO
            + "\ndef use_var():\n    d = Dispatcher()\n    return d.run(3)\n",
        )

        calls = _edges(mock_ingestor, _CALLS)

        assert _targets(calls, f"{MODULE}.use").get(f"{CLS}._plain") == (
            cs.EdgeResolution.EXACT
        ), calls
        assert _targets(calls, f"{MODULE}.use_var").get(f"{CLS}._plain") == (
            cs.EdgeResolution.EXACT
        ), calls

    def test_self_call_through_alias(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        _index(
            temp_repo,
            mock_ingestor,
            "class Dispatcher:\n"
            "    def _plain(self):\n"
            "        return 1\n"
            "    run = _plain\n"
            "    def go(self):\n"
            "        return self.run()\n",
        )

        calls = _targets(_edges(mock_ingestor, _CALLS), f"{CLS}.go")

        assert f"{CLS}._plain" in calls, calls

    def test_inherited_alias(self, temp_repo: Path, mock_ingestor: MagicMock) -> None:
        _index(
            temp_repo,
            mock_ingestor,
            _ISSUE_REPRO + "\nclass Sub(Dispatcher):\n    pass\n"
            "\ndef use_sub():\n    return Sub().run(1)\n",
        )

        calls = _targets(_edges(mock_ingestor, _CALLS), f"{MODULE}.use_sub")

        assert f"{CLS}._plain" in calls, calls

    def test_instance_call_through_call_alias(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        # `obj(...)` runs type(obj).__call__; the alias picks one of two
        # methods at class creation, so both are candidates.
        _index(
            temp_repo,
            mock_ingestor,
            _ISSUE_REPRO + "\ndef use_var():\n    d = Dispatcher()\n    return d(3)\n",
        )

        calls = _edges(mock_ingestor, _CALLS)

        for caller in (f"{MODULE}.use", f"{MODULE}.use_var"):
            targets = _targets(calls, caller)
            assert targets.get(f"{CLS}._fast") == cs.EdgeResolution.OVERLOAD, targets
            assert targets.get(f"{CLS}._slow") == cs.EdgeResolution.OVERLOAD, targets

    def test_instance_call_reaches_a_defined_call_method(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        _index(
            temp_repo,
            mock_ingestor,
            "class Dispatcher:\n"
            "    def __call__(self, x):\n"
            "        return x\n"
            "\n"
            "def use():\n"
            "    d = Dispatcher()\n"
            "    return d(1) + Dispatcher()(2)\n",
        )

        calls = _targets(_edges(mock_ingestor, _CALLS), f"{MODULE}.use")

        assert calls.get(f"{CLS}.__call__") == cs.EdgeResolution.EXACT, calls

    def test_incremental_run_keeps_calls_through_an_unchanged_alias(
        self, temp_repo: Path
    ) -> None:
        # Re-parsing only the caller leaves the class's file to rehydration
        # from the graph, so the alias has to survive that round trip.
        parsers, queries = load_parsers()
        if cs.SupportedLanguage.PYTHON not in parsers:
            pytest.skip("python parser not available")
        root = temp_repo / PROJECT
        (root / "pkg").mkdir(parents=True)
        (root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
        (root / "pkg" / "disp.py").write_text(_ISSUE_REPRO, encoding="utf-8")
        app_source = (
            "from pkg.disp import Dispatcher\n"
            "\n"
            "def caller():\n"
            "    d = Dispatcher()\n"
            "    return d.run(1) + d(2)\n"
        )
        (root / "app.py").write_text(app_source, encoding="utf-8")

        def calls_from_caller(store: _StatefulIngestor) -> set[str]:
            return {
                str(target)
                for _, source, rel, _, target in store.edges
                if rel == _CALLS and str(source) == f"{PROJECT}.app.caller"
            }

        store = _StatefulIngestor()
        GraphUpdater(
            ingestor=store, repo_path=root, parsers=parsers, queries=queries
        ).run(force=True)
        expected = {f"{CLS}._plain", f"{CLS}._fast", f"{CLS}._slow"}
        assert expected <= calls_from_caller(store)

        (root / "app.py").write_text(app_source + "# touched\n", encoding="utf-8")
        GraphUpdater(
            ingestor=store, repo_path=root, parsers=parsers, queries=queries
        ).run(force=False)
        assert expected <= calls_from_caller(store)


# ---------------------------------------------------------------------------
# Negative tests: what must stay exactly as it was
# ---------------------------------------------------------------------------


class TestNeighboursUnchanged:
    def test_method_defined_after_the_alias_is_not_its_target(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        # At `run = _helper` the class namespace has no `_helper` yet, so the
        # name is the module's function; the later method is not referenced.
        _index(
            temp_repo,
            mock_ingestor,
            "def _helper(x):\n"
            "    return x\n"
            "\n"
            "class Dispatcher:\n"
            "    run = _helper\n"
            "    def _helper(self):\n"
            "        return 1\n",
        )

        refs = _edges(mock_ingestor, _REFERENCES)

        assert not any(target == f"{CLS}._helper" for _, _, target in refs), refs
        assert f"{CLS}._helper" in _dead(mock_ingestor)

    def test_later_definition_replaces_the_alias(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        _index(
            temp_repo,
            mock_ingestor,
            "class Dispatcher:\n"
            "    def _plain(self):\n"
            "        return 1\n"
            "    run = _plain\n"
            "    def run(self):\n"
            "        return 2\n"
            "\n"
            "def use():\n"
            "    return Dispatcher().run()\n",
        )

        calls = _targets(_edges(mock_ingestor, _CALLS), f"{MODULE}.use")

        assert f"{CLS}.run" in calls, calls
        assert f"{CLS}._plain" not in calls, calls

    def test_condition_of_a_conditional_alias_is_not_referenced(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        # Only the branches are bound; the condition is truthiness-tested.
        _index(
            temp_repo,
            mock_ingestor,
            "class Dispatcher:\n"
            "    def _probe(self):\n"
            "        return True\n"
            "    def _fast(self):\n"
            "        return 1\n"
            "    def _slow(self):\n"
            "        return 2\n"
            "    run = _fast if _probe else _slow\n",
        )

        refs = _targets(_edges(mock_ingestor, _REFERENCES), MODULE)

        assert {f"{CLS}._fast", f"{CLS}._slow"} <= set(refs), refs
        assert f"{CLS}._probe" not in refs, refs

    def test_plain_data_attributes_add_no_edge(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        _index(
            temp_repo,
            mock_ingestor,
            "LIMIT = 3\n"
            "\n"
            "class Dispatcher:\n"
            "    limit = 10\n"
            "    label = 'x'\n"
            "    other = LIMIT\n"
            "    def _impl(self):\n"
            "        return 1\n",
        )

        refs = _edges(mock_ingestor, _REFERENCES)

        assert not any(target.startswith(CLS) for _, _, target in refs), refs
        assert f"{CLS}._impl" in _dead(mock_ingestor)

    def test_constructing_the_class_is_not_an_instance_call(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        _index(
            temp_repo,
            mock_ingestor,
            _ISSUE_REPRO.replace(
                "Dispatcher()(1) + Dispatcher().run(2)", "Dispatcher()"
            ),
        )

        calls = _targets(_edges(mock_ingestor, _CALLS), f"{MODULE}.use")
        instantiates = _targets(
            _edges(mock_ingestor, cs.RelationshipType.INSTANTIATES.value),
            f"{MODULE}.use",
        )

        assert CLS in instantiates, instantiates
        assert not any(target.startswith(f"{CLS}.") for target in calls), calls

    @pytest.mark.parametrize(
        ("caller", "source"),
        [
            (
                f"{CLS}.make",
                "    @classmethod\n    def make(cls):\n        return cls(1)\n",
            ),
            (
                f"{MODULE}.by_class_ref",
                "\ndef by_class_ref():\n    k = Dispatcher\n    return k(1)\n",
            ),
        ],
        ids=["cls-in-classmethod", "variable-holding-the-class"],
    )
    def test_calling_a_class_object_is_not_an_instance_call(
        self, temp_repo: Path, mock_ingestor: MagicMock, caller: str, source: str
    ) -> None:
        # `cls(1)` and `k(1)` with `k = Dispatcher` construct; neither runs
        # __call__ or what it aliases.
        body = (
            _ISSUE_REPRO.replace("    run = _plain\n", "    run = _plain\n" + source)
            if source.startswith("    ")
            else _ISSUE_REPRO + source
        )
        _index(temp_repo, mock_ingestor, body)

        calls = _targets(_edges(mock_ingestor, _CALLS), caller)

        assert not {f"{CLS}._fast", f"{CLS}._slow"} & set(calls), calls

    def test_untyped_callable_call_adds_no_edge(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        _index(
            temp_repo,
            mock_ingestor,
            _ISSUE_REPRO + "\ndef apply(fn):\n    return fn(1)\n",
        )

        calls = _targets(_edges(mock_ingestor, _CALLS), f"{MODULE}.apply")

        assert not any(target.startswith(f"{CLS}.") for target in calls), calls

    def test_other_classes_members_stay_their_own(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        _index(
            temp_repo,
            mock_ingestor,
            _ISSUE_REPRO + "\nclass Other:\n"
            "    def run(self):\n"
            "        return 0\n"
            "\nclass Plain:\n"
            "    pass\n"
            "\ndef use_other():\n"
            "    return Other().run()\n"
            "\ndef use_plain():\n"
            "    return Plain().run()\n",
        )

        calls = _edges(mock_ingestor, _CALLS)

        assert set(_targets(calls, f"{MODULE}.use_other")) == {f"{MODULE}.Other.run"}, (
            calls
        )
        assert not any(
            target.startswith(f"{CLS}.")
            for target in _targets(calls, f"{MODULE}.use_plain")
        ), calls

    def test_super_call_skips_the_subclass_own_alias(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        _index(
            temp_repo,
            mock_ingestor,
            "class Base:\n"
            "    def run(self):\n"
            "        return 1\n"
            "\n"
            "class Dispatcher(Base):\n"
            "    def _impl(self):\n"
            "        return super().run()\n"
            "    run = _impl\n",
        )

        calls = _targets(_edges(mock_ingestor, _CALLS), f"{CLS}._impl")

        assert f"{MODULE}.Base.run" in calls, calls
        assert f"{CLS}._impl" not in calls, calls

    def test_removing_a_method_drops_it_from_its_aliases(self) -> None:
        # A re-parsed file registers its aliases again with its methods, so a
        # swept method must not leave a stale alias behind.
        registry = FunctionRegistryTrie()
        registry[f"{CLS}._fast"] = NodeType.METHOD
        registry[f"{CLS}._slow"] = NodeType.METHOD
        registry.add_member_alias(f"{CLS}.__call__", f"{CLS}._fast")
        registry.add_member_alias(f"{CLS}.__call__", f"{CLS}._slow")
        registry.add_member_alias(f"{CLS}.run", f"{CLS}._fast")

        del registry[f"{CLS}._fast"]

        assert registry.member_alias_targets(f"{CLS}.__call__") == (f"{CLS}._slow",)
        assert registry.member_alias_targets(f"{CLS}.run") == ()

    def test_call_argument_form_is_unchanged(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        # `prop = property(_get, _set)` was already handled by the call pass.
        _index(
            temp_repo,
            mock_ingestor,
            "class Dispatcher:\n"
            "    def _get(self):\n"
            "        return 1\n"
            "    def _set(self, value):\n"
            "        pass\n"
            "    prop = property(_get, _set)\n",
        )

        calls = _targets(_edges(mock_ingestor, _CALLS), MODULE)

        assert calls.get(f"{CLS}._get") == cs.EdgeResolution.HEURISTIC, calls
        assert calls.get(f"{CLS}._set") == cs.EdgeResolution.HEURISTIC, calls
