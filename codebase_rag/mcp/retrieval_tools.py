"""The MCP tools that answer from the agent's own tools: `ask_agent`, the
generated-Cypher `query_code_graph`, semantic search, duplicate detection
and the source lookups (`get_code_snippet`, `get_function_source`).

Each calls the pre-built pydantic-ai tool directly, under the ingestor lock
and behind the partial-graph refusal, since no wrapper of the registry's
sits between such a tool and the graph.
"""

from pathlib import Path

from loguru import logger
from pydantic_ai import Agent, DeferredToolRequests, Tool

from codebase_rag import constants as cs
from codebase_rag import exceptions as ex
from codebase_rag import logs as ls
from codebase_rag import tool_errors as te
from codebase_rag.services.llm import create_rag_orchestrator, run_to_text_answer
from codebase_rag.tools import tool_descriptions as td
from codebase_rag.tools.codebase_query import create_query_tool
from codebase_rag.types_defs import CodeSnippetResultDict, QueryResultDict
from codebase_rag.utils.path_utils import derive_project_name

from . import constants as mcs
from .handler_base import ToolHandlerBase, plain_function, run_in_thread


def _query_refusal(error: str) -> QueryResultDict:
    # The error once: a failed query has no query and no rows to summarise.
    return QueryResultDict(error=error, query_used=cs.QUERY_NOT_AVAILABLE, results=[])


class RetrievalTools(ToolHandlerBase):
    """The agent, the generated query, and the search and source tools."""

    def _agent_query_tool(self) -> Tool:
        """The graph-query tool the agent gets: the workspace's, if any.

        The pre-built tool is bound to the project this server's root
        derives to, which a workspace need not contain, so through
        `ask_agent` it answered for a project outside the allow-list (bot
        review on PR #1972). With a workspace the tool is bound to the
        workspace default; without a default it refuses naming the choices,
        the same answer `query_code_graph` gives a bare request.
        """
        if self.workspace is None:
            return self._query_tool
        default, scope_error = self._workspace_scope(None)
        # The workspace allow-list says which projects are SERVED, not which
        # are INDEXED. Every other project-taking tool meets the graph's own
        # unknown-project check later; this one binds the query tool up
        # front, so a served-but-unindexed default produced an empty answer
        # where the others refuse (PR #1972).
        if scope_error is None and default is not None:
            indexed = self.ingestor.list_projects()
            if default not in indexed:
                scope_error = self._unknown_project_error(default, sorted(indexed))
                default = None
        if scope_error is None and default is not None:
            return create_query_tool(
                ingestor=self.ingestor,
                cypher_gen=self.cypher_gen,
                console=self._stderr_console,
                project_name=default,
            )

        def refuse(natural_language_query: str) -> str:
            return str(scope_error)

        return Tool(
            refuse, name=td.AgenticToolName.QUERY_GRAPH, description=td.CODEBASE_QUERY
        )

    @property
    def rag_agent(self) -> Agent[None, str | DeferredToolRequests]:
        if self._rag_agent is None:
            tools = [
                self._agent_query_tool(),
                self._code_tool,
                self._file_reader_tool,
                self._file_writer_tool,
                self._file_editor_tool,
                self._shell_command_tool,
                self._directory_lister_tool,
                # The same instances the direct MCP tools use: a second copy
                # would give the orchestrator its own roots cache and let the
                # two routes disagree about a project indexed mid-session.
                # Under a workspace the project-taking ones are wrapped so
                # the agent cannot reach outside the allow-list (#1972).
                self._function_source_tool,
                self._workspace_scoped_tool(self._find_duplicates_tool),
            ]
            if self._semantic_search_tool is not None:
                tools.append(self._workspace_scoped_tool(self._semantic_search_tool))
            if self._structural_available:
                tools.append(self._structural_search_tool)
                tools.append(self._structural_editor_tool)
            self._rag_agent, _ = create_rag_orchestrator(
                tools=tools, project_root=Path(self.project_root)
            )
        return self._rag_agent

    async def ask_agent(self, question: str) -> dict[str, str]:
        logger.info(ls.MCP_ASK_AGENT.format(question=question))
        try:
            # The agent is given the RAW tool objects (self._query_tool,
            # self._code_tool, the semantic search tool), not the locked
            # handler methods on this class -- so its graph reads bypass every
            # wrapper below. The lock therefore has to be taken here, around
            # the whole run: an agent answer assembled from two graph
            # generations is wrong in a way no single tool call would reveal.
            #
            # Held for the full run rather than per tool call, because the
            # answer is composed ACROSS calls. Serialising each call
            # individually would still let a rebuild land between them, which
            # is the case this is meant to exclude.
            async with self._ingestor_lock:
                # Guarded HERE, not in a wrapper, precisely because the agent
                # holds the raw tools: there is no wrapper of ours between it
                # and the graph. And inside the lock like every other reader,
                # so a rebuild cannot land between the check and the run.
                # An agent answer is composed across several tool calls and
                # comes back with no sign that the definitions behind it were
                # missing, so a partial graph is worse here than for a single
                # read -- the caller cannot tell (PR #1547).
                project = derive_project_name(Path(self.project_root))
                if (
                    refusal := self.marker.partial_graph_refusal(
                        project, cs.MCPToolName.ASK_AGENT
                    )
                ) is not None:
                    return {cs.DICT_KEY_ERROR: refusal}
                # No operator to ask: an edit or a non-read-only command is
                # denied, and the model answers without it.
                answer = await run_to_text_answer(
                    self.rag_agent,
                    question,
                    approve=False,
                    denial=mcs.MCP_ASK_AGENT_APPROVAL_DENIED,
                )
            return {"output": answer}
        except Exception as e:  # noqa: BLE001 -- tool boundary: any failure becomes the tool's error result
            logger.error(ls.MCP_ASK_AGENT_ERROR.format(error=e))
            return {"error": mcs.MCP_ASK_AGENT_ERROR.format(error=e)}

    async def semantic_search(
        self, natural_language_query: str, top_k: int = 5, project: str | None = None
    ) -> str:
        search_tool = self._semantic_search_tool
        if search_tool is None:
            # Registered only with the semantic extra installed; a direct
            # call without it gets the answer the tool itself gives.
            return te.failure(ex.SEMANTIC_EXTRA)
        logger.info(ls.MCP_SEMANTIC_SEARCH.format(query=natural_language_query))
        # The underlying tool already accepted `project` and passes it to
        # `search_embeddings`, which filters in the vector store. Only this
        # handler failed to forward it, so scoping here is plumbing rather
        # than a second filter (issue #1494).
        project, scope_error = self._workspace_scope(project)
        if scope_error is not None:
            return scope_error
        if project is not None:
            known = await run_in_thread(self.ingestor.list_projects)
            if project not in known:
                return self._unknown_project_error(project, known)
        # The vector store is searched first, but the hits are hydrated from
        # the GRAPH, so a partial graph loses matches the embeddings still
        # know about.
        search_project = project or derive_project_name(Path(self.project_root))
        # Serialise against index/update, which delete and rebuild the graph
        # under this lock; an interleaved read mixes generations. The guard
        # is inside the lock for the same reason: an update landing between
        # the check and the read would leave an approved read running against
        # the partial graph (PR #1547).
        async with self._ingestor_lock:
            if refusal := await run_in_thread(
                self.marker.partial_graph_refusal,
                search_project,
                cs.MCPToolName.SEMANTIC_SEARCH,
            ):
                return refusal
            result = await plain_function(search_tool)(
                query=natural_language_query, top_k=top_k, project=project
            )
        if isinstance(result, te.ToolFailure):
            return result
        return str(result)

    async def find_duplicate_code(
        self,
        project: str | None = None,
        threshold: float = cs.DUPLICATES_DEFAULT_THRESHOLD,
        min_size: int = cs.DUPLICATES_DEFAULT_MIN_NODES,
        limit: int = cs.DUPLICATES_DEFAULT_GROUP_LIMIT,
    ) -> str:
        """Report structurally duplicated functions (issue #1342).

        Duplicate detection spans several graph reads -- fingerprints, then
        skipped-symbol coverage -- so it takes the lock for the same reason
        `flow_verdict` does: index/update delete and rebuild while holding it,
        and an interleaved read would report groups from one generation with
        coverage from another.
        """
        project, scope_error = self._workspace_scope(project)
        if scope_error is not None:
            return scope_error
        dup_project = project or derive_project_name(Path(self.project_root))
        async with self._ingestor_lock:
            if refusal := await run_in_thread(
                self.marker.partial_graph_refusal,
                dup_project,
                cs.MCPToolName.FIND_DUPLICATE_CODE,
            ):
                return refusal
            result = await plain_function(self._find_duplicates_tool)(
                project=project, threshold=threshold, min_size=min_size, limit=limit
            )
        if isinstance(result, te.ToolFailure):
            return result
        return str(result)

    async def get_function_source(self, node_id: int) -> str:
        """Fetch a function's source by graph node id (issue #1342).

        A node id is only meaningful within one generation of the graph, so
        the lookup must not straddle a rebuild that could reassign it.
        """
        # A node id from a partial graph may name a node that the completed
        # index would not have produced at all.
        async with self._ingestor_lock:
            if refusal := await run_in_thread(
                self.marker.partial_graph_refusal,
                derive_project_name(Path(self.project_root)),
                cs.MCPToolName.GET_FUNCTION_SOURCE,
            ):
                return refusal
            result = await plain_function(self._function_source_tool)(node_id=node_id)
        if isinstance(result, te.ToolFailure):
            return result
        return str(result)

    async def query_code_graph(
        self, natural_language_query: str, project: str | None = None
    ) -> QueryResultDict:
        logger.info(ls.MCP_QUERY_CODE_GRAPH.format(query=natural_language_query))
        try:
            # The workspace allow-list and default first (issue #1494), then
            # the graph's own check: a typo returning zero rows is
            # indistinguishable from a genuine empty result.
            project, scope_error = self._workspace_scope(project)
            if scope_error is not None:
                return _query_refusal(scope_error)
            if project is not None:
                known = await run_in_thread(self.ingestor.list_projects)
                if project not in known:
                    unknown = self._unknown_project_error(project, known)
                    return _query_refusal(unknown)
            # The same refusal the eight `_graph_query` tools apply. This one
            # does not share that path -- it binds a per-request query tool
            # below -- so the check is repeated here rather than inherited.
            project_name = project or derive_project_name(Path(self.project_root))
            # Per REQUEST, not per process: one HTTP server hosts several
            # projects, and a scope fixed at startup would force a process
            # each (issue #1494). The pre-built `_query_tool` has its project
            # bound at construction, so a scoped request gets a tool of its
            # own -- which runs the evidence guard and the row filter BEFORE
            # its row cap and token truncation. Filtering here instead spent
            # the caps on foreign rows and left `summary` describing rows the
            # caller never received (issue #1508).
            query_tool = (
                self._query_tool
                if project is None
                else create_query_tool(
                    ingestor=self.ingestor,
                    cypher_gen=self.cypher_gen,
                    console=self._stderr_console,
                    project_name=project,
                )
            )
            # Serialise against index/update, which delete and rebuild the
            # graph under this lock; an interleaved read mixes generations.
            # The guard is evaluated in the SAME lock scope as the read: an
            # earlier version checked under `_refusal_under_lock`, released
            # the lock and reacquired it here, so an update could mark the
            # graph incomplete in the gap and the approved query still ran
            # (PR #1547).
            async with self._ingestor_lock:
                if refusal := await run_in_thread(
                    self.marker.partial_graph_refusal,
                    project_name,
                    cs.MCPToolName.QUERY_CODE_GRAPH,
                ):
                    return _query_refusal(refusal)
                graph_data = await plain_function(query_tool)(natural_language_query)
            result_dict: QueryResultDict = graph_data.model_dump()
            # The error key marks a scoping refusal; absent it means success,
            # so a None must not leak into the wire shape as a present key.
            if graph_data.error is None:
                result_dict.pop(mcs.MCP_KEY_ERROR, None)
            logger.info(
                ls.MCP_QUERY_RESULTS.format(
                    count=len(result_dict.get(cs.DICT_KEY_RESULTS, []))
                )
            )
            return result_dict
        except Exception as e:  # noqa: BLE001 -- tool boundary: any failure becomes the tool's error result
            logger.exception(ls.MCP_ERROR_QUERY.format(error=e))
            return _query_refusal(
                mcs.MCP_TOOL_EXEC_ERROR.format(
                    name=cs.MCPToolName.QUERY_CODE_GRAPH, error=e
                )
            )

    async def get_code_snippet(self, qualified_name: str) -> CodeSnippetResultDict:
        logger.info(ls.MCP_GET_CODE_SNIPPET.format(name=qualified_name))
        try:
            # Serialise against index/update, which delete and rebuild the
            # graph under this lock; an interleaved read mixes generations.
            # The guard is inside the lock so an update cannot land between
            # the check and the read (PR #1547).
            async with self._ingestor_lock:
                project_name = derive_project_name(Path(self.project_root))
                if refusal := await run_in_thread(
                    self.marker.partial_graph_refusal,
                    project_name,
                    cs.MCPToolName.GET_CODE_SNIPPET,
                ):
                    return CodeSnippetResultDict(error=refusal, found=False)
                snippet = await plain_function(self._code_tool)(
                    qualified_name=qualified_name
                )
                if isinstance(snippet, str):
                    # The workspace guard answered instead of the retriever.
                    return CodeSnippetResultDict(error=snippet, found=False)
                result: CodeSnippetResultDict = snippet.model_dump()
                if result.get(mcs.MCP_KEY_FOUND) is False:
                    # No snippet is a refusal, worded as `callers` words an
                    # unknown name when the graph has none (issue #3244).
                    unknown = await run_in_thread(
                        self._unknown_target_error, project_name, qualified_name
                    )
                    result[cs.DICT_KEY_ERROR] = (
                        unknown
                        or result.get(mcs.MCP_KEY_ERROR_MESSAGE)
                        or te.CODE_ENTITY_NOT_FOUND
                    )
            return result
        except Exception as e:  # noqa: BLE001 -- tool boundary: any failure becomes the tool's error result
            logger.error(ls.MCP_ERROR_CODE_SNIPPET.format(error=e))
            return CodeSnippetResultDict(error=str(e), found=False)
