# A `#[cfg(test)]` gate on a Rust mod declaration marks the TARGET module
# as test code, whatever its name: ripgrep's `#[cfg(test)] mod testutil;`
# compiles testutil.rs only for tests, yet its helpers were reported as
# dead production code because only `tests`/`test` NAMES counted. The gate
# is recorded on the Module node's decorators at parse time; dead-code
# treats any symbol under a gated module as test code, exactly as it does
# for name-matched modules. Issue #1010.
from __future__ import annotations

from codebase_rag import constants as cs
from codebase_rag import cypher_queries as cq
from codebase_rag.dead_code import collect_dead_code, default_dead_code_config
from codebase_rag.types_defs import ResultRow

_FUNCTION = cs.NodeLabel.FUNCTION.value
_MODULE = cs.NodeLabel.MODULE.value


class FakeIngestor:
    def __init__(self, nodes: list[ResultRow], rels: list[ResultRow]) -> None:
        self._nodes = nodes
        self._rels = rels

    def fetch_all(
        self, query: str, params: dict[str, str] | None = None
    ) -> list[ResultRow]:
        if query == cq.CYPHER_DEAD_CODE_NODES:
            return self._nodes
        return self._rels


def _function(qn: str, name: str, path: str) -> ResultRow:
    return {
        "label": _FUNCTION,
        "qualified_name": qn,
        "name": name,
        "path": path,
        "start_line": 1,
        "end_line": 2,
        "decorators": [],
        "is_exported": False,
        "overrides_external": False,
    }


def _module(qn: str, path: str, decorators: list[str] | None = None) -> ResultRow:
    return {
        "label": _MODULE,
        "qualified_name": qn,
        "name": qn.rsplit(".", 1)[-1],
        "path": path,
        "start_line": 1,
        "end_line": 1,
        "decorators": decorators or [],
        "is_exported": False,
        "overrides_external": False,
    }


def _collect(nodes: list[ResultRow], include_tests: bool) -> set[str]:
    rows = collect_dead_code(
        FakeIngestor(nodes, []),
        "proj",
        default_dead_code_config(include_tests=include_tests, include_classes=False),
    )
    return {row["qualified_name"] for row in rows}


def _gated_fixture() -> list[ResultRow]:
    # `#[cfg(test)] mod testutil;` in lib.rs: the gate lands on the target
    # Module node's decorators; the module name matches NO test spelling.
    return [
        _module("proj.src.testutil", "src/testutil.rs", ["#[cfg(test)]"]),
        _function("proj.src.testutil.fixture", "fixture", "src/testutil.rs"),
        _function(
            "proj.src.testutil.Helper.unused_by_name",
            "unused_by_name",
            "src/testutil.rs",
        ),
    ]


def test_cfg_test_gated_module_symbols_root_with_tests_included() -> None:
    dead = _collect(_gated_fixture(), include_tests=True)
    assert "proj.src.testutil.fixture" not in dead
    assert "proj.src.testutil.Helper.unused_by_name" not in dead


def test_cfg_test_gated_module_symbols_excluded_without_tests() -> None:
    dead = _collect(_gated_fixture(), include_tests=False)
    assert "proj.src.testutil.fixture" not in dead
    assert "proj.src.testutil.Helper.unused_by_name" not in dead


def test_ungated_module_still_reports_dead_symbols() -> None:
    # The same shape without the gate stays reportable: the new branch
    # must not silence ordinary modules.
    nodes = [
        _module("proj.src.testutil", "src/testutil.rs"),
        _function("proj.src.testutil.fixture", "fixture", "src/testutil.rs"),
    ]
    dead = _collect(nodes, include_tests=True)
    assert "proj.src.testutil.fixture" in dead


def test_cfg_test_attribute_matches_through_whitespace() -> None:
    # Attributes are token streams: `#[cfg( test )]` names the same gate.
    nodes = [
        _module("proj.src.testutil", "src/testutil.rs", ["#[cfg( test )]"]),
        _function("proj.src.testutil.fixture", "fixture", "src/testutil.rs"),
    ]
    dead = _collect(nodes, include_tests=True)
    assert "proj.src.testutil.fixture" not in dead


def test_cfg_feature_gate_does_not_mark_test_code() -> None:
    # Only the test cfg counts: a feature gate is production code.
    nodes = [
        _module("proj.src.simd", "src/simd.rs", ['#[cfg(feature = "simd")]']),
        _function("proj.src.simd.accelerate", "accelerate", "src/simd.rs"),
    ]
    dead = _collect(nodes, include_tests=True)
    assert "proj.src.simd.accelerate" in dead
