# ruff: noqa: T201
"""Enumerate which algo.* procedures ArcadeDB actually exposes over Cypher.

ArcadeDB publishes an algorithm list but does not document the Cypher CALL
surface, and the two do not match. Run this against a live server and paste
the surviving names into ARCADE_PROCEDURE_CATALOG.

Usage:
    export ARCADEDB_PASSWORD=pw
    uv run python scripts/probe_arcade_procedures.py \
        --uri bolt://localhost:7687 --user root --database cgrtest

    # or, without the env var, you'll be prompted interactively:
    uv run python scripts/probe_arcade_procedures.py \
        --uri bolt://localhost:7687 --user root --database cgrtest
"""

from __future__ import annotations

import argparse
import getpass
import os

from neo4j import GraphDatabase, Query

CANDIDATES = [
    "algo.pageRank",
    "algo.articleRank",
    "algo.personalizedPageRank",
    "algo.betweenness",
    "algo.betweennessCentrality",
    "algo.closeness",
    "algo.closenessCentrality",
    "algo.harmonicCentrality",
    "algo.eigenvectorCentrality",
    "algo.degreeCentrality",
    "algo.katzCentrality",
    "algo.hits",
    "algo.eccentricity",
    "algo.wcc",
    "algo.scc",
    "algo.louvain",
    "algo.leiden",
    "algo.labelPropagation",
    "algo.slpa",
    "algo.triangleCount",
    "algo.localClusteringCoefficient",
    "algo.shortestPath",
    "algo.allShortestPaths",
    "algo.dijkstra",
    "algo.astar",
    "algo.bellmanFord",
    "algo.yens",
    "algo.kShortestPaths",
    "algo.allSimplePaths",
    "algo.floydWarshall",
    "algo.longestPath",
    "algo.bfs",
    "algo.topologicalSort",
]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--uri", required=True)
    parser.add_argument("--user", required=True)
    parser.add_argument("--database", required=True)
    args = parser.parse_args()

    # Never accept the password as an argv flag: argv is visible to every
    # other process on the machine via `ps` and gets written to shell
    # history. Read it from the environment, falling back to an
    # interactive, non-echoing prompt.
    password = os.environ.get("ARCADEDB_PASSWORD") or getpass.getpass(
        "ArcadeDB password: "
    )

    driver = GraphDatabase.driver(args.uri, auth=(args.user, password))
    available: list[str] = []
    with driver.session(database=args.database) as session:
        for name in CANDIDATES:
            try:
                # neo4j's stub types Query.text as LiteralString to steer
                # callers away from f-string-interpolated Cypher; `name`
                # only ever comes from the CANDIDATES literal above, never
                # from external input, so the dynamic str is safe here.
                result = session.run(
                    Query(f"CALL {name}() YIELD node RETURN node LIMIT 1")  # ty: ignore[invalid-argument-type]
                )
                list(result)
                available.append(name)
                print(f"OK      {name}")
            except Exception as e:
                head = str(e).splitlines()[0][:90]
                print(f"MISSING {name}  ({head})")
    driver.close()

    print("\nAvailable:")
    for name in available:
        print(f"  {name}")


if __name__ == "__main__":
    main()
