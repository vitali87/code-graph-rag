"""No module-level constant in ``evals/constants.py`` may be assigned twice.

A second assignment silently wins, so the earlier one is dead code that still
reads as authoritative. ``CPP_SUFFIXES`` was defined at two places whose values
differed by ``.c``: a reader who found the first definition would have been
wrong about what every consumer actually receives, and nothing anywhere would
have contradicted them (issue #1190).

Structural rather than a spot check on one name, because the defect is the
shadowing itself rather than that particular constant.
"""

from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path

_CONSTANTS = Path(__file__).resolve().parents[2] / "evals" / "constants.py"


def _module_level_assignment_counts() -> Counter[str]:
    tree = ast.parse(_CONSTANTS.read_text(encoding="utf-8"))
    names: Counter[str] = Counter()
    # Module level only: a class body is its own scope, so a name reused
    # inside one shadows nothing at module level.
    for node in tree.body:
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        for target in targets:
            if isinstance(target, ast.Name):
                names[target.id] += 1
    return names


def test_the_constants_module_exists_and_parses() -> None:
    # Guards the guard: a moved or renamed file would make the assertion below
    # vacuous by leaving the counter empty, which reads as "no duplicates".
    assert _CONSTANTS.is_file(), f"eval constants not found at {_CONSTANTS}"
    assert _module_level_assignment_counts(), "no module-level constants parsed"


def test_no_constant_is_assigned_twice() -> None:
    duplicates = {n: c for n, c in _module_level_assignment_counts().items() if c > 1}

    assert not duplicates, (
        "assigned more than once in evals/constants.py, so the later "
        f"assignment silently wins: {sorted(duplicates)}"
    )
