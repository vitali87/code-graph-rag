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


def _function(
    qn: str,
    name: str,
    path: str,
    decorators: list[str] | None = None,
    start_line: int = 1,
    end_line: int = 2,
) -> ResultRow:
    return {
        "label": _FUNCTION,
        "qualified_name": qn,
        "name": name,
        "path": path,
        "start_line": start_line,
        "end_line": end_line,
        "decorators": decorators or [],
        "is_exported": False,
        "overrides_external": False,
    }


def _module(qn: str, path: str) -> ResultRow:
    return {
        "label": _MODULE,
        "qualified_name": qn,
        "name": qn.rsplit(".", 1)[-1],
        "path": path,
        "start_line": 1,
        "end_line": 1,
        "decorators": [],
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
    # Attribute paths are token streams: `#[tokio :: test]` is the same
    # attribute as `#[tokio::test]`, so matching normalises whitespace.
    dead = _collect(
        [
            _function(
                "proj.src.lib.test_async",
                "test_async",
                "src/lib.rs",
                decorators=['#[tokio::test(flavor = "multi_thread")]'],
            ),
            _function(
                "proj.src.lib.test_spaced",
                "test_spaced",
                "src/lib.rs",
                decorators=["#[tokio :: test]"],
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
    # is test code even with no attribute anywhere. The Module node is
    # what makes `tests` a MODULE segment rather than a type or leaf.
    dead = _collect(
        [
            _module("proj.src.lib.tests", "src/lib.rs"),
            _function("proj.src.lib.tests.mk_input", "mk_input", "src/lib.rs"),
        ]
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
            _module("proj.src.lib.tests", "src/lib.rs"),
            _function(
                "proj.src.lib.test_add",
                "test_add",
                "src/lib.rs",
                decorators=["#[test]"],
                start_line=10,
                end_line=12,
            ),
            _function(
                "proj.src.lib.tests.mk_input",
                "mk_input",
                "src/lib.rs",
                start_line=20,
                end_line=22,
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
    # like `mod tests` on both flag polarities. Distinct spans and NO
    # call edge: only the module rule itself can suppress mk_input, so
    # this test genuinely pins the singular spelling.
    nodes = [
        _module("proj.src.lib.test", "src/lib.rs"),
        _function(
            "proj.src.lib.test.t_basic",
            "t_basic",
            "src/lib.rs",
            decorators=["#[test]"],
            start_line=10,
            end_line=12,
        ),
        _function(
            "proj.src.lib.test.mk_input",
            "mk_input",
            "src/lib.rs",
            start_line=20,
            end_line=22,
        ),
    ]
    assert _collect(nodes) == set()
    assert _collect(nodes, include_tests=False) == set()


def test_symbols_nested_in_test_fns_are_rooted_when_tests_included() -> None:
    # A fn defined inside a `#[test]` fn and consumed as a value
    # (`filter(is_even)`) has no CALLS edge; the span rule must root it
    # on the include side too, or the default report flags it dead.
    dead = _collect(
        [
            _function(
                "proj.src.lib.t",
                "t",
                "src/lib.rs",
                decorators=["#[test]"],
                start_line=10,
                end_line=20,
            ),
            _function(
                "proj.src.lib.is_even",
                "is_even",
                "src/lib.rs",
                start_line=12,
                end_line=13,
            ),
        ]
    )
    assert dead == set()


def test_symbols_sharing_a_test_fns_line_stay_reportable() -> None:
    # Line spans have no columns: a production fn packed onto the test
    # fn's first or last physical line is NOT lexically inside it, so
    # containment is strict on both ends and such symbols stay in the
    # report on both polarities.
    nodes = [
        _function(
            "proj.src.lib.t",
            "t",
            "src/lib.rs",
            decorators=["#[test]"],
            start_line=3,
            end_line=5,
        ),
        _function(
            "proj.src.lib.produce",
            "produce",
            "src/lib.rs",
            start_line=5,
            end_line=5,
        ),
    ]
    assert "proj.src.lib.produce" in _collect(nodes)
    assert "proj.src.lib.produce" in _collect(nodes, include_tests=False)


def test_symbols_nested_in_excluded_test_fns_are_excluded() -> None:
    # Nested fns register FLAT under the module qn and closures as
    # anonymous_<line>_<col>; neither carries the attribute or a tests
    # segment, so the exclusion must also cover symbols whose span lies
    # inside an excluded test fn's span in the same file.
    dead = _collect(
        [
            _function(
                "proj.src.lib.test_ctx",
                "test_ctx",
                "src/lib.rs",
                decorators=["#[test]"],
                start_line=10,
                end_line=30,
            ),
            _function(
                "proj.src.lib.nested_helper",
                "nested_helper",
                "src/lib.rs",
                start_line=12,
                end_line=14,
            ),
            _function(
                "proj.src.lib.anonymous_16_8",
                "anonymous_16_8",
                "src/lib.rs",
                start_line=16,
                end_line=18,
            ),
        ],
        include_tests=False,
    )
    assert dead == set()


def test_type_named_tests_does_not_mark_test_code() -> None:
    # A lowercase TYPE named `tests` shares the qn shape of a module but
    # has no Module node: its methods stay reportable.
    dead = _collect(
        [
            _module("proj.src.lib", "src/lib.rs"),
            _function("proj.src.lib.tests.dead_method", "dead_method", "src/lib.rs"),
            _function("proj.src.lib.helper", "helper", "src/lib.rs"),
        ],
        rels=[_calls("proj.src.lib.tests.dead_method", "proj.src.lib.helper")],
    )
    assert "proj.src.lib.tests.dead_method" in dead
    assert "proj.src.lib.helper" in dead


def test_dotted_project_prefix_does_not_mask_reporting() -> None:
    # A project name may itself contain dots ending in `tests`; the
    # module match keys on real Module qns, so the prefix segments never
    # silence reporting.
    rows = collect_dead_code(
        FakeIngestor(
            [
                _module("my.tests.src.lib", "src/lib.rs"),
                _function("my.tests.src.lib.orphan", "orphan", "src/lib.rs"),
            ],
            [],
        ),
        "my.tests",
        default_dead_code_config(include_tests=True, include_classes=False),
    )
    assert {row["qualified_name"] for row in rows} == {"my.tests.src.lib.orphan"}
