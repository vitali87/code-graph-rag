import socket
from pathlib import Path

import pytest
from typer.testing import CliRunner

from codebase_rag.cli import app
from codebase_rag.dead_code import (
    _is_js_well_known_symbol_root,
    is_well_known_symbol_member,
)
from evals.dead_code import cgr_dead_code, default_dead_code_config


def is_memgraph_up() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", 7687), timeout=0.1):
            return True
    except OSError:
        return False


skip_if_no_memgraph = pytest.mark.skipif(
    not is_memgraph_up(), reason="Memgraph is unreachable"
)


def test_is_well_known_symbol_member() -> None:
    # 4a: Unit tests for the predicate
    assert is_well_known_symbol_member("[Symbol.iterator]") is True
    assert is_well_known_symbol_member("Box.[Symbol.toStringTag]") is True
    assert is_well_known_symbol_member("A.B.[Symbol.asyncIterator]") is True
    assert is_well_known_symbol_member("[Symbol.hasInstance]") is True
    assert is_well_known_symbol_member("[Symbol.toPrimitive]") is True
    assert is_well_known_symbol_member("[Symbol.dispose]") is True

    assert is_well_known_symbol_member("iterator") is False
    assert is_well_known_symbol_member("Box.iterator") is False
    assert is_well_known_symbol_member("toStringTag") is False
    assert is_well_known_symbol_member("[Symbol.for('x')]") is False
    assert is_well_known_symbol_member("[mySym]") is False
    assert is_well_known_symbol_member("[Symbol.notARealSymbol]") is False
    assert is_well_known_symbol_member("[Symbol.iterator]Extra") is False


def test_method_symbol_root_predicate_is_allowlisted() -> None:
    # The METHOD-gated predicate must reject application registry symbols
    # too: rooting [Symbol.for('app.tag')] hides dead protocol members.
    assert _is_js_well_known_symbol_root("[Symbol.iterator]", True, "a.ts") is True
    assert _is_js_well_known_symbol_root("[ Symbol.iterator ]", True, "a.ts") is True
    assert _is_js_well_known_symbol_root('[Symbol["iterator"]]', True, "a.ts") is True
    assert (
        _is_js_well_known_symbol_root(
            "[Symbol.for('nodejs.util.inspect.custom')]", True, "a.ts"
        )
        is True
    )

    assert (
        _is_js_well_known_symbol_root("[Symbol.for('app.tag')]", True, "a.ts") is False
    )
    assert (
        _is_js_well_known_symbol_root('[Symbol.for("app.tag")]', True, "a.ts") is False
    )
    assert (
        _is_js_well_known_symbol_root("[Symbol.notARealSymbol]", True, "a.ts") is False
    )
    assert _is_js_well_known_symbol_root("[Symbol.iterator]", False, "a.ts") is False
    assert _is_js_well_known_symbol_root("[Symbol.iterator]", True, "a.py") is False
    assert _is_js_well_known_symbol_root("[mySym]", True, "a.ts") is False


@skip_if_no_memgraph
def test_javascript_well_known_symbols_liveness_roots(tmp_path: Path) -> None:
    # 4b + 4c: Parser/graph-shape test & DB-backed e2e test
    # Copy fixtures to temp repo
    fixtures_dir = Path("fixtures/well_known_symbols")
    if not fixtures_dir.exists():
        pytest.skip(f"Fixtures not found at {fixtures_dir}")

    for file in fixtures_dir.iterdir():
        if file.is_file():
            (tmp_path / file.name).write_text(
                file.read_text(encoding="utf-8"), encoding="utf-8"
            )

    dead = cgr_dead_code(tmp_path, "proj", default_dead_code_config(False, False))

    # ABSENT (fix working):
    for qn in dead:
        assert not qn.endswith("[Symbol.toStringTag]"), (
            f"Expected [Symbol.toStringTag] to be live, but found {qn} in dead code"
        )
        assert not qn.endswith("[Symbol.iterator]"), (
            f"Expected [Symbol.iterator] to be live, but found {qn} in dead code"
        )
        assert not qn.endswith("[Symbol.asyncIterator]"), (
            f"Expected [Symbol.asyncIterator] to be live, but found {qn} in dead code"
        )
        assert not qn.endswith("[Symbol.hasInstance]"), (
            f"Expected [Symbol.hasInstance] to be live, but found {qn} in dead code"
        )

    # PRESENT (no over-suppression):
    assert any("neverCalled" in qn for qn in dead), (
        "neverCalled (box.js) missing from dead code report"
    )
    assert any("iterator" in qn and "[Symbol" not in qn for qn in dead), (
        "plain iterator method (iterables.ts) missing from dead code report"
    )

    # PRESENT (negatives still dead):
    assert any("[Symbol.for('app.tag')]" in qn for qn in dead), (
        "[Symbol.for('app.tag')] missing from dead code report"
    )
    assert any("[local]" in qn for qn in dead), "[local] missing from dead code report"

    # usedHelper and add never appear
    assert not any("usedHelper" in qn for qn in dead), (
        "usedHelper is referenced and should not be dead"
    )
    assert not any("add" in qn for qn in dead), (
        "add is referenced and should not be dead"
    )


def test_dead_code_cli_smoke() -> None:
    # 4c: Smoke test
    runner = CliRunner()
    result = runner.invoke(app, ["dead-code", "--help"])
    assert result.exit_code == 0
    assert result.output.strip()
