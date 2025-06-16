# codebase_rag/services/llm.py
from pydantic_ai import Agent
from pydantic_ai.models.gemini import GeminiModel
from pydantic_ai.providers.google_gla import GoogleGLAProvider
from ..config import settings
from ..prompts import TEXT_TO_CYPHER_SYSTEM_PROMPT, RAG_ORCHESTRATOR_SYSTEM_PROMPT

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
    def __init__(self):
        try:
            llm = GeminiModel(
                settings.GEMINI_MODEL_ID,
                provider=GoogleGLAProvider(api_key=settings.GEMINI_API_KEY)
            )
            self.agent = Agent(
                model=llm,
                system_prompt=TEXT_TO_CYPHER_SYSTEM_PROMPT,
                output_type=str
            )
        except Exception as e:
            raise LLMGenerationError(f"Failed to initialize CypherGenerator: {e}")

    async def generate(self, natural_language_query: str) -> str:
        print(f"  [CypherGenerator] Generating query for: '{natural_language_query}'")
        try:
            result = await self.agent.run(natural_language_query)
            if not isinstance(result.output, str) or "MATCH" not in result.output.upper():
                raise LLMGenerationError(f"LLM did not generate a valid query. Output: {result.output}")
            
            query = _clean_cypher_response(result.output)
            print(f"  [CypherGenerator] Generated Cypher: {query}")
            return query
        except Exception as e:
            print(f"  [CypherGenerator] Error: {e}")
            raise LLMGenerationError(f"Cypher generation failed: {e}")


def create_rag_orchestrator(tools: list) -> Agent:
    """Factory function to create the main RAG orchestrator agent."""
    try:
        llm = GeminiModel(
            settings.GEMINI_MODEL_ID,
            provider=GoogleGLAProvider(api_key=settings.GEMINI_API_KEY)
        )
        return Agent(
            model=llm,
            system_prompt=RAG_ORCHESTRATOR_SYSTEM_PROMPT,
            tools=tools,
            debug_mode=True
        )
    except Exception as e:
        raise LLMGenerationError(f"Failed to initialize RAG Orchestrator: {e}")