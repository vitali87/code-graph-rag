# Edge resolution confidence and trace write-back (issue #1526): every
# CALLS/REFERENCES/INSTANTIATES edge says how it was bound, a trace upgrades
# the edges it observed and creates `dynamic` edges (with the dispatch
# literal's site when one exists) for calls the static pass missed, the
# deterministic tools surface the label, and dead-code can ignore edges
# below a confidence floor.
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag import cypher_queries as cq
from codebase_rag import graph_query
from codebase_rag.cypher_queries import (
    CYPHER_TRACE_CALLABLES,
    CYPHER_TRACE_CONFIRM_CALLS,
    CYPHER_TRACE_EXISTING_CALLS,
)
from codebase_rag.dead_code import collect_dead_code, default_dead_code_config
from codebase_rag.tests.conftest import create_and_run_updater
from codebase_rag.trace.ingest import ingest_trace
from codebase_rag.trace.records import (
    CallRecord,
    FramePoint,
    TraceHeader,
    write_trace_file,
)
from codebase_rag.types_defs import PropertyDict

PROJECT = "proj"


def _edges(mock: MagicMock, rel: str) -> list[tuple[str, str, PropertyDict]]:
    out = []
    for c in mock.ensure_relationship_batch.call_args_list:
        if str(c.args[1]) != rel:
            continue
        props = (
            c.kwargs.get("properties") or (c.args[3] if len(c.args) > 3 else {}) or {}
        )
        out.append((str(c.args[0][2]), str(c.args[2][2]), props))
    return out


def _resolutions(
    mock: MagicMock, caller_suffix: str, rel: str = "CALLS"
) -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for src, dst, props in _edges(mock, rel):
        if src.endswith(caller_suffix):
            found.setdefault(
                dst.rsplit(".", 1)[-1] if rel == "CALLS" else dst, set()
            ).add(str(props.get(cs.KEY_RESOLUTION)))
    return found


# --- static tagging -------------------------------------------------------------


def test_exact_and_heuristic_calls_are_tagged(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    (temp_repo / "pkg").mkdir()
    (temp_repo / "pkg" / "__init__.py").write_text("")
    (temp_repo / "pkg" / "util.py").write_text(
        "def helper():\n    return 1\n\n\ndef lonely():\n    return 2\n"
    )
    (temp_repo / "pkg" / "app.py").write_text(
        "from pkg.util import helper\n\n\ndef run():\n    helper()\n    lonely()\n"
    )
    create_and_run_updater(temp_repo, mock_ingestor)
    by_callee = _resolutions(mock_ingestor, ".pkg.app.run")
    # Bound through the import: exact. Not imported, found by name only: heuristic.
    assert by_callee["helper"] == {cs.EdgeResolution.EXACT}
    assert by_callee["lonely"] == {cs.EdgeResolution.HEURISTIC}


def test_an_engine_bound_call_does_not_inherit_the_previous_label(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # The Java engine binds `s.bar()` without going through the resolver's
    # generic path, where the verdict is reset; the label must still be
    # this call's own, whatever the node before it resolved to.
    (temp_repo / "Svc.java").write_text(
        "public class Svc {\n    public void bar() {}\n}\n"
    )
    (temp_repo / "Main.java").write_text(
        "public class Main {\n    Svc s;\n    void run() {\n        s.bar();\n"
        "        new Svc();\n        s.bar();\n    }\n}\n"
    )
    create_and_run_updater(temp_repo, mock_ingestor, skip_if_missing="java")
    by_callee = _resolutions(mock_ingestor, ".Main.run()")
    assert by_callee["bar()"] == {cs.EdgeResolution.EXACT}


def test_typed_cpp_operator_and_alias_calls_do_not_inherit_the_previous_label(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # `lonely()` binds by name only (heuristic). The typed operator call and
    # the body-local alias construction that follow bind through the C++
    # engine and the alias map, neither of which enters the resolver's
    # generic path where the verdict resets; each must carry its own label.
    (temp_repo / "vec.cpp").write_text(
        "namespace far { int lonely(); }\n"
        "namespace math {\n"
        "class Vec {\npublic:\n    Vec operator+(const Vec& other) const;\n};\n"
        "Vec Vec::operator+(const Vec& other) const { return *this; }\n"
        "class Foo { public: Foo(); };\nFoo::Foo() {}\n"
        "int combine(Vec a, Vec b) {\n"
        "    lonely();\n    Vec c = a + b;\n"
        "    lonely();\n    using Alias = Foo;\n    Alias();\n    return 0;\n}\n}\n"
    )
    create_and_run_updater(temp_repo, mock_ingestor, skip_if_missing="cpp")
    by_callee = _resolutions(mock_ingestor, ".math.combine")
    assert by_callee["lonely"] == {cs.EdgeResolution.HEURISTIC}
    assert by_callee["operator_plus"] == {cs.EdgeResolution.EXACT}
    assert by_callee["Foo"] == {cs.EdgeResolution.EXACT}


def test_resolving_a_calls_arguments_does_not_change_its_own_label(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # `helper(lonely)` binds helper through the import (exact) and then
    # resolves the argument reference `lonely` by name (heuristic); the
    # argument pass must not rewrite the verdict the call edge reads back.
    (temp_repo / "pkg").mkdir()
    (temp_repo / "pkg" / "__init__.py").write_text("")
    (temp_repo / "pkg" / "util.py").write_text(
        "def helper(f):\n    return f()\n\n\ndef lonely():\n    return 2\n"
    )
    (temp_repo / "pkg" / "app.py").write_text(
        "from pkg.util import helper\n\n\ndef run():\n    helper(lonely)\n"
    )
    create_and_run_updater(temp_repo, mock_ingestor)
    by_callee = _resolutions(mock_ingestor, ".pkg.app.run")
    assert by_callee["helper"] == {cs.EdgeResolution.EXACT}


def test_a_higher_order_calls_callback_does_not_change_its_own_label(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # `sorted` is a first-party shadow bound through the import (exact); the
    # higher-order pass then resolves its `key=lonely` callback by name
    # (heuristic) before the outer edge reads the verdict back.
    (temp_repo / "pkg").mkdir()
    (temp_repo / "pkg" / "__init__.py").write_text("")
    (temp_repo / "pkg" / "util.py").write_text(
        "def sorted(items, key=None):\n    return items\n\n\n"
        "def lonely(x):\n    return x\n"
    )
    (temp_repo / "pkg" / "app.py").write_text(
        "from pkg.util import sorted\n\n\ndef run(xs):\n    return sorted(xs, key=lonely)\n"
    )
    create_and_run_updater(temp_repo, mock_ingestor)
    by_callee = _resolutions(mock_ingestor, ".pkg.app.run")
    assert by_callee["sorted"] == {cs.EdgeResolution.EXACT}


def test_ambiguous_callee_fans_out_as_overload(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # Two same-line duplicates of one name register as variants; a call to
    # the name reaches both, each edge tagged overload.
    (temp_repo / "m.py").write_text(
        "def twin(a): return 1\n"
        "def other(): return twin(1)\n"
        "if True:\n    def twin(b): return 2\n"
    )
    create_and_run_updater(temp_repo, mock_ingestor)
    edges = [
        (dst, props)
        for src, dst, props in _edges(mock_ingestor, "CALLS")
        if src.endswith(".m.other")
    ]
    assert len(edges) >= 2, edges
    assert {props.get(cs.KEY_RESOLUTION) for _dst, props in edges} == {
        cs.EdgeResolution.OVERLOAD
    }


def test_trace_confirmation_leaves_trace_only_edges_alone() -> None:
    # A static edge and an earlier run's trace-only edge can share a pair;
    # confirming the pair must not relabel the `dynamic` one. No Cypher
    # engine runs here, so the filter is pinned on the query text.
    assert "coalesce(r.static_missed, false) = false" in CYPHER_TRACE_CONFIRM_CALLS


def test_schema_and_query_column_carry_resolution() -> None:
    from codebase_rag.types_defs import RELATIONSHIP_PROPERTY_SCHEMAS

    calls_schema = next(
        s for s in RELATIONSHIP_PROPERTY_SCHEMAS if "CALLS" in str(s.rel_types)
    )
    assert "resolution: string?" in calls_schema.properties
    assert "r.resolution AS resolution" in cq.CYPHER_GRAPH_CALLERS
    assert "r.resolution AS resolution" in cq.CYPHER_DEAD_CODE_RELS


def test_callers_rows_surface_resolution() -> None:
    def fetch_all(query: str, params: PropertyDict | None = None) -> list:
        assert query == cq.CYPHER_GRAPH_CALLERS
        return [
            {
                cs.KEY_LABEL: "Function",
                cs.KEY_QUALIFIED_NAME: f"{PROJECT}.a.f",
                cs.KEY_PATH: "a.py",
                cs.KEY_LINE: 3,
                cs.KEY_COL: 4,
                cs.KEY_RESOLUTION: cs.EdgeResolution.HEURISTIC,
            }
        ]

    (row,) = graph_query.callers(fetch_all, PROJECT, f"{PROJECT}.b.g")
    assert row["resolution"] == "heuristic"


# --- trace write-back ------------------------------------------------------------


class _Graph:
    def __init__(self, callables: list[dict], existing: list[dict]) -> None:
        self.callables = callables
        self.existing = existing
        self.edges: list[tuple] = []
        self.writes: list[tuple[str, dict]] = []

    def fetch_all(self, query: str, params: dict | None = None) -> list[dict]:
        if query == CYPHER_TRACE_CALLABLES:
            return self.callables
        if query == CYPHER_TRACE_EXISTING_CALLS:
            return self.existing
        raise AssertionError(query)

    def execute_write(self, query: str, params: dict | None = None) -> None:
        assert query == CYPHER_TRACE_CONFIRM_CALLS, (
            "only the in-place upgrade may write"
        )
        self.writes.append((query, dict(params or {})))

    def ensure_node_batch(self, label: str, properties: dict) -> None:
        raise AssertionError("no nodes")

    def ensure_relationship_batch(
        self, from_spec, rel_type, to_spec, properties=None
    ) -> None:
        self.edges.append((from_spec[2], to_spec[2], dict(properties or {})))

    def flush_all(self) -> None:
        return None


def _callable(qn: str, path: str, start: int, end: int) -> dict:
    return {
        cs.KEY_LABEL: "Function",
        cs.KEY_QUALIFIED_NAME: qn,
        cs.KEY_PATH: path,
        cs.KEY_START_LINE: start,
        cs.KEY_END_LINE: end,
    }


def _trace(
    path: Path,
    repo: Path,
    calls: list[tuple[tuple[str, str, int], tuple[str, str, int]]],
) -> Path:
    header = TraceHeader(
        version=cs.TRACE_FORMAT_VERSION,
        language=cs.TRACE_LANGUAGE_PYTHON,
        repo_root=str(repo),
        tracer="t",
        sampled=False,
    )
    records = [
        CallRecord(
            caller=FramePoint(path=str(repo / c[0]), qualname=c[1], line=c[2]),
            callee=FramePoint(path=str(repo / d[0]), qualname=d[1], line=d[2]),
            count=1,
            workloads=("w",),
            receiver_types=(),
        )
        for c, d in calls
    ]
    write_trace_file(path, header, records)
    return path


def test_trace_upgrades_observed_edges_and_tags_dynamic_ones(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "app.py").write_text(
        "def run(obj):\n"
        "    known(obj)\n"
        '    getattr(obj, "hidden")()\n'
        "    return REGISTRY['keyed']()\n"
    )
    (repo / "pkg" / "svc.py").write_text(
        "def known(o): pass\ndef hidden(): pass\ndef keyed(): pass\ndef ghost(): pass\n"
    )
    graph = _Graph(
        [
            _callable(f"{PROJECT}.pkg.app.run", "pkg/app.py", 1, 4),
            _callable(f"{PROJECT}.pkg.svc.known", "pkg/svc.py", 1, 1),
            _callable(f"{PROJECT}.pkg.svc.hidden", "pkg/svc.py", 2, 2),
            _callable(f"{PROJECT}.pkg.svc.keyed", "pkg/svc.py", 3, 3),
            _callable(f"{PROJECT}.pkg.svc.ghost", "pkg/svc.py", 4, 4),
        ],
        [
            {
                cs.KEY_FROM_QN: f"{PROJECT}.pkg.app.run",
                cs.KEY_TO_QN: f"{PROJECT}.pkg.svc.known",
            }
        ],
    )
    run = ("pkg/app.py", "run", 1)
    trace = _trace(
        tmp_path / "t.jsonl",
        repo,
        [
            (run, ("pkg/svc.py", "known", 1)),
            (run, ("pkg/svc.py", "hidden", 2)),
            (run, ("pkg/svc.py", "keyed", 3)),
            (run, ("pkg/svc.py", "ghost", 4)),
        ],
    )
    summary = ingest_trace(trace, graph, repo, PROJECT)
    assert summary.confirmed_static == 1
    assert summary.static_missed == 3

    # The observed static edge is upgraded in place, on every site.
    assert graph.writes == [
        (
            CYPHER_TRACE_CONFIRM_CALLS,
            {
                cs.KEY_FROM_QN: f"{PROJECT}.pkg.app.run",
                cs.KEY_TO_QN: f"{PROJECT}.pkg.svc.known",
                cs.KEY_RESOLUTION: "trace_confirmed",
            },
        )
    ]
    by_callee = {dst.rsplit(".", 1)[-1]: props for _src, dst, props in graph.edges}
    assert by_callee["known"][cs.KEY_RESOLUTION] == "trace_confirmed"
    # getattr(obj, "hidden"): the literal's site is recorded.
    hidden = by_callee["hidden"]
    assert hidden[cs.KEY_RESOLUTION] == "dynamic"
    assert hidden[cs.KEY_DISPATCH_LITERAL] is True
    assert (hidden[cs.KEY_LINE], hidden[cs.KEY_COL]) == (3, 17)
    assert cs.KEY_UNLOCATABLE not in hidden
    # REGISTRY['keyed']: a subscript key literal counts too.
    keyed = by_callee["keyed"]
    assert keyed[cs.KEY_DISPATCH_LITERAL] is True
    assert keyed[cs.KEY_LINE] == 4
    # No literal names `ghost`: the edge says so.
    ghost = by_callee["ghost"]
    assert ghost[cs.KEY_RESOLUTION] == "dynamic"
    assert ghost[cs.KEY_UNLOCATABLE] is True
    assert cs.KEY_LINE not in ghost


def test_dispatch_literal_is_recorded_only_when_it_is_unique(tmp_path: Path) -> None:
    from codebase_rag.trace.dispatch_site import locate_dispatch_literal

    (tmp_path / "app.py").write_text(
        "def run(obj):\n"
        "    def inner():\n"
        '        return getattr(obj, "solo")\n'
        '    later = lambda: getattr(obj, "solo")\n'
        '    getattr(obj, "solo")()\n'
        '    table = {"twice": 1}\n'
        '    return getattr(obj, "twice")()\n'
        "\n"
        "def other(obj):\n"
        '    return getattr(obj, "elsewhere")\n'
    )
    # The nested def's and the lambda's literals belong to other callables
    # (the trace never attributes their frames to `run`), so `solo` has one
    # site in `run`'s own body and that site is recorded.
    solo = locate_dispatch_literal(tmp_path, "app.py", 1, 7, "solo")
    assert solo is not None
    assert (solo[cs.KEY_LINE], solo[cs.KEY_COL]) == (5, 17)
    # An earlier same-named dict key and the getattr argument cannot be told
    # apart, so `twice` is unlocatable rather than pointed at the wrong one.
    assert locate_dispatch_literal(tmp_path, "app.py", 1, 7, "twice") is None
    # A literal outside the span never counts.
    assert locate_dispatch_literal(tmp_path, "app.py", 1, 7, "elsewhere") is None


def test_a_nested_callers_literal_is_its_own_and_siblings_do_not_leak(
    tmp_path: Path,
) -> None:
    from codebase_rag.trace.dispatch_site import locate_dispatch_literal

    # The trace attributes a nested function's frame to that function, whose
    # span is its own body; a sibling nested function's matching literal is
    # outside that span, and the outer function owns none of them.
    (tmp_path / "app.py").write_text(
        "def run(obj):\n"
        "    def first(o):\n"
        '        return getattr(o, "target")()\n'
        "\n"
        "    def second(o):\n"
        '        table = {"target": 1}\n'
        '        return getattr(o, "target")()\n'
        "\n"
        "    def third(o):\n"
        "        return o.plain()\n"
        "\n"
        "    return first(obj) or second(obj) or third(obj)\n"
    )
    first = locate_dispatch_literal(tmp_path, "app.py", 2, 3, "target")
    assert first is not None
    assert (first[cs.KEY_LINE], first[cs.KEY_COL]) == (3, 26)
    # `second` holds two candidates of its own: unlocatable, not the sibling's.
    assert locate_dispatch_literal(tmp_path, "app.py", 5, 7, "target") is None
    # `third` holds none: the siblings' literals must not leak in.
    assert locate_dispatch_literal(tmp_path, "app.py", 9, 10, "target") is None
    # The outer function owns no nested body's literal.
    assert locate_dispatch_literal(tmp_path, "app.py", 1, 12, "target") is None


@pytest.mark.parametrize(
    "computed",
    ["getattr(obj, name)()", "TABLE[name]()", "fn = getattr(obj, name)\n    fn()"],
)
def test_a_computed_dispatch_in_the_body_makes_the_edge_unlocatable(
    tmp_path: Path, computed: str
) -> None:
    from codebase_rag.trace.dispatch_site import locate_dispatch_literal

    # The traced call may have gone through the computed dispatch, so the
    # one unrelated literal naming the callee must not be reported as its
    # site; a literal-keyed lookup in the same body is not a computed one.
    (tmp_path / "app.py").write_text(
        "def run(obj, name):\n"
        f"    {computed}\n"
        '    getattr(obj, "hidden")\n'
        '    TABLE["keyed"]()\n'
    )
    assert locate_dispatch_literal(tmp_path, "app.py", 1, 5, "hidden") is None
    (tmp_path / "plain.py").write_text(
        'def run(obj, name):\n    getattr(obj, "hidden")\n    TABLE["keyed"]()\n'
    )
    assert locate_dispatch_literal(tmp_path, "plain.py", 1, 3, "hidden") is not None


# --- dead code floor --------------------------------------------------------------


def _dead_code_with(min_resolution: str | None) -> set[str]:
    nodes = [
        {
            cs.KEY_LABEL: "Function",
            cs.KEY_QUALIFIED_NAME: f"{PROJECT}.m.main",
            cs.KEY_NAME: "main",
            cs.KEY_PATH: "m.py",
            cs.KEY_DECORATORS: [],
            cs.KEY_START_LINE: 1,
            cs.KEY_END_LINE: 2,
        },
        {
            cs.KEY_LABEL: "Function",
            cs.KEY_QUALIFIED_NAME: f"{PROJECT}.m.guess",
            cs.KEY_NAME: "guess",
            cs.KEY_PATH: "m.py",
            cs.KEY_DECORATORS: [],
            cs.KEY_START_LINE: 3,
            cs.KEY_END_LINE: 4,
        },
        {
            cs.KEY_LABEL: "Function",
            cs.KEY_QUALIFIED_NAME: f"{PROJECT}.m.sure",
            cs.KEY_NAME: "sure",
            cs.KEY_PATH: "m.py",
            cs.KEY_DECORATORS: [],
            cs.KEY_START_LINE: 5,
            cs.KEY_END_LINE: 6,
        },
    ]
    rels = [
        {
            cs.KEY_FROM_LABEL: "Function",
            cs.KEY_FROM_QN: f"{PROJECT}.m.main",
            cs.KEY_REL_TYPE: "CALLS",
            cs.KEY_TO_LABEL: "Function",
            cs.KEY_TO_QN: f"{PROJECT}.m.guess",
            cs.KEY_RESOLUTION: "heuristic",
        },
        {
            cs.KEY_FROM_LABEL: "Function",
            cs.KEY_FROM_QN: f"{PROJECT}.m.main",
            cs.KEY_REL_TYPE: "CALLS",
            cs.KEY_TO_LABEL: "Function",
            cs.KEY_TO_QN: f"{PROJECT}.m.sure",
            cs.KEY_RESOLUTION: None,
        },
    ]
    ingestor = MagicMock()
    ingestor.fetch_all = MagicMock(
        side_effect=lambda q, p=None: nodes if q == cq.CYPHER_DEAD_CODE_NODES else rels
    )
    config = default_dead_code_config(
        include_tests=False, include_classes=False
    )._replace(entry_points=(f"{PROJECT}.m.main",), min_resolution=min_resolution)
    rows = collect_dead_code(ingestor, PROJECT, config)
    return {str(r[cs.KEY_QUALIFIED_NAME]) for r in rows}


def test_min_resolution_keeps_structural_relationships() -> None:
    # INHERITS and its kin carry no resolution label; a floor above
    # `exact` must not drop them, or the walk loses the paths that keep
    # overrides and protocol stubs alive.
    from unittest.mock import patch

    from codebase_rag import dead_code as dead_code_module

    rels = [
        {
            cs.KEY_FROM_LABEL: "Class",
            cs.KEY_FROM_QN: f"{PROJECT}.m.Derived",
            cs.KEY_REL_TYPE: "INHERITS",
            cs.KEY_TO_LABEL: "Class",
            cs.KEY_TO_QN: f"{PROJECT}.m.Base",
            cs.KEY_RESOLUTION: None,
        },
        {
            cs.KEY_FROM_LABEL: "Function",
            cs.KEY_FROM_QN: f"{PROJECT}.m.main",
            cs.KEY_REL_TYPE: "CALLS",
            cs.KEY_TO_LABEL: "Function",
            cs.KEY_TO_QN: f"{PROJECT}.m.sure",
            cs.KEY_RESOLUTION: "exact",
        },
    ]
    ingestor = MagicMock()
    ingestor.fetch_all = MagicMock(
        side_effect=lambda q, p=None: [] if q == cq.CYPHER_DEAD_CODE_NODES else rels
    )
    config = default_dead_code_config(
        include_tests=False, include_classes=False
    )._replace(entry_points=(f"{PROJECT}.m.main",), min_resolution="dynamic")
    with patch.object(
        dead_code_module, "dead_code_from_graph", return_value=set()
    ) as walk:
        collect_dead_code(ingestor, PROJECT, config)
    kept = {rel[2] for rel in walk.call_args.args[1]}
    assert kept == {"INHERITS"}, kept


def test_min_resolution_drops_heuristic_edges_from_the_walk() -> None:
    assert _dead_code_with(None) == set()
    # An unlabelled edge ranks as exact and survives; the heuristic one does not.
    assert _dead_code_with("exact") == {f"{PROJECT}.m.guess"}
    assert _dead_code_with("heuristic") == set()


def test_cli_accepts_min_resolution(tmp_path: Path) -> None:
    from unittest.mock import patch

    from typer.testing import CliRunner

    from codebase_rag.cli import app

    ingestor = MagicMock()
    ingestor.list_projects.return_value = [PROJECT]
    ingestor.fetch_all = MagicMock(return_value=[])
    ingestor.__enter__ = MagicMock(return_value=ingestor)
    ingestor.__exit__ = MagicMock(return_value=False)
    with patch("codebase_rag.cli.connect_memgraph", return_value=ingestor):
        result = CliRunner().invoke(
            app,
            [
                "dead-code",
                "-n",
                PROJECT,
                "--format",
                "json",
                "--min-resolution",
                "exact",
            ],
        )
    assert result.exit_code == 0, result.output
    with patch("codebase_rag.cli.connect_memgraph", return_value=ingestor):
        bad = CliRunner().invoke(
            app, ["dead-code", "-n", PROJECT, "--min-resolution", "maybe"]
        )
    assert bad.exit_code == 2


@pytest.mark.parametrize(
    ("label", "minimum", "expected"),
    [
        (None, "exact", True),
        ("heuristic", "exact", False),
        ("overload", "exact", False),
        ("overload", "overload", True),
        ("dynamic", "trace_confirmed", True),
        ("trace_confirmed", "exact", True),
        ("garbage", "heuristic", False),
    ],
)
def test_resolution_ranking(label: str | None, minimum: str, expected: bool) -> None:
    from codebase_rag.dead_code import resolution_at_least

    assert resolution_at_least(label, minimum) is expected
