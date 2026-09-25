"""Keep LLM-generated Cypher read-only.

A generated query is untrusted: the model that writes it reads repository
content, so a prompt injection can steer it. Two layers stand between it
and the database:

* Text checks (`services.llm`) reject write keywords and disallowed
  procedure CALLs. They run on `mask_literals_and_comments` output, so a
  keyword inside a string literal is not a false positive and a procedure
  name hidden behind backticks or a comment is still seen.
* The engine itself, via `MemgraphIngestor.fetch_read_only`. Neo4j runs the
  query in a READ access-mode session and refuses any write. Memgraph has
  no read-only session, so the query is planned with EXPLAIN and
  `check_memgraph_plan` refuses it if the planner reports a write operator
  or a disallowed procedure. The plan names what will actually execute,
  whatever the query text looks like.
"""

from __future__ import annotations

import re

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


def check_memgraph_plan(plan_rows: list[str], query: str) -> None:
    """Refuse a query whose Memgraph EXPLAIN plan would write.

    Each row is one plan line such as ` * CreateNode` or
    ` | * CallProcedure<pagerank.get> {node, rank}`; branch markers carry
    no operator and are skipped.
    """
    for row in plan_rows:
        operator = _PLAN_OPERATOR.search(row)
        if operator is None:
            continue
        if operator.group(0).startswith(cs.CYPHER_PLAN_WRITE_OPERATOR_PREFIXES):
            raise ex.ReadOnlyQueryError(
                ex.READ_ONLY_WRITE_OPERATOR.format(
                    operator=operator.group(0), query=query
                )
            )
        procedure = _PLAN_PROCEDURE.search(row)
        if procedure is not None and not is_allowed_procedure(procedure.group(1)):
            raise ex.ReadOnlyQueryError(
                ex.READ_ONLY_PROCEDURE.format(name=procedure.group(1), query=query)
            )
