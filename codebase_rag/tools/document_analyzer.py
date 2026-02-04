from __future__ import annotations

import mimetypes
import shutil
import uuid
from pathlib import Path

from loguru import logger
from pydantic_ai import Agent, BinaryContent, Tool

from .. import constants as cs
from .. import logs as ls
from .. import tool_errors as te
from ..config import settings
from ..services.llm import _create_provider_model
from . import tool_descriptions as td


class DocumentAnalyzer:
    def __init__(self, project_root: str) -> None:
        self.project_root = Path(project_root).resolve()
        logger.info(ls.DOC_ANALYZER_INIT.format(root=self.project_root))

    def _resolve_absolute_path(self, file_path: str) -> Path | str:
        source_path = Path(file_path)
        if not source_path.is_file():
            return te.DOC_FILE_NOT_FOUND.format(path=file_path)

        tmp_dir = self.project_root / cs.TMP_DIR
        tmp_dir.mkdir(exist_ok=True)

        tmp_file = tmp_dir / f"{uuid.uuid4()}-{source_path.name}"
        shutil.copy2(source_path, tmp_file)
        logger.info(ls.DOC_COPIED.format(path=tmp_file))
        return tmp_file

    def _resolve_relative_path(self, file_path: str) -> Path | str:
        full_path = (self.project_root / file_path).resolve()
        try:
            full_path.relative_to(self.project_root.resolve())
        except ValueError:
            return te.DOC_SECURITY_RISK.format(path=file_path)

        if not str(full_path).startswith(str(self.project_root.resolve())):
            return te.DOC_SECURITY_RISK.format(path=file_path)

        return full_path

    def _resolve_file_path(self, file_path: str) -> Path | str:
        if Path(file_path).is_absolute():
            return self._resolve_absolute_path(file_path)
        return self._resolve_relative_path(file_path)

    def analyze(self, file_path: str, question: str) -> str:
        logger.info(ls.TOOL_DOC_ANALYZE.format(path=file_path, question=question))

        try:
            resolved = self._resolve_file_path(file_path)
            if isinstance(resolved, str):
                return resolved
            full_path = resolved

            if not full_path.is_file():
                return te.DOC_FILE_NOT_FOUND.format(path=file_path)

            orchestrator_config = settings.active_orchestrator_config
            model = _create_provider_model(orchestrator_config)

            mime_type, _ = mimetypes.guess_type(full_path)
            if not mime_type:
                mime_type = cs.MIME_TYPE_DEFAULT

            agent = Agent(model=model)

            content = [
                BinaryContent(data=full_path.read_bytes(), media_type=mime_type),
                cs.DOC_PROMPT_PREFIX.format(question=question),
            ]

            result = agent.run_sync(content)

            logger.success(ls.DOC_SUCCESS.format(path=file_path))
            return str(result.data)  # type: ignore[attr-defined]

        except Exception as e:
            logger.exception(ls.DOC_FAILED.format(path=file_path, error=e))
            return te.DOC_ANALYSIS_FAILED.format(error=e)


def create_document_analyzer_tool(analyzer: DocumentAnalyzer) -> Tool:
    def analyze_document(file_path: str, question: str) -> str:
        try:
            result = analyzer.analyze(file_path, question)
            preview = result[:100] if result else "None"
            logger.debug(
                ls.DOC_RESULT.format(type=type(result).__name__, preview=preview)
            )
            return result
        except Exception as e:
            logger.exception(ls.DOC_EXCEPTION.format(error=e))
            if str(e).startswith("Error:") or str(e).startswith("API error:"):
                return str(e)
            return te.DOC_DURING_ANALYSIS.format(error=e)

    return Tool(
        function=analyze_document,
        name=td.AgenticToolName.ANALYZE_DOCUMENT,
        description=td.ANALYZE_DOCUMENT,
    )
