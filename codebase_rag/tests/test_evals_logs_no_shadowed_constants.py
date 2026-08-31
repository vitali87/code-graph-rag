"""Module-level names in the evals message modules must be bound once.

A second `NAME = ...` at module level silently wins: Python keeps the last
binding, the earlier one becomes dead code, and every caller gets a message
its author never saw. `ruff` does not catch it for string constants, so
`ruff check evals/` was clean while five `*_ORACLE_MISSING` names were bound
twice (issue #1518), four of them leaking a literal `{binary}` to users.

Asserted over the whole module rather than over the five known names: the
defect is a class, and #1516 removed the same shape from `constants.py`
where the two `CPP_SUFFIXES` bindings actually differed.
"""

from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path

import pytest

_EVALS = Path(__file__).resolve().parents[2] / "evals"


def _bindings(body: list[ast.stmt]) -> list[str]:
    """Every name `body` binds at module scope, in order, with repeats.

    Imports and `def`/`class` count: `from pkg import NAME` followed by
    `NAME = ...` is the same shadowing defect and would otherwise pass.
    A bare `NAME: str` does not, since an annotation without a value binds
    nothing at runtime.

    Branches of one `if`/`try` are UNIONED rather than concatenated. At most
    one branch executes, so `if x: FOO = 1` / `else: FOO = 2` binds `FOO`
    once; summing them would report a duplicate that cannot occur and make
    the guard fire on correct code.
    """
    names: list[str] = []
    for node in body:
        if isinstance(node, ast.Assign):
            names.extend(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign):
            if node.value is not None and isinstance(node.target, ast.Name):
                names.append(node.target.id)
        elif isinstance(node, ast.Import | ast.ImportFrom):
            names.extend(a.asname or a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            names.append(node.name)
        elif isinstance(node, ast.If):
            names.extend(
                sorted(set(_bindings(node.body)) | set(_bindings(node.orelse)))
            )
        elif isinstance(node, ast.Try):
            branches = [node.body, node.orelse, *(h.body for h in node.handlers)]
            alternatives: set[str] = set()
            for branch in branches:
                alternatives |= set(_bindings(branch))
            names.extend(sorted(alternatives))
            names.extend(_bindings(node.finalbody))
    return names


def _module_level_bindings(path: Path) -> list[str]:
    return _bindings(ast.parse(path.read_text(encoding="utf-8")).body)


_SHADOWS = [
    ("plain reassignment", "A = 1\nA = 2\n"),
    ("import then assign", "from pkg import A\nA = 2\n"),
    ("assign then import", "A = 1\nfrom pkg import A\n"),
    ("aliased import then assign", "import pkg as A\nA = 2\n"),
    ("def then assign", "def A():\n    pass\n\n\nA = 2\n"),
    ("class then assign", "class A:\n    pass\n\n\nA = 2\n"),
    ("annotated then plain", "A: int = 1\nA = 2\n"),
]

_CLEAN = [
    ("distinct names", "A = 1\nB = 2\n"),
    ("if/else alternatives", "if X:\n    A = 1\nelse:\n    A = 2\n"),
    ("try/except alternatives", "try:\n    A = 1\nexcept E:\n    A = 2\n"),
    ("bare annotation then assign", "A: int\nA = 2\n"),
    ("attribute assignment", "A = 1\nA.b = 2\n"),
    ("nested function local", "A = 1\n\n\ndef f():\n    A = 2\n"),
]


@pytest.mark.parametrize(("label", "source"), _SHADOWS)
def test_the_guard_detects_each_shadowing_form(label: str, source: str) -> None:
    """The guard must fire on every binding form, not only `NAME = ...`.

    Without these, `from pkg import NAME` above `NAME = ...` reads as clean,
    which is the same defect wearing a different statement.
    """
    names = _bindings(ast.parse(source).body)

    duplicates = [name for name, count in Counter(names).items() if count > 1]
    assert duplicates == ["A"], f"{label}: guard missed the shadow, saw {names}"


@pytest.mark.parametrize(("label", "source"), _CLEAN)
def test_the_guard_does_not_fire_on_correct_code(label: str, source: str) -> None:
    """A guard that fires on correct code gets deleted rather than obeyed.

    Branches of one `if`/`try` bind at most once at runtime, and a bare
    annotation binds nothing, so neither is a duplicate.
    """
    names = _bindings(ast.parse(source).body)

    duplicates = [name for name, count in Counter(names).items() if count > 1]
    assert not duplicates, f"{label}: guard reported a false duplicate {duplicates}"


@pytest.mark.parametrize("module", ["logs.py", "constants.py"])
def test_no_module_level_name_is_bound_twice(module: str) -> None:
    path = _EVALS / module

    names = _module_level_bindings(path)

    assert names, f"{module}: parsed no module-level bindings at all"
    duplicates = sorted(name for name, count in Counter(names).items() if count > 1)
    assert not duplicates, f"{module} binds these names more than once: {duplicates}"
