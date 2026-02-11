import itertools
from pathlib import Path

from loguru import logger

from codebase_rag import constants as cs
from codebase_rag import logs as lg
from codebase_rag import tool_errors as te
from codebase_rag.config import settings
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.models import ToolMetadata
from codebase_rag.parser_loader import load_parsers
from codebase_rag.services.graph_service import MemgraphIngestor
from codebase_rag.services.llm import CypherGenerator
from codebase_rag.tools import tool_descriptions as td
from codebase_rag.tools.code_retrieval import CodeRetriever, create_code_retrieval_tool
from codebase_rag.tools.codebase_query import create_query_tool
from codebase_rag.tools.directory_lister import (
    DirectoryLister,
    create_directory_lister_tool,
)
from codebase_rag.tools.file_editor import FileEditor, create_file_editor_tool
from codebase_rag.tools.file_reader import FileReader, create_file_reader_tool
from codebase_rag.tools.file_writer import FileWriter, create_file_writer_tool
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
)

from ..utils.path_utils import validate_allowed_path


class MCPToolsRegistry:
    def __init__(
        self,
        project_root: str,
        ingestor: MemgraphIngestor,
        cypher_gen: CypherGenerator,
        mode: str = "edit",
    ) -> None:
        self.project_root = project_root
        self.ingestor = ingestor
        self.cypher_gen = cypher_gen
        self.mode = mode

        self.parsers, self.queries = load_parsers()

        self.code_retriever = CodeRetriever(
            project_root, ingestor, allowed_roots=settings.allowed_project_roots_set
        )
        self.file_editor = FileEditor(project_root=project_root, mode=mode)
        self.file_reader = FileReader(
            project_root=project_root,
            mode=mode,
            allowed_roots=settings.allowed_project_roots_set,
        )
        self.file_writer = FileWriter(project_root=project_root, mode=mode)

        logger.info(lg.MCP_TOOLS_REGISTRY_MODE.format(mode=mode))

        self.directory_lister = DirectoryLister(
            project_root=project_root,
            allowed_roots=settings.allowed_project_roots_set,
        )

        self._query_tool = create_query_tool(
            ingestor=ingestor, cypher_gen=cypher_gen, console=None
        )
        self._code_tool = create_code_retrieval_tool(code_retriever=self.code_retriever)
        self._file_editor_tool = create_file_editor_tool(file_editor=self.file_editor)
        self._file_reader_tool = create_file_reader_tool(file_reader=self.file_reader)
        self._file_writer_tool = create_file_writer_tool(file_writer=self.file_writer)
        self._directory_lister_tool = create_directory_lister_tool(
            directory_lister=self.directory_lister
        )

        self._tools: dict[str, ToolMetadata] = self._build_tools()

    def _build_tools(self) -> dict[str, ToolMetadata]:
        tools: dict[str, ToolMetadata] = {}

        tools.update(
            {
                cs.MCPToolName.QUERY_CODE_GRAPH: ToolMetadata(
                    name=cs.MCPToolName.QUERY_CODE_GRAPH,
                    description=td.MCP_TOOLS[cs.MCPToolName.QUERY_CODE_GRAPH],
                    input_schema=MCPInputSchema(
                        type=cs.MCPSchemaType.OBJECT,
                        properties={
                            cs.MCPParamName.NATURAL_LANGUAGE_QUERY: MCPInputSchemaProperty(
                                type=cs.MCPSchemaType.STRING,
                                description=td.MCP_PARAM_NATURAL_LANGUAGE_QUERY,
                            )
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
            }
        )

        if self.mode == "edit":
            tools.update(
                {
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
                }
            )

        return tools

    async def list_projects(self) -> ListProjectsResult:
        logger.info(lg.MCP_LISTING_PROJECTS)
        try:
            projects = self.ingestor.list_projects()
            return ListProjectsSuccessResult(projects=projects, count=len(projects))
        except Exception as e:
            logger.error(lg.MCP_ERROR_LIST_PROJECTS.format(error=e))
            return ListProjectsErrorResult(error=str(e), projects=[], count=0)

    async def delete_project(self, project_name: str) -> DeleteProjectResult:
        logger.info(lg.MCP_DELETING_PROJECT.format(project_name=project_name))
        try:
            projects = self.ingestor.list_projects()
            if project_name not in projects:
                return DeleteProjectErrorResult(
                    success=False,
                    error=te.MCP_PROJECT_NOT_FOUND.format(
                        project_name=project_name, projects=projects
                    ),
                )
            self.ingestor.delete_project(project_name)
            return DeleteProjectSuccessResult(
                success=True,
                project=project_name,
                message=cs.MCP_PROJECT_DELETED.format(project_name=project_name),
            )
        except Exception as e:
            logger.error(lg.MCP_ERROR_DELETE_PROJECT.format(error=e))
            return DeleteProjectErrorResult(success=False, error=str(e))

    async def wipe_database(self, confirm: bool) -> str:
        if not confirm:
            return cs.MCP_WIPE_CANCELLED
        logger.warning(lg.MCP_WIPING_DATABASE)
        try:
            self.ingestor.clean_database()
            return cs.MCP_WIPE_SUCCESS
        except Exception as e:
            logger.error(lg.MCP_ERROR_WIPE.format(error=e))
            return cs.MCP_WIPE_ERROR.format(error=e)

    async def index_repository(self) -> str:
        logger.info(lg.MCP_INDEXING_REPO.format(path=self.project_root))
        project_name = Path(self.project_root).resolve().name
        try:
            logger.info(lg.MCP_CLEARING_PROJECT.format(project_name=project_name))
            self.ingestor.delete_project(project_name)

            updater = GraphUpdater(
                ingestor=self.ingestor,
                repo_path=Path(self.project_root),
                parsers=self.parsers,
                queries=self.queries,
            )
            updater.run()

            return cs.MCP_INDEX_SUCCESS_PROJECT.format(
                path=self.project_root, project_name=project_name
            )
        except Exception as e:
            logger.error(lg.MCP_ERROR_INDEXING.format(error=e))
            return cs.MCP_INDEX_ERROR.format(error=e)

    async def query_code_graph(self, natural_language_query: str) -> QueryResultDict:
        logger.info(lg.MCP_QUERY_CODE_GRAPH.format(query=natural_language_query))
        try:
            graph_data = await self._query_tool.function(natural_language_query)
            result_dict: QueryResultDict = graph_data.model_dump()
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
            result = await self._file_editor_tool.function(
                file_path=file_path,
                target_code=target_code,
                replacement_code=replacement_code,
            )
            return str(result)
        except Exception as e:
            logger.error(lg.MCP_ERROR_REPLACE.format(error=e))
            return te.ERROR_WRAPPER.format(message=e)

    async def read_file(
        self, file_path: str, offset: int | None = None, limit: int | None = None
    ) -> str:
        logger.info(lg.MCP_READ_FILE.format(path=file_path, offset=offset, limit=limit))
        try:
            if offset is not None or limit is not None:
                project_root_path = Path(self.project_root).resolve()
                safe_path = validate_allowed_path(
                    file_path, project_root_path, self.file_reader.allowed_roots
                )

                start = offset if offset is not None else 0

                with safe_path.open("r", encoding=cs.ENCODING_UTF8) as f:
                    skipped_count = sum(1 for _ in itertools.islice(f, start))

                    if limit is not None:
                        sliced_lines = [line for _, line in zip(range(limit), f)]
                    else:
                        sliced_lines = list(f)

                    paginated_content = "".join(sliced_lines)

                    remaining_lines_count = sum(1 for _ in f)
                    total_lines = (
                        skipped_count + len(sliced_lines) + remaining_lines_count
                    )

                    header = cs.MCP_PAGINATION_HEADER.format(
                        start=start + 1,
                        end=start + len(sliced_lines),
                        total=total_lines,
                    )
                    return header + paginated_content
            else:
                result = await self._file_reader_tool.function(file_path=file_path)
                return str(result)

        except Exception as e:
            logger.error(lg.MCP_ERROR_READ.format(error=e))
            return te.ERROR_WRAPPER.format(message=e)

    async def write_file(self, file_path: str, content: str) -> str:
        logger.info(lg.MCP_WRITE_FILE.format(path=file_path))
        try:
            result = await self._file_writer_tool.function(
                file_path=file_path, content=content
            )
            if result.success:
                return cs.MCP_WRITE_SUCCESS.format(path=file_path)
            return te.ERROR_WRAPPER.format(message=result.error_message)
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
    mode: str = "edit",
) -> MCPToolsRegistry:
    return MCPToolsRegistry(
        project_root=project_root,
        ingestor=ingestor,
        cypher_gen=cypher_gen,
        mode=mode,
    )
