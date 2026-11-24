"""Run the suite on the Python engine, the Rust engine, or both, and compare.

The `--engine-mode` option (docs/reports/PHASE1_SEAMS.md, section 2):

- `python`, the default: today's suite, unchanged.
- `rust`: the session runs with `CGR_INDEX_ENGINE=rust`, in its own process
  (the setting) and in any `cgr` a test starts (the variable), so every
  language the build can index on Rust is indexed there and the rest on
  Python. It is set when the session is configured, so the runs of session-
  and module-scoped fixtures are on it too.
- `both`: as `rust`, and each time `GraphUpdater.run` returns on a repository
  holding a language the run's selection puts on Rust, the repository is
  indexed afresh twice more, once with every language on Python and once
  with that selection. When the two graphs differ, the test fails at
  teardown; for a run in a shared fixture, every test that uses it does.

While the build can index no language on Rust (`rust_engine_languages()` is
empty), `rust` runs exactly what `python` runs and `both` indexes nothing
twice: an updater with no language on Rust is skipped before its repository
is read.

`@pytest.mark.index_engine("go=python")` sets one test's selection over the
mode's, for an internals test that must stay on Python until it is
rewritten. The test still runs in every mode.

Loaded as a plugin from pyproject's `addopts` (`-p`), as `suite_shards` is:
a bare `pytest`, CI's invocation, loads the tests' conftest too late to
register an option. In the default mode nothing from `codebase_rag` is
imported, set or wrapped unless a test carries the marker, so it costs
nothing.
"""

from __future__ import annotations

import functools
import hashlib
import json
import os
import re
import tempfile
import traceback
from collections import defaultdict
from collections.abc import Callable, Generator
from contextlib import contextmanager
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

import pytest
from loguru import logger

if TYPE_CHECKING:
    from codebase_rag import constants as cs
    from codebase_rag.capture import CaptureSelection
    from codebase_rag.graph_updater import GraphUpdater
    from codebase_rag.types_defs import ExportRecord

ENGINE_MODE_OPTION = "--engine-mode"
ENGINE_MODE_HELP = (
    "Index on the Python engine (python, the default), on the Rust engine "
    "wherever this build can (rust), or on both, failing a test whose "
    "repository the two index differently (both)."
)
ENGINE_MODE_HEADER = "engine mode: {mode}"
INDEX_ENGINE_MARKER = "index_engine"
INDEX_ENGINE_ENV = "CGR_INDEX_ENGINE"
INDEX_ENGINE_SETTING = "INDEX_ENGINE"
MARKER_USAGE = (
    "@pytest.mark.index_engine takes one CGR_INDEX_ENGINE value, such as "
    "'go=python'; got {args!r}"
)
MARKER_INVALID = "@pytest.mark.index_engine({spec!r}): {error}"
# When set, each difference is also written out in full, under the test's id:
# both sides' graphs and their difference. Read by this plugin only; it is
# not a cgr setting.
PARITY_DIFF_DIR_ENV = "CGR_PARITY_DIFF_DIR"

# Every per-file pass swallows all exceptions so one bad file cannot abandon a
# whole index, and only logs. Nothing looked at those logs, so a bug inside a
# pass dropped the file's edges while the run reported success, and the suite
# saw it at best as unrelated assertion failures elsewhere (issue #1070). One
# entry per pass, and the import pass logs at WARNING rather than ERROR. The
# suite's gate (conftest's `_fail_on_swallowed_pass_errors`) and the scratch
# runs here watch the same ones.
PASS_FAILURE_PREFIXES = (
    "Failed to process calls in ",
    # A caller's local types, contained to that caller (issue #3173).
    "Failed to infer local types in ",
    "Failed to parse or ingest ",
    "Failed to parse imports in ",
    "Error parsing ",
)
PASS_FAILURE_LEVEL = "WARNING"
# The `extra` key every record a scratch run logs carries, naming its engine.
SCRATCH_RUN_KEY = "cgr_engine_scratch_run"

SCRATCH_PREFIX = "cgr-engines-"
SCRATCH_GRAPH = "graph.sqlite"
SCRATCH_STATE = "state"
SCRATCH_HOST = "unused"
GRAPH_FILE = "{engine}.graph"
DIFF_FILE = "graph.diff"
UNREADABLE = b"\0"
UNSAFE_PATH_CHARS = re.compile(r"[^\w.-]")
PATH_SAFE = "_"

REPORT_LINES = 20
REPORT_HEADING = "Python and Rust engines differ on {repo} ({languages} on Rust)"
REPORT_SAME = "  {artefact}: same"
REPORT_DIFFERS = "  {artefact}: {python} lines only on Python, {rust} only on Rust"
REPORT_FIRST = "  <- first difference"
REPORT_LINE = "    {line}"
REPORT_MORE = "    ... and {count} more"
REPORT_NOT_COMPARED = "  {artefact}: not compared"
REPORT_SWALLOWED = "  {engine} run swallowed {count} pass failure{plural}:"
REPORT_RAISED = "  {engine} run raised:"
REPORT_SEPARATOR = "\n\n"
REPORT_SHARED = "In the {scope}-scoped fixture {argname!r}, which this test uses:\n"
REPORT_SECTION = "engine comparison"
TEARDOWN = "teardown"
FUNCTION_SCOPE = "function"
FIXTURE_ID_SEPARATOR = "::"
OUTSIDE_A_TEST = "outside-a-test"
ENGINE_SESSION_PLUGIN = "cgr-engine-session"
ARTEFACT_GRAPH = "graph"
LANGUAGE_SEPARATOR = ", "
DIFF_ONLY_PYTHON = "-"


class EngineMode(StrEnum):
    PYTHON = "python"
    RUST = "rust"
    BOTH = "both"


class Side(NamedTuple):
    """One engine's fresh index of a repository: its graph's lines, the pass
    failures the run swallowed, and the traceback of what it raised, if it
    did not finish."""

    lines: list[str]
    failures: list[str]
    raised: list[str]


class RepoState(NamedTuple):
    """What a fresh index of a repository depends on: the updater's inputs
    and a digest of every eligible file's path and bytes."""

    repo: str
    project: str
    capture: CaptureSelection
    exclude: frozenset[str] | None
    unignore: frozenset[str] | None
    rust: frozenset[cs.SupportedLanguage]
    files: str


type Run = Callable[[GraphUpdater, bool], None]


class SharedFixture(NamedTuple):
    """A session-, package-, module- or class-scoped fixture: its name, its
    scope, and the node it was set up for. Its value is shared by every test
    under that node that uses it, so a difference its run shows is each of
    theirs."""

    argname: str
    scope: str
    node: pytest.Item | pytest.Collector

    @property
    def label(self) -> str:
        return f"{self.node.nodeid}{FIXTURE_ID_SEPARATOR}{self.argname}"


class EngineSession:
    """The `rust` and `both` modes, for the whole session.

    The selection is set when the session is configured and the comparison
    wraps `GraphUpdater.run` from then on, so a run made in a session- or
    module-scoped fixture, which is set up before any function-scoped one,
    is on the mode and is compared like a run in a test. A test that sets
    the variable or patches the setting itself, and the marker, still win
    for that test: their patches are applied over this one and undone after.
    """

    def __init__(self, mode: EngineMode) -> None:
        from codebase_rag.config import settings

        self._patch = pytest.MonkeyPatch()
        self._patch.setattr(settings, INDEX_ENGINE_SETTING, EngineMode.RUST.value)
        self._patch.setenv(INDEX_ENGINE_ENV, EngineMode.RUST.value)
        # The test running now, and the shared fixtures being set up for it,
        # innermost last: what a run that returns is reported against.
        self._item: pytest.Item | None = None
        self._fixtures: list[SharedFixture] = []
        self._compared: defaultdict[str, set[RepoState]] = defaultdict(set)
        self._test_reports: defaultdict[str, list[str]] = defaultdict(list)
        self._fixture_reports: defaultdict[SharedFixture, list[str]] = defaultdict(list)
        if mode is EngineMode.BOTH:
            self._compare_after_every_run()

    def _compare_after_every_run(self) -> None:
        from codebase_rag.graph_updater import GraphUpdater

        # Installed before any test or fixture wraps `run`, so the scratch
        # runs call `run` as the class defines it, and the suite's grammar
        # skip and pass-failure gate, and any wrapper a test adds, see the
        # test's own runs only.
        class_run = GraphUpdater.run

        @functools.wraps(class_run)
        def run_then_compare(updater: GraphUpdater, force: bool = False) -> None:
            class_run(updater, force)
            # After the run, so one that skips for a missing grammar or fails
            # pays nothing extra.
            self._compare(updater, class_run)

        self._patch.setattr(GraphUpdater, "run", run_then_compare)

    def _compare(self, updater: GraphUpdater, run: Run) -> None:
        if not updater.rust_languages:
            return
        fixture = self._fixtures[-1] if self._fixtures else None
        if fixture is not None:
            label = fixture.label
        elif self._item is not None:
            label = self._item.nodeid
        else:
            label = OUTSIDE_A_TEST
        report = compare_engines(updater, run, self._compared[label], label)
        if report is None:
            return
        if fixture is not None:
            self._fixture_reports[fixture].append(report)
        elif self._item is not None:
            self._test_reports[label].append(report)
        else:
            # No test to fail: collection, or a thread a test left running.
            pytest.fail(report, pytrace=False)

    def _reports_for(self, item: pytest.Item) -> list[str]:
        """What the test's own runs found, and what the runs of the shared
        fixtures it uses found, each once."""
        self._compared.pop(item.nodeid, None)
        chain = item.listchain()
        # Only a test function requests fixtures; any other item uses none.
        uses = item.fixturenames if isinstance(item, pytest.Function) else ()
        shared = [
            REPORT_SHARED.format(scope=fixture.scope, argname=fixture.argname) + report
            for fixture, reports in self._fixture_reports.items()
            if fixture.argname in uses and fixture.node in chain
            for report in reports
        ]
        return [*shared, *self._test_reports.pop(item.nodeid, [])]

    @pytest.hookimpl(wrapper=True)
    def pytest_runtest_protocol(
        self, item: pytest.Item, nextitem: pytest.Item | None
    ) -> Generator[None, bool | None, bool | None]:
        self._item = item
        try:
            return (yield)
        finally:
            self._item = None

    @pytest.hookimpl(wrapper=True)
    def pytest_fixture_setup[T](
        self, fixturedef: pytest.FixtureDef[T], request: pytest.FixtureRequest
    ) -> Generator[None, T, T]:
        if fixturedef.scope == FUNCTION_SCOPE:
            return (yield)
        self._fixtures.append(
            SharedFixture(fixturedef.argname, fixturedef.scope, request.node)
        )
        try:
            return (yield)
        finally:
            self._fixtures.pop()

    @pytest.hookimpl(wrapper=True)
    def pytest_runtest_teardown(
        self, item: pytest.Item, nextitem: pytest.Item | None
    ) -> Generator[None, None, None]:
        # After every fixture's teardown, shared ones included, so a run made
        # there is reported too; at teardown, as the pass-failure gate fails,
        # so the test's own assertions still report.
        try:
            yield
        except BaseException:
            if reports := self._reports_for(item):
                item.add_report_section(
                    TEARDOWN, REPORT_SECTION, REPORT_SEPARATOR.join(reports)
                )
            raise
        if reports := self._reports_for(item):
            pytest.fail(REPORT_SEPARATOR.join(reports), pytrace=False)

    def pytest_unconfigure(self, config: pytest.Config) -> None:
        self._patch.undo()


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        ENGINE_MODE_OPTION,
        choices=[mode.value for mode in EngineMode],
        default=EngineMode.PYTHON.value,
        help=ENGINE_MODE_HELP,
    )


def pytest_configure(config: pytest.Config) -> None:
    mode = EngineMode(config.getoption(ENGINE_MODE_OPTION))
    if mode is not EngineMode.PYTHON:
        config.pluginmanager.register(EngineSession(mode), ENGINE_SESSION_PLUGIN)


def pytest_report_header(config: pytest.Config) -> str:
    return ENGINE_MODE_HEADER.format(mode=config.getoption(ENGINE_MODE_OPTION))


def _marked_selection(node: pytest.Item) -> str | None:
    marker = node.get_closest_marker(INDEX_ENGINE_MARKER)
    if marker is None:
        return None
    match marker.args:
        case (str() as spec,) if not marker.kwargs:
            return spec
        case _:
            pytest.fail(MARKER_USAGE.format(args=marker.args), pytrace=False)


@pytest.fixture(autouse=True)
def _engine_mode(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The selection the test's marker asks for, over the mode's, for the
    runs in the test and in its function-scoped fixtures, and in any `cgr`
    they start. Shared fixtures keep the mode's: their value is shared with
    tests the marker is not on."""
    from_marker = _marked_selection(request.node)
    if from_marker is None:
        return
    from codebase_rag.config import settings
    from codebase_rag.index_engine import index_engine_error

    if (error := index_engine_error(from_marker)) is not None:
        pytest.fail(MARKER_INVALID.format(spec=from_marker, error=error), pytrace=False)
    monkeypatch.setattr(settings, INDEX_ENGINE_SETTING, from_marker.lower())
    monkeypatch.setenv(INDEX_ENGINE_ENV, from_marker)


def _languages_on_rust(
    updater: GraphUpdater, files: list[tuple[str, str, str]]
) -> frozenset[cs.SupportedLanguage]:
    from codebase_rag.language_spec import get_language_for_extension

    held = {
        language
        for _dirpath, filename, _rel_path in files
        if (language := get_language_for_extension(Path(filename).suffix))
    }
    return frozenset(held & updater.rust_languages)


def _state(updater: GraphUpdater, files: list[tuple[str, str, str]]) -> RepoState:
    digest = hashlib.sha256()
    for dirpath, filename, rel_path in files:
        digest.update(rel_path.encode())
        try:
            digest.update(hashlib.sha256(Path(dirpath, filename).read_bytes()).digest())
        except OSError:
            digest.update(UNREADABLE)
    return RepoState(
        repo=str(updater.repo_path),
        project=updater.project_name,
        capture=updater.capture,
        exclude=updater.exclude_paths,
        unignore=updater.unignore_paths,
        rust=updater.rust_languages,
        files=digest.hexdigest(),
    )


def compare_engines(
    updater: GraphUpdater, run: Run, compared: set[RepoState], label: str
) -> str | None:
    """How the Python and Rust engines' fresh indexes of `updater`'s
    repository differ, or None when they do not or need no comparing.

    Compared only when the two selections part on a language the repository
    holds: never for an updater with no language on Rust (known without
    reading the repository), nor for a single-file target, and once per
    distinct state of the repository within a test or shared fixture,
    recorded in `compared`. An incremental run is compared as a fresh index
    of the files it saw. `label` names where the run was made, for
    `CGR_PARITY_DIFF_DIR`.
    """
    from codebase_rag.utils.path_utils import walk_eligible_files

    if not updater.rust_languages or updater._single_file is not None:
        return None
    files = list(
        walk_eligible_files(
            updater.repo_path, updater.exclude_paths, updater.unignore_paths
        )
    )
    languages = _languages_on_rust(updater, files)
    if not languages:
        return None
    state = _state(updater, files)
    if state in compared:
        return None
    compared.add(state)
    with tempfile.TemporaryDirectory(
        prefix=SCRATCH_PREFIX, ignore_cleanup_errors=True
    ) as tmp:
        python, rust = (
            _side(updater, engine, Path(tmp) / engine, run) for engine in _engines()
        )
    report = _report(updater, languages, python, rust)
    if report is not None and (root := os.environ.get(PARITY_DIFF_DIR_ENV)):
        _write_difference(Path(root), label, len(compared), python, rust)
    return report


def _engines() -> tuple[cs.IndexEngine, cs.IndexEngine]:
    from codebase_rag import constants as cs

    return cs.IndexEngine.PYTHON, cs.IndexEngine.RUST


@contextmanager
def swallowed_failures(engine: cs.IndexEngine) -> Generator[list[str]]:
    """The pass failures logged inside the block, which is one scratch run on
    `engine`'s side.

    Every record the block logs carries `SCRATCH_RUN_KEY`, so the suite's
    pass-failure gate, which is listening while the test's own `run`
    returns, leaves the scratch run's failures to the comparison's report.
    """
    failures: list[str] = []
    sink = logger.add(
        lambda message: failures.append(message.record["message"]),
        level=PASS_FAILURE_LEVEL,
        filter=lambda record: (
            record["extra"].get(SCRATCH_RUN_KEY) == engine
            and record["message"].startswith(PASS_FAILURE_PREFIXES)
        ),
    )
    try:
        with logger.contextualize(**{SCRATCH_RUN_KEY: engine}):
            yield failures
    finally:
        logger.remove(sink)


def fresh_index(
    updater: GraphUpdater, engine: cs.IndexEngine, scratch: Path, run: Run
) -> ExportRecord:
    """`updater`'s repository indexed afresh into a scratch embedded graph,
    and that graph exported as `cgr start -o` writes it, as JSON.

    On `engine`'s side: every language on Python, or the test's own
    selection for Rust. A full index (`force`), with its own state directory
    and no embeddings, by a replica of `updater`, so neither changes the
    test's graph nor reads its sync state.
    """
    from codebase_rag import constants as cs
    from codebase_rag.graph_dialects import DIALECT_SQLITE, get_dialect
    from codebase_rag.index_engine import parse_index_engine
    from codebase_rag.services.graph_service import MemgraphIngestor

    scratch.mkdir(parents=True)
    selection = (
        parse_index_engine(cs.IndexEngine.PYTHON)
        if engine == cs.IndexEngine.PYTHON
        else None
    )
    with MemgraphIngestor(
        host=SCRATCH_HOST,
        port=0,
        dialect=get_dialect(DIALECT_SQLITE),
        sqlite_path=str(scratch / SCRATCH_GRAPH),
    ) as ingestor:
        ingestor.ensure_constraints()
        replica = updater.replica(
            ingestor, state_dir=scratch / SCRATCH_STATE, index_engine=selection
        )
        run(replica, True)
        export: ExportRecord = json.loads(
            json.dumps(ingestor.export_graph_to_dict(), ensure_ascii=False)
        )
        return export


def _side(
    updater: GraphUpdater, engine: cs.IndexEngine, scratch: Path, run: Run
) -> Side:
    from codebase_rag.canonical_graph import canonical

    with swallowed_failures(engine) as failures:
        try:
            export = fresh_index(updater, engine, scratch, run)
        except Exception as error:
            # Reported with the comparison rather than raised from the
            # test's own `run`, which returned.
            raised = "".join(traceback.format_exception(error)).splitlines()
            return Side([], failures, raised)
    return Side(canonical(export, updater.repo_path), failures, [])


def _artefact(name: str, diff: list[str], first: bool) -> list[str]:
    if not diff:
        return [REPORT_SAME.format(artefact=name)]
    python = sum(1 for line in diff if line.startswith(DIFF_ONLY_PYTHON))
    heading = REPORT_DIFFERS.format(
        artefact=name, python=python, rust=len(diff) - python
    )
    lines = [f"{heading}{REPORT_FIRST}" if first else heading]
    lines.extend(REPORT_LINE.format(line=line) for line in diff[:REPORT_LINES])
    if len(diff) > REPORT_LINES:
        lines.append(REPORT_MORE.format(count=len(diff) - REPORT_LINES))
    return lines


def _report(
    updater: GraphUpdater,
    languages: frozenset[cs.SupportedLanguage],
    python: Side,
    rust: Side,
) -> str | None:
    """The comparison's message: the repository and the languages on Rust,
    then each artefact in pipeline order with the first that differs marked,
    then any failure a scratch run swallowed or raised."""
    from codebase_rag.canonical_graph import difference

    finished = not python.raised and not rust.raised
    diff = difference(python.lines, rust.lines) if finished else []
    if finished and not diff and not python.failures and not rust.failures:
        return None
    lines = [
        REPORT_HEADING.format(
            repo=updater.repo_path,
            languages=LANGUAGE_SEPARATOR.join(sorted(languages)),
        ),
        *(
            _artefact(ARTEFACT_GRAPH, diff, first=bool(diff))
            if finished
            else [REPORT_NOT_COMPARED.format(artefact=ARTEFACT_GRAPH)]
        ),
    ]
    for engine, side in zip(_engines(), (python, rust), strict=True):
        name = engine.capitalize()
        if count := len(side.failures):
            plural = "" if count == 1 else "s"
            lines.append(
                REPORT_SWALLOWED.format(engine=name, count=count, plural=plural)
            )
            lines.extend(
                REPORT_LINE.format(line=line) for line in side.failures[:REPORT_LINES]
            )
        if side.raised:
            lines.append(REPORT_RAISED.format(engine=name))
            lines.extend(REPORT_LINE.format(line=line) for line in side.raised)
    return "\n".join(lines)


def _write_difference(
    root: Path, label: str, number: int, python: Side, rust: Side
) -> None:
    from codebase_rag.canonical_graph import difference, write_graph

    target = root / UNSAFE_PATH_CHARS.sub(PATH_SAFE, label) / str(number)
    target.mkdir(parents=True, exist_ok=True)
    for engine, side in zip(_engines(), (python, rust), strict=True):
        write_graph(target / GRAPH_FILE.format(engine=engine), side.lines)
    write_graph(target / DIFF_FILE, difference(python.lines, rust.lines))
