from loguru import logger
from pydantic_ai import Agent, DeferredToolRequests, Tool

from ..config import settings
from ..deps import RAGDeps
from ..exceptions import LLMGenerationError
from ..prompts import (
    CYPHER_SYSTEM_PROMPT,
    LOCAL_CYPHER_SYSTEM_PROMPT,
    RAG_ORCHESTRATOR_SYSTEM_PROMPT,
)
from ..providers.base import get_provider
from ..tools.code_retrieval import get_code_snippet
from ..tools.codebase_query import query_codebase_knowledge_graph
from ..tools.directory_lister import list_directory
from ..tools.document_analyzer import analyze_document
from ..tools.file_editor import replace_code_surgically
from ..tools.file_reader import read_file_content
from ..tools.file_writer import create_new_file
from ..tools.semantic_search import get_function_source_by_id, semantic_search_functions
from ..tools.shell_command import run_shell_command


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


def create_rag_orchestrator() -> Agent[RAGDeps, str | DeferredToolRequests]:
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
            deps_type=RAGDeps,
            system_prompt=RAG_ORCHESTRATOR_SYSTEM_PROMPT,
            tools=[
                query_codebase_knowledge_graph,
                get_code_snippet,
                read_file_content,
                Tool(create_new_file, requires_approval=True),
                Tool(replace_code_surgically, requires_approval=True),
                Tool(run_shell_command, requires_approval=True),
                list_directory,
                analyze_document,
                semantic_search_functions,
                get_function_source_by_id,
            ],
            retries=settings.AGENT_RETRIES,
            output_retries=100,
            output_type=[str, DeferredToolRequests],
        )
    except Exception as e:
        raise LLMGenerationError(f"Failed to initialize RAG Orchestrator: {e}") from e
