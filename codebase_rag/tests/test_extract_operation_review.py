"""Extract-function regressions raised in review of PR #2062 (and #2060)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from codebase_rag.editing import ExtractRefused, extract
from codebase_rag.tests.extract_inline_helpers import (
    PROJECT,
    REPORT_PY,
    _index,
    _project_qn,
    _write,
)

_NODE = shutil.which("node")


def _repo(temp_repo: Path, files: dict[str, str]) -> Path:
    root = temp_repo / PROJECT
    root.mkdir()
    for rel, text in files.items():
        _write(root, rel, text)
    return root


def _run_node(path: Path) -> str:
    if _NODE is None:
        pytest.skip("node is not installed")
    done = subprocess.run(
        [_NODE, str(path)], capture_output=True, text=True, check=False
    )
    assert done.returncode == 0, done.stderr
    return done.stdout.strip()


# --- callable label (Copilot, PR #2062) -------------------------------------------


def test_extract_refuses_a_class_body(temp_repo: Path) -> None:
    source = "class Config:\n    a = 1\n    b = a + 1\n    c = b * 2\n"
    root = _repo(temp_repo, {"pkg/__init__.py": "", "pkg/config.py": source})
    store, _updater = _index(root)
    with pytest.raises(ExtractRefused, match="not a function or method"):
        extract(
            root,
            store.fetch_all,
            PROJECT,
            _project_qn("pkg.config.Config"),
            (3, 3),
            "derive",
            dry_run=True,
        )
    assert (root / "pkg/config.py").read_text() == source


# --- JS/TS class methods (Greptile + Copilot, PRs #2060/#2062) -------------------

COUNTER_JS = """\
class Counter {
  constructor() {
    this.items = [1, 2, 3];
  }

  total(factor) {
    let result = 0;
    for (const item of this.items) {
      result += item * factor;
    }
    return result;
  }
}

console.log(new Counter().total(2));
"""


def test_extract_from_a_js_class_method_makes_a_method(temp_repo: Path) -> None:
    root = _repo(temp_repo, {"src/counter.js": COUNTER_JS})
    store, updater = _index(root)
    report = extract(
        root,
        store.fetch_all,
        PROJECT,
        _project_qn("src.counter.Counter.total"),
        (7, 10),
        "sumScaled",
        reingest=updater.reingest,
    )
    assert report.applied, report.message
    assert report.inputs == ("factor",)
    text = (root / "src/counter.js").read_text()
    assert "    const result = this.sumScaled(factor);\n    return result;\n" in text
    assert (
        "\n  sumScaled(factor) {\n    let result = 0;\n"
        "    for (const item of this.items) {\n      result += item * factor;\n"
        "    }\n    return result;\n  }\n"
    ) in text
    assert "function sumScaled" not in text
    assert report.new_qualified_name == _project_qn("src.counter.Counter.sumScaled")
    assert _run_node(root / "src/counter.js") == "12"


def test_extract_from_a_static_js_method_calls_through_the_class(
    temp_repo: Path,
) -> None:
    source = (
        "class MathBox {\n"
        "  static twice(x) {\n"
        "    const doubled = x * 2;\n"
        "    return doubled;\n"
        "  }\n"
        "}\n\n"
        "const detached = MathBox.twice;\n"
        "console.log(detached(4));\n"
    )
    root = _repo(temp_repo, {"src/box.js": source})
    store, updater = _index(root)
    report = extract(
        root,
        store.fetch_all,
        PROJECT,
        _project_qn("src.box.MathBox.twice"),
        (3, 3),
        "double",
        reingest=updater.reingest,
    )
    assert report.applied, report.message
    text = (root / "src/box.js").read_text()
    assert "    const doubled = MathBox.double(x);\n" in text
    assert "\n  static double(x) {\n" in text
    # Called detached, so `this` would be undefined: the class name must be used.
    assert _run_node(root / "src/box.js") == "8"


# --- this / arguments (Greptile, PR #2062) ---------------------------------------

SCALED_JS = """\
function scaled(factor) {
  const base = this.base;
  const n = arguments.length;
  const g = function () { this.hit = 1; };
  const h = () => this.base;
  return base * factor * n + g.call(1) + h();
}
"""


@pytest.mark.parametrize(
    ("span", "word"),
    [((2, 2), "this"), ((3, 3), "arguments"), ((5, 5), "this")],
    ids=["this", "arguments", "arrow-inherits-this"],
)
def test_extract_refuses_this_or_arguments_in_a_standalone_js_function(
    temp_repo: Path, span: tuple[int, int], word: str
) -> None:
    root = _repo(temp_repo, {"src/scaled.js": SCALED_JS})
    store, _updater = _index(root)
    with pytest.raises(ExtractRefused, match=f"`{word}`"):
        extract(
            root,
            store.fetch_all,
            PROJECT,
            _project_qn("src.scaled.scaled"),
            span,
            "part",
            dry_run=True,
        )


def test_extract_allows_this_inside_a_nested_js_function(temp_repo: Path) -> None:
    # Control: a `function` expression has its own `this`, so moving it keeps
    # its meaning. (Its body avoids `return`, which the early-exit check
    # does not yet scope to the nested function.)
    root = _repo(temp_repo, {"src/scaled.js": SCALED_JS})
    store, _updater = _index(root)
    report = extract(
        root,
        store.fetch_all,
        PROJECT,
        _project_qn("src.scaled.scaled"),
        (4, 4),
        "makeG",
        dry_run=True,
    )
    assert report.outputs == ("g",)


def test_extract_refuses_arguments_in_a_js_method(temp_repo: Path) -> None:
    source = (
        "class Args {\n"
        "  count(a, b) {\n"
        "    const n = arguments.length;\n"
        "    return n;\n"
        "  }\n"
        "}\n"
    )
    root = _repo(temp_repo, {"src/args.js": source})
    store, _updater = _index(root)
    with pytest.raises(ExtractRefused, match="`arguments`"):
        extract(
            root,
            store.fetch_all,
            PROJECT,
            _project_qn("src.args.Args.count"),
            (3, 3),
            "part",
            dry_run=True,
        )


# --- await (Greptile, PR #2062) ---------------------------------------------------

ASYNC_PY = """\
async def fetch(x):
    return x


async def run(n, items):
    base = n + 1
    value = await fetch(base)
    total = 0
    async for item in items:
        total += item
    return value + total
"""

ASYNC_JS = """\
async function fetchIt(x) {
  return x;
}

async function run(n, items) {
  const base = n + 1;
  const value = await fetchIt(base);
  let total = 0;
  for await (const item of items) {
    total += item;
  }
  const later = async () => await fetchIt(total);
  return value + total + (await later());
}
"""


@pytest.mark.parametrize(
    ("rel", "source", "qn", "span"),
    [
        ("pkg/run.py", ASYNC_PY, "pkg.run.run", (7, 7)),
        ("pkg/run.py", ASYNC_PY, "pkg.run.run", (9, 10)),
        ("src/run.js", ASYNC_JS, "src.run.run", (7, 7)),
        ("src/run.js", ASYNC_JS, "src.run.run", (9, 11)),
    ],
    ids=["py-await", "py-async-for", "js-await", "js-for-await"],
)
def test_extract_refuses_a_span_that_awaits(
    temp_repo: Path, rel: str, source: str, qn: str, span: tuple[int, int]
) -> None:
    root = _repo(temp_repo, {"pkg/__init__.py": "", rel: source})
    store, _updater = _index(root)
    with pytest.raises(ExtractRefused, match="await"):
        extract(
            root, store.fetch_all, PROJECT, _project_qn(qn), span, "part", dry_run=True
        )


def test_extract_allows_await_inside_a_nested_async_arrow(temp_repo: Path) -> None:
    # Control: the `await` belongs to the arrow, which stays async.
    root = _repo(temp_repo, {"src/run.js": ASYNC_JS})
    store, _updater = _index(root)
    report = extract(
        root,
        store.fetch_all,
        PROJECT,
        _project_qn("src.run.run"),
        (12, 12),
        "makeLater",
        dry_run=True,
    )
    assert report.outputs == ("later",)


# --- mixed JS outputs (Greptile, PR #2062) ----------------------------------------

MIXED_JS = """\
"use strict";
function build(items) {
  let total = 0;
  const label = "n";
  total = items.length;
  const doubled = total * 2;
  return `${label}${total}${doubled}`;
}

console.log(build([1, 2, 3]));
"""


def test_extract_js_declares_fresh_outputs_beside_reassigned_ones(
    temp_repo: Path,
) -> None:
    root = _repo(temp_repo, {"src/mixed.js": MIXED_JS})
    store, updater = _index(root)
    report = extract(
        root,
        store.fetch_all,
        PROJECT,
        _project_qn("src.mixed.build"),
        (5, 6),
        "compute",
        reingest=updater.reingest,
    )
    assert report.applied, report.message
    assert report.outputs == ("total", "doubled")
    text = (root / "src/mixed.js").read_text()
    assert "  let doubled;\n  ({ total, doubled } = compute(" in text
    assert _run_node(root / "src/mixed.js") == "n36"


# --- CRLF (Greptile, PRs #2060/#2062) ---------------------------------------------

REPORT_TS = """\
export function build(items: number[], factor: number): string {
  let total = 0;
  for (const item of items) {
    total += item * factor;
  }
  return `${total}`;
}
"""


@pytest.mark.parametrize(
    ("rel", "source", "qn", "span", "helper"),
    [
        (
            "pkg/report.py",
            REPORT_PY,
            "pkg.report.build",
            (3, 11),
            b"def accumulate(items, factor):\r\n",
        ),
        (
            "src/report.ts",
            REPORT_TS,
            "src.report.build",
            (2, 5),
            b"function accumulate(items: number[], factor: number) {\r\n",
        ),
    ],
    ids=["python", "typescript"],
)
def test_extract_keeps_crlf_newlines(
    temp_repo: Path,
    rel: str,
    source: str,
    qn: str,
    span: tuple[int, int],
    helper: bytes,
) -> None:
    root = temp_repo / PROJECT
    root.mkdir()
    _write(root, "pkg/__init__.py", "")
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(source.replace("\n", "\r\n").encode())
    store, updater = _index(root)
    report = extract(
        root,
        store.fetch_all,
        PROJECT,
        _project_qn(qn),
        span,
        "accumulate",
        reingest=updater.reingest,
    )
    assert report.applied, report.message
    data = target.read_bytes()
    assert helper in data
    assert data.count(b"\n") == data.count(b"\r\n"), data
