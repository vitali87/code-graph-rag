#!/usr/bin/env python3
"""Check a cgr-graph-rs wheel against another on a graph both read.

`record DIR` builds a small call graph with code-graph-rag's embedded graph
and stores what the installed extension answers for every ported procedure.
`check DIR` asks the extension installed where it runs the same questions,
and needs nothing but the extension and the standard library, so it runs in
a bare container for a libc no runner provides (musl).
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from importlib import import_module
from pathlib import Path
from types import ModuleType

MODULE = "cgr_graph_rs"
GRAPH = "graph.sqlite"
EXPECTED = "expected.json"
ENCODING = "utf-8"
PROCEDURES = (
    "pagerank.get",
    "degree_centrality.get",
    "weakly_connected_components.get",
    "nxalg.strongly_connected_components",
    "betweenness_centrality.get",
)
JSON_SAMPLE = {"k": [1, 2.5, None, True, "é"]}
NODES = (
    "UNWIND range(1, 60) AS i "
    "CREATE (:Function {qualified_name: 'f' + toString(i), i: i})"
)
# A deterministic tangle of calls, with cycles, for every procedure to chew on.
EDGES = (
    "MATCH (a:Function), (b:Function) "
    "WHERE a <> b AND (a.i * 7 + b.i) % 11 = 0 "
    "CREATE (a)-[:CALLS]->(b)"
)
VERDICT = "{name}: {result}"
SAME = "identical"
DIFFERENT = "DIFFERENT"


def _extension() -> ModuleType:
    # Imported by name, as the engine does, because the compiled module ships
    # no type stubs.
    return import_module(MODULE)


def _answers(reader, version: str | None) -> dict[str, str]:
    answers = {
        name: repr(reader.run_procedure(version, name, [], None)) for name in PROCEDURES
    }
    answers["render_json"] = _extension().render_json(JSON_SAMPLE, True)
    return answers


def record(directory: Path) -> int:
    from codebase_rag.constants.embedded_graph import (
        EG_BUSY_TIMEOUT_MS,
        EG_CACHE_SIZE_KIB,
    )
    from codebase_rag.embedded_graph import sql as sq
    from codebase_rag.embedded_graph.connection import EmbeddedConnection

    directory.mkdir(parents=True, exist_ok=True)
    path = directory / GRAPH
    path.unlink(missing_ok=True)
    with EmbeddedConnection(str(path)) as conn:
        conn.run(NODES)
        conn.run(EDGES)
    with sqlite3.connect(path) as conn:
        version = conn.execute(sq.GRAPH_VERSION).fetchone()[0]
    reader = _extension().Reader(
        str(path), EG_BUSY_TIMEOUT_MS, EG_CACHE_SIZE_KIB, sq.GRAPH_VERSION
    )
    try:
        answers = _answers(reader, version)
    finally:
        reader.close()
    setup = {
        "version": version,
        "busy_ms": EG_BUSY_TIMEOUT_MS,
        "cache_kib": EG_CACHE_SIZE_KIB,
        "version_sql": sq.GRAPH_VERSION,
        "answers": answers,
    }
    (directory / EXPECTED).write_text(json.dumps(setup), encoding=ENCODING)
    return 0


def check(directory: Path) -> int:
    setup = json.loads((directory / EXPECTED).read_text(encoding=ENCODING))
    reader = _extension().Reader(
        str(directory / GRAPH),
        setup["busy_ms"],
        setup["cache_kib"],
        setup["version_sql"],
    )
    try:
        answers = _answers(reader, setup["version"])
    finally:
        reader.close()
    differing = 0
    for name, expected in setup["answers"].items():
        same = answers.get(name) == expected
        differing += not same
        print(VERDICT.format(name=name, result=SAME if same else DIFFERENT))  # noqa: T201
    return 1 if differing else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("mode", choices=("record", "check"))
    parser.add_argument("directory", type=Path)
    args = parser.parse_args(argv)
    return (record if args.mode == "record" else check)(args.directory)


if __name__ == "__main__":
    sys.exit(main())
