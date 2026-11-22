#!/usr/bin/env python3
"""Record, or check, what commands and MCP tools answer on the pinned repositories.

Each repository in fixtures/reference_graphs/repos.json is fetched and
indexed as scripts/reference_graphs.py does it (the same cache, an embedded
graph of its own, the `cgr` on PATH or the command `--cgr` names), then asked
every case in fixtures/reference_answers/<repo>/cases.json: a command line,
run with `-q`, or an MCP tool call, sent to `cgr mcp-server` over stdio
JSON-RPC as a client sends it. Each answer, the exit status and the output or
the tool's `isError` and content, is kept as `<case>.txt` beside the list.
What differs between machines and runs is written as a placeholder: the
checkout's directory as `<root>`, the scratch directory the commands ran in
as `<tmp>`, and the graph's row number of a node as `<node_id>` (a case
names the node by its qualified name instead). Nothing else is rewritten.
The answers come from `cgr`, not from importing it, so the same files hold
the Rust rewrite to what the Python implementation answers today.

Run as a module, with what the reference graphs are recorded with:

    uv run --extra treesitter-full --extra ast-grep python -m scripts.reference_answers record
    uv run python -m scripts.reference_answers check            # exit 1 on a difference
    uv run python -m scripts.reference_answers check --runs 2   # and on a run-to-run one
    uv run python -m scripts.reference_answers record itsdangerous

With `--runs N`, runs after the first index a copy of the checkout at another
path under another `PYTHONHASHSEED` and ask it again; every run must answer
the same. The install is compared with fixtures/reference_graphs/
environment.txt first, as the graphs' check does: answers are recorded with
the install the graphs are.
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import queue
import re
import shlex
import subprocess
import sys
import threading
from collections import deque
from collections.abc import Generator, Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from typing import NamedTuple

from codebase_rag import constants as cs
from codebase_rag.types_defs import JsonValue, ReferenceCase, ReferenceRepo
from scripts.reference_graphs import (
    DEFAULT_CACHE,
    PROJECT_ROOT,
    REFERENCE_DIR,
    ExportRecord,
    Indexed,
    cgr_command,
    checkouts,
    environment_gate,
    fetch,
    indexed,
    load_manifest,
    say,
)

ANSWERS_DIR = PROJECT_ROOT / cs.REFERENCE_ANSWERS_DIR
_CASE_NAME = re.compile(cs.REFERENCE_ANSWERS_CASE_NAME)
_NODE_ID_ARGUMENT = re.compile(cs.REFERENCE_ANSWERS_NODE_ID_ARGUMENT)

type JsonObject = dict[str, JsonValue]


class RepoOutcome(NamedTuple):
    """What asking one repository came to, reported in manifest order."""

    lines: list[str]
    status: int
    answers: dict[str, str]


def cases_path(repo: ReferenceRepo, directory: Path = ANSWERS_DIR) -> Path:
    return directory / repo["name"] / cs.REFERENCE_ANSWERS_CASES


def answer_path(repo: ReferenceRepo, name: str, directory: Path = ANSWERS_DIR) -> Path:
    return directory / repo["name"] / f"{name}{cs.REFERENCE_ANSWERS_SUFFIX}"


def _case_error(path: Path, problem: str) -> SystemExit:
    return SystemExit(f"{path}: {problem}")


def load_cases(
    repo: ReferenceRepo, directory: Path = ANSWERS_DIR
) -> list[ReferenceCase]:
    """The repository's cases, refused whole when one is malformed: a case
    that silently asked nothing would read as an answer kept."""
    path = cases_path(repo, directory)
    if not path.is_file():
        return []
    entries = json.loads(path.read_text(encoding=cs.ENCODING_UTF8))
    if not isinstance(entries, list):
        raise _case_error(path, "expected a list of cases")
    cases: list[ReferenceCase] = []
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise _case_error(path, f"a case is not an object: {entry!r}")
        name = entry.get("name")
        if not isinstance(name, str) or not _CASE_NAME.fullmatch(name):
            raise _case_error(path, f"a case needs a file-safe name: {entry!r}")
        if name in seen:
            raise _case_error(path, f"two cases are named {name}")
        seen.add(name)
        unknown = set(entry) - set(ReferenceCase.__annotations__)
        if unknown:
            raise _case_error(path, f"{name}: unknown keys {sorted(unknown)}")
        cli, tool, arguments = (
            entry.get("cli"),
            entry.get("tool"),
            entry.get("arguments"),
        )
        if (cli is None) == (tool is None):
            raise _case_error(path, f"{name}: give exactly one of cli and tool")
        if cli is not None:
            if not isinstance(cli, list) or not all(isinstance(a, str) for a in cli):
                raise _case_error(path, f"{name}: cli is a list of arguments")
            if arguments is not None:
                raise _case_error(path, f"{name}: arguments go with a tool")
            cases.append(ReferenceCase(name=name, cli=cli))
            continue
        if not isinstance(tool, str) or not tool:
            raise _case_error(path, f"{name}: tool is a tool's name")
        if arguments is not None and not isinstance(arguments, dict):
            raise _case_error(path, f"{name}: arguments is an object")
        case = ReferenceCase(name=name, tool=tool)
        if arguments is not None:
            case["arguments"] = arguments
        cases.append(case)
    return cases


def _spellings(path: Path) -> set[str]:
    """Every way an answer may write `path`: as given and resolved, native
    and with forward slashes, and inside a JSON string."""
    plain = {
        spelling
        for candidate in (path, path.resolve())
        for spelling in (str(candidate), candidate.as_posix())
    }
    return plain | {json.dumps(spelling)[1:-1] for spelling in plain}


class Placeholders(NamedTuple):
    """What an answer writes as a placeholder, longest spelling first."""

    replacements: tuple[tuple[re.Pattern[str], str], ...]

    @classmethod
    def build(
        cls, root: Path, work: Path, node_ids: Iterable[int] = ()
    ) -> Placeholders:
        paths = [
            (spelling, cs.REFERENCE_ROOT_PLACEHOLDER) for spelling in _spellings(root)
        ] + [
            (spelling, cs.REFERENCE_ANSWERS_TMP_PLACEHOLDER)
            for spelling in _spellings(work)
        ]
        paths.sort(key=lambda pair: len(pair[0]), reverse=True)
        # A path ends where a name could not go on: `<root>-old` would be
        # another directory, `<root>/src` is under it.
        replacements = [
            (re.compile(f"{re.escape(spelling)}(?![\\w-])"), placeholder)
            for spelling, placeholder in paths
        ]
        # The number alone: not a digit of a longer one, nor a decimal's.
        replacements.extend(
            (
                re.compile(f"(?<![\\w.]){node_id}(?!\\w|\\.\\d)"),
                cs.REFERENCE_ANSWERS_NODE_ID_PLACEHOLDER,
            )
            for node_id in node_ids
        )
        return cls(tuple(replacements))

    def apply(self, text: str) -> str:
        for pattern, placeholder in self.replacements:
            text = pattern.sub(placeholder, text)
        return text


def _node_ids(export: ExportRecord) -> dict[str, list[int]]:
    """The row numbers of the export's nodes, by qualified name."""
    found: dict[str, list[int]] = {}
    nodes = export.get("nodes")
    for node in nodes if isinstance(nodes, list) else []:
        if not isinstance(node, dict):
            continue
        properties = node.get("properties")
        node_id = node.get("node_id")
        name = (
            properties.get(cs.KEY_QUALIFIED_NAME)
            if isinstance(properties, dict)
            else None
        )
        if isinstance(name, str) and isinstance(node_id, int):
            found.setdefault(name, []).append(node_id)
    return found


class _Arguments:
    """A case's arguments made concrete for one run: `<root>` is the
    checkout, `<node_id:NAME>` the row number of the node named NAME."""

    def __init__(self, root: Path, export: ExportRecord) -> None:
        self._root = str(root)
        self._ids = _node_ids(export)
        self.used_ids: list[int] = []

    def text(self, value: str) -> str | int:
        if match := _NODE_ID_ARGUMENT.fullmatch(value):
            name = match["qualified_name"]
            ids = self._ids.get(name, [])
            if len(ids) != 1:
                raise SystemExit(
                    f"{name} names {len(ids)} nodes in the export; a case's "
                    "`<node_id:...>` must name exactly one"
                )
            self.used_ids.append(ids[0])
            return ids[0]
        return value.replace(cs.REFERENCE_ROOT_PLACEHOLDER, self._root)

    def json(self, value: JsonValue) -> JsonValue:
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, list):
            return [self.json(item) for item in value]
        if isinstance(value, dict):
            return {key: self.json(item) for key, item in value.items()}
        return value


def _section(name: str, text: str) -> str:
    # A section ends at a line's end, so the next header starts its own.
    body = text if not text or text.endswith("\n") else f"{text}\n"
    return f"{cs.REFERENCE_ANSWERS_SECTION.format(name=name)}\n{body}"


def cli_answer(args: Sequence[str], code: int, stdout: str, stderr: str) -> str:
    command = shlex.join([cs.REFERENCE_CGR_PROGRAM, cs.REFERENCE_ANSWERS_QUIET, *args])
    return (
        f"{cs.REFERENCE_ANSWERS_CLI_HEADER.format(command=command)}\n"
        f"{cs.REFERENCE_ANSWERS_STATUS.format(code=code)}\n"
        + _section(cs.REFERENCE_ANSWERS_STDOUT, stdout)
        + _section(cs.REFERENCE_ANSWERS_STDERR, stderr)
    )


def _without_prose(schema: JsonValue) -> JsonValue:
    # As the public surface records the schemas: the prose changes with
    # every rewording and is not what a client relies on.
    if isinstance(schema, dict):
        return {
            key: _without_prose(value)
            for key, value in schema.items()
            if key not in cs.SURFACE_PROSE_SCHEMA_KEYS
        }
    if isinstance(schema, list):
        return [_without_prose(item) for item in schema]
    return schema


def _rpc_error(response: JsonObject) -> str | None:
    error = response.get("error")
    if not isinstance(error, dict):
        return None
    return cs.REFERENCE_ANSWERS_RPC_ERROR.format(
        code=error.get("code"), message=error.get("message")
    )


def tools_answer(response: JsonObject) -> str:
    header = cs.REFERENCE_ANSWERS_MCP_HEADER.format(
        method=cs.REFERENCE_ANSWERS_TOOLS_LIST
    )
    if (error := _rpc_error(response)) is not None:
        return f"{header}\n{error}\n"
    result = response.get("result")
    tools = result.get("tools") if isinstance(result, dict) else None
    listed: list[JsonValue] = [
        {
            "name": tool.get("name"),
            "inputSchema": _without_prose(tool.get("inputSchema")),
        }
        for tool in (tools if isinstance(tools, list) else [])
        if isinstance(tool, dict)
        and tool.get("name") not in cs.REFERENCE_ANSWERS_OPTIONAL_TOOLS
    ]
    text = json.dumps(listed, indent=2, sort_keys=True, ensure_ascii=False)
    return f"{header}\n{_section(cs.REFERENCE_ANSWERS_TOOLS, text)}"


def call_answer(
    tool: str, arguments: JsonObject, response: JsonObject, placeholders: Placeholders
) -> str:
    header = cs.REFERENCE_ANSWERS_MCP_CALL_HEADER.format(
        method=cs.REFERENCE_ANSWERS_TOOLS_CALL,
        tool=tool,
        arguments=json.dumps(arguments, sort_keys=True, ensure_ascii=False),
    )
    if (error := _rpc_error(response)) is not None:
        return f"{header}\n{placeholders.apply(error)}\n"
    result = response.get("result")
    if not isinstance(result, dict):
        raise SystemExit(f"tools/call {tool}: a response without a result")
    is_error = json.dumps(result.get("isError", False))
    parts = [f"{header}\n{cs.REFERENCE_ANSWERS_IS_ERROR.format(value=is_error)}\n"]
    content = result.get("content")
    for item in content if isinstance(content, list) else []:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("type"))
        text = item.get("text")
        parts.append(
            _section(kind, placeholders.apply(text) if isinstance(text, str) else "")
        )
    return "".join(parts)


class McpSession:
    """`cgr mcp-server` on stdio, spoken to as a client does: one JSON-RPC
    message per line each way, `initialize` first."""

    def __init__(self, command: Sequence[str], env: dict[str, str], cwd: Path) -> None:
        self._command = shlex.join(command)
        self._process = subprocess.Popen(
            list(command),
            cwd=cwd,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding=cs.ENCODING_UTF8,
        )
        self._lines: queue.Queue[str | None] = queue.Queue()
        # Drained as it comes: the server logs every call to stderr, and a
        # full pipe would stop it mid-answer.
        self._stderr: deque[str] = deque(maxlen=200)
        self._next_id = 0
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

    def _read_stdout(self) -> None:
        stream = self._process.stdout
        if stream is not None:
            for line in stream:
                self._lines.put(line)
        self._lines.put(None)

    def _read_stderr(self) -> None:
        stream = self._process.stderr
        if stream is not None:
            for line in stream:
                self._stderr.append(line)

    def _failure(self, problem: str) -> SystemExit:
        tail = "".join(self._stderr)[-cs.REFERENCE_ANSWERS_STDERR_TAIL :]
        return SystemExit(f"`{self._command}` {problem}\n{tail}")

    def _send(self, message: JsonObject) -> None:
        stream = self._process.stdin
        if stream is None:
            raise self._failure("has no stdin")
        try:
            stream.write(f"{json.dumps(message)}\n")
            stream.flush()
        except OSError as e:
            raise self._failure(f"stopped reading its input: {e}") from e

    def notify(self, method: str) -> None:
        self._send({"jsonrpc": cs.REFERENCE_ANSWERS_JSONRPC, "method": method})

    def request(self, method: str, params: JsonObject | None = None) -> JsonObject:
        self._next_id += 1
        message: JsonObject = {
            "jsonrpc": cs.REFERENCE_ANSWERS_JSONRPC,
            "id": self._next_id,
            "method": method,
        }
        if params is not None:
            message["params"] = params
        self._send(message)
        while True:
            try:
                line = self._lines.get(timeout=cs.REFERENCE_ANSWERS_CALL_TIMEOUT_S)
            except queue.Empty as e:
                raise self._failure(f"did not answer {method} in time") from e
            if line is None:
                raise self._failure(f"exited before answering {method}")
            try:
                response = json.loads(line)
            except json.JSONDecodeError as e:
                raise self._failure(f"wrote a line that is not JSON: {line!r}") from e
            # Notifications and the server's own requests carry no answer.
            if isinstance(response, dict) and response.get("id") == self._next_id:
                if "method" not in response:
                    return response

    def close(self) -> None:
        if self._process.stdin is not None:
            self._process.stdin.close()
        try:
            self._process.wait(timeout=cs.REFERENCE_ANSWERS_CALL_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            self._process.kill()
            self._process.wait()


@contextmanager
def mcp_session(
    cgr: Sequence[str], env: dict[str, str], cwd: Path
) -> Generator[McpSession]:
    session = McpSession(
        [*cgr, "mcp-server", "--workspace", cs.REFERENCE_ANSWERS_WORKSPACE], env, cwd
    )
    try:
        session.request(
            cs.REFERENCE_ANSWERS_INITIALIZE,
            {
                "protocolVersion": cs.REFERENCE_ANSWERS_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {
                    "name": cs.REFERENCE_ANSWERS_CLIENT_NAME,
                    "version": "0",
                },
            },
        )
        session.notify(cs.REFERENCE_ANSWERS_INITIALIZED)
        yield session
    finally:
        session.close()


def _query_env(done: Indexed, root: Path) -> dict[str, str]:
    """The index's environment, so a command reads the graph it built, with a
    fixed console and the server rooted at the checkout."""
    env = {
        name: value
        for name, value in done.env.items()
        if name.upper() not in cs.REFERENCE_ANSWERS_DROPPED_ENV
    }
    env.update(cs.REFERENCE_ANSWERS_CONSOLE_ENV)
    env[cs.REFERENCE_ANSWERS_TARGET_REPO_ENV] = str(root)
    return env


def _run_cli(
    cgr: Sequence[str], args: Sequence[str], env: dict[str, str], cwd: Path
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [*cgr, cs.REFERENCE_ANSWERS_QUIET, *args],
        check=False,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        encoding=cs.ENCODING_UTF8,
        errors="backslashreplace",
        timeout=cs.REFERENCE_ANSWERS_CALL_TIMEOUT_S,
    )


def _workspace(
    cgr: Sequence[str], repo: ReferenceRepo, root: Path, env: dict[str, str], cwd: Path
) -> None:
    """A workspace serving the checkout under the reference index's name."""
    for args in (
        ["workspace", "create", cs.REFERENCE_ANSWERS_WORKSPACE],
        [
            "workspace",
            "add-repo",
            cs.REFERENCE_ANSWERS_WORKSPACE,
            str(root),
            "--project-name",
            repo["name"],
        ],
    ):
        result = _run_cli(cgr, args, env, cwd)
        if result.returncode != 0:
            raise SystemExit(
                f"{repo['name']}: `{shlex.join([*cgr, *args])}` exited "
                f"{result.returncode}\n{result.stderr[-cs.REFERENCE_ANSWERS_STDERR_TAIL :]}"
            )


def ask(
    cgr: Sequence[str],
    repo: ReferenceRepo,
    cases: Sequence[ReferenceCase],
    root: Path,
    done: Indexed,
) -> dict[str, str]:
    """Every case's answer, by case name, from the graph `done` built of `root`."""
    env = _query_env(done, root)
    answers: dict[str, str] = {}
    for case in cases:
        if "cli" not in case:
            continue
        arguments = _Arguments(root, done.export)
        args = [str(arguments.text(arg)) for arg in case["cli"]]
        result = _run_cli(cgr, args, env, done.work)
        placeholders = Placeholders.build(root, done.work, arguments.used_ids)
        answers[case["name"]] = cli_answer(
            case["cli"],
            result.returncode,
            placeholders.apply(result.stdout),
            placeholders.apply(result.stderr),
        )
    calls = [case for case in cases if "tool" in case]
    if not calls:
        return answers
    _workspace(cgr, repo, root, env, done.work)
    with mcp_session(cgr, env, done.work) as session:
        for case in calls:
            tool = case["tool"]
            if tool == cs.REFERENCE_ANSWERS_TOOLS_LIST:
                answers[case["name"]] = tools_answer(session.request(tool))
                continue
            arguments = _Arguments(root, done.export)
            given = case.get("arguments", {})
            concrete = arguments.json(given)
            response = session.request(
                cs.REFERENCE_ANSWERS_TOOLS_CALL,
                {"name": tool, "arguments": concrete},
            )
            answers[case["name"]] = call_answer(
                tool,
                given,
                response,
                Placeholders.build(root, done.work, arguments.used_ids),
            )
    return answers


def difference(expected: str, actual: str) -> list[str]:
    """`expected` against `actual` as a unified diff, empty only when they are
    the same text.

    Split on the one line end the answers are written with, not
    `splitlines()`'s wider set: a carriage return, a U+2028 or a missing last
    newline is a difference, and a carriage return is shown as `\\r`.
    """
    return [
        line.replace("\r", "\\r")
        for line in difflib.unified_diff(
            expected.split("\n"),
            actual.split("\n"),
            "recorded",
            "answered",
            lineterm="",
        )
    ]


def _report(lines: list[str], title: str, diff: Sequence[str]) -> None:
    lines.append(title)
    lines.extend(f"    {line}" for line in diff[: cs.REFERENCE_ANSWERS_DIFF_LINES])
    if len(diff) > cs.REFERENCE_ANSWERS_DIFF_LINES:
        lines.append(f"    ... and {len(diff) - cs.REFERENCE_ANSWERS_DIFF_LINES} more")


def read_answer(path: Path) -> str:
    # Bytes as written: an answer may hold a carriage return of its own.
    return path.read_bytes().decode(cs.ENCODING_UTF8)


def write_answer(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode(cs.ENCODING_UTF8))


def recorded_names(repo: ReferenceRepo, directory: Path) -> set[str]:
    folder = directory / repo["name"]
    return {
        path.name.removesuffix(cs.REFERENCE_ANSWERS_SUFFIX)
        for path in folder.glob(f"*{cs.REFERENCE_ANSWERS_SUFFIX}")
    }


def _shown(path: Path) -> Path:
    return path.relative_to(PROJECT_ROOT) if path.is_relative_to(PROJECT_ROOT) else path


def compare(
    repo: ReferenceRepo,
    cases: Sequence[ReferenceCase],
    answers: dict[str, str],
    directory: Path,
) -> tuple[int, list[str]]:
    """Whether `answers` are the recorded ones, and the lines saying where not."""
    status = 0
    lines: list[str] = []
    for case in cases:
        path = answer_path(repo, case["name"], directory)
        if not path.is_file():
            status = cs.REFERENCE_EXIT_DIFFERENT
            lines.append(f"  {case['name']}: no recorded answer at {_shown(path)}")
            continue
        diff = difference(read_answer(path), answers[case["name"]])
        if diff:
            status = cs.REFERENCE_EXIT_DIFFERENT
            _report(lines, f"  {case['name']} differs from {_shown(path)}", diff)
    for name in sorted(recorded_names(repo, directory) - {c["name"] for c in cases}):
        status = cs.REFERENCE_EXIT_DIFFERENT
        lines.append(
            f"  {_shown(answer_path(repo, name, directory))} answers no case in "
            f"{_shown(cases_path(repo, directory))}"
        )
    return status, lines


def _answers(
    cgr: Sequence[str],
    repo: ReferenceRepo,
    cases: Sequence[ReferenceCase],
    checkout: Path,
    runs: int,
) -> tuple[dict[str, str], list[tuple[int, dict[str, str]]]]:
    """The first run's answers and, for each later run, its own."""
    first: dict[str, str] = {}
    later: list[tuple[int, dict[str, str]]] = []
    for seed, source in checkouts(repo, checkout, runs):
        with indexed(cgr, repo, source, seed) as done:
            answers = ask(cgr, repo, cases, source, done)
        if seed is None:
            first = answers
        else:
            later.append((seed, answers))
    return first, later


def ask_repo(
    command: str,
    repo: ReferenceRepo,
    cgr: Sequence[str],
    cache: Path,
    runs: int,
    directory: Path,
) -> RepoOutcome:
    cases = load_cases(repo, directory)
    lines: list[str] = []
    if not cases:
        lines.append(
            f"{repo['name']} ({repo['language']}): no cases at "
            f"{_shown(cases_path(repo, directory))}"
        )
        return RepoOutcome(lines, cs.REFERENCE_EXIT_DIFFERENT, {})
    checkout = fetch(repo, cache)
    first, later = _answers(cgr, repo, cases, checkout, runs)
    lines.append(f"{repo['name']} ({repo['language']}): {len(first)} answers")
    status = 0
    for seed, answers in later:
        for case in cases:
            diff = difference(first[case["name"]], answers[case["name"]])
            if diff:
                status = cs.REFERENCE_EXIT_DIFFERENT
                _report(
                    lines,
                    f"  {case['name']}: the run with PYTHONHASHSEED={seed} "
                    "answers otherwise",
                    diff,
                )
    if command == "check":
        found, differences = compare(repo, cases, first, directory)
        status = status or found
        lines.extend(differences)
    return RepoOutcome(lines, status, first)


def record(repo: ReferenceRepo, answers: dict[str, str], directory: Path) -> None:
    """Write `answers` and drop the recorded answers no case asks for."""
    for name in recorded_names(repo, directory) - set(answers):
        answer_path(repo, name, directory).unlink()
    for name, text in answers.items():
        write_answer(answer_path(repo, name, directory), text)


def run(
    command: str,
    repos: Sequence[ReferenceRepo],
    cgr: Sequence[str],
    cache: Path,
    runs: int,
    directory: Path = ANSWERS_DIR,
    *,
    jobs: int = 1,
    environment_directory: Path = REFERENCE_DIR,
) -> int:
    # Always compared, a full recording too: environment.txt is the graphs'
    # to write, and answers recorded with another install would not match it.
    _, proceed = environment_gate(
        command,
        cgr,
        environment_directory,
        True,
        what=cs.REFERENCE_ANSWERS_RECORDED,
        regenerate=cs.REFERENCE_ANSWERS_REGENERATE,
    )
    if not proceed:
        return cs.REFERENCE_EXIT_FAILED
    status = 0
    # `record` writes nothing until every repository has answered and every
    # run agreed, as the graphs' does.
    staged: list[tuple[ReferenceRepo, dict[str, str]]] = []
    # Each repository has a graph and a server of its own, so they run side
    # by side; inside one the cases run in their order, every run alike.
    with ThreadPoolExecutor(max_workers=max(jobs, 1)) as pool:
        outcomes = pool.map(
            lambda repo: ask_repo(command, repo, cgr, cache, runs, directory), repos
        )
        for repo, outcome in zip(repos, outcomes, strict=True):
            for line in outcome.lines:
                say(line)
            status = status or outcome.status
            staged.append((repo, outcome.answers))
    if command == "record":
        if status:
            say("Runs disagreed or a repository has no cases, so nothing was written.")
            return status
        for repo, answers in staged:
            record(repo, answers, directory)
    if command == "check" and status == cs.REFERENCE_EXIT_DIFFERENT:
        say(
            "If the change is intended, run "
            f"`{cs.REFERENCE_ANSWERS_REGENERATE}` and commit "
            f"{cs.REFERENCE_ANSWERS_DIR}."
        )
    return status


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("command", choices=("record", "check"))
    parser.add_argument("names", nargs="*", help="only these repositories")
    parser.add_argument(
        "--cgr", default=cs.REFERENCE_CGR_PROGRAM, help="the command that runs cgr"
    )
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument(
        "--jobs",
        type=int,
        default=min(cs.REFERENCE_ANSWERS_DEFAULT_JOBS, os.cpu_count() or 1),
        help="repositories asked at once",
    )
    args = parser.parse_args(argv)
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
        jobs=args.jobs,
    )


if __name__ == "__main__":
    sys.exit(main())
