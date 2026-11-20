"""The JARVIS micro-benchmark scorer (evals/jarvis_micro.py).

Its numbers are only comparable with the paper's if cgr's edges are spelled
the way the ground truth spells them, and if both sides are cut to the same
first-party edges. These pin both on a dataset laid out like the real one,
so no test needs the network.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from codebase_rag.parser_loader import load_parsers
from evals import constants as ec
from evals.jarvis_micro import (
    Score,
    add,
    cgr_edges,
    modules,
    programs,
    score_program,
    truth_edges,
)

# imports/init_func_import from the benchmark, with a class and a builtin
# call added: the module calls as `main`, a package as its own name, and a
# class call lands on its `__init__`.
PROGRAM = {
    "main.py": (
        "from nested import func, func2\n"
        "class A:\n    def __init__(self):\n        print('x')\n"
        "func()\nfunc2()\nA()\n"
    ),
    "nested/__init__.py": "from .mod import func2\n\n\ndef func():\n    pass\n",
    "nested/mod.py": "def func2():\n    pass\n",
}
TRUTH = {
    "main": ["nested.func", "nested.mod.func2", "main.A.__init__"],
    "main.A.__init__": ["<builtin>.print"],
    "nested": [],
    "nested.func": [],
    "nested.mod": [],
    "nested.mod.func2": [],
}
EXPECTED = {
    ("main", "nested.func"),
    ("main", "nested.mod.func2"),
    ("main", "main.A.__init__"),
}


def _dataset(root: Path) -> Path:
    source = root / ec.JARVIS_PROGRAMS_DIR / "imports" / "init_func_import"
    for rel, text in PROGRAM.items():
        (source / rel).parent.mkdir(parents=True, exist_ok=True)
        (source / rel).write_text(text, encoding="utf-8")
    truth = root / ec.JARVIS_TRUTH_DIR / "imports" / "init_func_import"
    truth.mkdir(parents=True)
    (truth / ec.JARVIS_TRUTH_FILE).write_text(json.dumps(TRUTH), encoding="utf-8")
    # The real dataset spells one category with a space in its programs and an
    # underscore in its ground truth.
    spaced = root / ec.JARVIS_PROGRAMS_DIR / "new_direct calls" / "class_call"
    spaced.mkdir(parents=True)
    (spaced / "main.py").write_text("", encoding="utf-8")
    for rel in ("new_direct_calls/class_call", "imports/gone"):
        # `imports/gone` has a ground truth and no program, so is not scored.
        (root / ec.JARVIS_TRUTH_DIR / rel).mkdir(parents=True)
        (root / ec.JARVIS_TRUTH_DIR / rel / ec.JARVIS_TRUTH_FILE).write_text(
            "{}", encoding="utf-8"
        )
    return root


def test_programs_pair_each_ground_truth_with_its_source(tmp_path: Path) -> None:
    found = programs(_dataset(tmp_path))
    assert [(p.category, p.name, p.source.parent.name) for p in found] == [
        ("imports", "init_func_import", "imports"),
        ("new_direct_calls", "class_call", "new_direct calls"),
    ]


def test_a_package_is_named_like_the_ground_truth_names_it(tmp_path: Path) -> None:
    program = programs(_dataset(tmp_path))[0]
    assert modules(program.source) == {"main", "nested", "nested.mod"}


def test_only_first_party_edges_are_scored(tmp_path: Path) -> None:
    program = programs(_dataset(tmp_path))[0]
    assert truth_edges(program.truth, modules(program.source)) == EXPECTED


def test_cgr_edges_are_spelled_like_the_ground_truth(tmp_path: Path) -> None:
    program = programs(_dataset(tmp_path))[0]
    parsers, queries = load_parsers()
    assert cgr_edges(program.source, modules(program.source), parsers, queries) == (
        EXPECTED
    )


@pytest.mark.parametrize(
    ("found", "precision", "recall"),
    [
        (EXPECTED, 1.0, 1.0),
        ({("main", "nested.func"), ("main", "main.other")}, 0.5, 1 / 3),
        # Claiming no edge claims no false one either.
        (set(), 1.0, 0.0),
    ],
)
def test_score(found: set[tuple[str, str]], precision: float, recall: float) -> None:
    s = score_program(found, EXPECTED)
    assert (s.precision, s.recall) == pytest.approx((precision, recall))
    # A program with no first-party call (`builtins/functions` calls only
    # builtins) scores perfect rather than dividing by zero.
    assert Score(0, 0, 0).precision == Score(0, 0, 0).recall == 1.0
    assert add(s, Score(1, 2, 3)) == Score(s.tp + 1, s.fp + 2, s.fn + 3)
