#!/usr/bin/env python3
"""Record, or check, the hand-off files of the pinned repositories.

docs/reports/PHASE1_SEAMS.md, section 3. Each repository in
fixtures/reference_graphs/repos.json is fetched at its pinned commit, as
scripts/reference_graphs.py fetches it (the same cache), and indexed afresh
`--runs` times, every run in a Python process of its own: the hash seed is
fixed when an interpreter starts, so runs in one process could not vary it.
As the reference graphs' runs do, the first indexes the checkout under the
interpreter's own seed (never one left in the shell) and each later one a
copy at another path under `PYTHONHASHSEED` 1, 2, and so on, with the same
environment: none of the caller's cgr settings, the frontends that need no
compiler, and the `--index-engine` value, when one is given. Each run calls
`GraphUpdater`
directly with a hand-off directory; the hand-offs are not reachable through
`cgr`, on purpose. Every run must write the first run's `files.txt` and
`index.txt` byte for byte.

Each run also checks the Rust engine's frame (docs/reports/PHASE2_FRAME.md,
section 4): its file plan against Python's walk, as the index takes it, its
change detection (the hash cache and folder times the run would publish)
against Python's, its structure (the Package and Folder rows, the map of
each folder's package and the File rows) against Python's, its module-name
plan against the `module` lines of `index.txt`, and its reading of the
ignore files against `load_ignore_patterns`. Any difference, written to
`frame.txt` beside the hand-offs, fails the check.

`compare --engines A,B` (docs/reports/PHASE3_GO.md, section 6) indexes each
pinned repository whose language the two `CGR_INDEX_ENGINE` selections put
on different engines (as they ask, `auto` read as this release's default)
once under each, with the Go type checker on, and compares the two runs'
file plans in order and their project indexes and graphs as multisets, as
the `both` cells do; it fails on any difference. A selection of several
tokens separates them with `;`. The type checker's answers change with the
Go release, so nothing of that path is recorded: both sides run live, and a
Go repository is compared only with `go` on PATH.

`--preview LANGUAGES` runs those languages on the Rust engine wherever the
selection asks for it, before the build lists them (`index_engine.PREVIEW`),
in every run's process.

    uv run python -m scripts.handoff check --runs 2      # exit 1 on a difference
    uv run python -m scripts.handoff check uuid --index-engine go=rust
    uv run python -m scripts.handoff record --out DIR    # under DIR/<repository>
    uv run python -m scripts.handoff compare --engines python,go=rust

The files are not checked in: both engines exist side by side, so they are
compared live. `record` writes the first run's, once every run agreed, for
reading, or for comparing two engine selections with `diff -r`.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

from codebase_rag import constants as cs
from codebase_rag.canonical_graph import (
    difference,
    difference_in_order,
    handoff_difference,
    read_graph,
    read_handoff,
)
from codebase_rag.types_defs import ReferenceRepo
from scripts.reference_graphs import (
    DEFAULT_CACHE,
    PROJECT_ROOT,
    _index_env,
    checkouts,
    fetch,
    index_engine_option,
    load_manifest,
    say,
)

if TYPE_CHECKING:
    from codebase_rag.config import CgrignorePatterns
    from codebase_rag.services.graph_service import MemgraphIngestor

# The module each run starts, and the command it runs there.
MODULE = "scripts.handoff"
INDEX_COMMAND = "index"
RECORD_COMMAND = "record"
CHECK_COMMAND = "check"
COMPARE_COMMAND = "compare"
PREVIEW_FLAG = "--preview"
GRAPH_FLAG = "--graph"
DEFAULT_RUNS = 2
SCRATCH_PREFIX = "cgr-handoff-"
SCRATCH_GRAPH = "graph.sqlite"
SCRATCH_STATE = "state"
SCRATCH_HOST = "unused"
RUN_DIR = "run{number}"
PYTHONPATH_ENV = "PYTHONPATH"
RUN_LOG_LEVEL = "WARNING"
STDERR_TAIL = 4000
# Where a run writes how the frame differed from Python, empty when it did
# not: not a hand-off, which both engines write.
FRAME_FILE = "frame.txt"
FRAME_READING = "ignore {kind}\t{pattern}"
FRAME_EXCLUDE = "exclude"
FRAME_UNIGNORE = "unignore"
FRAME_NOT_COMPARED = "! the frame was not compared: the extension lacks it"
FILE_LINE = f"{cs.HandoffKind.FILE}{cs.REFERENCE_FIELD_SEPARATOR}"
# The graph a run exports with `--graph`, as the reference graphs write
# theirs: what `compare` holds the two sides to after their hand-offs.
GRAPH_FILE = "graph.txt"
# `compare`'s two selections, and the Go program the type checker needs.
ENGINES_SEPARATOR = ","
ENGINES_COUNT = 2
GO_PROGRAM = "go"
COMPARE_HEADING = "{repo} ({language}): {first} against {second}"
COMPARE_SAME = "  {file}: same"
COMPARE_DIFFERS = (
    "  {file}: {first} lines only under {first_name}, {second} only under {second_name}"
)
COMPARE_FIRST = "  <- first difference"
ENGINES_USAGE = (
    "give two CGR_INDEX_ENGINE values, comma-separated, such as python,go=rust; "
    "a value of several tokens separates them with ';'"
)
NOTHING_TO_COMPARE = (
    "no repository chosen holds a language that {first} and {second} put on "
    "different engines"
)
NO_GO = (
    "{repo}: comparing a Go repository runs the Go type checker, and there is "
    "no `go` on PATH"
)

# One run's hand-off files, by name, as the lines `read_handoff` reads.
type Handoffs = dict[cs.HandoffFile, list[str]]


class IndexOptions(NamedTuple):
    """How a run indexes: on which `CGR_INDEX_ENGINE` selection (None for
    none), with which languages previewed on Rust, with the Go type checker
    or the reference graphs' tree-sitter alone, and whether it exports its
    graph beside its hand-offs."""

    index_engine: str | None = None
    preview: str | None = None
    go_types: bool = False
    graph: bool = False


def _child_env(
    home: Path, seed: int | None, index_engine: str | None, go_types: bool = False
) -> dict[str, str]:
    """The reference graphs' index environment, with this checkout first on
    the import path: the run starts `scripts.handoff` from a scratch
    directory, so that no `.env` of the caller's applies."""
    env = _index_env(home, seed, index_engine)
    if seed is None:
        # The interpreter's own seed, not one left in the shell: that could
        # be a later run's, and the two runs would hash alike.
        env.pop(cs.REFERENCE_SEED_ENV, None)
    if go_types:
        env[cs.SETTING_GO_FRONTEND] = cs.GoFrontend.GOTYPES
    paths = [str(PROJECT_ROOT), *filter(None, [env.get(PYTHONPATH_ENV)])]
    env[PYTHONPATH_ENV] = os.pathsep.join(paths)
    return env


def index_run(
    repo: ReferenceRepo,
    source: Path,
    seed: int | None,
    out: Path,
    options: IndexOptions,
) -> None:
    """Index `source` afresh in a Python process of its own, under `seed`,
    writing its hand-off files to `out`."""
    with tempfile.TemporaryDirectory(prefix=SCRATCH_PREFIX) as tmp:
        work = Path(tmp)
        home = work / "home"
        home.mkdir()
        command = [
            sys.executable,
            "-m",
            MODULE,
            INDEX_COMMAND,
            # Absolute: the run starts in the scratch directory.
            str(source.resolve()),
            str(out.resolve()),
            "--project-name",
            repo["name"],
        ]
        if options.preview is not None:
            command.extend((PREVIEW_FLAG, options.preview))
        if options.graph:
            command.append(GRAPH_FLAG)
        result = subprocess.run(
            command,
            check=False,
            cwd=work,
            env=_child_env(home, seed, options.index_engine, options.go_types),
            capture_output=True,
            text=True,
            encoding=cs.ENCODING_UTF8,
            errors="replace",
            timeout=cs.REFERENCE_INDEX_TIMEOUT_S,
        )
    if result.returncode != 0:
        raise SystemExit(
            f"{repo['name']}: `{shlex.join(command)}` exited "
            f"{result.returncode}\n{result.stderr[-STDERR_TAIL:]}"
        )


def index_in_process(
    repo_path: Path,
    out: Path,
    project_name: str,
    preview: str | None = None,
    graph: bool = False,
) -> None:
    """One run, in this process: `repo_path` indexed afresh as `cgr start`
    indexes it (its ignore files, the capture the settings give), into a
    scratch embedded graph, writing the hand-off files to `out`, with the
    languages `preview` names on Rust, and with `graph` its export too."""
    # Imported here, not when `record` or `check` load this module: the
    # settings merge the working directory's `.env` when they load.
    from loguru import logger

    from codebase_rag import engine_frame, index_engine
    from codebase_rag.config import load_ignore_patterns
    from codebase_rag.graph_dialects import DIALECT_SQLITE, get_dialect
    from codebase_rag.graph_updater import GraphUpdater
    from codebase_rag.parser_loader import load_parsers
    from codebase_rag.services.graph_service import MemgraphIngestor

    # Warnings and errors only, as `cgr -q` shows none at all: what a run
    # says reaches the caller only when it fails.
    logger.remove()
    logger.add(sys.stderr, level=RUN_LOG_LEVEL, backtrace=False)
    if preview is not None:
        index_engine.PREVIEW = index_engine.parse_preview(preview)
    ignore = load_ignore_patterns(repo_path)
    frame = _FrameCheck()
    engine_frame.CHECK = frame
    parsers, queries = load_parsers()
    with (
        tempfile.TemporaryDirectory(prefix=SCRATCH_PREFIX) as tmp,
        MemgraphIngestor(
            host=SCRATCH_HOST,
            port=0,
            dialect=get_dialect(DIALECT_SQLITE),
            sqlite_path=str(Path(tmp) / SCRATCH_GRAPH),
        ) as ingestor,
    ):
        ingestor.ensure_constraints()
        GraphUpdater(
            ingestor=ingestor,
            repo_path=repo_path,
            parsers=parsers,
            queries=queries,
            unignore_paths=ignore.unignore or None,
            exclude_paths=ignore.exclude or None,
            project_name=project_name,
            project_named=True,
            skip_embeddings=True,
            state_dir=Path(tmp) / SCRATCH_STATE,
            handoff_dir=out,
        ).run(force=True)
        if graph:
            _write_export(ingestor, repo_path, out / GRAPH_FILE)
    engine_frame.CHECK = None
    lines = [*frame.lines, *_reading_difference(repo_path, ignore)]
    if not frame.compared:
        lines.append(FRAME_NOT_COMPARED)
    (out / FRAME_FILE).write_bytes(cs.LINE_FEED.join(lines).encode(cs.ENCODING_UTF8))


def _write_export(ingestor: MemgraphIngestor, repo_path: Path, path: Path) -> None:
    """The run's graph, exported as `cgr start -o` writes it, as the
    reference graphs' sorted lines."""
    from codebase_rag.canonical_graph import canonical, write_graph

    export = json.loads(json.dumps(ingestor.export_graph_to_dict(), ensure_ascii=False))
    write_graph(path, canonical(export, repo_path))


class _FrameCheck:
    """`engine_frame.CHECK` for one run: it compares the run's plan, its
    change detection, its structure and its module names, and keeps the
    differences."""

    def __init__(self) -> None:
        self.compared = 0
        self.lines: list[str] = []

    def compares(self) -> bool:
        self.compared += 1
        return True

    def differs(self, repo_path: Path, difference: Sequence[str]) -> None:
        self.lines.extend(difference)

    def compares_changes(self) -> bool:
        self.compared += 1
        return True

    def changes_differ(self, repo_path: Path, difference: Sequence[str]) -> None:
        self.lines.extend(difference)

    def compares_structure(self) -> bool:
        self.compared += 1
        return True

    def structure_differs(self, repo_path: Path, difference: Sequence[str]) -> None:
        self.lines.extend(difference)

    def compares_modules(self) -> bool:
        self.compared += 1
        return True

    def modules_differ(self, repo_path: Path, difference: Sequence[str]) -> None:
        self.lines.extend(difference)


def _reading_difference(repo_path: Path, ignore: CgrignorePatterns) -> list[str]:
    """How the frame's reading of the ignore files differs from Python's,
    `-` for a pattern only Python's has, `+` for one only the frame's has."""
    from codebase_rag import engine_frame

    frame = engine_frame.frame_ignore_patterns(repo_path)
    if frame is None:
        return [FRAME_NOT_COMPARED]
    sides = (
        (cs.REFERENCE_ONLY_EXPECTED, FRAME_EXCLUDE, ignore.exclude - frame.exclude),
        (cs.REFERENCE_ONLY_EXPECTED, FRAME_UNIGNORE, ignore.unignore - frame.unignore),
        (cs.REFERENCE_ONLY_ACTUAL, FRAME_EXCLUDE, frame.exclude - ignore.exclude),
        (cs.REFERENCE_ONLY_ACTUAL, FRAME_UNIGNORE, frame.unignore - ignore.unignore),
    )
    return [
        side.format(line=FRAME_READING.format(kind=kind, pattern=pattern))
        for side, kind, patterns in sides
        for pattern in sorted(patterns)
    ]


def _read(repo: ReferenceRepo, out: Path) -> Handoffs:
    handoffs: Handoffs = {}
    for file in cs.HandoffFile:
        if not (out / file).is_file():
            raise SystemExit(f"{repo['name']}: a run wrote no {file}")
        handoffs[file] = read_handoff(out / file)
    return handoffs


def _frame_difference(repo: ReferenceRepo, out: Path) -> list[str]:
    """The lines a run wrote of how the frame differed from Python."""
    if not (out / FRAME_FILE).is_file():
        raise SystemExit(f"{repo['name']}: a run wrote no {FRAME_FILE}")
    return [line for line in read_handoff(out / FRAME_FILE) if line]


def _summary(handoffs: Handoffs) -> str:
    plan = handoffs[cs.HandoffFile.FILES]
    files = sum(1 for line in plan if line.startswith(FILE_LINE))
    # Past the header, and not the empty string after the last line end.
    index_lines = sum(1 for line in handoffs[cs.HandoffFile.INDEX][1:] if line)
    return f"{files} files, {index_lines} index lines"


def _report(lines: list[str], title: str, diff: Sequence[str]) -> None:
    removed = sum(1 for line in diff if line.startswith("-"))
    lines.append(
        f"{title}: {removed} lines only before, {len(diff) - removed} only after"
    )
    lines.extend(f"    {line}" for line in diff[: cs.REFERENCE_DIFF_LINES])
    if len(diff) > cs.REFERENCE_DIFF_LINES:
        lines.append(f"    ... and {len(diff) - cs.REFERENCE_DIFF_LINES} more")


def check_repo(
    repo: ReferenceRepo,
    cache: Path,
    runs: int,
    options: IndexOptions,
    work: Path,
) -> tuple[int, list[str], Handoffs]:
    """Whether every run of `repo` wrote the first run's files, the lines
    saying where not, and the first run's files."""
    checkout = fetch(repo, cache)
    outcomes: list[tuple[int | None, Handoffs]] = []
    frame: list[tuple[int | None, list[str]]] = []
    for number, (seed, source) in enumerate(checkouts(repo, checkout, runs)):
        out = work / repo["name"] / RUN_DIR.format(number=number)
        index_run(repo, source, seed, out, options)
        outcomes.append((seed, _read(repo, out)))
        frame.append((seed, _frame_difference(repo, out)))
    (_, first), *later = outcomes
    lines = [f"{repo['name']} ({repo['language']}): {_summary(first)}"]
    status = 0
    for seed, diff in frame:
        if diff:
            status = cs.REFERENCE_EXIT_DIFFERENT
            _report(
                lines,
                f"  the frame differs from Python in the run with "
                f"PYTHONHASHSEED={seed}",
                diff,
            )
    for seed, handoffs in later:
        for file in cs.HandoffFile:
            # In order for both files: each must be the first run's byte for
            # byte, and the index is written sorted.
            diff = difference_in_order(first[file], handoffs[file])
            if diff:
                status = cs.REFERENCE_EXIT_DIFFERENT
                _report(
                    lines,
                    f"  the run with PYTHONHASHSEED={seed} differs from the "
                    f"first in {file}",
                    diff,
                )
    return status, lines, first


def run(
    repos: Sequence[ReferenceRepo],
    cache: Path,
    runs: int,
    *,
    out: Path | None = None,
    index_engine: str | None = None,
    preview: str | None = None,
) -> int:
    """Check every run of each of `repos` against its first, and with `out`,
    record the first runs' files there."""
    options = IndexOptions(index_engine=index_engine, preview=preview)
    status = 0
    # A recording writes nothing until every repository has indexed and
    # every run agreed, as the reference graphs' does.
    staged: list[tuple[ReferenceRepo, Handoffs]] = []
    with tempfile.TemporaryDirectory(prefix=SCRATCH_PREFIX) as tmp:
        for repo in repos:
            found, lines, first = check_repo(repo, cache, runs, options, Path(tmp))
            for line in lines:
                say(line)
            status = status or found
            staged.append((repo, first))
    if out is None:
        return status
    if status:
        say("Runs disagreed, so no hand-off was written.")
        return status
    for repo, handoffs in staged:
        directory = out / repo["name"]
        directory.mkdir(parents=True, exist_ok=True)
        for file, lines in handoffs.items():
            # Joined as `read_handoff` split them: the bytes the run wrote.
            text = cs.LINE_FEED.join(lines)
            (directory / file).write_bytes(text.encode(cs.ENCODING_UTF8))
    return status


def parted_languages(first: str, second: str) -> frozenset[cs.SupportedLanguage]:
    """The languages two `CGR_INDEX_ENGINE` selections put on different
    engines, as they ask: whether this build can run either there decides
    what a run does, not what the comparison is for."""
    from codebase_rag.index_engine import asked_index_engines, parse_index_engine

    one = asked_index_engines(parse_index_engine(first))
    other = asked_index_engines(parse_index_engine(second))
    return frozenset(
        language for language, engine in one.items() if other[language] != engine
    )


def compared_repos(
    repos: Sequence[ReferenceRepo], first: str, second: str
) -> list[ReferenceRepo]:
    """Those of `repos` whose language the two selections part on."""
    parted = parted_languages(first, second)
    return [repo for repo in repos if repo["language"] in parted]


class Side(NamedTuple):
    """One side of a comparison: the hand-offs its run wrote and its graph."""

    handoffs: Handoffs
    graph: list[str]


def _side(repo: ReferenceRepo, out: Path) -> Side:
    if not (out / GRAPH_FILE).is_file():
        raise SystemExit(f"{repo['name']}: a run wrote no {GRAPH_FILE}")
    return Side(_read(repo, out), read_graph(out / GRAPH_FILE))


def _differences(first: Side, second: Side) -> list[tuple[str, list[str]]]:
    """Each file's difference, in the order of the pipeline: the file plan in
    order, the project index and the graph as multisets, as `both` compares
    them, so the first that differs names the stage at which the two part."""
    handoffs = [
        (
            str(file),
            handoff_difference(file, first.handoffs[file], second.handoffs[file]),
        )
        for file in cs.HandoffFile
    ]
    return [*handoffs, (GRAPH_FILE, difference(first.graph, second.graph))]


def compare_repo(
    repo: ReferenceRepo,
    cache: Path,
    engines: tuple[str, str],
    preview: str | None,
    work: Path,
) -> tuple[int, list[str]]:
    """Whether `repo` indexed under each of `engines`, both from its
    checkout and with the type checker on, gives the same file plan, project
    index and graph, and the lines saying where not, the first difference
    marked."""
    if repo["language"] == cs.SupportedLanguage.GO and shutil.which(GO_PROGRAM) is None:
        raise SystemExit(NO_GO.format(repo=repo["name"]))
    checkout = fetch(repo, cache)
    sides: list[Side] = []
    for number, engine in enumerate(engines):
        out = work / repo["name"] / RUN_DIR.format(number=number)
        options = IndexOptions(engine, preview, go_types=True, graph=True)
        index_run(repo, checkout, None, out, options)
        sides.append(_side(repo, out))
    first_name, second_name = engines
    lines = [
        COMPARE_HEADING.format(
            repo=repo["name"],
            language=repo["language"],
            first=first_name,
            second=second_name,
        )
    ]
    found = False
    for file, diff in _differences(*sides):
        if not diff:
            lines.append(COMPARE_SAME.format(file=file))
            continue
        only_first = sum(1 for line in diff if line.startswith("-"))
        heading = COMPARE_DIFFERS.format(
            file=file,
            first=only_first,
            first_name=first_name,
            second=len(diff) - only_first,
            second_name=second_name,
        )
        lines.append(heading if found else f"{heading}{COMPARE_FIRST}")
        found = True
        lines.extend(f"    {line}" for line in diff[: cs.REFERENCE_DIFF_LINES])
        if len(diff) > cs.REFERENCE_DIFF_LINES:
            lines.append(f"    ... and {len(diff) - cs.REFERENCE_DIFF_LINES} more")
    return (cs.REFERENCE_EXIT_DIFFERENT if found else 0), lines


def compare(
    repos: Sequence[ReferenceRepo],
    cache: Path,
    engines: tuple[str, str],
    preview: str | None = None,
) -> int:
    """Compare each of `repos` under the two selections, exit 1 when any
    differs."""
    status = 0
    with tempfile.TemporaryDirectory(prefix=SCRATCH_PREFIX) as tmp:
        for repo in repos:
            found, lines = compare_repo(repo, cache, engines, preview, Path(tmp))
            for line in lines:
                say(line)
            status = status or found
    return status


def engines_option(text: str) -> tuple[str, str]:
    """`--engines`: two selections, each refused the way cgr refuses the
    setting at start-up."""
    from codebase_rag.index_engine import index_engine_error

    engines = text.split(ENGINES_SEPARATOR)
    if len(engines) != ENGINES_COUNT or not all(engines):
        raise argparse.ArgumentTypeError(ENGINES_USAGE)
    for engine in engines:
        if (error := index_engine_error(engine)) is not None:
            raise argparse.ArgumentTypeError(error)
    first, second = engines
    return first, second


def preview_option(text: str) -> str:
    """`--preview`, refused as `parse_preview` refuses it, before any
    repository is fetched."""
    from codebase_rag.index_engine import preview_error

    if (error := preview_error(text)) is not None:
        raise argparse.ArgumentTypeError(error)
    return text


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    repos_args = argparse.ArgumentParser(add_help=False)
    repos_args.add_argument("names", nargs="*", help="only these repositories")
    repos_args.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    repos_args.add_argument(
        PREVIEW_FLAG,
        type=preview_option,
        metavar="LANGUAGES",
        help="run these languages on Rust wherever the selection asks, before "
        "the build lists them",
    )
    shared = argparse.ArgumentParser(add_help=False, parents=[repos_args])
    shared.add_argument("--runs", type=int, default=DEFAULT_RUNS)
    shared.add_argument(
        "--index-engine",
        type=index_engine_option,
        metavar="SPEC",
        help="index on this CGR_INDEX_ENGINE selection (none of the shell's arrives)",
    )
    commands.add_parser(CHECK_COMMAND, parents=[shared])
    record = commands.add_parser(RECORD_COMMAND, parents=[shared])
    record.add_argument(
        "--out", type=Path, required=True, help="write DIR/<repository>/*.txt"
    )
    comparison = commands.add_parser(
        COMPARE_COMMAND,
        parents=[repos_args],
        help="index each repository under two selections and compare",
    )
    comparison.add_argument(
        "--engines", type=engines_option, required=True, metavar="A,B"
    )
    one = commands.add_parser(
        INDEX_COMMAND, help="one run, in this process: what record and check start"
    )
    one.add_argument("repo_path", type=Path)
    one.add_argument("out", type=Path)
    one.add_argument("--project-name", required=True)
    one.add_argument(PREVIEW_FLAG, type=preview_option, metavar="LANGUAGES")
    one.add_argument(GRAPH_FLAG, action="store_true", help="export the graph too")
    args = parser.parse_args(argv)
    if args.command == INDEX_COMMAND:
        index_in_process(
            args.repo_path, args.out, args.project_name, args.preview, args.graph
        )
        return 0
    if args.command == CHECK_COMMAND and args.runs < 2:
        parser.error("a check compares runs: give --runs 2 or more")
    repos = load_manifest()
    unknown = set(args.names) - {repo["name"] for repo in repos}
    if unknown:
        parser.error(f"unknown repositories: {', '.join(sorted(unknown))}")
    chosen = [repo for repo in repos if not args.names or repo["name"] in args.names]
    if args.command == COMPARE_COMMAND:
        first, second = args.engines
        if not (compared := compared_repos(chosen, first, second)):
            parser.error(NOTHING_TO_COMPARE.format(first=first, second=second))
        return compare(compared, args.cache, args.engines, args.preview)
    return run(
        chosen,
        args.cache,
        max(args.runs, 1),
        out=args.out if args.command == RECORD_COMMAND else None,
        index_engine=args.index_engine,
        preview=args.preview,
    )


if __name__ == "__main__":
    sys.exit(main())
