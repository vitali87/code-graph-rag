"""Extract-function edit tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag.editing import ExtractRefused, extract
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.tests.extract_inline_helpers import (
    PROJECT,
    REPORT_PY,
    _extract_inline_repo,  # noqa: F401 - pytest fixture
    _index,
    _qn,
    _smoke,
    _write,
)
from evals.cgr_graph import _StatefulIngestor


def test_extract_ten_lines_with_two_inputs_and_one_output_in_python(
    extract_inline_repo: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    root, store, updater = extract_inline_repo
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
    extract_inline_repo: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    root, store, _updater = extract_inline_repo
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
