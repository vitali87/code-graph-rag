"""`--engine-mode`: the suite on the Python engine, the Rust engine, or both.

docs/reports/PHASE1_SEAMS.md, section 2. The plugin's tests run a session of
their own in a child process, with the plugin loaded as `addopts` loads it, so
neither this session's mode nor its autouse wrappers around `GraphUpdater.run`
reach the session under test. The last test checks the suite's pass-failure
gate in this session.
"""

from __future__ import annotations

import fnmatch
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from loguru import logger

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.tests import engine_modes
from codebase_rag.tests.conftest import create_and_run_updater

PLUGIN = ("-p", "codebase_rag.tests.engine_modes", "-p", "no:cacheprovider")

# What a test reads of its engine selection: the setting, the variable, and
# what a `cgr` it starts says about the selection it read.
SELECTION_PROBE = """
import os
import subprocess
import sys

import pytest

from codebase_rag.config import settings

CLI = "from codebase_rag.cli import app; app()"


def report(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "m.py").write_text("def f():\\n    return 1\\n", encoding="utf-8")
    child = subprocess.run(
        [sys.executable, "-c", CLI, "index", "--repo-path", str(repo),
         "-o", str(tmp_path / "index")],
        env={**os.environ, "LOGURU_LEVEL": "DEBUG", "PYTHONUTF8": "1"},
        cwd=tmp_path, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=300,
    )
    assert child.returncode == 0, child.stderr
    said = [
        line.split(" - ", 1)[1]
        for line in child.stderr.splitlines()
        if "Index engine" in line or "CGR_INDEX_ENGINE" in line
    ]
    name = os.environ["PYTEST_CURRENT_TEST"].split("::")[-1].split(" ")[0]
    print(f"\\nPROBE {name} SETTING={settings.INDEX_ENGINE}")
    print(f"PROBE {name} VARIABLE={os.environ.get('CGR_INDEX_ENGINE')}")
    print(f"PROBE {name} CHILD={said}")


def test_mode(tmp_path):
    report(tmp_path)


@pytest.mark.index_engine("go=rust")
def test_marked(tmp_path):
    report(tmp_path)


@pytest.fixture
def own_selection(monkeypatch):
    monkeypatch.setattr(settings, "INDEX_ENGINE", "python")
    monkeypatch.setenv("CGR_INDEX_ENGINE", "python")


def test_own(own_selection, tmp_path):
    report(tmp_path)
"""

# The child's line for `rust` asked through the default, which this build
# runs on Python, and for Go asked by name.
CHILD_RUST_DEFAULT = (
    "Index engine: this build cannot index python, javascript, typescript, "
    "tsx, rust, go, scala, java, c, cpp, php, lua, c_sharp, dart, sql, julia "
    "on Rust, so they index on Python"
)
CHILD_GO_ON_RUST = (
    "CGR_INDEX_ENGINE asks for go on Rust, which this build cannot index it "
    "on; go indexes on Python"
)

# Indexes a small Go repository into an embedded graph, as a test of the
# suite does, and counts the scratch indexes `both` makes.
INDEXING = """
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag import index_engine
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers

GO = "package main\\n\\nfunc helper() int { return 1 }\\n\\nfunc main() { helper() }\\n"


@pytest.fixture
def replicas(monkeypatch):
    made = []
    original = GraphUpdater.replica

    def counted(self, ingestor, **kwargs):
        made.append(kwargs.get("index_engine"))
        return original(self, ingestor, **kwargs)

    monkeypatch.setattr(GraphUpdater, "replica", counted)
    return made


@pytest.fixture
def go_on_rust(monkeypatch):
    monkeypatch.setattr(
        index_engine, "rust_engine_languages", lambda: frozenset({cs.SupportedLanguage.GO})
    )


def go_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "main.go").write_text(GO, encoding="utf-8")
    return repo


def index(target, tmp_path):
    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=MagicMock(), repo_path=target, parsers=parsers, queries=queries,
        skip_embeddings=True, state_dir=tmp_path / "state",
    )
    updater.run()
    return updater
"""

COMPARISONS = """
def test_nothing_moved(replicas, tmp_path):
    updater = index(go_repo(tmp_path), tmp_path)
    assert updater.rust_languages == frozenset()
    assert replicas == []


def test_go_on_rust(go_on_rust, replicas, tmp_path):
    updater = index(go_repo(tmp_path), tmp_path)
    assert updater.rust_languages == {cs.SupportedLanguage.GO}
    # Once with every language on Python, once with the test's own selection.
    assert [selection is None for selection in replicas] == [False, True]


def test_no_file_on_rust(go_on_rust, replicas, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "m.py").write_text("def f():\\n    return 1\\n", encoding="utf-8")
    index(repo, tmp_path)
    assert replicas == []


def test_each_state_once(go_on_rust, replicas, tmp_path):
    repo = go_repo(tmp_path)
    updater = index(repo, tmp_path)
    updater.run()
    assert len(replicas) == 2
    (repo / "extra.go").write_text("package main\\n\\nfunc extra() {}\\n", encoding="utf-8")
    updater.run()
    assert len(replicas) == 4


def test_single_file(go_on_rust, replicas, tmp_path):
    repo = go_repo(tmp_path)
    index(repo / "main.go", tmp_path)
    assert replicas == []
"""

NEVER_COMPARED = """
def test_go_on_rust(go_on_rust, replicas, tmp_path):
    index(go_repo(tmp_path), tmp_path)
    assert replicas == []
"""

# One edge dropped and one property changed on the Rust side's graph.
PLANTED = """
from codebase_rag.tests import engine_modes


@pytest.fixture
def planted(monkeypatch):
    original = engine_modes.fresh_index

    def plant(updater, engine, scratch, run):
        export = original(updater, engine, scratch, run)
        if engine == cs.IndexEngine.RUST:
            export["relationships"] = [
                rel for rel in export["relationships"] if rel["type"] != "CALLS"
            ]
            for node in export["nodes"]:
                if "File" in node["labels"]:
                    node["properties"]["extension"] = ".gox"
        return export

    monkeypatch.setattr(engine_modes, "fresh_index", plant)


def test_planted(go_on_rust, planted, tmp_path):
    index(go_repo(tmp_path), tmp_path)
"""

# A pass that raises on every file: each scratch run swallows it as the
# test's own run does.
SWALLOWED = """
from codebase_rag.parsers.call_processor import CallProcessor


@pytest.fixture
def broken_calls(monkeypatch):
    def boom(*_args, **_kwargs):
        raise RuntimeError("pass exploded")

    monkeypatch.setattr(CallProcessor, "_ingest_function_calls", boom)


def test_swallowed(go_on_rust, broken_calls, tmp_path):
    index(go_repo(tmp_path), tmp_path)
"""

# A Rust side that does not finish.
RAISING = """
from codebase_rag.tests import engine_modes


@pytest.fixture
def rust_raises(monkeypatch):
    original = engine_modes.fresh_index

    def raise_on_rust(updater, engine, scratch, run):
        if engine == cs.IndexEngine.RUST:
            raise RuntimeError("the Rust side stopped")
        return original(updater, engine, scratch, run)

    monkeypatch.setattr(engine_modes, "fresh_index", raise_on_rust)


def test_raising(go_on_rust, rust_raises, tmp_path):
    index(go_repo(tmp_path), tmp_path)
"""

PLANTED_EDGE = (
    '- E\tCALLS\tFunction\t"repo.main.main"\tFunction\t"repo.main.helper"\t{*}'
)
PLANTED_NODE = (
    '- N\tFile\t"<root>/main.go"\t{"extension":".go","name":"main.go","path":"main.go"}',
    '+ N\tFile\t"<root>/main.go"\t{"extension":".gox","name":"main.go","path":"main.go"}',
)


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # This session's own mode sets CGR_INDEX_ENGINE; the session under test
    # starts from none, as CI's does. Go stays on tree-sitter so no index
    # builds the go/types helper.
    for name in (
        engine_modes.INDEX_ENGINE_ENV,
        engine_modes.PARITY_DIFF_DIR_ENV,
        "LOGURU_LEVEL",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CGR_HOME", str(tmp_path / "cgr-home"))
    monkeypatch.setenv("GO_FRONTEND", cs.GoFrontend.TREESITTER.value)


@pytest.fixture
def go_grammar() -> None:
    from codebase_rag.parser_loader import load_parsers

    parsers, _queries = load_parsers()
    if cs.SupportedLanguage.GO not in parsers:
        pytest.skip("go parser not available")


def _run(pytester: pytest.Pytester, *args: str) -> pytest.RunResult:
    return pytester.runpytest_subprocess(*PLUGIN, *args)


def _printed(result: pytest.RunResult, test: str) -> list[str]:
    """What the session's `test` printed of its selection, in order."""
    prefix = f"PROBE {test} "
    return [
        line.removeprefix(prefix) for line in result.outlines if line.startswith(prefix)
    ]


def _report(result: pytest.RunResult) -> list[str]:
    """The lines of the first engine report in the session's output."""
    lines = result.outlines
    start = next(
        i
        for i, line in enumerate(lines)
        if line.startswith("Python and Rust engines differ on ")
    )
    end = start + 1
    while end < len(lines) and lines[end].startswith("  "):
        end += 1
    return lines[start:end]


@pytest.mark.parametrize("mode", ["python", "rust", "both"])
def test_the_header_names_the_mode(
    pytester: pytest.Pytester, clean_env: None, mode: str
) -> None:
    pytester.makepyfile("def test_nothing():\n    pass\n")

    result = _run(pytester, "--engine-mode", mode)

    result.assert_outcomes(passed=1)
    assert f"engine mode: {mode}" in result.outlines


def test_the_mode_is_python_unless_asked(
    pytester: pytest.Pytester, clean_env: None
) -> None:
    pytester.makepyfile("def test_nothing():\n    pass\n")

    result = _run(pytester)

    result.assert_outcomes(passed=1)
    assert "engine mode: python" in result.outlines


def test_an_unknown_mode_is_refused(pytester: pytest.Pytester, clean_env: None) -> None:
    pytester.makepyfile("def test_nothing():\n    pass\n")

    result = _run(pytester, "--engine-mode", "fortran")

    assert result.ret == pytest.ExitCode.USAGE_ERROR
    assert "invalid choice: 'fortran'" in result.stderr.str()


def test_python_mode_leaves_the_selection_alone(
    pytester: pytest.Pytester, clean_env: None
) -> None:
    pytester.makepyfile(SELECTION_PROBE)

    result = _run(pytester, "-s", "--engine-mode", "python", "-k", "test_mode")

    result.assert_outcomes(passed=1)
    assert _printed(result, "test_mode") == [
        "SETTING=auto",
        "VARIABLE=None",
        "CHILD=[]",
    ]


@pytest.mark.parametrize("mode", ["rust", "both"])
def test_rust_and_both_put_the_test_and_its_cgr_on_rust(
    pytester: pytest.Pytester, clean_env: None, mode: str
) -> None:
    pytester.makepyfile(SELECTION_PROBE)

    result = _run(pytester, "-s", "--engine-mode", mode)

    result.assert_outcomes(passed=3)
    assert _printed(result, "test_mode") == [
        "SETTING=rust",
        "VARIABLE=rust",
        f"CHILD={[CHILD_RUST_DEFAULT]}",
    ]
    # The marker and a test's own fixture each win over the mode.
    assert _printed(result, "test_marked") == [
        "SETTING=go=rust",
        "VARIABLE=go=rust",
        f"CHILD={[CHILD_GO_ON_RUST]}",
    ]
    assert _printed(result, "test_own") == [
        "SETTING=python",
        "VARIABLE=python",
        "CHILD=[]",
    ]


def test_the_marker_applies_in_python_mode_too(
    pytester: pytest.Pytester, clean_env: None
) -> None:
    pytester.makepyfile(SELECTION_PROBE)

    result = _run(pytester, "-s", "-k", "test_marked")

    result.assert_outcomes(passed=1)
    assert _printed(result, "test_marked") == [
        "SETTING=go=rust",
        "VARIABLE=go=rust",
        f"CHILD={[CHILD_GO_ON_RUST]}",
    ]


def test_an_invalid_marker_value_fails_its_test(
    pytester: pytest.Pytester, clean_env: None
) -> None:
    pytester.makepyfile(
        "import pytest\n\n\n"
        '@pytest.mark.index_engine("go=fortran")\n'
        "def test_marked():\n    pass\n"
    )

    result = _run(pytester, "--engine-mode", "rust")

    result.assert_outcomes(errors=1)
    assert any(
        "'fortran' is not an engine; use one of auto, python, rust." in line
        for line in result.outlines
    ), result.outlines


def test_both_compares_only_what_has_moved(
    pytester: pytest.Pytester, clean_env: None, go_grammar: None
) -> None:
    # Nothing on Rust, Go on Rust and equal to itself, a repository with no
    # file on Rust, one state compared once and an edited one again, and a
    # single-file target: each of these passes with no report.
    pytester.makepyfile(INDEXING + COMPARISONS)

    result = _run(pytester, "--engine-mode", "both")

    result.assert_outcomes(passed=5)
    assert not any("engines differ" in line for line in result.outlines)


@pytest.mark.parametrize("mode", ["python", "rust"])
def test_python_and_rust_modes_never_compare(
    pytester: pytest.Pytester, clean_env: None, go_grammar: None, mode: str
) -> None:
    pytester.makepyfile(INDEXING + NEVER_COMPARED)

    result = _run(pytester, "--engine-mode", mode)

    result.assert_outcomes(passed=1)


def test_a_planted_difference_is_reported_as_exactly_its_lines(
    pytester: pytest.Pytester,
    clean_env: None,
    go_grammar: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    diff_dir = pytester.path / "parity"
    monkeypatch.setenv(engine_modes.PARITY_DIFF_DIR_ENV, str(diff_dir))
    pytester.makepyfile(INDEXING + PLANTED)

    result = _run(pytester, "--engine-mode", "both")

    # The test's own run passed; the comparison fails it at teardown.
    result.assert_outcomes(passed=1, errors=1)
    report = _report(result)
    heading, graph, *diff = report
    assert fnmatch.fnmatch(
        heading, "Python and Rust engines differ on *repo (go on Rust)"
    )
    assert graph == (
        "  graph: 2 lines only on Python, 1 only on Rust  <- first difference"
    )
    assert len(diff) == 3, report
    assert fnmatch.fnmatch(diff[0], f"    {PLANTED_EDGE}"), diff
    assert diff[1:] == [f"    {line}" for line in PLANTED_NODE]

    # Both sides' graphs and the whole difference, under the test's id.
    written = sorted(
        p.relative_to(diff_dir).as_posix() for p in diff_dir.rglob("*") if p.is_file()
    )
    assert written == [
        "test_a_planted_difference_is_reported_as_exactly_its_lines.py__test_planted/1/graph.diff",
        "test_a_planted_difference_is_reported_as_exactly_its_lines.py__test_planted/1/python.graph",
        "test_a_planted_difference_is_reported_as_exactly_its_lines.py__test_planted/1/rust.graph",
    ]
    test_dir = next(diff_dir.iterdir()) / "1"
    differences = (test_dir / "graph.diff").read_text(encoding="utf-8").splitlines()
    assert len(differences) == 3
    assert fnmatch.fnmatch(differences[0], PLANTED_EDGE)
    assert differences[1:] == list(PLANTED_NODE)


def test_a_failure_a_scratch_run_swallows_is_reported(
    pytester: pytest.Pytester, clean_env: None, go_grammar: None
) -> None:
    pytester.makepyfile(INDEXING + SWALLOWED)

    result = _run(pytester, "--engine-mode", "both")

    result.assert_outcomes(passed=1, errors=1)
    report = _report(result)
    # Each run swallows the pass's failure for both callers and their file.
    swallowed = [
        "    Failed to process calls in repo.main.helper (main.go)*",
        "    Failed to process calls in repo.main.main (main.go)*",
        "    Failed to process calls in *main.go: pass exploded",
    ]
    expected = [
        "  graph: same",
        "  Python run swallowed 3 pass failures:",
        *swallowed,
        "  Rust run swallowed 3 pass failures:",
        *swallowed,
    ]
    assert len(report) == 1 + len(expected), report
    assert all(map(fnmatch.fnmatch, report[1:], expected)), report


def test_a_scratch_run_that_raises_is_reported_not_compared(
    pytester: pytest.Pytester, clean_env: None, go_grammar: None
) -> None:
    pytester.makepyfile(INDEXING + RAISING)

    result = _run(pytester, "--engine-mode", "both")

    # The test's own run returned; the scratch run's error is the report's.
    result.assert_outcomes(passed=1, errors=1)
    report = _report(result)
    assert report[1:4] == [
        "  graph: not compared",
        "  Rust run raised:",
        "    Traceback (most recent call last):",
    ]
    assert report[-1] == "    RuntimeError: the Rust side stopped"


def test_the_pass_failure_gate_leaves_scratch_runs_to_the_comparison(
    temp_repo: Path,
    mock_ingestor: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    _fail_on_swallowed_pass_errors: list[str],
) -> None:
    # A scratch run happens inside the test's own `run`, while the gate
    # listens; what it logs is the comparison's to report, not the gate's.
    (temp_repo / "m.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    original = GraphUpdater._register_generated_sources
    scratch: list[str] = []

    def log_both(self: GraphUpdater) -> None:
        logger.error("Failed to process calls in the test's own run")
        with engine_modes.swallowed_failures(cs.IndexEngine.RUST) as failures:
            logger.error("Failed to process calls in a scratch run")
        scratch.extend(failures)
        original(self)

    monkeypatch.setattr(GraphUpdater, "_register_generated_sources", log_both)

    create_and_run_updater(temp_repo, mock_ingestor)

    assert _fail_on_swallowed_pass_errors == [
        "Failed to process calls in the test's own run"
    ]
    assert scratch == ["Failed to process calls in a scratch run"]
    _fail_on_swallowed_pass_errors.clear()
