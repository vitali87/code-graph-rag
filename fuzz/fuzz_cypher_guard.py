"""Fuzz the read-only guard on LLM-generated Cypher.

A generated query is untrusted: the model writing it reads repository content,
so a prompt injection can steer it. `services.cypher_guard` and the validators
in `services.llm` are what keep such a query from writing to, or exhausting,
the graph. Three modes, chosen by the first byte:

* raw -- any text through the masker, the response cleaner, the validators and
  both plan parsers. Only the exceptions those functions document may escape.
* query -- a query assembled from tokens whose masked form is known: string
  literals with escapes, both comment forms, backtick identifiers, write
  keywords split by comments, styled procedure CALLs and variable-length
  relationships. The masker must produce exactly the expected text, a write
  keyword or a disallowed procedure in code position must be rejected, and
  the range check must reject exactly the queries holding an unbounded path.
* plan -- operator lists rendered as Memgraph plan rows and Neo4j plan pairs.
  Both parsers must recover the operators, and `check_plan` must refuse any
  plan holding a documented write operator or a disallowed procedure, and
  accept one made only of known reads and allowed procedures.

The input is a stream of records, `kind sep arg len payload[len]`, read from
the front, so a seed is a few bytes written by hand in `build_corpus.py`.

Run locally (Linux; atheris does not build against Apple Clang):

    uv run --extra fuzz python fuzz/fuzz_cypher_guard.py -max_total_time=60
"""

import sys
from collections.abc import Iterator
from typing import NamedTuple

import atheris
from loguru import logger

with atheris.instrument_imports():
    from codebase_rag import constants as cs
    from codebase_rag import exceptions as ex
    from codebase_rag.services.cypher_guard import (
        PlanOperator,
        check_plan,
        is_allowed_procedure,
        mask_literals_and_comments,
        memgraph_plan_operators,
        neo4j_plan_operators,
    )
    from codebase_rag.services.llm import (
        _clean_cypher_response,
        _validate_call_procedures,
        _validate_cypher_read_only,
        _validate_no_unbounded_paths,
    )

MODES = 3
MODE_RAW, MODE_QUERY, MODE_PLAN = range(MODES)
PLAN_CHOICES = 4
PLAN_READ, PLAN_WRITE, PLAN_PROCEDURE, PLAN_UNKNOWN = range(PLAN_CHOICES)
HEADER = 4
MAX_PAYLOAD = 48

# (raw, masked). Every separator starts with whitespace, so a token ending in
# `/` never fuses with a following `/` or `*` into a comment opener.
SEPARATORS: tuple[tuple[str, str], ...] = (
    (" ", " "),
    ("\n", "\n"),
    ("\t", "\t"),
    (" /*c*/ ", "   "),
    (" //c\n", "  \n"),
)

# Code tokens free of quotes, backticks and comment openers, so each masks to
# itself. None spells a write keyword, opens a variable-length relationship or
# calls a procedure, and `OFFSET` and `SETTINGS` hold `SET` inside a word.
CODE_WORDS: tuple[str, ...] = (
    "MATCH",
    "(n)",
    "(m:Function)",
    "-[:CALLS]->",
    "RETURN",
    "n.name",
    "WHERE",
    "=",
    "<>",
    "CONTAINS",
    "STARTS WITH",
    "LIMIT",
    "10",
    "{",
    "}",
    ",",
    ":",
    "WITH",
    "count(n)",
    "AS",
    "OPTIONAL",
    "ORDER BY",
    "DESC",
    "*",
    "/",
    "-",
    "1.5",
    "[1, 2]",
    "OFFSET",
    "SETTINGS",
)

KEYWORDS: tuple[str, ...] = tuple(sorted(cs.CYPHER_DANGEROUS_KEYWORDS))

# Allowed, denied inside an allowed family, and outside every family.
PROCEDURES: tuple[str, ...] = (
    "pagerank.get",
    "schema.node_type_properties",
    "schema.assert",
    "graph_util.chain_nodes",
    "mg.load_all",
    "apoc.periodic.iterate",
    "dbms.security.createUser",
    "db.create.setNodeVectorProperty",
)

# (bounds, unbounded). A bare `*`, an open range, or a properties map with no
# hop count is unbounded; a hop count or an upper-bounded range is not.
VARLEN_BOUNDS: tuple[tuple[str, bool], ...] = (
    ("*", True),
    ("*1..", True),
    ("*..", True),
    ("* ..", True),
    ("*{w: 1}", True),
    ("*2", False),
    ("*1..3", False),
    ("*..3", False),
    ("* 1 .. 3", False),
    ("*1..3 {w: 1}", False),
)

READ_OPERATORS: tuple[str, ...] = tuple(sorted(cs.CYPHER_PLAN_READ_OPERATORS))

# Operators that change the database, from each engine's operator reference.
# Kept here rather than derived from the guard: the guard holds an allowlist,
# and this is the independent statement of what it must never let through.
WRITE_OPERATORS: tuple[str, ...] = (
    # Memgraph
    "CreateNode",
    "CreateExpand",
    "Delete",
    "SetProperty",
    "SetProperties",
    "SetLabels",
    "RemoveProperty",
    "RemoveLabels",
    "Merge",
    "Foreach",
    "LoadCsv",
    "PeriodicCommit",
    "PeriodicSubquery",
    # Neo4j
    "Create",
    "DeleteNode",
    "DeleteRelationship",
    "DeletePath",
    "DeleteExpression",
    "DetachDelete",
    "DetachDeleteNode",
    "DetachDeletePath",
    "DetachDeleteExpression",
    "LockingMerge",
    "MergeCreateNode",
    "MergeCreateRelationship",
    "SetNodeProperty",
    "SetNodeProperties",
    "SetNodePropertiesFromMap",
    "SetPropertiesFromMap",
    "SetRelationshipProperty",
    "SetRelationshipProperties",
    "SetRelationshipPropertiesFromMap",
    "LoadCSV",
    "TransactionApply",
    "TransactionForeach",
    "CreateIndex",
    "DropIndex",
    "CreateConstraint",
    "DropConstraint",
)

# Neo4j decorates an operator with a mode and a runtime.
NEO4J_MODES: tuple[str, ...] = ("", "(All)", "(Into)")
NEO4J_RUNTIMES: tuple[str, ...] = ("", "@neo4j", "@slotted", "@pipelined")
MEMGRAPH_BRANCHES: tuple[str, ...] = (" * ", " | * ", " | | * ")

PROCEDURE_OPERATOR_MEMGRAPH = "CallProcedure"
PROCEDURE_OPERATOR_NEO4J = "ProcedureCall"


class Record(NamedTuple):
    kind: int
    sep: int
    arg: int
    payload: bytes


class Token(NamedTuple):
    raw: str
    masked: str
    keyword: bool = False
    procedure: str | None = None
    unbounded: bool = False
    # Whether the masked text is only code words and blanks, so no validator
    # has anything to reject.
    inert: bool = False


def records(data: bytes) -> Iterator[Record]:
    i = 0
    while i + HEADER <= len(data):
        kind, sep, arg, length = data[i : i + HEADER]
        i += HEADER
        length %= MAX_PAYLOAD + 1
        yield Record(kind, sep, arg, data[i : i + length])
        i += length


def _text(payload: bytes) -> str:
    return payload.decode(errors="replace")


def _literal(record: Record) -> Token:
    quote = cs.CYPHER_STRING_QUOTES[record.arg % len(cs.CYPHER_STRING_QUOTES)]
    escape = cs.CYPHER_STRING_ESCAPE
    body = "".join(
        escape + char if char in (quote, escape) else char
        for char in _text(record.payload)
    )
    return Token(quote + body + quote, cs.CYPHER_MASKED_LITERAL, inert=True)


def _line_comment(record: Record) -> Token:
    body = _text(record.payload).replace(cs.CYPHER_LINE_END, " ")
    return Token(
        cs.CYPHER_LINE_COMMENT + body + cs.CYPHER_LINE_END,
        cs.CYPHER_MASKED_COMMENT + cs.CYPHER_LINE_END,
        inert=True,
    )


def _block_comment(record: Record) -> Token:
    close = cs.CYPHER_BLOCK_COMMENT_CLOSE
    body = _text(record.payload).replace(close, close[0] + " " + close[1:])
    return Token(
        cs.CYPHER_BLOCK_COMMENT_OPEN + body + close,
        cs.CYPHER_MASKED_COMMENT,
        inert=True,
    )


def _backticked(name: str) -> str:
    tick = cs.CYPHER_BACKTICK
    return tick + name.replace(tick, tick * 2) + tick


def _identifier(record: Record) -> Token:
    name = _text(record.payload)
    return Token(_backticked(name), name)


def _keyword(record: Record) -> Token:
    keyword = KEYWORDS[record.arg % len(KEYWORDS)]
    styled = (str.upper, str.lower, str.title)[record.sep % 3](keyword)
    join = record.payload[0] % len(SEPARATORS) if record.payload else 0
    raw_join, masked_join = SEPARATORS[join]
    words = styled.split()
    return Token(raw_join.join(words), masked_join.join(words), keyword=True)


def _procedure(record: Record) -> Token:
    name = PROCEDURES[record.arg % len(PROCEDURES)]
    parts = name.split(".")
    style = record.payload[0] % 6 if record.payload else 0
    raw, masked = {
        0: (name, name),
        1: (_backticked(name), name),
        2: (".".join(_backticked(p) for p in parts), name),
        3: (" . ".join(parts), " . ".join(parts)),
        4: ("/*x*/.".join(parts), " .".join(parts)),
        5: ("//x\n.".join(parts), " \n.".join(parts)),
    }[style]
    call = ("CALL", "call", "Call")[record.sep % 3]
    sep_raw, sep_masked = SEPARATORS[record.sep % len(SEPARATORS)]
    return Token(
        call + sep_raw + raw + "()",
        call + sep_masked + masked + "()",
        procedure=name,
    )


def _relationship(record: Record) -> Token:
    bounds, unbounded = VARLEN_BOUNDS[record.arg % len(VARLEN_BOUNDS)]
    shape = record.sep % 4
    if shape == 0:
        variable = masked_variable = ""
    elif shape == 1:
        variable = masked_variable = "r"
    elif shape == 2:
        variable = masked_variable = "r:CALLS|IMPORTS"
    else:
        name = _text(record.payload)
        variable, masked_variable = _backticked(name), name
    return Token(
        "-[" + variable + bounds + "]->",
        "-[" + masked_variable + bounds + "]->",
        unbounded=unbounded,
    )


def _code(record: Record) -> Token:
    word = CODE_WORDS[record.arg % len(CODE_WORDS)]
    return Token(word, word, inert=True)


TOKEN_KINDS = (
    _code,
    _literal,
    _line_comment,
    _block_comment,
    _identifier,
    _keyword,
    _procedure,
    _relationship,
)


def build_query(data: bytes) -> tuple[str, str, list[Token]]:
    """The query, its expected masked form, and the tokens it was built from."""
    raw: list[str] = []
    masked: list[str] = []
    tokens: list[Token] = []
    for record in records(data):
        token = TOKEN_KINDS[record.kind % len(TOKEN_KINDS)](record)
        sep_raw, sep_masked = SEPARATORS[record.sep % len(SEPARATORS)]
        raw += [token.raw, sep_raw]
        masked += [token.masked, sep_masked]
        tokens.append(token)
    return "".join(raw), "".join(masked), tokens


VALIDATORS = (
    _validate_cypher_read_only,
    _validate_no_unbounded_paths,
    _validate_call_procedures,
)


def _rejects(validator: object, query: str) -> bool:
    try:
        validator(query)  # type: ignore[operator]
    except ex.LLMGenerationError:
        return True
    return False


def _check_query(data: bytes) -> None:
    query, expected, tokens = build_query(data)
    masked = mask_literals_and_comments(query)
    if masked != expected:
        raise AssertionError(f"masked {query!r} to {masked!r}, expected {expected!r}")
    if any(t.keyword for t in tokens) and not _rejects(
        _validate_cypher_read_only, query
    ):
        raise AssertionError(f"write keyword accepted: {query!r}")
    if any(
        t.procedure is not None and not is_allowed_procedure(t.procedure)
        for t in tokens
    ) and not _rejects(_validate_call_procedures, query):
        raise AssertionError(f"disallowed procedure accepted: {query!r}")
    # Only a relationship token puts `[` and `*` together once names, literals
    # and comments are masked, so the range check's verdict is exact here.
    unbounded = any(t.unbounded for t in tokens)
    if _rejects(_validate_no_unbounded_paths, query) != unbounded:
        verdict = "unbounded path accepted" if unbounded else "bounded path rejected"
        raise AssertionError(f"{verdict}: {query!r}")
    if all(t.inert for t in tokens):
        for validator in VALIDATORS:
            if _rejects(validator, query):
                raise AssertionError(f"{validator.__name__} rejected {query!r}")


class PlannedOperator(NamedTuple):
    name: str
    procedure: str | None
    writes: bool
    # A known read operator or an allowed procedure; an invented name is
    # neither, and the guard may refuse it either way.
    reads: bool

    @property
    def in_memgraph(self) -> bool:
        # Memgraph names its operators in letters only; a Neo4j name such as
        # `Top1WithTies` never appears in a Memgraph plan row.
        return self.procedure is not None or self.name.isalpha()


def build_plan(
    data: bytes,
) -> tuple[list[PlannedOperator], list[str], list[tuple[str, str]]]:
    """Operators, then their Memgraph plan rows and Neo4j (type, details) pairs."""
    planned: list[PlannedOperator] = []
    rows: list[str] = []
    pairs: list[tuple[str, str]] = []
    for record in records(data):
        choice = record.kind % PLAN_CHOICES
        if choice == PLAN_READ:
            name = READ_OPERATORS[record.arg % len(READ_OPERATORS)]
            planned.append(PlannedOperator(name, None, writes=False, reads=True))
        elif choice == PLAN_WRITE:
            name = WRITE_OPERATORS[record.arg % len(WRITE_OPERATORS)]
            planned.append(PlannedOperator(name, None, writes=True, reads=False))
        elif choice == PLAN_PROCEDURE:
            procedure = PROCEDURES[record.arg % len(PROCEDURES)]
            allowed = is_allowed_procedure(procedure)
            planned.append(
                PlannedOperator("", procedure, writes=not allowed, reads=allowed)
            )
        else:
            name = "".join(chr(ord("A") + b % 26) for b in record.payload) or "X"
            planned.append(PlannedOperator(name, None, writes=False, reads=False))
        branch = MEMGRAPH_BRANCHES[record.sep % len(MEMGRAPH_BRANCHES)]
        mode = NEO4J_MODES[record.sep % len(NEO4J_MODES)]
        runtime = NEO4J_RUNTIMES[record.arg % len(NEO4J_RUNTIMES)]
        current = planned[-1]
        if current.procedure is not None:
            rows.append(
                f"{branch}{PROCEDURE_OPERATOR_MEMGRAPH}<{current.procedure}> {{x}}"
            )
            pairs.append(
                (
                    PROCEDURE_OPERATOR_NEO4J + runtime,
                    f"{current.procedure}() :: (x :: STRING)",
                )
            )
        else:
            if current.in_memgraph:
                rows.append(f"{branch}{current.name}")
            pairs.append((current.name + mode + runtime, ""))
    return planned, rows, pairs


def _refused(operators: list[PlanOperator]) -> bool:
    try:
        check_plan(operators, "q")
    except ex.ReadOnlyQueryError:
        return True
    return False


def _check_plan(data: bytes) -> None:
    planned, rows, pairs = build_plan(data)
    for engine, operators, procedure_operator, shown in (
        (
            "memgraph",
            memgraph_plan_operators(rows),
            PROCEDURE_OPERATOR_MEMGRAPH,
            [p for p in planned if p.in_memgraph],
        ),
        ("neo4j", neo4j_plan_operators(pairs), PROCEDURE_OPERATOR_NEO4J, planned),
    ):
        expected = [
            PlanOperator(procedure_operator, p.procedure)
            if p.procedure is not None
            else PlanOperator(p.name, None)
            for p in shown
        ]
        bare = [
            PlanOperator(
                o.name.split(cs.CYPHER_PLAN_PROCEDURE_ARGS_OPEN, 1)[0], o.procedure
            )
            for o in operators
        ]
        if bare != expected:
            raise AssertionError(f"{engine} parsed {rows or pairs} as {operators}")
        refused = _refused(operators)
        if not shown and not refused:
            raise AssertionError(f"{engine}: an empty plan was accepted")
        if any(p.writes for p in shown) and not refused:
            raise AssertionError(f"{engine}: a writing plan was accepted: {operators}")
        if shown and all(p.reads for p in shown) and refused:
            raise AssertionError(f"{engine}: a read-only plan was refused: {operators}")


def _check_raw(data: bytes) -> None:
    text = data.decode(errors="replace")
    mask_literals_and_comments(text)
    cleaned = _clean_cypher_response(text)
    if not cleaned.endswith(cs.CYPHER_SEMICOLON):
        raise AssertionError(f"cleaned response lacks its terminator: {cleaned!r}")
    for validator in VALIDATORS:
        _rejects(validator, cleaned)
    lines = text.splitlines()
    pairs = [tuple((line.split("\t", 1) + [""])[:2]) for line in lines]
    for operators in (memgraph_plan_operators(lines), neo4j_plan_operators(pairs)):
        refused = _refused(operators)
        if not refused and any(
            o.name.split(cs.CYPHER_PLAN_PROCEDURE_ARGS_OPEN, 1)[0] in WRITE_OPERATORS
            for o in operators
        ):
            raise AssertionError(f"a writing plan was accepted: {operators}")


def fuzz_cypher_guard(data: bytes) -> None:
    if not data:
        return
    mode, body = data[0] % MODES, data[1:]
    if mode == MODE_RAW:
        _check_raw(body)
    elif mode == MODE_QUERY:
        _check_query(body)
    else:
        _check_plan(body)


def main() -> None:
    logger.disable("codebase_rag")
    atheris.Setup(sys.argv, atheris.instrument_func(fuzz_cypher_guard))
    atheris.Fuzz()


if __name__ == "__main__":
    main()
