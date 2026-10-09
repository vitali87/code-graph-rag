"""A Go call reaches only Go code, whatever else shares the package directory.

The build-variant fan-out keeps every same-package copy of a Go function
alive (gin's `validate` under `//go:build !nomsgpack` / `nomsgpack`). It
matched copies by directory and name, and a module qn drops its file's
extension, so `gen.py`'s and `preview.js`'s `render` beside `render.go`
looked like build variants too: `main` called all three (`exact` under
go/types) and renaming the Python function rewrote the Go call (#3026).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag import graph_updater as gu
from codebase_rag.editing.rename import rename
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.tests.conftest import create_and_run_updater, get_relationships
from codebase_rag.tests.test_rename_op import _index
from evals.cgr_graph import _StatefulIngestor

_FILES = {
    "go.mod": "module example.com/gomix\n\ngo 1.22\n",
    "tools/main.go": (
        'package main\n\nimport "fmt"\n\n'
        "func main() {\n"
        '\tfmt.Println(render("x"))\n'
        '\tfmt.Println(encode("y"))\n'
        "}\n"
    ),
    "tools/render.go": (
        'package main\n\nfunc render(s string) string {\n\treturn "<" + s + ">"\n}\n'
    ),
    "tools/gen.py": "def render(s):\n    return s.upper()\n",
    "tools/preview.js": (
        "function render(s) {\n  return s;\n}\nmodule.exports = { render };\n"
    ),
    "tools/encode_json.go": (
        "//go:build !msgpack\n\npackage main\n\n"
        'func encode(s string) string {\n\treturn "json:" + s\n}\n'
    ),
    "tools/encode_msgpack.go": (
        "//go:build msgpack\n\npackage main\n\n"
        'func encode(s string) string {\n\treturn "msgpack:" + s\n}\n'
    ),
    "tools/encode.py": "def encode(s):\n    return s\n",
}

_Calls = dict[str, set[str]]


def _write(root: Path) -> None:
    for rel, text in _FILES.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")


def _short(qn: object) -> str:
    return str(qn).split(".tools.", 1)[-1]


@pytest.fixture(autouse=True)
def _treesitter_go(monkeypatch: pytest.MonkeyPatch) -> None:
    # The fan-out runs after either frontend binds the call; pin the one
    # every machine has so the edge set does not depend on the toolchain.
    monkeypatch.setattr(gu.settings, "GO_FRONTEND", cs.GoFrontend.TREESITTER)


def _main_callees(temp_repo: Path) -> set[str]:
    _write(temp_repo)
    mock = MagicMock()
    create_and_run_updater(temp_repo, mock, skip_if_missing="go")
    return {
        _short(c.args[2][2])
        for c in get_relationships(mock, cs.RelationshipType.CALLS)
        if _short(c.args[0][2]) == "main.main"
    }


def test_main_calls_only_the_go_functions(temp_repo: Path) -> None:
    callees = _main_callees(temp_repo)
    assert "render.render" in callees, callees
    assert not callees & {"gen.render", "preview.render", "encode.encode"}, callees


def test_both_build_variants_stay_reached(temp_repo: Path) -> None:
    # Negative: the fan-out still keeps each build-tag copy of `encode` alive.
    callees = _main_callees(temp_repo)
    assert {"encode_json.encode", "encode_msgpack.encode"} <= callees, callees


def test_rename_of_the_python_function_leaves_the_go_call(temp_repo: Path) -> None:
    _write(temp_repo)
    graph = _index(temp_repo, MagicMock())
    report = rename(
        temp_repo,
        graph.fetch_all,
        graph.project,
        f"{graph.project}.tools.gen.render",
        "to_upper",
    )
    assert report.applied, report.message
    assert "def to_upper(s):" in (temp_repo / "tools/gen.py").read_text()
    main_go = (temp_repo / "tools/main.go").read_text()
    assert 'fmt.Println(render("x"))' in main_go, main_go


def test_an_incremental_sync_keeps_to_go(temp_repo: Path) -> None:
    # Re-parsing only main.go leaves the other files to the graph's rows,
    # whose paths still say which language each sibling is.
    parsers, queries = load_parsers()
    if "go" not in parsers:
        pytest.skip("go parser not available")
    _write(temp_repo)
    store = _StatefulIngestor()

    def sync(force: bool) -> set[str]:
        GraphUpdater(
            ingestor=store, repo_path=temp_repo, parsers=parsers, queries=queries
        ).run(force=force)
        return {
            _short(target)
            for _, source, rel, _, target in store.edges
            if rel == cs.RelationshipType.CALLS.value and _short(source) == "main.main"
        }

    sync(force=True)
    main_go = temp_repo / "tools/main.go"
    main_go.write_text(main_go.read_text() + "// touched\n", encoding="utf-8")
    assert sync(force=False) == {
        "render.render",
        "encode_json.encode",
        "encode_msgpack.encode",
    }
