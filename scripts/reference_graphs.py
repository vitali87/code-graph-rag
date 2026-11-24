#!/usr/bin/env python3
"""Record, or check, the reference graphs of pinned repositories (issue #3629).

Each repository in fixtures/reference_graphs/repos.json is fetched at its
pinned commit, indexed by the `cgr` on PATH (or the command `--cgr` names)
into an embedded graph of its own, exported, and written out as sorted text:
one node or relationship per line, keyed by what the graph keys it on, with
the checkout's directory written as `<root>` and the time of the run left
out. The graphs come from `cgr`, not from importing the indexer, so the same
files hold the Rust rewrite to the graphs the Python indexer builds today.
That line format is codebase_rag/canonical_graph.py's, shared with the
rewrite's parity checks.

They are recorded and checked with every tier's extra installed, as the CI
job installs them (`uv sync --extra treesitter-full --extra ast-grep`):

    uv run --extra treesitter-full --extra ast-grep python scripts/reference_graphs.py record
    uv run python scripts/reference_graphs.py check            # exit 1 on a difference
    uv run python scripts/reference_graphs.py check --runs 3   # and on a run-to-run one
    uv run python scripts/reference_graphs.py record itsdangerous
    uv run python scripts/reference_graphs.py environment      # what this install parses

With `--runs N`, runs after the first index a copy of the checkout at another
path under another `PYTHONHASHSEED`; every run must give the same graph.

No cgr setting of the caller's reaches the index, so a `CGR_INDEX_ENGINE` in
the shell does not either. `--index-engine SPEC` is how an index runs on
another engine selection: it takes a `CGR_INDEX_ENGINE` value and every run
indexes with it, which the graphs must not show:

    uv run python scripts/reference_graphs.py check --index-engine go=rust

A full `record` also writes `environment.txt`: what the install could parse
(the languages each tier loads, the frontend each setting resolves to), read
in a process of its own by this Python, the one `cgr` comes from under
`uv run`. `check`, and a `record` of some repositories only, first compare
it with this machine's and stop on a difference, naming what is missing: a
language the machine cannot parse would otherwise read as its files' nodes
all gone. `record` refuses a `--cgr` that does not run under this Python, so
the file and the graphs describe one install; `check` with one (the Rust
engine) compares the graphs only, and says so.
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
from collections.abc import Generator, Iterable, Iterator, Sequence
from contextlib import contextmanager
from functools import cache
from pathlib import Path
from typing import NamedTuple

from codebase_rag import constants as cs
from codebase_rag.canonical_graph import (
    canonical,
    difference,
    read_graph,
    write_graph,
)
from codebase_rag.types_defs import ExportRecord, ReferenceRepo

# The environment as the caller set it, read before anything imports cgr's
# settings: that import merges the working directory's `.env` into
# `os.environ`, and a `GIT_DIR` or a cgr setting from it must not reach the
# Git calls or the index.
_CALLER_ENV = dict(os.environ)

PROJECT_ROOT = Path(__file__).parent.parent
REFERENCE_DIR = PROJECT_ROOT / cs.REFERENCE_DIR
DEFAULT_CACHE = Path.home() / ".cache" / cs.REFERENCE_CACHE_DIRNAME


class Indexed(NamedTuple):
    """A repository indexed into a graph of its own, while that graph lives."""

    export: ExportRecord
    # The scratch directory the index ran from; its `home` holds the graph.
    work: Path
    # The environment it ran with, so a command can read the same graph.
    env: dict[str, str]


def say(text: str) -> None:
    sys.stdout.write(f"{text}\n")


def load_manifest(directory: Path = REFERENCE_DIR) -> list[ReferenceRepo]:
    entries = json.loads(
        (directory / cs.REFERENCE_MANIFEST).read_text(encoding=cs.ENCODING_UTF8)
    )
    return [
        ReferenceRepo(
            name=entry["name"],
            language=entry["language"],
            repository=entry["repository"],
            commit=entry["commit"],
        )
        for entry in entries
    ]


def graph_path(repo: ReferenceRepo, directory: Path = REFERENCE_DIR) -> Path:
    return directory / f"{repo['name']}{cs.REFERENCE_GRAPH_SUFFIX}"


def environment_path(directory: Path = REFERENCE_DIR) -> Path:
    return directory / cs.REFERENCE_ENVIRONMENT_FILE


def _git_env() -> dict[str, str]:
    # A `GIT_DIR` or `GIT_WORK_TREE` would point every call at another
    # repository; `GIT_CONFIG*` stay, as in `tools/language.py`.
    return {
        name: value
        for name, value in _CALLER_ENV.items()
        if name not in cs.GIT_LOCATION_ENV_VARS
    }


def _git(args: Sequence[str], cwd: Path) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=_git_env(),
        check=True,
        capture_output=True,
        text=True,
        timeout=cs.REFERENCE_FETCH_TIMEOUT_S,
    ).stdout.strip()


def fetch(repo: ReferenceRepo, cache: Path) -> Path:
    """The repository checked out at its pinned commit, fetched once per cache.

    The fetch happens in a staging directory of its own, which is renamed into
    place only once it holds the verified commit: two runs sharing a cache
    never see each other's half-done checkout, and an interrupted one leaves
    nothing a later run would take for complete.
    """
    # Named after the repository: the directory's name is part of the graph
    # (the root Package node's `name`).
    pinned = cache.resolve() / repo["commit"]
    checkout = pinned / repo["name"]
    if checkout.is_dir():
        return checkout
    pinned.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="staging-", dir=pinned) as tmp:
        staging = Path(tmp) / repo["name"]
        staging.mkdir()
        url = cs.REFERENCE_GIT_URL.format(repository=repo["repository"])
        _git(["init", "-q"], staging)
        _git(["fetch", "-q", "--depth", "1", url, repo["commit"]], staging)
        _git(
            ["-c", "advice.detachedHead=false", "checkout", "-q", "FETCH_HEAD"],
            staging,
        )
        head = _git(["rev-parse", "HEAD"], staging)
        if head != repo["commit"]:
            raise SystemExit(
                f"{repo['name']}: fetched {head}, expected {repo['commit']}"
            )
        try:
            staging.rename(checkout)
        except OSError:
            # Another run published the same commit first; keep that one.
            if not checkout.is_dir():
                raise
    return checkout


@cache
def _setting_env_names() -> frozenset[str]:
    """Every environment variable a cgr setting is read from, upper-cased."""
    # Imported here, after `_CALLER_ENV` was taken (see there), and with the
    # environment put back afterwards, so not even this process keeps what
    # the import merged in from a `.env`.
    from pydantic import AliasChoices

    before = dict(os.environ)
    try:
        from codebase_rag.config import AppConfig
    finally:
        os.environ.clear()
        os.environ.update(before)

    names: set[str] = set()
    for name, field in AppConfig.model_fields.items():
        alias = field.validation_alias
        if isinstance(alias, str):
            names.add(alias.upper())
        elif isinstance(alias, AliasChoices):
            names.update(
                choice.upper() for choice in alias.choices if isinstance(choice, str)
            )
        else:
            names.add(name.upper())
    return frozenset(names)


def _index_env(
    home: Path, seed: int | None, index_engine: str | None = None
) -> dict[str, str]:
    # None of the caller's cgr settings reach the index: a `CGR_CAPTURE` or a
    # frontend choice left in a shell would change the graph `record` writes.
    # Settings are read whatever the case of their name, so `go_frontend`
    # counts as `GO_FRONTEND`. An engine selection arrives only as asked for,
    # through `index_engine`.
    controlled = _setting_env_names() | {
        name.upper() for name in (*cs.REFERENCE_ENV, cs.REFERENCE_HOME_ENV)
    }
    env = {
        name: value
        for name, value in _CALLER_ENV.items()
        if name.upper() not in controlled
    }
    env.update(cs.REFERENCE_ENV)
    env[cs.REFERENCE_HOME_ENV] = str(home)
    if seed is not None:
        env[cs.REFERENCE_SEED_ENV] = str(seed)
    if index_engine is not None:
        env[cs.INDEX_ENGINE_ENV] = index_engine
    return env


@contextmanager
def indexed(
    cgr: Sequence[str],
    repo: ReferenceRepo,
    source: Path,
    seed: int | None,
    index_engine: str | None = None,
) -> Generator[Indexed]:
    """Index `source` into a graph of its own, which lives until the block
    ends, and hand back `cgr`'s JSON export of it."""
    with tempfile.TemporaryDirectory(prefix="cgr-reference-") as tmp:
        work = Path(tmp)
        home = work / "home"
        home.mkdir()
        output = work / "graph.json"
        env = _index_env(home, seed, index_engine)
        command = [
            *cgr,
            "-q",
            "start",
            "--repo-path",
            # Absolute: the index runs from the scratch directory.
            str(source.resolve()),
            "--update-graph",
            "--no-embeddings",
            "--no-start-stack",
            "--project-name",
            repo["name"],
            "-o",
            str(output),
        ]
        # Run from the scratch directory so no `.env` of the caller's applies.
        result = subprocess.run(
            command,
            check=False,
            cwd=work,
            env=env,
            capture_output=True,
            text=True,
            timeout=cs.REFERENCE_INDEX_TIMEOUT_S,
        )
        if result.returncode != 0 or not output.is_file():
            raise SystemExit(
                f"{repo['name']}: `{shlex.join(command)}` exited "
                f"{result.returncode}\n{result.stderr[-4000:]}"
            )
        yield Indexed(
            json.loads(output.read_text(encoding=cs.ENCODING_UTF8)), work, env
        )


def index(
    cgr: Sequence[str],
    repo: ReferenceRepo,
    source: Path,
    seed: int | None,
    index_engine: str | None = None,
) -> ExportRecord:
    """Index `source` into a graph of its own and return `cgr`'s JSON export."""
    with indexed(cgr, repo, source, seed, index_engine) as done:
        return done.export


def parse_capabilities() -> list[str]:
    """What this install parses, as sorted `<tier>` and `<name>` lines.

    Asks what indexing asks: the tree-sitter grammars that load, the ast-grep
    tier's languages when it runs (it needs ast-grep-py and readable
    configs), the document tier, and each frontend's mode as its setting
    resolves. Imports the indexer, so `probe` runs it in a process of its own
    with the index's environment.
    """
    from codebase_rag.parser_loader import load_parsers
    from codebase_rag.parsers import (
        cpp_frontend,
        csharp_frontend,
        go_frontend,
        java_frontend,
        py_frontend,
    )
    from codebase_rag.parsers.ast_grep_tier import (
        ast_grep_importable,
        structural_tier_languages,
    )
    from codebase_rag.parsers.document_tier import document_tier_available

    tiers: list[tuple[str, str]] = [
        (cs.LanguageTier.TREE_SITTER, str(language)) for language in load_parsers()[0]
    ]
    if ast_grep_importable():
        try:
            structural = list(structural_tier_languages())
        except Exception:  # noqa: BLE001
            # The tier disables itself on a config it cannot read.
            structural = []
        tiers.extend((cs.LanguageTier.AST_GREP, name) for name in structural)
    if document_tier_available():
        tiers.append((cs.LanguageTier.DOCUMENT, cs.DOCUMENT_TIER_LANGUAGE))
    frontends = (
        (cs.SETTING_CPP_FRONTEND, cpp_frontend.resolve_cpp_frontend),
        (cs.SETTING_CSHARP_FRONTEND, csharp_frontend.resolve_csharp_frontend),
        (cs.SETTING_GO_FRONTEND, go_frontend.resolve_go_frontend),
        (cs.SETTING_JAVA_FRONTEND, java_frontend.resolve_java_frontend),
        (cs.SETTING_PYTHON_FRONTEND, py_frontend.resolve_python_frontend),
    )
    tiers.extend(
        (
            cs.REFERENCE_FRONTEND_TIER,
            cs.REFERENCE_FRONTEND_ENTRY.format(setting=setting, mode=resolve().value),
        )
        for setting, resolve in frontends
    )
    return sorted(cs.REFERENCE_FIELD_SEPARATOR.join(entry) for entry in tiers)


def probe() -> list[str]:
    """`parse_capabilities` as an index would find it: under this Python,
    with the index's environment, from a scratch directory."""
    with tempfile.TemporaryDirectory(prefix="cgr-reference-") as tmp:
        work = Path(tmp)
        home = work / "home"
        home.mkdir()
        command = [
            sys.executable,
            str(Path(__file__).resolve()),
            cs.REFERENCE_ENVIRONMENT_COMMAND,
            cs.REFERENCE_IN_PROCESS_FLAG,
        ]
        result = subprocess.run(
            command,
            check=False,
            cwd=work,
            env=_index_env(home, None),
            capture_output=True,
            text=True,
            encoding=cs.ENCODING_UTF8,
            timeout=cs.REFERENCE_PROBE_TIMEOUT_S,
        )
    if result.returncode != 0:
        raise SystemExit(
            cs.REFERENCE_PROBE_FAILED.format(
                command=shlex.join(command),
                code=result.returncode,
                stderr=result.stderr[-4000:],
            )
        )
    return sorted(line.strip() for line in result.stdout.split("\n") if line.strip())


def _by_tier(entries: Iterable[str], machine: Sequence[str] | None) -> str:
    """`bash, ruby (ast-grep missing); Markdown (document)`. A tier reads as
    missing when `machine`, this machine's entries, has none of it."""
    names: dict[str, list[str]] = {}
    for entry in sorted(entries):
        tier, _, name = entry.partition(cs.REFERENCE_FIELD_SEPARATOR)
        names.setdefault(tier, []).append(name)
    present = (
        None
        if machine is None
        else {entry.partition(cs.REFERENCE_FIELD_SEPARATOR)[0] for entry in machine}
    )
    return cs.REFERENCE_GROUP_SEPARATOR.join(
        (
            cs.REFERENCE_TIER_MISSING
            if present is not None and tier not in present
            else cs.REFERENCE_TIER_ENTRIES
        ).format(names=cs.REFERENCE_LIST_SEPARATOR.join(listed), tier=tier)
        for tier, listed in names.items()
    )


def environment_refusal(
    recorded: Sequence[str] | None, current: Sequence[str], directory: Path
) -> str | None:
    """Why the graphs cannot be compared on this machine, or None when it
    parses exactly what they were recorded with."""
    if recorded is None:
        return cs.REFERENCE_NO_ENVIRONMENT.format(
            path=environment_path(directory), regenerate=cs.REFERENCE_REGENERATE
        )
    missing = set(recorded) - set(current)
    extra = set(current) - set(recorded)
    if not missing and not extra:
        return None
    differences: list[str] = []
    if missing:
        differences.append(
            cs.REFERENCE_CANNOT_PARSE.format(entries=_by_tier(missing, current))
        )
    if extra:
        differences.append(
            cs.REFERENCE_ALSO_PARSES.format(entries=_by_tier(extra, None))
        )
    return cs.REFERENCE_ENVIRONMENT_DIFFERS.format(
        differences=cs.REFERENCE_GROUP_SEPARATOR.join(differences),
        install=cs.REFERENCE_INSTALL,
        regenerate=cs.REFERENCE_REGENERATE,
    )


def _summary(lines: Sequence[str]) -> str:
    nodes = sum(1 for line in lines if line.startswith(cs.REFERENCE_NODE_TAG))
    return f"{nodes} nodes, {len(lines) - nodes} relationships"


def _report(title: str, diff: Sequence[str]) -> None:
    removed = sum(1 for line in diff if line.startswith("-"))
    say(f"{title}: {removed} lines only before, {len(diff) - removed} only after")
    for line in diff[: cs.REFERENCE_DIFF_LINES]:
        say(f"    {line}")
    if len(diff) > cs.REFERENCE_DIFF_LINES:
        say(f"    ... and {len(diff) - cs.REFERENCE_DIFF_LINES} more")


def checkouts(
    repo: ReferenceRepo, checkout: Path, runs: int
) -> Iterator[tuple[int | None, Path]]:
    """Where each of `runs` runs indexes, with the hash seed it runs under:
    the checkout itself, then for each later run a copy at another path,
    which lives until the next one is asked for."""
    yield None, checkout
    for seed in range(1, runs):
        with tempfile.TemporaryDirectory(prefix="cgr-reference-copy-") as tmp:
            copy = Path(tmp) / f"run{seed}" / repo["name"]
            shutil.copytree(checkout, copy, symlinks=True)
            yield seed, copy


def _graphs(
    cgr: Sequence[str],
    repo: ReferenceRepo,
    checkout: Path,
    runs: int,
    index_engine: str | None,
) -> tuple[list[str], list[tuple[int, list[str]]]]:
    """The first run's graph and, for each later run, its own: each later run
    indexes a copy at another path under another hash seed."""
    first: list[str] = []
    later: list[tuple[int, list[str]]] = []
    for seed, source in checkouts(repo, checkout, runs):
        graph = canonical(index(cgr, repo, source, seed, index_engine), source)
        if seed is None:
            first = graph
        else:
            later.append((seed, graph))
    return first, later


def _same_file(path: str) -> str:
    # `absolute()`, not `resolve()`: a venv's python is a symlink to the
    # base interpreter, which is not this Python's install.
    return os.path.normcase(str(Path(path).absolute()))


def _runs_this_python(cgr: Sequence[str]) -> bool:
    """Whether `cgr` runs under this Python: its own `cgr` script, or this
    interpreter itself (`python -m ...`)."""
    found = shutil.which(cgr[0]) if cgr else None
    if found is None:
        return False
    here = sys.executable
    own = shutil.which(cs.REFERENCE_CGR_PROGRAM, path=str(Path(here).parent))
    ours = {_same_file(here)} | ({_same_file(own)} if own else set())
    return _same_file(found) in ours


def environment_gate(
    command: str,
    cgr: Sequence[str],
    directory: Path,
    partial: bool,
    *,
    what: str = cs.REFERENCE_GRAPHS_RECORDED,
    regenerate: str = cs.REFERENCE_REGENERATE,
) -> tuple[list[str], bool]:
    """What this Python parses, and whether the run may go on.

    `what` and `regenerate` name what the run records and the command
    that records it, for a check of something other than the graphs that
    shares their environment.txt.
    """
    if not _runs_this_python(cgr):
        if command == "record":
            say(
                cs.REFERENCE_OTHER_CGR_RECORD.format(
                    cgr=shlex.join(cgr),
                    python=sys.executable,
                    recorded=what,
                    regenerate=regenerate,
                )
            )
            return [], False
        say(
            cs.REFERENCE_OTHER_CGR_CHECK.format(
                cgr=shlex.join(cgr),
                path=environment_path(directory),
                recorded=what,
            )
        )
        return [], True
    environment = probe()
    # A partial recording joins graphs recorded elsewhere, so it must parse
    # what they were recorded with, as a check must.
    if command == "check" or partial:
        recorded = environment_path(directory)
        refusal = environment_refusal(
            read_graph(recorded) if recorded.is_file() else None,
            environment,
            directory,
        )
        if refusal:
            say(refusal)
            return environment, False
    return environment, True


def run(
    command: str,
    repos: Sequence[ReferenceRepo],
    cgr: Sequence[str],
    cache: Path,
    runs: int,
    directory: Path = REFERENCE_DIR,
    *,
    partial: bool = False,
    index_engine: str | None = None,
) -> int:
    environment, proceed = environment_gate(command, cgr, directory, partial)
    if not proceed:
        return cs.REFERENCE_EXIT_FAILED
    status = 0
    # `record` writes nothing until every repository has indexed and every
    # run agreed: a half-finished or unstable recording must not replace a
    # good graph.
    staged: list[tuple[Path, list[str]]] = []
    for repo in repos:
        checkout = fetch(repo, cache)
        first, later = _graphs(cgr, repo, checkout, runs, index_engine)
        say(f"{repo['name']} ({repo['language']}): {_summary(first)}")
        for seed, graph in later:
            diff = difference(first, graph)
            if diff:
                status = cs.REFERENCE_EXIT_DIFFERENT
                _report(
                    f"  run with PYTHONHASHSEED={seed} differs from the first", diff
                )
        path = graph_path(repo, directory)
        if command == "record":
            staged.append((path, first))
            continue
        if not path.is_file():
            status = cs.REFERENCE_EXIT_DIFFERENT
            say(f"  no recorded graph at {path}; run `{cs.REFERENCE_REGENERATE}`")
            continue
        diff = difference(read_graph(path), first)
        if diff:
            status = cs.REFERENCE_EXIT_DIFFERENT
            shown = (
                path.relative_to(PROJECT_ROOT)
                if path.is_relative_to(PROJECT_ROOT)
                else path
            )
            _report(f"  differs from {shown}", diff)
    if command == "record":
        if status:
            say("Runs disagreed, so no graph was written.")
            return status
        for path, lines in staged:
            write_graph(path, lines)
        if not partial:
            write_graph(environment_path(directory), environment)
    if command == "check" and status == cs.REFERENCE_EXIT_DIFFERENT:
        say(
            "If the change is intended, run "
            f"`{cs.REFERENCE_REGENERATE}` and commit {cs.REFERENCE_DIR}."
        )
    return status


def cgr_command(text: str) -> list[str]:
    """`--cgr` as an argument list, with a relative program path made
    absolute: the index runs from a scratch directory."""
    program, *rest = shlex.split(text)
    if os.sep in program or "/" in program:
        # `absolute()`, not `resolve()`: a venv's python is a symlink to the
        # base interpreter, which cannot import what the venv installed.
        program = str(Path(program).absolute())
    return [program, *rest]


def index_engine_option(text: str) -> str:
    """`--index-engine`, refused the way cgr refuses the setting at start-up,
    before any repository is fetched."""
    # Imported here: `_CALLER_ENV` is taken when this module loads.
    from codebase_rag.index_engine import index_engine_error

    if (error := index_engine_error(text)) is not None:
        raise argparse.ArgumentTypeError(error)
    return text


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "command", choices=("record", "check", cs.REFERENCE_ENVIRONMENT_COMMAND)
    )
    parser.add_argument("names", nargs="*", help="only these repositories")
    parser.add_argument(
        "--cgr", default=cs.REFERENCE_CGR_PROGRAM, help="the command that runs cgr"
    )
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument(
        "--index-engine",
        type=index_engine_option,
        metavar="SPEC",
        help="index on this CGR_INDEX_ENGINE selection (none of the shell's arrives)",
    )
    # How `probe` runs `environment` in its own process.
    parser.add_argument(
        cs.REFERENCE_IN_PROCESS_FLAG, action="store_true", help=argparse.SUPPRESS
    )
    args = parser.parse_args(argv)
    if args.command == cs.REFERENCE_ENVIRONMENT_COMMAND:
        for line in parse_capabilities() if args.in_process else probe():
            say(line)
        return 0
    repos = load_manifest()
    unknown = set(args.names) - {repo["name"] for repo in repos}
    if unknown:
        parser.error(f"unknown repositories: {', '.join(sorted(unknown))}")
    chosen = [repo for repo in repos if not args.names or repo["name"] in args.names]
    return run(
        args.command,
        chosen,
        cgr_command(args.cgr),
        args.cache,
        max(args.runs, 1),
        partial=bool(args.names),
        index_engine=args.index_engine,
    )


if __name__ == "__main__":
    sys.exit(main())
