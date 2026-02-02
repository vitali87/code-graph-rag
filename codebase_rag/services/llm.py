from __future__ import annotations

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
    """Clean LLM response to extract pure Cypher query.

    Handles markdown formatting that models sometimes output:
    - Triple backticks (```cypher ... ```)
    - Bold text (**Cypher Query:**)
    - Headers and other markdown
    """
    import re

    query = response_text.strip()

    # Extract content from code blocks if present (```cypher ... ``` or ``` ... ```)
    code_block_match = re.search(r"```(?:cypher)?\s*(.*?)```", query, re.DOTALL | re.IGNORECASE)
    if code_block_match:
        query = code_block_match.group(1).strip()
    else:
        # Remove markdown bold/headers (e.g., **Cypher Query:**)
        query = re.sub(r"\*\*[^*]+\*\*:?\s*", "", query)
        # Remove single backticks
        query = query.replace(cs.CYPHER_BACKTICK, "")
        # Remove "cypher" prefix if present
        if query.lower().startswith(cs.CYPHER_PREFIX):
            query = query[len(cs.CYPHER_PREFIX):].strip()

    if not query.endswith(cs.CYPHER_SEMICOLON):
        query += cs.CYPHER_SEMICOLON
    return query


class CypherGenerator:
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
