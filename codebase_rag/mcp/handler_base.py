"""What every MCP tool handler shares: the registry's state, running
blocking graph work off the event loop, calling a pydantic-ai tool directly,
and the project and workspace scoping each project-taking tool applies.

`MCPToolsRegistry` (tools.py) sets the state in its `__init__` and composes
the per-group handlers built on this class.
"""

import asyncio
import functools
from abc import abstractmethod
from collections.abc import Callable, Collection, Mapping
from pathlib import Path

from pydantic_ai import Agent, DeferredToolRequests, Tool
from pydantic_ai.tools import ToolFuncPlain
from tree_sitter import Parser

from codebase_rag import constants as cs
from codebase_rag import cypher_queries as cq
from codebase_rag import graph_query
from codebase_rag import tool_errors as te
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.mcp.run_marker import RunMarker
from codebase_rag.services.graph_service import GraphIngestor
from codebase_rag.services.llm import CypherQueryGenerator
from codebase_rag.types_defs import GraphRefusal, LanguageQueries
from codebase_rag.utils.path_utils import derive_project_name
from codebase_rag.utils.terminal_console import TerminalAwareConsole
from codebase_rag.workspaces import WorkspaceConfig

from . import constants as mcs


async def run_in_thread[**P, T](
    fn: Callable[P, T], /, *args: P.args, **kwargs: P.kwargs
) -> T:
    """`asyncio.to_thread` that a cancelled request cannot outrun.

    Cancelling a request cancels its handler, but no thread can be stopped:
    with a bare `to_thread` the handler unwound at once, its `async with
    self._ingestor_lock` released, and the next mutating call ran while the
    thread was still writing. A `delete_project` under a cancelled
    `index_repository` left 13,404 orphan nodes that every later sync and
    delete missed (issue #2920). The cancellation is delivered once the
    thread has ended, so a lock taken around this call is held as long as
    the work it guards runs.
    """
    work = asyncio.ensure_future(asyncio.to_thread(fn, *args, **kwargs))
    cancelled = False
    while True:
        try:
            result = await asyncio.shield(work)
        except asyncio.CancelledError:
            if work.cancelled():
                raise
            cancelled = True
            continue
        except Exception as exc:
            if cancelled:
                # The caller was cancelled and gets its cancellation, not the
                # failure of work it no longer waits for (bot review on PR
                # #2987); the failure stays attached as its cause.
                raise asyncio.CancelledError from exc
            raise
        break
    if cancelled:
        raise asyncio.CancelledError
    return result


def plain_function(tool: Tool) -> ToolFuncPlain[...]:
    # pydantic-ai types `function` as taking a RunContext or not; every tool
    # this registry calls directly is built without one, which this states
    # once instead of at each call site.
    return tool.function


class ToolHandlerBase:
    """The fields `MCPToolsRegistry.__init__` sets, and the scoping every
    handler group applies before it reads or writes the graph."""

    project_root: str
    ingestor: GraphIngestor
    cypher_gen: CypherQueryGenerator
    workspace: WorkspaceConfig | None
    marker: RunMarker
    parsers: Mapping[cs.SupportedLanguage, Parser]
    queries: Mapping[cs.SupportedLanguage, LanguageQueries]
    _ingestor_lock: asyncio.Lock
    _live_updater: GraphUpdater | None
    _stderr_console: TerminalAwareConsole
    _query_tool: Tool
    _code_tool: Tool
    _file_editor_tool: Tool
    _file_reader_tool: Tool
    _file_writer_tool: Tool
    _directory_lister_tool: Tool
    _shell_command_tool: Tool[None]
    _structural_search_tool: Tool
    _structural_editor_tool: Tool
    _structural_written: list[str]
    _structural_available: bool
    _find_duplicates_tool: Tool
    _function_source_tool: Tool
    _rag_agent: Agent[None, str | DeferredToolRequests] | None
    _semantic_search_tool: Tool | None

    @abstractmethod
    async def _graph_query[T](
        self,
        tool: cs.MCPToolName,
        project: str | None,
        run: Callable[[str], T | GraphRefusal],
        target: str | None = None,
    ) -> T | GraphRefusal:
        """Run `run` for the project a request means, under the ingestor lock
        and behind the refusals every graph read takes: the registry's
        dispatcher (tools.py)."""

    def _workspace_root_refusal(
        self, workspace: WorkspaceConfig
    ) -> te.ToolFailure | None:
        """Why the root-bound tools are refused under `workspace`: this
        server's directory is none of its repositories."""
        root = Path(self.project_root).resolve()
        repos = [repo.repo_path() for repo in workspace.repos]
        if root in repos:
            return None
        return te.ToolFailure(
            mcs.MCP_ROOT_OUTSIDE_WORKSPACE.format(
                root=root,
                workspace=workspace.name,
                known=cs.SEPARATOR_COMMA_SPACE.join(map(str, repos)),
            )
        )

    def _workspace_scoped_tool(self, tool: Tool) -> Tool:
        """`tool` with its `project` argument held to the workspace allow-list
        and default, for the agent; a no-op without a workspace. `wraps`
        keeps the signature, which is the tool's schema."""
        if self.workspace is None:
            return tool
        original = plain_function(tool)

        @functools.wraps(original)
        async def scoped(*args: object, **kwargs: object) -> object:
            requested = kwargs.get(cs.MCPParamName.PROJECT)
            project, scope_error = self._workspace_scope(
                requested if isinstance(requested, str) else None
            )
            if scope_error is not None:
                return scope_error
            kwargs[cs.MCPParamName.PROJECT] = project
            return await original(*args, **kwargs)

        return Tool(scoped, name=tool.name, description=tool.description)

    def _workspace_name_refusal(
        self, qualified_name: str, indexed: list[str] | None = None
    ) -> te.ToolFailure | None:
        """Why a qualified name is refused under a workspace: it belongs to
        no served project. The agent's source readers take a name, not a
        project, so the allow-list is applied to the name's project prefix
        (PR #1972). `indexed` is the graph's project list,
        read by the caller off the event loop; absent, it is read here."""
        if self.workspace is None:
            return None
        names = self.workspace.project_names()
        if indexed is None:
            indexed = self.ingestor.list_projects()
        owner = self._name_owner(qualified_name, [*names, *indexed])
        if owner is not None and owner in names:
            return None
        return te.ToolFailure(
            mcs.MCP_NAME_OUTSIDE_WORKSPACE.format(
                name=qualified_name,
                workspace=self.workspace.name,
                known=cs.SEPARATOR_COMMA_SPACE.join(names),
            )
        )

    @staticmethod
    def _name_owner(qualified_name: str, projects: list[str]) -> str | None:
        # The name's project is the LONGEST indexed or served project name
        # it sits under, not the first served one that is a prefix: with
        # `foo` served and `foo.bar` indexed outside the workspace,
        # `foo.bar.pkg.fn` belongs to `foo.bar` (bot review on PR #1972,
        # the dotted-prefix class of issue #1970).
        owners = [
            (len(candidate), candidate)
            for candidate in set(projects)
            if qualified_name == candidate
            or qualified_name.startswith(f"{candidate}{cs.SEPARATOR_DOT}")
        ]
        return max(owners)[1] if owners else None

    def _workspace_source_refusal(self, qualified_name: str) -> te.ToolFailure | None:
        """Why a source read of `qualified_name` is refused under a
        workspace: the name is outside it, or its project is served but the
        graph does not show it indexed from the workspace's checkout. The
        source readers read the graph-stored path, or fall back to this
        server's root, so a moved repo or a stale graph would otherwise serve
        an allowed name from outside the workspace; `definition` applies the
        same stored-root proof (PR #1972). Reads the graph: call it
        off the event loop."""
        if self.workspace is None:
            return None
        indexed = self.ingestor.list_projects()
        refusal = self._workspace_name_refusal(qualified_name, indexed)
        if refusal is not None:
            return refusal
        names = self.workspace.project_names()
        owner = self._name_owner(qualified_name, [*names, *indexed])
        if owner is not None and self._source_root_for(owner) is not None:
            return None
        return te.ToolFailure(
            mcs.MCP_WORKSPACE_SOURCE_UNPROVEN.format(
                name=qualified_name, project=owner, workspace=self.workspace.name
            )
        )

    def _workspace_scoped_by_name(self, tool: Tool) -> Tool:
        """`tool`, taking a qualified name, refusing one outside the
        workspace; a no-op without a workspace."""
        if self.workspace is None:
            return tool
        original = plain_function(tool)

        @functools.wraps(original)
        async def scoped(
            qualified_name: str, *args: object, **kwargs: object
        ) -> object:
            # The refusal reads the graph: off the event loop, which the
            # direct handler holds under the ingestor lock.
            refusal = await run_in_thread(
                self._workspace_source_refusal, qualified_name
            )
            if refusal is not None:
                return refusal
            return await original(qualified_name, *args, **kwargs)

        return Tool(scoped, name=tool.name, description=tool.description)

    def _workspace_scoped_by_node(self, tool: Tool) -> Tool:
        """`tool`, taking a node id, refusing a node whose definition is
        outside the workspace: the id is resolved to its qualified name first
        (one read, only under a workspace); an id that resolves to nothing
        is left to the tool, which reports it unavailable."""
        if self.workspace is None:
            return tool
        original = plain_function(tool)

        @functools.wraps(original)
        async def scoped(node_id: int, *args: object, **kwargs: object) -> object:
            rows = await run_in_thread(
                self.ingestor.fetch_all,
                cq.CYPHER_GET_FUNCTION_SOURCE_LOCATION,
                {cs.KEY_NODE_ID: node_id},
            )
            name = rows[0].get(cs.KEY_QUALIFIED_NAME) if rows else None
            if isinstance(name, str):
                refusal = await run_in_thread(self._workspace_source_refusal, name)
                if refusal is not None:
                    return refusal
            return await original(node_id, *args, **kwargs)

        return Tool(scoped, name=tool.name, description=tool.description)

    def _nameable(self, projects: Collection[str]) -> list[str]:
        """The `projects` a refusal may name: under a workspace, only the
        ones it serves; without one, all of them.

        The graph is shared, and a workspace project that is not indexed
        passes the allow-list and is refused as unknown, so its "did you
        mean" and its fallback list were drawn from every indexed project and
        named ones outside the workspace (PR #2562, CWE-200).
        """
        if self.workspace is None:
            return list(projects)
        served = set(self.workspace.project_names())
        return [name for name in projects if name in served]

    def _unknown_project_error(self, project: str, known: list[str]) -> te.ToolFailure:
        # The close matches when there are any; a shared graph lists dozens
        # of `<dir>__<hash>` names, and the one meant is the one to name.
        known = self._nameable(known)
        close = graph_query.close_project_names(project, known)
        if close:
            return te.ToolFailure(
                mcs.MCP_UNKNOWN_PROJECT_NAMED.format(project=project)
                + graph_query.did_you_mean(close)
            )
        listed = cs.SEPARATOR_COMMA_SPACE.join(known)
        if self.workspace is None:
            return te.ToolFailure(
                mcs.MCP_UNKNOWN_PROJECT.format(project=project, known=listed)
            )
        # Labelled as the workspace's, so a short list does not read as the
        # whole graph, and an empty one is said rather than left blank.
        if not known:
            return te.ToolFailure(
                mcs.MCP_UNKNOWN_WORKSPACE_PROJECT_NONE_INDEXED.format(
                    project=project, workspace=self.workspace.name
                )
            )
        return te.ToolFailure(
            mcs.MCP_UNKNOWN_WORKSPACE_PROJECT.format(
                project=project, workspace=self.workspace.name, known=listed
            )
        )

    def _root_not_indexed_error(
        self,
        project_name: str,
        known: list[str],
        message: str = mcs.MCP_ROOT_NOT_INDEXED,
    ) -> te.ToolFailure:
        root = Path(self.project_root)
        # A repository indexed under a name of its own is the likeliest
        # meaning, then names spelled close to the derived one; both only
        # among the projects this server may name.
        rooted = self._nameable(
            graph_query.projects_rooted_at(self.ingestor.list_project_roots(), root)
        )
        close = graph_query.close_project_names(project_name, self._nameable(known))
        return te.ToolFailure(
            message.format(path=root.resolve())
            + graph_query.did_you_mean(list(dict.fromkeys([*rooted, *close])))
        )

    @staticmethod
    def _target_refusal(unknown: graph_query.UnknownTargetError) -> str:
        return unknown.refusal(mcs.MCP_UNKNOWN_TARGET, mcs.MCP_UNKNOWN_TARGET_HINT)

    def _unknown_target_error(self, project_name: str, target: str) -> str | None:
        try:
            graph_query.require_known(self.ingestor.fetch_all, project_name, target)
        except graph_query.UnknownTargetError as unknown:
            return self._target_refusal(unknown)
        return None

    def _workspace_scope(
        self, project: str | None
    ) -> tuple[str | None, te.ToolFailure | None]:
        """(the project a request means, why it is refused) under a workspace.

        The one decision every project-taking tool applies (issue #1494,
        local review): a name outside the workspace is refused with the
        workspace's names; a bare request takes the workspace default or is
        refused naming the choices. Without a workspace nothing changes. The
        graph's own unknown-project check still follows in every caller, so
        an unindexed workspace repo reads as unknown, never as served.
        """
        if self.workspace is None:
            return project, None
        names = self.workspace.project_names()
        if project is not None:
            if project in names:
                return project, None
            return None, te.ToolFailure(
                mcs.MCP_PROJECT_OUTSIDE_WORKSPACE.format(
                    project=project,
                    workspace=self.workspace.name,
                    known=cs.SEPARATOR_COMMA_SPACE.join(names),
                )
            )
        default = self._workspace_default_project(self.workspace)
        if default is None:
            return None, te.ToolFailure(
                mcs.MCP_WORKSPACE_DEFAULT_AMBIGUOUS.format(
                    workspace=self.workspace.name,
                    count=len(names),
                    known=cs.SEPARATOR_COMMA_SPACE.join(names),
                )
            )
        return default, None

    def _fixed_root_project(self) -> tuple[str, None] | tuple[None, te.ToolFailure]:
        """(project, refusal) for a handler that takes no `project` argument.

        flow_verdict, explain_traceback and rank_root_causes derive their
        project from this server's directory. Without a workspace that is
        the whole world and the derivation stands. UNDER a workspace it is
        not: `TARGET_REPO_PATH` may point at an indexed repo the workspace
        does not serve, and a bare call would then answer from outside the
        boundary. The workspace default applies, and an ambiguous one
        refuses rather than guessing (PR #1972).
        """
        if self.workspace is None:
            return derive_project_name(Path(self.project_root)), None
        default = self._workspace_default_project(self.workspace)
        if default is not None:
            return default, None
        names = self.workspace.project_names()
        return None, te.ToolFailure(
            mcs.MCP_WORKSPACE_DEFAULT_AMBIGUOUS.format(
                workspace=self.workspace.name,
                count=len(names),
                known=cs.SEPARATOR_COMMA_SPACE.join(names),
            )
        )

    def _unindexed_refusal(self, project: str) -> te.ToolFailure | None:
        """Why a fixed-root handler must not read `project`, or None.

        `_fixed_root_project` derives the name without asking the graph. A
        project the graph does not hold has no FLOWS_TO edge and no Module
        to report as uncovered, so flow_verdict answered NO_FLOW, the
        verified absence, and the traceback tools resolved nothing with no
        note (issue #3193). The check `_graph_query` makes, with its
        messages: a workspace default is a named project, a derived one is
        the server's root.
        """
        known = self.ingestor.list_projects()
        if project in known:
            return None
        if self.workspace is not None:
            return self._unknown_project_error(project, known)
        return self._root_not_indexed_error(
            project, known, mcs.MCP_FIXED_ROOT_NOT_INDEXED
        )

    def _workspace_default_project(self, workspace: WorkspaceConfig) -> str | None:
        """The project of `workspace` a request without `project` means.

        The repo rooted at this server's directory, if the workspace has
        one; else the only project, if it has one; else nothing, and the
        caller must say which. Never a guess between several.
        """
        root = Path(self.project_root).resolve()
        for repo in workspace.repos:
            if repo.repo_path() == root:
                return repo.project_name
        names = workspace.project_names()
        return names[0] if len(names) == 1 else None

    def _source_root_for(self, project_name: str) -> Path | None:
        # Source is read from disk only when the selected project was indexed
        # from this server's repository (its Project node's stored root):
        # another project's definition carries a relative path that may also
        # exist here and would read the wrong file, and a matching name alone
        # does not prove the root, so its span is answered without source.
        # A workspace names each repo's root, so its projects are answered
        # from their own checkouts under the same stored-root proof.
        candidate = Path(self.project_root)
        if self.workspace is not None:
            for repo in self.workspace.repos:
                if repo.project_name == project_name:
                    candidate = repo.repo_path()
                    break
        return graph_query.source_root_for(
            self.ingestor.fetch_all, project_name, candidate
        )
