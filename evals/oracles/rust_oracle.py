from __future__ import annotations

import json
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path

from filelock import FileLock

from codebase_rag import constants as cs

from .. import constants as ec
from ..types_defs import GraphData, OraclePayload
from ._common import is_ignored, payload_to_graph

_ORACLE_DIR = Path(__file__).parent / ec.RS_ORACLE_DIRNAME
_MANIFEST = _ORACLE_DIR / "Cargo.toml"
_BUILD_LOCK = _ORACLE_DIR / ec.RS_ORACLE_BUILD_LOCK
_CALLABLE_KINDS = frozenset({cs.NodeLabel.FUNCTION.value, cs.NodeLabel.METHOD.value})


def rust_available() -> bool:
    return shutil.which(ec.CARGO_BIN) is not None


@lru_cache(maxsize=1)
def _binary_path() -> Path:
    # (H) Ask cargo where the release binary lands rather than hardcoding
    # (H) `target/release/rs_oracle`: a user/global cargo config (e.g. the common
    # (H) macOS `build.target-dir = "target.noindex"` Spotlight workaround) or
    # (H) CARGO_TARGET_DIR redirects it elsewhere. `cargo metadata` is read-only
    # (H) (no build, no link race), and memoized so it runs once per process.
    proc = subprocess.run(
        [
            ec.CARGO_BIN,
            ec.CARGO_METADATA,
            ec.CARGO_FORMAT_VERSION,
            ec.CARGO_FORMAT_VERSION_1,
            ec.CARGO_NO_DEPS,
            ec.CARGO_MANIFEST,
            str(_MANIFEST),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    target_dir = Path(json.loads(proc.stdout)[ec.CARGO_META_TARGET_DIR_KEY])
    return target_dir / ec.CARGO_RELEASE_DIRNAME / ec.RS_ORACLE_BIN


def _sources_newer_than(binary: Path) -> bool:
    binary_mtime = binary.stat().st_mtime
    sources = [_MANIFEST, *(_ORACLE_DIR / ec.CARGO_SRC_DIRNAME).glob(ec.CARGO_RS_GLOB)]
    return any(src.stat().st_mtime > binary_mtime for src in sources)


def _ensure_oracle_built() -> Path:
    # (H) Build the syn oracle binary exactly once, serialized across parallel
    # (H) pytest-xdist workers by a cross-process file lock, then exec the binary
    # (H) directly instead of via `cargo run`. `cargo run` re-links the release
    # (H) binary on every invocation, so concurrent workers raced that link step --
    # (H) one worker exec'd the binary while another rewrote it (ETXTBSY), and cargo
    # (H) exited 101. That was an intermittent CI flake (seen on the macos-py3.12
    # (H) runner under `pytest -n auto`). A prebuilt binary is only ever read
    # (H) afterwards, so concurrent execs are safe. The mtime guard still rebuilds
    # (H) when the oracle source changes (local dev correctness).
    binary = _binary_path()
    with FileLock(str(_BUILD_LOCK)):
        if not binary.exists() or _sources_newer_than(binary):
            subprocess.run(
                [
                    ec.CARGO_BIN,
                    ec.CARGO_BUILD,
                    ec.CARGO_RELEASE,
                    ec.CARGO_QUIET,
                    ec.CARGO_MANIFEST,
                    str(_MANIFEST),
                ],
                capture_output=True,
                text=True,
                check=True,
            )
    return binary


def _run_rust_oracle_payload(target: Path) -> OraclePayload:
    proc = subprocess.run(
        [str(_ensure_oracle_built()), str(target)],
        capture_output=True,
        text=True,
        check=True,
    )
    payload: OraclePayload = json.loads(proc.stdout or "{}")
    return payload


def run_rust_oracle(target: Path) -> GraphData:
    return payload_to_graph(_run_rust_oracle_payload(target))


def run_rust_call_oracle(target: Path) -> tuple[set[tuple[str, str]], frozenset[str]]:
    # (H) File-level Rust call sites restricted to first-party callees (a callee
    # (H) whose simple name is a declared Function/Method), with the declared name
    # (H) universe so the cgr side can be held to the same set. Mirrors the Go
    # (H) call oracle (run_go_call_oracle).
    payload = _run_rust_oracle_payload(target)
    declared = frozenset(
        rec[ec.ORACLE_KEY_NAME]
        for rec in payload.get(ec.ORACLE_KEY_NODES, [])
        if rec.get(ec.ORACLE_KEY_KIND) in _CALLABLE_KINDS
    )
    edges = {
        (call[ec.ORACLE_KEY_FILE], call[ec.ORACLE_KEY_NAME])
        for call in payload.get(ec.ORACLE_KEY_CALLS, [])
        if call[ec.ORACLE_KEY_NAME] in declared
        and not is_ignored(call[ec.ORACLE_KEY_FILE])
    }
    return edges, declared
