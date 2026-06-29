from pathlib import Path

import pytest

from evals import constants as ec
from evals.oracles import php_oracle_available
from evals.php_retrieval import (
    cgr_php_call_edges,
    oracle_php_call_edges,
    score_php_retrieval,
)

needs_node = pytest.mark.skipif(
    not php_oracle_available(), reason="node toolchain not installed"
)


def _make_project(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "util.php").write_text(
        "<?php\nfunction free(): int { return 2; }\n",
        encoding="utf-8",
    )
    (root / "T.php").write_text(
        "<?php\nclass T {\n"
        "    public function helper(): int { return 1; }\n"
        "    public function caller(): int { return $this->helper(); }\n"
        "    public static function make(): int { return 3; }\n"
        "    public function orphan(): int { return 9; }\n"
        "}\n",
        encoding="utf-8",
    )
    (root / "use.php").write_text(
        "<?php\nfunction useIt(): int {\n"
        "    $t = new T();\n"
        "    return free() + T::make() + $t->caller();\n"
        "}\n",
        encoding="utf-8",
    )


@needs_node
def test_oracle_captures_first_party_php_calls(tmp_path: Path) -> None:
    _make_project(tmp_path)
    edges, declared = oracle_php_call_edges(tmp_path)

    # (H) $this->helper(), free(), T::make(), $t->caller() are first-party calls.
    assert ("T.php", "helper") in edges
    assert ("use.php", "free") in edges
    assert ("use.php", "make") in edges
    assert ("use.php", "caller") in edges
    # (H) orphan is declared but never called -> never a call edge.
    assert ("T.php", "orphan") not in edges
    assert {"helper", "caller", "make", "free", "orphan", "useIt"} <= declared


@needs_node
def test_cgr_matches_oracle_on_clean_php_project(tmp_path: Path) -> None:
    _make_project(tmp_path)
    oracle, declared = oracle_php_call_edges(tmp_path)
    cgr = cgr_php_call_edges(tmp_path, tmp_path.name, declared)
    assert cgr == oracle


@needs_node
def test_php_dynamic_member_call_not_emitted(tmp_path: Path) -> None:
    # (H) A dynamic member call (`$this->$method()`) has a `variable` offset whose
    # (H) name is the variable identifier ("method"), not a static method name. The
    # (H) oracle must not emit it as a call edge even when it collides with a
    # (H) declared first-party method name, or it becomes a false ground-truth edge.
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "c.php").write_text(
        "<?php\nclass C {\n"
        "    public function method(): int { return 1; }\n"
        "    public function go(): int { $method = 'x'; return $this->$method(); }\n"
        "}\n",
        encoding="utf-8",
    )
    edges, declared = oracle_php_call_edges(tmp_path)
    assert "method" in declared
    assert ("c.php", "method") not in edges


def test_score_php_retrieval_prf() -> None:
    result = score_php_retrieval(
        {("a.php", "f"), ("a.php", "g")}, {("a.php", "f"), ("b.php", "h")}
    )
    row = next(r for r in result.rows if r["label"] == ec.PHP_RETRIEVAL_LABEL)
    assert (row["tp"], row["fp"], row["fn"]) == (1, 1, 1)
