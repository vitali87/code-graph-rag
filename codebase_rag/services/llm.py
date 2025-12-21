from loguru import logger
from pydantic_ai import Agent, DeferredToolRequests, Tool

from ..config import settings
from ..prompts import (
    CYPHER_SYSTEM_PROMPT,
    LOCAL_CYPHER_SYSTEM_PROMPT,
    build_rag_orchestrator_prompt,
)
from ..providers.base import get_provider


class LLMGenerationError(Exception):
    pass


def _clean_cypher_response(response_text: str) -> str:
    query = response_text.strip().replace("`", "")
    if query.startswith("cypher"):
        query = query[6:].strip()
    if not query.endswith(";"):
        query += ";"
    return query


class CypherGenerator:
    def __init__(self) -> None:
        try:
            config = settings.active_cypher_config

            provider = get_provider(
                config.provider,
                api_key=config.api_key,
                endpoint=config.endpoint,
                project_id=config.project_id,
                region=config.region,
                provider_type=config.provider_type,
                thinking_budget=config.thinking_budget,
            )

            llm = provider.create_model(config.model_id)

            system_prompt = (
                LOCAL_CYPHER_SYSTEM_PROMPT
                if config.provider == "ollama"
                else CYPHER_SYSTEM_PROMPT
            )

            self.agent = Agent(
                model=llm,
                system_prompt=system_prompt,
                output_type=str,
                retries=settings.AGENT_RETRIES,
            )
        except Exception as e:
            raise LLMGenerationError(
                f"Failed to initialize CypherGenerator: {e}"
            ) from e

    async def generate(self, natural_language_query: str) -> str:
        logger.info(
            f"  [CypherGenerator] Generating query for: '{natural_language_query}'"
        )
        try:
            result = await self.agent.run(natural_language_query)
            if (
                not isinstance(result.output, str)
                or "MATCH" not in result.output.upper()
            ):
                raise LLMGenerationError(
                    f"LLM did not generate a valid query. Output: {result.output}"
                )

            query = _clean_cypher_response(result.output)
            logger.info(f"  [CypherGenerator] Generated Cypher: {query}")
            return query
        except Exception as e:
            logger.error(f"  [CypherGenerator] Error: {e}")
            raise LLMGenerationError(f"Cypher generation failed: {e}") from e


def create_rag_orchestrator(tools: list[Tool]) -> Agent:
    try:
        config = settings.active_orchestrator_config

        provider = get_provider(
            config.provider,
            api_key=config.api_key,
            endpoint=config.endpoint,
            project_id=config.project_id,
            region=config.region,
            provider_type=config.provider_type,
            thinking_budget=config.thinking_budget,
        )

        llm = provider.create_model(config.model_id)

        return Agent(
            model=llm,
            system_prompt=build_rag_orchestrator_prompt(tools),
            tools=tools,
            retries=settings.AGENT_RETRIES,
            output_retries=100,
            output_type=[str, DeferredToolRequests],
        )
    except Exception as e:
        raise LLMGenerationError(f"Failed to initialize RAG Orchestrator: {e}") from e
