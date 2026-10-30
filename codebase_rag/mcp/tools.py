"""The MCP tools registry: which tools this server offers, the handler
serving each, and the dispatch from a tool name to that handler. Each
tool's input schema is in tool_schemas.py.

The handlers live in per-group modules the registry composes: the graph's
writers (lifecycle_tools.py), the deterministic graph queries
(graph_tools.py), rename (edit_tools.py), the file tools (file_tools.py),
the agent and its search tools (retrieval_tools.py) and the flow and
traceback analysis (analysis_tools.py), all on `ToolHandlerBase`
(handler_base.py). The graph queries run through `_graph_query`, the
dispatcher here that holds the ingestor lock around a read and applies its
refusals.
"""

import asyncio
import sys
from collections.abc import Callable
from pathlib import Path

from loguru import logger

from codebase_rag import constants as cs
from codebase_rag import graph_query
from codebase_rag import logs as ls
from codebase_rag import tool_errors as te
from codebase_rag.mcp.run_marker import RunMarker
from codebase_rag.models import ToolMetadata
from codebase_rag.parser_loader import load_parsers
from codebase_rag.services.graph_service import GraphIngestor
from codebase_rag.services.llm import CypherQueryGenerator
from codebase_rag.tools import tool_descriptions as td
from codebase_rag.tools.ast_grep_service import AstGrepService
from codebase_rag.tools.code_retrieval import (
    CodeRetriever,
    create_code_retrieval_tool,
)
from codebase_rag.tools.codebase_query import create_query_tool
from codebase_rag.tools.directory_lister import (
    DirectoryLister,
    create_directory_lister_tool,
)
from codebase_rag.tools.duplicate_detection import create_find_duplicates_tool
from codebase_rag.tools.file_editor import FileEditor, create_file_editor_tool
from codebase_rag.tools.file_reader import FileReader, create_file_reader_tool
from codebase_rag.tools.file_writer import FileWriter, create_file_writer_tool
from codebase_rag.tools.semantic_search import create_get_function_source_tool
from codebase_rag.tools.shell_command import ShellCommander, create_shell_command_tool
from codebase_rag.tools.structural_editor import create_structural_editor_tool
from codebase_rag.tools.structural_search import create_structural_search_tool
from codebase_rag.types_defs import (
    GraphRefusal,
    MCPHandlerType,
    MCPToolSchema,
)
from codebase_rag.utils.dependencies import has_ast_grep, has_semantic_dependencies
from codebase_rag.utils.path_utils import derive_project_name
from codebase_rag.utils.terminal_console import terminal_aware_console
from codebase_rag.workspaces import WorkspaceConfig

from . import constants as mcs
from . import tool_schemas as mts
from .analysis_tools import AnalysisTools
from .edit_tools import EditTools
from .file_tools import FileTools
from .graph_tools import GraphQueryTools
from .handler_base import run_in_thread
from .retrieval_tools import RetrievalTools


def _tool(name: cs.MCPToolName, handler: MCPHandlerType) -> ToolMetadata:
    """An MCP tool: its description from `td.MCP_TOOLS`, its schema from
    `tool_schemas`, and the handler serving it."""
    return ToolMetadata(
        name=name,
        description=td.MCP_TOOLS[name],
        input_schema=mts.input_schema(name),
        handler=handler,
        returns_json=mts.returns_json(name),
    )


def _found_nothing(result: object) -> bool:
    """An empty walk, or a lookup that answered `found: false`."""
    return result == [] or (
        isinstance(result, dict) and result.get(mcs.MCP_KEY_FOUND) is False
    )


def _refusing_handler(refusal: te.ToolFailure, returns_json: bool) -> MCPHandlerType:
    async def refuse(**_kwargs: object) -> object:
        return {cs.DICT_KEY_ERROR: refusal} if returns_json else refusal

    return refuse


# The read-only tools routed through `_graph_query`. Listed rather than
# inferred: `rename` shares that path for the ingestor lock but is a WRITE,
# and refusing it here would replace its own refusal payload (which carries
# `ambiguous`/`unlocatable`) with a generic error.
#
# This set only governs the `_graph_query` dispatcher. Seven other handlers
# (flow_verdict, explain_traceback, rank_root_causes, semantic_search,
# find_duplicate_code, get_function_source, get_code_snippet) do not go
# through that dispatcher at all -- which is exactly why they went unguarded
# until PR #1547 -- and call `marker.partial_graph_refusal` directly instead.
#
# An explicit inventory fails OPEN: a tool nobody adds here is simply not
# guarded, silently. `_GRAPH_READING_TOOLS` below closes that by naming the
# NON-readers instead, so a new tool is guarded by default and
# `test_every_graph_reader_is_guarded` fails if one is neither.
# The tools that do NOT read the code graph: lifecycle and project
# management, plain file and text operations, and the agent passthrough.
# Named this way round deliberately. The graph readers are then everything
# else, so a tool added to `MCPToolName` counts as a reader until someone
# says otherwise, and `test_every_graph_reader_is_guarded` fails until it is
# either guarded or listed here. The previous shape -- an explicit list of
# readers -- failed OPEN, which is how seven readers went unguarded from the
# day the incomplete-graph refusal was written (PR #1547).
_NOT_GRAPH_READERS = frozenset(
    {
        cs.MCPToolName.LIST_PROJECTS,
        cs.MCPToolName.DELETE_PROJECT,
        cs.MCPToolName.WIPE_DATABASE,
        cs.MCPToolName.INDEX_REPOSITORY,
        cs.MCPToolName.UPDATE_REPOSITORY,
        cs.MCPToolName.REINGEST,
        cs.MCPToolName.RENAME,
        cs.MCPToolName.SURGICAL_REPLACE_CODE,
        cs.MCPToolName.READ_FILE,
        cs.MCPToolName.WRITE_FILE,
        cs.MCPToolName.LIST_DIRECTORY,
        cs.MCPToolName.STRUCTURAL_SEARCH,
        cs.MCPToolName.STRUCTURAL_REPLACE,
        cs.MCPToolName.ASK_AGENT,
    }
)

# The non-readers that act on this server's directory rather than on a
# project they name. Under a workspace that does not serve that directory
# they are refused: they would index, read or edit a repository outside the
# allow-list. `test_every_non_reader_is_classified_for_workspaces` fails
# until a new non-reader is placed here or in the set below it.
_ROOT_BOUND_TOOLS = frozenset(
    {
        cs.MCPToolName.INDEX_REPOSITORY,
        cs.MCPToolName.UPDATE_REPOSITORY,
        cs.MCPToolName.REINGEST,
        cs.MCPToolName.SURGICAL_REPLACE_CODE,
        cs.MCPToolName.READ_FILE,
        cs.MCPToolName.WRITE_FILE,
        cs.MCPToolName.LIST_DIRECTORY,
        cs.MCPToolName.STRUCTURAL_SEARCH,
        cs.MCPToolName.STRUCTURAL_REPLACE,
        # The agent's file, shell and edit tools are bound to the same root.
        cs.MCPToolName.ASK_AGENT,
    }
)

# The non-readers that name their project, and so take the allow-list
# themselves.
_PROJECT_NAMED_TOOLS = frozenset(
    {
        cs.MCPToolName.LIST_PROJECTS,
        cs.MCPToolName.DELETE_PROJECT,
        cs.MCPToolName.RENAME,
    }
)

# Every graph reader, derived. `_READS_THE_GRAPH` below is the subset routed
# through `_graph_query`; the rest call `marker.partial_graph_refusal` themselves.
_GRAPH_READING_TOOLS = frozenset(cs.MCPToolName) - _NOT_GRAPH_READERS

_READS_THE_GRAPH = frozenset(
    {
        cs.MCPToolName.RESOLVE,
        cs.MCPToolName.DEFINITION,
        cs.MCPToolName.CALLERS,
        cs.MCPToolName.CALLEES,
        cs.MCPToolName.IMPLEMENTORS,
        cs.MCPToolName.OVERRIDES,
        cs.MCPToolName.IMPORTERS,
        cs.MCPToolName.TESTS_REACHING,
        cs.MCPToolName.ENDPOINTS,
        cs.MCPToolName.ENDPOINT_CALLERS,
        cs.MCPToolName.REMOTE_DEPENDENCIES,
        # `annotate` writes, but it READS first: the subject is resolved
        # against the graph, and on a partial graph a missing sibling makes
        # an ambiguous name look unique, so the note lands on the wrong
        # definition with no refusal. Guarded like the readers (#1808).
        cs.MCPToolName.ANNOTATE,
        cs.MCPToolName.GLOSSES,
        # A pure READER, unlike the edit operations in `_NOT_GRAPH_READERS`:
        # it ranks and excerpts, and writes nothing. It is routed through
        # `_graph_query` like the deterministic readers above, so it belongs
        # here rather than being exempted -- a partial graph would silently
        # drop callers, callees and reaching tests from the slice, and the
        # budget would be spent on a context that is quietly incomplete
        # (issue #1536).
        cs.MCPToolName.CONTEXT,
    }
)


class MCPToolsRegistry(
    RetrievalTools, AnalysisTools, GraphQueryTools, EditTools, FileTools
):
    def __init__(
        self,
        project_root: str,
        ingestor: GraphIngestor,
        cypher_gen: CypherQueryGenerator,
        workspace: WorkspaceConfig | None = None,
    ) -> None:
        self.project_root = project_root
        self.ingestor = ingestor
        self.cypher_gen = cypher_gen
        # The workspace this server serves, if one was loaded (issue #1494):
        # an allow-list on top of the graph's own project check, never a
        # substitute for it, and the source root for each of its repos.
        self.workspace = workspace
        self._ingestor_lock = asyncio.Lock()
        # The updater that last indexed this root, kept warm so reingest()
        # resolves cross-file calls without re-reading the registry from the
        # graph on every call (issue #1524).
        self._live_updater = None
        # The durable incomplete-run marker this registry writes, with what
        # this process knows about damage to the graph it serves.
        self.marker = RunMarker(lambda: self.ingestor)

        self.parsers, self.queries = load_parsers()

        self.code_retriever = CodeRetriever(project_root, ingestor)
        self.file_editor = FileEditor(project_root=project_root)
        self.file_reader = FileReader(project_root=project_root)
        self.file_writer = FileWriter(
            project_root=project_root, write_lock=self.file_editor.write_lock
        )
        self.directory_lister = DirectoryLister(project_root=project_root)
        self.shell_commander = ShellCommander(project_root=project_root)
        self.ast_grep_service = AstGrepService(project_root=project_root)

        # Kept on self: a scoped request builds its own query tool per call,
        # and that tool must print to the same console as the pre-built one.
        self._stderr_console = terminal_aware_console(file=sys.stderr)
        self._query_tool = create_query_tool(
            ingestor=ingestor, cypher_gen=cypher_gen, console=self._stderr_console
        )
        self._code_tool = create_code_retrieval_tool(code_retriever=self.code_retriever)
        self._file_editor_tool = create_file_editor_tool(file_editor=self.file_editor)
        self._file_reader_tool = create_file_reader_tool(file_reader=self.file_reader)
        self._file_writer_tool = create_file_writer_tool(file_writer=self.file_writer)
        self._directory_lister_tool = create_directory_lister_tool(
            directory_lister=self.directory_lister
        )
        self._shell_command_tool = create_shell_command_tool(
            shell_commander=self.shell_commander
        )
        self._structural_search_tool = create_structural_search_tool(
            service=self.ast_grep_service
        )
        # Files the last structural_replace wrote, for the structural delta.
        self._structural_written = []
        self._structural_editor_tool = create_structural_editor_tool(
            service=self.ast_grep_service, on_changes=self._note_structural_changes
        )
        self._structural_available = has_ast_grep()

        # Both read-only, and both were CLI-only until issue #1342.
        # `find_duplicate_code` was absent from MCP entirely;
        # `get_function_source` existed here only inside the `ask_agent`
        # toolset, so a client could not call it directly, only ask an LLM to
        # decide to call it. Neither takes a ReadContentRecord: that gate
        # exists to keep repository content out of `web_search`, and MCP
        # exposes no web-reaching tool for it to guard.
        self._find_duplicates_tool = create_find_duplicates_tool(self.ingestor)
        self._function_source_tool = create_get_function_source_tool(self.ingestor)
        # Held to the workspace ONCE, here, so the direct MCP handlers and
        # the agent's tool list share the guard: wrapping only the agent's
        # copies left the MCP client itself served source from outside the
        # allow-list (PR #1972). No-ops without a
        # workspace.
        self._code_tool = self._workspace_scoped_by_name(self._code_tool)
        self._function_source_tool = self._workspace_scoped_by_node(
            self._function_source_tool
        )

        self._rag_agent = None

        self._semantic_search_tool = None

        if has_semantic_dependencies():
            from codebase_rag.tools.semantic_search import (
                create_semantic_search_tool,
            )

            self._semantic_search_tool = create_semantic_search_tool(self.ingestor)
        else:
            logger.info(ls.MCP_SEMANTIC_NOT_AVAILABLE)

        self._tools = self._build_tools()
        self._hold_tools_to_workspace()

    def _build_tools(self) -> dict[str, ToolMetadata]:
        """Every tool this server offers, by name: its schema and handler."""
        handlers: dict[cs.MCPToolName, MCPHandlerType] = {
            cs.MCPToolName.LIST_PROJECTS: self.list_projects,
            cs.MCPToolName.DELETE_PROJECT: self.delete_project,
            cs.MCPToolName.WIPE_DATABASE: self.wipe_database,
            cs.MCPToolName.INDEX_REPOSITORY: self.index_repository,
            cs.MCPToolName.UPDATE_REPOSITORY: self.update_repository,
            cs.MCPToolName.REINGEST: self.reingest,
            cs.MCPToolName.RESOLVE: self.resolve,
            cs.MCPToolName.DEFINITION: self.definition,
            cs.MCPToolName.CALLERS: self.callers,
            cs.MCPToolName.CALLEES: self.callees,
            cs.MCPToolName.IMPLEMENTORS: self.implementors,
            cs.MCPToolName.OVERRIDES: self.overrides,
            cs.MCPToolName.ANNOTATE: self.annotate,
            cs.MCPToolName.GLOSSES: self.glosses,
            cs.MCPToolName.IMPORTERS: self.importers,
            cs.MCPToolName.TESTS_REACHING: self.tests_reaching,
            cs.MCPToolName.ENDPOINTS: self.endpoints,
            cs.MCPToolName.ENDPOINT_CALLERS: self.endpoint_callers,
            cs.MCPToolName.REMOTE_DEPENDENCIES: self.remote_dependencies,
            cs.MCPToolName.RENAME: self.rename,
            cs.MCPToolName.CONTEXT: self.context,
            cs.MCPToolName.QUERY_CODE_GRAPH: self.query_code_graph,
            cs.MCPToolName.GET_CODE_SNIPPET: self.get_code_snippet,
            cs.MCPToolName.SURGICAL_REPLACE_CODE: self.surgical_replace_code,
            cs.MCPToolName.READ_FILE: self.read_file,
            cs.MCPToolName.WRITE_FILE: self.write_file,
            cs.MCPToolName.LIST_DIRECTORY: self.list_directory,
        }
        if self._semantic_search_tool is not None:
            handlers[cs.MCPToolName.SEMANTIC_SEARCH] = self.semantic_search
        if self._structural_available:
            handlers[cs.MCPToolName.STRUCTURAL_SEARCH] = self.structural_search
            handlers[cs.MCPToolName.STRUCTURAL_REPLACE] = self.structural_replace
        # Unconditional: both read the graph only. get_function_source lives in
        # semantic_search.py but touches none of the embedding machinery, so
        # gating it on the semantic extra would hide a graph-only tool behind
        # a dependency it never uses.
        handlers[cs.MCPToolName.FIND_DUPLICATE_CODE] = self.find_duplicate_code
        handlers[cs.MCPToolName.GET_FUNCTION_SOURCE] = self.get_function_source
        handlers[cs.MCPToolName.ASK_AGENT] = self.ask_agent
        handlers[cs.MCPToolName.FLOW_VERDICT] = self.flow_verdict
        handlers[cs.MCPToolName.EXPLAIN_TRACEBACK] = self.explain_traceback
        handlers[cs.MCPToolName.RANK_ROOT_CAUSES] = self.rank_root_causes
        return {name: _tool(name, handler) for name, handler in handlers.items()}

    def _hold_tools_to_workspace(self) -> None:
        """Refuse, under a workspace, the tools that would act outside it.

        The project-taking tools apply the allow-list per call, but some act
        on what no project argument names. `wipe_database` deletes every
        project in the shared graph, so a workspace server never runs it.
        The root-bound tools act on this server's directory, which a
        workspace need not serve (`_fixed_root_project`); they run only when
        that directory is one of its repositories.
        """
        workspace = self.workspace
        if workspace is None:
            return
        root_refusal = self._workspace_root_refusal(workspace)
        for name, metadata in self._tools.items():
            if name == cs.MCPToolName.WIPE_DATABASE:
                refusal: te.ToolFailure | None = te.ToolFailure(
                    mcs.MCP_WIPE_REFUSED_IN_WORKSPACE.format(
                        workspace=workspace.name,
                        known=cs.SEPARATOR_COMMA_SPACE.join(workspace.project_names()),
                    )
                )
            elif name in _ROOT_BOUND_TOOLS:
                refusal = root_refusal
            else:
                refusal = None
            if refusal is not None:
                metadata.handler = _refusing_handler(refusal, metadata.returns_json)

    # --- the graph queries' dispatch (issue #1523) -------------------------------

    async def _graph_query[T](
        self,
        tool: cs.MCPToolName,
        project: str | None,
        run: Callable[[str], T | GraphRefusal],
        target: str | None = None,
    ) -> T | GraphRefusal:
        # Every path that reads the graph, the project check included, runs
        # under the ingestor lock so an index/update rebuild cannot tear the
        # read (issue #1471). A typo in `project` would otherwise read as a
        # genuine empty result; the default is the project this server's
        # root derives to. `target` is the qualified name the query walks
        # from: an empty answer for a name the graph never saw is refused,
        # not returned as "nothing calls it" (issue #2461).
        try:
            async with self._ingestor_lock:
                # The workspace allow-list is checked FIRST and narrows only:
                # a name outside it is refused with the workspace's names,
                # and a name inside it is still held to the graph's own
                # check below, so an unindexed workspace repo reads as
                # unknown rather than as a second, weaker path (issue #1494).
                project, scope_error = self._workspace_scope(project)
                if scope_error is not None:
                    return GraphRefusal(error=scope_error)
                known = await run_in_thread(self.ingestor.list_projects)
                if project is not None and project not in known:
                    return GraphRefusal(
                        error=self._unknown_project_error(project, known)
                    )
                project_name = project or derive_project_name(Path(self.project_root))
                # The derived default is held to the same check: a server
                # rooted in a directory that was never indexed answered every
                # read with nothing (issue #2461).
                if project_name not in known:
                    return GraphRefusal(
                        error=await run_in_thread(
                            self._root_not_indexed_error, project_name, known
                        )
                    )
                # A read is as wrong as a reingest when the graph is known
                # partial: a failed run (or a rollback whose re-ingest
                # failed) leaves definitions and edges missing, and every
                # tool here would report that absence as fact. `_reingest`
                # already refuses on this flag; the read paths dispatched
                # regardless, so a caller could not tell a restored graph
                # from a complete one.
                # The durable marker is consulted only when this process has
                # no opinion. `marker.persisted_incomplete` answers True when the
                # store cannot be reached -- right for a reingest, which must
                # not start on an unknown state, but wrong here: it would
                # report every store outage as "graph incomplete" and bury
                # the real error the caller needs to see. A read that cannot
                # reach the store fails below as itself.
                # The same project-scoped decision the direct readers use:
                # reading the bare flag here refused project B because
                # project A's recovery failed (PR #1547).
                #
                # Evaluated INSIDE the lock, with the read, because they must
                # serialise: an update that lands between an approval and the
                # read sets the graph incomplete and the approved read then
                # runs against the partial graph.
                if tool in _READS_THE_GRAPH and (
                    refusal := await run_in_thread(
                        self.marker.partial_graph_refusal, project_name, tool
                    )
                ):
                    return GraphRefusal(error=refusal)
                result = await run_in_thread(run, project_name)
                if target is not None and _found_nothing(result):
                    await run_in_thread(
                        graph_query.require_known,
                        self.ingestor.fetch_all,
                        project_name,
                        target,
                    )
                return result
        except graph_query.UnknownTargetError as unknown:
            # Raised by the lookup itself (a location, an endpoint) or by
            # `require_known` for an empty answer: one refusal either way.
            return GraphRefusal(error=self._target_refusal(unknown))
        except Exception as e:  # noqa: BLE001 -- tool boundary: any failure becomes the tool's error result
            logger.error(ls.MCP_GRAPH_QUERY_ERROR.format(tool=tool, error=e))
            return GraphRefusal(
                error=mcs.MCP_GRAPH_QUERY_ERROR.format(tool=tool, error=e)
            )

    def get_tool_schemas(self) -> list[MCPToolSchema]:
        return [
            MCPToolSchema(
                name=metadata.name,
                description=metadata.description,
                inputSchema=metadata.input_schema,
            )
            for metadata in self._tools.values()
        ]

    def get_tool_handler(self, name: str) -> tuple[MCPHandlerType, bool] | None:
        metadata = self._tools.get(name)
        return None if metadata is None else (metadata.handler, metadata.returns_json)
