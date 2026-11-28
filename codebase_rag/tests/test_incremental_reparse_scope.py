# Which unchanged files a sync re-parses beside an edited one (issue #3781).
#
# A one-line edit of golang/tools' go/packages/packages.go re-parsed 1,544 of
# its 2,945 files and took longer than a fresh index. Two rules brought in
# most of them: the callable-flow closure re-parsed every caller of anything
# in a file holding a function that invokes a parameter (812 files), and
# every re-parsed Go file brought in its whole package (660). The flow edges
# such a holder loses come only from the calls into its invoking functions,
# and an unchanged Go file shares nothing with its package beyond the types
# its methods hang off and the methods hung off its types, so each rule now
# re-parses only those. Every sync here is checked against a clean index of
# the edited tree.
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.config import settings
from codebase_rag.graph_dialects import DIALECT_SQLITE, get_dialect
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.parsers.callable_flow_scope import (
    FileFlowFunctions,
    flow_caller_paths,
)
from codebase_rag.services.graph_service import MemgraphIngestor
from codebase_rag.tests.test_incremental_interface_skip import (
    PROJECT,
    _clean,
    _diff,
    _edit_after_cache,
    _materialise,
    _sync,
    _updater,
)
from codebase_rag.types_defs import ResultRow
from evals.cgr_graph import _StatefulIngestor

_GO_MOD = "module example.com/m\n\ngo 1.21\n"

# registry.* holds a function that invokes its parameter and one that does
# not; plugins.* passes a callback to the first, reader.* calls only the
# second. registry.* depends on util.*, which each case edits.
_PY_FLOW = {
    "util.py": "def log(msg):\n    print(msg)\n",
    "registry.py": (
        "from util import log\n\n\n"
        "def run_callback(fn):\n"
        '    log("running")\n'
        "    return fn()\n\n\n"
        "def describe():\n"
        '    return "registry"\n'
    ),
    "plugins.py": (
        "from registry import run_callback\n\n\n"
        "def _on_start():\n"
        '    return "started"\n\n\n'
        "def main():\n"
        "    return run_callback(_on_start)\n"
    ),
    "reader.py": (
        "from registry import describe\n\n\ndef show():\n    return describe()\n"
    ),
}
_JS_FLOW = {
    "util.js": "export function log(msg) {\n  console.log(msg);\n}\n",
    "registry.js": (
        "import { log } from './util';\n\n"
        "export function runCallback(fn) {\n"
        "  log('running');\n"
        "  return fn();\n"
        "}\n\n"
        "export function describe() {\n  return 'registry';\n}\n"
    ),
    "plugins.js": (
        "import { runCallback } from './registry';\n\n"
        "function onStart() {\n  return 1;\n}\n\n"
        "export function main() {\n  return runCallback(onStart);\n}\n"
    ),
    "reader.js": (
        "import { describe } from './registry';\n\n"
        "export function show() {\n  return describe();\n}\n"
    ),
}
_GO_FLOW = {
    "go.mod": _GO_MOD,
    "util/util.go": "package util\n\nfunc Log(msg string) {}\n",
    "registry/registry.go": (
        'package registry\n\nimport "example.com/m/util"\n\n'
        "func RunCallback(fn func() int) int {\n"
        '\tutil.Log("running")\n'
        "\treturn fn()\n}\n\n"
        'func Describe() string {\n\treturn "registry"\n}\n'
    ),
    "plugins/plugins.go": (
        'package plugins\n\nimport "example.com/m/registry"\n\n'
        "func onStart() int {\n\treturn 1\n}\n\n"
        "func Main() int {\n\treturn registry.RunCallback(onStart)\n}\n"
    ),
    "reader/reader.go": (
        'package reader\n\nimport "example.com/m/registry"\n\n'
        "func Show() string {\n\treturn registry.Describe()\n}\n"
    ),
}

FLOW_CASES = [
    (
        "python",
        _PY_FLOW,
        {"util.py": _PY_FLOW["util.py"].replace("(msg)", "(msg, level=0)", 1)},
        {"util.py", "registry.py", "plugins.py"},
    ),
    (
        "javascript",
        _JS_FLOW,
        {"util.js": _JS_FLOW["util.js"].replace("(msg)", "(msg, level)", 1)},
        {"util.js", "registry.js", "plugins.js"},
    ),
    (
        "go",
        _GO_FLOW,
        {"util/util.go": _GO_FLOW["util/util.go"] + "// edited\n"},
        {"util/util.go", "registry/registry.go", "plugins/plugins.go"},
    ),
]


@pytest.fixture
def treesitter_go(monkeypatch: pytest.MonkeyPatch) -> None:
    # The re-parse set is the same on every frontend; the tree-sitter path
    # needs no Go toolchain.
    if cs.SupportedLanguage.GO not in load_parsers()[0]:
        pytest.skip("go parser not available")
    monkeypatch.setattr(settings, "GO_FRONTEND", cs.GoFrontend.TREESITTER)


@pytest.mark.usefixtures("treesitter_go")
@pytest.mark.parametrize(
    ("files", "edits", "reparsed"),
    [case[1:] for case in FLOW_CASES],
    ids=[case[0] for case in FLOW_CASES],
)
def test_a_flow_holder_brings_in_only_the_callers_of_its_invoking_functions(
    temp_repo: Path,
    files: dict[str, str],
    edits: dict[str, str],
    reparsed: set[str],
) -> None:
    # The holder re-parses as a dependent of the edited file, which drops its
    # `run_callback -> _on_start` edge until the passer's walk writes it
    # again. The reader calls only `describe`, which records no binding, and
    # keeps its edges as any unchanged caller does.
    root = temp_repo / PROJECT
    got, synced = _sync(root, files, edits)

    assert got == reparsed
    clean = _clean(root)
    assert synced == clean, _diff(synced, clean)


def test_flow_callers_are_the_callers_of_a_flow_function_or_a_closure() -> None:
    rows: list[ResultRow] = [
        {
            cs.KEY_CALLER_PATH: "plugins.py",
            cs.KEY_TARGET_PATH: "registry.py",
            cs.KEY_TARGET_NAME: "run_callback",
        },
        {
            cs.KEY_CALLER_PATH: "reader.py",
            cs.KEY_TARGET_PATH: "registry.py",
            cs.KEY_TARGET_NAME: "describe",
        },
        # The same name in a file that does not hold it as a flow function.
        {
            cs.KEY_CALLER_PATH: "other.py",
            cs.KEY_TARGET_PATH: "plain.py",
            cs.KEY_TARGET_NAME: "run_callback",
        },
        # A closure, named by its position.
        {
            cs.KEY_CALLER_PATH: "alias.js",
            cs.KEY_TARGET_PATH: "make.js",
            cs.KEY_TARGET_NAME: "anonymous_2_9",
        },
        # A C++ definition named with its scope.
        {
            cs.KEY_CALLER_PATH: "plugins.cpp",
            cs.KEY_TARGET_PATH: "registry.cpp",
            cs.KEY_TARGET_NAME: "Registry::run",
        },
        {cs.KEY_CALLER_PATH: "nameless.py", cs.KEY_TARGET_PATH: "registry.py"},
    ]
    holders = {
        "registry.py": {"run_callback"},
        "plain.py": {"helper"},
        "make.js": {"make"},
        "registry.cpp": {"run"},
    }

    assert flow_caller_paths(rows, holders) == [
        "alias.js",
        "plugins.cpp",
        "plugins.py",
    ]


# box.go declares Box, whose Grow box_extra.go declares; tag.go and
# tag_methods.go are such a pair too, and other.go stands alone. Only box.go
# and box_extra.go call into util.
_GO_PACKAGE = {
    "go.mod": _GO_MOD,
    "util/util.go": (
        "package util\n\nfunc Clamp(n int) int {\n\tif n < 0 {\n\t\treturn 0\n\t}\n"
        "\treturn n\n}\n\nfunc Double(n int) int {\n\treturn n * 2\n}\n"
    ),
    "box/box.go": (
        'package box\n\nimport "example.com/m/util"\n\n'
        "type Box struct{ n int }\n\n"
        "func (b *Box) Size() int { return util.Clamp(b.n) }\n"
    ),
    "box/box_extra.go": (
        'package box\n\nimport "example.com/m/util"\n\n'
        "func (b *Box) Grow() int { return util.Double(b.n) }\n"
    ),
    "box/tag.go": "package box\n\ntype Tag struct{}\n",
    "box/tag_methods.go": (
        'package box\n\nfunc (t Tag) Label() string { return "t" }\n'
    ),
    "box/other.go": "package box\n\nfunc Other() int { return 1 }\n",
}
_UTIL_COMMENT = {"util/util.go": _GO_PACKAGE["util/util.go"] + "// edited\n"}


@pytest.mark.usefixtures("treesitter_go")
def test_an_unchanged_go_file_reparses_without_the_rest_of_its_package(
    temp_repo: Path,
) -> None:
    root = temp_repo / PROJECT
    got, synced = _sync(root, _GO_PACKAGE, _UTIL_COMMENT)

    assert got == {"util/util.go", "box/box.go", "box/box_extra.go"}
    clean = _clean(root)
    assert synced == clean, _diff(synced, clean)


@pytest.mark.usefixtures("treesitter_go")
def test_a_go_methods_file_reparses_with_the_file_its_type_is_in(
    temp_repo: Path,
) -> None:
    # Grow hangs off box.go's Box, so the re-parse delete of box_extra.go
    # alone cannot reach it: its old CALLS edge into a.A.Run would stay
    # beside the b.B.Run a clean index binds once util.New returns a *b.B.
    files = {
        "go.mod": _GO_MOD,
        "a/a.go": "package a\n\ntype A struct{}\n\nfunc (x *A) Run() int { return 1 }\n",
        "b/b.go": "package b\n\ntype B struct{}\n\nfunc (x *B) Run() int { return 2 }\n",
        "util/util.go": (
            'package util\n\nimport "example.com/m/a"\n\n'
            "func New() *a.A { return &a.A{} }\n"
        ),
        "box/box.go": "package box\n\ntype Box struct{ n int }\n",
        "box/box_extra.go": (
            'package box\n\nimport "example.com/m/util"\n\n'
            "func (b *Box) Grow() int { return util.New().Run() }\n"
        ),
        "box/other.go": "package box\n\nfunc Other() int { return 1 }\n",
    }
    edits = {
        "util/util.go": (
            'package util\n\nimport "example.com/m/b"\n\n'
            "func New() *b.B { return &b.B{} }\n"
        )
    }
    root = temp_repo / PROJECT
    got, synced = _sync(root, files, edits)

    assert got == {"util/util.go", "box/box_extra.go", "box/box.go"}
    clean = _clean(root)
    assert synced == clean, _diff(synced, clean)


@pytest.mark.usefixtures("treesitter_go")
def test_an_edited_go_file_still_reparses_its_whole_package(temp_repo: Path) -> None:
    # Its source changed, so a type or method it declares may now pair with
    # any file beside it, which the graph does not record yet.
    root = temp_repo / PROJECT
    got, synced = _sync(
        root,
        _GO_PACKAGE,
        {"box/other.go": _GO_PACKAGE["box/other.go"] + "// edited\n"},
    )

    assert got == {path for path in _GO_PACKAGE if path.startswith("box/")}
    clean = _clean(root)
    assert synced == clean, _diff(synced, clean)


def _count_flow_scans(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    scans: dict[str, int] = {}
    original = GraphUpdater._file_flow_functions

    def counted(
        self: GraphUpdater, filepath: Path, file_bytes: bytes
    ) -> FileFlowFunctions | None:
        key = filepath.name
        scans[key] = scans.get(key, 0) + 1
        return original(self, filepath, file_bytes)

    monkeypatch.setattr(GraphUpdater, "_file_flow_functions", counted)
    return scans


@pytest.mark.usefixtures("treesitter_go")
def test_a_full_build_scans_no_file_for_callable_flow(
    temp_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Every walked file re-parses on a full build, so the closure could only
    # queue files already queued.
    root = temp_repo / PROJECT
    _materialise(root, _GO_FLOW)
    scans = _count_flow_scans(monkeypatch)

    _clean(root)

    assert scans == {}


@pytest.mark.usefixtures("treesitter_go")
def test_the_flow_closure_scans_each_reparsed_file_once(
    temp_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Only box_extra.go calls into util; box.go joins in the first round for
    # the Box its Grow hangs off, so the closure runs a second round over
    # every re-parsed file.
    root = temp_repo / PROJECT
    files = {
        **_GO_PACKAGE,
        "box/box.go": "package box\n\ntype Box struct{ n int }\n",
    }
    _materialise(root, files)
    store = _StatefulIngestor()
    _updater(store, root).run(force=True)
    _edit_after_cache(root, _UTIL_COMMENT)
    scans = _count_flow_scans(monkeypatch)

    _updater(store, root).run()

    assert set(scans) == {"util.go", "box.go", "box_extra.go"}
    assert set(scans.values()) == {1}, scans


def _embedded_graph(path: Path) -> tuple[set[tuple[str, ...]], set[tuple[str, ...]]]:
    """Every node with its properties and every edge with its properties, as
    the embedded store holds them, keyed by what the store keys them on."""
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        names: dict[int, str] = {}
        nodes: set[tuple[str, ...]] = set()
        for node_id, label, key, props in connection.execute(
            "SELECT id, label, key, props FROM cgr_nodes"
        ):
            names[node_id] = f"{label}:{key}"
            if label != cs.NodeLabel.PROJECT.value:
                nodes.add((label, str(key), props))
        edges = {
            (names[src], kind, names[dst], props)
            for src, dst, kind, props in connection.execute(
                "SELECT src, dst, type, props FROM cgr_edges"
            )
        }
    finally:
        connection.close()
    return nodes, edges


def _embedded_index(root: Path, graph: Path, force: bool) -> set[str]:
    parsers, queries = load_parsers()
    with MemgraphIngestor(
        host="unused",
        port=0,
        dialect=get_dialect(DIALECT_SQLITE),
        sqlite_path=str(graph),
    ) as ingestor:
        ingestor.ensure_constraints()
        updater = GraphUpdater(
            ingestor=ingestor,
            repo_path=root,
            parsers=parsers,
            queries=queries,
            project_name=PROJECT,
        )
        updater.run(force=force)
        return set(updater._reparsed_file_keys)


@pytest.mark.usefixtures("treesitter_go")
@pytest.mark.parametrize(
    ("files", "edits", "reparsed"),
    [
        (
            _GO_PACKAGE,
            _UTIL_COMMENT,
            {"util/util.go", "box/box.go", "box/box_extra.go"},
        ),
        (_GO_FLOW, FLOW_CASES[2][2], FLOW_CASES[2][3]),
    ],
    ids=["member-owners", "flow-callers"],
)
def test_the_embedded_graph_answers_both_lookups_as_the_double_does(
    temp_repo: Path,
    files: dict[str, str],
    edits: dict[str, str],
    reparsed: set[str],
) -> None:
    # The lookups are Cypher on the graph; the cases above run them on the
    # eval double, and here on the store a user's sync reads.
    root = temp_repo / PROJECT
    _materialise(root, files)
    synced, fresh = temp_repo / "synced.sqlite", temp_repo / "fresh.sqlite"
    _embedded_index(root, synced, force=True)
    _edit_after_cache(root, edits)

    assert _embedded_index(root, synced, force=False) == reparsed
    _embedded_index(root, fresh, force=True)
    assert _embedded_graph(synced) == _embedded_graph(fresh)
