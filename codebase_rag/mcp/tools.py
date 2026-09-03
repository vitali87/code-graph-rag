import asyncio
import itertools
import json
import sys
from collections.abc import Callable
from pathlib import Path

from loguru import logger
from pydantic_ai import Agent
from rich.console import Console

from codebase_rag import constants as cs
from codebase_rag import graph_query
from codebase_rag import logs as lg
from codebase_rag import structural_delta as sd
from codebase_rag import tool_errors as te
from codebase_rag.config import load_ignore_patterns
from codebase_rag.graph_updater import GraphUpdater, ReingestAborted
from codebase_rag.models import ToolMetadata
from codebase_rag.parser_loader import load_parsers
from codebase_rag.services.graph_service import MemgraphIngestor
from codebase_rag.services.llm import CypherGenerator, create_rag_orchestrator
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
    CodeSnippetResultDict,
    DeleteProjectErrorResult,
    DeleteProjectResult,
    DeleteProjectSuccessResult,
    ListProjectsErrorResult,
    ListProjectsResult,
    ListProjectsSuccessResult,
    MCPHandlerType,
    MCPInputSchema,
    MCPInputSchemaProperty,
    MCPToolSchema,
    QueryResultDict,
    ReingestToolResult,
    StructuralReplaceChange,
)
from codebase_rag.utils.dependencies import has_ast_grep, has_semantic_dependencies
from codebase_rag.utils.path_utils import derive_project_name
from codebase_rag.vector_store import clear_all_embeddings, delete_project_embeddings


def _read_file_slice(full_path: Path, start: int, limit: int | None) -> str:
    with open(full_path, encoding=cs.ENCODING_UTF8) as f:
        skipped_count = sum(1 for _ in itertools.islice(f, start))

        if limit is not None:
            sliced_lines = [line for _, line in zip(range(limit), f)]
        else:
            sliced_lines = list(f)

        paginated_content = "".join(sliced_lines)
        remaining_lines_count = sum(1 for _ in f)

    total_lines = skipped_count + len(sliced_lines) + remaining_lines_count
    header = cs.MCP_PAGINATION_HEADER.format(
        start=start + 1,
        end=start + len(sliced_lines),
        total=total_lines,
    )
    return header + paginated_content


class MCPToolsRegistry:
    def __init__(
        self,
        project_root: str,
        ingestor: MemgraphIngestor,
        cypher_gen: CypherGenerator,
    ) -> None:
        self.project_root = project_root
        self.ingestor = ingestor
        self.cypher_gen = cypher_gen
        self._ingestor_lock = asyncio.Lock()
        # The updater that last indexed this root, kept warm so reingest()
        # resolves cross-file calls without re-reading the registry from the
        # graph on every call (issue #1524).
        self._live_updater: GraphUpdater | None = None
        # True from the moment an index or update starts mutating the graph
        # until it completes: a run that failed part way leaves a graph a
        # scoped reingest must not hydrate from, because it would treat the
        # missing and stale definitions as authoritative.
        self._graph_incomplete = False

        self.parsers, self.queries = load_parsers()

        self.code_retriever = CodeRetriever(project_root, ingestor)
        self.file_editor = FileEditor(project_root=project_root)
        self.file_reader = FileReader(project_root=project_root)
        self.file_writer = FileWriter(project_root=project_root)
        self.directory_lister = DirectoryLister(project_root=project_root)
        self.shell_commander = ShellCommander(project_root=project_root)
        self.ast_grep_service = AstGrepService(project_root=project_root)

        # Kept on self: a scoped request builds its own query tool per call,
        # and that tool must print to the same console as the pre-built one.
        self._stderr_console = Console(file=sys.stderr, width=None, force_terminal=True)
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
        self._structural_written: list[str] = []
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

        self._rag_agent: Agent | None = None

        self._semantic_search_tool = None
        self._semantic_search_available = False

        if has_semantic_dependencies():
            from codebase_rag.tools.semantic_search import (
                create_semantic_search_tool,
            )

            self._semantic_search_tool = create_semantic_search_tool(self.ingestor)
            self._semantic_search_available = True
        else:
            logger.info(lg.MCP_SEMANTIC_NOT_AVAILABLE)

        self._tools: dict[str, ToolMetadata] = {
            cs.MCPToolName.LIST_PROJECTS: ToolMetadata(
                name=cs.MCPToolName.LIST_PROJECTS,
                description=td.MCP_TOOLS[cs.MCPToolName.LIST_PROJECTS],
                input_schema=MCPInputSchema(
                    type=cs.MCPSchemaType.OBJECT,
                    properties={},
                    required=[],
                ),
                handler=self.list_projects,
                returns_json=True,
            ),
            cs.MCPToolName.DELETE_PROJECT: ToolMetadata(
                name=cs.MCPToolName.DELETE_PROJECT,
                description=td.MCP_TOOLS[cs.MCPToolName.DELETE_PROJECT],
                input_schema=MCPInputSchema(
                    type=cs.MCPSchemaType.OBJECT,
                    properties={
                        cs.MCPParamName.PROJECT_NAME: MCPInputSchemaProperty(
                            type=cs.MCPSchemaType.STRING,
                            description=td.MCP_PARAM_PROJECT_NAME,
                        )
                    },
                    required=[cs.MCPParamName.PROJECT_NAME],
                ),
                handler=self.delete_project,
                returns_json=True,
            ),
            cs.MCPToolName.WIPE_DATABASE: ToolMetadata(
                name=cs.MCPToolName.WIPE_DATABASE,
                description=td.MCP_TOOLS[cs.MCPToolName.WIPE_DATABASE],
                input_schema=MCPInputSchema(
                    type=cs.MCPSchemaType.OBJECT,
                    properties={
                        cs.MCPParamName.CONFIRM: MCPInputSchemaProperty(
                            type=cs.MCPSchemaType.BOOLEAN,
                            description=td.MCP_PARAM_CONFIRM,
                        )
                    },
                    required=[cs.MCPParamName.CONFIRM],
                ),
                handler=self.wipe_database,
                returns_json=False,
            ),
            cs.MCPToolName.INDEX_REPOSITORY: ToolMetadata(
                name=cs.MCPToolName.INDEX_REPOSITORY,
                description=td.MCP_TOOLS[cs.MCPToolName.INDEX_REPOSITORY],
                input_schema=MCPInputSchema(
                    type=cs.MCPSchemaType.OBJECT,
                    properties={},
                    required=[],
                ),
                handler=self.index_repository,
                returns_json=False,
            ),
            cs.MCPToolName.UPDATE_REPOSITORY: ToolMetadata(
                name=cs.MCPToolName.UPDATE_REPOSITORY,
                description=td.MCP_TOOLS[cs.MCPToolName.UPDATE_REPOSITORY],
                input_schema=MCPInputSchema(
                    type=cs.MCPSchemaType.OBJECT,
                    properties={},
                    required=[],
                ),
                handler=self.update_repository,
                returns_json=False,
            ),
            cs.MCPToolName.REINGEST: ToolMetadata(
                name=cs.MCPToolName.REINGEST,
                description=td.MCP_TOOLS[cs.MCPToolName.REINGEST],
                input_schema=MCPInputSchema(
                    type=cs.MCPSchemaType.OBJECT,
                    properties={
                        cs.MCPParamName.PATHS: MCPInputSchemaProperty(
                            type=cs.MCPSchemaType.ARRAY,
                            description=td.MCP_PARAM_REINGEST_PATHS,
                            items={cs.MCPSchemaField.TYPE: cs.MCPSchemaType.STRING},
                        ),
                        cs.MCPParamName.DELETED: MCPInputSchemaProperty(
                            type=cs.MCPSchemaType.ARRAY,
                            description=td.MCP_PARAM_REINGEST_DELETED,
                            items={cs.MCPSchemaField.TYPE: cs.MCPSchemaType.STRING},
                        ),
                    },
                    required=[cs.MCPParamName.PATHS],
                ),
                handler=self.reingest,
                returns_json=True,
            ),
            cs.MCPToolName.RESOLVE: self._graph_tool(
                cs.MCPToolName.RESOLVE,
                {cs.MCPParamName.TARGET: td.MCP_PARAM_TARGET},
                [cs.MCPParamName.TARGET],
                self.resolve,
            ),
            cs.MCPToolName.DEFINITION: self._graph_tool(
                cs.MCPToolName.DEFINITION,
                {cs.MCPParamName.QUALIFIED_NAME: td.MCP_PARAM_QUALIFIED_NAME},
                [cs.MCPParamName.QUALIFIED_NAME],
                self.definition,
            ),
            cs.MCPToolName.CALLERS: self._graph_tool(
                cs.MCPToolName.CALLERS,
                {
                    cs.MCPParamName.QUALIFIED_NAME: td.MCP_PARAM_QUALIFIED_NAME,
                    cs.MCPParamName.DEPTH: td.MCP_PARAM_DEPTH,
                },
                [cs.MCPParamName.QUALIFIED_NAME],
                self.callers,
                integer_params={cs.MCPParamName.DEPTH},
            ),
            cs.MCPToolName.CALLEES: self._graph_tool(
                cs.MCPToolName.CALLEES,
                {
                    cs.MCPParamName.QUALIFIED_NAME: td.MCP_PARAM_QUALIFIED_NAME,
                    cs.MCPParamName.DEPTH: td.MCP_PARAM_DEPTH,
                },
                [cs.MCPParamName.QUALIFIED_NAME],
                self.callees,
                integer_params={cs.MCPParamName.DEPTH},
            ),
            cs.MCPToolName.IMPLEMENTORS: self._graph_tool(
                cs.MCPToolName.IMPLEMENTORS,
                {cs.MCPParamName.QUALIFIED_NAME: td.MCP_PARAM_QUALIFIED_NAME},
                [cs.MCPParamName.QUALIFIED_NAME],
                self.implementors,
            ),
            cs.MCPToolName.OVERRIDES: self._graph_tool(
                cs.MCPToolName.OVERRIDES,
                {cs.MCPParamName.QUALIFIED_NAME: td.MCP_PARAM_QUALIFIED_NAME},
                [cs.MCPParamName.QUALIFIED_NAME],
                self.overrides,
            ),
            cs.MCPToolName.IMPORTERS: self._graph_tool(
                cs.MCPToolName.IMPORTERS,
                {cs.MCPParamName.MODULE_QN: td.MCP_PARAM_MODULE_QN},
                [cs.MCPParamName.MODULE_QN],
                self.importers,
            ),
            cs.MCPToolName.TESTS_REACHING: self._graph_tool(
                cs.MCPToolName.TESTS_REACHING,
                {cs.MCPParamName.QUALIFIED_NAME: td.MCP_PARAM_QUALIFIED_NAME},
                [cs.MCPParamName.QUALIFIED_NAME],
                self.tests_reaching,
            ),
            cs.MCPToolName.QUERY_CODE_GRAPH: ToolMetadata(
                name=cs.MCPToolName.QUERY_CODE_GRAPH,
                description=td.MCP_TOOLS[cs.MCPToolName.QUERY_CODE_GRAPH],
                input_schema=MCPInputSchema(
                    type=cs.MCPSchemaType.OBJECT,
                    properties={
                        cs.MCPParamName.NATURAL_LANGUAGE_QUERY: MCPInputSchemaProperty(
                            type=cs.MCPSchemaType.STRING,
                            description=td.MCP_PARAM_NATURAL_LANGUAGE_QUERY,
                        ),
                        # Declared as well as accepted: a client reads this
                        # schema to learn what it may send, so omitting it
                        # made per-request scoping undiscoverable (#1494).
                        cs.MCPParamName.PROJECT: MCPInputSchemaProperty(
                            type=cs.MCPSchemaType.STRING,
                            description=td.MCP_PARAM_PROJECT,
                        ),
                    },
                    required=[cs.MCPParamName.NATURAL_LANGUAGE_QUERY],
                ),
                handler=self.query_code_graph,
                returns_json=True,
            ),
            cs.MCPToolName.GET_CODE_SNIPPET: ToolMetadata(
                name=cs.MCPToolName.GET_CODE_SNIPPET,
                description=td.MCP_TOOLS[cs.MCPToolName.GET_CODE_SNIPPET],
                input_schema=MCPInputSchema(
                    type=cs.MCPSchemaType.OBJECT,
                    properties={
                        cs.MCPParamName.QUALIFIED_NAME: MCPInputSchemaProperty(
                            type=cs.MCPSchemaType.STRING,
                            description=td.MCP_PARAM_QUALIFIED_NAME,
                        )
                    },
                    required=[cs.MCPParamName.QUALIFIED_NAME],
                ),
                handler=self.get_code_snippet,
                returns_json=True,
            ),
            cs.MCPToolName.SURGICAL_REPLACE_CODE: ToolMetadata(
                name=cs.MCPToolName.SURGICAL_REPLACE_CODE,
                description=td.MCP_TOOLS[cs.MCPToolName.SURGICAL_REPLACE_CODE],
                input_schema=MCPInputSchema(
                    type=cs.MCPSchemaType.OBJECT,
                    properties={
                        cs.MCPParamName.FILE_PATH: MCPInputSchemaProperty(
                            type=cs.MCPSchemaType.STRING,
                            description=td.MCP_PARAM_FILE_PATH,
                        ),
                        cs.MCPParamName.TARGET_CODE: MCPInputSchemaProperty(
                            type=cs.MCPSchemaType.STRING,
                            description=td.MCP_PARAM_TARGET_CODE,
                        ),
                        cs.MCPParamName.REPLACEMENT_CODE: MCPInputSchemaProperty(
                            type=cs.MCPSchemaType.STRING,
                            description=td.MCP_PARAM_REPLACEMENT_CODE,
                        ),
                    },
                    required=[
                        cs.MCPParamName.FILE_PATH,
                        cs.MCPParamName.TARGET_CODE,
                        cs.MCPParamName.REPLACEMENT_CODE,
                    ],
                ),
                handler=self.surgical_replace_code,
                returns_json=False,
            ),
            cs.MCPToolName.READ_FILE: ToolMetadata(
                name=cs.MCPToolName.READ_FILE,
                description=td.MCP_TOOLS[cs.MCPToolName.READ_FILE],
                input_schema=MCPInputSchema(
                    type=cs.MCPSchemaType.OBJECT,
                    properties={
                        cs.MCPParamName.FILE_PATH: MCPInputSchemaProperty(
                            type=cs.MCPSchemaType.STRING,
                            description=td.MCP_PARAM_FILE_PATH,
                        ),
                        cs.MCPParamName.OFFSET: MCPInputSchemaProperty(
                            type=cs.MCPSchemaType.INTEGER,
                            description=td.MCP_PARAM_OFFSET,
                        ),
                        cs.MCPParamName.LIMIT: MCPInputSchemaProperty(
                            type=cs.MCPSchemaType.INTEGER,
                            description=td.MCP_PARAM_LIMIT,
                        ),
                    },
                    required=[cs.MCPParamName.FILE_PATH],
                ),
                handler=self.read_file,
                returns_json=False,
            ),
            cs.MCPToolName.WRITE_FILE: ToolMetadata(
                name=cs.MCPToolName.WRITE_FILE,
                description=td.MCP_TOOLS[cs.MCPToolName.WRITE_FILE],
                input_schema=MCPInputSchema(
                    type=cs.MCPSchemaType.OBJECT,
                    properties={
                        cs.MCPParamName.FILE_PATH: MCPInputSchemaProperty(
                            type=cs.MCPSchemaType.STRING,
                            description=td.MCP_PARAM_FILE_PATH,
                        ),
                        cs.MCPParamName.CONTENT: MCPInputSchemaProperty(
                            type=cs.MCPSchemaType.STRING,
                            description=td.MCP_PARAM_CONTENT,
                        ),
                    },
                    required=[
                        cs.MCPParamName.FILE_PATH,
                        cs.MCPParamName.CONTENT,
                    ],
                ),
                handler=self.write_file,
                returns_json=False,
            ),
            cs.MCPToolName.LIST_DIRECTORY: ToolMetadata(
                name=cs.MCPToolName.LIST_DIRECTORY,
                description=td.MCP_TOOLS[cs.MCPToolName.LIST_DIRECTORY],
                input_schema=MCPInputSchema(
                    type=cs.MCPSchemaType.OBJECT,
                    properties={
                        cs.MCPParamName.DIRECTORY_PATH: MCPInputSchemaProperty(
                            type=cs.MCPSchemaType.STRING,
                            description=td.MCP_PARAM_DIRECTORY_PATH,
                            default=cs.MCP_DEFAULT_DIRECTORY,
                        )
                    },
                    required=[],
                ),
                handler=self.list_directory,
                returns_json=False,
            ),
        }
        if self._semantic_search_available:
            self._tools[cs.MCPToolName.SEMANTIC_SEARCH] = ToolMetadata(
                name=cs.MCPToolName.SEMANTIC_SEARCH,
                description=td.MCP_TOOLS[cs.MCPToolName.SEMANTIC_SEARCH],
                input_schema=MCPInputSchema(
                    type=cs.MCPSchemaType.OBJECT,
                    properties={
                        cs.MCPParamName.NATURAL_LANGUAGE_QUERY: MCPInputSchemaProperty(
                            type=cs.MCPSchemaType.STRING,
                            description=td.MCP_PARAM_NATURAL_LANGUAGE_QUERY,
                        ),
                        cs.MCPParamName.TOP_K: MCPInputSchemaProperty(
                            type=cs.MCPSchemaType.INTEGER,
                            description=td.MCP_PARAM_TOP_K,
                            default=5,
                        ),
                        cs.MCPParamName.PROJECT: MCPInputSchemaProperty(
                            type=cs.MCPSchemaType.STRING,
                            description=td.MCP_PARAM_PROJECT,
                        ),
                    },
                    required=[cs.MCPParamName.NATURAL_LANGUAGE_QUERY],
                ),
                handler=self.semantic_search,
                returns_json=False,
            )

        if self._structural_available:
            self._tools[cs.MCPToolName.STRUCTURAL_SEARCH] = ToolMetadata(
                name=cs.MCPToolName.STRUCTURAL_SEARCH,
                description=td.MCP_TOOLS[cs.MCPToolName.STRUCTURAL_SEARCH],
                input_schema=MCPInputSchema(
                    type=cs.MCPSchemaType.OBJECT,
                    properties={
                        cs.MCPParamName.PATTERN: MCPInputSchemaProperty(
                            type=cs.MCPSchemaType.STRING,
                            description=td.MCP_PARAM_PATTERN,
                        ),
                        cs.MCPParamName.LANGUAGE: MCPInputSchemaProperty(
                            type=cs.MCPSchemaType.STRING,
                            description=td.MCP_PARAM_LANGUAGE,
                        ),
                    },
                    required=[cs.MCPParamName.PATTERN],
                ),
                handler=self.structural_search,
                returns_json=False,
            )
            self._tools[cs.MCPToolName.STRUCTURAL_REPLACE] = ToolMetadata(
                name=cs.MCPToolName.STRUCTURAL_REPLACE,
                description=td.MCP_TOOLS[cs.MCPToolName.STRUCTURAL_REPLACE],
                input_schema=MCPInputSchema(
                    type=cs.MCPSchemaType.OBJECT,
                    properties={
                        cs.MCPParamName.PATTERN: MCPInputSchemaProperty(
                            type=cs.MCPSchemaType.STRING,
                            description=td.MCP_PARAM_PATTERN,
                        ),
                        cs.MCPParamName.REWRITE: MCPInputSchemaProperty(
                            type=cs.MCPSchemaType.STRING,
                            description=td.MCP_PARAM_REWRITE,
                        ),
                        cs.MCPParamName.LANGUAGE: MCPInputSchemaProperty(
                            type=cs.MCPSchemaType.STRING,
                            description=td.MCP_PARAM_LANGUAGE,
                        ),
                        cs.MCPParamName.DRY_RUN: MCPInputSchemaProperty(
                            type=cs.MCPSchemaType.BOOLEAN,
                            description=td.MCP_PARAM_DRY_RUN,
                            default=True,
                        ),
                    },
                    required=[cs.MCPParamName.PATTERN, cs.MCPParamName.REWRITE],
                ),
                handler=self.structural_replace,
                returns_json=False,
            )

        # Unconditional: both read the graph only. get_function_source lives in
        # semantic_search.py but touches none of the embedding machinery, so
        # gating it on the semantic extra would hide a graph-only tool behind
        # a dependency it never uses.
        self._tools[cs.MCPToolName.FIND_DUPLICATE_CODE] = ToolMetadata(
            name=cs.MCPToolName.FIND_DUPLICATE_CODE,
            description=td.MCP_TOOLS[cs.MCPToolName.FIND_DUPLICATE_CODE],
            input_schema=MCPInputSchema(
                type=cs.MCPSchemaType.OBJECT,
                properties={
                    cs.MCPParamName.PROJECT: MCPInputSchemaProperty(
                        type=cs.MCPSchemaType.STRING,
                        description=td.MCP_PARAM_PROJECT,
                    ),
                    cs.MCPParamName.THRESHOLD: MCPInputSchemaProperty(
                        type=cs.MCPSchemaType.NUMBER,
                        description=td.MCP_PARAM_THRESHOLD,
                        default=cs.DUPLICATES_DEFAULT_THRESHOLD,
                    ),
                    cs.MCPParamName.MIN_SIZE: MCPInputSchemaProperty(
                        type=cs.MCPSchemaType.INTEGER,
                        description=td.MCP_PARAM_MIN_SIZE,
                        default=cs.DUPLICATES_DEFAULT_MIN_NODES,
                    ),
                    cs.MCPParamName.LIMIT: MCPInputSchemaProperty(
                        type=cs.MCPSchemaType.INTEGER,
                        description=td.MCP_PARAM_DUPLICATES_LIMIT,
                        default=cs.DUPLICATES_DEFAULT_GROUP_LIMIT,
                    ),
                },
                required=[],
            ),
            handler=self.find_duplicate_code,
            returns_json=False,
        )
        self._tools[cs.MCPToolName.GET_FUNCTION_SOURCE] = ToolMetadata(
            name=cs.MCPToolName.GET_FUNCTION_SOURCE,
            description=td.MCP_TOOLS[cs.MCPToolName.GET_FUNCTION_SOURCE],
            input_schema=MCPInputSchema(
                type=cs.MCPSchemaType.OBJECT,
                properties={
                    cs.MCPParamName.NODE_ID: MCPInputSchemaProperty(
                        type=cs.MCPSchemaType.INTEGER,
                        description=td.MCP_PARAM_NODE_ID,
                    )
                },
                required=[cs.MCPParamName.NODE_ID],
            ),
            handler=self.get_function_source,
            returns_json=False,
        )

        self._tools[cs.MCPToolName.ASK_AGENT] = ToolMetadata(
            name=cs.MCPToolName.ASK_AGENT,
            description=td.MCP_TOOLS[cs.MCPToolName.ASK_AGENT],
            input_schema=MCPInputSchema(
                type=cs.MCPSchemaType.OBJECT,
                properties={
                    cs.MCPParamName.QUESTION: MCPInputSchemaProperty(
                        type=cs.MCPSchemaType.STRING,
                        description=td.MCP_PARAM_QUESTION,
                    )
                },
                required=[cs.MCPParamName.QUESTION],
            ),
            handler=self.ask_agent,
            returns_json=True,
        )

        self._tools[cs.MCPToolName.FLOW_VERDICT] = ToolMetadata(
            name=cs.MCPToolName.FLOW_VERDICT,
            description=td.MCP_TOOLS[cs.MCPToolName.FLOW_VERDICT],
            input_schema=MCPInputSchema(
                type=cs.MCPSchemaType.OBJECT,
                properties={
                    cs.MCPParamName.SOURCE_QN: MCPInputSchemaProperty(
                        type=cs.MCPSchemaType.STRING,
                        description=td.MCP_PARAM_SOURCE_QN,
                    ),
                    cs.MCPParamName.SINK_QN: MCPInputSchemaProperty(
                        type=cs.MCPSchemaType.STRING,
                        description=td.MCP_PARAM_SINK_QN,
                    ),
                },
                required=[cs.MCPParamName.SOURCE_QN, cs.MCPParamName.SINK_QN],
            ),
            handler=self.flow_verdict,
            returns_json=True,
        )

        traceback_schema = MCPInputSchema(
            type=cs.MCPSchemaType.OBJECT,
            properties={
                cs.MCPParamName.TRACEBACK_TEXT: MCPInputSchemaProperty(
                    type=cs.MCPSchemaType.STRING,
                    description=td.MCP_PARAM_TRACEBACK_TEXT,
                )
            },
            required=[cs.MCPParamName.TRACEBACK_TEXT],
        )
        self._tools[cs.MCPToolName.EXPLAIN_TRACEBACK] = ToolMetadata(
            name=cs.MCPToolName.EXPLAIN_TRACEBACK,
            description=td.MCP_TOOLS[cs.MCPToolName.EXPLAIN_TRACEBACK],
            input_schema=traceback_schema,
            handler=self.explain_traceback,
            returns_json=True,
        )
        self._tools[cs.MCPToolName.RANK_ROOT_CAUSES] = ToolMetadata(
            name=cs.MCPToolName.RANK_ROOT_CAUSES,
            description=td.MCP_TOOLS[cs.MCPToolName.RANK_ROOT_CAUSES],
            input_schema=traceback_schema,
            handler=self.rank_root_causes,
            returns_json=True,
        )

    @property
    def rag_agent(self) -> Agent:
        if self._rag_agent is None:
            tools = [
                self._query_tool,
                self._code_tool,
                self._file_reader_tool,
                self._file_writer_tool,
                self._file_editor_tool,
                self._shell_command_tool,
                self._directory_lister_tool,
                # The same instances the direct MCP tools use: a second copy
                # would give the orchestrator its own roots cache and let the
                # two routes disagree about a project indexed mid-session.
                self._function_source_tool,
                self._find_duplicates_tool,
            ]
            if self._semantic_search_tool is not None:
                tools.append(self._semantic_search_tool)
            if self._structural_available:
                tools.append(self._structural_search_tool)
                tools.append(self._structural_editor_tool)
            self._rag_agent, _ = create_rag_orchestrator(
                tools=tools, project_root=Path(self.project_root)
            )
        return self._rag_agent

    # Setter lets tests inject a mock agent without triggering LLM init
    @rag_agent.setter
    def rag_agent(self, value: Agent) -> None:
        self._rag_agent = value

    async def flow_verdict(
        self, source_qualified_name: str, sink_qualified_name: str
    ) -> dict:
        from codebase_rag.flow_verdict import flow_reachability_verdict

        project = derive_project_name(Path(self.project_root))
        # The edge scan and coverage read must see one consistent graph:
        # index/update handlers hold this lock while they delete and
        # rebuild, and an interleaved read would mix generations.
        async with self._ingestor_lock:
            result = await asyncio.to_thread(
                flow_reachability_verdict,
                self.ingestor.fetch_all,
                project,
                source_qualified_name,
                sink_qualified_name,
            )
        return {
            "verdict": result.verdict,
            "path": list(result.path),
            "gaps": list(result.gaps),
        }

    async def explain_traceback(self, traceback_text: str) -> dict:
        from codebase_rag.crash_correlation import explain_traceback

        project = derive_project_name(Path(self.project_root))
        async with self._ingestor_lock:
            report = await asyncio.to_thread(
                explain_traceback,
                self.ingestor.fetch_all,
                project,
                Path(self.project_root),
                traceback_text,
            )
        return {
            "exception_type": report.exception_type,
            "exception_message": report.exception_message,
            "frames": [frame._asdict() for frame in report.frames],
            "flow_gaps": list(report.flow_gaps),
            # `rate` is a property, so `_asdict()` omits it; mapped explicitly
            # because the ratio is the number the caller acts on (issue #227).
            "resolution": {
                "total": report.resolution.total,
                "resolved": report.resolution.resolved,
                "rate": report.resolution.rate,
            },
        }

    async def rank_root_causes(self, traceback_text: str) -> dict:
        from codebase_rag.crash_correlation import rank_root_causes

        project = derive_project_name(Path(self.project_root))
        async with self._ingestor_lock:
            report = await asyncio.to_thread(
                rank_root_causes,
                self.ingestor.fetch_all,
                project,
                Path(self.project_root),
                traceback_text,
            )
        return {
            "exception_type": report.exception_type,
            "exception_message": report.exception_message,
            "failing": report.failing,
            "anchor_is_crash_site": report.anchor_is_crash_site,
            "candidates": [candidate._asdict() for candidate in report.candidates],
            "flow_used": report.flow_used,
            "flow_gaps": list(report.flow_gaps),
        }

    async def list_projects(self) -> ListProjectsResult:
        logger.info(lg.MCP_LISTING_PROJECTS)
        try:
            # Serialise against index/update, which delete and rebuild the
            # graph under this lock; an interleaved read mixes generations.
            async with self._ingestor_lock:
                projects = await asyncio.to_thread(self.ingestor.list_projects)
            return ListProjectsSuccessResult(projects=projects, count=len(projects))
        except Exception as e:
            logger.error(lg.MCP_ERROR_LIST_PROJECTS.format(error=e))
            return ListProjectsErrorResult(error=str(e), projects=[], count=0)

    def _get_project_node_ids(self, project_name: str) -> list[int]:
        rows = self.ingestor.fetch_all(
            cs.CYPHER_QUERY_PROJECT_NODE_IDS,
            {cs.KEY_PROJECT_NAME: project_name},
        )
        result: list[int] = []
        for row in rows:
            node_id = row.get(cs.KEY_NODE_ID)
            if isinstance(node_id, int):
                result.append(node_id)
        return result

    def _cleanup_project_embeddings(self, project_name: str) -> None:
        node_ids = self._get_project_node_ids(project_name)
        delete_project_embeddings(project_name, node_ids)

    def _delete_project_sync(self, project_name: str) -> DeleteProjectResult:
        projects = self.ingestor.list_projects()
        if project_name not in projects:
            return DeleteProjectErrorResult(
                success=False,
                error=te.MCP_PROJECT_NOT_FOUND.format(
                    project_name=project_name, projects=projects
                ),
            )
        # Before the first write, not after: a delete that fails part way
        # has already removed graph data the retained updater still
        # describes, and the project may still be listed, so only the
        # incomplete flag stops the next reingest from resolving against
        # those definitions over what is left. After a completed delete the
        # project is gone and the not-indexed guard takes over.
        self._live_updater = None
        self._graph_incomplete = True
        self._cleanup_project_embeddings(project_name)
        self.ingestor.delete_project(project_name)
        self._graph_incomplete = False
        return DeleteProjectSuccessResult(
            success=True,
            project=project_name,
            message=cs.MCP_PROJECT_DELETED.format(project_name=project_name),
        )

    async def delete_project(self, project_name: str) -> DeleteProjectResult:
        logger.info(lg.MCP_DELETING_PROJECT.format(project_name=project_name))
        try:
            async with self._ingestor_lock:
                return await asyncio.to_thread(self._delete_project_sync, project_name)
        except Exception as e:
            logger.error(lg.MCP_ERROR_DELETE_PROJECT.format(error=e))
            return DeleteProjectErrorResult(success=False, error=str(e))

    async def wipe_database(self, confirm: bool) -> str:
        if not confirm:
            return cs.MCP_WIPE_CANCELLED
        logger.warning(lg.MCP_WIPING_DATABASE)
        try:
            async with self._ingestor_lock:
                # Same posture as delete_project: dropped and marked before
                # the wipe, which can fail part way; cleared once the graph
                # is gone, when the not-indexed guard covers the rest. The
                # embedding sweep after it cannot leave a partial graph, so
                # its failure keeps the updater dropped and nothing else.
                self._live_updater = None
                self._graph_incomplete = True
                await asyncio.to_thread(self.ingestor.clean_database)
                self._graph_incomplete = False
                await asyncio.to_thread(clear_all_embeddings)
            return cs.MCP_WIPE_SUCCESS
        except Exception as e:
            logger.error(lg.MCP_ERROR_WIPE.format(error=e))
            return cs.MCP_WIPE_ERROR.format(error=e)

    def _ignore_sets(self) -> tuple[frozenset[str] | None, frozenset[str] | None]:
        # The CLI resolves `.cgrignore` and the root `.gitignore` through
        # `load_ignore_patterns` before every ingest; MCP omitted it entirely,
        # so an MCP-driven run parsed what the ignore files exclude and built a
        # different graph from the CLI for the same repo (#1616). MCP has no
        # `--exclude` flag and no interactive setup, so that loader is the whole
        # contract here -- the CLI's extra `cli_excludes` union and
        # `prompt_for_unignored_directories` branch have no MCP counterpart.
        # `or None` mirrors the CLI's own `... or None` idiom and the
        # parameters' default. It is call-shape parity, not behaviour: every
        # consumer gates on truthiness (`if exclude_paths and ...` in
        # should_skip_path/should_skip_rel_file/should_keep_dir), so an empty
        # frozenset and None are indistinguishable downstream.
        patterns = load_ignore_patterns(Path(self.project_root))
        return patterns.exclude or None, patterns.unignore or None

    def _index_repository_sync(self) -> str:
        # Same collision-resistant derivation as the CLI: a bare directory
        # name would let two repos named alike delete each other's graphs.
        project_name = derive_project_name(Path(self.project_root))
        logger.info(lg.MCP_CLEARING_PROJECT.format(project_name=project_name))
        # Before the first write, not after: the delete and the flushes are
        # autocommit writes that can fail part way. From here until the
        # rebuild completes, a later reingest must refuse rather than
        # resolve against the retained updater's definitions or against
        # whatever partial graph a failure left behind.
        self._live_updater = None
        self._graph_incomplete = True
        self._cleanup_project_embeddings(project_name)
        self.ingestor.delete_project(project_name)

        self.ingestor.ensure_constraints()
        self.ingestor.flush_all()

        exclude_paths, unignore_paths = self._ignore_sets()
        updater = GraphUpdater(
            ingestor=self.ingestor,
            repo_path=Path(self.project_root),
            parsers=self.parsers,
            queries=self.queries,
            unignore_paths=unignore_paths,
            exclude_paths=exclude_paths,
            project_name=project_name,
        )
        updater.run()
        self.ingestor.flush_all()
        self._graph_incomplete = False
        self._live_updater = updater

        return cs.MCP_INDEX_SUCCESS_PROJECT.format(
            path=self.project_root, project_name=project_name
        )

    async def _run_ingest(
        self,
        sync_fn: Callable[[], str],
        start_log: str,
        error_log: str,
        error_message: str,
    ) -> str:
        # Indexing and updating differ only in their sync body and their three
        # messages; the lock/thread/error protocol is one implementation.
        logger.info(start_log.format(path=self.project_root))
        try:
            async with self._ingestor_lock:
                return await asyncio.to_thread(sync_fn)
        except Exception as e:
            logger.error(error_log.format(error=e))
            return error_message.format(error=e)

    async def index_repository(self) -> str:
        return await self._run_ingest(
            self._index_repository_sync,
            lg.MCP_INDEXING_REPO,
            lg.MCP_ERROR_INDEXING,
            cs.MCP_INDEX_ERROR,
        )

    def _update_repository_sync(self) -> str:
        project_name = derive_project_name(Path(self.project_root))

        # Dropped and marked before the first write: the initial flush
        # commits batches left by earlier calls and can fail part way, and
        # the run itself mutates the graph before it can fail. Either way
        # the retained updater would describe the graph as it was, and a
        # later reingest would resolve against definitions the partial
        # update has already replaced. The incomplete flag makes that
        # reingest refuse until an update completes, since hydrating from
        # the partial graph would be no better.
        self._live_updater = None
        self._graph_incomplete = True
        self.ingestor.ensure_constraints()
        self.ingestor.flush_all()

        exclude_paths, unignore_paths = self._ignore_sets()
        updater = GraphUpdater(
            ingestor=self.ingestor,
            repo_path=Path(self.project_root),
            parsers=self.parsers,
            queries=self.queries,
            unignore_paths=unignore_paths,
            exclude_paths=exclude_paths,
            project_name=project_name,
        )
        updater.run()
        self.ingestor.flush_all()
        self._graph_incomplete = False
        self._live_updater = updater
        return cs.MCP_UPDATE_SUCCESS.format(path=self.project_root)

    def _updater_for_reingest(self) -> GraphUpdater:
        updater = self._live_updater
        if updater is None:
            # A scoped re-ingest completes a graph; it cannot stand in for
            # the first index. After delete_project or wipe_database the
            # project is gone, and hydrating from nothing would leave every
            # unrelated definition missing.
            project_name = derive_project_name(Path(self.project_root))
            if self._graph_incomplete:
                raise ValueError(
                    cs.MCP_REINGEST_AFTER_FAILED_RUN.format(project=project_name)
                )
            if project_name not in self.ingestor.list_projects():
                raise ValueError(
                    cs.MCP_REINGEST_NEEDS_INDEX.format(project=project_name)
                )
            self.ingestor.ensure_constraints()
            # The same exclusion set the index and update paths use, or an
            # agent-named path under a CLI-excluded directory would be
            # indexed here and kept by every later update.
            exclude_paths, unignore_paths = self._ignore_sets()
            updater = GraphUpdater(
                ingestor=self.ingestor,
                repo_path=Path(self.project_root),
                parsers=self.parsers,
                queries=self.queries,
                unignore_paths=unignore_paths,
                exclude_paths=exclude_paths,
                project_name=project_name,
            )
            self._live_updater = updater
        return updater

    def _reingest_sync(
        self, paths: list[str], deleted: list[str]
    ) -> ReingestToolResult:
        updater = self._updater_for_reingest()
        try:
            report = updater.reingest(paths, deleted=deleted)
        except (ValueError, ReingestAborted):
            # A refusal (a path outside the repository, a directory) is
            # raised while the paths are split, and an abort while the call
            # was still reading the graph; neither touched anything, so the
            # updater is still valid and the call may simply be retried.
            raise
        except Exception:
            # The run may have deleted the affected subtrees and never
            # rebuilt them: the retained updater describes a graph that no
            # longer exists, and the next scoped call must not reuse it
            # over that partial state. update_repository is the recovery.
            self._live_updater = None
            self._graph_incomplete = True
            raise
        return ReingestToolResult(
            reparsed=list(report.reparsed),
            affected=list(report.affected),
            removed=list(report.removed),
            skipped=list(report.skipped),
            elapsed_ms=round(report.elapsed_ms, 1),
        )

    async def reingest(
        self, paths: list[str], deleted: list[str] | None = None
    ) -> ReingestToolResult:
        logger.info(lg.MCP_REINGESTING.format(count=len(paths)))
        try:
            async with self._ingestor_lock:
                return await asyncio.to_thread(
                    self._reingest_sync, list(paths), list(deleted or ())
                )
        except Exception as e:
            logger.error(lg.MCP_ERROR_REINGEST.format(error=e))
            return ReingestToolResult(
                error=cs.MCP_REINGEST_ERROR.format(error=e),
                reparsed=[],
                affected=[],
                removed=[],
                skipped=[],
                elapsed_ms=0.0,
            )

    async def update_repository(self) -> str:
        return await self._run_ingest(
            self._update_repository_sync,
            lg.MCP_UPDATING_REPO,
            lg.MCP_ERROR_UPDATING,
            cs.MCP_UPDATE_ERROR,
        )

    async def semantic_search(
        self, natural_language_query: str, top_k: int = 5, project: str | None = None
    ) -> str:
        assert self._semantic_search_tool is not None
        logger.info(lg.MCP_SEMANTIC_SEARCH.format(query=natural_language_query))
        # The underlying tool already accepted `project` and passes it to
        # `search_embeddings`, which filters in the vector store. Only this
        # handler failed to forward it, so scoping here is plumbing rather
        # than a second filter (issue #1494).
        if project is not None:
            known = await asyncio.to_thread(self.ingestor.list_projects)
            if project not in known:
                return cs.MCP_UNKNOWN_PROJECT.format(
                    project=project, known=cs.SEPARATOR_COMMA_SPACE.join(known)
                )
        # Serialise against index/update, which delete and rebuild the graph
        # under this lock; an interleaved read mixes generations.
        async with self._ingestor_lock:
            result = await self._semantic_search_tool.function(
                query=natural_language_query, top_k=top_k, project=project
            )
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
        async with self._ingestor_lock:
            result = await self._find_duplicates_tool.function(
                project=project, threshold=threshold, min_size=min_size, limit=limit
            )
        return str(result)

    async def get_function_source(self, node_id: int) -> str:
        """Fetch a function's source by graph node id (issue #1342).

        A node id is only meaningful within one generation of the graph, so
        the lookup must not straddle a rebuild that could reassign it.
        """
        async with self._ingestor_lock:
            result = await self._function_source_tool.function(node_id=node_id)
        return str(result)

    async def structural_search(self, pattern: str, language: str | None = None) -> str:
        result = await self._structural_search_tool.function(
            pattern=pattern, language=language
        )
        return str(result)

    async def structural_replace(
        self,
        pattern: str,
        rewrite: str,
        language: str | None = None,
        dry_run: bool = True,
    ) -> str:
        # The write and the re-ingest that follows it share the lock, so a
        # rebuild cannot land between the two (issue #1525).
        async with self._ingestor_lock:
            self._structural_written = []
            result = await self._structural_editor_tool.function(
                pattern=pattern, rewrite=rewrite, language=language, dry_run=dry_run
            )
            written = list(self._structural_written)
            if dry_run or not written:
                return str(result)
            return str(result) + await asyncio.to_thread(
                self._delta_after_write, written
            )

    def _note_structural_changes(self, changes: list[StructuralReplaceChange]) -> None:
        self._structural_written = [c["file"] for c in changes if c["applied"]]

    def _delta_after_write(self, paths: list[str]) -> str:
        """The structural delta of a write, as text to append to its result.

        Never fails the write: a project that is not indexed yields nothing,
        and any other failure is reported in place of the delta.
        """
        root = Path(self.project_root)
        relative = sd.normalise_paths(paths, root)
        try:
            if self._live_updater is None and (
                derive_project_name(root) not in self.ingestor.list_projects()
            ):
                return ""
            updater = self._updater_for_reingest()
            deleted = [p for p in relative if not (root / p).exists()]
            changed = [p for p in relative if p not in deleted]
            delta = sd.observe(
                self.ingestor.fetch_all,
                updater.project_name,
                relative,
                lambda: updater.reingest(changed, deleted=deleted),
                repo_root=root,
            )
        except Exception as e:
            logger.warning(lg.MCP_DELTA_FAILED.format(error=e))
            return "\n\n" + cs.MCP_DELTA_ERROR.format(error=e)
        return (
            "\n\n"
            + cs.MCP_DELTA_HEADER
            + "\n"
            + json.dumps(delta, indent=cs.MCP_JSON_INDENT)
        )

    async def ask_agent(self, question: str) -> dict[str, str]:
        logger.info(lg.MCP_ASK_AGENT.format(question=question))
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
                response = await self.rag_agent.run(question, message_history=[])
            return {"output": str(response.output)}
        except Exception as e:
            logger.error(lg.MCP_ASK_AGENT_ERROR.format(error=e))
            return {"error": cs.MCP_ASK_AGENT_ERROR.format(error=e)}

    # --- deterministic graph queries (issue #1523) -------------------------------

    def _graph_tool(
        self,
        name: cs.MCPToolName,
        params: dict[str, str],
        required: list[str],
        handler: MCPHandlerType,
        integer_params: set[str] | None = None,
    ) -> ToolMetadata:
        properties = {
            key: MCPInputSchemaProperty(
                type=cs.MCPSchemaType.INTEGER
                if integer_params and key in integer_params
                else cs.MCPSchemaType.STRING,
                description=description,
            )
            for key, description in params.items()
        }
        properties[cs.MCPParamName.PROJECT] = MCPInputSchemaProperty(
            type=cs.MCPSchemaType.STRING, description=td.MCP_PARAM_PROJECT
        )
        return ToolMetadata(
            name=name,
            description=td.MCP_TOOLS[name],
            input_schema=MCPInputSchema(
                type=cs.MCPSchemaType.OBJECT, properties=properties, required=required
            ),
            handler=handler,
            returns_json=True,
        )

    async def _graph_query(
        self,
        tool: cs.MCPToolName,
        project: str | None,
        run: Callable[[str], object],
    ) -> object:
        # Every path that reads the graph, the project check included, runs
        # under the ingestor lock so an index/update rebuild cannot tear the
        # read (issue #1471). A typo in `project` would otherwise read as a
        # genuine empty result; the default is the project this server's
        # root derives to.
        try:
            async with self._ingestor_lock:
                if project is not None:
                    known = await asyncio.to_thread(self.ingestor.list_projects)
                    if project not in known:
                        return {
                            cs.DICT_KEY_ERROR: cs.MCP_UNKNOWN_PROJECT.format(
                                project=project,
                                known=cs.SEPARATOR_COMMA_SPACE.join(known),
                            )
                        }
                project_name = project or derive_project_name(Path(self.project_root))
                return await asyncio.to_thread(run, project_name)
        except Exception as e:
            logger.error(lg.MCP_GRAPH_QUERY_ERROR.format(tool=tool, error=e))
            return {
                cs.DICT_KEY_ERROR: cs.MCP_GRAPH_QUERY_ERROR.format(tool=tool, error=e)
            }

    @staticmethod
    def _depth(depth: int | None) -> int:
        return max(1, min(int(depth or 1), cs.GRAPH_QUERY_MAX_DEPTH))

    async def resolve(self, target: str, project: str | None = None) -> object:
        return await self._graph_query(
            cs.MCPToolName.RESOLVE,
            project,
            lambda name: graph_query.resolve(self.ingestor.fetch_all, name, target),
        )

    def _source_root_for(self, project_name: str) -> Path | None:
        # Source is read from disk only when the selected project was indexed
        # from this server's repository (its Project node's stored root):
        # another project's definition carries a relative path that may also
        # exist here and would read the wrong file, and a matching name alone
        # does not prove the root, so its span is answered without source.
        return graph_query.source_root_for(
            self.ingestor.fetch_all, project_name, Path(self.project_root)
        )

    async def definition(
        self, qualified_name: str, project: str | None = None
    ) -> object:
        return await self._graph_query(
            cs.MCPToolName.DEFINITION,
            project,
            lambda name: graph_query.definition(
                self.ingestor.fetch_all,
                name,
                qualified_name,
                self._source_root_for(name),
            ),
        )

    async def callers(
        self, qualified_name: str, depth: int | None = None, project: str | None = None
    ) -> object:
        return await self._graph_query(
            cs.MCPToolName.CALLERS,
            project,
            lambda name: graph_query.callers(
                self.ingestor.fetch_all, name, qualified_name, self._depth(depth)
            ),
        )

    async def callees(
        self, qualified_name: str, depth: int | None = None, project: str | None = None
    ) -> object:
        return await self._graph_query(
            cs.MCPToolName.CALLEES,
            project,
            lambda name: graph_query.callees(
                self.ingestor.fetch_all, name, qualified_name, self._depth(depth)
            ),
        )

    async def implementors(
        self, qualified_name: str, project: str | None = None
    ) -> object:
        return await self._graph_query(
            cs.MCPToolName.IMPLEMENTORS,
            project,
            lambda name: graph_query.implementors(
                self.ingestor.fetch_all, name, qualified_name
            ),
        )

    async def overrides(
        self, qualified_name: str, project: str | None = None
    ) -> object:
        return await self._graph_query(
            cs.MCPToolName.OVERRIDES,
            project,
            lambda name: graph_query.overrides(
                self.ingestor.fetch_all, name, qualified_name
            ),
        )

    async def importers(
        self, module_qualified_name: str, project: str | None = None
    ) -> object:
        return await self._graph_query(
            cs.MCPToolName.IMPORTERS,
            project,
            lambda name: graph_query.importers(
                self.ingestor.fetch_all, name, module_qualified_name
            ),
        )

    async def tests_reaching(
        self, qualified_name: str, project: str | None = None
    ) -> object:
        return await self._graph_query(
            cs.MCPToolName.TESTS_REACHING,
            project,
            lambda name: graph_query.tests_reaching(
                self.ingestor.fetch_all, name, qualified_name
            ),
        )

    async def query_code_graph(
        self, natural_language_query: str, project: str | None = None
    ) -> QueryResultDict:
        logger.info(lg.MCP_QUERY_CODE_GRAPH.format(query=natural_language_query))
        try:
            # Validated against the known projects first: a typo returning
            # zero rows is indistinguishable from a genuine empty result.
            if project is not None:
                known = await asyncio.to_thread(self.ingestor.list_projects)
                if project not in known:
                    return QueryResultDict(
                        error=cs.MCP_UNKNOWN_PROJECT.format(
                            project=project, known=cs.SEPARATOR_COMMA_SPACE.join(known)
                        ),
                        query_used=cs.QUERY_NOT_AVAILABLE,
                        results=[],
                        summary=cs.MCP_UNKNOWN_PROJECT.format(
                            project=project, known=cs.SEPARATOR_COMMA_SPACE.join(known)
                        ),
                    )
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
            async with self._ingestor_lock:
                graph_data = await query_tool.function(natural_language_query)
            result_dict: QueryResultDict = graph_data.model_dump()
            # The error key marks a scoping refusal; absent it means success,
            # so a None must not leak into the wire shape as a present key.
            if graph_data.error is None:
                result_dict.pop(cs.MCP_KEY_ERROR, None)
            logger.info(
                lg.MCP_QUERY_RESULTS.format(
                    count=len(result_dict.get(cs.DICT_KEY_RESULTS, []))
                )
            )
            return result_dict
        except Exception as e:
            logger.exception(lg.MCP_ERROR_QUERY.format(error=e))
            return QueryResultDict(
                error=str(e),
                query_used=cs.QUERY_NOT_AVAILABLE,
                results=[],
                summary=cs.MCP_TOOL_EXEC_ERROR.format(
                    name=cs.MCPToolName.QUERY_CODE_GRAPH, error=e
                ),
            )

    async def get_code_snippet(self, qualified_name: str) -> CodeSnippetResultDict:
        logger.info(lg.MCP_GET_CODE_SNIPPET.format(name=qualified_name))
        try:
            # Serialise against index/update, which delete and rebuild the
            # graph under this lock; an interleaved read mixes generations.
            async with self._ingestor_lock:
                snippet = await self._code_tool.function(qualified_name=qualified_name)
            result: CodeSnippetResultDict | None = snippet.model_dump()
            if result is None:
                return CodeSnippetResultDict(
                    error=te.MCP_TOOL_RETURNED_NONE,
                    found=False,
                    error_message=te.MCP_INVALID_RESPONSE,
                )
            return result
        except Exception as e:
            logger.error(lg.MCP_ERROR_CODE_SNIPPET.format(error=e))
            return CodeSnippetResultDict(
                error=str(e),
                found=False,
                error_message=str(e),
            )

    async def surgical_replace_code(
        self, file_path: str, target_code: str, replacement_code: str
    ) -> str:
        logger.info(lg.MCP_SURGICAL_REPLACE.format(path=file_path))
        try:
            async with self._ingestor_lock:
                result = await self._file_editor_tool.function(
                    file_path=file_path,
                    target_code=target_code,
                    replacement_code=replacement_code,
                )
                if str(result) != cs.MSG_SURGICAL_SUCCESS.format(path=file_path):
                    return str(result)
                delta = await asyncio.to_thread(self._delta_after_write, [file_path])
                return str(result) + delta
        except Exception as e:
            logger.error(lg.MCP_ERROR_REPLACE.format(error=e))
            return te.ERROR_WRAPPER.format(message=e)

    async def read_file(
        self, file_path: str, offset: int | None = None, limit: int | None = None
    ) -> str:
        logger.info(lg.MCP_READ_FILE.format(path=file_path, offset=offset, limit=limit))
        try:
            if offset is not None or limit is not None:
                project_root = Path(self.project_root).resolve()
                try:
                    full_path = (project_root / file_path).resolve()
                    full_path.relative_to(project_root)
                except (ValueError, RuntimeError):
                    return te.ERROR_WRAPPER.format(
                        message=lg.FILE_OUTSIDE_ROOT.format(action="access")
                    )
                start = offset if offset is not None else 0
                return await asyncio.to_thread(
                    _read_file_slice, full_path, start, limit
                )
            else:
                result = await self._file_reader_tool.function(file_path=file_path)
                return str(result)

        except Exception as e:
            logger.error(lg.MCP_ERROR_READ.format(error=e))
            return te.ERROR_WRAPPER.format(message=e)

    async def write_file(self, file_path: str, content: str) -> str:
        logger.info(lg.MCP_WRITE_FILE.format(path=file_path))
        try:
            async with self._ingestor_lock:
                result = await self._file_writer_tool.function(
                    file_path=file_path, content=content
                )
                if not result.success:
                    return te.ERROR_WRAPPER.format(message=result.error_message)
                delta = await asyncio.to_thread(self._delta_after_write, [file_path])
                return cs.MCP_WRITE_SUCCESS.format(path=file_path) + delta
        except Exception as e:
            logger.error(lg.MCP_ERROR_WRITE.format(error=e))
            return te.ERROR_WRAPPER.format(message=e)

    async def list_directory(
        self, directory_path: str = cs.MCP_DEFAULT_DIRECTORY
    ) -> str:
        logger.info(lg.MCP_LIST_DIR.format(path=directory_path))
        try:
            result = self._directory_lister_tool.function(directory_path=directory_path)
            return str(result)
        except Exception as e:
            logger.error(lg.MCP_ERROR_LIST_DIR.format(error=e))
            return te.ERROR_WRAPPER.format(message=e)

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


def create_mcp_tools_registry(
    project_root: str,
    ingestor: MemgraphIngestor,
    cypher_gen: CypherGenerator,
) -> MCPToolsRegistry:
    return MCPToolsRegistry(
        project_root=project_root,
        ingestor=ingestor,
        cypher_gen=cypher_gen,
    )
