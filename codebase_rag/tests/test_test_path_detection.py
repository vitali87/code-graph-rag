# Issue #2618: test code was detected by case-sensitive raw substrings of the
# path. `test_` inside shortest_/latest_ made production code a test (never
# reported dead, returned by tests-reaching), while `Tests/`, `*.Tests/`,
# `*_unittest.cc` and Gradle source sets were never recognised. The one
# classifier (`matches_test_path`) now reads path segments and file-name
# words; every consumer (dead-code both polarities, graph tests-reaching, the
# structural delta, endpoint emission) goes through it.
from __future__ import annotations

import pytest

from codebase_rag import constants as cs
from codebase_rag import cypher_queries as cq
from codebase_rag import graph_query
from codebase_rag.dead_code import collect_dead_code, default_dead_code_config
from codebase_rag.path_filters import matches_test_path
from codebase_rag.structural_delta import _tests_reaching as delta_tests_reaching
from codebase_rag.types_defs import PropertyDict, ResultRow

P = "proj"
_FUNCTION = cs.NodeLabel.FUNCTION.value
_METHOD = cs.NodeLabel.METHOD.value

# --- the classifier ---------------------------------------------------------

# Production paths the substring rules called tests: a word merely containing
# "test" (shortest, latest, contest, attestation) or a longer word after `_`.
_PRODUCTION_WITH_TEST_INSIDE_A_WORD = (
    "geo/shortest_route.py",
    "geo/latest_prices.py",
    "networkx/algorithms/shortest_paths/weighted.py",
    "networkx/algorithms/shortest_paths/generic.py",
    "benchmarks/benchmarks/benchmark_shortest_path.py",
    "lib/fastest_route.py",
    "lib/greatest_common_divisor.py",
    "src/contest_entry.py",
    "src/contests/entry.py",
    "pkg/attestation/verify.go",
    "experiments/ab_testing.py",
    "audit/is_tested.py",
    "src/Latest.java",
    "src/Contest.cs",
)

# Real test layouts the previous substring rules already recognised; they
# must keep being test code.
_ESTABLISHED_TEST_LAYOUTS = (
    "tests/test_app.py",
    "test_app.py",
    "pkg/app_test.py",
    "conftest.py",
    "pkg/conftest.py",
    "pkg/server_test.go",
    "src/app.spec.ts",
    "src/app.test.tsx",
    "src/__tests__/helper.ts",
    "src/test/java/com/acme/FooTest.java",
    "src/test/java/com/acme/Fixtures.java",
    "test/helpers.js",
    "tests/integration.rs",
    "test_utils/fixtures.py",
    "integration_tests/db.py",
    "pandas/_testing/asserters.py",
    "networkx/algorithms/shortest_paths/tests/test_weighted.py",
)

# Test layouts the issue lists as never recognised.
_UNRECOGNISED_TEST_LAYOUTS = (
    # .NET test projects and *Tests.cs files
    "src/FluentValidation.Tests/SyncAsyncParityTests.cs",
    "src/FluentValidation.Tests/Helpers.cs",
    "src/Acme.UnitTests/Parser.cs",
    "src/Acme.IntegrationTests/Db.cs",
    "src/Acme.Specs/Parser.cs",
    "src/Acme/ParserTests.cs",
    # Chromium / abseil / googletest
    "snappy_unittest.cc",
    "base/strings/string_util_unittest.cc",
    # SwiftPM and Xcode test targets
    "Tests/AlamofireTests/SessionTests.swift",
    "Tests/Helpers.swift",
    "AppTests/Fixtures.swift",
    # Gradle / Android / Kotlin Multiplatform source sets
    "app/src/androidTest/java/com/acme/Robot.kt",
    "src/integrationTest/java/com/acme/Db.java",
    "src/testFixtures/java/com/acme/Fixtures.java",
    "shared/src/commonTest/kotlin/Fakes.kt",
    # Jasmine / RSpec top-level spec/ directory
    "spec/support/helpers.js",
    "spec/models/user_spec.rb",
    # kebab-case test file
    "src/foo-test.js",
)


@pytest.mark.parametrize("path", _PRODUCTION_WITH_TEST_INSIDE_A_WORD)
def test_word_containing_test_is_not_test_code(path: str) -> None:
    assert not matches_test_path(path)


@pytest.mark.parametrize("path", _ESTABLISHED_TEST_LAYOUTS)
def test_established_test_layouts_are_still_test_code(path: str) -> None:
    assert matches_test_path(path)


@pytest.mark.parametrize("path", _UNRECOGNISED_TEST_LAYOUTS)
def test_issue_test_layouts_are_test_code(path: str) -> None:
    assert matches_test_path(path)


@pytest.mark.parametrize(
    "path",
    ("Tests/helpers.py", "TESTS/helpers.py", "Test/helpers.js", "pkg/Foo_Test.cpp"),
)
def test_directory_and_word_case_does_not_matter(path: str) -> None:
    assert matches_test_path(path)


@pytest.mark.parametrize(
    "path",
    (
        # A nested spec/ is as often a domain package as a suite: the JDK's
        # java/security/spec, swagger-ui's plugins/spec. Only the top-level
        # spec/ (RSpec, Jasmine) marks tests.
        "src/java.base/share/classes/java/security/spec/KeySpec.java",
        "src/core/plugins/spec/actions.js",
        # `_spec` is a production suffix (TensorFlow's type_spec, tensor_spec).
        "tensorflow/python/framework/type_spec.py",
        # CamelCase Spec is a production suffix too (PodSpec, SecretKeySpec).
        "src/main/java/io/k8s/PodSpec.java",
        # A Test word INSIDE a CamelCase name is not a test marker:
        # FluentValidation ships TestHelper to users as production API.
        "src/FluentValidation/TestHelper/ValidatorTestExtensions.cs",
        # Go keeps fixtures in testdata/, never compiled as tests.
        "pkg/parser/testdata/input.go",
        "src/app.py",
        "",
    ),
)
def test_neighbouring_production_paths_stay_production(path: str) -> None:
    assert not matches_test_path(path)


def test_leading_slash_is_the_repo_root() -> None:
    # A root spec/ keeps its meaning with or without the leading slash
    # callers used to add.
    assert matches_test_path("/spec/models/user_spec.rb")
    assert not matches_test_path("/src/spec/user.rb")


# --- dead-code (issue reproduction) -------------------------------------------


class _FakeIngestor:
    def __init__(self, nodes: list[ResultRow], rels: list[ResultRow]) -> None:
        self._nodes = nodes
        self._rels = rels

    def fetch_all(
        self, query: str, params: dict[str, str] | None = None
    ) -> list[ResultRow]:
        if query == cq.CYPHER_DEAD_CODE_NODES:
            return self._nodes
        return self._rels


def _node(label: str, qn: str, path: str) -> ResultRow:
    return {
        "label": label,
        "qualified_name": qn,
        "name": qn.rsplit(".", 1)[-1],
        "path": path,
        "start_line": 1,
        "end_line": 2,
        "decorators": [],
        "is_exported": False,
        "overrides_external": False,
    }


def _calls(src: ResultRow, dst: ResultRow) -> ResultRow:
    return {
        "from_label": src["label"],
        "from_qn": src["qualified_name"],
        "rel_type": cs.RelationshipType.CALLS.value,
        "to_label": dst["label"],
        "to_qn": dst["qualified_name"],
    }


_HELPERS = (
    _node(_FUNCTION, f"{P}.geo.shortest_route._unused_helper", "geo/shortest_route.py"),
    _node(_FUNCTION, f"{P}.geo.latest_prices._unused_helper2", "geo/latest_prices.py"),
    _node(_FUNCTION, f"{P}.geo.route._unused_helper3", "geo/route.py"),
)


@pytest.mark.parametrize("include_tests", (True, False))
def test_identical_unused_helpers_are_all_reported(include_tests: bool) -> None:
    rows = collect_dead_code(
        _FakeIngestor(list(_HELPERS), []),
        P,
        default_dead_code_config(include_tests=include_tests, include_classes=False),
    )
    assert {row["qualified_name"] for row in rows} == {
        str(helper["qualified_name"]) for helper in _HELPERS
    }


_COMPRESS = _node(_FUNCTION, f"{P}.snappy.Compress", "snappy.cc")
_UNITTEST = _node(_FUNCTION, f"{P}.snappy_unittest.TestCompress", "snappy_unittest.cc")


def test_unittest_file_roots_what_it_exercises() -> None:
    # With tests included, the *_unittest.cc caller is a root: neither it
    # nor the production function it alone calls is dead.
    rows = collect_dead_code(
        _FakeIngestor([_COMPRESS, _UNITTEST], [_calls(_UNITTEST, _COMPRESS)]),
        P,
        default_dead_code_config(include_tests=True, include_classes=False),
    )
    assert rows == []


def test_unittest_file_is_excluded_without_tests() -> None:
    # --no-include-tests: the test is not reported, the production function
    # only a test calls is.
    rows = collect_dead_code(
        _FakeIngestor([_COMPRESS, _UNITTEST], [_calls(_UNITTEST, _COMPRESS)]),
        P,
        default_dead_code_config(include_tests=False, include_classes=False),
    )
    assert [row["qualified_name"] for row in rows] == [f"{P}.snappy.Compress"]


# --- tests-reaching: graph query and structural delta -------------------------

_NX = f"{P}.networkx.algorithms.shortest_paths"
_WEIGHT_FN = _node(
    _FUNCTION,
    f"{_NX}.weighted._weight_function",
    "networkx/algorithms/shortest_paths/weighted.py",
)
_DIJKSTRA = _node(
    _FUNCTION,
    f"{_NX}.weighted.dijkstra_path",
    "networkx/algorithms/shortest_paths/weighted.py",
)
_NX_TEST = _node(
    _FUNCTION,
    f"{_NX}.tests.test_weighted.test_dijkstra",
    "networkx/algorithms/shortest_paths/tests/test_weighted.py",
)
_BENCHMARK = _node(
    _FUNCTION,
    f"{P}.benchmarks.benchmarks.benchmark_shortest_path.time_dijkstra",
    "benchmarks/benchmarks/benchmark_shortest_path.py",
)
_FV = f"{P}.src.FluentValidation"
_VALIDATE = _node(
    _METHOD,
    f"{_FV}.AbstractValidator.AbstractValidator.Validate(T)",
    "src/FluentValidation/AbstractValidator.cs",
)
_PARITY_TEST = _node(
    _METHOD,
    f"{_FV}.Tests.SyncAsyncParityTests.SyncAsyncParityTests.Validates",
    "src/FluentValidation.Tests/SyncAsyncParityTests.cs",
)
_TEST_HELPER = _node(
    _METHOD,
    f"{_FV}.TestHelper.ValidatorTestExtensions.ValidatorTestExtensions.TestValidate",
    "src/FluentValidation/TestHelper/ValidatorTestExtensions.cs",
)
_SWIFT_REQUEST = _node(_METHOD, f"{P}.Source.Session.request", "Source/Session.swift")
_SWIFT_TEST = _node(
    _METHOD,
    f"{P}.Tests.SessionTests.SessionTests.testRequest",
    "Tests/SessionTests.swift",
)
_BENCH = _node(_FUNCTION, f"{P}.snappy_benchmark.BM_Compress", "snappy_benchmark.cc")

_NODES = (
    _WEIGHT_FN,
    _DIJKSTRA,
    _NX_TEST,
    _BENCHMARK,
    _COMPRESS,
    _UNITTEST,
    _BENCH,
    _VALIDATE,
    _PARITY_TEST,
    _TEST_HELPER,
    _SWIFT_REQUEST,
    _SWIFT_TEST,
)
_CALL_EDGES = (
    (_DIJKSTRA, _WEIGHT_FN),
    (_NX_TEST, _DIJKSTRA),
    (_BENCHMARK, _DIJKSTRA),
    (_UNITTEST, _COMPRESS),
    (_BENCH, _COMPRESS),
    (_PARITY_TEST, _VALIDATE),
    (_TEST_HELPER, _VALIDATE),
    (_SWIFT_TEST, _SWIFT_REQUEST),
)


def _graph_fetch(query: str, params: PropertyDict | None = None) -> list[ResultRow]:
    if query == cq.CYPHER_DEAD_CODE_NODES:
        return [dict(n, rust_cfg_test_mods=[], rust_ungated_mods=[]) for n in _NODES]
    if query == cq.CYPHER_DEAD_CODE_RELS:
        return [_calls(src, dst) for src, dst in _CALL_EDGES]
    return []


def _delta_fetch(query: str, params: PropertyDict | None = None) -> list[ResultRow]:
    assert query == cq.CYPHER_DELTA_CALLERS_OF, query[:60]
    frontier = set(params[cs.KEY_QNS]) if params else set()
    return [
        dict(src, to_qn=dst["qualified_name"])
        for src, dst in _CALL_EDGES
        if dst["qualified_name"] in frontier
    ]


def _reaching(target: ResultRow) -> list[tuple[int, str]]:
    rows = graph_query.tests_reaching(_graph_fetch, P, str(target["qualified_name"]))
    return [(r["depth"], r["qualified_name"]) for r in rows]


def _delta_reaching(target: ResultRow) -> list[tuple[int, str]]:
    rows = delta_tests_reaching(
        _delta_fetch, P, [str(target["qualified_name"])], longer_project_prefixes=()
    )
    return [(r["depth"], r["qualified_name"]) for r in rows]


# The same expectations hold for `cgr graph tests-reaching` / MCP
# tests_reaching and for the structural delta `cgr check` reports.
_REACHING_CASES = (
    # Production code under shortest_paths/ and a benchmark are not tests.
    (_WEIGHT_FN, [(2, str(_NX_TEST["qualified_name"]))]),
    # A *_unittest.cc caller is a test; the benchmark caller is not.
    (_COMPRESS, [(1, str(_UNITTEST["qualified_name"]))]),
    # A .NET *.Tests project is a test; the shipped TestHelper is not.
    (_VALIDATE, [(1, str(_PARITY_TEST["qualified_name"]))]),
    # SwiftPM's capital-T Tests/.
    (_SWIFT_REQUEST, [(1, str(_SWIFT_TEST["qualified_name"]))]),
)


@pytest.mark.parametrize(("target", "expected"), _REACHING_CASES)
def test_graph_tests_reaching_lists_exactly_the_tests(
    target: ResultRow, expected: list[tuple[int, str]]
) -> None:
    assert _reaching(target) == expected


@pytest.mark.parametrize(("target", "expected"), _REACHING_CASES)
def test_delta_tests_reaching_lists_exactly_the_tests(
    target: ResultRow, expected: list[tuple[int, str]]
) -> None:
    assert _delta_reaching(target) == expected
