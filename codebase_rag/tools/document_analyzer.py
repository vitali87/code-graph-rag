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

from ..config import settings
from ..constants import (
    DOC_PROMPT_PREFIX,
    ERR_DOC_ACCESS_OUTSIDE_ROOT,
    ERR_DOC_ANALYSIS_FAILED,
    ERR_DOC_API_VALIDATION,
    ERR_DOC_DURING_ANALYSIS,
    ERR_DOC_FILE_NOT_FOUND,
    ERR_DOC_IMAGE_PROCESS,
    ERR_DOC_SECURITY_RISK,
    ERR_DOC_UNSUPPORTED_PROVIDER,
    ERR_DOCUMENT_UNSUPPORTED,
    LOG_DOC_ANALYZER_API_ERR,
    LOG_DOC_ANALYZER_INIT,
    LOG_DOC_API_ERROR,
    LOG_DOC_COPIED,
    LOG_DOC_EXCEPTION,
    LOG_DOC_FAILED,
    LOG_DOC_NO_TEXT,
    LOG_DOC_RESULT,
    LOG_DOC_SUCCESS,
    LOG_TOOL_DOC_ANALYZE,
    MIME_TYPE_DEFAULT,
    MSG_DOC_NO_CANDIDATES,
    MSG_DOC_NO_CONTENT,
    TMP_DIR,
    GoogleProviderType,
    Provider,
)
from . import tool_descriptions as td


class _NotSupportedClient:
    def __getattr__(self, name: str) -> NoReturn:
        raise NotImplementedError(ERR_DOC_UNSUPPORTED_PROVIDER)


class DocumentAnalyzer:
    def __init__(self, project_root: str) -> None:
        self.project_root = Path(project_root).resolve()

        orchestrator_config = settings.active_orchestrator_config
        orchestrator_provider = orchestrator_config.provider

        if orchestrator_provider == Provider.GOOGLE:
            if orchestrator_config.provider_type == GoogleProviderType.VERTEX:
                self.client = genai.Client(
                    project=orchestrator_config.project_id,
                    location=orchestrator_config.region,
                )
            else:
                self.client = genai.Client(api_key=orchestrator_config.api_key)
        else:
            self.client = _NotSupportedClient()

        logger.info(LOG_DOC_ANALYZER_INIT.format(root=self.project_root))

    def analyze(self, file_path: str, question: str) -> str:
        logger.info(LOG_TOOL_DOC_ANALYZE.format(path=file_path, question=question))
        if isinstance(self.client, _NotSupportedClient):
            return f"Error: {ERR_DOCUMENT_UNSUPPORTED}"

        try:
            if Path(file_path).is_absolute():
                source_path = Path(file_path)
                if not source_path.is_file():
                    return f"Error: {ERR_DOC_FILE_NOT_FOUND.format(path=file_path)}"

                tmp_dir = self.project_root / TMP_DIR
                tmp_dir.mkdir(exist_ok=True)

                tmp_file = tmp_dir / f"{uuid.uuid4()}-{source_path.name}"
                shutil.copy2(source_path, tmp_file)
                full_path = tmp_file
                logger.info(LOG_DOC_COPIED.format(path=full_path))
            else:
                full_path = (self.project_root / file_path).resolve()
                try:
                    full_path.relative_to(self.project_root.resolve())
                except ValueError:
                    return ERR_DOC_SECURITY_RISK.format(path=file_path)

                if not str(full_path).startswith(str(self.project_root.resolve())):
                    return ERR_DOC_SECURITY_RISK.format(path=file_path)

            if not full_path.is_file():
                return f"Error: {ERR_DOC_FILE_NOT_FOUND.format(path=file_path)}"

            mime_type, _ = mimetypes.guess_type(full_path)
            if not mime_type:
                mime_type = MIME_TYPE_DEFAULT

            file_bytes = full_path.read_bytes()

            prompt_parts = [
                types.Part.from_bytes(data=file_bytes, mime_type=mime_type),
                DOC_PROMPT_PREFIX.format(question=question),
            ]

            orchestrator_config = settings.active_orchestrator_config
            response = self.client.models.generate_content(
                model=orchestrator_config.model_id, contents=prompt_parts
            )

            logger.success(LOG_DOC_SUCCESS.format(path=file_path))

            if hasattr(response, "text") and response.text:
                return str(response.text)
            elif hasattr(response, "candidates") and response.candidates:
                for candidate in response.candidates:
                    if hasattr(candidate, "content") and candidate.content:
                        parts = candidate.content.parts
                        if parts and hasattr(parts[0], "text"):
                            return str(parts[0].text)
                return MSG_DOC_NO_CANDIDATES
            else:
                logger.warning(LOG_DOC_NO_TEXT.format(response=response))
                return MSG_DOC_NO_CONTENT

        except ValueError as e:
            if "does not start with" in str(e):
                err_msg = ERR_DOC_ACCESS_OUTSIDE_ROOT.format(path=file_path)
                logger.error(err_msg)
                return f"Error: {err_msg}"
            else:
                logger.error(LOG_DOC_ANALYZER_API_ERR.format(error=e))
                return f"Error: {ERR_DOC_API_VALIDATION.format(error=e)}"
        except ClientError as e:
            logger.error(LOG_DOC_API_ERROR.format(path=file_path, error=e))
            if "Unable to process input image" in str(e):
                return f"Error: {ERR_DOC_IMAGE_PROCESS}"
            return f"API error: {e}"
        except Exception as e:
            logger.error(LOG_DOC_FAILED.format(path=file_path, error=e), exc_info=True)
            return ERR_DOC_ANALYSIS_FAILED.format(error=e)


def create_document_analyzer_tool(analyzer: DocumentAnalyzer) -> Tool:
    def analyze_document(file_path: str, question: str) -> str:
        try:
            result = analyzer.analyze(file_path, question)
            preview = result[:100] if result else "None"
            logger.debug(
                LOG_DOC_RESULT.format(type=type(result).__name__, preview=preview)
            )
            return result
        except Exception as e:
            logger.error(LOG_DOC_EXCEPTION.format(error=e), exc_info=True)
            if str(e).startswith("Error:") or str(e).startswith("API error:"):
                return str(e)
            return ERR_DOC_DURING_ANALYSIS.format(error=e)

    return Tool(
        function=analyze_document,
        name="analyze_document",
        description=td.ANALYZE_DOCUMENT,
    )
