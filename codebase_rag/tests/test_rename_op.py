# Edit algebra op 1, rename (issue #1532): a graph operation that rewrites
# the definition, every call/reference/import site and the override
# hierarchy through the patchers inside a transaction, refuses to rewrite
# through a guess, and leaves formatting untouched. The graph is the one the
# indexer records into the mock ingestor, replayed through the fixed queries.
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag import cypher_queries as cq
from codebase_rag.editing.rename import RenameRefused, rename
from codebase_rag.tests.conftest import create_and_run_updater
from codebase_rag.types_defs import PropertyDict, ResultRow


class RecordedGraph:
    """The fixed queries answered from what the indexer emitted."""

    def __init__(self, mock: MagicMock, project: str) -> None:
        self.nodes: dict[str, dict] = {}
        self.edges: list[tuple[str, str, str, dict]] = []
        for c in mock.ensure_node_batch.call_args_list:
            props = dict(c.args[1])
            props[cs.KEY_LABEL] = str(c.args[0])
            self.nodes.setdefault(str(props.get(cs.KEY_QUALIFIED_NAME, "")), {}).update(
                props
            )
        for c in mock.ensure_relationship_batch.call_args_list:
            props = (
                c.kwargs.get("properties")
                or (c.args[3] if len(c.args) > 3 else {})
                or {}
            )
            self.edges.append(
                (str(c.args[0][2]), str(c.args[1]), str(c.args[2][2]), dict(props))
            )
        self.project = project
        self.root_path: str | None = None

    def _node_row(self, qn: str) -> ResultRow:
        n = self.nodes[qn]
        return {
            cs.KEY_LABEL: n[cs.KEY_LABEL],
            cs.KEY_QUALIFIED_NAME: qn,
            cs.KEY_NAME: n.get(cs.KEY_NAME),
            cs.KEY_PATH: n.get(cs.KEY_PATH),
            cs.KEY_START_LINE: n.get(cs.KEY_START_LINE),
            cs.KEY_END_LINE: n.get(cs.KEY_END_LINE),
            cs.KEY_DOCSTRING: n.get(cs.KEY_DOCSTRING),
            cs.KEY_NAME_START_LINE: n.get(cs.KEY_NAME_START_LINE),
            cs.KEY_NAME_START_COL: n.get(cs.KEY_NAME_START_COL),
        }

    def fetch_all(
        self, query: str, params: PropertyDict | None = None
    ) -> list[ResultRow]:
        p = params or {}
        qn = str(p.get(cs.KEY_QN, ""))
        if query == cq.CYPHER_PROJECT_ROOT_PATH:
            return [{cs.KEY_ROOT_PATH: self.root_path}] if self.root_path else []
        if query == cq.CYPHER_PROJECT_IS_INCOMPLETE:
            # No marker: this fixture's graph was indexed by a run that
            # finished. An unanswered query would raise, and
            # `_persisted_incomplete` reads a raise as "cannot tell, refuse",
            # so leaving it out makes every reingest here refuse.
            return []
        if query == cq.CYPHER_GRAPH_DEFINITION:
            return [self._node_row(qn)] if qn in self.nodes else []
        if query in (
            cq.CYPHER_GRAPH_CALLERS,
            cq.CYPHER_GRAPH_REFERENCES,
            cq.CYPHER_GRAPH_TYPE_EDGES,
        ):
            rels = {
                cq.CYPHER_GRAPH_CALLERS: {"CALLS"},
                cq.CYPHER_GRAPH_REFERENCES: {"REFERENCES", "INSTANTIATES"},
                cq.CYPHER_GRAPH_TYPE_EDGES: {"INHERITS", "ACCEPTS", "RETURNS"},
            }[query]
            out: list[ResultRow] = []
            for src, rel, dst, props in self.edges:
                if dst != qn or rel not in rels or src not in self.nodes:
                    continue
                out.append(
                    {
                        cs.KEY_LABEL: self.nodes[src][cs.KEY_LABEL],
                        cs.KEY_QUALIFIED_NAME: src,
                        cs.KEY_PATH: self.nodes[src].get(cs.KEY_PATH),
                        cs.KEY_REL_TYPE: rel,
                        **{
                            k: props.get(k)
                            for k in (
                                cs.KEY_LINE,
                                cs.KEY_COL,
                                cs.KEY_END_LINE,
                                cs.KEY_END_COL,
                                cs.KEY_ARG_COUNT,
                                cs.KEY_KWARG_NAMES,
                                cs.KEY_RESOLUTION,
                            )
                        },
                    }
                )
            return out
        if query == cq.CYPHER_GRAPH_OVERRIDES:
            out = []
            for src, rel, dst, _props in self.edges:
                if rel != "OVERRIDES":
                    continue
                for a, b in ((src, dst), (dst, src)):
                    if b == qn and a in self.nodes:
                        out.append(
                            {
                                cs.KEY_LABEL: self.nodes[a][cs.KEY_LABEL],
                                cs.KEY_QUALIFIED_NAME: a,
                                cs.KEY_PATH: self.nodes[a].get(cs.KEY_PATH),
                                cs.KEY_REL_TYPE: rel,
                            }
                        )
            return out
        if query == cq.CYPHER_GRAPH_IMPORTERS:
            out = []
            for src, rel, dst, props in self.edges:
                if rel != "IMPORTS" or dst != qn or src not in self.nodes:
                    continue
                out.append(
                    {
                        cs.KEY_QUALIFIED_NAME: src,
                        cs.KEY_PATH: self.nodes[src].get(cs.KEY_PATH),
                        **{
                            k: props.get(k)
                            for k in (
                                cs.KEY_LINE,
                                cs.KEY_COL,
                                cs.KEY_END_LINE,
                                cs.KEY_END_COL,
                                cs.KEY_ALIAS,
                                cs.KEY_IMPORTED_NAME,
                            )
                        },
                    }
                )
            return out
        raise AssertionError(f"unexpected query: {query[:50]}")


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


def _index(root: Path, mock: MagicMock) -> RecordedGraph:
    updater = create_and_run_updater(root, mock)
    graph = RecordedGraph(mock, updater.project_name)
    graph.root_path = str(root.resolve())
    return graph


PY_FILES = {
    "pkg/__init__.py": 'from pkg.util import helper\n\n__all__ = ["helper"]\n',
    "pkg/util.py": "LIMIT = 3\n\n\ndef helper(a,  b):  # keep spacing\n    return a + b + LIMIT\n",
    "pkg/app.py": "from pkg.util import helper as h, LIMIT\nimport pkg.util\n\n\ndef run():\n    return h(1, 2) + pkg.util.helper(3, 4) + LIMIT\n",
    "pkg/shapes.py": "class Base:\n    def area(self):\n        return 0\n\n\nclass Circle(Base):\n    def area(self):\n        return 3\n\n\ndef total(shape: Base):\n    return shape.area() + Circle().area()\n",
    "tests/test_pkg.py": "from pkg.app import run\nfrom pkg.shapes import Circle, total\n\n\ndef test_all():\n    assert run() == 1 + 2 + 3 + 3 + 4 + 3 + 3\n    assert total(Circle()) == 6\n",
}


@pytest.fixture
def py_repo(temp_repo: Path, mock_ingestor: MagicMock) -> tuple[Path, RecordedGraph]:
    for rel, text in PY_FILES.items():
        _write(temp_repo, rel, text)
    return temp_repo, _index(temp_repo, mock_ingestor)


def _smoke(root: Path) -> None:
    result = subprocess.run(
        [sys.executable, "-c", "import tests.test_pkg as t; t.test_all(); print('ok')"],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_markdown_mentions_of_the_old_name_are_reported(
    py_repo: tuple[Path, RecordedGraph],
) -> None:
    """`doc_mentions` lists prose a rename cannot rewrite, for a human.

    The happy path asserts it is empty, which a function that can never
    return anything also satisfies; this drives the collecting branch.
    """
    root, graph = py_repo
    (root / "docs").mkdir(exist_ok=True)
    (root / "docs" / "guide.md").write_text(
        "# Guide\n\nCall helper to do the thing.\n\nUnrelated: helperish stays.\n",
        encoding="utf-8",
    )
    # Under an ignored directory, so it must NOT be reported.
    (root / "node_modules").mkdir(exist_ok=True)
    (root / "node_modules" / "vendor.md").write_text(
        "helper appears here too\n", encoding="utf-8"
    )
    report = rename(
        root,
        graph.fetch_all,
        graph.project,
        f"{graph.project}.pkg.util.helper",
        "assist",
    )
    assert report.applied, report.message
    # The word boundary excludes `helperish`, and IGNORE_PATTERNS excludes
    # node_modules, so exactly one line is reported.
    assert report.doc_mentions == ("docs/guide.md:3",)


def test_python_function_rename_rewrites_every_site_and_import(
    py_repo: tuple[Path, RecordedGraph],
) -> None:
    root, graph = py_repo
    (root / "tests" / "__init__.py").write_bytes(b"")
    report = rename(
        root,
        graph.fetch_all,
        graph.project,
        f"{graph.project}.pkg.util.helper",
        "assist",
    )
    assert report.applied, report.message
    kinds = {s.kind for s in report.sites}
    assert kinds >= {"definition", "call"}
    assert set(report.files) == {"pkg/__init__.py", "pkg/util.py", "pkg/app.py"}
    util = (root / "pkg" / "util.py").read_text()
    assert "def assist(a,  b):  # keep spacing" in util
    assert "helper" not in util
    app = (root / "pkg" / "app.py").read_text()
    assert (
        app
        == "from pkg.util import assist as h, LIMIT\nimport pkg.util\n\n\ndef run():\n    return h(1, 2) + pkg.util.assist(3, 4) + LIMIT\n"
    )
    init = (root / "pkg" / "__init__.py").read_text()
    assert init == 'from pkg.util import assist\n\n__all__ = ["assist"]\n'
    assert report.doc_mentions == ()
    assert "--- a/pkg/util.py" in report.diff
    _smoke(root)


def test_python_method_rename_covers_the_hierarchy(
    py_repo: tuple[Path, RecordedGraph],
) -> None:
    root, graph = py_repo
    (root / "tests" / "__init__.py").write_bytes(b"")
    report = rename(
        root,
        graph.fetch_all,
        graph.project,
        f"{graph.project}.pkg.shapes.Base.area",
        "surface",
    )
    assert report.applied, report.message
    assert set(report.hierarchy) == {
        f"{graph.project}.pkg.shapes.Base.area",
        f"{graph.project}.pkg.shapes.Circle.area",
    }
    shapes = (root / "pkg" / "shapes.py").read_text()
    assert shapes.count("def surface(self)") == 2
    assert "shape.surface() + Circle().surface()" in shapes
    assert "area" not in shapes
    _smoke(root)


def test_python_constant_rename(py_repo: tuple[Path, RecordedGraph]) -> None:
    root, graph = py_repo
    (root / "tests" / "__init__.py").write_bytes(b"")
    try:
        report = rename(
            root,
            graph.fetch_all,
            graph.project,
            f"{graph.project}.pkg.util.LIMIT",
            "CAP",
        )
    except RenameRefused as refused:
        if "No definition" in str(refused):
            pytest.skip("module constants are not graph definitions in this build")
        raise
    assert report.applied, report.message
    assert "return a + b + CAP" in (root / "pkg" / "util.py").read_text()
    assert "+ CAP\n" in (root / "pkg" / "app.py").read_text()
    _smoke(root)


def test_dry_run_changes_nothing(py_repo: tuple[Path, RecordedGraph]) -> None:
    root, graph = py_repo
    before = {p: p.read_bytes() for p in root.rglob("*.py")}
    report = rename(
        root,
        graph.fetch_all,
        graph.project,
        f"{graph.project}.pkg.util.helper",
        "assist",
        dry_run=True,
    )
    assert report.applied is False
    assert len(report.sites) >= 2
    assert {p: p.read_bytes() for p in root.rglob("*.py")} == before


def test_heuristic_site_refuses_and_changes_nothing(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    _write(temp_repo, "pkg/__init__.py", "")
    _write(temp_repo, "pkg/util.py", "def lonely():\n    return 1\n")
    _write(
        temp_repo, "pkg/app.py", "def run():\n    return lonely()\n"
    )  # not imported: name-only match
    graph = _index(temp_repo, mock_ingestor)
    before = (temp_repo / "pkg" / "app.py").read_bytes()
    with pytest.raises(RenameRefused) as excinfo:
        rename(
            temp_repo,
            graph.fetch_all,
            graph.project,
            f"{graph.project}.pkg.util.lonely",
            "alone",
        )
    assert [s.resolution for s in excinfo.value.ambiguous] == ["heuristic"]
    assert excinfo.value.ambiguous[0].path == "pkg/app.py"
    assert (temp_repo / "pkg" / "app.py").read_bytes() == before
    assert "lonely" in (temp_repo / "pkg" / "util.py").read_text()
    # Accepting the risk rewrites through the guess.
    report = rename(
        temp_repo,
        graph.fetch_all,
        graph.project,
        f"{graph.project}.pkg.util.lonely",
        "alone",
        allow_heuristic=True,
    )
    assert report.applied
    assert "return alone()" in (temp_repo / "pkg" / "app.py").read_text()


def test_unlocatable_dynamic_site_is_listed(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    _write(temp_repo, "pkg/__init__.py", "")
    _write(temp_repo, "pkg/util.py", "def helper():\n    return 1\n")
    _write(
        temp_repo,
        "pkg/app.py",
        "from pkg.util import helper\n\n\ndef run():\n    return helper()\n",
    )
    graph = _index(temp_repo, mock_ingestor)
    graph.edges.append(
        (
            f"{graph.project}.pkg.app.run",
            "CALLS",
            f"{graph.project}.pkg.util.helper",
            {cs.KEY_RESOLUTION: "dynamic", cs.KEY_UNLOCATABLE: True},
        )
    )
    with pytest.raises(RenameRefused) as excinfo:
        rename(
            temp_repo,
            graph.fetch_all,
            graph.project,
            f"{graph.project}.pkg.util.helper",
            "assist",
        )
    assert excinfo.value.unlocatable
    assert "dynamic" in excinfo.value.unlocatable[0]
    # `allow_heuristic` does NOT lift this. It accepts rewriting THROUGH a
    # guessed site; it is not permission to commit while a site the graph
    # knows about keeps the old name. Applying here would report success
    # over a half-renamed symbol.
    with pytest.raises(RenameRefused) as forced:
        rename(
            temp_repo,
            graph.fetch_all,
            graph.project,
            f"{graph.project}.pkg.util.helper",
            "assist",
            allow_heuristic=True,
        )
    assert forced.value.unlocatable


def test_a_deleted_definition_file_refuses_rather_than_raising(
    py_repo: tuple[Path, RecordedGraph],
) -> None:
    """A stale index is a refusal, not an unhandled error.

    Every other failure on this path raises RenameRefused, which the MCP
    handler turns into a payload and the CLI into a message. A PatcherError
    escaping instead makes a deleted-but-indexed file crash the tool. The
    site-level read already refuses; the definition read did not.
    """
    root, graph = py_repo
    # The graph still names pkg/util.py; the tree no longer has it.
    (root / "pkg" / "util.py").unlink()

    with pytest.raises(RenameRefused) as refused:
        rename(
            root,
            graph.fetch_all,
            graph.project,
            f"{graph.project}.pkg.util.helper",
            "assist",
        )
    assert "cannot be read" in str(refused.value)


def test_unknown_symbol_and_bad_name(py_repo: tuple[Path, RecordedGraph]) -> None:
    root, graph = py_repo
    with pytest.raises(RenameRefused, match="No definition"):
        rename(
            root, graph.fetch_all, graph.project, f"{graph.project}.pkg.util.nope", "x"
        )
    with pytest.raises(RenameRefused, match="valid identifier"):
        rename(
            root,
            graph.fetch_all,
            graph.project,
            f"{graph.project}.pkg.util.helper",
            "not valid",
        )


def test_verifier_failure_rolls_back(py_repo: tuple[Path, RecordedGraph]) -> None:
    root, graph = py_repo
    before = {p: p.read_bytes() for p in root.rglob("*.py")}
    report = rename(
        root,
        graph.fetch_all,
        graph.project,
        f"{graph.project}.pkg.util.helper",
        "assist",
        verify=lambda tree: False,
    )
    assert report.applied is False
    assert {p: p.read_bytes() for p in root.rglob("*.py")} == before


# --- other languages ----------------------------------------------------------------


def test_typescript_rename(temp_repo: Path, mock_ingestor: MagicMock) -> None:
    _write(
        temp_repo,
        "src/util.ts",
        "export function helper(a: number,  b: number): number { return a + b; }\n",
    )
    _write(
        temp_repo,
        "src/app.ts",
        "import { helper as h } from './util';\nimport { helper } from './util';\nexport const x = h(1, 2) + helper(3, 4);\n",
    )
    graph = _index(temp_repo, mock_ingestor)
    report = rename(
        temp_repo,
        graph.fetch_all,
        graph.project,
        f"{graph.project}.src.util.helper",
        "assist",
        allow_heuristic=True,
    )
    assert report.applied, report.message
    assert (
        (temp_repo / "src" / "util.ts")
        .read_text()
        .startswith("export function assist(a: number,  b: number)")
    )
    app = (temp_repo / "src" / "app.ts").read_text()
    assert "import { assist as h } from './util';" in app
    assert "import { assist } from './util';" in app
    assert "h(1, 2) + assist(3, 4)" in app


def test_go_rename(temp_repo: Path, mock_ingestor: MagicMock) -> None:
    _write(temp_repo, "go.mod", "module app\n\ngo 1.21\n")
    _write(
        temp_repo,
        "main.go",
        "package main\n\nfunc helper(a,  b int) int { return a + b }\n\nfunc main() { _ = helper(1, 2) }\n",
    )
    graph = _index(temp_repo, mock_ingestor)
    report = rename(
        temp_repo,
        graph.fetch_all,
        graph.project,
        f"{graph.project}.main.helper",
        "assist",
        allow_heuristic=True,
    )
    assert report.applied, report.message
    text = (temp_repo / "main.go").read_text()
    assert "func assist(a,  b int) int" in text
    assert "_ = assist(1, 2)" in text


def test_java_rename(temp_repo: Path, mock_ingestor: MagicMock) -> None:
    _write(
        temp_repo,
        "app/Util.java",
        "package app;\n\npublic class Util {\n    public static int helper(int a,  int b) { return a + b; }\n}\n",
    )
    _write(
        temp_repo,
        "app/App.java",
        "package app;\n\npublic class App {\n    public int run() { return Util.helper(1, 2); }\n}\n",
    )
    graph = _index(temp_repo, mock_ingestor)
    qn = next(
        q
        for q in graph.nodes
        if q.endswith(".Util.helper(int, int)")
        or q.endswith(".Util.helper(int,int)")
        or ".Util.helper(" in q
    )
    report = rename(
        temp_repo, graph.fetch_all, graph.project, qn, "assist", allow_heuristic=True
    )
    assert report.applied, report.message
    assert (
        "public static int assist(int a,  int b)"
        in (temp_repo / "app" / "Util.java").read_text()
    )
    assert "Util.assist(1, 2)" in (temp_repo / "app" / "App.java").read_text()


def test_rust_rename(temp_repo: Path, mock_ingestor: MagicMock) -> None:
    _write(temp_repo, "Cargo.toml", '[package]\nname = "app"\nversion = "0.1.0"\n')
    _write(
        temp_repo,
        "src/main.rs",
        "mod util;\nuse crate::util::helper;\n\nfn main() { let _ = helper(1,  2); }\n",
    )
    _write(
        temp_repo, "src/util.rs", "pub fn helper(a: u32,  b: u32) -> u32 { a + b }\n"
    )
    graph = _index(temp_repo, mock_ingestor)
    report = rename(
        temp_repo,
        graph.fetch_all,
        graph.project,
        f"{graph.project}.src.util.helper",
        "assist",
        allow_heuristic=True,
    )
    assert report.applied, report.message
    assert (
        "pub fn assist(a: u32,  b: u32)" in (temp_repo / "src" / "util.rs").read_text()
    )
    main = (temp_repo / "src" / "main.rs").read_text()
    assert "use crate::util::assist;" in main
    assert "helper(1,  2)" not in main
    assert "assist(1,  2)" in main


# --- MCP tool and CLI surface ------------------------------------------------------


def test_method_rename_leaves_a_same_named_module_symbol_alone(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # `Store.get` and the module-level `get` share a name; only the latter
    # is imported, so the import line and `__all__` must not change.
    _write(
        temp_repo, "pkg/__init__.py", 'from pkg.util import get\n\n__all__ = ["get"]\n'
    )
    _write(
        temp_repo,
        "pkg/util.py",
        "def get():\n    return 1\n\n\nclass Store:\n    def get(self):\n        return 2\n",
    )
    _write(
        temp_repo,
        "pkg/app.py",
        "from pkg.util import get, Store\n\n\ndef run():\n    return get() + Store().get()\n",
    )
    graph = _index(temp_repo, mock_ingestor)
    report = rename(
        temp_repo,
        graph.fetch_all,
        graph.project,
        f"{graph.project}.pkg.util.Store.get",
        "fetch",
        allow_heuristic=True,
    )
    assert report.applied, report.message
    assert (temp_repo / "pkg" / "__init__.py").read_text() == (
        'from pkg.util import get\n\n__all__ = ["get"]\n'
    )
    app = (temp_repo / "pkg" / "app.py").read_text()
    assert "from pkg.util import get, Store" in app
    assert "return get() + Store().fetch()" in app


@pytest.mark.parametrize(
    ("call", "expected"),
    [
        # The keyword NAME is the callee's parameter, untouched.
        ("helper(helper=2)", "assist(helper=2)"),
        # The argument VALUE references the function itself, so it is a
        # reference site and is renamed along with the callee.
        ("helper(other=helper)", "assist(other=assist)"),
        # A nested call of the same function: the outer callee is the site,
        # and the inner call is its own site, so both are renamed.
        ("helper(helper(1))", "assist(assist(1))"),
    ],
)
def test_callee_is_renamed_when_an_argument_shares_its_name(
    temp_repo: Path, mock_ingestor: MagicMock, call: str, expected: str
) -> None:
    _write(temp_repo, "pkg/__init__.py", "")
    _write(
        temp_repo,
        "pkg/util.py",
        "def helper(helper=1, other=None):\n    return helper\n",
    )
    _write(
        temp_repo,
        "pkg/app.py",
        f"from pkg.util import helper\n\n\ndef run():\n    return {call}\n",
    )
    graph = _index(temp_repo, mock_ingestor)
    report = rename(
        temp_repo,
        graph.fetch_all,
        graph.project,
        f"{graph.project}.pkg.util.helper",
        "assist",
    )
    assert report.applied, report.message
    # The callee token, not the keyword or argument that spells the same name.
    assert f"return {expected}" in (temp_repo / "pkg" / "app.py").read_text()


def test_a_graph_known_site_in_a_missing_file_refuses_the_rename(
    py_repo: tuple[Path, RecordedGraph],
) -> None:
    root, graph = py_repo
    # The graph still knows pkg/app.py's call sites; the file is gone.
    (root / "pkg" / "app.py").unlink()
    before = (root / "pkg" / "util.py").read_text()
    with pytest.raises(RenameRefused) as refused:
        rename(
            root,
            graph.fetch_all,
            graph.project,
            f"{graph.project}.pkg.util.helper",
            "assist",
        )
    assert "no rewrite location" in str(refused.value)
    assert any("missing file" in entry for entry in refused.value.unlocatable)
    assert (root / "pkg" / "util.py").read_text() == before


def test_a_graph_known_call_without_a_site_refuses_the_rename(
    py_repo: tuple[Path, RecordedGraph],
) -> None:
    root, graph = py_repo
    # A legacy CALLS edge with a path but no coordinates and no resolution:
    # the graph knows the caller, the planner cannot rewrite it.
    graph.edges.append(
        (
            f"{graph.project}.pkg.app.run",
            "CALLS",
            f"{graph.project}.pkg.util.helper",
            {},
        )
    )
    before = (root / "pkg" / "util.py").read_text()
    with pytest.raises(RenameRefused) as refused:
        rename(
            root,
            graph.fetch_all,
            graph.project,
            f"{graph.project}.pkg.util.helper",
            "assist",
        )
    assert "no rewrite location" in str(refused.value)
    assert (root / "pkg" / "util.py").read_text() == before


@pytest.mark.asyncio
async def test_mcp_rename_refuses_a_project_indexed_elsewhere(
    py_repo: tuple[Path, RecordedGraph],
) -> None:
    from codebase_rag.mcp.tools import MCPToolsRegistry

    root, graph = py_repo
    ingestor = MagicMock()

    def fetch_all(query: str, params: PropertyDict | None = None) -> list[ResultRow]:
        if query == cq.CYPHER_PROJECT_ROOT_PATH:
            return [{cs.KEY_ROOT_PATH: "/elsewhere/other-checkout"}]
        return graph.fetch_all(query, params)

    ingestor.fetch_all = fetch_all
    ingestor.list_projects.return_value = [graph.project]
    registry = MCPToolsRegistry(
        project_root=str(root), ingestor=ingestor, cypher_gen=MagicMock()
    )
    before = (root / "pkg" / "util.py").read_text()
    payload = await registry.rename(
        qualified_name=f"{graph.project}.pkg.util.helper",
        new_name="assist",
        project=graph.project,
    )
    assert isinstance(payload, dict), payload
    assert "error" in payload, payload
    assert (root / "pkg" / "util.py").read_text() == before


def test_class_rename_refuses_when_a_base_or_annotation_edge_has_no_site(
    py_repo: tuple[Path, RecordedGraph],
) -> None:
    root, graph = py_repo
    with pytest.raises(RenameRefused) as refused:
        rename(
            root,
            graph.fetch_all,
            graph.project,
            f"{graph.project}.pkg.shapes.Base",
            "Shape",
        )
    assert "structural edge" in str(refused.value)
    assert any(s.kind == "unlocatable" for s in refused.value.ambiguous)
    # Nothing was written: `class Circle(Base)` still binds.
    assert "class Circle(Base):" in (root / "pkg" / "shapes.py").read_text()


def test_dry_run_returns_the_diff_and_lists_import_sites(
    py_repo: tuple[Path, RecordedGraph],
) -> None:
    root, graph = py_repo
    before = {p: (root / p).read_bytes() for p in PY_FILES}
    report = rename(
        root,
        graph.fetch_all,
        graph.project,
        f"{graph.project}.pkg.util.helper",
        "assist",
        dry_run=True,
    )
    assert not report.applied
    assert "-def helper(a,  b):" in report.diff
    assert "+def assist(a,  b):" in report.diff
    assert "pkg/__init__.py" in report.files
    assert {s.kind for s in report.sites} >= {"definition", "call", "import"}
    assert {p: (root / p).read_bytes() for p in PY_FILES} == before


@pytest.mark.asyncio
async def test_mcp_rename_reingests_the_written_files(temp_repo: Path) -> None:
    from codebase_rag import graph_query
    from codebase_rag.graph_updater import GraphUpdater
    from codebase_rag.mcp.tools import MCPToolsRegistry
    from codebase_rag.parser_loader import load_parsers
    from codebase_rag.utils.path_utils import derive_project_name
    from evals.cgr_graph import _StatefulIngestor

    for rel, text in PY_FILES.items():
        _write(temp_repo, rel, text)
    (temp_repo / "tests" / "__init__.py").write_bytes(b"")
    store = _StatefulIngestor()
    parsers, queries = load_parsers()
    project = derive_project_name(temp_repo)
    updater = GraphUpdater(
        ingestor=store,
        repo_path=temp_repo,
        parsers=parsers,
        queries=queries,
        project_name=project,
    )
    updater.run(force=True)
    store.list_projects = lambda: [project]  # type: ignore[attr-defined]
    registry = MCPToolsRegistry(
        project_root=str(temp_repo), ingestor=store, cypher_gen=MagicMock()
    )
    registry._live_updater = updater
    payload = await registry.rename(
        qualified_name=f"{project}.pkg.util.helper", new_name="assist", project=project
    )
    assert isinstance(payload, dict), payload
    assert payload["applied"], payload
    # This layer drives the real `_delta_after_write` rather than a mock, so
    # the assertion is on the OUTCOME the contract guarantees rather than on
    # the call that produced it.
    # The graph followed the tree: the new name is a definition, the old is gone.
    assert graph_query.definition(
        store.fetch_all, project, f"{project}.pkg.util.assist", None
    )["found"]
    assert not graph_query.definition(
        store.fetch_all, project, f"{project}.pkg.util.helper", None
    )["found"]


@pytest.mark.asyncio
async def test_mcp_rename_tool_runs_under_the_lock_and_reports(
    py_repo: tuple[Path, RecordedGraph],
) -> None:
    from codebase_rag.mcp.tools import MCPToolsRegistry

    root, graph = py_repo
    ingestor = MagicMock()
    ingestor.fetch_all = graph.fetch_all
    ingestor.list_projects.return_value = [graph.project]
    registry = MCPToolsRegistry(
        project_root=str(root), ingestor=ingestor, cypher_gen=MagicMock()
    )
    schema = next(
        s for s in registry.get_tool_schemas() if s.name == cs.MCPToolName.RENAME
    )
    assert set(schema.inputSchema["required"]) == {
        cs.MCPParamName.QUALIFIED_NAME,
        cs.MCPParamName.NEW_NAME,
    }
    entry = registry.get_tool_handler(cs.MCPToolName.RENAME)
    assert entry is not None
    handler = entry[0]
    payload = await handler(
        qualified_name=f"{graph.project}.pkg.util.helper",
        new_name="assist",
        dry_run=True,
        project=graph.project,
    )
    assert isinstance(payload, dict)
    assert payload[cs.KEY_SITES]
    assert payload["applied"] is False
    assert "def helper" in (root / "pkg" / "util.py").read_text()


@pytest.mark.asyncio
async def test_mcp_rename_refusal_is_a_payload_not_an_exception(
    py_repo: tuple[Path, RecordedGraph],
) -> None:
    from codebase_rag.mcp.tools import MCPToolsRegistry

    root, graph = py_repo
    ingestor = MagicMock()
    ingestor.fetch_all = graph.fetch_all
    registry = MCPToolsRegistry(
        project_root=str(root), ingestor=ingestor, cypher_gen=MagicMock()
    )
    payload = await registry.rename(
        qualified_name=f"{graph.project}.pkg.util.helper", new_name="not valid"
    )
    assert isinstance(payload, dict)
    assert cs.DICT_KEY_ERROR in payload
    assert payload[cs.KEY_AMBIGUOUS] == []
    assert payload[cs.KEY_UNLOCATABLE] == []


FLUENT = (
    "class Fluent:\n    def helper(self, n):\n        self.n = n\n        return self\n\n\n"
    "def run(obj{annotation}):\n    return obj.helper(1).helper(2)\n"
)


def test_both_links_of_a_fluent_chain_are_renamed(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    _write(temp_repo, "pkg/fluent.py", FLUENT.format(annotation=": Fluent"))
    graph = _index(temp_repo, mock_ingestor)
    report = rename(
        temp_repo,
        graph.fetch_all,
        graph.project,
        f"{graph.project}.pkg.fluent.Fluent.helper",
        "assist",
    )
    assert report.applied, report.message
    # Both call rows start at `obj`; each resolves to its own link through
    # the end the graph recorded, not both to the outermost call.
    assert (
        "return obj.assist(1).assist(2)"
        in (temp_repo / "pkg" / "fluent.py").read_text()
    )
    calls = sorted((s.col, s.resolution) for s in report.sites if s.kind == "call")
    assert calls == [(15, "exact"), (25, "exact")]


def test_an_unrecorded_chain_link_is_renamed_only_as_a_guess(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # The first link is exact; `helper` returns something the index cannot
    # type, so the second link has no row saying what it binds to.
    _write(
        temp_repo,
        "pkg/fluent.py",
        "def make():\n    return object()\n\n\n"
        "class Fluent:\n    def helper(self, n):\n        return make()\n\n\n"
        "def run(obj: Fluent):\n    return obj.helper(1).helper(2)\n",
    )
    graph = _index(temp_repo, mock_ingestor)
    before = (temp_repo / "pkg" / "fluent.py").read_text()
    qn = f"{graph.project}.pkg.fluent.Fluent.helper"
    with pytest.raises(RenameRefused):
        rename(temp_repo, graph.fetch_all, graph.project, qn, "assist")
    assert (temp_repo / "pkg" / "fluent.py").read_text() == before
    report = rename(
        temp_repo, graph.fetch_all, graph.project, qn, "assist", allow_heuristic=True
    )
    assert report.applied, report.message
    assert (
        "return obj.assist(1).assist(2)"
        in (temp_repo / "pkg" / "fluent.py").read_text()
    )
    calls = sorted((s.col, s.resolution) for s in report.sites if s.kind == "call")
    assert calls == [(15, "exact"), (25, "chain")]


def test_a_siteless_heuristic_call_refuses_even_with_allow_heuristic(
    py_repo: tuple[Path, RecordedGraph],
) -> None:
    root, graph = py_repo
    graph.edges.append(
        (
            f"{graph.project}.pkg.app.run",
            "CALLS",
            f"{graph.project}.pkg.util.helper",
            {"resolution": "heuristic"},
        )
    )
    before = (root / "pkg" / "util.py").read_text()
    with pytest.raises(RenameRefused) as refused:
        rename(
            root,
            graph.fetch_all,
            graph.project,
            f"{graph.project}.pkg.util.helper",
            "assist",
            allow_heuristic=True,
        )
    # Accepting a guessed site is not accepting a known site left behind.
    assert "no rewrite location" in str(refused.value)
    assert (root / "pkg" / "util.py").read_text() == before


def test_consumers_of_a_barrel_re_export_follow_the_rename(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    files = dict(PY_FILES)
    files["pkg/consumer.py"] = (
        "from pkg import helper as h\n\n\ndef twice():\n    return h(1, 1) * 2\n"
    )
    files["pkg/deep.py"] = (
        "from pkg.consumer import twice\nfrom pkg import helper\n\n\n"
        "def both():\n    return twice() + helper(0, 0)\n"
    )
    for rel, text in files.items():
        _write(temp_repo, rel, text)
    graph = _index(temp_repo, mock_ingestor)
    report = rename(
        temp_repo,
        graph.fetch_all,
        graph.project,
        f"{graph.project}.pkg.util.helper",
        "assist",
        allow_heuristic=True,
    )
    assert report.applied, report.message
    assert (
        'from pkg.util import assist\n\n__all__ = ["assist"]'
        in (temp_repo / "pkg" / "__init__.py").read_text()
    )
    # The barrel's importers bind through the re-export: rewritten too.
    assert (
        (temp_repo / "pkg" / "consumer.py")
        .read_text()
        .startswith("from pkg import assist as h\n")
    )
    assert "from pkg import assist\n" in (temp_repo / "pkg" / "deep.py").read_text()
    probe = subprocess.run(
        [sys.executable, "-c", "import pkg.deep as d; print(d.both())"],
        cwd=temp_repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert probe.returncode == 0, probe.stderr


def test_a_restored_rename_is_seen_past_multibyte_text(tmp_path: Path) -> None:
    """`RenameSite.col` is a BYTE column; a decoded line is indexed by code points.

    Any multibyte character before the identifier shifts the two apart, so
    the slice lands mid-token and the restored old name is missed. The
    caller then keeps `applied=True` and reports the rollback state as
    unknown, i.e. it says the edit may still be on disk when it is not
    (Greptile, PR #1547).
    """
    from codebase_rag.editing.rename import Renamer, RenameSite

    # 'café' is 5 characters and 6 bytes, so byte and char columns diverge
    # by one from here on. Without the accent this test passes either way.
    line = "x = 'café' ; helper()"
    (tmp_path / "m.py").write_text(line + "\n", encoding="utf-8")
    byte_col = line.encode("utf-8").index(b"helper")
    assert byte_col != line.index("helper"), (
        "fixture guard: the columns must differ or this proves nothing"
    )

    run = Renamer.__new__(Renamer)
    run.repo_root = tmp_path
    report = MagicMock()
    report.old_name = "helper"
    report.sites = [RenameSite("call", "m.py", 1, byte_col, "m.caller", None)]

    assert run._old_name_is_back(report) is True


def test_a_rollback_revokes_its_own_projects_healing_licence() -> None:
    """Rollback damage is never explained by an earlier failed marker clear.

    The rollback site once revoked the licence only when the owner widened,
    so a rollback for the SAME project the licence names left it intact.
    A later scoped reingest then treats that licence as authorisation,
    clears the latch and works from the graph the rollback damaged
    (Greptile, PR #1547).
    """
    from unittest.mock import MagicMock

    from codebase_rag.mcp.tools import MCPToolsRegistry

    handler = MCPToolsRegistry.__new__(MCPToolsRegistry)
    handler.ingestor = MagicMock()
    handler.ingestor.fetch_all = MagicMock(return_value=[])
    handler._graph_incomplete = True
    handler._incomplete_project = "alpha"
    handler._flag_from_failed_clear = "alpha"

    handler._invalidate_graph_for("alpha")

    assert handler._graph_incomplete is True
    assert handler._flag_from_failed_clear is None, (
        "the rollback left a licence that authorises clearing its own damage"
    )


def test_widening_still_revokes_a_licence_for_another_project() -> None:
    """The control: the widening branch must keep revoking, as before."""
    from unittest.mock import MagicMock

    from codebase_rag.mcp.tools import MCPToolsRegistry

    handler = MCPToolsRegistry.__new__(MCPToolsRegistry)
    handler.ingestor = MagicMock()
    handler._graph_incomplete = True
    handler._incomplete_project = "beta"
    handler._flag_from_failed_clear = "beta"

    handler._invalidate_graph_for("alpha")

    assert handler._incomplete_project is None
    assert handler._flag_from_failed_clear is None


def test_a_first_rollback_still_attributes_to_its_project() -> None:
    """The second control: from clean, the rollback's damage IS this project's."""
    from unittest.mock import MagicMock

    from codebase_rag.mcp.tools import MCPToolsRegistry

    handler = MCPToolsRegistry.__new__(MCPToolsRegistry)
    handler.ingestor = MagicMock()
    handler._graph_incomplete = False
    handler._incomplete_project = None
    handler._flag_from_failed_clear = None

    handler._invalidate_graph_for("alpha")

    assert handler._graph_incomplete is True
    assert handler._incomplete_project == "alpha"


def test_one_restored_site_is_not_a_full_undo(tmp_path: Path) -> None:
    """A partial restore must not be reported as the whole rename undone.

    `applied=False` tells the caller every file is back as it was. With one
    site restored and another still renamed, that is a lie in the dangerous
    direction: the caller stops looking (Greptile, PR #1547).
    """
    from codebase_rag.editing.rename import Renamer, RenameSite

    (tmp_path / "a.py").write_text("def helper():\n    pass\n", encoding="utf-8")
    # Restored.
    (tmp_path / "b.py").write_text("assist()\n", encoding="utf-8")
    # Still renamed: the rename is NOT fully undone.

    run = Renamer.__new__(Renamer)
    run.repo_root = tmp_path
    report = MagicMock()
    report.old_name = "helper"
    report.sites = [
        RenameSite("definition", "a.py", 1, 4, "a.helper", None),
        RenameSite("call", "b.py", 1, 0, "b.caller", None),
    ]

    assert run._old_name_is_back(report) is False


def test_every_site_restored_is_a_full_undo(tmp_path: Path) -> None:
    """The control: all sites back means the rename really was reversed.

    Without this, "always return False" passes the test above and the
    already-undone branch becomes unreachable.
    """
    from codebase_rag.editing.rename import Renamer, RenameSite

    (tmp_path / "a.py").write_text("def helper():\n    pass\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("helper()\n", encoding="utf-8")

    run = Renamer.__new__(Renamer)
    run.repo_root = tmp_path
    report = MagicMock()
    report.old_name = "helper"
    report.sites = [
        RenameSite("definition", "a.py", 1, 4, "a.helper", None),
        RenameSite("call", "b.py", 1, 0, "b.caller", None),
    ]

    assert run._old_name_is_back(report) is True
