# Covers PHP inheritance-edge validation: cgr's INHERITS (class/interface
# extends) and IMPLEMENTS (class implements) edges are graded against the
# php-parser oracle, by (source node, base SIMPLE NAME).
from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.parser_loader import load_parsers
from evals import constants as ec
from evals.cgr_graph import extract_cgr_php_graph
from evals.oracles import php_oracle_available, run_php_oracle
from evals.score import score_name_edge_types

PHP_SRC = """\
<?php
namespace App;

interface Shape {}
interface Drawable {}
interface Big extends Shape, Drawable {}

class Base {}
class Circle extends Base implements Shape, Drawable {}
"""


def _require_php() -> None:
    if not php_oracle_available():
        pytest.skip("node/npm toolchain not available")
    if cs.SupportedLanguage.PHP not in load_parsers()[0]:
        pytest.skip("php parser not available")


def test_cgr_matches_php_parser_oracle_on_inheritance_edges(tmp_path: Path) -> None:
    _require_php()
    project = tmp_path / "php_inh_edge"
    project.mkdir()
    (project / "lib.php").write_text(PHP_SRC, encoding="utf-8")

    cgr = extract_cgr_php_graph(project, project.name)
    oracle = run_php_oracle(project)

    result = score_name_edge_types(cgr, oracle, ec.INHERITANCE_NAME_EDGE_TYPES)
    by_label = {row["label"]: row for row in result.rows}
    for label in (
        cs.RelationshipType.INHERITS.value,
        cs.RelationshipType.IMPLEMENTS.value,
    ):
        row = by_label.get(label)
        assert row is not None, (label, by_label, result.diff)
        assert row["precision"] == 1.0 and row["recall"] == 1.0, (
            label,
            row,
            result.diff,
        )
