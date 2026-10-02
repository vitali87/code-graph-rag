"""Issue #2438: only a name, a member access or a function hands a callback over.

Every argument of a call to a builtin or unresolved callee was resolved as a
function reference by its full source text, against the receiver's inferred
type. A property read on a builtin-typed receiver (`t.is(error.customDelay,
undefined)`) became a CALLS edge to `builtin.Error.prototype.customDelay`, and
arithmetic (`Math.max(items.length - start, 0)`) one to
`builtin.Array.prototype.length - start`. Neither target is ever a node, so the
database dropped every such row and the flush warned about it on every index.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag.tests.conftest import get_relationships, run_updater

BUILTIN_QN_PREFIX = f"{cs.BUILTIN_PREFIX}{cs.SEPARATOR_DOT}"

# The issue's repro, verbatim.
ERROR_PROPS_TS = """import test from 'ava';

function helper() { return 1; }

test('error props', t => {
\thelper();
\tconst error = new Error('boom');
\tt.is(error.customDelay, undefined);
\tt.is(error.name, 'Error');
});
"""

# The issue comment's repro, verbatim.
CLAMP_JS = """function clamp(args, start) {
  var items = Array(3);
  var a = Math.max(items.length, 0);
  var b = Math.max(items.length - start, 0);
  var c = Math.min(items.length * 2, 9);
  return a + b + c;
}

module.exports = clamp;
"""


def _edges(mock_ingestor: MagicMock) -> set[tuple[str, str, str]]:
    return {
        (str(c.args[1]), str(c.args[0][2]), str(c.args[2][2]))
        for rel in (cs.RelationshipType.CALLS, cs.RelationshipType.REFERENCES)
        for c in get_relationships(mock_ingestor, rel.value)
    }


def _index(repo: Path, mock_ingestor: MagicMock, files: dict[str, str]) -> None:
    for name, source in files.items():
        (repo / name).write_text(source)
    run_updater(repo, mock_ingestor)


def _builtin_targets(edges: set[tuple[str, str, str]]) -> list[str]:
    return sorted(t for _, _, t in edges if t.startswith(BUILTIN_QN_PREFIX))


def test_error_property_reads_passed_as_arguments_are_not_callbacks(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    _index(temp_repo, mock_ingestor, {"app.test.ts": ERROR_PROPS_TS})

    assert _builtin_targets(_edges(mock_ingestor)) == []


def test_arithmetic_on_a_builtin_property_is_not_a_callback(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    _index(temp_repo, mock_ingestor, {"args.js": CLAMP_JS})

    assert _builtin_targets(_edges(mock_ingestor)) == []


def test_the_real_calls_next_to_the_property_reads_keep_their_edges(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # Negative: the direct call `helper()` and the inline test callback are
    # still edges once the property reads stop minting phantoms.
    _index(temp_repo, mock_ingestor, {"app.test.ts": ERROR_PROPS_TS})

    targets = {t for _, _, t in _edges(mock_ingestor)}
    project = temp_repo.name
    assert f"{project}.app.test.helper" in targets, targets
    assert f"{project}.app.test.anonymous_4_20" in targets, targets


JS_CALLBACKS = """const obj = { method() { return 1; } };

function helper() { return 1; }

function main(arr, start) {
  arr.map(helper);
  setTimeout(obj.method);
  Math.max(arr.length - start, 0);
}
"""

TS_CALLBACKS = """class Svc {
  handle(): number { return 1; }
}

function helper(): number { return 1; }

export function main(items: string[], s: Svc): void {
  items.forEach(helper);
  setTimeout(s.handle);
  const err = new Error('x');
  console.log(err.stack, -items.length);
}
"""

PY_CALLBACKS = """class Svc:
    def handle(self, x):
        return x


def helper(x):
    return x


def main(xs):
    s = Svc()
    sorted(xs, key=helper)
    list(map(s.handle, xs))
    max(len(xs) - 1, 0)
"""


@pytest.mark.parametrize(
    ("file_name", "source", "expected"),
    [
        (
            "cb.js",
            JS_CALLBACKS,
            {("cb.main", "cb.helper"), ("cb.main", "cb.method")},
        ),
        (
            "cb.ts",
            TS_CALLBACKS,
            {("cb.main", "cb.helper"), ("cb.main", "cb.Svc.handle")},
        ),
        (
            "cb.py",
            PY_CALLBACKS,
            {("cb.main", "cb.helper"), ("cb.main", "cb.Svc.handle")},
        ),
    ],
    ids=["javascript", "typescript", "python"],
)
def test_a_callback_passed_by_name_or_member_keeps_its_edge(
    temp_repo: Path,
    mock_ingestor: MagicMock,
    file_name: str,
    source: str,
    expected: set[tuple[str, str]],
) -> None:
    # Negative: `arr.map(helper)`, `setTimeout(obj.method)`,
    # `sorted(xs, key=helper)`, `map(s.handle, xs)` still reference the
    # callback, next to the computed arguments that no longer do.
    _index(temp_repo, mock_ingestor, {file_name: source})

    project = temp_repo.name
    edges = _edges(mock_ingestor)
    pairs = {(src, dst) for _, src, dst in edges}
    for caller, callee in expected:
        assert (f"{project}.{caller}", f"{project}.{callee}") in pairs, edges
    assert _builtin_targets(edges) == []


GO_CALLBACKS = """package main

type Svc struct{}

func (s *Svc) Handle() {}

func helper() {}

func run(f func()) {}

func main() {
\ts := &Svc{}
\trun(helper)
\trun(s.Handle)
}
"""

CPP_CALLBACKS = """namespace ns { void fn() {} }
struct Cls { static void m() {} };
void helper() {}
void run(void (*f)()) {}
void go() {
  run(helper);
  run(ns::fn);
  run(&Cls::m);
}
"""

CS_CALLBACKS = """using System;
class App {
  void Handle() {}
  static void Helper() {}
  void Go(int n) {
    Run(this.Handle);
    Run(App.Helper);
  }
  void Run(Action a) {}
}
"""

DART_CALLBACKS = """class Ticker {
  void tick() {}
}

void helper() {}

void run(Function f) {}

void main() {
  final t = Ticker();
  run(helper);
  run(t.tick);
}
"""


@pytest.mark.parametrize(
    ("file_name", "source", "expected"),
    [
        (
            "main.go",
            GO_CALLBACKS,
            {("main.main", "main.helper"), ("main.main", "main.Svc.Handle")},
        ),
        (
            "cb.cpp",
            CPP_CALLBACKS,
            {("cb.go", "cb.helper"), ("cb.go", "cb.ns.fn"), ("cb.go", "cb.Cls.m")},
        ),
        (
            "App.cs",
            CS_CALLBACKS,
            {
                ("App.App.Go(int)", "App.App.Handle"),
                ("App.App.Go(int)", "App.App.Helper"),
            },
        ),
        (
            "cb.dart",
            DART_CALLBACKS,
            {("cb.main", "cb.helper"), ("cb.main", "cb.Ticker.tick")},
        ),
    ],
    ids=["go", "cpp", "csharp", "dart"],
)
def test_other_languages_keep_their_callback_references(
    temp_repo: Path,
    mock_ingestor: MagicMock,
    file_name: str,
    source: str,
    expected: set[tuple[str, str]],
) -> None:
    # Negative: the same path serves Go selectors, C++ qualified names and
    # address-of, C# member access and Dart tear-offs; the argument gate must
    # keep each of them a reference.
    _index(temp_repo, mock_ingestor, {file_name: source})

    project = temp_repo.name
    pairs = {(src, dst) for _, src, dst in _edges(mock_ingestor)}
    for caller, callee in expected:
        assert (f"{project}.{caller}", f"{project}.{callee}") in pairs, pairs
