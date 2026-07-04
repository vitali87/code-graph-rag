from __future__ import annotations

import shutil
from pathlib import Path

from codebase_rag import constants as cs

from .. import constants as ec
from ..types_defs import GraphData, OraclePayload
from ._common import is_ignored, payload_to_graph, run_node_oracle_payload

_ORACLE_DIR = Path(__file__).parent / ec.TS_ORACLE_DIRNAME
_SCRIPT = _ORACLE_DIR / ec.TS_ORACLE_SCRIPT
_NODE_MODULES = _ORACLE_DIR / ec.NODE_MODULES_DIRNAME
_CALLABLE_KINDS = frozenset({cs.NodeLabel.FUNCTION.value, cs.NodeLabel.METHOD.value})


def typescript_available() -> bool:
    return (
        shutil.which(ec.NODE_BIN) is not None and shutil.which(ec.NPM_BIN) is not None
    )


def _run_payload(target: Path, suffixes: tuple[str, ...]) -> OraclePayload:
    return run_node_oracle_payload(_ORACLE_DIR, _SCRIPT, (str(target), *suffixes))


def _run(target: Path, suffixes: tuple[str, ...]) -> GraphData:
    return payload_to_graph(_run_payload(target, suffixes))


def run_typescript_oracle(target: Path) -> GraphData:
    return _run(target, ec.TS_SUFFIXES)


def run_javascript_oracle(target: Path) -> GraphData:
    return _run(target, ec.JS_SUFFIXES)


def run_typescript_call_oracle(
    target: Path,
) -> tuple[set[tuple[str, str]], frozenset[str]]:
    # (H) File-level TypeScript call sites restricted to first-party callees (a
    # (H) callee whose simple name is a declared Function/Method), with the declared
    # (H) name universe so the cgr side can be held to the same set. Mirrors the Go,
    # (H) Rust, and Java call oracles.
    return _call_edges(target, ec.TS_SUFFIXES)


def run_javascript_call_oracle(
    target: Path,
) -> tuple[set[tuple[str, str]], frozenset[str]]:
    # (H) File-level JavaScript call sites, same tsc oracle over .js/.jsx. tsc's
    # (H) syntactic parse handles JS, so this is independent of cgr's tree-sitter JS
    # (H) frontend and measures cgr's cross-file JS call resolution against ground
    # (H) truth (mirrors run_typescript_call_oracle).
    return _call_edges(target, ec.JS_SUFFIXES)


def _call_edges(
    target: Path, suffixes: tuple[str, ...]
) -> tuple[set[tuple[str, str]], frozenset[str]]:
    payload = _run_payload(target, suffixes)
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
