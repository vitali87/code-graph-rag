"""Which Go files one build compiles together (issue #2483).

A package may declare a name once per build: `i_linux.go` and
`i_windows.go` each declaring `I`, or two files under `//go:build linux`
and `//go:build !linux`. No build holds both, so neither is a redeclaration
and neither is the package's `I` in every build. Two declarations whose
files one build can compile together are a redeclaration that build
rejects, so the name is ambiguous there.

A file's constraint is its name's `_GOOS`, `_GOARCH` or `_GOOS_GOARCH`
suffix and its `//go:build` line (or, without one, its `// +build` lines),
as `go/build` reads them. Files build together when some GOOS, GOARCH and
set of other tags satisfies all their constraints. A tag this cannot place
(`cgo`, `go1.21`, a custom tag) may be either, so two files it cannot tell
apart are taken to build together.
"""

from __future__ import annotations

import functools
import itertools
import re
from collections.abc import Callable, Iterable
from pathlib import Path, PurePosixPath
from typing import NamedTuple

from ... import constants as cs

# go/build's syslist: the GOOS and GOARCH values a file name or tag names.
KNOWN_OS = frozenset(
    {
        "aix",
        "android",
        "darwin",
        "dragonfly",
        "freebsd",
        "hurd",
        "illumos",
        "ios",
        "js",
        "linux",
        "nacl",
        "netbsd",
        "openbsd",
        "plan9",
        "solaris",
        "wasip1",
        "windows",
        "zos",
    }
)
UNIX_OS = frozenset(
    {
        "aix",
        "android",
        "darwin",
        "dragonfly",
        "freebsd",
        "hurd",
        "illumos",
        "ios",
        "linux",
        "netbsd",
        "openbsd",
        "solaris",
    }
)
KNOWN_ARCH = frozenset(
    {
        "386",
        "amd64",
        "amd64p32",
        "arm",
        "armbe",
        "arm64",
        "arm64be",
        "loong64",
        "mips",
        "mipsle",
        "mips64",
        "mips64le",
        "mips64p32",
        "mips64p32le",
        "ppc",
        "ppc64",
        "ppc64le",
        "riscv",
        "riscv64",
        "s390",
        "s390x",
        "sparc",
        "sparc64",
        "wasm",
    }
)
# GOOS=android builds `linux` files and tags too, illumos `solaris`, ios
# `darwin`.
_IMPLIED_OS = {"android": "linux", "illumos": "solaris", "ios": "darwin"}
_UNIX_TAG = "unix"
# A file's head holds its constraints; a generated file can be megabytes.
_HEADER_SCAN_BYTES = 64 * 1024
# Beyond this many tags of their own (not a GOOS or GOARCH), each both asked
# for and ruled out, constraints are not enumerated: whether they hold
# together is unknown.
_MAX_FREE_TAGS = 10
# A constraint nested deeper than this (`!` and parentheses), or longer than
# this many tokens, is not read: it is UNKNOWN. Go's own reader rejects such
# expressions too.
_MAX_DEPTH = 100
_MAX_TOKENS = 1000
_GO_BUILD_PREFIX = "//go:build"
_PLUS_BUILD_PREFIX = "// +build"
_TOKEN = re.compile(r"\s*(\(|\)|!|&&|\|\||[A-Za-z0-9_.]+)")
_TAG_NAME = re.compile(r"[A-Za-z0-9_.]+")


class Expr(NamedTuple):
    """A constraint: a tag (`op` "tag", `name` set), its negation, or an
    all-of ("and") or any-of ("or") of `parts`. An empty all-of holds in
    every build."""

    op: str
    name: str = ""
    parts: tuple[Expr, ...] = ()


ALWAYS = Expr("and")
NEVER = Expr("or")
# A constraint this cannot read (malformed, nested or long past what it
# reads, cut off by the head it scans): neither true nor false in any build.
# Every decision resting on it is unknown, and an unknown decision links
# nothing and calls nothing exact.
UNKNOWN = Expr("unknown")


def _tag(name: str) -> Expr:
    return Expr("tag", name)


def all_of(parts: Iterable[Expr]) -> Expr:
    """Every one of `parts`."""
    kept = tuple(dict.fromkeys(part for part in parts if part != ALWAYS))
    if NEVER in kept:
        return NEVER
    if UNKNOWN in kept:
        return UNKNOWN
    return kept[0] if len(kept) == 1 else Expr("and", parts=kept)


def any_of(parts: Iterable[Expr]) -> Expr:
    """One of `parts`."""
    kept = tuple(dict.fromkeys(part for part in parts if part != NEVER))
    if ALWAYS in kept:
        return ALWAYS
    if UNKNOWN in kept:
        return UNKNOWN
    return kept[0] if len(kept) == 1 else Expr("or", parts=kept)


def negate(part: Expr) -> Expr:
    if part == ALWAYS:
        return NEVER
    if part == NEVER:
        return ALWAYS
    if part == UNKNOWN:
        return UNKNOWN
    return Expr("not", parts=(part,))


_all = all_of
_any = any_of
_not = negate


def name_constraint(rel_path: str) -> Expr:
    """What a file's name asks of the build, as `go/build`'s goodOSArchFile
    reads it: everything before the first `_` is free, a `_test` suffix is
    dropped, and a trailing GOOS, GOARCH or GOOS_GOARCH constrains."""
    stem = PurePosixPath(rel_path).name.removesuffix(cs.EXT_GO)
    underscore = stem.find("_")
    if underscore < 0:
        return ALWAYS
    parts = stem[underscore:].split("_")
    if parts[-1] == "test":
        parts = parts[:-1]
    if len(parts) >= 2 and parts[-2] in KNOWN_OS and parts[-1] in KNOWN_ARCH:
        return _all((_tag(parts[-2]), _tag(parts[-1])))
    if parts and (parts[-1] in KNOWN_OS or parts[-1] in KNOWN_ARCH):
        return _tag(parts[-1])
    return ALWAYS


def header_constraint(text: str, truncated: bool = False) -> Expr:
    """The `//go:build` expression in a file's head, or its `// +build`
    lines without one. A constraint this cannot read is UNKNOWN, and so is
    the head of a file cut off (`truncated`) before its first code line:
    its constraint may lie past what was read."""
    go_build: str | None = None
    plus_lines: list[str] = []
    pending: list[str] = []
    reached_code = False
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            plus_lines.extend(pending)
            pending.clear()
            continue
        if not line.startswith("//"):
            reached_code = True
            break
        if go_build is None and _is_directive(line, _GO_BUILD_PREFIX):
            go_build = line[len(_GO_BUILD_PREFIX) :]
        elif _is_directive(line, _PLUS_BUILD_PREFIX):
            pending.append(line[len(_PLUS_BUILD_PREFIX) :])
    if truncated and not reached_code:
        return UNKNOWN
    if go_build is not None:
        return _parse_go_build(go_build)
    return _all(_plus_build(line) for line in plus_lines)


def _is_directive(line: str, prefix: str) -> bool:
    return line.startswith(prefix) and line[len(prefix) : len(prefix) + 1] in (
        " ",
        "\t",
    )


def _plus_build(line: str) -> Expr:
    # `a,!b c`: space-separated options any of which holds, each a
    # comma-separated all-of of tags or negated tags. A term that is no tag
    # makes the line UNKNOWN.
    terms = [term.split(",") for term in line.split()]
    if any(
        not _TAG_NAME.fullmatch(term.removeprefix("!"))
        for option in terms
        for term in option
    ):
        return UNKNOWN
    options = [
        _all(
            _not(_tag(term[1:])) if term.startswith("!") else _tag(term)
            for term in option
        )
        for option in terms
    ]
    return _any(options) if options else ALWAYS


def _parse_go_build(text: str) -> Expr:
    tokens: list[str] = []
    position = 0
    stripped = text.rstrip()
    while position < len(stripped):
        match = _TOKEN.match(stripped, position)
        if match is None or len(tokens) >= _MAX_TOKENS:
            return UNKNOWN
        tokens.append(match.group(1))
        position = match.end()
    parser = _ExprParser(tokens)
    try:
        parsed = parser.parse_or()
    except _TooDeepError:
        return UNKNOWN
    return parsed if parsed is not None and parser.done() else UNKNOWN


class _TooDeepError(Exception):
    """A constraint nested past `_MAX_DEPTH`."""


class _ExprParser:
    """`//go:build` grammar: `||` over `&&` over `!`, parentheses, tags."""

    def __init__(self, tokens: list[str]) -> None:
        self.tokens = tokens
        self.index = 0
        self.depth = 0

    def done(self) -> bool:
        return self.index == len(self.tokens)

    def _peek(self) -> str | None:
        return self.tokens[self.index] if self.index < len(self.tokens) else None

    def parse_or(self) -> Expr | None:
        return self._chain("||", self.parse_and, _any)

    def parse_and(self) -> Expr | None:
        return self._chain("&&", self.parse_not, _all)

    def _chain(
        self,
        operator: str,
        operand: Callable[[], Expr | None],
        combine: Callable[[Iterable[Expr]], Expr],
    ) -> Expr | None:
        first = operand()
        if first is None:
            return None
        parts = [first]
        while self._peek() == operator:
            self.index += 1
            if (part := operand()) is None:
                return None
            parts.append(part)
        return parts[0] if len(parts) == 1 else combine(parts)

    def parse_not(self) -> Expr | None:
        token = self._peek()
        if token is None:
            return None
        self.index += 1
        if token not in ("!", "("):
            return None if token in (")", "&&", "||") else _tag(token)
        self.depth += 1
        if self.depth > _MAX_DEPTH:
            raise _TooDeepError
        inner = self.parse_not() if token == "!" else self._parse_group()
        self.depth -= 1
        if inner is None:
            return None
        return _not(inner) if token == "!" else inner

    def _parse_group(self) -> Expr | None:
        inner = self.parse_or()
        if inner is None or self._peek() != ")":
            return None
        self.index += 1
        return inner


def _polarities(
    expr: Expr, positive: bool, into: dict[str, set[bool]]
) -> dict[str, set[bool]]:
    # Each tag, and whether it is asked for (True), ruled out (False) or both.
    if expr.op == "tag":
        into.setdefault(expr.name, set()).add(positive)
    for part in expr.parts:
        _polarities(part, positive != (expr.op == "not"), into)
    return into


class _Build(NamedTuple):
    goos: str
    goarch: str
    tags: frozenset[str]

    def has(self, tag: str) -> bool:
        return (
            tag in (self.goos, self.goarch)
            or (tag == _UNIX_TAG and self.goos in UNIX_OS)
            or _IMPLIED_OS.get(self.goos) == tag
            or tag in self.tags
        )


def _holds(expr: Expr, build: _Build) -> bool:
    if expr.op == "tag":
        return build.has(expr.name)
    if expr.op == "not":
        return not _holds(expr.parts[0], build)
    if expr.op == "and":
        return all(_holds(part, build) for part in expr.parts)
    return any(_holds(part, build) for part in expr.parts)


def decide(exprs: Iterable[Expr]) -> bool | None:
    """Whether one build satisfies every constraint in `exprs`: True or
    False when this can tell, None when it cannot (an UNKNOWN constraint,
    or more tags of their own than it enumerates)."""
    combined = _all(exprs)
    if combined == UNKNOWN:
        return None
    if combined in (ALWAYS, NEVER):
        return combined == ALWAYS
    return _decide(combined)


def possible(exprs: Iterable[Expr]) -> bool:
    """Whether one build certainly satisfies every one of `exprs`: what a
    link, an override or a call edge needs. Unknown is no."""
    return decide(exprs) is True


def impossible(exprs: Iterable[Expr]) -> bool:
    """Whether no build certainly satisfies them all: what telling two
    declarations apart as build variants needs. Unknown is no."""
    return decide(exprs) is False


def holds_wherever(scope: Expr, claim: Expr) -> bool:
    """Whether every build satisfying `scope` certainly satisfies `claim`."""
    return impossible((scope, negate(claim)))


@functools.lru_cache(maxsize=4096)
def _decide(combined: Expr) -> bool | None:
    polarities = _polarities(combined, True, {})
    tags = set(polarities)
    os_bound = tags & (KNOWN_OS | {_UNIX_TAG})
    arch_bound = tags & KNOWN_ARCH
    own = tags - KNOWN_OS - KNOWN_ARCH - {_UNIX_TAG}
    # A tag of its own only ever asked for is best set, one only ever ruled
    # out best unset: either way no other part fails for it. Only a tag
    # both asked for and ruled out is enumerated.
    asked = frozenset(tag for tag in own if polarities[tag] == {True})
    free = sorted(tag for tag in own if len(polarities[tag]) > 1)
    if len(free) > _MAX_FREE_TAGS:
        return None
    oses = sorted(KNOWN_OS) if os_bound else ["linux"]
    arches = sorted(KNOWN_ARCH) if arch_bound else ["amd64"]
    subsets = [
        asked | frozenset(chosen)
        for size in range(len(free) + 1)
        for chosen in itertools.combinations(free, size)
    ]
    return any(
        _holds(combined, _Build(goos, goarch, chosen))
        for goos in oses
        for goarch in arches
        for chosen in subsets
    )


class GoBuilds:
    """Each Go file's build constraint, read from its name and its head on
    disk (a file cgr parses is the one on disk), re-read when the file
    changes, and which files one build can compile together."""

    def __init__(self, repo_path: Path) -> None:
        self.repo_path = repo_path
        self._constraints: dict[str, tuple[tuple[int, int] | None, Expr]] = {}
        self._decided: dict[frozenset[Expr], bool | None] = {}

    def constraint(self, rel_path: str) -> Expr:
        if not rel_path:
            return ALWAYS
        path = self.repo_path / rel_path
        try:
            stat = path.stat()
            stamp: tuple[int, int] | None = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            stamp = None
        cached = self._constraints.get(rel_path)
        if cached is not None and cached[0] == stamp:
            return cached[1]
        constraint = _all((name_constraint(rel_path), _safe_header(path)))
        self._constraints[rel_path] = (stamp, constraint)
        return constraint

    def _decide(self, rel_paths: Iterable[str]) -> bool | None:
        key = frozenset(self.constraint(path) for path in rel_paths)
        if key not in self._decided:
            self._decided[key] = decide(key)
        return self._decided[key]

    def together(self, rel_paths: Iterable[str]) -> bool:
        """Whether one build certainly compiles every file in `rel_paths`."""
        return self._decide(rel_paths) is True

    def apart(self, rel_paths: Iterable[str]) -> bool:
        """Whether no build certainly compiles them all."""
        return self._decide(rel_paths) is False

    def covered(self, rel_path: str, others: Iterable[str]) -> bool:
        """Whether every build compiling `rel_path` compiles one of
        `others`."""
        return holds_wherever(
            self.constraint(rel_path), _any(self.constraint(path) for path in others)
        )

    def everywhere(self, rel_path: str, needed: Iterable[str]) -> bool:
        """Whether every build compiling `rel_path` compiles all of
        `needed`."""
        return holds_wherever(
            self.constraint(rel_path), _all(self.constraint(path) for path in needed)
        )

    def variants(
        self, candidates: Iterable[tuple[str, str]], rel_path: str
    ) -> tuple[str, ...] | None:
        """Of same-named package-level declarations, as (qn, declaring
        file), the ones a build compiling `rel_path` may hold, sorted: each
        in a file not certainly apart from it. None when two of them may
        build with it together, a redeclaration that build rejects."""
        usable = sorted(
            (qn, path) for qn, path in candidates if not self.apart((rel_path, path))
        )
        for (_, first), (_, second) in itertools.combinations(usable, 2):
            if not self.apart((rel_path, first, second)):
                return None
        return tuple(qn for qn, _ in usable)


def _safe_header(path: Path) -> Expr:
    # Whatever a file's head holds, reading its constraint never stops an
    # index: a constraint this cannot read is UNKNOWN.
    try:
        head = _read_head(path)
        return header_constraint(
            head.decode(cs.ENCODING_UTF8, errors="replace"),
            truncated=len(head) >= _HEADER_SCAN_BYTES,
        )
    except Exception:  # noqa: BLE001 - any failure is an unread constraint
        return UNKNOWN


def _read_head(path: Path) -> bytes:
    try:
        with path.open("rb") as handle:
            return handle.read(_HEADER_SCAN_BYTES)
    except OSError:
        return b""
