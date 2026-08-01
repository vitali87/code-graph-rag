# Exercises the PHP structure oracle harness (evals/oracles/php_oracle +
# evals/php_l1.py): the php-parser oracle is ground truth, cgr's PHP nodes are
# graded against it on (kind, file, start_line). Includes an attributed class
# (span starts at the attribute) and an anonymous class (methods modelled as
# Functions).
from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.parser_loader import load_parsers
from evals import constants as ec
from evals.cgr_graph import extract_cgr_php_nodes
from evals.oracles import php_oracle_available, run_php_oracle
from evals.score import score_node_kinds
from evals.types_defs import GraphData

PHP_SRC = """\
<?php
namespace Demo;

interface Shape { public function area(): float; }

trait Greet { public function hello() { return "hi"; } }

enum Suit { case Hearts; case Spades; }

#[\\AllowDynamicProperties]
class Point implements Shape {
    public function __construct(public int $x) {}
    public function area(): float { return 1.0; }
}

function freeFn(int $a): int { return $a + 1; }

function makeHandler(): Shape {
    return new class implements Shape {
        public function area(): float { return 0.0; }
    };
}
"""


def _require_php() -> None:
    if not php_oracle_available():
        pytest.skip("node/npm toolchain not available")
    if cs.SupportedLanguage.PHP not in load_parsers()[0]:
        pytest.skip("php parser not available")


def test_cgr_matches_php_parser_oracle_on_php_structure(tmp_path: Path) -> None:
    _require_php()
    project = tmp_path / "php_oracle_test"
    project.mkdir()
    (project / "sample.php").write_text(PHP_SRC, encoding="utf-8")

    cgr = GraphData(
        nodes=extract_cgr_php_nodes(project, project.name),
        edges=set(),
        name_edges=set(),
    )
    oracle = run_php_oracle(project)

    result = score_node_kinds(cgr, oracle, ec.PHP_SCORED_NODE_KINDS)
    by_label = {row["label"]: row for row in result.rows}
    for label in ("Class", "Interface", "Enum", "Method", "Function"):
        row = by_label.get(label)
        assert row is not None, (label, by_label)
        assert row["precision"] == 1.0 and row["recall"] == 1.0, (label, row)
