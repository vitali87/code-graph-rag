#!/usr/bin/env python3
"""Refuse a pull request that removes a test without naming its replacement.

Rule 4 of the Rust rewrite (docs/reports/RUST_REWRITE_PLAN.md): no test
disappears silently. codebase_rag/tests/removed_tests.toml names the
replacement of every removed test, and `check` fails on a test the base has
and the head lacks with no entry, on an entry whose replacement the head lacks
(after following the entries of replacements removed in turn), on an entry
for a test still on the head, and on `obsolete` code still on the head or, for
a test removed here, never on the base.

Both trees are read from Git objects, never imported: the Python tests by an
`ast` walk of the files pytest collects, under each tree's own pytest
settings, the Rust tests as the `#[test]` functions under rust/. That takes
seconds, where collecting the suite means installing and importing it twice.
It sees test functions, not parameters, so a dropped parametrize case is left
to review. A renamed or moved test counts as removed.

Usage: python3 scripts/test_removals.py check --base <commit> [--head <rev>]
       [--pull-request <number>]
The base is fetched from origin when the clone lacks it. Exit 1 when the check
fails, 2 when it cannot run. Standard library only, so the lint job runs it
without syncing the environment. Design: docs/reports/PHASE1_SEAMS.md, section 4.
"""

# ruff: noqa: T201
from __future__ import annotations

import argparse
import ast
import fnmatch
import re
import subprocess
import sys
import tomllib
from collections.abc import Iterable, Iterator
from datetime import date, time
from enum import StrEnum
from pathlib import Path
from typing import NamedTuple

type TomlScalar = str | int | float | bool | date | time
type TomlValue = TomlScalar | list[TomlValue] | dict[str, TomlValue]

MAP_PATH = "codebase_rag/tests/removed_tests.toml"
PYPROJECT = "pyproject.toml"
RUST_ROOT = "rust/"
RUST_SUFFIX = ".rs"
REMOTE = "origin"
SEPARATOR = "::"
SYMLINK_MODE = b"120000"

# pytest's own defaults, for a tree whose pyproject.toml leaves a key unset.
DEFAULT_FILES = ("test_*.py", "*_test.py")
DEFAULT_CLASSES = ("Test",)
DEFAULT_FUNCTIONS = ("test",)
DEFAULT_NORECURSE = tuple(
    "*.egg .* _darcs build CVS dist node_modules venv {arch}".split()
)
GLOB_CHARS = frozenset("*?[")
# pytest will not collect a class with its own constructor.
CONSTRUCTORS = frozenset({"__init__", "__new__"})
FIXTURE = "fixture"

ENTRY_KEYS = ("test", "replaced_by", "obsolete", "reason", "pull_request")
TABLE = "removed"

RUST_TEST = re.compile(
    # `#[test]`, or a runtime's own, such as `#[tokio::test]`
    r"#\s*\[\s*(?:[A-Za-z_]\w*\s*::\s*)*test\s*\]"
    # then any other attributes, such as `#[should_panic(...)]`
    r"(?:\s*#\s*\[[^\]]*\])*"
    r"\s*(?:(?:pub(?:\s*\([^)]*\))?|const|async|unsafe)\s+)*"
    r"fn\s+(?:r#)?([A-Za-z_]\w*)"
)
RUST_RAW_STRING = re.compile(r'(?<![\w])[bc]?r(#*)"')
RUST_CHAR = re.compile(r"'(?:\\(?:x[0-9a-fA-F]{2}|u\{[0-9a-fA-F]{1,6}\}|.)|[^\\'\n])'")


class Rule(StrEnum):
    NO_ENTRY = "removed without an entry"
    MISSING_REPLACEMENT = "replacement not on the head"
    REPLACEMENT_LOOP = "replacements loop"
    STALE_ENTRY = "entry for a test on the head"
    OBSOLETE_PRESENT = "obsolete code still on the head"
    OBSOLETE_NOT_DELETED = "obsolete code not on the base"
    BAD_ENTRY = "malformed map"


MESSAGES = {
    Rule.MISSING_REPLACEMENT: (
        "The entry for {test} names a replacement the head does not have: "
        "{chain}. Name a test that exists, as file::Class::function without "
        "parameters; if {missing} was removed too, give it an entry of its own."
    ),
    Rule.REPLACEMENT_LOOP: (
        "The entry for {test} follows its replacements round a loop: {chain}. "
        "The chain must end at a test the head has."
    ),
    Rule.STALE_ENTRY: (
        "{test} is still on the head, so its entry in {map} is stale. Remove "
        "the entry; a test that is back needs none."
    ),
    Rule.OBSOLETE_PRESENT: (
        "The entry for {test} says the code it checked, {path}, is deleted, "
        "but the head still has it. Delete it in this pull request, or name "
        "the replacement with replaced_by."
    ),
    Rule.OBSOLETE_NOT_DELETED: (
        "The entry for {test} names {path} as the code deleted with it, but "
        "the base has no {path} either. Name what this pull request deletes."
    ),
}

ENTRY_TO_ADD = (
    "[[removed]]\n"
    'test = "{test}"\n'
    "replaced_by = [{replaced_by}]\n"
    'reason = "<what it checked, and how the replacement checks the same>"\n'
    "pull_request = {pull_request}"
)
NO_REPLACEMENT_YET = '"<file::Class::function of the test that checks it now>"'
NO_NUMBER_YET = "<this pull request's number>"
NO_ENTRY = (
    "{test} is on the base but not on the head, and {map} has no entry for "
    "it. A removed, renamed or moved test names the test that now checks what "
    "it checked. Add to {map}:\n\n{entry}\n\n"
    "If this pull request deletes the code it checked, write obsolete = "
    '"<the deleted file>" in place of replaced_by.'
)
BAD_TOML = "{path} is not valid TOML: {error}"
BAD_TABLE = "{map}: the map holds [[removed]] entries only, not {key!r}."
BAD_ENTRY = "{map}, entry {index}: {error}"
NOT_A_TABLE = "it is not a table"
UNKNOWN_KEY = "unknown key {key!r}; an entry has " + ", ".join(ENTRY_KEYS) + "."
BAD_TEST = "test must be a test id, file::Class::function without parameters."
BAD_REPLACED_BY = "replaced_by must list ids, file::Class::function, no parameters."
BAD_OBSOLETE = "obsolete must name the deleted file or directory."
BOTH_GIVEN = "give replaced_by or obsolete, not both."
NEITHER_GIVEN = "give replaced_by, or obsolete if the code it checked is deleted."
BAD_REASON = "reason must say why the test was removed."
BAD_NUMBER = "pull_request must be the number of the pull request."
DUPLICATE = "{test} has more than one entry."

UNPARSABLE = "{path} at {commit} does not parse, so its tests cannot be listed: {error}"
NO_COMMIT = "{rev} is not a commit here, and fetching it from {remote} failed."
GIT_FAILED = "git {command} failed: {error}"
BAD_REV = "{rev!r} is not a revision."
UNREADABLE_OBJECT = "git cat-file could not read an object: {header}"
CANNOT_RUN = "test_removals: {error}"
PASSED = (
    "No test removed without an entry: {base_tests} tests on the base, "
    "{head_tests} on the head, {entries} entries in {map}."
)
FAILED = (
    "{count} problem(s) with removed tests. Every test the base has and the "
    "head does not needs an entry in {map} (CONTRIBUTING.md, Removing a test)."
)


class Problem(NamedTuple):
    rule: Rule
    # The test the problem is about, or the map itself.
    subject: str
    message: str


class Entry(NamedTuple):
    test: str
    replaced_by: tuple[str, ...]
    obsolete: str | None


class Report(NamedTuple):
    problems: tuple[Problem, ...]
    base_tests: int
    head_tests: int
    entries: int


class PytestConfig(NamedTuple):
    testpaths: tuple[str, ...]
    python_files: tuple[str, ...]
    python_classes: tuple[str, ...]
    python_functions: tuple[str, ...]
    norecursedirs: tuple[str, ...]


class CheckError(Exception):
    pass


class _Repository:
    """A clone's Git objects, read without touching its working tree."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._blobs: dict[str, bytes] = {}
        self._listed: dict[tuple[str, str, PytestConfig], frozenset[str]] = {}

    def git(self, *args: str, stdin: bytes | None = None) -> bytes:
        result = subprocess.run(
            ["git", "-C", str(self.root), *args],
            input=stdin,
            capture_output=True,
            check=False,
        )
        if result.returncode:
            error = result.stderr.decode("utf-8", "replace").strip()
            raise CheckError(GIT_FAILED.format(command=" ".join(args), error=error))
        return result.stdout

    def commit(self, rev: str, fetch: bool = False) -> str:
        # Never let a value reach `git fetch` as an option.
        if rev.startswith("-"):
            raise CheckError(BAD_REV.format(rev=rev))
        try:
            out = self.git("rev-parse", "--verify", f"{rev}^{{commit}}")
            return out.decode().strip()
        except CheckError:
            if not fetch:
                raise
        # Depth 1 only where the clone is shallow already: in a full clone it
        # would make the fetched commit a shallow boundary.
        shallow = self.git("rev-parse", "--is-shallow-repository").strip() == b"true"
        depth = ["--depth=1"] if shallow else []
        try:
            self.git("fetch", "--quiet", "--no-tags", *depth, REMOTE, rev)
        except CheckError as error:
            raise CheckError(NO_COMMIT.format(rev=rev, remote=REMOTE)) from error
        return self.commit("FETCH_HEAD")

    def files(self, commit: str) -> dict[str, str]:
        files: dict[str, str] = {}
        listing = self.git("ls-tree", "-r", "-z", "--full-tree", commit)
        for record in listing.split(b"\0"):
            meta, _, path = record.partition(b"\t")
            if not path:
                continue
            mode, kind, blob = meta.split()
            if kind == b"blob" and mode != SYMLINK_MODE:
                files[path.decode("utf-8", "surrogateescape")] = blob.decode()
        return files

    def blobs(self, ids: Iterable[str]) -> dict[str, bytes]:
        if wanted := sorted(set(ids) - self._blobs.keys()):
            batch = "".join(f"{blob}\n" for blob in wanted).encode()
            out = self.git("cat-file", "--batch", stdin=batch)
            position = 0
            for blob in wanted:
                end = out.index(b"\n", position)
                header = out[position:end].decode("utf-8", "replace")
                if len(fields := header.split()) != 3:
                    raise CheckError(UNREADABLE_OBJECT.format(header=header))
                size = int(fields[2])
                self._blobs[blob] = out[end + 1 : end + 1 + size]
                position = end + 1 + size + 1
        return self._blobs

    def read(self, files: dict[str, str], path: str) -> bytes | None:
        blob = files.get(path)
        return None if blob is None else self.blobs([blob])[blob]

    def tests(self, commit: str, files: dict[str, str]) -> frozenset[str]:
        config = pytest_config(self.read(files, PYPROJECT))
        python = [path for path in files if collected(path, config)]
        rust = [p for p in files if p.startswith(RUST_ROOT) and p.endswith(RUST_SUFFIX)]
        blobs = self.blobs(files[path] for path in [*python, *rust])
        tests: set[str] = set()
        for path in python:
            key = (path, files[path], config)
            if key not in self._listed:
                try:
                    self._listed[key] = python_tests(path, blobs[files[path]], config)
                except (SyntaxError, ValueError) as error:
                    raise CheckError(
                        UNPARSABLE.format(path=path, commit=commit[:12], error=error)
                    ) from error
            tests |= self._listed[key]
        for path in rust:
            tests |= rust_tests(path, blobs[files[path]].decode("utf-8", "replace"))
        return frozenset(tests)


def _strings(value: TomlValue | None, default: tuple[str, ...]) -> tuple[str, ...]:
    # An ini-style value is one whitespace-separated string; TOML gives a list.
    if isinstance(value, str):
        return tuple(value.split())
    if isinstance(value, list):
        return tuple(str(item) for item in value)
    return default


def pytest_config(pyproject: bytes | None) -> PytestConfig:
    table: dict[str, TomlValue] = {}
    if pyproject is not None:
        try:
            data: dict[str, TomlValue] = tomllib.loads(pyproject.decode("utf-8"))
        except (tomllib.TOMLDecodeError, UnicodeDecodeError) as error:
            raise CheckError(BAD_TOML.format(path=PYPROJECT, error=error)) from error
        tool = data.get("tool")
        pytest_table = tool.get("pytest") if isinstance(tool, dict) else None
        if isinstance(pytest_table, dict):
            # pytest 9 reads `[tool.pytest]` itself; earlier ones only its
            # `ini_options` table.
            ini = pytest_table.get("ini_options")
            table = ini if isinstance(ini, dict) else pytest_table
    return PytestConfig(
        testpaths=tuple(
            path.removeprefix("./").strip("/")
            for path in _strings(table.get("testpaths"), ())
        ),
        python_files=_strings(table.get("python_files"), DEFAULT_FILES),
        python_classes=_strings(table.get("python_classes"), DEFAULT_CLASSES),
        python_functions=_strings(table.get("python_functions"), DEFAULT_FUNCTIONS),
        norecursedirs=_strings(table.get("norecursedirs"), DEFAULT_NORECURSE),
    )


def _matches_any(name: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in patterns)


def collected(path: str, config: PytestConfig) -> bool:
    """Whether pytest, run with no arguments, collects the file at `path`."""
    parts = path.split("/")
    for root in config.testpaths or ("",):
        if path == root:
            return path.endswith(".py")
        below = parts[len(root.split("/")) if root else 0 : -1]
        if (root and not path.startswith(f"{root}/")) or any(
            _matches_any(directory, config.norecursedirs) for directory in below
        ):
            continue
        # As pytest matches them: a pattern with a slash against the path.
        if any(
            _matches_any(
                path if "/" in pattern else parts[-1], (pattern, f"*/{pattern}")
            )
            for pattern in config.python_files
        ):
            return True
    return False


def _named(name: str, options: tuple[str, ...]) -> bool:
    # pytest's rule: a prefix, or a glob when the option holds a glob character.
    return any(
        name.startswith(option)
        or (bool(GLOB_CHARS & set(option)) and fnmatch.fnmatchcase(name, option))
        for option in options
    )


def _statements(body: list[ast.stmt]) -> Iterator[ast.stmt]:
    # A definition under `if`, `try`, `with` or a loop still lands in the
    # namespace pytest reads. Every branch is listed, the same on both trees.
    for node in body:
        match node:
            case ast.If() | ast.For() | ast.While():
                yield from _statements(node.body)
                yield from _statements(node.orelse)
            case ast.Try() | ast.TryStar():
                yield from _statements(node.body)
                for handler in node.handlers:
                    yield from _statements(handler.body)
                yield from _statements(node.orelse)
                yield from _statements(node.finalbody)
            case ast.With() | ast.AsyncWith():
                yield from _statements(node.body)
            case _:
                yield node


def _is_fixture(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    for decorator in node.decorator_list:
        match decorator.func if isinstance(decorator, ast.Call) else decorator:
            case ast.Name(id=name) | ast.Attribute(attr=name) if name == FIXTURE:
                return True
    return False


def _members(
    cls: ast.ClassDef, classes: dict[str, ast.ClassDef], seen: frozenset[str]
) -> dict[str, ast.stmt]:
    # pytest collects inherited test methods too. Bases defined in the same
    # module are followed; one imported from elsewhere cannot be read here.
    members: dict[str, ast.stmt] = {}
    for base in cls.bases:
        if isinstance(base, ast.Name) and base.id in classes and base.id not in seen:
            inherited = _members(classes[base.id], classes, seen | {base.id})
            for name, member in inherited.items():
                members.setdefault(name, member)
    for node in _statements(cls.body):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            members[node.name] = node
    return members


def _collect(
    prefix: str,
    node: ast.stmt,
    classes: dict[str, ast.ClassDef],
    config: PytestConfig,
) -> set[str]:
    match node:
        case ast.FunctionDef() | ast.AsyncFunctionDef() if _named(
            node.name, config.python_functions
        ) and not _is_fixture(node):
            return {f"{prefix}{SEPARATOR}{node.name}"}
        case ast.ClassDef() if _named(node.name, config.python_classes):
            members = _members(node, classes, frozenset({node.name}))
            functions = ast.FunctionDef | ast.AsyncFunctionDef
            if any(isinstance(members.get(name), functions) for name in CONSTRUCTORS):
                return set()
            inner = f"{prefix}{SEPARATOR}{node.name}"
            return set().union(
                *(_collect(inner, m, classes, config) for m in members.values())
            )
    return set()


def python_tests(path: str, source: bytes, config: PytestConfig) -> frozenset[str]:
    """The ids of the tests pytest collects from one file, without parameters."""
    module = ast.parse(source, filename=path)
    statements = list(_statements(module.body))
    classes = {node.name: node for node in statements if isinstance(node, ast.ClassDef)}
    return frozenset().union(*(_collect(path, n, classes, config) for n in statements))


def _rust_code(text: str) -> str:
    """`text` with comments and the insides of string and char literals blanked."""
    out: list[str] = []
    position, size = 0, len(text)
    while position < size:
        if text.startswith("//", position):
            end = text.find("\n", position)
            position = size if end < 0 else end
            continue
        if text.startswith("/*", position):
            depth, position = 1, position + 2
            while position < size and depth:
                if text.startswith("/*", position):
                    depth, position = depth + 1, position + 2
                elif text.startswith("*/", position):
                    depth, position = depth - 1, position + 2
                else:
                    position += 1
            out.append(" ")
            continue
        if raw := RUST_RAW_STRING.match(text, position):
            end = text.find('"' + raw.group(1), raw.end())
            position = size if end < 0 else end + 1 + len(raw.group(1))
            out.append('""')
            continue
        if text[position] == '"':
            position += 1
            while position < size and text[position] != '"':
                position += 2 if text[position] == "\\" else 1
            position += 1
            out.append('""')
            continue
        if char := RUST_CHAR.match(text, position):
            position = char.end()
            out.append("''")
            continue
        out.append(text[position])
        position += 1
    return "".join(out)


def rust_tests(path: str, text: str) -> frozenset[str]:
    """The ids of the `#[test]` functions in one Rust file."""
    return frozenset(
        f"{path}{SEPARATOR}{match.group(1)}"
        for match in RUST_TEST.finditer(_rust_code(text))
    )


def _is_test_id(value: TomlValue | None) -> bool:
    return (
        isinstance(value, str)
        and SEPARATOR in value
        and "[" not in value
        and value == value.strip()
        and all(value.split(SEPARATOR))
    )


def _entry(raw: TomlValue) -> tuple[Entry | None, list[str]]:
    if not isinstance(raw, dict):
        return None, [NOT_A_TABLE]
    errors = [UNKNOWN_KEY.format(key=key) for key in raw if key not in ENTRY_KEYS]
    reason, number = raw.get("reason"), raw.get("pull_request")
    if not isinstance(reason, str) or not reason.strip():
        errors.append(BAD_REASON)
    if not isinstance(number, int) or isinstance(number, bool) or number < 1:
        errors.append(BAD_NUMBER)
    replacements: tuple[str, ...] = ()
    gone: str | None = None
    match raw.get("replaced_by"), raw.get("obsolete"):
        case None, None:
            errors.append(NEITHER_GIVEN)
        case list() as names, None if names and all(map(_is_test_id, names)):
            replacements = tuple(str(name) for name in names)
        case _, None:
            errors.append(BAD_REPLACED_BY)
        case None, str() as path if path.strip().strip("/"):
            gone = path.strip().strip("/")
        case None, _:
            errors.append(BAD_OBSOLETE)
        case _:
            errors.append(BOTH_GIVEN)
    test = raw.get("test")
    if not isinstance(test, str) or not _is_test_id(test):
        return None, [BAD_TEST, *errors]
    return Entry(test, replacements, gone), errors


def read_map(text: bytes | None) -> tuple[list[Entry], list[Problem]]:
    """The map's entries, and what is wrong with it. No file is no entries."""
    if text is None:
        return [], []
    try:
        data: dict[str, TomlValue] = tomllib.loads(text.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as error:
        return [], [
            Problem(
                Rule.BAD_ENTRY, MAP_PATH, BAD_TOML.format(path=MAP_PATH, error=error)
            )
        ]
    problems = [
        Problem(Rule.BAD_ENTRY, MAP_PATH, BAD_TABLE.format(map=MAP_PATH, key=key))
        for key in data
        if key != TABLE
    ]
    raw_entries = data.get(TABLE, [])
    if not isinstance(raw_entries, list):
        problems.append(
            Problem(Rule.BAD_ENTRY, MAP_PATH, BAD_TABLE.format(map=MAP_PATH, key=TABLE))
        )
        return [], problems
    entries: dict[str, Entry] = {}
    for index, raw in enumerate(raw_entries, 1):
        entry, errors = _entry(raw)
        subject = entry.test if entry else MAP_PATH
        if entry and entry.test in entries:
            errors.append(DUPLICATE.format(test=entry.test))
        elif entry:
            entries[entry.test] = entry
        problems += [
            Problem(
                Rule.BAD_ENTRY,
                subject,
                BAD_ENTRY.format(map=MAP_PATH, index=index, error=error),
            )
            for error in errors
        ]
    return list(entries.values()), problems


def _present(files: dict[str, str], path: str) -> bool:
    return path in files or any(name.startswith(f"{path}/") for name in files)


def _dead_end(
    test: str,
    entries: dict[str, Entry],
    head: frozenset[str],
    chain: tuple[str, ...],
) -> tuple[Rule, tuple[str, ...]] | None:
    """The first chain of replacements from `test` that reaches no test."""
    chain = (*chain, test)
    if test in head:
        return None
    if test in chain[:-1]:
        return Rule.REPLACEMENT_LOOP, chain
    if (entry := entries.get(test)) is None:
        return Rule.MISSING_REPLACEMENT, chain
    for replacement in entry.replaced_by:
        if found := _dead_end(replacement, entries, head, chain):
            return found
    # An `obsolete` entry ends its chain: the code it checked is gone.
    return None


def _no_entry(test: str, added: frozenset[str], pull_request: int | None) -> str:
    # A moved test keeps its function's name: suggest the added tests that
    # have it, as the likely new ids.
    name = test.rsplit(SEPARATOR, 1)[-1]
    moved = sorted(t for t in added if t.rsplit(SEPARATOR, 1)[-1] == name)
    entry = ENTRY_TO_ADD.format(
        test=test,
        replaced_by=", ".join(f'"{t}"' for t in moved) or NO_REPLACEMENT_YET,
        pull_request=NO_NUMBER_YET if pull_request is None else pull_request,
    )
    return NO_ENTRY.format(test=test, map=MAP_PATH, entry=entry)


def _entry_problems(
    entry: Entry,
    entries: dict[str, Entry],
    removed: frozenset[str],
    base_files: dict[str, str],
    head_files: dict[str, str],
    head_tests: frozenset[str],
) -> Iterator[Problem]:
    found: list[tuple[Rule, tuple[str, ...]]] = []
    if entry.test in head_tests:
        found.append((Rule.STALE_ENTRY, ()))
    if entry.obsolete is not None:
        if _present(head_files, entry.obsolete):
            found.append((Rule.OBSOLETE_PRESENT, ()))
        elif entry.test in removed and not _present(base_files, entry.obsolete):
            found.append((Rule.OBSOLETE_NOT_DELETED, ()))
    # One broken chain per entry is enough to act on.
    chains = (
        _dead_end(r, entries, head_tests, (entry.test,)) for r in entry.replaced_by
    )
    if broken := next(filter(None, chains), None):
        found.append(broken)
    for rule, chain in found:
        message = MESSAGES[rule].format(
            test=entry.test,
            map=MAP_PATH,
            path=entry.obsolete,
            chain=" -> ".join(chain),
            missing=chain[-1] if chain else "",
        )
        yield Problem(rule, entry.test, message)


def check(
    root: Path, base: str, head: str = "HEAD", pull_request: int | None = None
) -> Report:
    """Compare the tests of `head` with those of `base`, against `head`'s map."""
    repository = _Repository(root)
    base_commit = repository.commit(base, fetch=True)
    head_commit = repository.commit(head)
    base_files = repository.files(base_commit)
    head_files = repository.files(head_commit)
    base_tests = repository.tests(base_commit, base_files)
    head_tests = repository.tests(head_commit, head_files)
    entries, map_problems = read_map(repository.read(head_files, MAP_PATH))
    by_test = {entry.test: entry for entry in entries}
    removed = base_tests - head_tests
    added = head_tests - base_tests
    problems = [
        Problem(Rule.NO_ENTRY, test, _no_entry(test, added, pull_request))
        for test in sorted(removed - by_test.keys())
    ]
    problems += map_problems
    for entry in entries:
        problems += _entry_problems(
            entry, by_test, removed, base_files, head_files, head_tests
        )
    return Report(tuple(problems), len(base_tests), len(head_tests), len(entries))


def list_tests(root: Path, rev: str) -> frozenset[str]:
    """Every test id at `rev`, as `check` lists it."""
    repository = _Repository(root)
    commit = repository.commit(rev)
    return repository.tests(commit, repository.files(commit))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Refuse a removed test without an entry in " + MAP_PATH
    )
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("check", help="compare a head's tests with its base's")
    run.add_argument(
        "--base", required=True, help="the commit to compare with; fetched if missing"
    )
    run.add_argument("--head", default="HEAD", help="the commit to check")
    run.add_argument(
        "--pull-request",
        type=int,
        help="the pull request's number, written into the entries it suggests",
    )
    args = parser.parse_args(argv)
    try:
        report = check(Path.cwd(), args.base, args.head, args.pull_request)
    except CheckError as error:
        print(CANNOT_RUN.format(error=error), file=sys.stderr)
        return 2
    if not report.problems:
        print(PASSED.format(**report._asdict(), map=MAP_PATH))
        return 0
    print(FAILED.format(count=len(report.problems), map=MAP_PATH))
    for problem in report.problems:
        print(f"\n{problem.message}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
