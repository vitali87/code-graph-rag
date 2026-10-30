"""The deterministic graph-query MCP tools (issue #1523): resolve,
definition, callers, callees, implementors, overrides, importers, tests
reaching, endpoints and remote dependencies, the agent-authored notes
(issue #1808) and the ranked context slice (issue #1536).

Each is a pure delegation to the registry's locked `_graph_query`.
"""

from typing import TYPE_CHECKING

from codebase_rag import constants as cs
from codebase_rag import graph_query
from codebase_rag.gloss import notes as gloss
from codebase_rag.gloss.anchor import ParsedSource, SourceReader, parse_source
from codebase_rag.services.provenance import head_commit
from codebase_rag.tools.semantic_search import semantic_code_search
from codebase_rag.types_defs import GraphRefusal, SemanticSearchResult

from . import constants as mcs
from .handler_base import ToolHandlerBase

if TYPE_CHECKING:
    from codebase_rag.context_slice import ContextSlice

# The relationships each cross-service tool needs for a non-empty answer:
# every one its queries MATCH, not the ones they only count or optionally
# join. All sit in the opt-in `io` group, so on a project synced without one
# an empty answer means "never recorded", not "none" (issue #2521).
_CAPTURE_READ_BY: dict[cs.MCPToolName, frozenset[cs.RelationshipType]] = {
    cs.MCPToolName.ENDPOINTS: frozenset({cs.RelationshipType.EXPOSES}),
    # A caller reaches the handler's EXPOSES through its own READS_FROM or
    # WRITES_TO, by way of RESOLVES_TO for a URL: with any of them left out
    # some callers could never have been recorded (PR #2596).
    cs.MCPToolName.ENDPOINT_CALLERS: frozenset(
        {
            cs.RelationshipType.EXPOSES,
            cs.RelationshipType.READS_FROM,
            cs.RelationshipType.WRITES_TO,
            cs.RelationshipType.RESOLVES_TO,
        }
    ),
    cs.MCPToolName.REMOTE_DEPENDENCIES: frozenset(
        {cs.RelationshipType.READS_FROM, cs.RelationshipType.WRITES_TO}
    ),
}


class GraphQueryTools(ToolHandlerBase):
    """The read-only walks of the code graph, and the notes written on it."""

    @staticmethod
    def _depth(depth: int | None) -> int:
        return max(1, min(int(depth or 1), cs.GRAPH_QUERY_MAX_DEPTH))

    async def resolve(
        self, target: str, project: str | None = None
    ) -> list[graph_query.SymbolRow] | GraphRefusal:
        return await self._graph_query(
            cs.MCPToolName.RESOLVE,
            project,
            lambda name: self._resolve_rows(name, target),
        )

    def _resolve_rows(
        self, project_name: str, target: str
    ) -> list[graph_query.SymbolRow]:
        # A line no file can have is refused as such (issue #3244); the
        # graph would only be asked about it to overflow a Bolt integer.
        location = graph_query.parse_location(target)
        if location is not None and location[1] > cs.GRAPH_MAX_SOURCE_LINE:
            path, line = location
            raise graph_query.UnknownTargetError(
                target,
                reason=mcs.MCP_RESOLVE_LINE_OUT_OF_RANGE.format(path=path, line=line),
            )
        return graph_query.resolve_or_refuse(
            self.ingestor.fetch_all, project_name, target
        )

    def _uncaptured_refusal[T](
        self, tool: cs.MCPToolName, project_name: str, rows: list[T]
    ) -> list[T] | GraphRefusal:
        # Only an EMPTY answer is questioned, so a project with rows pays no
        # extra read, and a graph with no recorded capture keeps its `[]`.
        if rows:
            return rows
        gap = graph_query.capture_gap(
            self.ingestor.fetch_all, project_name, _CAPTURE_READ_BY[tool]
        )
        if gap is None:
            return rows
        return GraphRefusal(
            error=mcs.MCP_CAPTURE_GROUP_MISSING.format(
                tool=tool,
                project=project_name,
                groups=cs.CHAR_COMMA.join(gap["groups"]),
                relationships=cs.SEPARATOR_COMMA_SPACE.join(gap["relationships"]),
                root=gap["root_path"] or cs.MCP_DEFAULT_DIRECTORY,
            )
        )

    async def definition(
        self, qualified_name: str, project: str | None = None
    ) -> graph_query.DefinitionRow | GraphRefusal:
        return await self._graph_query(
            cs.MCPToolName.DEFINITION,
            project,
            lambda name: graph_query.definition(
                self.ingestor.fetch_all,
                name,
                qualified_name,
                self._source_root_for(name),
            ),
            # Refused as `callers` refuses an unknown name (issue #3244).
            target=qualified_name,
        )

    async def endpoints(
        self, project: str | None = None
    ) -> list[graph_query.EndpointRow] | GraphRefusal:
        return await self._graph_query(
            cs.MCPToolName.ENDPOINTS,
            project,
            lambda name: self._uncaptured_refusal(
                cs.MCPToolName.ENDPOINTS,
                name,
                graph_query.endpoints(self.ingestor.fetch_all, name),
            ),
        )

    async def endpoint_callers(
        self, target: str, project: str | None = None
    ) -> list[graph_query.EndpointCallerRow] | GraphRefusal:
        return await self._graph_query(
            cs.MCPToolName.ENDPOINT_CALLERS,
            project,
            lambda name: self._endpoint_callers(name, target),
        )

    def _endpoint_callers(
        self, project_name: str, target: str
    ) -> list[graph_query.EndpointCallerRow] | GraphRefusal:
        # A target that names no endpoint is refused, as `callers` refuses an
        # unknown name: `[]` means the endpoint exists and nothing reaches it
        # (issue #3186). A project synced without what the tool reads holds no
        # endpoint at all, so that refusal names the missing capture instead.
        tool = cs.MCPToolName.ENDPOINT_CALLERS
        try:
            rows = graph_query.endpoint_callers(
                self.ingestor.fetch_all, project_name, target
            )
        except graph_query.UnknownEndpointError:
            if gap := self._uncaptured_refusal(tool, project_name, []):
                return gap
            raise
        return self._uncaptured_refusal(tool, project_name, rows)

    async def remote_dependencies(
        self, project: str | None = None
    ) -> list[graph_query.RemoteDependencyRow] | GraphRefusal:
        return await self._graph_query(
            cs.MCPToolName.REMOTE_DEPENDENCIES,
            project,
            lambda name: self._uncaptured_refusal(
                cs.MCPToolName.REMOTE_DEPENDENCIES,
                name,
                graph_query.remote_dependencies(self.ingestor.fetch_all, name),
            ),
        )

    async def callers(
        self, qualified_name: str, depth: int | None = None, project: str | None = None
    ) -> list[graph_query.CallSiteRow] | GraphRefusal:
        return await self._graph_query(
            cs.MCPToolName.CALLERS,
            project,
            lambda name: graph_query.callers(
                self.ingestor.fetch_all, name, qualified_name, self._depth(depth)
            ),
            target=qualified_name,
        )

    async def callees(
        self, qualified_name: str, depth: int | None = None, project: str | None = None
    ) -> list[graph_query.CallSiteRow] | GraphRefusal:
        return await self._graph_query(
            cs.MCPToolName.CALLEES,
            project,
            lambda name: graph_query.callees(
                self.ingestor.fetch_all, name, qualified_name, self._depth(depth)
            ),
            target=qualified_name,
        )

    async def implementors(
        self, qualified_name: str, project: str | None = None, depth: int | None = None
    ) -> list[graph_query.RelatedRow] | GraphRefusal:
        return await self._graph_query(
            cs.MCPToolName.IMPLEMENTORS,
            project,
            lambda name: graph_query.implementors(
                self.ingestor.fetch_all, name, qualified_name, self._depth(depth)
            ),
            target=qualified_name,
        )

    async def overrides(
        self, qualified_name: str, project: str | None = None, depth: int | None = None
    ) -> list[graph_query.RelatedRow] | GraphRefusal:
        return await self._graph_query(
            cs.MCPToolName.OVERRIDES,
            project,
            lambda name: graph_query.overrides(
                self.ingestor.fetch_all, name, qualified_name, self._depth(depth)
            ),
            target=qualified_name,
        )

    async def importers(
        self,
        module_qualified_name: str,
        project: str | None = None,
        through_reexports: bool = False,
    ) -> (
        list[graph_query.ImporterRow]
        | list[graph_query.ReexportImporterRow]
        | GraphRefusal
    ):
        # A single `return await` keeps the handler a pure delegation to the
        # locked `_graph_query`, so the read is serialised against a rebuild
        # (issue #1471) whichever query the flag picks.
        return await self._graph_query(
            cs.MCPToolName.IMPORTERS,
            project,
            lambda name: (
                graph_query.importers_through_reexports
                if through_reexports
                else graph_query.importers
            )(self.ingestor.fetch_all, name, module_qualified_name),
            target=module_qualified_name,
        )

    async def tests_reaching(
        self, qualified_name: str, project: str | None = None
    ) -> list[graph_query.TestReachRow] | GraphRefusal:
        return await self._graph_query(
            cs.MCPToolName.TESTS_REACHING,
            project,
            lambda name: graph_query.tests_reaching(
                self.ingestor.fetch_all, name, qualified_name
            ),
            target=qualified_name,
        )

    # --- agent-authored notes (issue #1808) -----------------------------------

    def _commit_sha_for(self, project_name: str) -> str | None:
        # Recorded only when the graph project was indexed from this server's
        # checkout: another project's HEAD is not this repository's HEAD, and
        # a note stamped with the wrong commit is worse than one without.
        root = self._source_root_for(project_name)
        return head_commit(root) if root is not None else None

    async def annotate(
        self,
        target: str,
        body: str,
        kind: str,
        mentions: str | None = None,
        author: str | None = None,
        project: str | None = None,
    ) -> gloss.GlossRow | gloss.GlossRefusal | GraphRefusal:
        # Runs under the ingestor lock like every graph read, so the write
        # cannot interleave with an index or reingest deleting its subject.
        return await self._graph_query(
            cs.MCPToolName.ANNOTATE,
            project,
            lambda name: gloss.write_gloss(
                self.ingestor.fetch_all,
                self.ingestor.execute_write,
                name,
                target,
                body,
                kind,
                mentions,
                author,
                self._commit_sha_for(name),
                self._source_reader_for(name),
            ),
        )

    def _source_reader_for(self, project_name: str) -> SourceReader | None:
        # The note's text-quote anchor needs the subject's file, read from
        # disk only under the same rule as `definition`'s source: the
        # project must have been indexed from this server's checkout.
        root = self._source_root_for(project_name)
        if root is None:
            return None

        def read(project: str, path: str) -> ParsedSource | None:
            if project != project_name:
                return None
            target = (root / path).resolve()
            if root not in (target, *target.parents):
                return None
            try:
                text = target.read_text(encoding=cs.ENCODING_UTF8, errors="replace")
            except OSError:
                return None
            return ParsedSource(text, parse_source(self.parsers, target, text))

        return read

    async def glosses(
        self, target: str, project: str | None = None
    ) -> gloss.GlossesResult | gloss.GlossRefusal | GraphRefusal:
        return await self._graph_query(
            cs.MCPToolName.GLOSSES,
            project,
            lambda name: gloss.glosses_for(self.ingestor.fetch_all, name, target),
        )

    async def context(
        self,
        target: str,
        budget_tokens: int = cs.CONTEXT_DEFAULT_BUDGET,
        project: str | None = None,
    ) -> "ContextSlice | GraphRefusal":
        return await self._graph_query(
            cs.MCPToolName.CONTEXT,
            project,
            lambda name: self._run_context(name, target, budget_tokens),
        )

    def _run_context(
        self, project_name: str, target: str, budget_tokens: int
    ) -> "ContextSlice | GraphRefusal":
        from codebase_rag.context_slice import context as build_context

        # The CLI enforces this with typer's min=1; without the same check an
        # MCP caller's zero or negative budget returns an empty slice that
        # reads as success. Checked here rather than in `context` so that
        # handler stays a pure delegation to the locked `_graph_query`.
        if budget_tokens < 1:
            return GraphRefusal(
                error=mcs.MCP_CONTEXT_BUDGET_INVALID.format(budget=budget_tokens)
            )

        # Scoped to the project `_graph_query` resolved, not the optional
        # argument: an omitted `project` would otherwise search every project
        # and resolve to a name the project-scoped reads cannot find.
        def search(text: str) -> list[SemanticSearchResult]:
            return semantic_code_search(self.ingestor, text, project=project_name)

        return build_context(
            self.ingestor.fetch_all,
            project_name,
            target,
            budget_tokens,
            self._source_root_for(project_name),
            search=search if self._semantic_search_tool is not None else None,
        )
