# A `#[cfg(test)]` gate on a Rust mod declaration marks the TARGET module
# as test code, whatever its name: ripgrep's `#[cfg(test)] mod testutil;`
# compiles testutil.rs only for tests, yet its helpers were reported as
# dead production code because only `tests`/`test` NAMES counted. The gate
# is recorded at parse time (target-qn candidates on the DECLARING module,
# own decorators on bodied inline mods); dead-code treats any symbol under
# a gated module as test code, exactly as it does for name-matched
# modules. Issue #1010.
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


def _module(
    qn: str,
    path: str,
    decorators: list[str] | None = None,
    rust_cfg_test_mods: list[str] | None = None,
    rust_ungated_mods: list[str] | None = None,
) -> ResultRow:
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
        "rust_cfg_test_mods": rust_cfg_test_mods or [],
        "rust_ungated_mods": rust_ungated_mods or [],
    }


def _collect(nodes: list[ResultRow], include_tests: bool) -> set[str]:
    rows = collect_dead_code(
        FakeIngestor(nodes, []),
        "proj",
        default_dead_code_config(include_tests=include_tests, include_classes=False),
    )
    return {row["qualified_name"] for row in rows}


def _gated_fixture() -> list[ResultRow]:
    # `#[cfg(test)] mod testutil;` in lib.rs: the DECLARING module records
    # the target qn; the target module's name matches NO test spelling.
    return [
        _module("proj.src.lib", "src/lib.rs", rust_cfg_test_mods=["proj.src.testutil"]),
        _module("proj.src.testutil", "src/testutil.rs"),
        _function("proj.src.testutil.fixture", "fixture", "src/testutil.rs"),
        _function(
            "proj.src.testutil.Helper.unused_by_name",
            "unused_by_name",
            "src/testutil.rs",
        ),
    ]


def test_declared_gate_roots_target_symbols_with_tests_included() -> None:
    dead = _collect(_gated_fixture(), include_tests=True)
    assert "proj.src.testutil.fixture" not in dead
    assert "proj.src.testutil.Helper.unused_by_name" not in dead


def test_declared_gate_excludes_target_symbols_without_tests() -> None:
    dead = _collect(_gated_fixture(), include_tests=False)
    assert "proj.src.testutil.fixture" not in dead
    assert "proj.src.testutil.Helper.unused_by_name" not in dead


def test_ungated_module_still_reports_dead_symbols() -> None:
    # The same shape without the gate stays reportable: the new branch
    # must not silence ordinary modules.
    nodes = [
        _module("proj.src.lib", "src/lib.rs"),
        _module("proj.src.testutil", "src/testutil.rs"),
        _function("proj.src.testutil.fixture", "fixture", "src/testutil.rs"),
    ]
    dead = _collect(nodes, include_tests=True)
    assert "proj.src.testutil.fixture" in dead


def test_declared_candidate_naming_no_real_module_stays_inert() -> None:
    # A candidate qn no Module node backs (a #[path] override moved the
    # file elsewhere) must not suppress same-shaped symbols.
    nodes = [
        _module("proj.src.lib", "src/lib.rs", rust_cfg_test_mods=["proj.src.ghost"]),
        _function("proj.src.ghost.fixture", "fixture", "src/ghost.rs"),
    ]
    dead = _collect(nodes, include_tests=True)
    assert "proj.src.ghost.fixture" in dead


def test_ungated_declaration_from_another_target_wins() -> None:
    # src/lib.rs gates `mod util;` for tests while src/main.rs declares the
    # SAME file module ungated: the bin target compiles it as production
    # code, so the gate must not hide its symbols in either mode.
    nodes = [
        _module("proj.src.lib", "src/lib.rs", rust_cfg_test_mods=["proj.src.util"]),
        _module("proj.src.main", "src/main.rs", rust_ungated_mods=["proj.src.util"]),
        _module("proj.src.util", "src/util.rs"),
        _function("proj.src.util.helper", "helper", "src/util.rs"),
    ]
    dead = _collect(nodes, include_tests=True)
    assert "proj.src.util.helper" in dead
    dead = _collect(nodes, include_tests=False)
    assert "proj.src.util.helper" in dead


def test_own_cfg_test_decorator_marks_inline_module() -> None:
    # A bodied inline gated mod carries the gate on its own node; the
    # whitespace-variant spelling names the same attribute.
    nodes = [
        _module("proj.src.lib.checks", "src/lib.rs", decorators=["#[cfg( test )]"]),
        _function("proj.src.lib.checks.fixture", "fixture", "src/lib.rs"),
    ]
    dead = _collect(nodes, include_tests=True)
    assert "proj.src.lib.checks.fixture" not in dead


def test_cfg_feature_gate_does_not_mark_test_code() -> None:
    # Only the test cfg counts: a feature gate is production code.
    nodes = [
        _module("proj.src.simd", "src/simd.rs", ['#[cfg(feature = "simd")]']),
        _function("proj.src.simd.accelerate", "accelerate", "src/simd.rs"),
    ]
    dead = _collect(nodes, include_tests=True)
    assert "proj.src.simd.accelerate" in dead
