"""The Rust Cypher frontend against the Python parser it ports.

`rust/cgr_graph_rs/src/cypher` ports the embedded engine's lexer and parser
(issue #386), and `parser.parse` takes its tree whenever the extension is
installed, so a port that drifted would change what a query means for those
users alone. Every case here runs both ways. A tree must match the Python
one down to each leaf's type, which `==` would miss (`1 == 1.0 == True`, and
a StrEnum member equals its value), so trees are compared by their reprs,
which spell out every class and leaf; a rejected query must raise the same
CypherSyntaxError, offset and message. The port may decline a query whose meaning rests on Python
itself (its Unicode tables, unbounded ints, its recursion limit) by
returning None, and `parse` must then still give the Python answer.

The cases: every string constant cgr ships (its queries, and thousands of
other strings the parser must reject), every string in the embedded-graph
tests, the write queries the ingestor builds per label, edge cases written
for the lexer and grammar, and seeded mutations of all the valid ones.
"""

from __future__ import annotations

import ast
import importlib
import pkgutil
import random
import re
from collections.abc import Callable, Iterable, Iterator
from functools import cache
from pathlib import Path

import pytest

from codebase_rag import constants
from codebase_rag import cypher_queries as cq
from codebase_rag.constants import NODE_UNIQUE_CONSTRAINTS
from codebase_rag.embedded_graph.lexer import CypherSyntaxError, tokenize
from codebase_rag.embedded_graph.parser import (
    Statement,
    parse,
    parse_python,
    rust_parser,
)
from codebase_rag.graph_dialects import MemgraphDialect, Neo4jDialect
from codebase_rag.tools.graph_rerank import build_proximity_query

rust = pytest.importorskip("cgr_graph_rs")
if rust_parser() is None:
    pytest.skip(
        "this cgr_graph_rs build predates its Cypher parser, so the engine parses "
        "in Python; reinstall ./rust/cgr_graph_rs to test the port",
        allow_module_level=True,
    )

type Outcome = tuple[str | int, ...]
type Parser = Callable[[str], Statement | None]

_TESTS = Path(__file__).parent
# Kept out of the literals below, where `\u` would start a Python escape.
_U = chr(92) + "u"
_FUZZ_SEED = 386
_FUZZ_CASES = 5000
# Besides CypherSyntaxError (a ValueError), the reference raises ValueError
# from int() and chr() on some digits and escapes, and RecursionError when
# nested past its limit; the port declines all of those.
_PYTHON_ONLY_FAILURES = (ValueError, RecursionError)
_FUZZ_ALPHABET = "'\"`\\$.:;,()[]{}<>=!~+-*/%^|_ aZ09eE\né\xa0\xb2"

_EDGE_CASES = [
    # Strings: escapes, quotes of either kind, Unicode kept as written.
    "RETURN 'it\\'s', \"say \\\"hi\\\"\", 'tab\\tnew\\nline', 'back\\\\slash', '\\q\\b\\f\\r'",
    f"RETURN '{_U}0041{_U}00e9{_U}4e2d', '{_U}0027', '\\U0041'",
    "RETURN 'café', `naïve name`, 'emoji \U0001f642' // trailing comment ü",
    "RETURN /* block */ 1 /* another */ + /**/ 2 // end",
    "RETURN 1 // comment without a newline",
    "RETURN `a``b`, `` AS empty, `RETURN` AS kw, `x y`.`z w`",
    # Numbers: floats in every spelling, ranges against floats. A `.` after
    # a value is an accessor, so `.5` is a number only after punctuation.
    "RETURN 1., 1.e3, 1e3, 1E-3, 2.5e+2, 0.0, 007, 9223372036854775807, "
    "[.5, -.5, (.5)], 1 + .5",
    "RETURN 1.7976931348623157e308, 1e400, 5e-324, 1e-400, 0.1 + 0.2",
    "RETURN range(0, 10)[2..4], [1, 2, 3][..2], [1, 2, 3][1..], [0][0..], [1][..], "
    "[1][.5..]",
    "RETURN (1).k, [1][0].k, {a: 1}.a",
    "RETURN -1, - 1, --1, -(-1), +1, -2^2, 2^-1, 1 - -1, -9223372036854775807",
    # Precedence and associativity.
    "RETURN 1 + 2 * 3 - 4 / 5 % 6 ^ 7 ^ 8, (1 + 2) * 3, 1 - 2 - 3, 2 ^ 3 ^ 2",
    "RETURN NOT true AND false OR true XOR false, NOT NOT true, true OR false AND NOT false",
    "RETURN 1 < 2 = true, 1 <= 2 <> 3 >= 4, 1 != 2, 'a' =~ 'a.*', 1 > 2",
    "MATCH (n) WHERE n.name STARTS WITH 'a' AND n.name ENDS WITH 'z' AND n.name "
    "CONTAINS 'm' AND n.k IN [1, 2] AND n.x IS NULL AND n.y IS NOT NULL RETURN n",
    "RETURN 1 + 2 IN [3] = true IS NULL",
    # Labels: alternation in patterns, label tests in expressions.
    "MATCH (n:Function|Method) RETURN n",
    "MATCH (n) WHERE n:Function OR n:Class:Method OR n:A|B RETURN n:Function AS f",
    "MATCH (n:Union:`Weird Label`) RETURN n.end, n.`order` AS count, n.match, n.return",
    "MATCH (`n`) RETURN `n`.k, `n`:A, (n:A) AS paren",
    # Quantifiers, comprehensions, CASE, maps, calls.
    "RETURN any(x IN [1, 2] WHERE x > 1), all(x IN [] WHERE x), none(x IN [null]), "
    "single(x IN [1] WHERE x = 1), ANY(`y` IN [1])",
    "RETURN all(l), any(l, 2), none + 1 AS n, single",
    "RETURN [x IN range(1, 10) WHERE x % 2 = 0 | x * x], [x IN [1] | x], "
    "[x IN [1] WHERE x], [`x` IN [1]], [x, y]",
    "RETURN CASE WHEN 1 = 1 THEN 'a' WHEN 2 = 2 THEN 'b' ELSE 'c' END, "
    "CASE 1 WHEN 1 THEN 2 END, CASE n.k WHEN 1 THEN CASE WHEN true THEN 1 END END",
    "RETURN {a: 1, b: {c: [1, 2, {d: null}]}, `e f`: $p, match: 1, `3`: 3}, {}",
    "RETURN labels(n)[0], n.name + ' ' + toString(n.k), coalesce(n.a, n.b), size(collect(n))",
    "RETURN apoc.text.join(['a'], ','), Apoc.Coll.Sum([1]), date.truncate('day', x), a.b.c(1).d",
    "RETURN count(*), COUNT(DISTINCT n), count(n), Collect(DISTINCT n.k), exists(n.k)",
    "RETURN n.k[0..-1], n.k[-1], n['k'], n.k[1][2].z, [[1]][0][0]",
    "RETURN $param, $0, $_x, $p.k, $p[0], $",
    "RETURN n.k AS `weird alias`, n /* c */ . k, n.`a``b` + 1",
    # Clauses.
    "CALL pagerank.get() YIELD node, rank RETURN node, rank ORDER BY rank DESC LIMIT 10",
    "CALL db.labels() YIELD label AS l WHERE l STARTS WITH 'F' RETURN l",
    "CALL nxalg.shortest_path(1, 2) YIELD path RETURN path",
    "CALL `odd`.`name`(1, 'x') YIELD a, b AS c RETURN a, c",
    "MERGE (n:Module {qualified_name: $qn}) ON CREATE SET n.created = timestamp(), "
    "n += $props ON MATCH SET n.seen = n.seen + 1, n:Visited RETURN n",
    "MATCH (a), (b) MERGE (a)-[r:CALLS {via: 'x'}]->(b) ON CREATE SET r = {} "
    "ON CREATE SET r.k = 1 RETURN count(r)",
    "UNWIND $batch AS row FOREACH (x IN row.items | CREATE (:Item {k: x}) "
    "MERGE (:Seen {k: x})) RETURN count(*)",
    "FOREACH (a IN [1] | FOREACH (b IN [2] | SET n.k = a + b))",
    "UNWIND [1, 2, 3] AS x WITH x WHERE x > 1 RETURN sum(x) AS s",
    "OPTIONAL MATCH (n)-[r]-(m) OPTIONAL MATCH (m)-->(o) RETURN n, r, m, o",
    "MATCH p = (a)-[:R*1..3]->(b)<-[:S*]-(c)-[*2]-(d)-[r*..4 {k: 1}]->(e)"
    "-[:T*0..]->(f)-[$props]-(g) RETURN p",
    "MATCH (a)<-->(b), (c)--(d), (e)<--(f), (g {k: $v}) RETURN a",
    "MATCH (a)-[:A|:B|C]->(b), (c)-[r]->(d) RETURN a",
    "EXPLAIN MATCH (n) RETURN n",
    "PROFILE MATCH (n) RETURN n",
    "explain RETURN 1;",
    "MATCH (n) RETURN DISTINCT n.k AS k ORDER BY k ASC, n.x DESCENDING, n.y ASCENDING, "
    "n.z DESC SKIP 1 LIMIT 2",
    "MATCH (n) WITH *, n.k AS k WHERE k > 1 RETURN *",
    "MATCH (n) WITH DISTINCT n ORDER BY n.k SKIP $s LIMIT $l WHERE n.k RETURN n",
    "RETURN 1 AS a UNION RETURN 2 AS a UNION ALL RETURN 3 AS a",
    "MATCH (n) DETACH DELETE n",
    "MATCH (n)-[r]->() DELETE r, n",
    "MATCH (n) SET n:Label, n.k = 1, n += {a: 1}, n = {b: 2} REMOVE n.k, n:Label",
    "CREATE (a:A {k: 1})-[:R {w: 0.5}]->(b:B), (c), p = (d)-[:S]->(e)",
    "WITH 1 AS one MATCH (n) WHERE n.x = one RETURN n",
    # Schema commands in both engines' spellings.
    "CREATE INDEX ON :Function(qualified_name)",
    "CREATE INDEX ON :F(a, b);",
    "CREATE INDEX idx IF NOT EXISTS FOR (n:Function) ON (n.name, n.qualified_name)",
    "CREATE CONSTRAINT ON (n:Module) ASSERT n.qualified_name IS UNIQUE;",
    "CREATE CONSTRAINT c FOR (n:Module) REQUIRE (n.a, n.b) IS UNIQUE",
    "DROP INDEX ON :Function(name)",
    "DROP CONSTRAINT ON (n:Module) ASSERT n.qn IS UNIQUE",
    "DROP INDEX some_name",
    "DROP INDEX ON",
    "SHOW INDEX INFO",
    "SHOW CONSTRAINT INFO",
    "SHOW STORAGE INFO",
    "show indexes",
    "SHOW CONSTRAINTS;",
    # Whitespace Python's str.isspace() counts, beyond ASCII too.
    "  RETURN 1  ;  ",
    "\tRETURN\n1\r\n",
    "RETURN\x0b1\x0c,\x1c2\x1d,\x1e3\x1f",
    "RETURN\xa01,\u20032,\u30003,\u20284,\u00855",
]

_INVALID = [
    "",
    "   ",
    "// only a comment",
    "RETURN",
    "RETURN 1,",
    "MATCH (n RETURN n",
    "MATCH (n) RETURN n n",
    "RETURN 'unterminated",
    'RETURN "unterminated\\"',
    "RETURN 'ends in a backslash\\",
    "RETURN /* unterminated",
    "RETURN /*/",
    "RETURN `unterminated",
    "RETURN `a``",
    "RETURN #",
    "RETURN 1 ! 2",
    "RETURN @x",
    "RETURN ?",
    "RETURN 1 & 2",
    "RETURN ~1",
    "RETURN \\",
    "RETURN \x00",
    "RETURN \x01",
    "RETURN \x7f",
    "RETURN x.5",
    "RETURN .5",
    "RETURN 1e, 1e+, 2ex",
    "RETURN [1..3]",
    "RETURN [1...5], [1. .5], (1).5",
    "RETURN n {.name}",
    "RETURN 1 ``",
    "RETURN shortestPath((a)-->(b))",
    "MATCH p = shortestPath((a)-[*]-(b)) RETURN p",
    "MATCH p = allShortestPaths((a)-[*]-(b)) RETURN p",
    "MATCH (n) SET n",
    "MATCH (n) SET n.k",
    "MATCH (n) SET n:A:B",
    "MATCH (n) REMOVE n",
    "CREATE INDEX",
    "CREATE INDEX ON :",
    "CREATE CONSTRAINT ON (n) ASSERT n.k IS UNIQUE",
    "DROP",
    "DROP TABLE x",
    "MERGE (n) ON SET n.k = 1",
    "MERGE (n) ON MATCH n.k = 1",
    "FOREACH (x IN [1] CREATE ())",
    "FOREACH x IN [1] | CREATE ()",
    "FOREACH (x IN [1] | WHERE)",
    "UNWIND [1] x RETURN x",
    "UNWIND [1] AS RETURN 1",
    "UNWIND [1] AS match RETURN 1",
    "CALL pagerank.get( YIELD node",
    "CALL pagerank.get() YIELD",
    "CALL () YIELD x",
    "RETURN CASE WHEN 1 THEN 2",
    "RETURN CASE END",
    "RETURN [x IN]",
    "RETURN [x IN [1] WHERE]",
    "RETURN {a 1}",
    "RETURN {a: 1,}",
    "RETURN {1: 1}",
    "RETURN any(x IN)",
    "RETURN any(1 IN [1])",
    "RETURN count(* )x",
    "RETURN f(,)",
    "RETURN a.",
    "RETURN a[",
    "RETURN a[1",
    "RETURN a[1..",
    "RETURN (1",
    "RETURN 1)",
    "RETURN n.k:A",
    "MATCH (n) WHERE (n)-[:R]->() RETURN n",
    "MATCH (a)-[r:]->(b) RETURN a",
    "MATCH (a)-[*a]->(b) RETURN a",
    "MATCH (a)-[*1..x]->(b) RETURN a",
    "MATCH (a)-[*.5]->(b) RETURN a",
    "MATCH (a)<-[r]-(b RETURN a",
    "MATCH (a)->(b) RETURN a",
    "MATCH (a)<>(b) RETURN a",
    "MATCH (n:) RETURN n",
    "MATCH (n:A|) RETURN n",
    "MATCH (match) RETURN 1",
    "MATCH n RETURN n",
    "MATCH return = (n) RETURN 1",
    "WITH 1 AS x RETURN x AS",
    "RETURN 1 ORDER x",
    "RETURN 1 ORDER BY",
    "RETURN 1 SKIP",
    "MATCH (n) WHERE RETURN n",
    "RETURN 1 IS NULL NULL",
    "RETURN 1 IS NOT",
    "RETURN 1 STARTS 'a'",
    "RETURN 1 ENDS WITH",
    "UNION RETURN 1",
    "RETURN 1 UNION",
    "RETURN 1 UNION ALL",
    "EXPLAIN",
    "EXPLAIN CREATE INDEX ON :A(b)",
    "RETURN 1; RETURN 2",
    "RETURN 1;;",
    "RETURN $`x`",
    "RETURN 'a' 'b'",
    "RETURN \"it's\" 'x'",
    "RETURN 1 'a\"b\\'c'",
    "RETURN 1 `back``tick`",
    "RETURN 1 `with 'quote`",
    "RETURN 1 `new\nline`",
    "RETURN 1 `tab\there`",
    "RETURN 1 `ctl\x01`",
    "RETURN 1 `é`",
    "RETURN 1 'é\xa0'",
    "RETURN 1 `\x85\u200b\U0001f642`",
    "MATCH (n) RETURN n QUERY MEMORY LIMIT 100 MB",
]

# Text the port leaves to Python, each for a reason the comment names.
_DECLINED = [
    # Identifiers and digits outside ASCII: Python's str.isalpha/isdigit
    # decide them, and int() reads any decimal digit.
    "RETURN café",
    "MATCH (n:Über) RETURN n",
    "RETURN \u0663",
    # Python upper-cases the dotless `ı` to `I`, so this `ın` is the IN keyword.
    "RETURN x \u0131n [1]",
    "RETURN $é",
    "RETURN 1\xb2",
    # Beyond 64 bits.
    "RETURN 99999999999999999999",
    "MATCH ()-[*1..99999999999999999999]->() RETURN 1",
    # `\u` escapes int(code, 16) and chr() read by rules of their own.
    f"RETURN '{_U}ZZZZ'",
    f"RETURN '{_U}12'",
    f"RETURN '{_U}+0ff'",
    f"RETURN '{_U}0x1f'",
    f"RETURN '{_U}d83d{_U}de00'",
    # A str with a lone surrogate has no UTF-8 form at all.
    f"RETURN '{chr(0xD800)}'",
    # Nesting deep enough to near Python's recursion limit.
    "RETURN " + "(" * 40 + "1" + ")" * 40,
    "RETURN " + "-" * 40 + "1",
    "RETURN " + "NOT " * 40 + "true",
    "RETURN [" + ", ".join(["n.k + 1"] * 600) + "]",
]


def _string_constants() -> list[str]:
    modules = [cq] + [
        importlib.import_module(f"{constants.__name__}.{info.name}")
        for info in pkgutil.iter_modules(constants.__path__)
    ]
    found = {v for m in modules for v in vars(m).values() if type(v) is str}
    return sorted(found)


def _test_literals() -> list[str]:
    found: set[str] = set()
    for path in sorted(_TESTS.glob("test_embedded_graph_*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                found.add(node.value)
    return sorted(found)


def _built_queries() -> list[str]:
    """The queries the ingestor and tools format from templates."""
    queries = [
        cq.build_nodes_by_ids_query([1, 2, 3]),
        build_proximity_query([1, 2, 3]),
        cq.wrap_with_unwind(
            cq.build_missing_rel_endpoints_query(
                "Module", "qualified_name", "File", "path", 50
            )
        ),
        MemgraphDialect().show_constraints(),
        Neo4jDialect().show_constraints(),
    ]
    for label, key in sorted(NODE_UNIQUE_CONSTRAINTS.items()):
        queries += [
            cq.wrap_with_unwind(cq.build_merge_node_query(label, key)),
            cq.wrap_with_unwind(cq.build_create_node_query(label, key)),
            cq.build_node_props_query(label, key),
            cq.build_remove_node_keys_query(label, key, ["a", "b_c"]),
            cq.build_constraint_query(label, key),
        ]
        for dialect in (MemgraphDialect(), Neo4jDialect()):
            queries += [
                dialect.create_index(label, key),
                dialect.create_constraint(label, key),
                dialect.drop_constraint(label, key, "legacy"),
            ]
    for has_props in (False, True):
        ends = ("Function", "qualified_name", "CALLS", "Method", "qualified_name")
        queries += [
            cq.wrap_with_unwind(cq.build_merge_relationship_query(*ends, has_props)),
            cq.wrap_with_unwind(
                cq.build_merge_relationship_query(*ends, has_props, ("via", "kind"))
            ),
            cq.wrap_with_unwind(cq.build_create_relationship_query(*ends, has_props)),
        ]
    return queries


def _accepted(texts: Iterable[str]) -> list[str]:
    valid = []
    for text in texts:
        try:
            parse_python(text)
        except _PYTHON_ONLY_FAILURES:
            continue
        valid.append(text)
    return valid


@cache
def _corpus() -> tuple[str, ...]:
    """Every case that is not a mutation, deduplicated, in a stable order."""
    texts = [
        *_string_constants(),
        *_test_literals(),
        *_built_queries(),
        *_EDGE_CASES,
        *_INVALID,
    ]
    return tuple(dict.fromkeys(texts))


def _outcome(parser: Parser, text: str) -> Outcome | None:
    """What a parser makes of `text`: its tree, or its error; None if declined."""
    try:
        tree = parser(text)
    except CypherSyntaxError as err:
        return (type(err).__name__, err.pos, str(err))
    except _PYTHON_ONLY_FAILURES as err:
        return (type(err).__name__, str(err))
    if tree is None:
        return None
    return ("tree", repr(tree))


def _python_specific(text: str) -> bool:
    return not text.isascii() or _U in text or re.search(r"\d{19}", text) is not None


def _disagreements(
    texts: Iterable[str],
) -> list[tuple[str, Outcome | None, Outcome | None]]:
    """Cases where the port's answer is not Python's, or it declined one it should take."""
    bad = []
    for text in texts:
        expected = _outcome(parse_python, text)
        got = _outcome(rust.parse_cypher, text)
        if got is None:
            # Declined: fine for text Python reads by rules of its own, as
            # long as the engine's `parse` then gives Python's answer.
            got = _outcome(parse.__wrapped__, text)
            if not _python_specific(text):
                got = None
        if got != expected:
            bad.append((text, expected, got))
    return bad


def _assert_agree(texts: Iterable[str]) -> None:
    bad = _disagreements(texts)
    assert not bad, f"{len(bad)} disagreements, first: {bad[:3]}"


def _spans(text: str) -> list[tuple[int, int]]:
    """Each token with the whitespace and comments after it."""
    tokens = tokenize(text)
    return [(tok.pos, nxt.pos) for tok, nxt in zip(tokens, tokens[1:])]


def _mutations(seeds: list[str], count: int, seed: int) -> Iterator[str]:
    """Seeds with a token cut, doubled or swapped, or a character cut or added.

    A token's span runs to the next token, so it takes its whitespace along.
    """
    rng = random.Random(seed)
    for _ in range(count):
        text = rng.choice(seeds)
        spans = _spans(text) or [(0, len(text))]
        i, j = sorted(rng.sample(range(len(spans)), 2)) if len(spans) > 1 else (0, 0)
        (si, ei), (sj, ej) = spans[i], spans[j]
        at = rng.randrange(len(text) + 1)
        match rng.randrange(6):
            case 0:
                yield text[:si] + text[ei:]
            case 1:
                yield text[:ei] + text[si:ei] + text[ei:]
            case 2 if j > i:
                yield text[:si] + text[sj:ej] + text[ei:sj] + text[si:ei] + text[ej:]
            case 3 if i + 1 < len(spans):
                (sk, ek) = spans[i + 1]
                yield text[:si] + text[sk:ek] + text[si:ei] + text[ek:]
            case 4:
                yield text[:at] + text[at + 1 :]
            case _:
                yield text[:at] + rng.choice(_FUZZ_ALPHABET) + text[at:]


def test_the_engine_parses_with_the_extension() -> None:
    assert rust_parser() is rust.parse_cypher


def test_the_corpus_covers_cgr_queries_and_rejections() -> None:
    valid = _accepted(_corpus())
    # Guards against a collection step silently finding nothing.
    assert len(valid) >= 400
    assert len(_corpus()) - len(valid) >= 2000


def test_every_query_cgr_ships_parses_identically() -> None:
    texts = [*_accepted(_string_constants()), *_accepted(_built_queries())]
    assert all(rust.parse_cypher(t) is not None for t in texts)
    _assert_agree(texts)


def test_every_string_in_the_engine_tests_parses_identically() -> None:
    _assert_agree(_test_literals())


def test_edge_cases_parse_identically() -> None:
    assert _accepted(_EDGE_CASES) == _EDGE_CASES
    _assert_agree(_EDGE_CASES)


def test_rejections_raise_the_same_error() -> None:
    assert not _accepted(_INVALID)
    _assert_agree(_INVALID)
    # Every other string constant cgr ships is a rejection case too.
    _assert_agree(_string_constants())


def test_python_specific_text_is_declined_and_still_parses() -> None:
    for text in _DECLINED:
        assert rust.parse_cypher(text) is None, text
        assert _outcome(parse.__wrapped__, text) == _outcome(parse_python, text)


def test_mutated_queries_parse_or_fail_identically() -> None:
    seeds = list(_accepted(_corpus()))
    cases = list(_mutations(seeds, _FUZZ_CASES, _FUZZ_SEED))
    assert len(cases) == _FUZZ_CASES
    _assert_agree(cases)
    # Both outcomes must be exercised, or the mutations are too timid or
    # too wild to test anything.
    rejected = len(cases) - len(_accepted(cases))
    assert _FUZZ_CASES // 10 < rejected < _FUZZ_CASES * 9 // 10


@pytest.mark.parametrize("text", ["MATCH (n RETURN n", "RETURN 1 'é\xa0'"])
def test_engine_parse_raises_the_extensions_error(text: str) -> None:
    with pytest.raises(CypherSyntaxError) as from_engine:
        parse(text)
    with pytest.raises(CypherSyntaxError) as from_python:
        parse_python(text)
    assert (from_engine.value.pos, str(from_engine.value)) == (
        from_python.value.pos,
        str(from_python.value),
    )
