from __future__ import annotations

import re
from typing import TYPE_CHECKING

from loguru import logger
from pydantic_ai import Agent, DeferredToolRequests, Tool

from .. import constants as cs
from .. import exceptions as ex
from .. import logs as ls
from ..config import ModelConfig, settings
from ..prompts import (
    CYPHER_SYSTEM_PROMPT,
    LOCAL_CYPHER_SYSTEM_PROMPT,
    build_rag_orchestrator_prompt,
)
from ..providers.base import get_provider_from_config

if TYPE_CHECKING:
    from pydantic_ai.models import Model


def _create_provider_model(config: ModelConfig) -> Model:
    provider = get_provider_from_config(config)
    return provider.create_model(config.model_id)


def _clean_cypher_response(response_text: str) -> str:
    query = response_text.strip().replace(cs.CYPHER_BACKTICK, "")
    if query.startswith(cs.CYPHER_PREFIX):
        query = query[len(cs.CYPHER_PREFIX) :].strip()
    if not query.endswith(cs.CYPHER_SEMICOLON):
        query += cs.CYPHER_SEMICOLON
    return query


_COMMENT_OR_WS = r"(?:\s|/\*.*?\*/)+"


def _build_keyword_pattern(keyword: str) -> re.Pattern[str]:
    parts = keyword.split()
    if len(parts) == 1:
        return re.compile(rf"\b{re.escape(parts[0])}\b")
    joined = _COMMENT_OR_WS.join(re.escape(p) for p in parts)
    return re.compile(rf"\b{joined}\b", re.DOTALL)


_CYPHER_DANGEROUS_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (kw, _build_keyword_pattern(kw)) for kw in cs.CYPHER_DANGEROUS_KEYWORDS
]


def _validate_cypher_read_only(query: str) -> None:
    upper_query = query.upper()
    for keyword, pattern in _CYPHER_DANGEROUS_PATTERNS:
        if pattern.search(upper_query):
            raise ex.LLMGenerationError(
                ex.LLM_DANGEROUS_QUERY.format(keyword=keyword, query=query)
            )


class CypherGenerator:
    __slots__ = ("agent",)

    def __init__(self) -> None:
        try:
            config = settings.active_cypher_config
            llm = _create_provider_model(config)

            system_prompt = (
                LOCAL_CYPHER_SYSTEM_PROMPT
                if config.provider == cs.Provider.OLLAMA
                else CYPHER_SYSTEM_PROMPT
            )

            self.agent = Agent(
                model=llm,
                system_prompt=system_prompt,
                output_type=str,
                retries=settings.AGENT_RETRIES,
            )
        except Exception as e:
            raise ex.LLMGenerationError(ex.LLM_INIT_CYPHER.format(error=e)) from e

    async def generate(self, natural_language_query: str) -> str:
        logger.info(ls.CYPHER_GENERATING.format(query=natural_language_query))
        try:
            result = await self.agent.run(natural_language_query)
            if (
                not isinstance(result.output, str)
                or cs.CYPHER_MATCH_KEYWORD not in result.output.upper()
            ):
                raise ex.LLMGenerationError(
                    ex.LLM_INVALID_QUERY.format(output=result.output)
                )

            query = _clean_cypher_response(result.output)
            _validate_cypher_read_only(query)
            logger.info(ls.CYPHER_GENERATED.format(query=query))
            return query
        except Exception as e:
            logger.error(ls.CYPHER_ERROR.format(error=e))
            raise ex.LLMGenerationError(ex.LLM_GENERATION_FAILED.format(error=e)) from e


def create_rag_orchestrator(tools: list[Tool]) -> Agent:
    try:
        config = settings.active_orchestrator_config
        llm = _create_provider_model(config)

        return Agent(
            model=llm,
            system_prompt=build_rag_orchestrator_prompt(tools),
            tools=tools,
            retries=settings.AGENT_RETRIES,
            output_retries=settings.ORCHESTRATOR_OUTPUT_RETRIES,
            output_type=[str, DeferredToolRequests],
        )
    except Exception as e:
        raise ex.LLMGenerationError(ex.LLM_INIT_ORCHESTRATOR.format(error=e)) from e
