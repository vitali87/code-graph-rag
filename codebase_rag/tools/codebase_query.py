from pydantic_ai import Tool, RunContext
from rich.table import Table
from rich.panel import Panel
from rich.console import Console
from ..schemas import GraphData
from ..graph_updater import MemgraphIngestor
from ..services.llm import CypherGenerator, LLMGenerationError
from loguru import logger


class GraphQueryError(Exception):
    """Custom exception for graph query failures."""

    pass


def create_query_tool(ingestor: MemgraphIngestor, cypher_gen: CypherGenerator) -> Tool:
    """
    Factory function that creates the knowledge graph query tool,
    injecting its dependencies.
    """
    console = Console()

    async def query_codebase_knowledge_graph(
        ctx: RunContext, natural_language_query: str
    ) -> GraphData:
        """
        Queries the codebase knowledge graph. Translates a natural language question
        into a Cypher query, executes it against the Memgraph database, and returns
        the structured results.
        """
        logger.info(f"[Tool:QueryGraph] Received NL query: '{natural_language_query}'")
        cypher_query = "N/A"
        try:
            cypher_query = await cypher_gen.generate(natural_language_query)

            results = ingestor.fetch_all(cypher_query)

            if results:
                table = Table(
                    show_header=True,
                    header_style="bold magenta",
                )
                headers = results[0].keys()
                for header in headers:
                    table.add_column(header)

                for row in results:
                    renderable_values = []
                    for value in row.values():
                        if value is None:
                            renderable_values.append("")
                        elif isinstance(value, (int, float)):
                            # Let Rich handle number formatting by converting to string
                            renderable_values.append(str(value))
                        elif isinstance(value, bool):
                            renderable_values.append("✓" if value else "✗")
                        else:
                            renderable_values.append(str(value))
                    table.add_row(*renderable_values)

                console.print(
                    Panel(
                        table,
                        title="[bold blue]Cypher Query Results[/bold blue]",
                        expand=False,
                    )
                )

            summary = f"Successfully retrieved {len(results)} item(s) from the graph."
            return GraphData(query_used=cypher_query, results=results, summary=summary)
        except LLMGenerationError as e:
            return GraphData(
                query_used="N/A",
                results=[],
                summary=f"I couldn't translate your request into a database query. Error: {e}",
            )
        except Exception as e:
            logger.error(
                f"[Tool:QueryGraph] Error during query execution: {e}", exc_info=True
            )
            return GraphData(
                query_used=cypher_query,
                results=[],
                summary=f"There was an error querying the database: {e}",
            )

    return Tool(
        function=query_codebase_knowledge_graph,
        description="Use this tool to query the codebase knowledge graph for specific information like classes, functions, methods, dependencies, or code structure.",
    )
