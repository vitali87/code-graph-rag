from __future__ import annotations

import asyncio
import re

from loguru import logger
from pydantic_ai import Tool
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from .. import constants as cs
from .. import exceptions as ex
from .. import logs as ls
from ..config import settings
from ..constants import (
    QUERY_NOT_AVAILABLE,
    QUERY_RESULTS_PANEL_TITLE,
    QUERY_SUMMARY_DB_ERROR,
    QUERY_SUMMARY_SUCCESS,
    QUERY_SUMMARY_TIMEOUT,
    QUERY_SUMMARY_TRANSLATION_FAILED,
    QUERY_SUMMARY_TRUNCATED,
    QUERY_SUMMARY_UNSCOPEABLE,
)
from ..schemas import QueryGraphData
from ..services import QueryProtocol
from ..services.llm import CypherGenerator
from ..types_defs import ResultRow
from ..utils.token_utils import truncate_results_by_tokens
from . import tool_descriptions as td

# A PROJECTED PROPERTY: `n.qualified_name`, with nothing appended. The
# trailing boundary is what rejects `AS qualified_name_of_thing`, and the
# required `<identifier>.` prefix is what rejects a bare string literal.
_PROJECTED_QUALIFIED_NAME_RE = re.compile(
    rf"[A-Z_][A-Z0-9_]*\.{cs.CYPHER_QUALIFIED_NAME_TOKEN}(?![A-Z0-9_])"
)

# A Cypher property read, `<entity>.<property>`. Grouping a projection by
# the entity half says which entities a row exposes, so an entity that
# contributes properties without its qualified name can be spotted.
_PROPERTY_READ_RE = re.compile(r"\b([A-Z_][A-Z0-9_]*)\.([A-Z_][A-Z0-9_]*)")

# A bare entity inside an aggregate, `count(b)`. Such an entity is
# MEASURED without any property being read, so it needs attributing too.
# `count(b.name)` is caught by the property-read pattern instead.
_AGGREGATED_ENTITY_RE = re.compile(
    r"\b(?:COUNT|SUM|AVG|MIN|MAX|COLLECT)\(\s*(?:DISTINCT\s+)?([A-Z_][A-Z0-9_]*)\s*\)"
)

# Any entity MEASURED by an aggregate, in either spelling: `count(b)` and
# `count(b.qualified_name)` both report on `b`. The property suffix is an
# optional tail on one pattern rather than a second alternative, so the two
# forms cannot drift apart and the captured group is the entity either way.
_AGGREGATED_READ_RE = re.compile(
    r"\b(?:COUNT|SUM|AVG|MIN|MAX|COLLECT)\(\s*(?:DISTINCT\s+)?"
    r"([A-Z_][A-Z0-9_]*)(?:\.[A-Z_][A-Z0-9_]*)?\s*\)"
)

# Constructs a scoped query may not use, matched as whole words.
_UNANALYSABLE_RE = re.compile(cs.CYPHER_UNANALYSABLE_PATTERN)

# Text that returns nothing: quoted strings (a constant, not a read) and
# comments (not executed). Blanked before textual analysis so neither is
# mistaken for a property read.
_INERT_TEXT_RE = re.compile(
    r"'(?:[^'\\]|\\.)*'"
    r'|"(?:[^"\\]|\\.)*"'
    r"|//[^\n]*"
    r"|/\*.*?\*/",
    re.DOTALL,
)

# A project name appearing as a LITERAL in the query text, uppercased by the
# caller. Used to check that an aggregate's restriction names the requested
# project rather than some other one.
_PROJECT_LITERAL_RE = re.compile(
    rf"[A-Z0-9_-]+{cs.PROJECT_NAME_DIGEST_MARKER}[0-9A-F]{{{cs.PROJECT_NAME_DIGEST_LEN}}}\.?"
)

# `derive_project_name` builds "<base>__<8 hex digits>". Requiring the whole
# shape, not just the "__" marker, is what stops `__init__` being read as a
# project-qualified name.
_PROJECT_NAME_RE = re.compile(
    rf".+{cs.PROJECT_NAME_DIGEST_MARKER}[0-9a-f]{{{cs.PROJECT_NAME_DIGEST_LEN}}}"
)


def scope_rows_to_project(
    rows: list[ResultRow], project_name: str | None
) -> list[ResultRow]:
    """`rows` restricted to one project, or unchanged when none is given.

    Enforced HERE rather than in the generated Cypher, because the model
    writes that query and may omit the filter -- which is precisely what
    made the reported cross-project bleed intermittent (issue #1494). A
    code-level filter is always-or-never.

    Every string in the row is inspected -- values, list and tuple and set
    members, and dict KEYS as well as dict values, at any depth. Not a
    fixed list of column names: the repo's own queries return
    `from_qn`/`to_qn`, and a generated query may label a column anything,
    so keying on known names failed open for exactly the shapes that leak.

    Enumerated rather than asserted. An earlier version of this sentence
    said "every string VALUE", written while describing the recursion, and
    dict keys were genuinely unreachable by it.

    A row naming no project at all is kept: `RETURN count(n)` identifies
    nobody, and discarding it would turn scoping into silent data loss.
    Such rows are also unattributable, which is why
    `requires_project_evidence` refuses a scoped query that produces them.
    """
    if not project_name:
        return rows
    prefix = f"{project_name}{cs.SEPARATOR_DOT}"
    kept: list[ResultRow] = []
    for row in rows:
        if _row_is_outside(row, prefix):
            continue
        kept.append(row)
    return kept


def requires_project_evidence(
    cypher_query: str, project_name: str | None = None
) -> bool:
    """Whether `cypher_query` returns something a project filter can judge.

    `scope_rows_to_project` decides per row, from the values it is given. A
    query like `RETURN n.name, n.path` hands it rows with no project
    evidence at all, so it cannot tell one project's rows from another's --
    and it keeps them, since it cannot prove them foreign either.

    That gap is closed here rather than there: a SCOPED request whose query
    projects no qualified name is refused, so the caller learns the scope
    could not be honoured instead of silently receiving every project.

    Evidence means a projected PROPERTY (`n.qualified_name`), not the token
    appearing somewhere: an alias like `AS qualified_name_of_thing` returns
    no qualified name at all.

    An aggregate counts only when the query ITSELF restricts by qualified
    name. `RETURN count(n)` exposes no names but does expose a MAGNITUDE
    spanning every indexed project, so a scoped caller would learn the size
    of projects they never asked about. Restricting the match keeps
    counting usable without that leak.
    """
    executable = _without_inert_text(cypher_query)
    # The restriction check needs comments gone but quoted strings kept: a
    # predicate inside a comment restricts nothing, while the project literal
    # it looks for lives inside quotes (issue #1494).
    restrictable = _without_comments(cypher_query)
    # DEFAULT-DENY ON STRUCTURE. Four review rounds found four ways to
    # satisfy a textual evidence check while returning unattributable data,
    # and two more used UNION so only the final branch was inspected.
    # Enumerating bypasses is endless -- every construct not yet considered
    # is a candidate -- so a scoped query must have the shape the prompt
    # already mandates ("MATCH, WHERE, RETURN, LIMIT" with plain aliased
    # property reads) and anything else is refused rather than analysed.
    if _UNANALYSABLE_RE.search(executable.upper()):
        return False
    projection = _return_clause(executable)
    if not projection:
        return False
    # A projection term must be a bare property read or an aggregate over
    # one. `left(b.qualified_name, 3)` mentions a qualified name without
    # attributing `b`, so a transformed term cannot count as evidence.
    if not _every_term_is_plain(projection):
        return False
    # EVERY AGGREGATED ENTITY MUST BE RESTRICTED, checked BEFORE the
    # attributability branch below, which returns early and would make this
    # unreachable for exactly the queries that need it.
    #
    # Attributability and restriction answer different questions, and only the
    # second is the right one for an aggregate. Attributability asks "does this
    # row say who it is about"; restriction asks "is this entity confined to my
    # project". For a projection of NAMES those coincide. An aggregate returns
    # no names, so `count(b.qualified_name)` satisfies attributability
    # trivially -- `b` does project its own qualified name -- while the
    # MAGNITUDE still counts every `b` in every indexed project. `collect` is
    # worse again: it returns the other projects' names themselves.
    aggregated_only = _entities_only_ever_aggregated(projection)
    if aggregated_only and not _restricts_to_project(
        restrictable, project_name, aggregated_only
    ):
        return False
    # A PROJECTED PROPERTY (`x.qualified_name`), not the bare token: an
    # ALIAS containing it -- `RETURN n.name AS qualified_name_of_thing` --
    # returns no qualified name at all, yet satisfied a substring match.
    #
    # And EVERY entity whose properties are returned must carry its own,
    # not just one of them: `RETURN a.qualified_name, b.name, b.path`
    # yields a row that is partly in-project, where `b`'s name and path
    # belong to an entity nothing attributes. One entity's qualified name
    # does not vouch for another's properties.
    if _PROJECTED_QUALIFIED_NAME_RE.search(projection):
        return _every_projected_entity_is_attributable(projection)
    # An aggregate exposes no NAMES but does expose a MAGNITUDE: a scoped
    # caller receiving `count(n)` over every indexed project learns the
    # size of projects they did not ask about. So it counts as evidence
    # only when the QUERY ITSELF restricts by qualified name -- then the
    # number is attributable and counting stays usable when scoped.
    terms = [term.strip() for term in projection.split(cs.CHAR_COMMA)]
    all_aggregates = bool(terms) and all(
        any(agg in term for agg in cs.CYPHER_AGGREGATE_TOKENS) for term in terms
    )
    if not all_aggregates:
        return False
    # The restriction must constrain the alias being COUNTED, not merely
    # exist. `MATCH (a),(b) WHERE a.qualified_name STARTS WITH "alpha."
    # RETURN count(b)` restricts `a` and counts `b`, which nothing bounds.
    counted = set(_AGGREGATED_ENTITY_RE.findall(projection))
    return _restricts_to_project(restrictable, project_name, counted)


def _without_comments(cypher_query: str) -> str:
    """`cypher_query` with COMMENTS blanked and quoted strings kept.

    What the restriction check needs, and distinct from
    `_without_inert_text`. A comment is not executed, so a project predicate
    written inside one restricts nothing -- but a quoted string IS the
    project literal the check looks for, so it has to survive.

    Reuses `_INERT_TEXT_RE` rather than adding a second pattern: it already
    alternates quoted strings BEFORE comments, so a string is consumed as a
    string and `"http://x"` is never read as a line comment. Only the
    decision differs, so it belongs in the callback, not in a new regex.
    """

    def _blank(match: re.Match[str]) -> str:
        text = match.group(0)
        if text[:1] in ("'", '"'):
            return text
        return "".join("\n" if ch == "\n" else " " for ch in text)

    return _INERT_TEXT_RE.sub(_blank, cypher_query)


def _without_inert_text(cypher_query: str) -> str:
    """`cypher_query` with quoted spans and comments blanked, same length.

    Neither returns anything. A property read inside quotes is a CONSTANT
    (`RETURN "n.qualified_name" AS lit` projects a string), and one inside
    a comment is not executed at all -- but textual matching saw both as
    reads. A trailing `// n.qualified_name` was the sharper case, since the
    projection is taken from the text after the last RETURN and the comment
    sits inside it.

    Blanking rather than deleting keeps offsets stable, so the RETURN
    marker still falls where it does in the original. Newlines are
    preserved so a `//` comment does not swallow the line after it.

    Deliberately NOT applied when looking for a literal PROJECT NAME,
    which legitimately lives inside quotes.
    """

    def _blank(match: re.Match[str]) -> str:
        return "".join("\n" if ch == "\n" else " " for ch in match.group(0))

    return _INERT_TEXT_RE.sub(_blank, cypher_query)


def _every_term_is_plain(projection: str) -> bool:
    """Whether every returned term is a bare property read or an aggregate.

    `left(b.qualified_name, 3)` mentions a qualified name without
    attributing `b` -- the value returned is a fragment, and the row filter
    would judge the row on a string that is not a qualified name at all.

    Aggregates are allowed because they name nobody; a bare alias with no
    property read (a literal, a parameter) is allowed for the same reason.
    """
    for term in projection.split(cs.CHAR_COMMA):
        reads = _PROPERTY_READ_RE.findall(term)
        if not reads:
            continue
        if any(agg in term for agg in cs.CYPHER_AGGREGATE_TOKENS):
            continue
        entity, prop = reads[0]
        # The term must be exactly `<entity>.<prop>`, optionally aliased.
        bare = term.split(cs.CYPHER_ALIAS_KEYWORD, 1)[0].strip()
        if bare != f"{entity}{cs.SEPARATOR_DOT}{prop}":
            return False
    return True


def _entities_only_ever_aggregated(projection: str) -> set[str]:
    """Entities the projection MEASURES but never returns a plain value for.

    The distinction decides whether the row filter can do its job. An entity
    also projected as a plain property (`RETURN a.qualified_name, count(a)`)
    groups the aggregate by a value the filter judges per row, so a foreign
    group is dropped whole. An entity appearing ONLY inside aggregates
    contributes no such column, so its magnitude is computed across every
    indexed project and arrives as a number nothing can attribute.

    Both aggregate spellings count as measuring: `count(b)` and
    `count(b.qualified_name)` report on `b` alike. That the second one also
    mentions a qualified name is what made it look attributable -- the value
    is consumed by the aggregate, not returned.
    """
    plain: set[str] = set()
    aggregated: set[str] = set()
    for term in projection.split(cs.CHAR_COMMA):
        if any(agg in term for agg in cs.CYPHER_AGGREGATE_TOKENS):
            aggregated.update(_AGGREGATED_READ_RE.findall(term))
            continue
        for entity, prop in _PROPERTY_READ_RE.findall(term):
            # Only the QUALIFIED NAME excuses an aggregate. Any other plain
            # property is a grouping column the row filter cannot judge:
            # `RETURN n.name, count(n.qualified_name)` returns a name and a
            # number, neither attributable, while the count still spans every
            # project. An earlier version of this accepted any plain read
            # (issue #1494).
            if prop == cs.CYPHER_QUALIFIED_NAME_TOKEN:
                plain.add(entity)
    return aggregated - plain


def _every_projected_entity_is_attributable(projection: str) -> bool:
    """Whether every entity read in `projection` also projects its own qn.

    Cypher property reads are `<entity>.<property>`, so grouping the
    projection by entity identifier says which entities the row exposes.
    An entity contributing properties WITHOUT its qualified name cannot be
    attributed to a project, and the row filter -- which judges a row as a
    whole -- will keep the row on the strength of some other entity's
    qualified name.

    Entities reading only their qualified name are fine, and so are
    literals, which name no entity at all.

    An AGGREGATED entity counts too. `RETURN a.qualified_name, count(b)`
    was accepted because `b` appears as a bare identifier inside the
    aggregate and never as a property read -- so it never entered this
    map, while the count reported how many `b` exist across every project.
    An aggregate names nobody but it still MEASURES someone.
    """
    reads: dict[str, set[str]] = {}
    for entity, prop in _PROPERTY_READ_RE.findall(projection):
        reads.setdefault(entity, set()).add(prop)
    for entity in _AGGREGATED_ENTITY_RE.findall(projection):
        reads.setdefault(entity, set())
    return all(cs.CYPHER_QUALIFIED_NAME_TOKEN in props for props in reads.values())


def _restricts_to_project(
    cypher_query: str, project_name: str | None, counted: set[str] | None = None
) -> bool:
    """Whether the query narrows `counted`'s rows to `project_name`.

    Restricting by *a* qualified name is not restricting to *yours*: a query
    filtering on another project's prefix would otherwise pass and return
    that project's count to a scoped caller -- the magnitude leak again, one
    level deeper.

    So the LITERAL project name is required. A parameter's value is
    invisible here, so treating "no literal" as "safely parameterised"
    accepted a restriction to ANY project -- and, for
    `n.qualified_name = m.qualified_name`, to another matched entity.

    Nothing useful is lost: the caller knows their own project name and
    the prompt already instructs the model to emit it, so the one shape
    this can verify is also the one it should get. Interpolation is not
    a concern here because the name is validated against `list_projects`
    before it reaches the query.
    """
    upper = cypher_query.upper()
    marker = upper.rfind(cs.CYPHER_RETURN_KEYWORD)
    body = upper if marker < 0 else upper[:marker]
    # A RESTRICTIVE predicate, not merely a mention. `WHERE
    # n.qualified_name IS NOT NULL` matches every indexed node in every
    # project, so a count over it spans them all -- yet it satisfied a
    # check that only looked for the property appearing before RETURN.
    if (
        not any(op in body for op in cs.CYPHER_PREFIX_PREDICATES)
        or _PROJECTED_QUALIFIED_NAME_RE.search(body) is None
    ):
        return False
    if not project_name:
        return False
    # A DISJUNCTION makes the restriction optional. `... STARTS WITH
    # 'alpha.' OR TRUE` contains the predicate but does not require it, and
    # every check here searches for the predicate rather than proving it
    # mandatory -- so the aggregate ran across every project while the
    # substring match was satisfied (issue #1494).
    #
    # Refused rather than analysed: deciding whether a predicate holds on
    # every Boolean path is a satisfiability question, and this module's
    # rule for shapes it cannot analyse is to reject them. `AND` is
    # unaffected, so the ordinary scoped count keeps working.
    if cs.CYPHER_DISJUNCTION in body:
        return False
    # The LITERAL project name is required. "No literal" was read as
    # "safely parameterised", but a parameter's VALUE is invisible here,
    # so that accepted a restriction to any project at all -- and, for
    # `n.qualified_name = m.qualified_name`, to another matched entity.
    literals = _PROJECT_LITERAL_RE.findall(body)
    if not literals:
        return False
    if not all(
        literal.rstrip(cs.SEPARATOR_DOT) == project_name.upper() for literal in literals
    ):
        return False
    # Every COUNTED alias must itself be restricted. A restriction on `a`
    # says nothing about `count(b)`, and the two differ whenever the query
    # matches more than one entity.
    return all(
        _alias_is_restricted(body, alias, project_name.upper())
        for alias in counted or ()
    )


def _alias_is_restricted(body: str, alias: str, project_upper: str) -> bool:
    """Whether `body` binds `alias`'s qualified name to THIS project's prefix.

    The operand is required, not just the operator. Checking only that
    `<alias>.qualified_name` was followed by a prefix predicate accepted a
    VACUOUS one -- `STARTS WITH ''` -- and the separate literal check was
    satisfied by the project name appearing anywhere, including in a
    tautology like `'proj' = 'proj'`. Together those two half-checks passed
    a query whose aggregate spanned every project (issue #1494).
    """
    operators = "|".join(re.escape(op.strip()) for op in cs.CYPHER_PREFIX_PREDICATES)
    pattern = re.compile(
        rf"\b{re.escape(alias)}\.{cs.CYPHER_QUALIFIED_NAME_TOKEN}\s*"
        rf"(?:{operators})\s*['\"]{re.escape(project_upper)}"
    )
    return pattern.search(body) is not None


def _return_clause(cypher_query: str) -> str:
    """The uppercased RETURN projection, without trailing ORDER BY / LIMIT.

    Evidence has to be in what the query RETURNS. A substring match over
    the whole query counted `WHERE n.qualified_name STARTS WITH ...` as
    evidence while the projection returned only `n.name` -- precisely the
    unattributable rows this guard exists to refuse.
    """
    upper = cypher_query.upper()
    marker = upper.rfind(cs.CYPHER_RETURN_KEYWORD)
    if marker < 0:
        return ""
    projection = upper[marker + len(cs.CYPHER_RETURN_KEYWORD) :]
    for tail in cs.CYPHER_POST_RETURN_KEYWORDS:
        cut = projection.find(tail)
        if cut >= 0:
            projection = projection[:cut]
    return projection.strip()


def _looks_like_a_qualified_name(value: str) -> bool:
    """Whether `value` is a project-qualified name rather than free text.

    `derive_project_name` produces `<base>__<8 hex digits>`, and the FULL
    shape is required -- not merely the `__` marker. `__init__` contains
    that marker, so a marker-only test discarded every row carrying
    Python's most common method name: the too-aggressive direction, which
    fails as badly as leaking.

    Matching on dots alone would be worse still, since a docstring or a
    path may contain them.
    """
    head = value.split(cs.SEPARATOR_DOT, 1)[0]
    return _PROJECT_NAME_RE.fullmatch(head) is not None


def _row_is_outside(row: ResultRow, prefix: str) -> bool:
    """Whether `row` names any project other than the one `prefix` selects.

    Every string VALUE is inspected, not a fixed list of key names. The
    repo's own queries return `from_qn`/`to_qn`/`caller_qualified_name`,
    and a generated query may label a column anything at all, so keying on
    known names would fail open for precisely the shapes that leak.

    A row naming no project at all -- `RETURN count(n)` -- is kept, since
    it identifies nothing belonging to anyone else.
    """
    return any(_names_another_project(value, prefix) for value in row.values())


def _names_another_project(value: object, prefix: str) -> bool:
    """Whether `value`, at any depth, carries a foreign qualified name.

    Cypher returns lists and maps -- `collect(m.qualified_name)`, a map
    projection -- so inspecting only top-level strings let a whole list of
    another project's names ride through untouched.
    """
    if isinstance(value, str):
        return _looks_like_a_qualified_name(value) and not value.startswith(prefix)
    if isinstance(value, dict):
        # KEYS as well as values: a map projection keyed BY qualified name
        # carries foreign names in a position `.values()` never reaches.
        return any(
            _names_another_project(item, prefix)
            for pair in value.items()
            for item in pair
        )
    if isinstance(value, list | tuple | set | frozenset):
        return any(_names_another_project(item, prefix) for item in value)
    # Scalars carry no name and are safe to keep -- `RETURN count(n)` must
    # survive scoping, and a number identifies nobody.
    if value is None or isinstance(value, bool | int | float):
        return False
    # ANYTHING ELSE FAILS CLOSED. A graph driver's Node, Relationship or Path
    # is neither a string nor a built-in container, so it fell through to a
    # permissive `return False` and rode through the filter -- then rendered
    # with `str()`, printing another project's properties under an active
    # scope. Unreadable means unattributable, which is the same rule this
    # module applies to unanalysable QUERIES; applying it to values too keeps
    # the two halves consistent (issue #1494).
    return True


def create_query_tool(
    ingestor: QueryProtocol,
    cypher_gen: CypherGenerator,
    console: Console | None = None,
    project_name: str | None = None,
) -> Tool:
    if console is None:
        console = Console(width=None, stderr=True, force_terminal=True)

    async def query_codebase_knowledge_graph(
        natural_language_query: str,
    ) -> QueryGraphData:
        logger.info(ls.TOOL_QUERY_RECEIVED.format(query=natural_language_query))
        cypher_query = QUERY_NOT_AVAILABLE
        try:
            cypher_query = await cypher_gen.generate(natural_language_query)

            results = await asyncio.wait_for(
                asyncio.to_thread(ingestor.fetch_all, cypher_query),
                timeout=settings.QUERY_TIMEOUT_S,
            )

            # A query returning no qualified name yields rows the filter
            # cannot attribute, so it keeps them. Answering a SCOPED
            # request with those would ignore the scope silently, so the
            # guard belongs here as well as in the MCP handler -- the CLI
            # is scoped too when exactly one project is active.
            if project_name is not None and not requires_project_evidence(
                cypher_query, project_name
            ):
                return QueryGraphData(
                    query_used=cypher_query,
                    results=[],
                    summary=QUERY_SUMMARY_UNSCOPEABLE.format(project=project_name),
                )

            # Before the row cap and the token truncation, so a scoped query
            # spends its budget on rows the caller can actually use rather
            # than on another project's rows that are about to be dropped.
            results = scope_rows_to_project(results, project_name)

            total_count = len(results)
            if total_count > settings.QUERY_RESULT_ROW_CAP:
                results = results[: settings.QUERY_RESULT_ROW_CAP]

            results, tokens_used, was_truncated = truncate_results_by_tokens(
                results,
                max_tokens=settings.QUERY_RESULT_MAX_TOKENS,
                original_total=total_count,
            )

            if results:
                table = Table(
                    show_header=True,
                    header_style="bold magenta",
                )
                headers = results[0].keys()
                for header in headers:
                    table.add_column(header)

                for row in results:
                    renderable_values = []
                    for value in row.values():
                        if value is None:
                            renderable_values.append("")
                        elif isinstance(value, bool):
                            renderable_values.append("✓" if value else "✗")
                        elif isinstance(value, int | float):
                            renderable_values.append(str(value))
                        else:
                            renderable_values.append(str(value))
                    table.add_row(*renderable_values)

                console.print(
                    Panel(
                        table,
                        title=QUERY_RESULTS_PANEL_TITLE,
                        expand=False,
                    )
                )

            if was_truncated or total_count > len(results):
                summary = QUERY_SUMMARY_TRUNCATED.format(
                    kept=len(results),
                    total=total_count,
                    tokens=tokens_used,
                    max_tokens=settings.QUERY_RESULT_MAX_TOKENS,
                )
            else:
                summary = QUERY_SUMMARY_SUCCESS.format(count=len(results))
            return QueryGraphData(
                query_used=cypher_query, results=results, summary=summary
            )
        except ex.LLMGenerationError as e:
            return QueryGraphData(
                query_used=QUERY_NOT_AVAILABLE,
                results=[],
                summary=QUERY_SUMMARY_TRANSLATION_FAILED.format(error=e),
            )
        except TimeoutError:
            logger.warning(
                ls.TOOL_QUERY_TIMEOUT.format(
                    timeout=settings.QUERY_TIMEOUT_S, query=cypher_query
                )
            )
            return QueryGraphData(
                query_used=cypher_query,
                results=[],
                summary=QUERY_SUMMARY_TIMEOUT.format(timeout=settings.QUERY_TIMEOUT_S),
            )
        except Exception as e:
            logger.exception(ls.TOOL_QUERY_ERROR.format(error=e))
            return QueryGraphData(
                query_used=cypher_query,
                results=[],
                summary=QUERY_SUMMARY_DB_ERROR.format(error=e),
            )

    return Tool(
        function=query_codebase_knowledge_graph,
        name=td.AgenticToolName.QUERY_GRAPH,
        description=td.CODEBASE_QUERY,
    )
