"""Issue #2459: a callable-parameter CALLS edge is located in its source's file.

`apply(double, 3)` in main.py hands `double` to `apply`, which invokes it as
`f(y)` in util.py, so the graph records `apply -[:CALLS]-> double`. That edge
carried the span of the ARGUMENT `double` in main.py while its source node
lives in util.py, so every consumer applied a main.py position to util.py:
`cgr graph callers` pointed past the end of the file, and `cgr rename` crashed
with a PatcherError (or, in a longer file, silently dropped the site). Go also
got a second copy at 1:0-7, a site left over from an unrelated pass.

The edge is now located at each invocation of the parameter inside the source
(`f(y)`, util.py:6) and names that parameter in `via_param`. The passing site
stays what it always was: the REFERENCES edge from the passing scope.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from watchdog.events import FileModifiedEvent

import realtime_updater
from codebase_rag import constants as cs
from codebase_rag import cypher_queries as cq
from codebase_rag import graph_query
from codebase_rag.editing.rename import QueryFn, RenameRefused, rename
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.parsers import call_processor as cp
from codebase_rag.tests.conftest import create_and_run_updater
from codebase_rag.types_defs import PropertyDict, PropertyParams, ResultRow
from evals.cgr_graph import _StatefulIngestor

PROJECT = "cbloc"
DOUBLE = f"{PROJECT}.pkg.main.double"

PY_UTIL = "# helpers\n\n\ndef apply(f, x):\n    y = x + 1\n    return f(y)\n"
PY_MAIN = (
    "from pkg.util import apply\n"
    "\n"
    "\n"
    "def double(x):\n"
    "    return x * 2\n"
    "\n"
    "\n"
    "def main():\n"
    "    total = 0\n"
    "    total += apply(double, 3)\n"
    "    return total\n"
)
PY_ISSUE = {"pkg/__init__.py": "", "pkg/util.py": PY_UTIL, "pkg/main.py": PY_MAIN}

Span = tuple[int, int, int, int]


def _span(text: str, needle: str, occurrence: int = 0) -> Span:
    """(line, col, end_line, end_col) of the n-th `needle` in `text`."""
    idx = -1
    for _ in range(occurrence + 1):
        idx = text.index(needle, idx + 1)
    line = text.count("\n", 0, idx) + 1
    col = idx - (text.rfind("\n", 0, idx) + 1)
    end = idx + len(needle)
    end_line = text.count("\n", 0, end) + 1
    end_col = end - (text.rfind("\n", 0, end) + 1)
    return line, col, end_line, end_col


def _write(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def _rels(
    mock: MagicMock, rel_type: str, src_suffix: str, dst_suffix: str
) -> list[PropertyDict]:
    out: list[PropertyDict] = []
    for c in mock.ensure_relationship_batch.call_args_list:
        if c.args[1] != rel_type:
            continue
        src, dst = str(c.args[0][2]), str(c.args[2][2])
        if not (src.endswith(src_suffix) and dst.endswith(dst_suffix)):
            continue
        props = c.kwargs.get("properties")
        if props is None and len(c.args) > 3:
            props = c.args[3]
        out.append(dict(props or {}))
    return out


def _site(props: PropertyDict) -> Span | None:
    if cs.KEY_LINE not in props:
        return None
    return (
        props[cs.KEY_LINE],
        props[cs.KEY_COL],
        props[cs.KEY_END_LINE],
        props[cs.KEY_END_COL],
    )


def _sites(props: list[PropertyDict]) -> set[Span | None]:
    return {_site(p) for p in props}


def _index(
    root: Path, files: dict[str, str], skip_if_missing: str | None = None
) -> MagicMock:
    _write(root, files)
    mock = MagicMock()
    create_and_run_updater(root, mock, skip_if_missing=skip_if_missing)
    return mock


def _store(root: Path, files: dict[str, str]) -> _StatefulIngestor:
    _write(root, files)
    parsers, queries = load_parsers()
    store = _StatefulIngestor()
    GraphUpdater(
        ingestor=store,
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name=PROJECT,
    ).run(force=True)
    return store


def _query(store: _StatefulIngestor) -> QueryFn:
    def fetch_all(query: str, params: PropertyParams | None) -> list[ResultRow]:
        return store.fetch_all(query, None if params is None else dict(params))

    return fetch_all


# --- the edge's location ----------------------------------------------------------


def test_python_callback_edge_is_located_at_the_invocation_in_its_own_file(
    temp_repo: Path,
) -> None:
    mock = _index(temp_repo, PY_ISSUE)

    edges = _rels(mock, "CALLS", ".pkg.util.apply", ".pkg.main.double")

    assert edges, "the callable-parameter edge is gone"
    # One site, `f(y)` on util.py:6, and never the argument `double` at
    # main.py 10:19-25, nor a copy without a site.
    assert _sites(edges) == {_span(PY_UTIL, "f(y)")}
    assert {p.get(cs.KEY_VIA_PARAM) for p in edges} == {"f"}


def test_each_invocation_of_the_parameter_is_its_own_site(temp_repo: Path) -> None:
    util = "def apply(f, x):\n    a = f(x)\n    return f(a)\n"
    mock = _index(temp_repo, {**PY_ISSUE, "pkg/util.py": util})

    edges = _rels(mock, "CALLS", ".pkg.util.apply", ".pkg.main.double")

    assert _sites(edges) == {_span(util, "f(x)"), _span(util, "f(a)")}


def test_an_invocation_inside_a_nested_closure_is_the_site(temp_repo: Path) -> None:
    util = (
        "def apply(f, x):\n    def run(v):\n        return f(v)\n\n    return run(x)\n"
    )
    mock = _index(temp_repo, {**PY_ISSUE, "pkg/util.py": util})

    edges = _rels(mock, "CALLS", ".pkg.util.apply", ".pkg.main.double")

    assert _sites(edges) == {_span(util, "f(v)")}


def test_a_forwarded_callback_is_located_where_the_receiver_invokes_it(
    temp_repo: Path,
) -> None:
    # main -> relay(double) -> apply(g, 1) -> f(x): only apply invokes it, and
    # the propagated edge used to carry no site at all.
    util = "def apply(f, x):\n    return f(x)\n"
    mid = "from pkg.util import apply\n\n\ndef relay(g):\n    return apply(g, 1)\n"
    main = (
        "from pkg.mid import relay\n\n\n"
        "def double(x):\n    return x * 2\n\n\n"
        "def main():\n    return relay(double)\n"
    )
    mock = _index(
        temp_repo,
        {
            "pkg/__init__.py": "",
            "pkg/util.py": util,
            "pkg/mid.py": mid,
            "pkg/main.py": main,
        },
    )

    edges = _rels(mock, "CALLS", ".pkg.util.apply", ".pkg.main.double")

    assert _sites(edges) == {_span(util, "f(x)")}
    assert {p.get(cs.KEY_VIA_PARAM) for p in edges} == {"f"}


GO_STORE = (
    "package store\n"
    "\n"
    "func Map(xs []int, f func(int) int) []int {\n"
    "\tout := []int{}\n"
    "\tfor _, x := range xs {\n"
    "\t\tout = append(out, f(x))\n"
    "\t}\n"
    "\treturn out\n"
    "}\n"
)
GO_MAIN = (
    "package main\n"
    "\n"
    "import (\n"
    '\t"fmt"\n'
    "\n"
    '\t"example.com/demo/store"\n'
    ")\n"
    "\n"
    "func double(x int) int {\n"
    "\treturn x * 2\n"
    "}\n"
    "\n"
    "func main() {\n"
    "\tfmt.Println(store.Map([]int{1, 2}, double))\n"
    "}\n"
)
GO_FILES = {
    "go.mod": "module example.com/demo\n\ngo 1.21\n",
    "store/store.go": GO_STORE,
    "main.go": GO_MAIN,
}


def test_go_callback_edge_has_one_site_at_the_invocation(temp_repo: Path) -> None:
    mock = _index(temp_repo, GO_FILES, skip_if_missing="go")

    edges = _rels(mock, "CALLS", ".store.store.Map", ".main.double")

    # Not the argument in main.go, and not the 1:0-7 site an unrelated pass
    # left behind for the propagated copy.
    assert _sites(edges) == {_span(GO_STORE, "f(x)")}
    assert {p.get(cs.KEY_VIA_PARAM) for p in edges} == {"f"}


JS_UTIL = "export function apply(f, x) {\n  const y = x + 1;\n  return f(y);\n}\n"
JS_MAIN = (
    'import { apply } from "./util.js";\n'
    "\n"
    "function double(x) {\n"
    "  return x * 2;\n"
    "}\n"
    "\n"
    "export function main() {\n"
    "  return apply(double, 3);\n"
    "}\n"
)


def test_js_callback_edge_is_located_at_the_invocation(temp_repo: Path) -> None:
    mock = _index(
        temp_repo,
        {"util.js": JS_UTIL, "main.js": JS_MAIN},
        skip_if_missing="javascript",
    )

    edges = _rels(mock, "CALLS", ".util.apply", ".main.double")

    assert _sites(edges) == {_span(JS_UTIL, "f(y)")}


def test_an_inline_arrow_callback_is_located_at_the_invocation(
    temp_repo: Path,
) -> None:
    # Only the direct-argument path reaches an inline function value.
    main = (
        'import { apply } from "./util.js";\n'
        "\n"
        "export function main() {\n"
        "  return apply((v) => v * 2, 3);\n"
        "}\n"
    )
    mock = _index(
        temp_repo,
        {"util.js": JS_UTIL, "main.js": main},
        skip_if_missing="javascript",
    )

    edges = _rels(mock, "CALLS", ".util.apply", ".main.main.anonymous_3_15")

    assert edges, "the inline arrow lost its callable-parameter edge"
    assert _sites(edges) == {_span(JS_UTIL, "f(y)")}
    assert {p.get(cs.KEY_VIA_PARAM) for p in edges} == {"f"}


# --- what must not move -------------------------------------------------------------


def test_the_direct_call_and_the_passing_reference_keep_their_sites(
    temp_repo: Path,
) -> None:
    mock = _index(temp_repo, PY_ISSUE)

    direct = _rels(mock, "CALLS", ".pkg.main.main", ".pkg.util.apply")
    assert _sites(direct) == {_span(PY_MAIN, "apply(double, 3)")}
    assert all(p[cs.KEY_ARG_COUNT] == 2 for p in direct)
    assert all(cs.KEY_VIA_PARAM not in p for p in direct)

    # The passing site belongs to the passing scope, as a REFERENCES edge.
    passed = _rels(mock, "REFERENCES", ".pkg.main.main", ".pkg.main.double")
    assert _sites(passed) == {_span(PY_MAIN, "double", occurrence=1)}
    assert all(cs.KEY_VIA_PARAM not in p for p in passed)


def test_a_callback_the_receiver_never_invokes_gets_no_calls_edge(
    temp_repo: Path,
) -> None:
    util = "HANDLERS = []\n\n\ndef register(f):\n    HANDLERS.append(f)\n"
    main = (
        "from pkg.util import register\n\n\n"
        "def double(x):\n    return x * 2\n\n\n"
        "def main():\n    register(double)\n"
    )
    mock = _index(
        temp_repo, {"pkg/__init__.py": "", "pkg/util.py": util, "pkg/main.py": main}
    )

    assert _rels(mock, "CALLS", ".pkg.util.register", ".pkg.main.double") == []
    assert _rels(mock, "REFERENCES", ".pkg.main.main", ".pkg.main.double")


def test_a_shadowing_closure_parameter_is_not_an_invocation_site(
    temp_repo: Path,
) -> None:
    # inner's own `f` hides apply's: only `f(x)` invokes what main passed.
    util = (
        "def apply(f, x):\n"
        "    y = f(x)\n"
        "\n"
        "    def inner(f):\n"
        "        return f(0)\n"
        "\n"
        "    return inner, y\n"
    )
    mock = _index(temp_repo, {**PY_ISSUE, "pkg/util.py": util})

    edges = _rels(mock, "CALLS", ".pkg.util.apply", ".pkg.main.double")

    assert _sites(edges) == {_span(util, "f(x)")}


def test_a_receiver_with_no_recorded_invocation_keeps_a_plain_siteless_edge(
    temp_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The registry says `apply` invokes `f`, but no walk recorded where. The
    # edge stays, with no site and no `via_param`: a site-less row merges on
    # its endpoints alone, so the property would land on any direct call
    # between the same pair and hide that real site from a rename.
    monkeypatch.setattr(cp, "callable_parameter_invocations", lambda *_: {})
    mock = _index(temp_repo, PY_ISSUE)

    edges = _rels(mock, "CALLS", ".pkg.util.apply", ".pkg.main.double")

    assert edges
    assert _sites(edges) == {None}
    assert all(cs.KEY_VIA_PARAM not in p for p in edges)


def test_editing_the_passing_file_keeps_the_receivers_invocation_site(
    temp_repo: Path,
) -> None:
    # The watch pass re-walks main.py alone; util.py's invocation site has to
    # survive from the walk that recorded it.
    project = temp_repo / "cbwatch"
    _write(project, PY_ISSUE)
    parsers, queries = load_parsers()
    mock = MagicMock()
    updater = GraphUpdater(
        ingestor=mock, repo_path=project, parsers=parsers, queries=queries
    )
    updater.run()
    main = project / "pkg" / "main.py"
    edited = PY_MAIN.replace("apply(double, 3)", "apply(double, 4)") + "\n# edited\n"
    main.write_text(edited, encoding="utf-8")
    mock.reset_mock()

    realtime_updater.CodeChangeEventHandler(updater, debounce_seconds=0).dispatch(
        FileModifiedEvent(str(main))
    )

    edges = _rels(mock, "CALLS", ".pkg.util.apply", ".pkg.main.double")
    assert _sites(edges) == {_span(PY_UTIL, "f(y)")}


def test_editing_the_receiver_moves_the_site_with_the_invocation(
    temp_repo: Path,
) -> None:
    project = temp_repo / "cbmove"
    _write(project, PY_ISSUE)
    parsers, queries = load_parsers()
    mock = MagicMock()
    updater = GraphUpdater(
        ingestor=mock, repo_path=project, parsers=parsers, queries=queries
    )
    updater.run()
    util = project / "pkg" / "util.py"
    moved = "# helpers\n# more\n" + PY_UTIL.split("\n", 1)[1]
    util.write_text(moved, encoding="utf-8")
    mock.reset_mock()

    realtime_updater.CodeChangeEventHandler(updater, debounce_seconds=0).dispatch(
        FileModifiedEvent(str(util))
    )

    edges = _rels(mock, "CALLS", ".pkg.util.apply", ".pkg.main.double")
    assert _sites(edges) == {_span(moved, "f(y)")}


# --- consumers: graph callers and rename ----------------------------------------------


def test_callers_row_points_into_the_callers_own_file(temp_repo: Path) -> None:
    store = _store(temp_repo, PY_ISSUE)

    rows = graph_query.callers(_query(store), PROJECT, DOUBLE)

    # util.py has 6 lines; the row used to say line 10, col 19.
    assert [(r["qualified_name"], r["path"], r["line"], r["col"]) for r in rows] == [
        (f"{PROJECT}.pkg.util.apply", "pkg/util.py", 6, 11)
    ]
    # The direct call keeps its own site in main.py.
    plain = graph_query.callers(_query(store), PROJECT, f"{PROJECT}.pkg.util.apply")
    assert [(r["qualified_name"], r["path"], r["line"], r["col"]) for r in plain] == [
        (f"{PROJECT}.pkg.main.main", "pkg/main.py", 10, 13)
    ]


def test_renaming_a_callback_passed_into_a_shorter_file_plans_cleanly(
    temp_repo: Path,
) -> None:
    store = _store(temp_repo, PY_ISSUE)

    report = rename(temp_repo, _query(store), PROJECT, DOUBLE, "twice", dry_run=True)

    assert report.unlocatable == ()
    assert sorted((s.kind, s.path, s.line, s.col) for s in report.sites) == [
        ("definition", "pkg/main.py", 4, 4),
        ("reference", "pkg/main.py", 10, 19),
    ]
    assert (temp_repo / "pkg" / "util.py").read_text(encoding="utf-8") == PY_UTIL


def test_renaming_a_callback_named_like_the_parameter_leaves_the_receiver_alone(
    temp_repo: Path,
) -> None:
    # The invocation site spells the PARAMETER `callback`, the same text as
    # the function being renamed; rewriting it would break `run`.
    util = "def run(callback):\n    return callback()\n"
    main = (
        "from pkg.util import run\n\n\n"
        "def callback():\n    return 1\n\n\n"
        "def main():\n    return run(callback)\n"
    )
    store = _store(
        temp_repo, {"pkg/__init__.py": "", "pkg/util.py": util, "pkg/main.py": main}
    )

    report = rename(
        temp_repo,
        _query(store),
        PROJECT,
        f"{PROJECT}.pkg.main.callback",
        "handler",
    )

    assert report.applied, report.message
    assert (temp_repo / "pkg" / "util.py").read_text(encoding="utf-8") == util
    assert (temp_repo / "pkg" / "main.py").read_text(encoding="utf-8") == (
        main.replace("def callback", "def handler").replace(
            "run(callback)", "run(handler)"
        )
    )


def test_a_site_past_the_end_of_its_file_refuses_instead_of_crashing(
    temp_repo: Path,
) -> None:
    # A stale graph (the file shrank since it was indexed) can still hand the
    # planner a position the file does not have.
    store = _store(temp_repo, PY_ISSUE)
    honest = _query(store)

    def stale(query: str, params: PropertyParams | None) -> list[ResultRow]:
        rows = honest(query, params)
        if query == cq.CYPHER_GRAPH_CALLERS:
            return [{**row, cs.KEY_LINE: 40, cs.KEY_END_LINE: 40} for row in rows]
        return rows

    with pytest.raises(RenameRefused) as refused:
        rename(temp_repo, stale, PROJECT, DOUBLE, "twice", dry_run=True)

    assert refused.value.unlocatable == [
        cs.RENAME_UNLOCATABLE_SITE.format(
            owner=f"{PROJECT}.pkg.util.apply",
            resolution=cs.RENAME_RESOLUTION_BAD_POSITION,
        )
    ]
    assert [(s.kind, s.path, s.line) for s in refused.value.ambiguous] == [
        ("unlocatable", "pkg/util.py", 40)
    ]


def test_renaming_a_class_a_factory_builds_through_its_parameter_still_refuses(
    temp_repo: Path,
) -> None:
    # `build(Model)` names the class where no edge of the class records it
    # (a class passed as a value is recorded against its constructor, here
    # absent), so the plan cannot reach that site. The INSTANTIATES edge on
    # `Model()` used to refuse only for carrying no site; it must still
    # refuse now that it has one, and say why.
    util = "def build(Model):\n    return Model()\n"
    main = (
        "from pkg.util import build\n\n\n"
        "class Model:\n    pass\n\n\n"
        "def main():\n    return build(Model)\n"
    )
    files = {"pkg/__init__.py": "", "pkg/util.py": util, "pkg/main.py": main}
    query = _query(_store(temp_repo, files))

    with pytest.raises(RenameRefused) as refused:
        rename(temp_repo, query, PROJECT, f"{PROJECT}.pkg.main.Model", "Entity")

    assert refused.value.unlocatable == [
        cs.RENAME_UNLOCATABLE_SITE.format(
            owner=f"{PROJECT}.pkg.util.build",
            resolution=cs.RENAME_RESOLUTION_CLASS_VIA_PARAM.format(param="Model"),
        )
    ]
    for rel, text in files.items():
        assert (temp_repo / rel).read_text(encoding="utf-8") == text
