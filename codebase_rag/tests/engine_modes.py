"""Run the suite on the Python engine, the Rust engine, or both, and compare.

The `--engine-mode` option (docs/reports/PHASE1_SEAMS.md, section 2):

- `python`, the default: today's suite, unchanged, with the engine's frame
  as users run it: on, beside Python's walk, warning of a difference and
  going on with Python's answers (engine_frame.py).
- `rust`: the session runs with `CGR_INDEX_ENGINE=rust`, in its own process
  (the setting) and in any `cgr` a test starts (the variable), so every
  language the build can index on Rust is indexed there and the rest on
  Python. It is set when the session is configured, so the runs of session-
  and module-scoped fixtures are on it too.
- `both`: as `rust`, and each time `GraphUpdater.run` returns on a repository
  holding a language the run's selection puts on Rust, the repository is
  indexed afresh twice more, once with every language on Python and once
  with that selection, each writing its hand-off files. The two runs' file
  plans, project indexes and graphs are compared in that order, the order of
  the pipeline, so the first that differs names the stage at which the
  engines part. When any differs, the test fails at teardown; for a run in a
  shared fixture, every test that uses it does.

The engine's frame (docs/reports/PHASE2_FRAME.md, section 4) is checked in
both: every index a test makes compares the frame's file plan with Python's
walk, its change detection (the in-sync check, the files new, changed and
deleted, the hash cache and folder times it would publish), its structure
(the Package and Folder rows, the map of each folder's package, and the
File rows of the files indexed) and, on a run whose module map starts
empty, its module-name plan (which file claims each module's name) with
Python's, whatever language is on Rust, at no second index, and a
difference fails the test as above. `both` turns the frame off
(`engine_frame.ENABLED`) and keeps Python's answers for the run; `rust`
leaves it on, so the run goes on with the frame's, but for the module
names, which stay Python's until phase 3. A test that patches the built-in names or endings the frame holds copies
of, or what Python's walk decides with or reads files by, is not compared;
nor is a run's change detection while a test patches what Python's decides
with, reads the state files by, or its clock, nor its structure while a
test patches what Python's structure pass decides with, nor its module
names while a test patches what Python names modules by or parses a file
into the map through.

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

import builtins
import functools
import hashlib
import json
import os
import re
import tempfile
import traceback
from collections import defaultdict
from collections.abc import Callable, Generator, Sequence
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
# both sides' hand-off files and each one's difference. Read by this plugin
# only; it is not a cgr setting.
PARITY_DIFF_DIR_ENV = "CGR_PARITY_DIFF_DIR"
# Where `change_shadow.py`'s processes each write what they compared, for the
# test that runs it under xdist to add up. Read by that plugin only.
CHANGE_SHADOW_DIR_ENV = "CGR_CHANGE_SHADOW_DIR"
# The same for `structure_shadow.py`, and for `module_shadow.py`.
STRUCTURE_SHADOW_DIR_ENV = "CGR_STRUCTURE_SHADOW_DIR"
MODULE_SHADOW_DIR_ENV = "CGR_MODULE_SHADOW_DIR"

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
SCRATCH_HANDOFF = "handoff"
SCRATCH_HOST = "unused"
# Each artefact's file in a hand-off directory: the engine writes `files.txt`
# and `index.txt`, and this plugin `graph.txt`, from the export, as the
# reference graphs write theirs.
HANDOFF_FILE = "{artefact}.txt"
DIFF_FILE = "{artefact}.diff"
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
# The functions in `utils/path_utils.py` and `os` Python's walk decides with.
WALK_PREDICATES = (
    "is_symlink_entry",
    "is_outside_junction",
    "is_special_file_entry",
    "is_utf8_name",
    "should_keep_dir",
    "should_skip_rel_file",
)
WALK_SYSTEM_CALLS = ("walk", "scandir", "lstat", "stat", "readlink")
FRAME_REPORT_HEADING = (
    "The frame's file plan differs from Python's on {repo}: {python} lines only "
    "in Python's, {rust} only in the frame's"
)
FRAME_CHANGES_HEADING = (
    "The frame's change detection differs from Python's on {repo}: {python} lines "
    "only in Python's, {rust} only in the frame's"
)
FRAME_STRUCTURE_HEADING = (
    "The frame's structure differs from Python's on {repo}: {python} lines only "
    "in Python's, {rust} only in the frame's"
)
# What Python's change detection decides with and reads the state files by,
# in `graph_updater.py` and on `GraphUpdater`, and the calls beneath them.
CHANGE_FUNCTIONS = (
    "_hash_file",
    "_hash_file_with_bytes",
    "_last_changed",
    "_vanished",
    "_is_gone_error",
    "_load_hash_cache",
    "_load_dir_mtimes",
    "_trustworthy_cache_mtime",
    "_cached_file_unchanged",
    "_cached_files_unchanged",
    "_scan_dir_children",
    "_direct_children",
    "_entry_flag",
)
CHANGE_METHODS = (
    "_files_in_sync",
    "_cached_dirs_unchanged",
    "_diff_dir_against_cache",
    "_first_indexable_addition",
    "_should_keep_dir",
    "_load_old_hashes",
    "_hash_baseline",
    "_collect_eligible_files",
    "_scan_eligible_file",
    "_scan_mtime_fast_path",
    "_single_target_vanished",
    "_single_target_gone_before_scan",
    "_keys_deleted_before_parse",
    "_deleted_keys_for_run",
)
CHANGE_PATH_METHODS = (
    "stat",
    "lstat",
    "is_file",
    "exists",
    "is_symlink",
    "open",
    "read_bytes",
    "read_text",
)
# What Python's structure pass decides with and resolves paths by, in
# `parsers/structure_processor.py` and on `StructureProcessor`, and the calls
# beneath them that its walk does not share with the file walk's.
STRUCTURE_FUNCTIONS = (
    "should_skip_path",
    "is_symlink_entry",
    "is_outside_junction",
    "is_utf8_name",
    "cached_relative_path",
    "cached_resolve_posix",
    "cached_file_identity_posix",
)
STRUCTURE_METHODS = (
    "_directories_to_derive",
    "_derive_one_directory",
    "_get_parent_identifier",
    "_package_row",
    "_folder_row",
    "file_row",
)
STRUCTURE_PATH_METHODS = ("exists", "resolve", "relative_to", "is_file")
FRAME_MODULES_HEADING = (
    "The frame's module names differ from Python's on {repo}: {python} lines "
    "only in Python's, {rust} only in the frame's"
)
# What Python's module naming decides with, each where it is looked up: in
# `parsers/definition_processor.py`, `language_spec.py` and
# `utils/path_utils.py`; and what a run parses a file into the module map
# through, on `DefinitionProcessor` and `GraphUpdater` and in
# `graph_updater.py`.
MODULE_DEFINITION_FUNCTIONS = (
    "has_other_language_sibling",
    "own_extension_module_qn",
    "base_module_qn",
    "declaration_extension",
    "has_implementation_sibling",
    "is_notebook_path",
    "parse_with_preproc_recovery",
    "grammar_bytes",
)
MODULE_LANGUAGE_FUNCTIONS = (
    "module_extension",
    "module_stem",
    "should_skip_path",
    "get_language_for_extension",
    "_directory_names",
)
MODULE_PATH_FUNCTIONS = (
    "module_stem",
    "should_skip_path",
    "is_symlink_entry",
    "python_stub_has_implementation",
)
MODULE_DEFINITION_METHODS = (
    "process_file",
    "_disambiguate_module_qn",
    "_parse_file_root",
)
MODULE_UPDATER_FUNCTIONS = (
    "get_language_spec",
    "python_stub_has_implementation",
    "is_notebook_path",
    "read_notebook",
)
MODULE_UPDATER_METHODS = ("_process_single_file", "_process_notebook", "_route")
REPORT_SECTION = "engine comparison"
TEARDOWN = "teardown"
FUNCTION_SCOPE = "function"
FIXTURE_ID_SEPARATOR = "::"
OUTSIDE_A_TEST = "outside-a-test"
ENGINE_SESSION_PLUGIN = "cgr-engine-session"
LANGUAGE_SEPARATOR = ", "
DIFF_ONLY_PYTHON = "-"


class EngineMode(StrEnum):
    PYTHON = "python"
    RUST = "rust"
    BOTH = "both"


# What each scratch run hands on, in the order of the pipeline: the file
# plan, the project index and the graph (docs/reports/PHASE1_SEAMS.md,
# section 3).
class Artefact(StrEnum):
    FILES = "files"
    INDEX = "index"
    GRAPH = "graph"

    @property
    def file(self) -> str:
        return HANDOFF_FILE.format(artefact=self)


# The artefacts the engine writes, as against the graph, which is exported.
ENGINE_HANDOFFS = (Artefact.FILES, Artefact.INDEX)


class Side(NamedTuple):
    """One engine's fresh index of a repository: the lines of each artefact
    it handed on (none for a hand-off it did not write), the pass failures
    the run swallowed, and the traceback of what it raised, if it did not
    finish."""

    artefacts: dict[Artefact, list[str]]
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
        from codebase_rag import engine_frame

        self._patch.setattr(engine_frame, "CHECK", FramePlanCheck(self._file_report))
        # On in production; `both` keeps Python's answers for the run.
        self._patch.setattr(engine_frame, "ENABLED", mode is EngineMode.RUST)
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
        label = self._label()
        report = compare_engines(updater, run, self._compared[label], label)
        if report is not None:
            self._file_report(report)

    def _label(self) -> str:
        """Where a run returning now was made: the innermost shared fixture
        being set up, else the test running."""
        if self._fixtures:
            return self._fixtures[-1].label
        if self._item is not None:
            return self._item.nodeid
        return OUTSIDE_A_TEST

    def _file_report(self, report: str) -> None:
        """`report` against what made the run: the shared fixture being set
        up, else the test running, each failing at teardown."""
        if self._fixtures:
            self._fixture_reports[self._fixtures[-1]].append(report)
        elif self._item is not None:
            self._test_reports[self._item.nodeid].append(report)
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


def frame_holds_python_constants() -> bool:
    """Whether the frame's copies of the walk's built-in names and endings
    are Python's: a test that patches one, to stand in for an older cgr,
    walks by rules the frame does not hold."""
    from codebase_rag import constants as cs
    from codebase_rag import engine_frame

    made = engine_frame.engine()
    rules = engine_frame.new_ignore_rules()
    if made is None or rules is None:
        return False
    engine_type, rules_type = type(made), type(rules)
    unconditional = frozenset(getattr(rules_type, "UNCONDITIONAL_IGNORE_SUFFIXES"))
    rescuable = frozenset(getattr(rules_type, "RESCUABLE_IGNORE_SUFFIXES"))
    return (
        frozenset(cs.IGNORE_PATTERNS)
        == frozenset(getattr(rules_type, "BUILTIN_IGNORED_DIRS"))
        and frozenset(cs.VCS_METADATA_DIRS)
        == frozenset(getattr(rules_type, "VCS_METADATA_DIRS"))
        and frozenset(cs.UNCONDITIONAL_IGNORE_FILENAME_ENDINGS) == unconditional
        and frozenset(cs.IGNORE_FILENAME_ENDINGS) == unconditional | rescuable
        and frozenset(cs.CGR_STATE_FILENAMES)
        == frozenset(getattr(engine_type, "STATE_FILENAMES"))
    )


def _walk_functions() -> tuple[Callable[..., object], ...]:
    """What Python's walk decides with and its files are read by: the walk's
    predicates, the system calls `os.walk` and they make, and the reads the
    sync and the comparison hash with."""
    from codebase_rag.utils import path_utils

    return (
        *(getattr(path_utils, name) for name in WALK_PREDICATES),
        *(getattr(os, name) for name in WALK_SYSTEM_CALLS),
        os.path.islink,
        os.path.isjunction,
        builtins.open,
        Path.read_bytes,
    )


def _change_functions() -> tuple[object, ...]:
    """What Python's change detection decides with and reads the state files
    by, the system calls beneath them, its clock and the platform it takes
    itself to run on."""
    from codebase_rag import graph_updater
    from codebase_rag.graph_updater import GraphUpdater

    return (
        *(getattr(graph_updater, name) for name in CHANGE_FUNCTIONS),
        *(GraphUpdater.__dict__[name] for name in CHANGE_METHODS),
        *(getattr(Path, name) for name in CHANGE_PATH_METHODS),
        os.stat,
        os.lstat,
        os.scandir,
        graph_updater.time.time,
        graph_updater.sys.platform,
    )


def _structure_functions() -> tuple[object, ...]:
    """What Python's structure pass decides with and resolves paths by, and
    the system calls beneath them."""
    from codebase_rag.parsers import structure_processor
    from codebase_rag.parsers.structure_processor import StructureProcessor

    return (
        *(getattr(structure_processor, name) for name in STRUCTURE_FUNCTIONS),
        *(StructureProcessor.__dict__[name] for name in STRUCTURE_METHODS),
        *(getattr(Path, name) for name in STRUCTURE_PATH_METHODS),
        os.path.realpath,
    )


def _module_functions() -> tuple[object, ...]:
    """What Python's module naming decides with, what a run parses a file
    into the module map through, and the calls beneath them that the walk
    does not share."""
    from codebase_rag import graph_updater, language_spec
    from codebase_rag.graph_updater import GraphUpdater
    from codebase_rag.parsers import definition_processor
    from codebase_rag.parsers.definition_processor import DefinitionProcessor
    from codebase_rag.utils import path_utils

    return (
        *(getattr(definition_processor, name) for name in MODULE_DEFINITION_FUNCTIONS),
        *(getattr(language_spec, name) for name in MODULE_LANGUAGE_FUNCTIONS),
        *(getattr(path_utils, name) for name in MODULE_PATH_FUNCTIONS),
        *(DefinitionProcessor.__dict__[name] for name in MODULE_DEFINITION_METHODS),
        *(getattr(graph_updater, name) for name in MODULE_UPDATER_FUNCTIONS),
        *(GraphUpdater.__dict__[name] for name in MODULE_UPDATER_METHODS),
        os.listdir,
        Path.is_file,
    )


def frame_report(
    repo_path: Path, difference: list[str], heading: str = FRAME_REPORT_HEADING
) -> str:
    """The message for an answer the frame and Python differ on: the
    repository, the count on each side, and the first differing lines, `-`
    for Python's and `+` for the frame's."""
    python = sum(1 for line in difference if line.startswith(DIFF_ONLY_PYTHON))
    lines = [
        heading.format(repo=repo_path, python=python, rust=len(difference) - python)
    ]
    lines.extend(REPORT_LINE.format(line=line) for line in difference[:REPORT_LINES])
    if len(difference) > REPORT_LINES:
        lines.append(REPORT_MORE.format(count=len(difference) - REPORT_LINES))
    return "\n".join(lines)


class FramePlanCheck:
    """`engine_frame.CHECK` in the `rust` and `both` modes: every index is
    compared, and a difference is filed against the test or the shared
    fixture that made the run.

    Not compared: a run made while a test has patched the built-in names or
    endings, or one of the functions or system calls Python's walk decides
    with or reads files by, to stand in for an older cgr or a failing system
    (a file that cannot be read). The frame walks and reads by its own
    copies and the real file system. A run's change detection is left out
    the same way while a test patches what it decides with, reads the
    state files by, its clock or the platform it takes itself to run on,
    its structure while a test patches what the structure pass decides
    with or resolves paths by, and its module names while a test patches
    what Python names modules by or parses a file into the map through.
    """

    def __init__(self, file_report: Callable[[str], None]) -> None:
        self._file_report = file_report
        self._walk = _walk_functions()
        self._changes = _change_functions()
        self._structure = _structure_functions()
        self._modules = _module_functions()

    def compares(self) -> bool:
        current = _walk_functions()
        return frame_holds_python_constants() and all(
            now is found for now, found in zip(current, self._walk, strict=True)
        )

    def differs(self, repo_path: Path, difference: Sequence[str]) -> None:
        self._file_report(frame_report(repo_path, list(difference)))

    def compares_changes(self) -> bool:
        current = _change_functions()
        return self.compares() and all(
            now is found or now == found
            for now, found in zip(current, self._changes, strict=True)
        )

    def changes_differ(self, repo_path: Path, difference: Sequence[str]) -> None:
        self._file_report(
            frame_report(repo_path, list(difference), FRAME_CHANGES_HEADING)
        )

    def compares_structure(self) -> bool:
        current = _structure_functions()
        return self.compares() and all(
            now is found for now, found in zip(current, self._structure, strict=True)
        )

    def structure_differs(self, repo_path: Path, difference: Sequence[str]) -> None:
        self._file_report(
            frame_report(repo_path, list(difference), FRAME_STRUCTURE_HEADING)
        )

    def compares_modules(self) -> bool:
        current = _module_functions()
        return self.compares() and all(
            now is found for now, found in zip(current, self._modules, strict=True)
        )

    def modules_differ(self, repo_path: Path, difference: Sequence[str]) -> None:
        self._file_report(
            frame_report(repo_path, list(difference), FRAME_MODULES_HEADING)
        )


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
    differences = _differences(python, rust)
    report = _report(updater, languages, python, rust, differences)
    if report is not None and (root := os.environ.get(PARITY_DIFF_DIR_ENV)):
        _write_difference(Path(root), label, len(compared), (python, rust), differences)
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
    test's graph nor reads its sync state. The run writes its hand-off files
    to `scratch / SCRATCH_HANDOFF`.
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
            ingestor,
            state_dir=scratch / SCRATCH_STATE,
            index_engine=selection,
            handoff_dir=scratch / SCRATCH_HANDOFF,
        )
        run(replica, True)
        export: ExportRecord = json.loads(
            json.dumps(ingestor.export_graph_to_dict(), ensure_ascii=False)
        )
        return export


def _written(path: Path) -> list[str]:
    """A hand-off file's lines, or none when the run did not write it, which
    then differs from any file written."""
    from codebase_rag.canonical_graph import read_handoff

    return read_handoff(path) if path.is_file() else []


def _side(
    updater: GraphUpdater, engine: cs.IndexEngine, scratch: Path, run: Run
) -> Side:
    from codebase_rag.canonical_graph import canonical

    with swallowed_failures(engine) as failures:
        try:
            export = fresh_index(updater, engine, scratch, run)
            artefacts = {
                artefact: _written(scratch / SCRATCH_HANDOFF / artefact.file)
                for artefact in ENGINE_HANDOFFS
            }
        except Exception as error:
            # Reported with the comparison rather than raised from the
            # test's own `run`, which returned.
            raised = "".join(traceback.format_exception(error)).splitlines()
            return Side({}, failures, raised)
    artefacts[Artefact.GRAPH] = canonical(export, updater.repo_path)
    return Side(artefacts, failures, [])


def _differences(python: Side, rust: Side) -> dict[Artefact, list[str]]:
    """Each artefact's difference, in pipeline order: the file plan in order,
    the project index and the graph as multisets. Nothing when a side did
    not finish, since its files would describe part of a run."""
    from codebase_rag import constants as cs
    from codebase_rag.canonical_graph import difference, handoff_difference

    if python.raised or rust.raised:
        return {}
    differences = {
        artefact: handoff_difference(
            cs.HandoffFile(artefact.file),
            python.artefacts[artefact],
            rust.artefacts[artefact],
        )
        for artefact in ENGINE_HANDOFFS
    }
    differences[Artefact.GRAPH] = difference(
        python.artefacts[Artefact.GRAPH], rust.artefacts[Artefact.GRAPH]
    )
    return differences


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
    differences: dict[Artefact, list[str]],
) -> str | None:
    """The comparison's message: the repository and the languages on Rust,
    then each artefact in pipeline order with the first that differs marked,
    then any failure a scratch run swallowed or raised."""
    first = next((artefact for artefact, diff in differences.items() if diff), None)
    if differences and first is None and not python.failures and not rust.failures:
        return None
    lines = [
        REPORT_HEADING.format(
            repo=updater.repo_path,
            languages=LANGUAGE_SEPARATOR.join(sorted(languages)),
        )
    ]
    if differences:
        for artefact, diff in differences.items():
            lines.extend(_artefact(artefact, diff, first=artefact == first))
    else:
        lines.extend(
            REPORT_NOT_COMPARED.format(artefact=artefact) for artefact in Artefact
        )
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
    root: Path,
    label: str,
    number: int,
    sides: tuple[Side, Side],
    differences: dict[Artefact, list[str]],
) -> None:
    """Each side's hand-off files under its engine's name, as its run left
    them (the graph as the reference graphs write theirs), and each
    artefact's whole difference beside them."""
    from codebase_rag import constants as cs
    from codebase_rag.canonical_graph import write_graph

    target = root / UNSAFE_PATH_CHARS.sub(PATH_SAFE, label) / str(number)
    for engine, side in zip(_engines(), sides, strict=True):
        handoffs = target / engine
        handoffs.mkdir(parents=True, exist_ok=True)
        for artefact, lines in side.artefacts.items():
            if artefact == Artefact.GRAPH:
                write_graph(handoffs / artefact.file, lines)
            else:
                # Joined as `read_handoff` split them: the bytes as written.
                text = cs.LINE_FEED.join(lines)
                (handoffs / artefact.file).write_bytes(text.encode(cs.ENCODING_UTF8))
    for artefact, diff in differences.items():
        write_graph(target / DIFF_FILE.format(artefact=artefact), diff)
