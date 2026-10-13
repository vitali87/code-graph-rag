"""The embedded graph engine against results recorded from Memgraph.

Each probe below ran on Memgraph 3.3 first; its columns and rows are what
Memgraph returned, copied verbatim (issue #386). They cover the semantics
where engines differ in small ways that change answers: null ordering,
three-valued logic in quantifiers and IN, integer division, `collect`
dropping nulls, `SET +=` with a null removing a key, OPTIONAL MATCH binding
nothing for a partial match, and relationship uniqueness in paths. The
probes share one graph and run in order, as they did on Memgraph.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from codebase_rag.embedded_graph.connection import EmbeddedConnection
from codebase_rag.types_defs import JsonValue

# (query, columns, rows) as Memgraph returned them.
MEMGRAPH_PROBES: list[tuple[str, list[str], list[list[JsonValue]]]] = [
    ("UNWIND [3, null, 1] AS x RETURN x ORDER BY x", ["x"], [[1], [3], [None]]),
    ("UNWIND [3, null, 1] AS x RETURN x ORDER BY x DESC", ["x"], [[None], [3], [1]]),
    (
        "UNWIND [1, null, 2] AS x RETURN collect(x) AS c, count(x) AS n, count(*) AS s",
        ["c", "n", "s"],
        [[[1, 2], 2, 3]],
    ),
    (
        "RETURN 7/2 AS a, 7.0/2 AS b, -7/2 AS c, 7 % 3 AS d, 2^3 AS e",
        ["a", "b", "c", "d", "e"],
        [[3, 3.5, -3, 1, 8.0]],
    ),
    (
        "RETURN toString(true) AS a, 'a' + 1 AS b, toString(1.0) AS c, toInteger('12') AS d, toInteger('x') AS e",
        ["a", "b", "c", "d", "e"],
        [["true", "a1", "1", 12, None]],
    ),
    (
        "CREATE (a:T {k:'a'})-[:R]->(b:T {k:'b'})-[:R]->(c:T {k:'c'}), (a)-[:R]->(c)",
        [],
        [],
    ),
    (
        "MATCH (a:T {k:'a'})-[:R*1..2]->(x) RETURN x.k AS k ORDER BY k",
        ["k"],
        [["b"], ["c"], ["c"]],
    ),
    ("MATCH (a:T {k:'a'})-[:R*]->(x) RETURN count(*) AS n", ["n"], [[3]]),
    ("MATCH (a:T {k:'a'})-[:R*0..]->(x) RETURN count(*) AS n", ["n"], [[4]]),
    (
        "RETURN [1,2] + [3] AS a, [1,2] + 3 AS b, size([1,2,3]) AS c, size('abc') AS d, head([]) AS e, [1,2,3][-1] AS f, [1,2,3][1..] AS g",
        ["a", "b", "c", "d", "e", "f", "g"],
        [[[1, 2, 3], [1, 2, 3], 3, 3, None, 3, [2, 3]]],
    ),
    (
        "RETURN 'abc' STARTS WITH '' AS a, 'abc' ENDS WITH '' AS b, null STARTS WITH 'a' AS c, 1 = '1' AS d, null = null AS e",
        ["a", "b", "c", "d", "e"],
        [[True, True, None, False, None]],
    ),
    (
        "WITH [1,2,null] AS l RETURN any(x IN l WHERE x > 1) AS a, all(x IN l WHERE x > 0) AS b, none(x IN l WHERE x > 5) AS c",
        ["a", "b", "c"],
        [[True, None, None]],
    ),
    (
        "MATCH (n:T {k:'a'}) RETURN labels(n) AS l, keys(n) AS k, properties(n) AS p",
        ["l", "k", "p"],
        [[["T"], ["k"], {"k": "a"}]],
    ),
    (
        "MATCH (n:T {k:'a'}) SET n.f = true, n.l = ['x','y'] RETURN n.f AS f, n.l AS l",
        ["f", "l"],
        [[True, ["x", "y"]]],
    ),
    (
        "MATCH (n:T {k:'a'}) SET n += {g: null, h: 2} RETURN keys(n) AS k",
        ["k"],
        [[["k", "f", "l", "h"]]],
    ),
    (
        "MATCH (n:T {k:'a'}) SET n.h = null RETURN keys(n) AS k",
        ["k"],
        [[["k", "f", "l"]]],
    ),
    (
        "UNWIND [{a:1},{a:2}] AS row RETURN row.a AS a, row.missing AS m",
        ["a", "m"],
        [[1, None], [2, None]],
    ),
    (
        "RETURN coalesce(null, 'x') AS a, coalesce(null, null) AS b, CASE WHEN 1 > 2 THEN 'y' END AS c",
        ["a", "b", "c"],
        [["x", None, None]],
    ),
    (
        "MATCH (a:T {k:'a'}) OPTIONAL MATCH (a)-[:NOPE]->(b) RETURN a.k AS k, b AS b",
        ["k", "b"],
        [["a", None]],
    ),
    (
        "MATCH (a:T {k:'a'}) OPTIONAL MATCH (a)-[:R]->(b)-[:NOPE]->(c) RETURN count(*) AS n, count(b) AS nb",
        ["n", "nb"],
        [[1, 0]],
    ),
    ("WITH 1 AS x, 2 AS y RETURN x, y, x + y", ["x", "y", "x + y"], [[1, 2, 3]]),
    ("UNWIND [1,1,2] AS x WITH DISTINCT x RETURN collect(x) AS c", ["c"], [[[1, 2]]]),
    ("UNWIND ['b','a'] AS x RETURN x ORDER BY x LIMIT 1", ["x"], [["a"]]),
    (
        "RETURN toLower(null) AS a, size(null) AS b, null IN [1] AS c, 1 IN [null] AS d, 1 IN [1, null] AS e",
        ["a", "b", "c", "d", "e"],
        [[None, None, None, False, True]],
    ),
    (
        "MATCH (a:T {k:'a'})-[r:R]->(b) RETURN type(r) AS t, startNode(r) = a AS s ORDER BY b.k",
        ["t", "s"],
        [["R", True], ["R", True]],
    ),
    ("RETURN 'x' =~ 'x|y' AS a, 'xy' =~ 'x' AS b", ["a", "b"], [[True, False]]),
    ("UNWIND [] AS x RETURN count(x) AS n, collect(x) AS c", ["n", "c"], [[0, []]]),
    ("MATCH (n:NOPE) RETURN count(n) AS n", ["n"], [[0]]),
    ("MATCH (n:NOPE) RETURN n.k AS k, count(n) AS c", ["k", "c"], []),
    (
        "RETURN round(2.5) AS a, round(-2.5) AS b, abs(-3) AS c, ceil(1.2) AS d, floor(1.8) AS e, sqrt(16) AS f",
        ["a", "b", "c", "d", "e", "f"],
        [[3.0, -3.0, 3, 2.0, 1.0, 4.0]],
    ),
    (
        "RETURN substring('hello', 1, 3) AS a, replace('aXa','X','Y') AS b, split('a.b.c','.') AS c, trim('  x ') AS d, left('abc',2) AS e, right('abc',2) AS f, reverse('abc') AS g",
        ["a", "b", "c", "d", "e", "f", "g"],
        [["ell", "aYa", ["a", "b", "c"], "x", "ab", "bc", "cba"]],
    ),
    (
        "RETURN range(1,5) AS a, range(0,10,3) AS b, last([1,2]) AS c, tail([1,2,3]) AS d",
        ["a", "b", "c", "d"],
        [[[1, 2, 3, 4, 5], [0, 3, 6, 9], 2, [2, 3]]],
    ),
    ("MATCH (n:T) RETURN n.k AS k ORDER BY k SKIP 1 LIMIT 1", ["k"], [["b"]]),
    (
        "UNWIND [1,2,3] AS x WITH x WHERE x > 1 RETURN sum(x) AS s, avg(x) AS a, min(x) AS mi, max(x) AS ma",
        ["s", "a", "mi", "ma"],
        [[5, 2.5, 2, 3]],
    ),
    (
        "MATCH (a:T {k:'a'}), (b:T {k:'b'}) MERGE (a)-[r:R]->(b) RETURN count(r) AS n",
        ["n"],
        [[1]],
    ),
    ("MATCH (a:T {k:'a'})-[r:R]-(x) RETURN x.k AS k ORDER BY k", ["k"], [["b"], ["c"]]),
    ("MATCH (x:T)<-[:R]-(a:T {k:'a'}) RETURN count(*) AS n", ["n"], [[2]]),
    (
        "MERGE (n:T {k:'z'}) ON CREATE SET n.c = 1 ON MATCH SET n.m = 1 RETURN n.c AS c, n.m AS m",
        ["c", "m"],
        [[1, None]],
    ),
    (
        "MERGE (n:T {k:'z'}) ON CREATE SET n.c = 1 ON MATCH SET n.m = 1 RETURN n.c AS c, n.m AS m",
        ["c", "m"],
        [[1, 1]],
    ),
    ("MATCH (n:T {k:'z'}) DETACH DELETE n RETURN count(*) AS n", ["n"], [[1]]),
    (
        "UNWIND [1,2] AS x RETURN x, [y IN [1,2,3] WHERE y > x | y * 10] AS l",
        ["x", "l"],
        [[1, [20, 30]], [2, [30]]],
    ),
    (
        "RETURN [x IN [1,2,3] | x * 2] AS a, [x IN [1,2,3] WHERE x > 1] AS b",
        ["a", "b"],
        [[[2, 4, 6], [2, 3]]],
    ),
    (
        "MATCH (n:T) WITH n ORDER BY n.k LIMIT 2 RETURN collect(n.k) AS c",
        ["c"],
        [[["a", "b"]]],
    ),
    (
        "MATCH (n:T) RETURN n.k AS k, n.k STARTS WITH 'a' AS s ORDER BY k",
        ["k", "s"],
        [["a", True], ["b", False], ["c", False]],
    ),
    (
        "UNWIND [1,2,3] AS x RETURN x % 2 AS p, count(*) AS n ORDER BY p",
        ["p", "n"],
        [[0, 1], [1, 2]],
    ),
    ("MATCH (n:T) WHERE n:T AND NOT n:X RETURN count(n) AS n", ["n"], [[3]]),
    (
        "UNWIND [[1,2],[3]] AS l UNWIND l AS x RETURN collect(x) AS c",
        ["c"],
        [[[1, 2, 3]]],
    ),
    (
        "RETURN {a: 1, b: [1, 'x']} AS m, {a:1}.a AS v",
        ["m", "v"],
        [[{"a": 1, "b": [1, "x"]}, 1]],
    ),
    # single(): one true element is true whatever else is null; with none
    # true, a null leaves the answer unknown.
    (
        "RETURN single(x IN [null, 1] WHERE x > 2) AS a, "
        "single(x IN [null, 3] WHERE x > 2) AS b, "
        "single(x IN [3, 4, null] WHERE x > 2) AS c, "
        "single(x IN [] WHERE x > 2) AS d, single(x IN [null] WHERE x > 2) AS e",
        ["a", "b", "c", "d", "e"],
        [[None, True, False, False, None]],
    ),
    # A relationship is used once per MATCH, across fixed and
    # variable-length parts and across patterns.
    ("CREATE (x:U {k:'x'})-[:CALLS]->(y:U {k:'y'})-[:CALLS]->(x)", [], []),
    (
        "MATCH (a:U)-[r:CALLS]->(b)-[:CALLS*1..2]->(c) "
        "RETURN a.k AS a, b.k AS b, c.k AS c ORDER BY a, b, c",
        ["a", "b", "c"],
        [["x", "y", "x"], ["y", "x", "y"]],
    ),
    (
        "MATCH (a:U)-[:CALLS*1..2]->(b), (b)-[:CALLS*1..2]->(c) "
        "RETURN a.k AS a, b.k AS b, c.k AS c ORDER BY a, b, c",
        ["a", "b", "c"],
        [["x", "y", "x"], ["y", "x", "y"]],
    ),
    (
        "MATCH (a:U)-[:CALLS*2]->(c), (a)-[r:CALLS]->(b) "
        "RETURN a.k AS a, b.k AS b, c.k AS c",
        ["a", "b", "c"],
        [],
    ),
]


def _comparable(value: JsonValue) -> JsonValue:
    # Memgraph's ids are its own, so a node compares by labels and
    # properties and a relationship by type and properties.
    if isinstance(value, dict) and "labels" in value and "properties" in value:
        return {"labels": sorted(value["labels"]), "properties": value["properties"]}
    if isinstance(value, dict) and "type" in value and "start" in value:
        return {"type": value["type"], "properties": value["properties"]}
    if isinstance(value, list):
        return [_comparable(v) for v in value]
    return value


@pytest.fixture
def conn() -> Iterator[EmbeddedConnection]:
    with EmbeddedConnection(":memory:") as c:
        yield c


def test_every_probe_matches_memgraph(conn: EmbeddedConnection) -> None:
    mismatches = []
    for query, columns, rows in MEMGRAPH_PROBES:
        result = conn.run(query)
        got = [[_comparable(v) for v in row] for row in result.rows]
        if result.columns != columns or got != rows:
            mismatches.append((query, columns, rows, result.columns, got))
    assert mismatches == []
