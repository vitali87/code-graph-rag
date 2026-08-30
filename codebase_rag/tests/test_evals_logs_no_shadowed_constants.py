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


def _module_level_bindings(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.Assign):
            names.extend(
                target.id for target in node.targets if isinstance(target, ast.Name)
            )
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.append(node.target.id)
    return names


@pytest.mark.parametrize("module", ["logs.py", "constants.py"])
def test_no_module_level_name_is_bound_twice(module: str) -> None:
    path = _EVALS / module

    names = _module_level_bindings(path)

    assert names, f"{module}: parsed no module-level bindings at all"
    duplicates = sorted(name for name, count in Counter(names).items() if count > 1)
    assert not duplicates, f"{module} binds these names more than once: {duplicates}"
