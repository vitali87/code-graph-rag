from __future__ import annotations

from pathlib import Path

from codebase_rag import constants as cs

from .. import constants as ec
from ..types_defs import GraphData, OraclePayload
from ._common import (
    is_ignored,
    node_oracle_available,
    node_oracle_skip_reason,
    payload_to_graph,
    run_node_oracle_payload,
)

_ORACLE_DIR = Path(__file__).parent / ec.TS_ORACLE_DIRNAME
_SCRIPT = _ORACLE_DIR / ec.TS_ORACLE_SCRIPT
_NODE_MODULES = _ORACLE_DIR / ec.NODE_MODULES_DIRNAME
_CALLABLE_KINDS = frozenset({cs.NodeLabel.FUNCTION.value, cs.NodeLabel.METHOD.value})


def typescript_available() -> bool:
    return node_oracle_available(_ORACLE_DIR)


def typescript_skip_reason() -> str | None:
    """Why this oracle cannot run here, or None when it can (issue #1639).

    On a clean checkout this answers None because the deps are not installed
    yet and the probe cannot run; `run_node_oracle_payload` re-checks once
    `ensure_node_deps` has fetched them and raises `NodeOracleUnavailable`,
    which the test guards below turn into a skip carrying the real reason.
    """
    return node_oracle_skip_reason(_ORACLE_DIR)


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
    # File-level TypeScript call sites restricted to first-party callees (simple
    # name is a declared Function/Method), with the declared name universe so the
    # cgr side is held to the same set. Mirrors the Go, Rust, and Java oracles.
    return _call_edges(target, ec.TS_SUFFIXES)


def run_javascript_call_oracle(
    target: Path,
) -> tuple[set[tuple[str, str]], frozenset[str]]:
    # File-level JavaScript call sites, same tsc oracle over .js/.jsx. tsc's
    # syntactic parse handles JS, so this is independent of cgr's tree-sitter JS
    # frontend and measures cgr's cross-file JS call resolution (mirrors
    # run_typescript_call_oracle).
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
