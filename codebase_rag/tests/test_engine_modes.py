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
        # Uncoloured: loguru colours its default sink on CI runners
        # (GITHUB_ACTIONS), and the lines are compared as text.
        env={
            **os.environ, "LOGURU_LEVEL": "DEBUG", "LOGURU_COLORIZE": "0",
            "PYTHONUTF8": "1",
        },
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

# Differences planted in the Rust side's hand-off files, as its run left
# them in its scratch hand-off directory: a definition missing from the
# project index (and the CALLS edge it would get), two files of the plan in
# another order, and a plan in another format version.
PLANTED_HANDOFFS = """
from codebase_rag.tests import engine_modes


def plant(edit):
    @pytest.fixture
    def planted(monkeypatch):
        original = engine_modes.fresh_index

        def edited(updater, engine, scratch, run):
            export = original(updater, engine, scratch, run)
            if engine == cs.IndexEngine.RUST:
                edit(scratch / engine_modes.SCRATCH_HANDOFF, export)
            return export

        monkeypatch.setattr(engine_modes, "fresh_index", edited)

    return planted


def drop_helper(handoffs, export):
    index = handoffs / "index.txt"
    lines = index.read_bytes().split(b"\\n")
    kept = [line for line in lines if not line.startswith(b"definition\\trepo.main.helper\\t")]
    index.write_bytes(b"\\n".join(kept))
    export["relationships"] = [
        rel for rel in export["relationships"] if rel["type"] != "CALLS"
    ]


def swap_files(handoffs, export):
    plan = handoffs / "files.txt"
    header, first, second, *rest = plan.read_bytes().split(b"\\n")
    plan.write_bytes(b"\\n".join([header, second, first, *rest]))


def newer_version(handoffs, export):
    plan = handoffs / "files.txt"
    plan.write_bytes(plan.read_bytes().replace(b" files.txt 1\\n", b" files.txt 2\\n", 1))


index_planted = plant(drop_helper)
order_planted = plant(swap_files)
version_planted = plant(newer_version)


def two_go_files(tmp_path):
    repo = go_repo(tmp_path)
    (repo / "zeta.go").write_text("package main\\n\\nfunc zeta() {}\\n", encoding="utf-8")
    return repo


def test_index(go_on_rust, index_planted, tmp_path):
    index(go_repo(tmp_path), tmp_path)


def test_order(go_on_rust, order_planted, tmp_path):
    index(two_go_files(tmp_path), tmp_path)


def test_version(go_on_rust, version_planted, tmp_path):
    index(go_repo(tmp_path), tmp_path)
"""

# What a run sees of its selection in a session-scoped fixture, a
# module-scoped one, and the test itself, with and without the marker.
SCOPED_PROBE = """
import os
from unittest.mock import MagicMock

import pytest

from codebase_rag.config import settings
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers


def selection(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "m.py").write_text("def f():\\n    return 1\\n", encoding="utf-8")
    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=MagicMock(), repo_path=repo, parsers=parsers, queries=queries,
        skip_embeddings=True, state_dir=tmp_path / "state",
    )
    updater.run()
    return (
        f"setting={settings.INDEX_ENGINE} "
        f"variable={os.environ.get('CGR_INDEX_ENGINE')} "
        f"updater={updater.index_engine.default}"
    )


@pytest.fixture(scope="session")
def session_selection(tmp_path_factory):
    return selection(tmp_path_factory.mktemp("session"))


@pytest.fixture(scope="module")
def module_selection(tmp_path_factory):
    return selection(tmp_path_factory.mktemp("module"))


def test_fixtures(session_selection, module_selection, tmp_path):
    print(f"\\nPROBE session {session_selection}")
    print(f"PROBE module {module_selection}")
    print(f"PROBE test {selection(tmp_path)}")


@pytest.mark.index_engine("python")
def test_marked(session_selection, module_selection, tmp_path):
    print(f"\\nPROBE marked-test {selection(tmp_path)}")
"""

# Go on Rust inside a broader-scoped fixture, with the scratch indexes
# counted and, for `planted`, one edge dropped and one property changed on the
# Rust side. Two tests use `planted`; one uses neither fixture.
SCOPED_COMPARISON = """
from codebase_rag.tests import engine_modes


def plant(original):
    def planted(updater, engine, scratch, run):
        export = original(updater, engine, scratch, run)
        if engine == cs.IndexEngine.RUST:
            export["relationships"] = [
                rel for rel in export["relationships"] if rel["type"] != "CALLS"
            ]
            for node in export["nodes"]:
                if "File" in node["labels"]:
                    node["properties"]["extension"] = ".gox"
        return export

    return planted


def index_go_on_rust(tmp_path, plant_difference):
    made = []
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            index_engine, "rust_engine_languages",
            lambda: frozenset({cs.SupportedLanguage.GO}),
        )
        original = GraphUpdater.replica

        def counted(self, ingestor, **kwargs):
            made.append(kwargs.get("index_engine"))
            return original(self, ingestor, **kwargs)

        mp.setattr(GraphUpdater, "replica", counted)
        if plant_difference:
            mp.setattr(engine_modes, "fresh_index", plant(engine_modes.fresh_index))
        index(go_repo(tmp_path), tmp_path)
    return made


@pytest.fixture(scope="{scope}")
def equal(tmp_path_factory):
    return index_go_on_rust(tmp_path_factory.mktemp("equal"), False)


@pytest.fixture(scope="{scope}")
def planted(tmp_path_factory):
    return index_go_on_rust(tmp_path_factory.mktemp("planted"), True)


def test_equal(equal):
    # Compared, once with every language on Python and once on the test's
    # own selection, and found the same.
    assert len(equal) == 2


def test_planted_first(planted):
    assert len(planted) == 2


def test_planted_second(planted):
    assert len(planted) == 2


def test_unrelated():
    pass
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


def _report(result: pytest.RunResult, after: int = 0) -> list[str]:
    """The lines of the first engine report in the session's output, or the
    first after its line `after`."""
    lines = result.outlines
    start = next(
        i
        for i, line in enumerate(lines)
        if i >= after and line.startswith("Python and Rust engines differ on ")
    )
    end = start + 1
    while end < len(lines) and lines[end].startswith("  "):
        end += 1
    return lines[start:end]


def _reports(result: pytest.RunResult) -> dict[str, list[str]]:
    """Each engine report in the session's output, by the test that failed
    at teardown with it."""
    marker = "ERROR at teardown of "
    return {
        line.strip("_ ").removeprefix(marker): _report(result, i)
        for i, line in enumerate(result.outlines)
        if marker in line
    }


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
    heading, files, index, graph, *diff = report
    assert fnmatch.fnmatch(
        heading, "Python and Rust engines differ on *repo (go on Rust)"
    )
    # The hand-offs, in pipeline order before the graph, agree.
    assert [files, index] == ["  files: same", "  index: same"]
    assert graph == (
        "  graph: 2 lines only on Python, 1 only on Rust  <- first difference"
    )
    assert len(diff) == 3, report
    assert fnmatch.fnmatch(diff[0], f"    {PLANTED_EDGE}"), diff
    assert diff[1:] == [f"    {line}" for line in PLANTED_NODE]

    # Both sides' hand-off files, the graph among them, and each whole
    # difference, under the test's id.
    test_dir = (
        diff_dir
        / "test_a_planted_difference_is_reported_as_exactly_its_lines.py__test_planted"
        / "1"
    )
    written = sorted(
        p.relative_to(test_dir).as_posix() for p in diff_dir.rglob("*") if p.is_file()
    )
    assert written == [
        "files.diff",
        "graph.diff",
        "index.diff",
        "python/files.txt",
        "python/graph.txt",
        "python/index.txt",
        "rust/files.txt",
        "rust/graph.txt",
        "rust/index.txt",
    ]
    differences = (test_dir / "graph.diff").read_text(encoding="utf-8").splitlines()
    assert len(differences) == 3
    assert fnmatch.fnmatch(differences[0], PLANTED_EDGE)
    assert differences[1:] == list(PLANTED_NODE)
    for artefact in ("files", "index"):
        assert (test_dir / f"{artefact}.diff").read_bytes() == b""
        python = (test_dir / "python" / f"{artefact}.txt").read_bytes()
        assert python.startswith(f"# cgr handoff {artefact}.txt 1\n".encode())
        assert (test_dir / "rust" / f"{artefact}.txt").read_bytes() == python


def test_both_reports_the_hand_offs_before_the_graph_marking_the_first_difference(
    pytester: pytest.Pytester, clean_env: None, go_grammar: None
) -> None:
    pytester.makepyfile(INDEXING + PLANTED_HANDOFFS)

    result = _run(pytester, "--engine-mode", "both")

    result.assert_outcomes(passed=3, errors=3)
    reports = _reports(result)
    assert sorted(reports) == ["test_index", "test_order", "test_version"]
    # A definition the Rust side's index lacks: the first stage at which the
    # engines part is the index, and the graph differs after it.
    assert reports["test_index"][1:] == [
        "  files: same",
        "  index: 1 lines only on Python, 0 only on Rust  <- first difference",
        '    - definition\trepo.main.helper\t{"label":"Function"}',
        "  graph: 1 lines only on Python, 0 only on Rust",
        reports["test_index"][-1],
    ]
    assert fnmatch.fnmatch(reports["test_index"][-1], f"    {PLANTED_EDGE}")
    # The same files in another order: the plan is compared in order.
    order = reports["test_order"][1:]
    assert order[0] == (
        "  files: 1 lines only on Python, 1 only on Rust  <- first difference"
    )
    assert fnmatch.fnmatch(order[1], '    + file\tzeta.go\t{"language":"go",*}')
    assert fnmatch.fnmatch(order[2], '    - file\tzeta.go\t{"language":"go",*}')
    assert order[3:] == ["  index: same", "  graph: same"]
    # Another format version: its lines are not compared, its header names it.
    assert reports["test_version"][1:] == [
        "  files: 1 lines only on Python, 1 only on Rust  <- first difference",
        "    - # cgr handoff files.txt 1",
        "    + # cgr handoff files.txt 2",
        "  index: same",
        "  graph: same",
    ]


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
        "  files: same",
        "  index: same",
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
    assert report[1:6] == [
        "  files: not compared",
        "  index: not compared",
        "  graph: not compared",
        "  Rust run raised:",
        "    Traceback (most recent call last):",
    ]
    assert report[-1] == "    RuntimeError: the Rust side stopped"


@pytest.mark.parametrize("mode", ["rust", "both"])
def test_broader_scoped_fixtures_index_on_the_mode_and_the_marker_wins_in_its_test(
    pytester: pytest.Pytester, clean_env: None, mode: str
) -> None:
    # A session- or module-scoped fixture is set up before any function-scoped
    # one, so the mode is applied for the whole session, not per test.
    pytester.makepyfile(SCOPED_PROBE)

    result = _run(pytester, "-s", "--engine-mode", mode)

    result.assert_outcomes(passed=2)
    on_rust = ["setting=rust variable=rust updater=rust"]
    assert _printed(result, "session") == on_rust
    assert _printed(result, "module") == on_rust
    assert _printed(result, "test") == on_rust
    assert _printed(result, "marked-test") == [
        "setting=python variable=python updater=python"
    ]


def test_python_mode_leaves_broader_scoped_fixtures_alone(
    pytester: pytest.Pytester, clean_env: None
) -> None:
    pytester.makepyfile(SCOPED_PROBE)

    result = _run(pytester, "-s")

    result.assert_outcomes(passed=2)
    default = ["setting=auto variable=None updater=auto"]
    assert _printed(result, "session") == default
    assert _printed(result, "module") == default
    assert _printed(result, "test") == default
    assert _printed(result, "marked-test") == [
        "setting=python variable=python updater=python"
    ]


@pytest.mark.parametrize("scope", ["module", "session"])
def test_both_compares_a_broader_scoped_fixtures_run_and_reports_on_its_tests(
    pytester: pytest.Pytester, clean_env: None, go_grammar: None, scope: str
) -> None:
    pytester.makepyfile(INDEXING + SCOPED_COMPARISON.replace("{scope}", scope))

    result = _run(pytester, "--engine-mode", "both")

    # Each test that uses the fixture fails at teardown with the report; the
    # tests that do not pass.
    result.assert_outcomes(passed=4, errors=2)
    teardowns = [
        line.strip("_ ") for line in result.outlines if "ERROR at teardown" in line
    ]
    assert teardowns == [
        "ERROR at teardown of test_planted_first",
        "ERROR at teardown of test_planted_second",
    ]
    lines = result.outlines
    found_in = f"In the {scope}-scoped fixture 'planted', which this test uses:"
    starts = [i for i, line in enumerate(lines) if line == found_in]
    assert len(starts) == 2, lines
    assert all(
        lines[i + 1].startswith("Python and Rust engines differ on ") for i in starts
    )
    report = _report(result)
    heading, files, index, graph, *diff = report
    assert fnmatch.fnmatch(
        heading, "Python and Rust engines differ on *repo (go on Rust)"
    )
    assert [files, index] == ["  files: same", "  index: same"]
    assert graph == (
        "  graph: 2 lines only on Python, 1 only on Rust  <- first difference"
    )
    assert len(diff) == 3, report
    assert fnmatch.fnmatch(diff[0], f"    {PLANTED_EDGE}"), diff
    assert diff[1:] == [f"    {line}" for line in PLANTED_NODE]


# The frame's file plan checked on every index (docs/reports/PHASE2_FRAME.md,
# section 4), whatever language is on Rust: a Python repository indexed
# cleanly, with the frame's walk planted with a difference (its first file
# left out), and so planted while the test patches Python's walk.
FRAME = """
from codebase_rag import engine_frame
from codebase_rag.utils import path_utils


@pytest.fixture
def planted_frame(monkeypatch):
    original = engine_frame.frame_file_plan

    def plant(repo_path, exclude=None, unignore=None):
        plan = original(repo_path, exclude, unignore)
        return plan._replace(files=plan.files[1:], hashes=plan.hashes[1:])

    monkeypatch.setattr(engine_frame, "frame_file_plan", plant)


def py_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("def a():\\n    return 1\\n", encoding="utf-8")
    (repo / "b.py").write_text("def b():\\n    return 2\\n", encoding="utf-8")
    return repo


def test_probe():
    check = type(engine_frame.CHECK).__name__
    print(f"\\nPROBE test_probe ENABLED={engine_frame.ENABLED} CHECK={check}")


def test_clean(tmp_path):
    index(py_repo(tmp_path), tmp_path)


def test_planted(planted_frame, tmp_path):
    index(py_repo(tmp_path), tmp_path)


def test_patched_walk(planted_frame, monkeypatch, tmp_path):
    monkeypatch.setattr(path_utils, "is_symlink_entry", lambda _path: False)
    index(py_repo(tmp_path), tmp_path)
"""


def _frame_report(result: pytest.RunResult) -> list[str]:
    """The lines of the first frame report in the session's output."""
    lines = result.outlines
    start = next(
        i
        for i, line in enumerate(lines)
        if line.startswith("The frame's file plan differs from Python's on ")
    )
    end = start + 1
    while end < len(lines) and lines[end].startswith("    "):
        end += 1
    return lines[start:end]


@pytest.mark.parametrize("mode", ["both", "rust"])
def test_every_index_checks_the_frame_s_file_plan_failing_on_a_difference(
    pytester: pytest.Pytester, clean_env: None, mode: str
) -> None:
    pytester.makepyfile(INDEXING + FRAME)

    result = _run(pytester, "-s", "--engine-mode", mode)

    # Only the planted run differs; a test patching Python's walk is not
    # compared. The planted test's own run passed, and fails at teardown.
    result.assert_outcomes(passed=4, errors=1)
    assert _printed(result, "test_probe") == [
        f"ENABLED={mode == 'rust'} CHECK=FramePlanCheck"
    ]
    assert any("ERROR at teardown of test_planted" in line for line in result.outlines)
    heading, *lines = _frame_report(result)
    assert fnmatch.fnmatch(
        heading,
        "The frame's file plan differs from Python's on *repo:"
        " 1 lines only in Python's, 0 only in the frame's",
    )
    assert len(lines) == 1
    assert fnmatch.fnmatch(lines[0], "    - file\ta.py\t*\t*repo\ta.py")


STRUCTURE = """
from codebase_rag import engine_frame


@pytest.fixture
def planted_structure(monkeypatch):
    original = engine_frame.frame_structure

    def plant(*args):
        found = original(*args)
        *kept, last = found.rows
        properties = tuple(
            (key, "renamed" if key == "name" else value)
            for key, value in last.node.properties
        )
        node = last.node._replace(properties=properties)
        return found._replace(rows=(*kept, last._replace(node=node)))

    monkeypatch.setattr(engine_frame, "frame_structure", plant)


def folder_repo(tmp_path):
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "a.py").write_text("X = 1\\n", encoding="utf-8")
    return repo


def test_structure_clean(tmp_path):
    index(folder_repo(tmp_path), tmp_path)


def test_structure_planted(planted_structure, tmp_path):
    updater = index(folder_repo(tmp_path), tmp_path)
    folders = [
        call.args[1]["name"]
        for call in updater.ingestor.ensure_node_batch.call_args_list
        if call.args[0] == "Folder"
    ]
    print(f"\\nPROBE test_structure_planted {folders}")
"""


@pytest.mark.parametrize("mode", ["both", "rust"])
def test_every_index_checks_the_frame_s_structure_failing_on_a_difference(
    pytester: pytest.Pytester, clean_env: None, mode: str
) -> None:
    pytester.makepyfile(INDEXING + STRUCTURE)

    result = _run(pytester, "-s", "--engine-mode", mode)

    # The planted run's own test passed, and fails at teardown; `rust` wrote
    # the frame's rows, which the strict check still reports.
    result.assert_outcomes(passed=2, errors=1)
    assert any(
        "ERROR at teardown of test_structure_planted" in line
        for line in result.outlines
    )
    assert _printed(result, "test_structure_planted") == [
        "['renamed']" if mode == "rust" else "['pkg']"
    ]
    start = next(
        i
        for i, line in enumerate(result.outlines)
        if line.startswith("The frame's structure differs from Python's on ")
    )
    heading, removed, added = result.outlines[start : start + 3]
    assert heading.endswith(": 1 lines only in Python's, 1 only in the frame's")
    assert fnmatch.fnmatch(removed, "    - node\tFolder\tpath=pkg\tname=pkg\t*")
    assert fnmatch.fnmatch(added, "    + node\tFolder\tpath=pkg\tname=renamed\t*")


MODULES = """
from codebase_rag import engine_frame


@pytest.fixture
def planted_modules(monkeypatch):
    original = engine_frame.frame_module_plan

    def plant(*args):
        return tuple(
            (f"{qn}.renamed", path, language) for qn, path, language in original(*args)
        )

    monkeypatch.setattr(engine_frame, "frame_module_plan", plant)


def module_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("X = 1\\n", encoding="utf-8")
    return repo


def test_modules_clean(tmp_path):
    index(module_repo(tmp_path), tmp_path)


def test_modules_planted(planted_modules, tmp_path):
    updater = index(module_repo(tmp_path), tmp_path)
    modules = sorted(updater.factory.definition_processor.module_qn_to_file_path)
    print(f"\\nPROBE test_modules_planted {modules}")
"""


@pytest.mark.parametrize("mode", ["both", "rust"])
def test_every_fresh_index_checks_the_frame_s_module_names_failing_on_a_difference(
    pytester: pytest.Pytester, clean_env: None, mode: str
) -> None:
    pytester.makepyfile(INDEXING + MODULES)

    result = _run(pytester, "-s", "--engine-mode", mode)

    # The planted run's own test passed, and fails at teardown; the run kept
    # Python's module names in either mode.
    result.assert_outcomes(passed=2, errors=1)
    assert any(
        "ERROR at teardown of test_modules_planted" in line for line in result.outlines
    )
    assert _printed(result, "test_modules_planted") == ["['repo.a']"]
    start = next(
        i
        for i, line in enumerate(result.outlines)
        if line.startswith("The frame's module names differ from Python's on ")
    )
    heading, removed, added = result.outlines[start : start + 3]
    assert heading.endswith(": 1 lines only in Python's, 1 only in the frame's")
    assert fnmatch.fnmatch(
        removed, '    - module\t*.a\t{"language":"python","path":"a.py"}'
    )
    assert fnmatch.fnmatch(
        added, '    + module\t*.a.renamed\t{"language":"python","path":"a.py"}'
    )


def test_python_mode_leaves_the_frame_alone(
    pytester: pytest.Pytester, clean_env: None
) -> None:
    pytester.makepyfile(INDEXING + FRAME)

    result = _run(pytester, "-s", "--engine-mode", "python")

    result.assert_outcomes(passed=4)
    assert _printed(result, "test_probe") == ["ENABLED=False CHECK=NoneType"]


def test_the_pass_failure_gate_leaves_scratch_runs_to_the_comparison(
    temp_repo: Path,
    mock_ingestor: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    _fail_on_swallowed_pass_errors: list[str],
) -> None:
    # A scratch run happens inside the test's own `run`, while the gate
    # listens; what it logs is the comparison's to report, not the gate's.
    # Logged from a stage only `run` calls, once: the suite's grammar probe
    # calls others, such as `_register_generated_sources`, before the run
    # whenever a grammar is missing, as on a base install.
    (temp_repo / "m.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    original = GraphUpdater._process_files
    scratch: list[str] = []

    def log_both(self: GraphUpdater, force: bool = False) -> None:
        logger.error("Failed to process calls in the test's own run")
        with engine_modes.swallowed_failures(cs.IndexEngine.RUST) as failures:
            logger.error("Failed to process calls in a scratch run")
        scratch.extend(failures)
        original(self, force)

    monkeypatch.setattr(GraphUpdater, "_process_files", log_both)

    create_and_run_updater(temp_repo, mock_ingestor)

    assert _fail_on_swallowed_pass_errors == [
        "Failed to process calls in the test's own run"
    ]
    assert scratch == ["Failed to process calls in a scratch run"]
    _fail_on_swallowed_pass_errors.clear()
