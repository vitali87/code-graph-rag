#!/usr/bin/env python3
"""Record, or check, the reference graphs of pinned repositories (issue #3629).

Each repository in fixtures/reference_graphs/repos.json is fetched at its
pinned commit, indexed by the `cgr` on PATH (or the command `--cgr` names)
into an embedded graph of its own, exported, and written out as sorted text:
one node or relationship per line, keyed by what the graph keys it on, with
the checkout's directory written as `<root>` and the time of the run left
out. Nothing here imports the indexer, so the same files hold the Rust
rewrite to the graphs the Python indexer builds today.

    uv run python scripts/reference_graphs.py record           # rewrite them all
    uv run python scripts/reference_graphs.py check            # exit 1 on a difference
    uv run python scripts/reference_graphs.py check --runs 3   # and on a run-to-run one
    uv run python scripts/reference_graphs.py record itsdangerous

With `--runs N`, runs after the first index a copy of the checkout at another
path under another `PYTHONHASHSEED`; every run must give the same graph.
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
from collections import Counter
from collections.abc import Iterable, Sequence
from functools import cache
from pathlib import Path

from codebase_rag import constants as cs
from codebase_rag.types_defs import JsonValue, ReferenceRepo

# The environment as the caller set it, read before anything imports cgr's
# settings: that import merges the working directory's `.env` into
# `os.environ`, and a `GIT_DIR` or a cgr setting from it must not reach the
# Git calls or the index.
_CALLER_ENV = dict(os.environ)

PROJECT_ROOT = Path(__file__).parent.parent
REFERENCE_DIR = PROJECT_ROOT / cs.REFERENCE_DIR
DEFAULT_CACHE = Path.home() / ".cache" / cs.REFERENCE_CACHE_DIRNAME

type ExportRecord = dict[str, JsonValue]


def _say(text: str) -> None:
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


def _index_env(home: Path, seed: int | None) -> dict[str, str]:
    # None of the caller's cgr settings reach the index: a `CGR_CAPTURE` or a
    # frontend choice left in a shell would change the graph `record` writes.
    # Settings are read whatever the case of their name, so `go_frontend`
    # counts as `GO_FRONTEND`.
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
    return env


def index(
    cgr: Sequence[str], repo: ReferenceRepo, source: Path, seed: int | None
) -> ExportRecord:
    """Index `source` into a graph of its own and return `cgr`'s JSON export."""
    with tempfile.TemporaryDirectory(prefix="cgr-reference-") as tmp:
        work = Path(tmp)
        home = work / "home"
        home.mkdir()
        output = work / "graph.json"
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
            env=_index_env(home, seed),
            capture_output=True,
            text=True,
            timeout=cs.REFERENCE_INDEX_TIMEOUT_S,
        )
        if result.returncode != 0 or not output.is_file():
            raise SystemExit(
                f"{repo['name']}: `{shlex.join(command)}` exited "
                f"{result.returncode}\n{result.stderr[-4000:]}"
            )
        return json.loads(output.read_text(encoding=cs.ENCODING_UTF8))


def _relative(value: JsonValue, roots: Sequence[str]) -> JsonValue:
    if isinstance(value, str):
        for root in roots:
            if value == root or value.startswith((f"{root}/", f"{root}{os.sep}")):
                rest = value[len(root) :].replace(os.sep, "/")
                return f"{cs.REFERENCE_ROOT_PLACEHOLDER}{rest}"
        return value
    if isinstance(value, list):
        return [_relative(item, roots) for item in value]
    if isinstance(value, dict):
        return {key: _relative(item, roots) for key, item in value.items()}
    return value


def _json(value: JsonValue) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _properties(record: ExportRecord, roots: Sequence[str]) -> dict[str, JsonValue]:
    properties = record.get("properties") or {}
    if not isinstance(properties, dict):
        raise SystemExit(f"export record without a properties map: {record}")
    return {
        name: _relative(value, roots)
        for name, value in properties.items()
        if name not in cs.REFERENCE_VOLATILE_PROPERTIES
    }


def canonical(export: ExportRecord, source: Path) -> list[str]:
    """The export as sorted lines, the same wherever and whenever it ran.

    A node is named by its labels and the property the graph keys that label
    on (`qualified_name`, `absolute_path`, `name`), not by the export's
    `node_id`, which is only a row number of this run's database. Duplicate
    lines are kept: an edge recorded twice is a difference.
    """
    # Native and forward-slash spellings of both: on Windows the export
    # writes `C:/cache/x/...` while `str(Path)` gives `C:\cache\x`.
    roots = sorted(
        {
            spelling
            for path in (source, source.resolve())
            for spelling in (str(path), path.as_posix())
        },
        key=len,
        reverse=True,
    )
    sep = cs.REFERENCE_FIELD_SEPARATOR
    names: dict[int, str] = {}
    lines: list[str] = []
    # Both lists must be there, even empty: an export missing one would read
    # as an empty graph and `record` would write it over a full one.
    nodes = export.get("nodes")
    relationships = export.get("relationships")
    if not isinstance(nodes, list) or not isinstance(relationships, list):
        raise SystemExit("export without node and relationship lists")
    for node in nodes:
        node_id = node.get("node_id") if isinstance(node, dict) else None
        if not isinstance(node, dict) or not isinstance(node_id, int):
            raise SystemExit(f"export node without a node_id: {node}")
        raw_labels = node.get("labels") or []
        labels = (
            sorted(str(label) for label in raw_labels)
            if isinstance(raw_labels, list)
            else []
        )
        properties = _properties(node, roots)
        key_name = next(
            (
                cs.NODE_UNIQUE_CONSTRAINTS[label]
                for label in labels
                if label in cs.NODE_UNIQUE_CONSTRAINTS
            ),
            None,
        )
        key = properties.pop(key_name, None) if key_name else None
        label_text = cs.REFERENCE_LABEL_SEPARATOR.join(labels)
        names[node_id] = sep.join((label_text, _json(key)))
        lines.append(
            sep.join((cs.REFERENCE_NODE_TAG, label_text, _json(key), _json(properties)))
        )
    for relationship in relationships:
        if not isinstance(relationship, dict):
            raise SystemExit(f"export relationship is not a record: {relationship}")
        from_id = relationship.get("from_id")
        to_id = relationship.get("to_id")
        source_name = names.get(from_id) if isinstance(from_id, int) else None
        target_name = names.get(to_id) if isinstance(to_id, int) else None
        if source_name is None or target_name is None:
            raise SystemExit(f"relationship to a node the export lacks: {relationship}")
        lines.append(
            sep.join(
                (
                    cs.REFERENCE_EDGE_TAG,
                    str(relationship.get("type")),
                    source_name,
                    target_name,
                    _json(_properties(relationship, roots)),
                )
            )
        )
    return sorted(lines)


def read_graph(path: Path) -> list[str]:
    # Split on the newlines `write_graph` writes, not `splitlines()`'s wider
    # set: a docstring may hold U+2028, which JSON leaves unescaped.
    text = path.read_text(encoding=cs.ENCODING_UTF8)
    return [line.removesuffix("\r") for line in text.split("\n") if line]


def write_graph(path: Path, lines: Iterable[str]) -> None:
    path.write_text("".join(f"{line}\n" for line in lines), encoding=cs.ENCODING_UTF8)


def difference(expected: Sequence[str], actual: Sequence[str]) -> list[str]:
    """The lines only one side has, `-` for `expected`'s and `+` for `actual`'s."""
    missing = Counter(expected) - Counter(actual)
    extra = Counter(actual) - Counter(expected)
    removed = [f"- {line}" for line in sorted(missing.elements())]
    added = [f"+ {line}" for line in sorted(extra.elements())]
    return removed + added


def _summary(lines: Sequence[str]) -> str:
    nodes = sum(1 for line in lines if line.startswith(cs.REFERENCE_NODE_TAG))
    return f"{nodes} nodes, {len(lines) - nodes} relationships"


def _report(title: str, diff: Sequence[str]) -> None:
    removed = sum(1 for line in diff if line.startswith("-"))
    _say(f"{title}: {removed} lines only before, {len(diff) - removed} only after")
    for line in diff[: cs.REFERENCE_DIFF_LINES]:
        _say(f"    {line}")
    if len(diff) > cs.REFERENCE_DIFF_LINES:
        _say(f"    ... and {len(diff) - cs.REFERENCE_DIFF_LINES} more")


def _graphs(
    cgr: Sequence[str], repo: ReferenceRepo, checkout: Path, runs: int
) -> tuple[list[str], list[tuple[int, list[str]]]]:
    """The first run's graph and, for each later run, its own: each later run
    indexes a copy at another path under another hash seed."""
    first = canonical(index(cgr, repo, checkout, None), checkout)
    later: list[tuple[int, list[str]]] = []
    for seed in range(1, runs):
        with tempfile.TemporaryDirectory(prefix="cgr-reference-copy-") as tmp:
            copy = Path(tmp) / f"run{seed}" / repo["name"]
            shutil.copytree(checkout, copy, symlinks=True)
            later.append((seed, canonical(index(cgr, repo, copy, seed), copy)))
    return first, later


def run(
    command: str,
    repos: Sequence[ReferenceRepo],
    cgr: Sequence[str],
    cache: Path,
    runs: int,
    directory: Path = REFERENCE_DIR,
) -> int:
    status = 0
    # `record` writes nothing until every repository has indexed and every
    # run agreed: a half-finished or unstable recording must not replace a
    # good graph.
    staged: list[tuple[Path, list[str]]] = []
    for repo in repos:
        checkout = fetch(repo, cache)
        first, later = _graphs(cgr, repo, checkout, runs)
        _say(f"{repo['name']} ({repo['language']}): {_summary(first)}")
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
            _say(f"  no recorded graph at {path}; run `{cs.REFERENCE_REGENERATE}`")
            continue
        diff = difference(read_graph(path), first)
        if diff:
            status = cs.REFERENCE_EXIT_DIFFERENT
            _report(f"  differs from {path.relative_to(PROJECT_ROOT)}", diff)
    if command == "record":
        if status:
            _say("Runs disagreed, so no graph was written.")
            return status
        for path, lines in staged:
            write_graph(path, lines)
    if command == "check" and status == cs.REFERENCE_EXIT_DIFFERENT:
        _say(
            "If the change is intended, run "
            f"`{cs.REFERENCE_REGENERATE}` and commit {cs.REFERENCE_DIR}."
        )
    return status


def _command(text: str) -> list[str]:
    """`--cgr` as an argument list, with a relative program path made
    absolute: the index runs from a scratch directory."""
    program, *rest = shlex.split(text)
    if os.sep in program or "/" in program:
        # `absolute()`, not `resolve()`: a venv's python is a symlink to the
        # base interpreter, which cannot import what the venv installed.
        program = str(Path(program).absolute())
    return [program, *rest]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=("record", "check"))
    parser.add_argument("names", nargs="*", help="only these repositories")
    parser.add_argument("--cgr", default="cgr", help="the command that runs cgr")
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--runs", type=int, default=1)
    args = parser.parse_args(argv)
    repos = load_manifest()
    unknown = set(args.names) - {repo["name"] for repo in repos}
    if unknown:
        parser.error(f"unknown repositories: {', '.join(sorted(unknown))}")
    chosen = [repo for repo in repos if not args.names or repo["name"] in args.names]
    return run(args.command, chosen, _command(args.cgr), args.cache, max(args.runs, 1))


if __name__ == "__main__":
    sys.exit(main())
