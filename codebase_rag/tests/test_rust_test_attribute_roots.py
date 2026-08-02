# Rust unit tests live INSIDE source files (`#[cfg(test)] mod tests` with
# `#[test]` functions), so path-based test detection never sees them: the
# tests and the production helpers only they exercise were reported dead
# despite --include-tests (ripgrep: 459 of 1811 candidates). A `#[test]`
# family attribute must root its function when tests are included, and mark
# it as test code to exclude when they are not. Issue #1008.
from __future__ import annotations

from codebase_rag import constants as cs
from codebase_rag import cypher_queries as cq
from codebase_rag.dead_code import collect_dead_code, default_dead_code_config
from codebase_rag.types_defs import ResultRow

_FUNCTION = cs.NodeLabel.FUNCTION.value


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


def _function(
    qn: str, name: str, path: str, decorators: list[str] | None = None
) -> ResultRow:
    return {
        "label": _FUNCTION,
        "qualified_name": qn,
        "name": name,
        "path": path,
        "start_line": 1,
        "end_line": 2,
        "decorators": decorators or [],
        "is_exported": False,
        "overrides_external": False,
    }


def _calls(from_qn: str, to_qn: str) -> ResultRow:
    return {
        "from_label": _FUNCTION,
        "from_qn": from_qn,
        "rel_type": cs.RelationshipType.CALLS.value,
        "to_label": _FUNCTION,
        "to_qn": to_qn,
    }


def _collect(
    nodes: list[ResultRow],
    rels: list[ResultRow] | None = None,
    include_tests: bool = True,
) -> set[str]:
    rows = collect_dead_code(
        FakeIngestor(nodes, rels or []),
        "proj",
        default_dead_code_config(include_tests=include_tests, include_classes=False),
    )
    return {row["qualified_name"] for row in rows}


def test_test_attribute_roots_function_and_its_callees() -> None:
    # The qns carry NO `tests` module segment, so the ATTRIBUTE alone must
    # decide (a `tests` segment in the fixtures would satisfy the module
    # rule first and leave the decorator branch untested).
    dead = _collect(
        [
            _function(
                "proj.src.lib.test_add",
                "test_add",
                "src/lib.rs",
                decorators=["#[test]"],
            ),
            _function(
                "proj.src.lib.helper_only_used_by_tests",
                "helper_only_used_by_tests",
                "src/lib.rs",
            ),
        ],
        rels=[
            _calls(
                "proj.src.lib.test_add",
                "proj.src.lib.helper_only_used_by_tests",
            )
        ],
    )
    assert "proj.src.lib.test_add" not in dead
    assert "proj.src.lib.helper_only_used_by_tests" not in dead


def test_scoped_test_attributes_root() -> None:
    dead = _collect(
        [
            _function(
                "proj.src.lib.test_async",
                "test_async",
                "src/lib.rs",
                decorators=['#[tokio::test(flavor = "multi_thread")]'],
            ),
            _function(
                "proj.src.lib.bench_add",
                "bench_add",
                "src/lib.rs",
                decorators=["#[bench]"],
            ),
        ]
    )
    assert dead == set()


def test_tests_module_symbols_root_without_attributes() -> None:
    # The module rule on its own: a plain helper inside a `tests` module
    # is test code even with no attribute anywhere.
    dead = _collect(
        [_function("proj.src.lib.tests.mk_input", "mk_input", "src/lib.rs")]
    )
    assert dead == set()


def test_plain_function_still_reports() -> None:
    dead = _collect([_function("proj.src.lib.orphan", "orphan", "src/lib.rs")])
    assert "proj.src.lib.orphan" in dead


def test_non_test_attribute_does_not_root() -> None:
    dead = _collect(
        [
            _function(
                "proj.src.lib.orphan",
                "orphan",
                "src/lib.rs",
                decorators=["#[inline]"],
            )
        ]
    )
    assert "proj.src.lib.orphan" in dead


def test_exclude_tests_suppresses_inline_test_symbols() -> None:
    # With tests excluded, an inline #[test] function (decorator-decided,
    # no `tests` segment) and a helper in a `mod tests` (module-decided)
    # are infrastructure, not dead production code: not roots, not
    # candidates.
    dead = _collect(
        [
            _function(
                "proj.src.lib.test_add",
                "test_add",
                "src/lib.rs",
                decorators=["#[test]"],
            ),
            _function(
                "proj.src.lib.tests.mk_input",
                "mk_input",
                "src/lib.rs",
            ),
        ],
        include_tests=False,
    )
    assert dead == set()


def test_test_attribute_on_non_rust_path_does_not_root() -> None:
    dead = _collect(
        [
            _function(
                "proj.app.orphan",
                "orphan",
                "app.py",
                decorators=["#[test]"],
            )
        ]
    )
    assert "proj.app.orphan" in dead


def test_symbol_named_tests_is_not_test_code() -> None:
    # `tests` must match MODULE segments only: a production method named
    # `tests` is ordinary Rust, and rooting it would hide its whole
    # callee closure from the report.
    dead = _collect(
        [
            _function("proj.src.lib.Suite.tests", "tests", "src/lib.rs"),
            _function("proj.src.lib.Suite.normalise", "normalise", "src/lib.rs"),
            _function("proj.src.lib.Suite.tally", "tally", "src/lib.rs"),
        ],
        rels=[
            _calls("proj.src.lib.Suite.tests", "proj.src.lib.Suite.normalise"),
            _calls("proj.src.lib.Suite.tests", "proj.src.lib.Suite.tally"),
        ],
    )
    assert "proj.src.lib.Suite.tests" in dead
    assert "proj.src.lib.Suite.normalise" in dead
    assert "proj.src.lib.Suite.tally" in dead


def test_project_named_tests_still_reports() -> None:
    # The project prefix is not a module segment: a project directory
    # named `tests` must not silence Rust dead-code reporting wholesale.
    rows = collect_dead_code(
        FakeIngestor([_function("tests.src.lib.orphan", "orphan", "src/lib.rs")], []),
        "tests",
        default_dead_code_config(include_tests=True, include_classes=False),
    )
    assert {row["qualified_name"] for row in rows} == {"tests.src.lib.orphan"}


def test_singular_test_module_matches_plural() -> None:
    # cs.TEST_PATH_PATTERNS covers both /tests/ and /test/ directories;
    # the inline-module rule mirrors it, so `mod test` behaves exactly
    # like `mod tests` on both flag polarities.
    nodes = [
        _function(
            "proj.src.lib.test.t_basic",
            "t_basic",
            "src/lib.rs",
            decorators=["#[test]"],
        ),
        _function("proj.src.lib.test.mk_input", "mk_input", "src/lib.rs"),
    ]
    rels = [_calls("proj.src.lib.test.t_basic", "proj.src.lib.test.mk_input")]
    assert _collect(nodes, rels) == set()
    assert _collect(nodes, rels, include_tests=False) == set()
