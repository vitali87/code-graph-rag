from __future__ import annotations

from pathlib import Path

from loguru import logger
from pydantic_ai import Tool

from .. import logs as ls
from .. import tool_errors as te
from ..constants import ENCODING_UTF8
from ..cypher_queries import CYPHER_FIND_BY_QUALIFIED_NAME
from ..schemas import CodeSnippet
from ..services import QueryProtocol
from . import tool_descriptions as td


class CodeRetriever:
    def __init__(self, project_root: str, ingestor: QueryProtocol):
        self.project_root = Path(project_root).resolve()
        self.ingestor = ingestor
        logger.info(ls.CODE_RETRIEVER_INIT.format(root=self.project_root))

    async def find_code_snippet(self, qualified_name: str) -> CodeSnippet:
        logger.info(ls.CODE_RETRIEVER_SEARCH.format(name=qualified_name))

        params = {"qn": qualified_name}
        try:
            results = self.ingestor.fetch_all(CYPHER_FIND_BY_QUALIFIED_NAME, params)

            if not results:
                return CodeSnippet(
                    qualified_name=qualified_name,
                    source_code="",
                    file_path="",
                    line_start=0,
                    line_end=0,
                    found=False,
                    error_message=te.CODE_ENTITY_NOT_FOUND,
                )

            res = results[0]
            file_path_str = res.get("path")
            start_line = res.get("start")
            end_line = res.get("end")

            if not all([file_path_str, start_line, end_line]):
                return CodeSnippet(
                    qualified_name=qualified_name,
                    source_code="",
                    file_path=file_path_str or "",
                    line_start=0,
                    line_end=0,
                    found=False,
                    error_message=te.CODE_MISSING_LOCATION,
                )

            full_path = self.project_root / file_path_str
            with full_path.open("r", encoding=ENCODING_UTF8) as f:
                all_lines = f.readlines()

            snippet_lines = all_lines[start_line - 1 : end_line]
            source_code = "".join(snippet_lines)

            return CodeSnippet(
                qualified_name=qualified_name,
                source_code=source_code,
                file_path=file_path_str,
                line_start=start_line,
                line_end=end_line,
                docstring=res.get("docstring"),
            )
        except Exception as e:
            logger.exception(ls.CODE_RETRIEVER_ERROR.format(error=e))
            return CodeSnippet(
                qualified_name=qualified_name,
                source_code="",
                file_path="",
                line_start=0,
                line_end=0,
                found=False,
                error_message=str(e),
            )


def create_code_retrieval_tool(code_retriever: CodeRetriever) -> Tool:
    async def get_code_snippet(qualified_name: str) -> CodeSnippet:
        logger.info(ls.CODE_TOOL_RETRIEVE.format(name=qualified_name))
        return await code_retriever.find_code_snippet(qualified_name)

    return Tool(
        function=get_code_snippet,
        name=td.AgenticToolName.GET_CODE_SNIPPET,
        description=td.CODE_RETRIEVAL,
    )
