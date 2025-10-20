from loguru import logger
from pydantic_ai import Tool
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from ..schemas import GraphData
from ..services import QueryProtocol
from ..services.llm import CypherGenerator, LLMGenerationError


class GraphQueryError(Exception):
    """Custom exception for graph query failures."""

    pass


def create_query_tool(
    ingestor: QueryProtocol,
    cypher_gen: CypherGenerator,
    console: Console | None = None,
) -> Tool:
    """
    Factory function that creates the knowledge graph query tool,
    injecting its dependencies.
    """
    # Use provided console or create a default one
    if console is None:
        console = Console(width=None, force_terminal=True)

    async def query_codebase_knowledge_graph(natural_language_query: str) -> GraphData:
        """
        Queries the codebase knowledge graph using natural language.

        Provide your question in plain English about the codebase structure,
        functions, classes, dependencies, or relationships. The tool will
        automatically translate your natural language question into the
        appropriate database query and return the results.

        Examples:
        - "Find all functions that call each other"
        - "What classes are in the user authentication module"
        - "Show me functions with the longest call chains"
        - "Which files contain functions related to database operations"
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
                        elif isinstance(value, bool):
                            # Check bool first since bool is a subclass of int in Python
                            renderable_values.append("✓" if value else "✗")
                        elif isinstance(value, int | float):
                            # Let Rich handle number formatting by converting to string
                            renderable_values.append(str(value))
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
        description="Query the codebase knowledge graph using natural language questions. Ask in plain English about classes, functions, methods, dependencies, or code structure. Examples: 'Find all functions that call each other', 'What classes are in the user module', 'Show me functions with the longest call chains'.",
    )
