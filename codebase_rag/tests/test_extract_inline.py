# extract and inline (issue #1535): a span of whole statements becomes a new
# function with the span's inputs as parameters and its outputs returned,
# and a single-return wrapper is substituted at each call site and removed.
# The graph is the in-memory stateful ingestor over a real index.

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.editing import ExtractRefused, InlineRefused, extract, inline
from codebase_rag.editing.transaction import load_history
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from evals.cgr_graph import _StatefulIngestor

PROJECT = "extract_fixture"

REPORT_PY = """\
def build(items, factor):
    header = "report"
    total = 0
    count = 0
    for item in items:
        if item is None:
            continue
        scaled = item * factor
        total += scaled
        count += 1
    average = total / count if count else 0
    lines = [header]
    lines.append(f"total={total}")
    lines.append(f"average={average}")
    return "\\n".join(lines)
"""

FIXTURE: dict[str, str] = {
    "pkg/__init__.py": "",
    "pkg/report.py": REPORT_PY,
    "pkg/util.py": (
        "def wrapper(a, b=1):\n    return a * b + 1\n\n\ndef other():\n    return 2\n"
    ),
    "pkg/app.py": (
        "from pkg.util import wrapper, other\n\n\n"
        "def one():\n    return wrapper(2)\n\n\n"
        "def two(x):\n    return wrapper(x + 1, b=3)\n\n\n"
        "def three():\n    return wrapper(other(), 2) + other()\n"
    ),
    "tests/__init__.py": "",
    "tests/test_app.py": (
        "from pkg.app import one, three, two\n"
        "from pkg.report import build\n\n\n"
        "def test_calls():\n    assert (one(), two(1), three()) == (3, 7, 7)\n\n\n"
        "def test_build():\n"
        "    assert build([1, None, 3], 2) == 'report\\ntotal=8\\naverage=4.0'\n"
    ),
}


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _index(root: Path) -> tuple[_StatefulIngestor, GraphUpdater]:
    parsers, queries = load_parsers()
    store = _StatefulIngestor()
    updater = GraphUpdater(
        ingestor=store,
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name=PROJECT,
    )
    updater.run(force=True)
    return store, updater


@pytest.fixture
def repo(temp_repo: Path) -> tuple[Path, _StatefulIngestor, GraphUpdater]:
    root = temp_repo / PROJECT
    root.mkdir()
    for rel, text in FIXTURE.items():
        _write(root, rel, text)
    store, updater = _index(root)
    return root, store, updater


def _qn(rel: str) -> str:
    return f"{PROJECT}.{rel}"


def _smoke(root: Path) -> None:
    subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests"],
        cwd=root,
        check=True,
        capture_output=True,
    )


# --- extract ---------------------------------------------------------------------


def test_extract_ten_lines_with_two_inputs_and_one_output_in_python(
    repo: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    root, store, updater = repo
    # Lines 3-12: the accumulation. It reads `items` and `factor` from the
    # parameters and binds `total`, `count`, `average`; only `total` and
    # `average` are read afterwards.
    report = extract(
        root,
        store.fetch_all,
        PROJECT,
        _qn("pkg.report.build"),
        (3, 11),
        "accumulate",
        reingest=updater.reingest,
    )
    assert report.applied, report.message
    assert report.inputs == ("items", "factor")
    assert report.outputs == ("total", "average")
    text = (root / "pkg/report.py").read_text()
    assert (
        'def build(items, factor):\n    header = "report"\n'
        "    total, average = accumulate(items, factor)\n    lines = [header]\n"
    ) in text
    assert (
        "\n\ndef accumulate(items, factor):\n    total = 0\n    count = 0\n"
        "    for item in items:\n        if item is None:\n            continue\n"
        "        scaled = item * factor\n        total += scaled\n        count += 1\n"
        "    average = total / count if count else 0\n    return total, average\n"
    ) in text
    assert report.new_qualified_name == _qn("pkg.report.accumulate")
    assert report.verdict is not None and report.verdict.ok
    _smoke(root)


def test_extract_in_typescript(temp_repo: Path) -> None:
    root = temp_repo / PROJECT
    root.mkdir()
    _write(
        root,
        "src/report.ts",
        "export function build(items: number[], factor: number): string {\n"
        "  const header = 'report';\n"
        "  let total = 0;\n"
        "  let count = 0;\n"
        "  for (const item of items) {\n"
        "    const scaled = item * factor;\n"
        "    total += scaled;\n"
        "    count += 1;\n"
        "  }\n"
        "  const average = count ? total / count : 0;\n"
        "  return `${header} ${total} ${average}`;\n"
        "}\n",
    )
    store, updater = _index(root)
    report = extract(
        root,
        store.fetch_all,
        PROJECT,
        _qn("src.report.build"),
        (3, 10),
        "accumulate",
        reingest=updater.reingest,
    )
    assert report.applied, report.message
    assert report.inputs == ("items", "factor")
    assert report.outputs == ("total", "average")
    text = (root / "src/report.ts").read_text()
    assert "  const { total, average } = accumulate(items, factor);\n" in text
    assert (
        "function accumulate(items: number[], factor: number) {\n"
        "  let total = 0;\n  let count = 0;\n"
        "  for (const item of items) {\n    const scaled = item * factor;\n"
        "    total += scaled;\n    count += 1;\n  }\n"
        "  const average = count ? total / count : 0;\n"
        "  return { total, average };\n}\n"
    ) in text


def test_extract_refuses_early_exits_and_split_statements(
    repo: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    root, store, _updater = repo
    # Lines 5-10 hold the loop with its `continue`: fine as a whole (the
    # continue targets the loop inside the span)... but a span holding the
    # `return` cannot be one call.
    with pytest.raises(ExtractRefused, match="leaves the function early"):
        extract(
            root,
            store.fetch_all,
            PROJECT,
            _qn("pkg.report.build"),
            (12, 15),
            "tail",
            dry_run=True,
        )
    with pytest.raises(ExtractRefused, match="cuts through the statement"):
        extract(
            root,
            store.fetch_all,
            PROJECT,
            _qn("pkg.report.build"),
            (3, 6),
            "part",
            dry_run=True,
        )
    with pytest.raises(ExtractRefused, match="No statement"):
        extract(
            root,
            store.fetch_all,
            PROJECT,
            _qn("pkg.report.build"),
            (40, 45),
            "none",
            dry_run=True,
        )
    with pytest.raises(ExtractRefused, match="No definition"):
        extract(
            root,
            store.fetch_all,
            PROJECT,
            _qn("pkg.report.nothing"),
            (3, 4),
            "x",
            dry_run=True,
        )
    assert (root / "pkg/report.py").read_text() == REPORT_PY


def test_extract_from_a_method_makes_a_method(temp_repo: Path) -> None:
    root = temp_repo / PROJECT
    root.mkdir()
    _write(root, "pkg/__init__.py", "")
    _write(
        root,
        "pkg/store.py",
        "class Store:\n    def __init__(self):\n        self.items = [1, 2]\n\n"
        "    def total(self, factor):\n        result = 0\n"
        "        for item in self.items:\n            result += item * factor\n"
        "        return result\n",
    )
    store, updater = _index(root)
    report = extract(
        root,
        store.fetch_all,
        PROJECT,
        _qn("pkg.store.Store.total"),
        (6, 8),
        "sum_scaled",
        reingest=updater.reingest,
    )
    assert report.applied, report.message
    text = (root / "pkg/store.py").read_text()
    assert "        result = self.sum_scaled(factor)\n        return result\n" in text
    assert (
        "    def sum_scaled(self, factor):\n        result = 0\n"
        "        for item in self.items:\n            result += item * factor\n"
        "        return result\n"
    ) in text
    assert report.inputs == ("factor",)


# --- inline ----------------------------------------------------------------------


def test_inline_trivial_wrapper_with_three_callers_removes_the_definition(
    repo: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    root, store, updater = repo
    report = inline(
        root,
        store.fetch_all,
        PROJECT,
        _qn("pkg.util.wrapper"),
        reingest=updater.reingest,
    )
    assert report.applied, report.message
    assert report.sites == ("pkg/app.py:5", "pkg/app.py:9", "pkg/app.py:13")
    assert report.definition_removed
    app = (root / "pkg/app.py").read_text()
    assert app.startswith("from pkg.util import other\n")
    assert "def one():\n    return (2 * 1 + 1)\n" in app
    assert "def two(x):\n    return ((x + 1) * 3 + 1)\n" in app
    assert "def three():\n    return ((other()) * 2 + 1) + other()\n" in app
    util = (root / "pkg/util.py").read_text()
    assert util == "def other():\n    return 2\n"
    assert report.verdict is not None and report.verdict.ok
    _smoke(root)


def test_inline_refuses_guessed_callers_and_multi_statement_bodies(
    repo: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    root, store, _updater = repo
    with pytest.raises(InlineRefused, match="single-return"):
        inline(root, store.fetch_all, PROJECT, _qn("pkg.report.build"), dry_run=True)
    edge = next(
        e
        for e in store.edges
        if e[1] == _qn("pkg.app.two")
        and e[2] == "CALLS"
        and e[4] == _qn("pkg.util.wrapper")
    )
    for site in store.sites_of(edge):
        site[cs.KEY_RESOLUTION] = cs.EdgeResolution.DYNAMIC.value
    with pytest.raises(InlineRefused) as excinfo:
        inline(root, store.fetch_all, PROJECT, _qn("pkg.util.wrapper"), dry_run=True)
    assert excinfo.value.sites == ["pkg/app.py:9"]
    assert (root / "pkg/app.py").read_text() == FIXTURE["pkg/app.py"]
    assert load_history(root) == []


def test_inline_dry_run_writes_nothing(
    repo: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    root, store, _updater = repo
    report = inline(
        root, store.fetch_all, PROJECT, _qn("pkg.util.wrapper"), dry_run=True
    )
    assert not report.applied and len(report.sites) == 3 and report.definition_removed
    assert (root / "pkg/util.py").read_text() == FIXTURE["pkg/util.py"]


async def test_mcp_extract_and_inline_tools(
    repo: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    from unittest.mock import MagicMock

    from codebase_rag.mcp.tools import MCPToolsRegistry

    root, store, updater = repo
    ingestor = MagicMock()
    ingestor.fetch_all = store.fetch_all
    ingestor.list_projects.return_value = [PROJECT]
    registry = MCPToolsRegistry(
        project_root=str(root), ingestor=ingestor, cypher_gen=MagicMock()
    )
    registry._live_updater = updater
    names = {s.name for s in registry.get_tool_schemas()}
    assert {cs.MCPToolName.EXTRACT, cs.MCPToolName.INLINE} <= names
    refused = await registry.extract(
        qualified_name=_qn("pkg.report.build"),
        start_line=12,
        end_line=15,
        new_name="tail",
        project=PROJECT,
    )
    assert isinstance(refused, dict) and cs.DICT_KEY_ERROR in refused
    payload = await registry.inline(
        qualified_name=_qn("pkg.util.wrapper"), project=PROJECT
    )
    assert isinstance(payload, dict) and payload["applied"] is True
    assert payload[cs.KEY_VERDICT]["ok"] is True


# A CRLF file's blank line is b"\r\n", so the loop in `_cut_span` that swallows
# the blank lines after a removed definition tested for b"\n" at a position
# holding b"\r" and stopped immediately, leaving the separator behind. Every
# Windows unit job failed on it while Linux and macOS stayed green, because no
# fixture in this suite used CRLF. Drive `_cut_span` directly on both endings:
# the LF case is the control that proves the assertion is not vacuous.
@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_cut_span_swallows_the_blank_lines_after_a_definition(newline: str) -> None:
    from codebase_rag.editing.move import _cut_span
    from codebase_rag.parser_loader import load_parsers

    parsers, _queries = load_parsers()
    if cs.SupportedLanguage.PYTHON not in parsers:
        pytest.skip("python parser not available")
    lines = [
        "def wrapper():",
        "    return other()",
        "",
        "",
        "def other():",
        "    return 2",
        "",
    ]
    source = newline.join(lines).encode(cs.ENCODING_UTF8)
    tree = parsers[cs.SupportedLanguage.PYTHON].parse(source)
    wrapper = tree.root_node.children[0]

    cut = _cut_span(source, wrapper)

    # What survives the cut is what the file keeps. Assert on that rather than
    # on the offset, so the test states the user-visible outcome.
    remainder = source[cut.end :].decode(cs.ENCODING_UTF8)
    assert remainder.startswith("def other():"), (
        f"blank separator survived the cut for {newline!r}: {remainder[:20]!r}"
    )
