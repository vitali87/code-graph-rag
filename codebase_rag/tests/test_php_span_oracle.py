# (H) Covers PHP node SPAN (end_line) validation: cgr's end_line for each node is
# (H) graded against the php-parser oracle (which emits node.loc.end.line), joined
# (H) on (kind, file, start). Exercises a class with a multi-line method, an
# (H) interface, an enum, and a multi-line function so spans are not single line.
from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.parser_loader import load_parsers
from evals import constants as ec
from evals.cgr_graph import extract_cgr_php_graph
from evals.oracles import php_oracle_available, run_php_oracle
from evals.score import score_span

PHP_SRC = """\
<?php

class Widget implements Shape
{
    private int $size = 0;

    public function area(
        int $scale
    ): int {
        return $this->size * $scale;
    }
}

interface Shape
{
    public function area(int $scale): int;
}

enum Color
{
    case Red;
    case Green;
}

function standalone(int $a): int
{
    return $a + 1;
}
"""


def _require_php() -> None:
    if not php_oracle_available():
        pytest.skip("node/npm toolchain not available")
    if cs.SupportedLanguage.PHP not in load_parsers()[0]:
        pytest.skip("php parser not available")


def test_cgr_matches_php_parser_oracle_on_node_spans(tmp_path: Path) -> None:
    _require_php()
    project = tmp_path / "php_span_test"
    project.mkdir()
    (project / "lib.php").write_text(PHP_SRC, encoding="utf-8")

    cgr = extract_cgr_php_graph(project, project.name)
    oracle = run_php_oracle(project)

    result = score_span(cgr, oracle, ec.PHP_SCORED_NODE_KINDS)
    by_label = {row["label"]: row for row in result.rows}
    aggregate = by_label.get(ec.AGGREGATE_LABEL)
    assert aggregate is not None, (by_label, result.diff)
    assert aggregate["precision"] == 1.0 and aggregate["recall"] == 1.0, (
        aggregate,
        result.diff,
    )
    assert aggregate["tp"] >= 4, aggregate
