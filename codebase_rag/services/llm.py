from typing import cast

from loguru import logger
from pydantic_ai import Agent, Tool
from pydantic_ai.models.gemini import GeminiModel, GeminiModelSettings
from pydantic_ai.models.openai import OpenAIModel
from pydantic_ai.providers.google_gla import GoogleGLAProvider
from pydantic_ai.providers.google_vertex import GoogleVertexProvider, VertexAiRegion
from pydantic_ai.providers.openai import OpenAIProvider

from ..config import settings
from ..prompts import (
    GEMINI_LITE_CYPHER_SYSTEM_PROMPT,
    LOCAL_CYPHER_SYSTEM_PROMPT,
    RAG_ORCHESTRATOR_SYSTEM_PROMPT,
)


class LLMGenerationError(Exception):
    """Custom exception for LLM generation failures."""

    pass


def _clean_cypher_response(response_text: str) -> str:
    """Utility to clean up common LLM formatting artifacts from a Cypher query."""
    query = response_text.strip().replace("`", "")
    if query.startswith("cypher"):
        query = query[6:].strip()
    if not query.endswith(";"):
        query += ";"
    return query


class CypherGenerator:
    """Generates Cypher queries from natural language."""

    def __init__(self) -> None:
        try:
            # Validate settings before initializing LLM services
            settings.validate_for_usage()
            model_settings = None
            if settings.LLM_PROVIDER == "gemini":
                if settings.GEMINI_PROVIDER == "vertex":
                    provider = GoogleVertexProvider(
                        project_id=settings.GCP_PROJECT_ID,
                        region=cast(VertexAiRegion, settings.GCP_REGION),
                        service_account_file=settings.GCP_SERVICE_ACCOUNT_FILE,
                    )
                else:
                    provider = GoogleGLAProvider(api_key=settings.GEMINI_API_KEY)  # type: ignore

                if settings.GEMINI_THINKING_BUDGET is not None:
                    model_settings = GeminiModelSettings(
                        gemini_thinking_config={
                            "thinking_budget": int(settings.GEMINI_THINKING_BUDGET)
                        }
                    )

                llm = GeminiModel(
                    settings.MODEL_CYPHER_ID,
                    provider=provider,
                )
                system_prompt = GEMINI_LITE_CYPHER_SYSTEM_PROMPT
            else:  # local provider
                llm = OpenAIModel(  # type: ignore
                    settings.LOCAL_CYPHER_MODEL_ID,
                    provider=OpenAIProvider(
                        api_key=settings.LOCAL_MODEL_API_KEY,
                        base_url=str(settings.LOCAL_MODEL_ENDPOINT),
                    ),
                )
                system_prompt = LOCAL_CYPHER_SYSTEM_PROMPT
            self.agent = Agent(
                model=llm,
                system_prompt=system_prompt,
                output_type=str,
                model_settings=model_settings,
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
    """Factory function to create the main RAG orchestrator agent."""
    try:
        # Validate settings before initializing LLM services
        settings.validate_for_usage()
        model_settings = None
        if settings.LLM_PROVIDER == "gemini":
            if settings.GEMINI_PROVIDER == "vertex":
                provider = GoogleVertexProvider(
                    project_id=settings.GCP_PROJECT_ID,
                    region=cast(VertexAiRegion, settings.GCP_REGION),
                    service_account_file=settings.GCP_SERVICE_ACCOUNT_FILE,
                )
            else:
                provider = GoogleGLAProvider(api_key=settings.GEMINI_API_KEY)  # type: ignore

            if settings.GEMINI_THINKING_BUDGET is not None:
                model_settings = GeminiModelSettings(
                    gemini_thinking_config={
                        "thinking_budget": int(settings.GEMINI_THINKING_BUDGET)
                    }
                )

            llm = GeminiModel(
                settings.GEMINI_MODEL_ID,
                provider=provider,
            )
        else:  # local provider
            llm = OpenAIModel(  # type: ignore
                settings.LOCAL_ORCHESTRATOR_MODEL_ID,
                provider=OpenAIProvider(
                    api_key=settings.LOCAL_MODEL_API_KEY,
                    base_url=str(settings.LOCAL_MODEL_ENDPOINT),
                ),
            )

        return Agent(
            model=llm,
            system_prompt=RAG_ORCHESTRATOR_SYSTEM_PROMPT,
            tools=tools,
            model_settings=model_settings,
        )  # type: ignore
    except Exception as e:
        raise LLMGenerationError(f"Failed to initialize RAG Orchestrator: {e}") from e
