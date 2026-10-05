"""Extract-function regressions raised in review of PR #2062 (and #2060)."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from codebase_rag import constants as cs
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
        [_NODE, str(path)], capture_output=True, encoding=cs.ENCODING_UTF8, check=False
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


# --- behaviour preservation (Greptile, PR #2932) ----------------------------------
#
# Each case extracts a span, runs the rewritten program and compares what it
# prints with what the original printed; a span that cannot keep that output
# must be refused and the file left alone.


def _run_python(path: Path) -> str:
    done = subprocess.run(
        [sys.executable, str(path)],
        capture_output=True,
        encoding=cs.ENCODING_UTF8,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    return done.stdout.strip()


def _run(path: Path) -> str:
    return _run_python(path) if path.suffix == ".py" else _run_node(path)


def _extract_and_run(
    temp_repo: Path, rel: str, source: str, qn: str, span: tuple[int, int]
) -> tuple[str, str]:
    """(output before, output after) extracting `span` of `qn` as `part`."""
    root = _repo(temp_repo, {rel: source})
    before = _run(root / rel)
    store, updater = _index(root)
    report = extract(
        root,
        store.fetch_all,
        PROJECT,
        _project_qn(qn),
        span,
        "part",
        reingest=updater.reingest,
    )
    assert report.applied, report.message
    return before, _run(root / rel)


def _refused(
    temp_repo: Path, rel: str, source: str, qn: str, span: tuple[int, int]
) -> str:
    root = _repo(temp_repo, {rel: source})
    store, _updater = _index(root)
    with pytest.raises(ExtractRefused) as refused:
        extract(
            root, store.fetch_all, PROJECT, _project_qn(qn), span, "part", dry_run=True
        )
    assert (root / rel).read_text() == source
    return str(refused.value)


BUMP_JS = """\
function bump(n) {
  let x = n;
  x += 1;
  x++;
  --x;
  x *= 3;
  return x;
}

console.log(bump(1));
"""

BUMP_PY = "def bump(n):\n    x = n\n    x += 1\n    return x\n\n\nprint(bump(1))\n"


@pytest.mark.parametrize(
    ("rel", "source", "span"),
    [
        ("src/bump.js", BUMP_JS, (3, 3)),
        ("src/bump.js", BUMP_JS, (4, 4)),
        ("src/bump.js", BUMP_JS, (5, 5)),
        ("src/bump.js", BUMP_JS, (6, 6)),
        ("pkg/bump.py", BUMP_PY, (3, 3)),
    ],
    ids=["js-augmented", "js-postfix", "js-prefix", "js-multiply", "py-augmented"],
)
def test_extracted_updates_flow_back_to_the_caller(
    temp_repo: Path, rel: str, source: str, span: tuple[int, int]
) -> None:
    qn = rel.rsplit(".", 1)[0].replace("/", ".") + ".bump"
    before, after = _extract_and_run(temp_repo, rel, source, qn, span)
    assert after == before


GROW_JS = """\
function grow(n) {
  let x = n + 1;
  x += 2;
  return x;
}

console.log(grow(1));
"""


def test_extracted_js_declaration_stays_writable(temp_repo: Path) -> None:
    before, after = _extract_and_run(
        temp_repo, "src/grow.js", GROW_JS, "src.grow.grow", (2, 2)
    )
    assert after == before == "4"
    text = (temp_repo / PROJECT / "src/grow.js").read_text()
    assert "  let x = part(n);\n" in text


PICK_PY = """\
def pick(flag):
    x = 0
    if flag:
        x = 1
    return x


print(pick(False), pick(True))
"""

PICK_JS = """\
function pick(flag) {
  let x = 0;
  if (flag) {
    x = 1;
  }
  return x;
}

console.log(pick(false), pick(true));
"""


@pytest.mark.parametrize(
    ("rel", "source", "span"),
    [("pkg/pick.py", PICK_PY, (3, 4)), ("src/pick.js", PICK_JS, (3, 5))],
    ids=["python", "javascript"],
)
def test_a_conditionally_written_output_is_also_an_input(
    temp_repo: Path, rel: str, source: str, span: tuple[int, int]
) -> None:
    qn = rel.rsplit(".", 1)[0].replace("/", ".") + ".pick"
    before, after = _extract_and_run(temp_repo, rel, source, qn, span)
    assert after == before == "0 1"


def test_extract_refuses_a_conditional_output_possibly_unbound_before(
    temp_repo: Path,
) -> None:
    source = (
        "def pick(flag, seed):\n"
        "    if seed:\n"
        "        x = 0\n"
        "    if flag:\n"
        "        x = 1\n"
        "    return x\n"
    )
    message = _refused(temp_repo, "pkg/pick.py", source, "pkg.pick.pick", (4, 5))
    assert "`x`" in message


LATE_PY = """\
def late():
    x = 0

    def read():
        return x

    x = 1
    return read()


print(late())
"""

LATE_JS = """\
function late() {
  let x = 0;
  const read = () => x;
  x = 1;
  return read();
}

console.log(late());
"""


@pytest.mark.parametrize(
    ("rel", "source", "span"),
    [("pkg/late.py", LATE_PY, (7, 7)), ("src/late.js", LATE_JS, (4, 4))],
    ids=["python", "javascript"],
)
def test_a_name_a_closure_captures_is_an_output(
    temp_repo: Path, rel: str, source: str, span: tuple[int, int]
) -> None:
    qn = rel.rsplit(".", 1)[0].replace("/", ".") + ".late"
    before, after = _extract_and_run(temp_repo, rel, source, qn, span)
    assert after == before == "1"


def test_extract_refuses_a_closure_over_a_name_rebound_after_the_span(
    temp_repo: Path,
) -> None:
    # Moved into the helper, `read` would capture the helper's copy of x and
    # never see the caller's later `x = 1`.
    message = _refused(temp_repo, "pkg/late.py", LATE_PY, "pkg.late.late", (4, 5))
    assert "`x`" in message


SHARED_FILES = {
    "pkg/__init__.py": "",
    "pkg/shared.py": (
        "counter = 0\n\n\n"
        "def bump():\n"
        "    global counter\n"
        "    counter += 1\n"
        "    return counter\n\n\n"
        "def outer():\n"
        "    count = 0\n\n"
        "    def inner():\n"
        "        nonlocal count\n"
        "        count += 1\n"
        "        return count\n\n"
        "    return inner()\n"
    ),
}


@pytest.mark.parametrize(
    ("qn", "span", "word"),
    [
        ("pkg.shared.bump", (6, 6), "global"),
        ("pkg.shared.outer.inner", (15, 15), "nonlocal"),
    ],
    ids=["global", "nonlocal"],
)
def test_extract_refuses_writes_to_global_or_nonlocal_names(
    temp_repo: Path, qn: str, span: tuple[int, int], word: str
) -> None:
    root = _repo(temp_repo, SHARED_FILES)
    store, _updater = _index(root)
    with pytest.raises(ExtractRefused, match=word):
        extract(
            root, store.fetch_all, PROJECT, _project_qn(qn), span, "part", dry_run=True
        )
    assert (root / "pkg/shared.py").read_text() == SHARED_FILES["pkg/shared.py"]


SCALE_PY = """\
class Scale:
    factor = 3

    @classmethod
    def apply(cls, x):
        y = x * cls.factor
        return y

    @staticmethod
    def twice(x):
        y = x * 2
        return y


print(Scale.apply(2), Scale.twice(5), Scale().apply(1), Scale().twice(1))
"""


@pytest.mark.parametrize(
    ("qn", "span", "call", "decorator"),
    [
        ("pkg.scale.Scale.apply", (6, 6), "y = cls.part(x)", "@classmethod"),
        ("pkg.scale.Scale.twice", (11, 11), "y = Scale.part(x)", "@staticmethod"),
    ],
    ids=["classmethod", "staticmethod"],
)
def test_extract_keeps_the_method_binding_of_class_and_static_methods(
    temp_repo: Path, qn: str, span: tuple[int, int], call: str, decorator: str
) -> None:
    before, after = _extract_and_run(temp_repo, "pkg/scale.py", SCALE_PY, qn, span)
    assert after == before == "6 10 3 2"
    text = (temp_repo / PROJECT / "pkg/scale.py").read_text()
    assert f"        {call}\n" in text
    assert f"    {decorator}\n    def part(" in text


_TSC = shutil.which("tsc")

AREA_TS = """\
export function area(n: number): number {
  const doubled: number = n * 2;
  const total = doubled + 1;
  return total;
}
"""


def test_extracted_typescript_parameters_keep_the_local_type(
    temp_repo: Path,
) -> None:
    root = _repo(temp_repo, {"src/area.ts": AREA_TS})
    store, updater = _index(root)
    report = extract(
        root,
        store.fetch_all,
        PROJECT,
        _project_qn("src.area.area"),
        (3, 3),
        "part",
        reingest=updater.reingest,
    )
    assert report.applied, report.message
    assert "function part(doubled: number) {\n" in (root / "src/area.ts").read_text()
    if _TSC is not None:
        done = subprocess.run(
            [_TSC, "--strict", "--noEmit", "src/area.ts"],
            cwd=root,
            capture_output=True,
            encoding=cs.ENCODING_UTF8,
            check=False,
        )
        assert done.returncode == 0, done.stdout + done.stderr


def test_extract_refuses_an_untyped_typescript_local_input(temp_repo: Path) -> None:
    source = AREA_TS.replace("doubled: number", "doubled")
    message = _refused(temp_repo, "src/area.ts", source, "src.area.area", (3, 3))
    assert "`doubled`" in message


LETTER_PY = '''\
def letter(name):
    text = """Dear
  {0},
thanks
    """.format(name)
    return text


print(repr(letter("Ann")))
'''

LETTER_JS = """\
function letter(name) {
  const text = `Dear
  ${name},
thanks
    `;
  return text;
}

console.log(JSON.stringify(letter("Ann")));
"""


@pytest.mark.parametrize(
    ("rel", "source", "span"),
    [("pkg/letter.py", LETTER_PY, (2, 5)), ("src/letter.js", LETTER_JS, (2, 5))],
    ids=["python", "javascript"],
)
def test_extract_leaves_multiline_string_contents_alone(
    temp_repo: Path, rel: str, source: str, span: tuple[int, int]
) -> None:
    qn = rel.rsplit(".", 1)[0].replace("/", ".") + ".letter"
    before, after = _extract_and_run(temp_repo, rel, source, qn, span)
    assert after == before


_LAZY_JS = """\
function pick(flag) {{
  let x = flag ? null : 0;
  {line}
  return x;
}}

console.log(pick(false), pick(true));
"""

_LAZY_PY = """\
def pick(flag, vals):
    x = None if flag else 0
    {line}
    return x


print(pick(False, []), pick(True, [5]))
"""


@pytest.mark.parametrize(
    ("rel", "template", "line"),
    [
        ("src/pick.js", _LAZY_JS, "flag && (x = 1);"),
        ("src/pick.js", _LAZY_JS, "flag || (x = 1);"),
        ("src/pick.js", _LAZY_JS, "flag ? (x = 1) : 0;"),
        ("src/pick.js", _LAZY_JS, "x ??= 1;"),
        ("src/pick.js", _LAZY_JS, "x ||= 1;"),
        ("src/pick.js", _LAZY_JS, "x &&= 1;"),
        ("pkg/pick.py", _LAZY_PY, "flag and (x := 1)"),
        ("pkg/pick.py", _LAZY_PY, "flag or (x := 1)"),
        ("pkg/pick.py", _LAZY_PY, "y = (x := 1) if flag else 0"),
        ("pkg/pick.py", _LAZY_PY, "[x := v for v in vals]"),
    ],
    ids=[
        "js-and",
        "js-or",
        "js-ternary",
        "js-nullish-assign",
        "js-or-assign",
        "js-and-assign",
        "py-and-walrus",
        "py-or-walrus",
        "py-conditional-walrus",
        "py-comprehension-walrus",
    ],
)
def test_a_write_under_a_short_circuit_is_conditional(
    temp_repo: Path, rel: str, template: str, line: str
) -> None:
    qn = rel.rsplit(".", 1)[0].replace("/", ".") + ".pick"
    source = template.format(line=line)
    before, after = _extract_and_run(temp_repo, rel, source, qn, (3, 3))
    assert after == before


_OUTER_JS = {
    "module-let": (
        "let total = 0;\n"
        "function add(n) {\n"
        "  const m = n * 2;\n"
        "  total += m;\n"
        "  return m;\n"
        "}\n\n"
        "add(2);\n"
        "console.log(total);\n"
    ),
    "undeclared-global": (
        "function add(n) {\n"
        "  const m = n * 2;\n"
        "  ready = m;\n"
        "  return m;\n"
        "}\n\n"
        "add(2);\n"
        "console.log(ready);\n"
    ),
}


@pytest.mark.parametrize(
    ("shape", "span", "name"),
    [("module-let", (4, 4), "total"), ("undeclared-global", (3, 3), "ready")],
    ids=["module-let", "undeclared-global"],
)
def test_extract_refuses_a_write_to_a_name_declared_outside_the_function(
    temp_repo: Path, shape: str, span: tuple[int, int], name: str
) -> None:
    # The helper would declare the name as its own local, so the write would
    # never reach the outer binding the caller and the module read.
    message = _refused(temp_repo, "src/add.js", _OUTER_JS[shape], "src.add.add", span)
    assert f"`{name}`" in message


def test_extract_allows_a_write_to_a_hoisted_var(temp_repo: Path) -> None:
    # Control: a `var` anywhere in the function is its own local, however
    # deeply it is nested, so the write stays the function's.
    source = (
        "function count(items) {\n"
        "  if (items.length) {\n"
        "    var seen = 0;\n"
        "  }\n"
        "  seen = items.length;\n"
        "  return seen;\n"
        "}\n\n"
        "console.log(count([1, 2]));\n"
    )
    before, after = _extract_and_run(
        temp_repo, "src/count.js", source, "src.count.count", (5, 5)
    )
    assert after == before == "2"


# --- generic TypeScript functions -------------------------------------------------

_GENERIC_TS = {
    "function": (
        "export function build<T extends U, U, V = string>(x: T, u: U): T[] {\n"
        "  const y: T = x;\n"
        "  const out = [y, y];\n"
        "  return out;\n"
        "}\n\n"
        "console.log(JSON.stringify(build(3, 4)));\n"
    ),
    "method": (
        "class Box<K> {\n"
        "  constructor(private k: K) {}\n\n"
        "  pick<T>(x: T): [T, K] {\n"
        "    const y: T = x;\n"
        "    const out: [T, K] = [y, this.k];\n"
        "    return out;\n"
        "  }\n"
        "}\n\n"
        "console.log(JSON.stringify(new Box('k').pick(3)));\n"
    ),
}


@pytest.mark.parametrize(
    ("shape", "qn", "span", "header", "call"),
    [
        (
            "function",
            "src.build.build",
            (2, 2),
            "function part<T extends U, U>(x: T) {",
            "const y = part<T, U>(x);",
        ),
        (
            "method",
            "src.build.Box.pick",
            (5, 5),
            "  part<T>(x: T) {",
            "const y = this.part<T>(x);",
        ),
    ],
    ids=["function", "method"],
)
def test_extracted_typescript_helper_keeps_its_type_parameters(
    temp_repo: Path,
    shape: str,
    qn: str,
    span: tuple[int, int],
    header: str,
    call: str,
) -> None:
    # A carried `x: T` names the enclosing function's own type parameter;
    # outside its declaration the helper must declare it again (TS2304).
    root = _repo(temp_repo, {"src/build.ts": _GENERIC_TS[shape]})
    store, updater = _index(root)
    report = extract(
        root,
        store.fetch_all,
        PROJECT,
        _project_qn(qn),
        span,
        "part",
        reingest=updater.reingest,
    )
    assert report.applied, report.message
    text = (root / "src/build.ts").read_text()
    assert header in text and call in text, text
    if _TSC is not None:
        done = subprocess.run(
            [_TSC, "--strict", "--noEmit", "--target", "es2020", "src/build.ts"],
            cwd=root,
            capture_output=True,
            encoding=cs.ENCODING_UTF8,
            check=False,
        )
        assert done.returncode == 0, done.stdout + done.stderr


# --- the helper's own name and line (Greptile, PR #2932) -------------------------

_SHADOWING = {
    "py-parameter": (
        "pkg/build.py",
        "def build(part, n):\n    y = n + 1\n    return y\n",
        (2, 2),
    ),
    "py-local": (
        "pkg/build.py",
        "def build(n):\n    y = n + 1\n    part = y * 2\n    return part\n",
        (2, 2),
    ),
    "js-parameter": (
        "src/build.js",
        "function build(part, n) {\n  const y = n + 1;\n  return y;\n}\n",
        (2, 2),
    ),
}


@pytest.mark.parametrize("shape", sorted(_SHADOWING))
def test_extract_refuses_a_helper_name_the_function_binds(
    temp_repo: Path, shape: str
) -> None:
    # The call names the helper bare, so it would call the parameter or the
    # local of the same name instead.
    rel, source, span = _SHADOWING[shape]
    qn = rel.rsplit(".", 1)[0].replace("/", ".") + ".build"
    message = _refused(temp_repo, rel, source, qn, span)
    assert "`part`" in message


_ONE_LINE = {
    "py-header": ("pkg/emit.py", "def emit(n): print(n)\n\n\nemit(7)\n"),
    "js-braces": ("src/emit.js", "function emit(n) { console.log(n); }\n\nemit(7);\n"),
}


@pytest.mark.parametrize("shape", sorted(_ONE_LINE))
def test_extract_refuses_a_body_sharing_a_line_with_its_header(
    temp_repo: Path, shape: str
) -> None:
    # Cutting the whole line would move the header (or the closing brace)
    # into the helper and leave a call at module level.
    rel, source = _ONE_LINE[shape]
    qn = rel.rsplit(".", 1)[0].replace("/", ".") + ".emit"
    message = _refused(temp_repo, rel, source, qn, (1, 1))
    assert "shares line 1" in message
