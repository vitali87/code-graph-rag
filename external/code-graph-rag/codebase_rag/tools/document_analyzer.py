from __future__ import annotations

import mimetypes
import shutil
import uuid
from pathlib import Path
from typing import NoReturn

from google import genai
from google.genai import types
from google.genai.errors import ClientError
from loguru import logger
from pydantic_ai import Tool

from .. import constants as cs
from .. import exceptions as ex
from .. import logs as ls
from .. import tool_errors as te
from ..config import settings
from . import tool_descriptions as td


class _NotSupportedClient:
    __slots__ = ()

    def __getattr__(self, name: str) -> NoReturn:
        raise NotImplementedError(ex.DOC_UNSUPPORTED_PROVIDER)


class DocumentAnalyzer:
    __slots__ = ("project_root", "client")

    def __init__(self, project_root: str) -> None:
        self.project_root = Path(project_root).resolve()

        orchestrator_config = settings.active_orchestrator_config
        orchestrator_provider = orchestrator_config.provider

        if orchestrator_provider == cs.Provider.GOOGLE:
            if orchestrator_config.provider_type == cs.GoogleProviderType.VERTEX:
                self.client = genai.Client(
                    vertexai=True,
                    credentials=orchestrator_config.service_account_file,
                    project=orchestrator_config.project_id,
                    location=orchestrator_config.region,
                )
            else:
                self.client = genai.Client(api_key=orchestrator_config.api_key)
        else:
            self.client = _NotSupportedClient()

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

    def _extract_response_text(self, response: types.GenerateContentResponse) -> str:
        if hasattr(response, "text") and response.text:
            return str(response.text)

        if hasattr(response, "candidates") and response.candidates:
            for candidate in response.candidates:
                if hasattr(candidate, "content") and candidate.content:
                    parts = candidate.content.parts
                    if parts and hasattr(parts[0], "text"):
                        return str(parts[0].text)
            return cs.MSG_DOC_NO_CANDIDATES

        logger.warning(ls.DOC_NO_TEXT.format(response=response))
        return cs.MSG_DOC_NO_CONTENT

    def _handle_analyze_error(self, error: Exception, file_path: str) -> str:
        if isinstance(error, ValueError):
            if "does not start with" in str(error):
                err_msg = te.DOC_ACCESS_OUTSIDE_ROOT.format(path=file_path)
                logger.error(err_msg)
                return err_msg
            logger.error(ls.DOC_ANALYZER_API_ERR.format(error=error))
            return te.DOC_API_VALIDATION.format(error=error)

        if isinstance(error, ClientError):
            logger.error(ls.DOC_API_ERROR.format(path=file_path, error=error))
            if "Unable to process input image" in str(error):
                return te.DOC_IMAGE_PROCESS
            return te.DOC_API_ERROR.format(error=error)

        logger.exception(ls.DOC_FAILED.format(path=file_path, error=error))
        return te.DOC_ANALYSIS_FAILED.format(error=error)

    def analyze(self, file_path: str, question: str) -> str:
        logger.info(ls.TOOL_DOC_ANALYZE.format(path=file_path, question=question))
        if isinstance(self.client, _NotSupportedClient):
            return te.DOCUMENT_UNSUPPORTED

        try:
            resolved = self._resolve_file_path(file_path)
            if isinstance(resolved, str):
                return resolved
            full_path = resolved

            if not full_path.is_file():
                return te.DOC_FILE_NOT_FOUND.format(path=file_path)

            mime_type, _ = mimetypes.guess_type(full_path)
            if not mime_type:
                mime_type = cs.MIME_TYPE_DEFAULT

            file_bytes = full_path.read_bytes()

            prompt_parts = [
                types.Part.from_bytes(data=file_bytes, mime_type=mime_type),
                cs.DOC_PROMPT_PREFIX.format(question=question),
            ]

            orchestrator_config = settings.active_orchestrator_config
            response = self.client.models.generate_content(
                model=orchestrator_config.model_id, contents=prompt_parts
            )

            logger.success(ls.DOC_SUCCESS.format(path=file_path))
            return self._extract_response_text(response)

        except Exception as e:
            return self._handle_analyze_error(e, file_path)


def create_document_analyzer_tool(analyzer: DocumentAnalyzer) -> Tool:
    def analyze_document(file_path: str, question: str) -> str:
        try:
            result = analyzer.analyze(file_path, question)
            preview = result[:100] if result else "None"
            logger.debug(ls.DOC_RESULT, type=type(result).__name__, preview=preview)
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
