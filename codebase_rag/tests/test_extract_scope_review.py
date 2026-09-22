"""Review findings on the extract/inline scope and transaction helpers
(Greptile and Copilot, PR #2057)."""

from __future__ import annotations

from pathlib import Path

import pytest
from tree_sitter import Node

from codebase_rag import constants as cs
from codebase_rag.editing import extract_transaction
from codebase_rag.editing.contract import Expectation, Verdict
from codebase_rag.editing.extract_scope import (
    _binds,
    _early_exit,
    _split_span,
)
from codebase_rag.editing.extract_types import ExtractReport
from codebase_rag.editing.transaction import EditTransaction, undo_last
from codebase_rag.parser_loader import load_parsers

_PARSERS = load_parsers()[0]


def _root(language: cs.SupportedLanguage, source: str) -> Node:
    return _PARSERS[language].parse(source.encode()).root_node


def _first(node: Node, kind: str) -> Node:
    stack = [node]
    while stack:
        current = stack.pop()
        if current.type == kind:
            return current
        stack.extend(reversed(current.children))
    raise AssertionError(f"no {kind} node")


def _span_io(
    language: cs.SupportedLanguage, source: str, kind: str, start: int, end: int
) -> tuple[list[str], list[str]]:
    from codebase_rag.editing import extract_scope

    definition = _first(_root(language, source), kind)
    span = _split_span(definition, start, end)
    return extract_scope._analyse_span_dependencies(definition, span)


# --- kMsGo / kMr-A: imports and destructuring bind names ---------------------------


def test_python_imports_in_the_span_are_bindings() -> None:
    source = (
        "def f():\n"
        "    import math as m\n"
        "    import os.path\n"
        "    from json import dumps as dump, loads\n"
        "    return m.pi, os.sep, dump, loads\n"
    )
    inputs, outputs = _span_io(
        cs.SupportedLanguage.PYTHON, source, cs.TS_PY_FUNCTION_DEFINITION, 2, 4
    )
    assert inputs == []
    # `os.path` binds `os`, an alias binds the alias and never the module.
    assert outputs == ["m", "os", "dump", "loads"]


def test_js_destructuring_in_the_span_is_a_binding() -> None:
    source = (
        "function f(o) {\n"
        "  let d, e;\n"
        "  const { a, b: c, z = o } = o;\n"
        "  [d, e = o] = o;\n"
        "  return a + c + d + e + z;\n"
        "}\n"
    )
    inputs, outputs = _span_io(
        cs.SupportedLanguage.JS, source, cs.TS_FUNCTION_DECLARATION, 3, 4
    )
    assert inputs == ["o"]
    # A default value (`= o`) is read, never bound.
    assert outputs == ["a", "c", "z", "d", "e"]


# --- kMsGq: assignment targets are not reads -----------------------------------------


def test_an_overwrite_does_not_read_its_target() -> None:
    source = (
        "def f(c, a, i):\n"
        "    if c:\n"
        "        x = 0\n"
        "    x = 1\n"
        "    a[i] = x\n"
        "    for k in a:\n"
        "        pass\n"
        "    return x, k\n"
    )
    inputs, outputs = _span_io(
        cs.SupportedLanguage.PYTHON, source, cs.TS_PY_FUNCTION_DEFINITION, 4, 7
    )
    # `x = 1` overwrites x: passing x in would evaluate a possibly unbound
    # name. The subscript target `a[i]` still READS a and i.
    assert inputs == ["a", "i"]
    assert outputs == ["x", "k"]


def test_an_augmented_assignment_still_reads_its_target() -> None:
    source = "def f(x):\n    x += 1\n    return x\n"
    inputs, outputs = _span_io(
        cs.SupportedLanguage.PYTHON, source, cs.TS_PY_FUNCTION_DEFINITION, 2, 2
    )
    assert (inputs, outputs) == (["x"], ["x"])
    # Pinned together with the overwrite case: a fix that dropped every
    # left-hand identifier would pass the overwrite test and fail here.
    source = "function f(y) {\n  let x = y;\n  x = 2;\n  return x;\n}\n"
    js_inputs, _ = _span_io(
        cs.SupportedLanguage.JS, source, cs.TS_FUNCTION_DECLARATION, 3, 3
    )
    assert js_inputs == []


# --- kMsGr: a break inside a selected switch stays in the span ----------------------


def test_a_switch_local_break_is_not_an_early_exit() -> None:
    source = (
        "function f(k, xs) {\n"
        "  switch (k) {\n"
        "    case 1:\n"
        "      k = 2;\n"
        "      break;\n"
        "  }\n"
        "  for (const x of xs) {\n"
        "    switch (x) {\n"
        "      case 1:\n"
        "        continue;\n"
        "    }\n"
        "  }\n"
        "  return k;\n"
        "}\n"
    )
    root = _root(cs.SupportedLanguage.JS, source)
    switches = [
        n
        for n in _walk(root)
        if n.type == cs.TS_JS_SWITCH_STATEMENT  # both switches, in order
    ]
    assert len(switches) == 2
    assert _early_exit(switches[0]) is None
    # A `continue` in a selected switch still targets the loop outside it.
    leaving = _early_exit(switches[1])
    assert leaving is not None and leaving.type == cs.TS_CONTINUE_STATEMENT


def _walk(node: Node) -> list[Node]:
    out, stack = [], [node]
    while stack:
        current = stack.pop()
        out.append(current)
        stack.extend(reversed(current.children))
    return out


# --- kYS8c: every JS/TS function and class form is a nested scope --------------------


@pytest.mark.parametrize(
    "statement",
    [
        "const g = function () { let y = 1; return y; };",
        "const g = function* () { let y = 1; return y; };",
        "const g = { m() { let y = 1; return y; } };",
        "const g = class { m() { let y = 1; return y; } };",
        "const g = function inner() { let y = 1; return y; };",
    ],
)
def test_js_function_and_class_expressions_are_nested_scopes(statement: str) -> None:
    source = f"function f() {{\n  {statement}\n  return g;\n}}\n"
    definition = _first(
        _root(cs.SupportedLanguage.JS, source), cs.TS_FUNCTION_DECLARATION
    )
    span = _split_span(definition, 2, 2)
    assert _early_exit(span.statements[0]) is None
    bound: list[str] = []
    _binds(span.statements[0], bound)
    # Only `g` lands in f's scope: neither the inner `y` nor the name of a
    # named function expression, which is visible only inside itself.
    assert bound == ["g"]


# --- kMsGw / kMr-F: descriptive name, old name kept for the higher layers ------------


def test_the_span_analysis_has_a_descriptive_name() -> None:
    from codebase_rag.editing import extract_scope

    assert extract_scope._analyse is extract_scope._analyse_span_dependencies


# --- kMsGt / kYS7-: the rollback targets this report's own transaction ---------------


def _commit_file(root: Path, rel: str, content: str) -> str:
    tx = EditTransaction(root)
    tx.stage(rel, content)
    outcome = tx.commit()
    assert outcome.applied
    return outcome.transaction_id


def _report(tx_id: str, rel: str) -> ExtractReport:
    return ExtractReport(
        qualified_name="p.f",
        new_qualified_name="p.g",
        path=rel,
        span=(1, 1),
        inputs=(),
        outputs=(),
        applied=True,
        transaction_id=tx_id,
        files=(rel,),
        diff="",
        message="applied",
    )


@pytest.fixture
def failing_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(extract_transaction, "measure", lambda *a, **k: None)
    monkeypatch.setattr(
        extract_transaction,
        "verify",
        lambda *a, **k: Verdict(False, ("broken",), (), None),
    )


def _enforce(root: Path, report: ExtractReport, calls: list[list[str]]):
    return extract_transaction._enforce(
        report,
        Expectation(operation=cs.CONTRACT_OP_EXTRACT),
        lambda *a, **k: [],
        "proj",
        root,
        calls.append,
        cs.EXTRACT_CONTRACT_FAILED,
    )


def test_rollback_reverses_only_this_reports_transaction(
    tmp_path: Path, failing_contract: None
) -> None:
    (tmp_path / "a.py").write_text("a = 0\n")
    (tmp_path / "b.py").write_text("b = 0\n")
    extract_tx = _commit_file(tmp_path, "a.py", "a = 1\n")
    _commit_file(tmp_path, "b.py", "b = 1\n")
    calls: list[list[str]] = []

    refused = _enforce(tmp_path, _report(extract_tx, "a.py"), calls)

    # The newer, unrelated edit survives; the extract is still on disk and
    # the report says so instead of claiming a rollback.
    assert (tmp_path / "b.py").read_text() == "b = 1\n"
    assert (tmp_path / "a.py").read_text() == "a = 1\n"
    assert refused.applied is False
    assert refused.message.startswith(cs.EDIT_ROLLBACK_REFUSED.split("{", 1)[0])
    assert "broken" in refused.message
    assert calls == []

    # Once it is the newest again, the same call rolls it back.
    undo_last(tmp_path)
    rolled = _enforce(tmp_path, _report(extract_tx, "a.py"), calls)
    assert (tmp_path / "a.py").read_text() == "a = 0\n"
    assert rolled.message == cs.EXTRACT_CONTRACT_FAILED.format(reasons="broken")
    assert calls == [["a.py"]]


def test_a_refused_undo_is_reported_not_raised(
    tmp_path: Path, failing_contract: None
) -> None:
    (tmp_path / "a.py").write_text("a = 0\n")
    extract_tx = _commit_file(tmp_path, "a.py", "a = 1\n")
    (tmp_path / "a.py").write_text("a = 2  # hand edit\n")
    calls: list[list[str]] = []

    report = _enforce(tmp_path, _report(extract_tx, "a.py"), calls)

    assert (tmp_path / "a.py").read_text() == "a = 2  # hand edit\n"
    assert report.applied is False
    assert report.message.startswith(cs.EDIT_ROLLBACK_REFUSED.split("{", 1)[0])
    assert calls == []


# --- coordinator (inline agent): parameters and atomic arguments ---------------------


@pytest.mark.parametrize(
    ("language", "source", "kind", "expected"),
    [
        (
            cs.SupportedLanguage.PYTHON,
            "def f(x, n=k, t: T = u, s: S, *a: A, **kw):\n    pass\n",
            cs.TS_PY_FUNCTION_DEFINITION,
            ["x", "n", "t", "s", "a", "kw"],
        ),
        (
            cs.SupportedLanguage.JS,
            "function f(x, n = k, {a, b: c} = d, ...r) {}\n",
            cs.TS_FUNCTION_DECLARATION,
            ["x", "n", "a", "c", "r"],
        ),
    ],
)
def test_parameter_names_exclude_defaults_and_annotations(
    language: cs.SupportedLanguage, source: str, kind: str, expected: list[str]
) -> None:
    from codebase_rag.editing.extract_scope import _parameter_names

    definition = _first(_root(language, source), kind)
    # A default value or an annotation is an expression the parameter list
    # evaluates, never a name it binds.
    assert _parameter_names(definition) == expected


@pytest.mark.parametrize(
    ("text", "atomic"),
    [
        ("'a' + 'b'", False),
        ('"a" if c else "b"', False),
        ("'a', 'b'", False),
        ("'it\\'s'", True),
        ('"a"', True),
        ("''", True),
        ("x.y", True),
        ("-1.5", True),
    ],
)
def test_simple_arg_matches_one_literal_only(text: str, atomic: bool) -> None:
    from codebase_rag.editing.extract_scope import _SIMPLE_ARG

    assert bool(_SIMPLE_ARG.match(text)) is atomic
