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
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import NamedTuple

from loguru import logger

from ... import constants as cs
from ... import logs as ls
from ...config import settings
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


class GoSemanticFacts(NamedTuple):
    """Everything one gotypes run learned about the module."""

    call_sites: dict[CallSiteKey, GoCallSite]
    # Sites go/types resolved to a callee OUTSIDE the module (stdlib, deps): the
    # compiler proof the call leaves the repo, so the name-trie fallback must
    # not fabricate a first-party edge there.
    external_sites: set[CallSiteKey]


def _empty_facts() -> GoSemanticFacts:
    # A fresh instance per failure path: the maps are handed to mutable
    # processor state, so a shared constant would alias across runs.
    return GoSemanticFacts({}, set())


_GO = "go"
_TOOL_SRC = Path(__file__).parent / "gotypes"
_TOOL_SOURCES = ("main.go", "go.mod", "go.sum")
_BINARY_NAME = "gotypes"
_BUILD_LOCK = ".build-lock"
# Same mkdir-lock discipline as the C# frontend: build the binary ONCE, then
# parallel workers run it read-only and never race a shared build output.
_LOCK_TRIES = 600
_LOCK_POLL_SECONDS = 0.5
_RUN_TIMEOUT = 900
# GOTOOLCHAIN=local pins the installed toolchain (never a network upgrade);
# CGO off keeps the binary portable across the machines a shared cache may span.
# Deps are fetched from the module cache at build time (pinned by go.sum), the
# same restore-at-build posture as the C# frontend: an offline failure degrades
# to tree-sitter, never worse.
_GO_ENV = {"GOTOOLCHAIN": "local", "CGO_ENABLED": "0"}


def go_frontend_available() -> bool:
    return shutil.which(_GO) is not None


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
    return settings.CGR_HOME.expanduser() / "go_gotypes"


def _newest_source_mtime() -> float:
    # go.mod/go.sum pin the resolved deps, so the three tracked sources are the
    # complete build input; a bump to any of them re-triggers the compile.
    return max((_TOOL_SRC / name).stat().st_mtime for name in _TOOL_SOURCES)


def _binary_fresh(binary: Path) -> bool:
    return binary.is_file() and binary.stat().st_mtime >= _newest_source_mtime()


def _acquire_build_lock(lock: Path, binary: Path) -> bool:
    # Serialise the one build across parallel workers. Returns True holding the
    # lock (caller must rmdir); False if it gave up because another worker
    # already produced a fresh binary or the tries ran out.
    for _ in range(_LOCK_TRIES):
        try:
            lock.mkdir()
            return True
        except FileExistsError:
            time.sleep(_LOCK_POLL_SECONDS)
            if _binary_fresh(binary):
                return False
    return False


def _compile_tool(go: str, src: Path, out: Path) -> bool:
    # Build from a copy in the writable cache, never the bundled source dir,
    # which is read-only under a pip install (the build writes nothing there,
    # but a shared GOCACHE/module fetch wants a writable working tree).
    src.mkdir(parents=True, exist_ok=True)
    for name in _TOOL_SOURCES:
        shutil.copy2(_TOOL_SRC / name, src / name)
    out.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [go, "build", "-o", str(out / _BINARY_NAME), "."],
        cwd=src,
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, **_GO_ENV},
    )
    if proc.returncode != 0:
        logger.warning(ls.GO_FRONTEND_BUILD_FAILED.format(stderr=proc.stderr.strip()))
    return proc.returncode == 0


def _build_tool(go: str) -> Path | None:
    cache = _cache_dir()
    src = cache / "src"
    out = cache / "out"
    binary = out / _BINARY_NAME
    if _binary_fresh(binary):
        return binary
    cache.mkdir(parents=True, exist_ok=True)
    lock = cache / _BUILD_LOCK
    if not _acquire_build_lock(lock, binary):
        return binary if _binary_fresh(binary) else None
    try:
        if not _binary_fresh(binary) and not _compile_tool(go, src, out):
            return None
    finally:
        lock.rmdir()
    return binary if _binary_fresh(binary) else None


def _parse_payload(stdout: str, stderr: str = "") -> GoSemanticFacts:
    lines = [line for line in stdout.splitlines() if line.strip()]
    if not lines:
        # No output at all: the tool crashed before printing its JSON line.
        logger.error(ls.GO_FRONTEND_PARSE_FAILED.format(stdout=stdout, stderr=stderr))
        return _empty_facts()
    try:
        payload = json.loads(lines[-1])
    except json.JSONDecodeError:
        logger.error(ls.GO_FRONTEND_PARSE_FAILED.format(stdout=stdout, stderr=stderr))
        return _empty_facts()
    if not isinstance(payload, dict):
        # Well-formed JSON of the wrong shape (a list or scalar): treat as a
        # tool contract violation and fall back rather than crash indexing.
        logger.error(ls.GO_FRONTEND_PARSE_FAILED.format(stdout=stdout, stderr=stderr))
        return _empty_facts()
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
            },
        )
    except (KeyError, TypeError, ValueError, AttributeError):
        # A malformed fact entry (missing key, non-int position, non-object
        # row) must degrade to tree-sitter, never abort the whole index run.
        logger.error(ls.GO_FRONTEND_PARSE_FAILED.format(stdout=stdout, stderr=stderr))
        return _empty_facts()
    if not facts.call_sites and not facts.external_sites and stderr.strip():
        # A well-formed but entirely empty payload with diagnostics means the
        # load went wrong (build tag, unresolved import); surface the tool's
        # stderr instead of looking identical to a genuinely call-free module.
        logger.warning(ls.GO_FRONTEND_NO_FACTS.format(stderr=stderr.strip()))
    return facts


def run_go_frontend(repo_path: Path) -> GoSemanticFacts:
    go = shutil.which(_GO)
    if go is None:
        return _empty_facts()
    if find_go_module(repo_path) is None:
        return _empty_facts()
    binary = _build_tool(go)
    if binary is None:
        logger.warning(ls.GO_FRONTEND_BUILD_FAILED.format(stderr=""))
        return _empty_facts()
    try:
        proc = subprocess.run(
            [str(binary), str(repo_path)],
            capture_output=True,
            text=True,
            check=False,
            timeout=_RUN_TIMEOUT,
            env={
                **os.environ,
                **_GO_ENV,
                "CGR_IGNORE_DIRS": ",".join(sorted(cs.IGNORE_PATTERNS)),
            },
        )
    except (subprocess.SubprocessError, OSError) as error:
        logger.warning(ls.GO_FRONTEND_RUN_FAILED.format(error=error))
        return _empty_facts()
    return _parse_payload(proc.stdout, proc.stderr)
