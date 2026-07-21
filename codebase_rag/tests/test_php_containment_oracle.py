# Covers PHP containment-edge validation: cgr's DEFINES (file module ->
# every named type and top-level function) and DEFINES_METHOD (class/
# interface/trait/enum -> method) edges are graded against the independent
# php-parser oracle, joined on (kind, file, line). Exercises an interface,
# a trait, an enum with a method, a class, and a free function.
from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.parser_loader import load_parsers
from evals import constants as ec
from evals.cgr_graph import extract_cgr_php_graph
from evals.oracles import php_oracle_available, run_php_oracle
from evals.score import score_edge_types

PHP_SRC = """\
<?php
namespace App;

interface Shape { public function area(): float; }

trait Greet { public function hello() { return "hi"; } }

enum Suit { case Hearts; public function label(): string { return "h"; } }

class Point implements Shape {
    public function __construct(public int $x) {}
    public function area(): float { return 1.0; }
}

function freeFn(int $a): int { return $a + 1; }
"""


def _require_php() -> None:
    if not php_oracle_available():
        pytest.skip("node/npm toolchain not available")
    if cs.SupportedLanguage.PHP not in load_parsers()[0]:
        pytest.skip("php parser not available")


def test_cgr_matches_php_parser_oracle_on_containment_edges(tmp_path: Path) -> None:
    _require_php()
    project = tmp_path / "php_edge"
    project.mkdir()
    (project / "lib.php").write_text(PHP_SRC, encoding="utf-8")

    cgr = extract_cgr_php_graph(project, project.name)
    oracle = run_php_oracle(project)

    result = score_edge_types(cgr, oracle, ec.SCORED_EDGE_TYPES)
    by_label = {row["label"]: row for row in result.rows}
    for label in (
        cs.RelationshipType.DEFINES.value,
        cs.RelationshipType.DEFINES_METHOD.value,
    ):
        row = by_label.get(label)
        assert row is not None, (label, by_label, result.diff)
        assert row["precision"] == 1.0 and row["recall"] == 1.0, (
            label,
            row,
            result.diff,
        )
