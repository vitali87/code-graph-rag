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
from ..utils.path_utils import validate_allowed_path
from . import tool_descriptions as td


class CodeRetriever:
    def __init__(
        self,
        project_root: str,
        ingestor: QueryProtocol,
        allowed_roots: frozenset[Path] | None = None,
    ):
        self.project_root = Path(project_root).resolve()
        self.ingestor = ingestor
        self.allowed_roots = (
            frozenset(root.resolve() for root in allowed_roots)
            if allowed_roots
            else None
        )
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
            project_name = res.get("project_name")
            start_line = res.get("start")
            end_line = res.get("end")

            absolute_path_str = res.get("absolute_path")
            relative_path_str = res.get("relative_path")

            if absolute_path_str:
                file_path_obj = Path(absolute_path_str)
            elif relative_path_str:
                file_path_obj = validate_allowed_path(
                    relative_path_str, self.project_root, self.allowed_roots
                )
                logger.warning(ls.NO_ABSOLUTE_PATH_FALLBACK.format(qn=qualified_name))
            else:
                file_path_obj = None

            if not (file_path_obj and start_line and end_line):
                return CodeSnippet(
                    qualified_name=qualified_name,
                    source_code="",
                    file_path=str(file_path_obj) if file_path_obj else "",
                    project_name=project_name,
                    line_start=0,
                    line_end=0,
                    found=False,
                    error_message=te.CODE_MISSING_LOCATION,
                )

            assert file_path_obj is not None

            with file_path_obj.open("r", encoding=ENCODING_UTF8) as f:
                all_lines = f.readlines()

            snippet_lines = all_lines[start_line - 1 : end_line]
            source_code = "".join(snippet_lines)

            return CodeSnippet(
                qualified_name=qualified_name,
                source_code=source_code,
                file_path=str(file_path_obj),
                project_name=project_name,
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
