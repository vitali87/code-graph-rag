# Go semantic frontend for cgr's hybrid mode (issue #1179). Runs a bundled Go
# tool (`gotypes/`) that loads the target module via go/packages + go/types and
# emits location-keyed call facts the tree-sitter name trie cannot derive: the
# exact first-party callee of every call expression (embedded-struct method
# promotion and scope shadowing resolved by the real type rules), and the
# sites whose callee resolves OUTSIDE the module (stdlib, deps) so the fallback
# must not fabricate a first-party edge there. Every join key that misses falls
# back to the heuristics, and no go toolchain, no go.mod, or a build failure all
# leave the facts empty, so indexing stays pure tree-sitter.
from __future__ import annotations

import json
import math
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import time
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import NamedTuple

from loguru import logger

from ... import constants as cs
from ... import logs as ls
from ...config import settings
from ...utils.path_utils import (
    is_regular_file,
    is_special_file_entry,
    is_symlink_entry,
)
from ..build_lock import artifact_fresh, build_cached_artifact, newest_source_mtime
from ..frontend_mode import resolve_frontend_mode
from ..go.module_paths import discover_go_module_paths

# Call-site join key: (rel_file, name_token_line, name_token_byte_col, simple_name).
# The NAME token, not the call-expression start: nested calls (`make().Handle(x)`
# wrap `make()`) share a start position, but their name tokens never collide.
# Columns are 0-based BYTE offsets on both sides, matching tree-sitter.
CallSiteKey = tuple[str, int, int, str]


class GoCallSite(NamedTuple):
    """Resolved target of one call: the first-party declaration cgr ingested."""

    name: str
    target_file: str
    target_line: int
    target_col: int


class GoImplements(NamedTuple):
    """A types.Implements-proven IMPLEMENTS edge: the implementer and the
    interface each as the declaring identifier's (file, line, col). A pure
    position-join -- Go reuses simple type names across packages, so the
    consumer resolves both ends by location, never by name."""

    impl_file: str
    impl_line: int
    impl_col: int
    iface_file: str
    iface_line: int
    iface_col: int


class GoSemanticFacts(NamedTuple):
    """Everything one gotypes run learned about the module."""

    call_sites: dict[CallSiteKey, GoCallSite]
    # Sites go/types resolved to a callee OUTSIDE the module (stdlib, deps): the
    # compiler proof the call leaves the repo, so the name-trie fallback must
    # not fabricate a first-party edge there.
    external_sites: set[CallSiteKey]
    # First-party concrete-type -> first-party interface pairs the compiler
    # proved. With them loaded they are the only Go IMPLEMENTS edges; without
    # them, the tree-sitter path matches method names and arities instead
    # (issue #2483), since there is no syntactic base list to read.
    implements: list[GoImplements]
    # Module anchors whose tool run FAILED (timeout, crash, unparseable
    # output). A failed anchor drops only its own module to the heuristics,
    # which is deliberate -- one wedged build must not blind a ten-module repo
    # -- but the degradation has to be recorded or it is invisible: a module
    # the tool never analysed is otherwise indistinguishable from one it
    # analysed and found nothing in, and the graph ends up compiler-accurate
    # for some modules and heuristic for others with nothing saying which
    # (issue #1462). Empty on a fully successful run, so a reader can trust it.
    #
    # A TUPLE, not a list, and that is why it can carry a default at all: a
    # NamedTuple default is one shared object, so a mutable `[]` would alias
    # across every run that omitted it -- the aliasing `_empty_facts` exists to
    # avoid. An empty tuple is immutable and safe to share.
    degraded_modules: tuple[str, ...] = ()
    # Module anchors whose tool run SUCCEEDED, spelled as `degraded_modules`
    # is. Within one, the implements pairs above are the whole answer, even
    # an empty one; the name-and-arity fallback keeps to the rest (#2483).
    analysed_modules: tuple[str, ...] = ()
    # The repo-relative files those runs type-checked. A run sees only the
    # files the current build selects, so a file for another GOOS or build
    # tag, or one of a package that failed to type-check, was never compared:
    # the pairs above are the answer only where every file a pair rests on
    # is here (PR #3360 review).
    checked_files: frozenset[str] = frozenset()
    # The declarations (file, line, col) of the types and interfaces the
    # runs compared. A checked file can declare one they skip, a generic
    # type or interface, and no pair says anything of it.
    compared_types: frozenset[tuple[str, int, int]] = frozenset()


def _empty_facts() -> GoSemanticFacts:
    # A fresh instance per failure path: the maps are handed to mutable
    # processor state, so a shared constant would alias across runs.
    return GoSemanticFacts({}, set(), [])


_GO = "go"
_TOOL_SRC = Path(__file__).parent / "gotypes"
_TOOL_SOURCES = ("main.go", "go.mod", "go.sum")
_BINARY_NAME = "gotypes"
_BUILD_LOCK = ".build-lock"
_CACHE_DIR_PREFIX = "go_gotypes"
_UNKNOWN_FLOOR = "unknown"
# Written when a build fails, as JSON holding the `go env GOVERSION` it failed
# with and when it failed: a failed build is not retried every sync, only once
# Go or the helper's sources change or GO_FRONTEND_BUILD_FAILURE_TTL_S passes
# (issue #2395). The time is stored in the file, not read from its mtime, which
# copying or restoring the cache can reset (PR #2417 review).
_BUILD_FAILED_MARKER = ".build-failed"
_MARKER_GO_VERSION = "go_version"
_MARKER_FAILED_AT = "failed_at"
_SECONDS_PER_MINUTE = 60
_GO_MOD = "go.mod"
_GO_DIRECTIVE = re.compile(r"^go\s+(\d+(?:\.\d+)*)\s*$", re.MULTILINE)
_GO_VERSION = re.compile(r"^go(\d+(?:\.\d+)*)$")
_VERSION_PROBE_TIMEOUT = 30
# Same mkdir-lock discipline as the C# frontend: build the binary ONCE, then
# parallel workers run it read-only and never race a shared build output.
_LOCK_TRIES = 600
_LOCK_POLL_SECONDS = 0.5
# `go build` may fetch modules, so it is bounded well above a warm rebuild but
# far below the run timeout: a wedged build must degrade to the tree-sitter
# backbone rather than block indexing (issue #1462). Matches the Java
# frontend's _BUILD_TIMEOUT, which had this from the start.
_BUILD_TIMEOUT = 300
_RUN_TIMEOUT = 900
# GOTOOLCHAIN=local pins the installed toolchain (never a network upgrade);
# CGO off keeps the binary portable across the machines a shared cache may span.
# Deps are fetched from the module cache at build time (pinned by go.sum), the
# same restore-at-build posture as the C# frontend: an offline failure degrades
# to tree-sitter, never worse.
_GO_ENV = {"GOTOOLCHAIN": "local", "CGO_ENABLED": "0"}
# Where a binary runs, which the helper's own build must leave to the host.
_TARGET_ENV = frozenset({"GOOS", "GOARCH"})
# The `go build -overlay` file the helper hands its loads, named in this
# variable, and the overlay's one field: each path it maps to "" is a file
# the build sees as absent.
_OVERLAY_ENV = "CGR_GO_OVERLAY"
_OVERLAY_FILE = "overlay.json"
_OVERLAY_REPLACE = "Replace"


def go_frontend_available() -> bool:
    return shutil.which(_GO) is not None


def resolve_go_frontend() -> cs.GoFrontend:
    # The single source of truth for the EFFECTIVE frontend: without a go
    # toolchain every gotypes-backed mode (AUTO, and an explicit GOTYPES, which
    # the graph build degrades to tree-sitter with a warning) resolves to
    # TREESITTER; with one, AUTO means GOTYPES.
    return resolve_frontend_mode(
        settings.GO_FRONTEND,
        cs.GoFrontend.TREESITTER,
        go_frontend_available,
        auto=cs.GoFrontend.AUTO,
        auto_resolves_to=cs.GoFrontend.GOTYPES,
    )


def find_go_module(repo_path: Path) -> Path | None:
    # The root-most module's go.mod (shortest anchor path), or None when the
    # repo has no non-ignored go.mod. Applicability only needs its presence;
    # the path is used for logging.
    mappings = discover_go_module_paths(repo_path)
    if not mappings:
        return None
    anchor = min(
        (anchor_dir for _, _, anchor_dir in mappings), key=lambda p: len(str(p))
    )
    return anchor / cs.DEP_FILE_GOMOD


def _cache_dir() -> Path:
    # Keyed by the Go the helper requires. Freshness compares only mtimes, so
    # a helper an older Go built while the floor was lower would otherwise be
    # reused after the floor rose, before `_too_old` ever looks at the Go
    # (PR #2417 review). Each floor gets a cache directory of its own.
    required = _required_go_version()
    key = _dotted(required) if required is not None else _UNKNOWN_FLOOR
    return settings.CGR_HOME.expanduser() / f"{_CACHE_DIR_PREFIX}-{key}"


def _newest_source_mtime() -> float:
    # go.mod/go.sum pin the resolved deps, so the three tracked sources are the
    # complete build input; a bump to any of them re-triggers the compile.
    return newest_source_mtime(_TOOL_SRC, _TOOL_SOURCES)


def _compile_tool(go: str, src: Path, out: Path) -> bool | None:
    """True when the helper built, False when `go build` failed, and None when
    it timed out, which says nothing about whether the helper builds."""
    # Build from a copy in the writable cache, never the bundled source dir,
    # which is read-only under a pip install (the build writes nothing there,
    # but a shared GOCACHE/module fetch wants a writable working tree).
    src.mkdir(parents=True, exist_ok=True)
    for name in _TOOL_SOURCES:
        shutil.copy2(_TOOL_SRC / name, src / name)
    out.mkdir(parents=True, exist_ok=True)
    try:
        proc = subprocess.run(
            [go, "build", "-o", str(out / _BINARY_NAME), "."],
            cwd=src,
            capture_output=True,
            text=True,
            encoding=cs.ENCODING_UTF8,
            check=False,
            timeout=_BUILD_TIMEOUT,
            env=_host_build_env(),
        )
    except subprocess.TimeoutExpired:
        # A wedged build degrades exactly like a failed one: the caller falls
        # back to tree-sitter. Raising here would turn a hung toolchain into a
        # failed index, which is the opposite of the protocol's invariant.
        logger.warning(
            ls.GO_FRONTEND_BUILD_FAILED.format(
                stderr=f"timed out after {_BUILD_TIMEOUT}s"
            )
        )
        return None
    if proc.returncode != 0:
        logger.warning(ls.GO_FRONTEND_BUILD_FAILED.format(stderr=proc.stderr.strip()))
    return proc.returncode == 0


def _host_build_env() -> dict[str, str]:
    """The environment the helper is built in: the host's GOOS and GOARCH.

    A GOOS or GOARCH set for the analysis (`GOOS=windows cgr ...` on Linux)
    picks the build go/types reads, and the helper's run honours it; built
    under it, the helper was a Windows binary this host cannot run, and the
    index fell back to tree-sitter (#3360 review)."""
    env = {key: value for key, value in os.environ.items() if key not in _TARGET_ENV}
    return {**env, **_GO_ENV}


def _dotted(version: tuple[int, ...]) -> str:
    return ".".join(str(part) for part in version)


def _required_go_version() -> tuple[int, ...] | None:
    """The helper module's `go` directive: the oldest Go that builds it."""
    try:
        text = (_TOOL_SRC / _GO_MOD).read_text(encoding=cs.ENCODING_UTF8)
    except OSError:
        return None
    match = _GO_DIRECTIVE.search(text)
    return tuple(int(part) for part in match.group(1).split(".")) if match else None


def _toolchain_version(go: str) -> str | None:
    """`go env GOVERSION` under the same env the build uses, or None."""
    try:
        proc = subprocess.run(
            [go, "env", "GOVERSION"],
            capture_output=True,
            text=True,
            encoding=cs.ENCODING_UTF8,
            errors="replace",
            check=False,
            timeout=_VERSION_PROBE_TIMEOUT,
            env={**os.environ, **_GO_ENV},
        )
    except (subprocess.SubprocessError, OSError):
        return None
    version = (proc.stdout or "").strip()
    return version if proc.returncode == 0 and version else None


def _too_old(version: str | None) -> tuple[int, ...] | None:
    """The version the helper needs when `version` is older than it, else None.

    A devel build or an unreadable answer is not evidence of an old Go, so it
    goes on to the build, which is the check that decides.
    """
    required = _required_go_version()
    match = _GO_VERSION.match(version or "")
    if required is None or match is None:
        return None
    found = tuple(int(part) for part in match.group(1).split("."))
    return required if found < required else None


def _failed_at(marker: Path, version: str | None) -> float | None:
    """When the build failed with this Go, or None without a readable marker
    for it."""
    try:
        payload = json.loads(marker.read_text(encoding=cs.ENCODING_UTF8))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get(_MARKER_GO_VERSION) != (version or ""):
        return None
    failed_at = payload.get(_MARKER_FAILED_AT)
    # bool is an int subclass, but a `true` in the marker holds no time.
    if isinstance(failed_at, bool) or not isinstance(failed_at, int | float):
        return None
    return float(failed_at)


def _failure_retry_in(marker: Path, version: str | None) -> float | None:
    """Seconds until a remembered failure with this Go is retried, or None to
    build now."""
    failed_at = _failed_at(marker, version)
    try:
        if failed_at is None or failed_at < _newest_source_mtime():
            return None
    except OSError:
        return None
    age = time.time() - failed_at
    # A failure dated after now means the clock moved back since, and holding
    # it would keep the build off until the clock caught up, plus the TTL.
    if not 0 <= age < cs.GO_FRONTEND_BUILD_FAILURE_TTL_S:
        return None
    return cs.GO_FRONTEND_BUILD_FAILURE_TTL_S - age


def _remember_failure(marker: Path, version: str | None) -> None:
    marker.write_text(
        json.dumps({_MARKER_GO_VERSION: version or "", _MARKER_FAILED_AT: time.time()}),
        encoding=cs.ENCODING_UTF8,
    )


def _skip_for_recent_failure(marker: Path, version: str | None) -> bool:
    """True, after one warning, while a failed build with this Go is
    remembered and not yet due for a retry."""
    retry_in = _failure_retry_in(marker, version)
    if retry_in is None:
        return False
    logger.warning(
        ls.GO_FRONTEND_BUILD_FAILED_EARLIER.format(
            version=version,
            minutes=math.ceil(retry_in / _SECONDS_PER_MINUTE),
            marker=marker,
        )
    )
    return True


def _compile_and_remember(
    go: str, src: Path, out: Path, marker: Path, version: str | None
) -> bool:
    # Runs under the build lock. A worker that passed `_build_tool`'s marker
    # check while another worker's build held the lock checks again here:
    # that build may just have failed and dated the marker, and building
    # anyway would cost every queued sync one more failing build (PR #2417
    # review). A build that succeeded meanwhile never gets here, because
    # build_cached_artifact rechecks the binary's freshness under the lock.
    if _skip_for_recent_failure(marker, version):
        return False
    built = _compile_tool(go, src, out)
    try:
        if built:
            marker.unlink(missing_ok=True)
        elif built is False:
            _remember_failure(marker, version)
    except OSError:
        pass
    return bool(built)


def _build_tool(go: str) -> Path | None:
    """The helper binary, or None after logging exactly one warning.

    The version floor is checked before a build so an old Go is named plainly
    instead of surfacing as `go build`'s stderr, and a failed build is not
    retried on every sync (issue #2395). It is checked before a cached helper
    is reused too: the helper drives the `go` on PATH to load packages, and a
    version manager can put an older Go there after a newer one built it, so
    a cached build must not let a Go below the floor run (PR #3410 review).
    """
    cache = _cache_dir()
    src = cache / "src"
    out = cache / "out"
    binary = out / _BINARY_NAME
    version = _toolchain_version(go)
    if (required := _too_old(version)) is not None:
        logger.warning(
            ls.GO_FRONTEND_TOOLCHAIN_TOO_OLD.format(
                required=_dotted(required), found=(version or "").removeprefix(_GO)
            )
        )
        return None
    if artifact_fresh(binary, _newest_source_mtime):
        return binary
    marker = cache / _BUILD_FAILED_MARKER
    # The fast path: a remembered failure skips the lock, and any wait on it,
    # entirely. `_compile_and_remember` checks again once the lock is held.
    if _skip_for_recent_failure(marker, version):
        return None
    return build_cached_artifact(
        cache,
        binary,
        lambda: artifact_fresh(binary, _newest_source_mtime),
        lambda: _compile_and_remember(go, src, out, marker, version),
        _BUILD_LOCK,
        _LOCK_TRIES,
        _LOCK_POLL_SECONDS,
    )


def _in_repo(
    module: object, directory: object, in_repo_modules: Mapping[str, frozenset[Path]]
) -> bool:
    # The callee's own module, matched exactly: `example.com/root/plugin`
    # can be a separate dependency under the in-repo `example.com/root`.
    if not isinstance(module, str) or module not in in_repo_modules:
        return False
    # And loaded from that module's directory here: `appb` can require a
    # published `example.com/liba`, which Go loads from the module cache
    # although the repository holds a copy. A payload that names no
    # directory (an older tool) goes by the module path alone.
    if directory is None:
        return True
    return (
        isinstance(directory, str)
        and Path(directory).resolve() in in_repo_modules[module]
    )


def _parse_payload(
    stdout: str,
    stderr: str = "",
    in_repo_modules: Mapping[str, frozenset[Path]] | None = None,
) -> GoSemanticFacts | None:
    """Facts from one tool run, or None when the OUTPUT was unusable.

    The tool analyses one module and calls everything outside it external,
    a sibling module of the same repository included. An external site
    whose callee's module is one of `in_repo_modules` (module path -> its
    resolved in-repo directories) is dropped, so the call resolves as
    first-party instead of being suppressed (issue #2809).

    None rather than empty facts for every failure path below: empty facts is
    the correct answer for a module the tool analysed and found nothing in, so
    reusing it for a contract violation makes the two indistinguishable and the
    degradation invisible (issue #1462).
    """
    in_repo = in_repo_modules or {}
    lines = [line for line in stdout.splitlines() if line.strip()]
    if not lines:
        # No output at all: the tool crashed before printing its JSON line.
        logger.error(ls.GO_FRONTEND_PARSE_FAILED.format(stdout=stdout, stderr=stderr))
        return None
    try:
        payload = json.loads(lines[-1])
    except json.JSONDecodeError:
        logger.error(ls.GO_FRONTEND_PARSE_FAILED.format(stdout=stdout, stderr=stderr))
        return None
    if not isinstance(payload, dict):
        # Well-formed JSON of the wrong shape (a list or scalar): treat as a
        # tool contract violation and fall back rather than crash indexing.
        logger.error(ls.GO_FRONTEND_PARSE_FAILED.format(stdout=stdout, stderr=stderr))
        return None
    try:
        facts = GoSemanticFacts(
            call_sites={
                (
                    site["file"],
                    int(site["line"]),
                    int(site["col"]),
                    site["name"],
                ): GoCallSite(
                    site["name"], site["tfile"], int(site["tline"]), int(site["tcol"])
                )
                for site in payload.get("calls", [])
            },
            external_sites={
                (site["file"], int(site["line"]), int(site["col"]), site["name"])
                for site in payload.get("externals", [])
                if not _in_repo(site.get("module"), site.get("dir"), in_repo)
            },
            implements=[
                GoImplements(
                    pair["file"],
                    int(pair["line"]),
                    int(pair["col"]),
                    pair["ifile"],
                    int(pair["iline"]),
                    int(pair["icol"]),
                )
                for pair in payload.get("implements", [])
            ],
            checked_files=frozenset(
                _checked_file(path) for path in payload.get("checked", [])
            ),
            compared_types=frozenset(
                (str(_checked_file(fact["file"])), int(fact["line"]), int(fact["col"]))
                for fact in payload.get("compared", [])
            ),
        )
    except (KeyError, TypeError, ValueError, AttributeError):
        # A malformed fact entry (missing key, non-int position, non-object
        # row) must degrade to tree-sitter, never abort the whole index run.
        logger.error(ls.GO_FRONTEND_PARSE_FAILED.format(stdout=stdout, stderr=stderr))
        return None
    if (
        not facts.call_sites
        and not facts.external_sites
        and not facts.implements
        and stderr.strip()
    ):
        # A well-formed but entirely empty payload with diagnostics means the
        # load went wrong (build tag, unresolved import); surface the tool's
        # stderr instead of looking identical to a genuinely call-free module.
        logger.warning(ls.GO_FRONTEND_NO_FACTS.format(stderr=stderr.strip()))
    return facts


def _module_anchors(repo_path: Path) -> list[Path]:
    # One tool invocation per module root: `./...` never descends into a
    # nested go.mod (its own module boundary), so a single root run would
    # silently miss every nested module's local calls (issue #1227).
    anchors = {anchor for _, _, anchor in discover_go_module_paths(repo_path)}
    return sorted(anchors)


def _in_repo_modules(repo_path: Path) -> dict[str, frozenset[Path]]:
    # A module path can head several go.mod files (a dependency-pinning stub
    # beside the real tree, issue #941), so each maps to all its directories.
    dirs: dict[str, set[Path]] = {}
    for module, _, anchor in discover_go_module_paths(repo_path):
        dirs.setdefault(module, set()).add(anchor.resolve())
    return {module: frozenset(found) for module, found in dirs.items()}


def _checked_file(path: object) -> str:
    if not isinstance(path, str):
        raise TypeError(path)
    return path


def _prefix_facts(facts: GoSemanticFacts, prefix: str) -> GoSemanticFacts:
    if not prefix:
        return facts

    def _rel(path: str) -> str:
        return f"{prefix}/{path}"

    return GoSemanticFacts(
        call_sites={
            (_rel(file), line, col, name): GoCallSite(
                site.name, _rel(site.target_file), site.target_line, site.target_col
            )
            for (file, line, col, name), site in facts.call_sites.items()
        },
        external_sites={
            (_rel(file), line, col, name)
            for file, line, col, name in facts.external_sites
        },
        implements=[
            GoImplements(
                _rel(pair.impl_file),
                pair.impl_line,
                pair.impl_col,
                _rel(pair.iface_file),
                pair.iface_line,
                pair.iface_col,
            )
            for pair in facts.implements
        ],
        checked_files=frozenset(_rel(path) for path in facts.checked_files),
        compared_types=frozenset(
            (_rel(file), line, col) for file, line, col in facts.compared_types
        ),
    )


def _special_files(repo_path: Path) -> list[str]:
    """Every pipe, socket or device in the repository, or link to one.

    The helper's loads open each source file of every package they reach,
    and opening a named pipe blocks until something writes to it: one
    `zz.go` pipe hung the run (issue #3736). Every folder is listed, those
    cgr ignores too, because Go loads them all the same: `./...` reaches
    `build/`, and a `replace` reaches a sibling module's folders. Only an
    entry the directory listing does not already show to be a regular file
    or a folder costs a `stat`. The paths are absolute, as Go reads an
    overlay's relative ones from the folder each load runs in.
    """
    found, _complete = _special_entries(os.path.abspath(repo_path))
    return sorted(found)


def _special_entries(root: str, limit: int | None = None) -> tuple[list[str], bool]:
    """The pipes, sockets and devices under `root`, and links to one, each
    spelled from `root`, and whether the listing ended before `limit`
    entries. Links are never followed, so no listing loops; a folder that
    cannot be listed is passed over."""
    found: list[str] = []
    seen = 0
    pending = [root]
    while pending:
        try:
            with os.scandir(pending.pop()) as entries:
                for entry in entries:
                    seen += 1
                    if limit is not None and seen > limit:
                        return found, False
                    if entry.is_dir(follow_symlinks=False):
                        pending.append(entry.path)
                    elif _is_special(entry):
                        found.append(entry.path)
        except OSError:
            continue
    return found, True


def _is_special(entry: os.DirEntry[str]) -> bool:
    """Whether a listed entry that is no folder is a pipe, socket or device,
    or a link to one. One the listing shows to be a regular file costs no
    `stat`."""
    if entry.is_file(follow_symlinks=False):
        return False
    path = entry.path
    return is_special_file_entry(path) or (
        is_symlink_entry(path) and is_special_file_entry(os.path.realpath(path))
    )


# One entry of what `go mod edit -json` or `go work edit -json` prints: a
# `Use` entry's fields, or a `Replace` entry's `Old` and `New` modules.
_EditEntry = dict[str, str | dict[str, str]]
_EditView = dict[str, list[_EditEntry]]


def _go_output(go: str, args: list[str], folder: str | None = None) -> str | None:
    """What `go` prints for `args`, run in `folder` when one is given, or
    None after a debug line when it fails."""
    command = [go, *args]
    env = {**os.environ, **_GO_ENV}
    if folder is not None:
        env[cs.GO_ENV_PWD] = folder
    try:
        proc = subprocess.run(
            command,
            cwd=folder,
            capture_output=True,
            text=True,
            encoding=cs.ENCODING_UTF8,
            errors="replace",
            check=False,
            timeout=cs.GO_QUERY_TIMEOUT_S,
            env=env,
        )
    except (subprocess.SubprocessError, OSError) as error:
        logger.debug(
            ls.GO_FRONTEND_QUERY_FAILED.format(command=shlex.join(command), error=error)
        )
        return None
    if proc.returncode != 0:
        logger.debug(
            ls.GO_FRONTEND_QUERY_FAILED.format(
                command=shlex.join(command), error=proc.stderr.strip()
            )
        )
        return None
    return proc.stdout


def _edit_view(go: str, args: tuple[str, ...], file: Path) -> _EditView:
    """`file` as `go mod edit -json` or `go work edit -json` prints it, or
    empty when it cannot be read."""
    if not is_regular_file(file):
        return {}
    output = _go_output(go, [*args, str(file)])
    try:
        view = json.loads(output) if output is not None else {}
    except ValueError:
        return {}
    return view if isinstance(view, dict) else {}


def _entries(view: _EditView, key: str) -> list[_EditEntry]:
    found = view.get(key)
    if not isinstance(found, list):
        return []
    return [entry for entry in found if isinstance(entry, dict)]


def _local_replace_paths(view: _EditView) -> list[str]:
    """The folder each `replace` in `view` names. A target that carries a
    version is a module Go fetches, not a folder."""
    paths: list[str] = []
    for entry in _entries(view, cs.GO_MOD_JSON_REPLACE):
        new = entry.get(cs.GO_MOD_JSON_NEW)
        if isinstance(new, dict) and not new.get(cs.GO_MOD_JSON_VERSION):
            paths.append(new.get(cs.GO_MOD_JSON_PATH, ""))
    return [path for path in paths if isinstance(path, str) and path]


def _use_paths(view: _EditView) -> list[str]:
    paths = [
        entry.get(cs.GO_WORK_JSON_DISK_PATH)
        for entry in _entries(view, cs.GO_WORK_JSON_USE)
    ]
    return [path for path in paths if isinstance(path, str) and path]


def _joined(folder: str, paths: list[str]) -> list[str]:
    """`paths` resolved against `folder` as Go resolves them: joined and
    cleaned by their spelling, not through the links on the way."""
    return [os.path.normpath(os.path.join(folder, path)) for path in paths]


def _mentions(path: Path, word: str) -> bool:
    try:
        return is_regular_file(path) and word in path.read_text(
            encoding=cs.ENCODING_UTF8
        )
    except (OSError, ValueError):
        return False


def _local_replace_targets(go: str, module: str) -> list[str]:
    """The folder each local `replace` in the go.mod of `module` names,
    joined to `module`.

    Read by `go mod edit -json`, Go's own parser, so a quoted path or a
    `replace ( ... )` block reads as the loads read it. A go.mod that does
    not mention `replace` costs no `go` run.
    """
    gomod = Path(module) / cs.DEP_FILE_GOMOD
    if not _mentions(gomod, cs.GO_KEYWORD_REPLACE):
        return []
    view = _edit_view(go, cs.GO_MOD_EDIT_JSON_ARGS, gomod)
    return _joined(module, _local_replace_paths(view))


def _workfile_above(folder: str) -> bool:
    """Whether `folder` or one above it holds an entry named go.work."""
    while not os.path.lexists(os.path.join(folder, cs.GO_WORK_FILE)):
        parent = os.path.dirname(folder)
        if parent == folder:
            return False
        folder = parent
    return True


def _active_workfile(go: str, module: str) -> str | None:
    """The go.work the helper's loads in `module` use, or None.

    `go env GOWORK`, run there with the helper's environment, answers as
    the loads choose. It is not run when GOWORK is `off`, nor when GOWORK
    is unset and no folder above holds a go.work, the usual case, where Go
    finds none either; a GOWORK kept in Go's own settings file by `go env
    -w` is then not looked for.
    """
    chosen = os.environ.get(cs.GO_ENV_GOWORK, "")
    if chosen == cs.GO_WORK_OFF:
        return None
    if chosen in cs.GO_WORK_SEARCH and not _workfile_above(module):
        return None
    output = _go_output(go, list(cs.GO_ENV_GOWORK_ARGS), module) or ""
    workfile = output.strip()
    return workfile if workfile and workfile != cs.GO_WORK_OFF else None


def _workspace_folders(go: str, workfile: str) -> tuple[list[str], list[str]]:
    """The modules a go.work uses and the folders its own `replace` names,
    each joined to the go.work's folder."""
    view = _edit_view(go, cs.GO_WORK_EDIT_JSON_ARGS, Path(workfile))
    base = os.path.dirname(workfile)
    return _joined(base, _use_paths(view)), _joined(base, _local_replace_paths(view))


def _loaded_folders(go: str, anchors: list[Path]) -> list[str]:
    """Each folder a local `replace` or the active workspace has the
    helper's loads read, spelled as Go spells it.

    Go applies the `replace` of each main module: the anchor's go.mod, and
    in a workspace the go.mod of each module it uses and the go.work's own.
    It loads the workspace's modules too, wherever they are. A go.work can
    sit above the repository or be named by GOWORK, so each anchor's is
    asked for (PR #3777 review); each is read once.
    """
    modules = [os.path.abspath(anchor) for anchor in anchors]
    workfiles = {
        workfile: None
        for module in modules
        if (workfile := _active_workfile(go, module)) is not None
    }
    folders: list[str] = []
    for workfile in workfiles:
        used, replaced = _workspace_folders(go, workfile)
        modules.extend(used)
        folders.extend(used + replaced)
    for module in dict.fromkeys(modules):
        folders.extend(_local_replace_targets(go, module))
    return folders


def _listed_with_repository(repo: str, target: str) -> bool:
    """Whether the repository's own listing already spells each entry of
    `target` as Go opens it: true of a folder inside it that no link leads
    to on the way down."""
    try:
        if os.path.commonpath([repo, target]) != repo:
            return False
    except ValueError:
        # Paths on different drives share no folder.
        return False
    current = repo
    for part in Path(os.path.relpath(target, repo)).parts:
        current = os.path.join(current, part)
        if is_symlink_entry(current):
            return False
    return True


def _bounded_special_entries(folder: str) -> list[str]:
    entries, complete = _special_entries(folder, cs.GO_REPLACE_TARGET_MAX_ENTRIES)
    if not complete:
        logger.warning(
            ls.GO_FRONTEND_REPLACE_LISTING_CUT.format(
                target=folder, limit=cs.GO_REPLACE_TARGET_MAX_ENTRIES
            )
        )
    return entries


def _reached_special_files(go: str, repo_path: Path, anchors: list[Path]) -> list[str]:
    """The pipes, sockets and devices Go opens under a spelling the
    repository's own listing never gives (issue #3744).

    A load reads a replaced or used module from the folder named for it,
    joined as `_loaded_folders` joins it, and an overlay matches paths by
    spelling. A folder outside the repository is never listed, and one a
    link leads to is listed by its real path, if at all. Each such folder is
    listed from the spelling Go joins, so every entry found is spelled as
    Go opens it. Such a folder can be anywhere, `/` included, so its
    listing is bounded; links below it are not followed, as in the
    repository's.
    """
    repo = os.path.abspath(repo_path)
    found: list[str] = []
    for folder in dict.fromkeys(_loaded_folders(go, anchors)):
        if not _listed_with_repository(repo, folder):
            found.extend(_bounded_special_entries(folder))
    return found


@contextmanager
def _overlay_without(paths: list[str]) -> Generator[Path | None]:
    """An overlay file that leaves `paths` out of the helper's build, or None
    when there are none, which leaves the build as it was."""
    if not paths:
        yield None
        return
    with tempfile.TemporaryDirectory() as tmp:
        overlay = Path(tmp) / _OVERLAY_FILE
        overlay.write_text(
            json.dumps({_OVERLAY_REPLACE: dict.fromkeys(paths, "")}),
            encoding=cs.ENCODING_UTF8,
        )
        yield overlay


def _run_tool_once(
    binary: Path,
    module_root: Path,
    ignored_dirs: frozenset[str],
    in_repo_modules: Mapping[str, frozenset[Path]] | None = None,
    overlay: Path | None = None,
) -> GoSemanticFacts | None:
    """Facts for one module anchor, or None when the tool run FAILED.

    None rather than empty facts, because they are different facts and the
    caller must tell them apart: a module the tool analysed and found nothing
    in yields empty facts and is fine, while a timed-out or crashed run means
    that module was never analysed at all (issue #1462).
    """
    env = {
        **os.environ,
        **_GO_ENV,
        "CGR_IGNORE_DIRS": ",".join(sorted(ignored_dirs)),
    }
    if overlay is not None:
        env[_OVERLAY_ENV] = str(overlay)
    try:
        proc = subprocess.run(
            [str(binary), str(module_root)],
            capture_output=True,
            text=True,
            encoding=cs.ENCODING_UTF8,
            check=False,
            timeout=_RUN_TIMEOUT,
            env=env,
        )
    except (subprocess.SubprocessError, OSError) as error:
        logger.warning(ls.GO_FRONTEND_RUN_FAILED.format(error=error))
        return None
    if proc.returncode != 0:
        # The tool's own exit code was never consulted, so a crashed run whose
        # stdout happened to parse -- or was simply empty -- looked exactly
        # like a module with nothing to report (issue #1462).
        logger.warning(
            ls.GO_FRONTEND_RUN_FAILED.format(error=f"exit {proc.returncode}")
        )
        return None
    return _parse_payload(proc.stdout, proc.stderr, in_repo_modules)


def run_go_frontend(
    repo_path: Path, ignored_dirs: frozenset[str] = cs.IGNORE_PATTERNS
) -> GoSemanticFacts:
    go = shutil.which(_GO)
    if go is None:
        return _empty_facts()
    anchors = _module_anchors(repo_path)
    if not anchors:
        return _empty_facts()
    binary = _build_tool(go)
    if binary is None:
        # `_build_tool` has already said why, with the build's own stderr; a
        # second warning here repeated it with an empty one (issue #2395).
        return _empty_facts()
    merged = _empty_facts()
    degraded: list[str] = []
    analysed: list[str] = []
    checked: set[str] = set()
    compared: set[tuple[str, int, int]] = set()
    in_repo_modules = _in_repo_modules(repo_path)
    special = {
        *_special_files(repo_path),
        *_reached_special_files(go, repo_path, anchors),
    }
    with _overlay_without(sorted(special)) as overlay:
        for anchor in anchors:
            prefix = (
                "" if anchor == repo_path else anchor.relative_to(repo_path).as_posix()
            )
            raw = _run_tool_once(binary, anchor, ignored_dirs, in_repo_modules, overlay)
            if raw is None:
                # This module drops to the heuristics, and the others keep
                # their compiler facts -- one wedged build must not blind a
                # ten-module repo. Recording it is what stops that
                # degradation being invisible (issue #1462).
                degraded.append(prefix or ".")
                logger.warning(ls.GO_FRONTEND_ANCHOR_DEGRADED.format(anchor=anchor))
                continue
            analysed.append(prefix or ".")
            facts = _prefix_facts(raw, prefix)
            merged.call_sites.update(facts.call_sites)
            merged.external_sites.update(facts.external_sites)
            merged.implements.extend(facts.implements)
            checked.update(facts.checked_files)
            compared.update(facts.compared_types)
    return merged._replace(
        degraded_modules=tuple(degraded),
        analysed_modules=tuple(analysed),
        checked_files=frozenset(checked),
        compared_types=frozenset(compared),
    )
