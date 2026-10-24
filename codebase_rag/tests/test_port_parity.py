"""`cgr port check` (epic #3413): pairing two implementations across languages
and judging each pair on its first-party calls and positional arity.

The end-to-end tests index a Python module and its Rust port into the
embedded graph, so the pairing and the verdicts run over the rows the real
engine returns; the helpers' edge cases are tested directly.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

import pytest
from typer.testing import CliRunner

from codebase_rag import constants as cs
from codebase_rag.cli import app
from codebase_rag.config import settings
from codebase_rag.graph_dialects import DIALECT_SQLITE
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.port_parity import (
    PairReport,
    ParityReport,
    PortCheckError,
    PortScope,
    _Arity,
    _arity,
    _arity_parity,
    _Callable,
    _calls_parity,
    _merge_resolution,
    has_differences,
    normalize_name,
    parse_pairs,
    port_check,
)
from codebase_rag.services.graph_service import MemgraphIngestor

PROJECT = "port_fixture"

FIXTURE: dict[str, str] = {
    "py/geometry.py": (
        "def area(width, height):\n"
        "    return scale(width) * height\n\n\n"
        "def scale(value):\n"
        "    return value * 2\n\n\n"
        "def perimeter(width, height):\n"
        "    return 2 * (width + height)\n\n\n"
        "def label():\n"
        "    return 'shape'\n\n\n"
        "def only_in_python():\n"
        "    return 1\n\n\n"
        "class Shape:\n"
        "    def describe(self, prefix):\n"
        "        return prefix + label()\n"
    ),
    "py/tests/test_geometry.py": (
        "from py.geometry import area\n\n\n"
        "def test_area():\n"
        "    assert area(1, 2) == 4\n"
    ),
    "rs/geometry.rs": (
        "pub fn area(width: i64, height: i64) -> i64 {\n"
        "    scale(width) * height\n"
        "}\n\n"
        "pub fn scale(value: i64) -> i64 {\n"
        "    value * 2\n"
        "}\n\n"
        "pub fn perimeter(width: i64) -> i64 {\n"
        "    4 * width\n"
        "}\n\n"
        "pub fn label() -> String {\n"
        '    String::from("shape")\n'
        "}\n\n"
        "pub fn only_in_rust() -> i64 {\n"
        "    1\n"
        "}\n\n"
        "pub struct Shape;\n\n"
        "impl Shape {\n"
        "    pub fn describe(&self, prefix: &str) -> String {\n"
        '        format!("{}!", prefix)\n'
        "    }\n"
        "}\n"
    ),
    # Two `run`s on the old side in different modules, one on the new side
    # in a module named for neither: ambiguous until a --pair settles it.
    "py/jobs/nightly.py": "def run():\n    return 'nightly'\n",
    "py/jobs/weekly.py": "def run():\n    return 'weekly'\n",
    "rs/jobs.rs": 'pub fn run() -> &\'static str {\n    "nightly"\n}\n',
}


# Repeated basenames (`a/utils`, `b/utils`) pair by their path under each
# scope; a module the port moved (`lexer.py` -> `cypher/lexer.rs`) still pairs
# by its file's stem.
LAYOUTS = "port_layouts"
LAYOUT_FIXTURE: dict[str, str] = {
    "legacy/a/utils.py": "def run(x):\n    return x\n",
    "legacy/b/utils.py": "def run():\n    return 0\n",
    "legacy/lexer.py": "def tokenize(text):\n    return text\n",
    "rewrite/a/utils.rs": "pub fn run(x: i64) -> i64 {\n    x\n}\n",
    "rewrite/b/utils.rs": "pub fn run() -> i64 {\n    0\n}\n",
    "rewrite/cypher/lexer.rs": "pub fn tokenize(text: &str) -> &str {\n    text\n}\n",
}


def _qn(rel: str, project: str = PROJECT) -> str:
    return f"{project}.{rel}"


def _index(parent: Path, project: str, files: dict[str, str]) -> Path:
    root = parent / project
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    parsers, queries = load_parsers()
    with MemgraphIngestor(host="unused", port=0) as ingestor:
        ingestor.ensure_constraints()
        GraphUpdater(
            ingestor=ingestor,
            repo_path=root,
            parsers=parsers,
            queries=queries,
            project_name=project,
        ).run(force=True)
    return root


@pytest.fixture
def indexed(temp_repo: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(settings, "GRAPH_BACKEND", DIALECT_SQLITE)
    return _index(temp_repo, PROJECT, FIXTURE)


@pytest.fixture
def layouts(temp_repo: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(settings, "GRAPH_BACKEND", DIALECT_SQLITE)
    return _index(temp_repo, LAYOUTS, LAYOUT_FIXTURE)


def _check(
    old: str = "py",
    new: str = "rs",
    pairs: Sequence[tuple[str, str]] = (),
    include_tests: bool = False,
    project: str = PROJECT,
) -> ParityReport:
    with MemgraphIngestor(host="unused", port=0) as ingestor:
        return port_check(
            ingestor.fetch_all,
            PortScope(project, old),
            PortScope(project, new),
            pairs,
            include_tests=include_tests,
        )


def _pair_of(report: ParityReport, old_qn: str) -> PairReport:
    (pair,) = [p for p in report["pairs"] if p["old"]["qualified_name"] == old_qn]
    return pair


def test_identical_twins_match_on_calls_and_arity(indexed: Path) -> None:
    report = _check()
    area = _pair_of(report, _qn("py.geometry.area"))
    assert area["new"]["qualified_name"] == _qn("rs.geometry.area")
    assert area["paired_by"] == cs.PairedBy.MODULE
    assert area["verdict"] == cs.ParityVerdict.MATCH
    assert area["calls"]["verdict"] == cs.ParityVerdict.MATCH
    assert area["arity"] == {
        "verdict": cs.ParityVerdict.MATCH,
        "old": 2,
        "new": 2,
        "open": False,
    }
    assert _pair_of(report, _qn("py.geometry.scale"))["verdict"] == (
        cs.ParityVerdict.MATCH
    )


def test_a_dropped_parameter_differs_on_arity(indexed: Path) -> None:
    perimeter = _pair_of(_check(), _qn("py.geometry.perimeter"))
    assert perimeter["verdict"] == cs.ParityVerdict.DIFFERS
    assert perimeter["arity"]["verdict"] == cs.ParityVerdict.DIFFERS
    assert (perimeter["arity"]["old"], perimeter["arity"]["new"]) == (2, 1)


def test_a_call_the_port_dropped_is_missing(indexed: Path) -> None:
    describe = _pair_of(_check(), _qn("py.geometry.Shape.describe"))
    assert describe["new"]["qualified_name"] == _qn("rs.geometry.Shape.describe")
    # The receiver is left out on both sides: `self, prefix` and
    # `&self, prefix` are one parameter each.
    assert describe["arity"]["verdict"] == cs.ParityVerdict.MATCH
    assert describe["calls"]["verdict"] == cs.ParityVerdict.DIFFERS
    assert describe["calls"]["missing"] == [
        {
            "old": _qn("py.geometry.label"),
            "new": _qn("rs.geometry.label"),
            "resolution": cs.EdgeResolution.EXACT.value,
        }
    ]
    assert describe["calls"]["extra"] == []


def test_unpaired_definitions_are_listed_per_side(indexed: Path) -> None:
    report = _check()
    assert [r["qualified_name"] for r in report["old_only"]] == [
        _qn("py.geometry.only_in_python")
    ]
    assert [r["qualified_name"] for r in report["new_only"]] == [
        _qn("rs.geometry.only_in_rust")
    ]


def test_tests_are_left_out_unless_asked_for(indexed: Path) -> None:
    test_qn = _qn("py.tests.test_geometry.test_area")
    every_qn = [r["qualified_name"] for r in _check(include_tests=True)["old_only"]]
    assert test_qn in every_qn
    assert test_qn not in [r["qualified_name"] for r in _check()["old_only"]]


def test_a_name_no_container_settles_is_ambiguous_until_paired(
    indexed: Path,
) -> None:
    report = _check()
    assert report["ambiguous"] == [
        {
            "name": "run",
            "old": [
                {
                    "qualified_name": _qn("py.jobs.nightly.run"),
                    "path": "py/jobs/nightly.py",
                    "start_line": 1,
                },
                {
                    "qualified_name": _qn("py.jobs.weekly.run"),
                    "path": "py/jobs/weekly.py",
                    "start_line": 1,
                },
            ],
            "new": [
                {
                    "qualified_name": _qn("rs.jobs.run"),
                    "path": "rs/jobs.rs",
                    "start_line": 1,
                }
            ],
        }
    ]
    paired = _check(pairs=[("nightly.run", "rs.jobs.run")])
    assert paired["ambiguous"] == []
    nightly = _pair_of(paired, _qn("py.jobs.nightly.run"))
    assert nightly["paired_by"] == cs.PairedBy.EXPLICIT
    assert nightly["verdict"] == cs.ParityVerdict.MATCH
    assert _qn("py.jobs.weekly.run") in [
        r["qualified_name"] for r in paired["old_only"]
    ]


def test_the_summary_counts_the_report(indexed: Path) -> None:
    report = _check()
    summary = report["summary"]
    # area, scale, label match; perimeter and Shape.describe differ.
    assert summary["pairs"] == len(report["pairs"]) == 5
    assert summary["differs"] == 2
    assert summary["match"] == 3
    assert summary["paired_by"] == {
        "explicit": 0,
        "module": 5,
        "container": 0,
        "name": 0,
    }
    assert (summary["old_only"], summary["new_only"], summary["ambiguous"]) == (
        1,
        1,
        1,
    )
    assert has_differences(report)
    assert report["old"] == {
        "project": PROJECT,
        "scope": "py",
        "definitions": 8,
    }


def test_repeated_basenames_pair_by_their_path_under_each_scope(
    layouts: Path,
) -> None:
    report = _check("legacy", "rewrite", project=LAYOUTS)
    assert report["ambiguous"] == []
    pairs = {
        p["old"]["qualified_name"]: (p["new"]["qualified_name"], p["paired_by"])
        for p in report["pairs"]
    }
    assert pairs == {
        _qn("legacy.a.utils.run", LAYOUTS): (
            _qn("rewrite.a.utils.run", LAYOUTS),
            cs.PairedBy.MODULE,
        ),
        _qn("legacy.b.utils.run", LAYOUTS): (
            _qn("rewrite.b.utils.run", LAYOUTS),
            cs.PairedBy.MODULE,
        ),
        # Moved under `cypher/`, so only the file's stem still agrees.
        _qn("legacy.lexer.tokenize", LAYOUTS): (
            _qn("rewrite.cypher.lexer.tokenize", LAYOUTS),
            cs.PairedBy.CONTAINER,
        ),
    }
    assert report["summary"]["match"] == 3


@pytest.mark.parametrize(
    ("old", "new"), [("py", "py/jobs"), (".", "rs"), ("py/", "./py")]
)
def test_overlapping_scopes_are_refused(indexed: Path, old: str, new: str) -> None:
    with pytest.raises(PortCheckError) as raised:
        _check(old, new)
    assert raised.value.exit_code == cs.PORT_EXIT_USAGE


def test_an_empty_scope_is_refused(indexed: Path) -> None:
    with pytest.raises(PortCheckError, match="lies under 'go'") as raised:
        _check("py", "go")
    assert raised.value.exit_code == cs.GRAPH_EXIT_UNKNOWN_TARGET


def test_a_pair_naming_no_single_definition_is_refused(indexed: Path) -> None:
    with pytest.raises(PortCheckError, match="names 2 definitions") as raised:
        _check(pairs=[("run", "rs.jobs.run")])
    assert raised.value.exit_code == cs.GRAPH_EXIT_UNKNOWN_TARGET


def test_a_definition_paired_twice_is_refused(indexed: Path) -> None:
    with pytest.raises(PortCheckError, match="more than once") as raised:
        _check(pairs=[("nightly.run", "rs.jobs.run"), ("weekly.run", "rs.jobs.run")])
    assert raised.value.exit_code == cs.PORT_EXIT_USAGE


# --- the CLI ---------------------------------------------------------------------


def _cli(*args: str) -> tuple[int, str, str]:
    result = CliRunner().invoke(
        app, ["port", "check", *args, "--project", PROJECT], prog_name="cgr"
    )
    return result.exit_code, result.stdout, result.stderr


def test_the_cli_prints_the_report_as_json(indexed: Path) -> None:
    code, out, _err = _cli("py", "rs")
    assert code == 0
    assert json.loads(out)["summary"]["pairs"] == 5


def test_fail_on_found_exits_one_when_a_pair_differs(indexed: Path) -> None:
    code, out, _err = _cli("py", "rs", "--fail-on-found")
    assert code == cs.PORT_EXIT_DIFFERS
    assert json.loads(out)["summary"]["differs"] == 2


def test_the_cli_refuses_an_unknown_new_project(indexed: Path) -> None:
    code, out, err = _cli("py", "rs", "--new-project", "nope__00000000")
    assert code == cs.GRAPH_EXIT_UNKNOWN_PROJECT
    assert out == ""
    assert "nope__00000000" in err


def test_the_cli_reports_a_malformed_pair_as_a_usage_error(indexed: Path) -> None:
    code, out, err = _cli("py", "rs", "--pair", "nightly.run")
    assert code == cs.PORT_EXIT_USAGE
    assert out == ""
    assert "OLD=NEW" in err


# --- the helpers -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("read_number", "read_number"),
        ("_read_number", "read_number"),
        ("readNumber", "read_number"),
        ("ReadNumber", "read_number"),
        ("HTTPServer", "http_server"),
        ("parseHTTP", "parse_http"),
        ("__init__", "init"),
        ("__", "__"),
    ],
)
def test_names_fold_case_convention_and_underscores(name: str, expected: str) -> None:
    assert normalize_name(name) == expected


def test_pairs_parse_old_equals_new() -> None:
    assert parse_pairs(["a.f = b.g", "x=y"]) == [("a.f", "b.g"), ("x", "y")]
    for bad in ("a.f", "=b", "a="):
        with pytest.raises(PortCheckError, match="OLD=NEW"):
            parse_pairs([bad])


def test_a_difference_resting_only_on_guessed_edges_is_unknown() -> None:
    twins = {"old.f": "new.f", "old.g": "new.g"}
    reverse = {v: k for k, v in twins.items()}
    heuristic = cs.EdgeResolution.HEURISTIC.value
    exact = cs.EdgeResolution.EXACT.value
    guessed = _calls_parity({"old.f": heuristic}, {}, twins, reverse)
    assert guessed["verdict"] == cs.ParityVerdict.UNKNOWN
    certain = _calls_parity({"old.f": heuristic}, {"new.g": exact}, twins, reverse)
    assert certain["verdict"] == cs.ParityVerdict.DIFFERS
    assert _calls_parity({"old.f": exact}, {"new.f": heuristic}, twins, reverse)[
        "verdict"
    ] == (cs.ParityVerdict.MATCH)


def test_callees_without_a_twin_are_listed_not_judged() -> None:
    parity = _calls_parity(
        {"old.helper": cs.EdgeResolution.EXACT.value},
        {"new.inlined": cs.EdgeResolution.EXACT.value},
        {},
        {},
    )
    assert parity["verdict"] == cs.ParityVerdict.MATCH
    assert parity["unpaired_old"] == ["old.helper"]
    assert parity["unpaired_new"] == ["new.inlined"]


def test_one_exact_site_makes_a_call_certain() -> None:
    callees: dict[str, str | None] = {}
    _merge_resolution(callees, "f", cs.EdgeResolution.HEURISTIC.value)
    _merge_resolution(callees, "f", cs.EdgeResolution.EXACT.value)
    _merge_resolution(callees, "f", cs.EdgeResolution.OVERLOAD.value)
    assert callees == {"f": cs.EdgeResolution.EXACT.value}


def _callable(
    path: str,
    label: str = cs.NodeLabel.FUNCTION.value,
    decorators: tuple[str, ...] = (),
    declared: int = 0,
) -> _Callable:
    return _Callable("p.m.f", label, "f", "m", path, 1, decorators, declared)


def test_arity_drops_receivers_in_every_language() -> None:
    method = cs.NodeLabel.METHOD.value
    assert _arity(_callable("m.py", method, declared=2), ("self", "x")) == _Arity(
        1, False
    )
    assert _arity(
        _callable("m.py", method, ("@staticmethod",), declared=1), ("x",)
    ) == _Arity(1, False)
    assert _arity(_callable("m.rs", method), ("self", "x")) == _Arity(1, False)
    assert _arity(_callable("m.cs"), ("this s", "n?")) == _Arity(1, False)


@pytest.mark.parametrize(
    ("path", "positional", "fixed"),
    [
        ("m.js", ("self",), 1),
        ("m.go", ("self", "x"), 2),
        ("m.ts", ("this s",), 1),
        ("m.java", ("self",), 1),
    ],
)
def test_only_the_language_writing_a_receiver_marker_loses_it(
    path: str, positional: tuple[str, ...], fixed: int
) -> None:
    # `self` is Rust's marker and `this name` C#'s; elsewhere they are
    # ordinary parameters written as declared.
    assert _arity(_callable(path), positional) == _Arity(fixed, False)


def test_parameters_past_the_positional_run_leave_arity_open() -> None:
    # `def f(self, *words)`: one positional name, two declared parameters.
    star = _arity(_callable("m.py", cs.NodeLabel.METHOD.value, declared=2), ("self",))
    assert star == _Arity(0, True)
    assert _arity(_callable("m.ts"), ("a", "...rest")) == _Arity(1, True)
    parity = _arity_parity(star, _Arity(1, False))
    assert parity["verdict"] == cs.ParityVerdict.UNKNOWN
    assert parity["open"] is True


def test_arity_a_language_does_not_record_is_unknown() -> None:
    assert _arity(_callable("m.lua"), None) is None
    parity = _arity_parity(None, _Arity(2, False))
    assert parity == {
        "verdict": cs.ParityVerdict.UNKNOWN,
        "old": None,
        "new": 2,
        "open": False,
    }
