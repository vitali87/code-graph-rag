"""Keep LLM-generated Cypher read-only.

A generated query is untrusted: the model that writes it reads repository
content, so a prompt injection can steer it. Two layers stand between it
and the database:

* Text checks (`services.llm`) reject write keywords and disallowed
  procedure CALLs. They run on `mask_literals_and_comments` output, so a
  keyword inside a string literal is not a false positive and a procedure
  name hidden behind backticks or a comment is still seen.
* The engine itself, via `MemgraphIngestor.fetch_read_only`: the query is
  planned with EXPLAIN and `check_plan` refuses it unless every operator the
  planner reports is a known read operator and every procedure is allowed. The plan names what will
  actually execute, whatever the query text looks like. Neo4j also runs the
  query in a READ access-mode session, but its driver documents that mode
  as routing, not access control, so the plan check is the boundary on
  both engines.
"""

from __future__ import annotations

import re
from typing import NamedTuple

from .. import constants as cs
from .. import exceptions as ex

_BACKTICK = cs.CYPHER_BACKTICK
_PLAN_OPERATOR = re.compile(cs.CYPHER_PLAN_OPERATOR_PATTERN)
_PLAN_PROCEDURE = re.compile(cs.CYPHER_PLAN_PROCEDURE_PATTERN)


def mask_literals_and_comments(query: str) -> str:
    """Blank string literals, drop comments and unquote backtick identifiers.

    The result is for inspection only, never for execution. A literal
    becomes `''` so its contents cannot trip a keyword check, a comment
    becomes a space so it cannot split or hide a token, and a backtick
    identifier keeps its text without the quotes, so ``CALL `mg.x`()``
    reads as `CALL mg.x()`. An unterminated literal or comment is left
    as-is: the engine rejects it anyway, and masking it could hide a real
    keyword from the checks.
    """
    out: list[str] = []
    i = 0
    n = len(query)
    while i < n:
        if query.startswith(cs.CYPHER_LINE_COMMENT, i):
            end = query.find(cs.CYPHER_LINE_END, i)
            i = n if end == -1 else end
            out.append(cs.CYPHER_MASKED_COMMENT)
        elif query.startswith(cs.CYPHER_BLOCK_COMMENT_OPEN, i):
            end = query.find(
                cs.CYPHER_BLOCK_COMMENT_CLOSE, i + len(cs.CYPHER_BLOCK_COMMENT_OPEN)
            )
            if end == -1:
                out.append(query[i:])
                break
            i = end + len(cs.CYPHER_BLOCK_COMMENT_CLOSE)
            out.append(cs.CYPHER_MASKED_COMMENT)
        elif query[i] in cs.CYPHER_STRING_QUOTES:
            end = _string_end(query, i)
            if end == -1:
                out.append(query[i:])
                break
            out.append(cs.CYPHER_MASKED_LITERAL)
            i = end + 1
        elif query[i] == _BACKTICK:
            end, name = _backtick_identifier(query, i)
            if end == -1:
                out.append(query[i:])
                break
            out.append(name)
            i = end + 1
        else:
            out.append(query[i])
            i += 1
    return "".join(out)


def _string_end(query: str, start: int) -> int:
    """Index of the quote closing the literal opened at `start`, or -1."""
    quote = query[start]
    i = start + 1
    while i < len(query):
        if query[i] == cs.CYPHER_STRING_ESCAPE:
            i += 2
            continue
        if query[i] == quote:
            return i
        i += 1
    return -1


def _backtick_identifier(query: str, start: int) -> tuple[int, str]:
    """End index and unquoted text of the identifier opened at `start`.

    A doubled backtick inside the identifier is a literal backtick.
    """
    parts: list[str] = []
    i = start + 1
    while i < len(query):
        if query[i] == _BACKTICK:
            if query.startswith(_BACKTICK * 2, i):
                parts.append(_BACKTICK)
                i += 2
                continue
            return i, "".join(parts)
        parts.append(query[i])
        i += 1
    return -1, ""


def is_allowed_procedure(name: str) -> bool:
    """Whether a generated query may CALL this procedure."""
    return name not in cs.CYPHER_DENIED_PROCEDURES and name.startswith(
        tuple(cs.CYPHER_ALLOWED_PROCEDURE_PREFIXES)
    )


class PlanOperator(NamedTuple):
    """One operator of an EXPLAIN plan, engine-neutral."""

    name: str
    # The called procedure, for a procedure-call operator whose name could
    # be read; None otherwise.
    procedure: str | None


def memgraph_plan_operators(plan_rows: list[str]) -> list[PlanOperator]:
    """Operators of a Memgraph EXPLAIN plan, one text row per operator.

    Rows look like ` * CreateNode` or
    ` | * CallProcedure<pagerank.get> {node, rank}`; branch markers carry no
    operator and are skipped.
    """
    operators = []
    for row in plan_rows:
        operator = _PLAN_OPERATOR.search(row)
        if operator is None:
            continue
        procedure = _PLAN_PROCEDURE.search(row)
        operators.append(
            PlanOperator(operator.group(0), procedure.group(1) if procedure else None)
        )
    return operators


def neo4j_plan_operators(plan: list[tuple[str, str]]) -> list[PlanOperator]:
    """Operators of a Neo4j EXPLAIN plan, as (operatorType, Details) pairs.

    Neo4j suffixes each type with its runtime (`Create@neo4j`), and gives a
    procedure call's signature in Details (`db.labels() :: (label :: STRING)`).
    """
    operators = []
    for operator_type, details in plan:
        name = operator_type.split(cs.CYPHER_PLAN_OPERATOR_RUNTIME_SEPARATOR, 1)[0]
        procedure = None
        if name in cs.CYPHER_PLAN_PROCEDURE_OPERATORS and details:
            procedure = details.split(cs.CYPHER_PLAN_PROCEDURE_ARGS_OPEN, 1)[0].strip()
        operators.append(PlanOperator(name, procedure or None))
    return operators


def check_plan(operators: list[PlanOperator], query: str) -> None:
    """Refuse a query unless its EXPLAIN plan provably only reads.

    Fails closed throughout: every operator must be a known read operator,
    every procedure call must name an allowed procedure, and an empty plan
    is refused rather than run on the assumption that it reads.
    """
    if not operators:
        raise ex.ReadOnlyQueryError(ex.READ_ONLY_UNREADABLE_PLAN.format(query=query))
    for operator in operators:
        if operator.name in cs.CYPHER_PLAN_PROCEDURE_OPERATORS:
            if operator.procedure is None or not is_allowed_procedure(
                operator.procedure
            ):
                raise ex.ReadOnlyQueryError(
                    ex.READ_ONLY_PROCEDURE.format(
                        name=operator.procedure or operator.name, query=query
                    )
                )
        elif not _is_read_operator(operator.name):
            raise ex.ReadOnlyQueryError(
                ex.READ_ONLY_UNKNOWN_OPERATOR.format(
                    operator=operator.name, query=query
                )
            )


def _is_read_operator(name: str) -> bool:
    # Neo4j qualifies some operators with a mode: `Expand(All)`, `Expand(Into)`.
    base = name.split(cs.CYPHER_PLAN_PROCEDURE_ARGS_OPEN, 1)[0]
    return (
        base in cs.CYPHER_PLAN_READ_OPERATORS
        or base.startswith(cs.CYPHER_PLAN_READ_OPERATOR_PREFIXES)
        or base.endswith(cs.CYPHER_PLAN_READ_OPERATOR_SUFFIXES)
    )
