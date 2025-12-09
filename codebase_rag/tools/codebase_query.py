from loguru import logger
from pydantic_ai import RunContext
from rich.panel import Panel
from rich.table import Table

from ..deps import RAGDeps
from ..exceptions import LLMGenerationError
from ..schemas import GraphData


async def query_codebase_knowledge_graph(
    ctx: RunContext[RAGDeps], natural_language_query: str
) -> GraphData:
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
        cypher_query = await ctx.deps.cypher_generator.generate(natural_language_query)

        results = ctx.deps.ingestor.fetch_all(cypher_query)

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
                        renderable_values.append("✓" if value else "✗")
                    elif isinstance(value, int | float):
                        renderable_values.append(str(value))
                    else:
                        renderable_values.append(str(value))
                table.add_row(*renderable_values)

            ctx.deps.console.print(
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
