"""Bundled javac fact provider for Java (issue #1181).

Java's resolution is the largest pure-heuristic stack in the repo: it matches
by name and arity where the language resolves overloads BY ARGUMENT TYPE. The
JDK ships the authoritative answer in the Compiler Tree API, so a small
bundled tool parses and ATTRIBUTES the repo's sources and emits the two
standard fact families.

Stage 1 attributes without the project classpath: intra-repo binding is still
exact, and a symbol that resolves outside repo source becomes an external
proof. An unattributed site (a genuinely missing dependency) emits nothing at
all, leaving the heuristics in charge -- a miss is never mistaken for a proof.
Any missing piece (no JDK, a build failure, a crash) degrades to pure
tree-sitter, the standing frontend invariant.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import NamedTuple

from loguru import logger

from ... import constants as cs
from ... import logs as ls
from ...config import settings
from ..build_lock import acquire_build_lock, release_build_lock
from ..frontends.protocol import CallSiteKey

_TOOL_SRC = Path(__file__).parent / "javac"
_TOOL_SOURCE = "cgr/Frontend.java"
_MAIN_CLASS = "cgr.Frontend"
_CLASS_FILE = "cgr/Frontend.class"
_BUILD_LOCK = ".build-lock"
_STAGING_DIR = "staging"
_LOCK_TRIES = 600
_LOCK_POLL_SECONDS = 0.5
_BUILD_TIMEOUT = 120
_RUN_TIMEOUT = 900
_PROBE_TIMEOUT = 10.0


class JavaCallSite(NamedTuple):
    """The declaration a call binds to, per javac's attribution."""

    name: str
    target_file: str
    target_line: int
    target_col: int


class JavaSemanticFacts(NamedTuple):
    """Everything one javac run learned about the repo."""

    call_sites: dict[CallSiteKey, JavaCallSite]
    external_sites: set[CallSiteKey]


def _empty_facts() -> JavaSemanticFacts:
    # A fresh instance per failure path: the maps are handed to mutable
    # processor state, so a shared constant would alias across runs.
    return JavaSemanticFacts({}, set())


def _toolchain_runs(binary: str) -> bool:
    # `which` only proves the binary exists. macOS ships stub shims that exit
    # non-zero the moment they run, so a which-only check reports a JDK that
    # is not there; require a clean `-version`.
    path = shutil.which(binary)
    if path is None:
        return False
    try:
        proc = subprocess.run(
            [path, "-version"],
            capture_output=True,
            text=True,
            check=False,
            timeout=_PROBE_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def java_frontend_available() -> bool:
    return _toolchain_runs(cs.JAVAC_BIN) and _toolchain_runs(cs.JAVA_BIN)


def resolve_java_frontend() -> cs.JavaFrontend:
    # The single source of truth for the EFFECTIVE frontend; the parser
    # fingerprint records the RESOLVED mode so a javac-backed graph and a
    # heuristic one never share an identity.
    mode = settings.JAVA_FRONTEND
    if mode == cs.JavaFrontend.HEURISTIC:
        return mode
    if not java_frontend_available():
        return cs.JavaFrontend.HEURISTIC
    return cs.JavaFrontend.JAVAC


def _cache_dir() -> Path:
    return settings.CGR_HOME.expanduser() / "java_javac"


def _class_fresh(out_dir: Path) -> bool:
    compiled = out_dir / _CLASS_FILE
    if not compiled.is_file():
        return False
    return compiled.stat().st_mtime >= (_TOOL_SRC / _TOOL_SOURCE).stat().st_mtime


def _build_tool(javac: str) -> Path | None:
    cache = _cache_dir()
    out_dir = cache / "out"
    if _class_fresh(out_dir):
        return out_dir
    cache.mkdir(parents=True, exist_ok=True)
    handle = acquire_build_lock(
        cache / _BUILD_LOCK,
        lambda: _class_fresh(out_dir),
        _LOCK_TRIES,
        _LOCK_POLL_SECONDS,
    )
    if handle is None:
        return out_dir if _class_fresh(out_dir) else None
    try:
        if not _class_fresh(out_dir) and not _compile_tool(javac, cache, out_dir):
            return None
    finally:
        release_build_lock(handle)
    return out_dir if _class_fresh(out_dir) else None


def _compile_tool(javac: str, cache: Path, out_dir: Path) -> bool:
    # Compile into a staging directory and publish by rename: a build killed
    # mid-write would otherwise leave a truncated class file whose mtime looks
    # fresh, and every later run would launch it, fail, and degrade -- for
    # good. A crash between the two steps leaves no build at all, which the
    # next run simply repeats.
    staging = cache / _STAGING_DIR
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True, exist_ok=True)
    try:
        proc = subprocess.run(
            [javac, "-d", str(staging), str(_TOOL_SRC / _TOOL_SOURCE)],
            capture_output=True,
            text=True,
            check=False,
            timeout=_BUILD_TIMEOUT,
        )
    except (subprocess.SubprocessError, OSError) as error:
        # A stalled toolchain must not hold indexing hostage: the frontend
        # degrades to tree-sitter like every other failure.
        logger.warning(ls.JAVA_FRONTEND_BUILD_FAILED.format(stderr=error))
        shutil.rmtree(staging, ignore_errors=True)
        return False
    if proc.returncode != 0:
        logger.warning(ls.JAVA_FRONTEND_BUILD_FAILED.format(stderr=proc.stderr.strip()))
        shutil.rmtree(staging, ignore_errors=True)
        return False
    try:
        shutil.rmtree(out_dir, ignore_errors=True)
        staging.rename(out_dir)
    except OSError as error:
        logger.warning(ls.JAVA_FRONTEND_BUILD_FAILED.format(stderr=error))
        shutil.rmtree(staging, ignore_errors=True)
        return False
    return True


def _parse_payload(stdout: str, stderr: str = "") -> JavaSemanticFacts:
    lines = [line for line in stdout.splitlines() if line.strip()]
    if not lines:
        logger.error(ls.JAVA_FRONTEND_PARSE_FAILED.format(stdout=stdout, stderr=stderr))
        return _empty_facts()
    try:
        payload = json.loads(lines[-1])
    except json.JSONDecodeError:
        logger.error(ls.JAVA_FRONTEND_PARSE_FAILED.format(stdout=stdout, stderr=stderr))
        return _empty_facts()
    if not isinstance(payload, dict):
        logger.error(ls.JAVA_FRONTEND_PARSE_FAILED.format(stdout=stdout, stderr=stderr))
        return _empty_facts()
    calls = payload.get("calls", [])
    externals = payload.get("externals", [])
    facts = _empty_facts()
    if not isinstance(calls, list) or not isinstance(externals, list):
        # Well-formed JSON carrying the wrong types for the fact arrays: a
        # tool contract violation, not a per-row defect.
        logger.error(ls.JAVA_FRONTEND_PARSE_FAILED.format(stdout=stdout, stderr=stderr))
        return facts
    for site in calls:
        try:
            key: CallSiteKey = (
                site["file"],
                int(site["line"]),
                int(site["col"]),
                site["name"],
            )
            facts.call_sites[key] = JavaCallSite(
                site["name"],
                site["tfile"],
                int(site["tline"]),
                int(site["tcol"]),
            )
        except (KeyError, TypeError, ValueError):
            # A malformed row drops rather than failing the payload: those
            # sites fall back to the heuristics.
            continue
    for site in externals:
        try:
            facts.external_sites.add(
                (site["file"], int(site["line"]), int(site["col"]), site["name"])
            )
        except (KeyError, TypeError, ValueError):
            continue
    return facts


def run_java_frontend(repo_path: Path) -> JavaSemanticFacts:
    javac = shutil.which(cs.JAVAC_BIN)
    java = shutil.which(cs.JAVA_BIN)
    if javac is None or java is None:
        return _empty_facts()
    out_dir = _build_tool(javac)
    if out_dir is None:
        return _empty_facts()
    logger.info(ls.JAVA_FRONTEND_RUNNING)
    try:
        proc = subprocess.run(
            [java, "-cp", str(out_dir), _MAIN_CLASS, str(repo_path)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=_RUN_TIMEOUT,
            env={
                **os.environ,
                "CGR_IGNORE_DIRS": ",".join(sorted(cs.IGNORE_PATTERNS)),
            },
        )
    except (subprocess.SubprocessError, OSError) as error:
        logger.warning(ls.JAVA_FRONTEND_RUN_FAILED.format(error=error))
        return _empty_facts()
    if proc.returncode != 0:
        # Partial JSON from a run that then failed would be a partial view of
        # the repo presented as a complete one.
        logger.warning(
            ls.JAVA_FRONTEND_RUN_FAILED.format(
                error=proc.stderr.strip() or proc.returncode
            )
        )
        return _empty_facts()
    return _parse_payload(proc.stdout, proc.stderr)
