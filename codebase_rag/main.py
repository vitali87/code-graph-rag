import asyncio
import sys
import typer
from typing import Optional, List
from rich.panel import Panel
from rich.prompt import Prompt
from rich.console import Console
from rich.table import Table

from .config import settings
from .graph_updater import MemgraphIngestor, GraphUpdater
from .services.llm import CypherGenerator, create_rag_orchestrator
from .tools.codebase_query import create_query_tool
from .tools.code_retrieval import create_code_retrieval_tool, CodeRetriever
from .tools.file_reader import create_file_reader_tool, FileReader
from .tools.file_writer import create_file_writer_tool, FileWriter
from .tools.file_editor import create_file_editor_tool, FileEditor

from loguru import logger

app = typer.Typer(
    name="graph-code",
    help="An accurate Retrieval-Augmented Generation (RAG) system that analyzes "
    "multi-language codebases using Tree-sitter, builds comprehensive knowledge "
    "graphs, and enables natural language querying of codebase structure and "
    "relationships.",
)
console = Console()


async def run_chat_loop(rag_agent, message_history: List):
    """Runs the main chat loop."""
    while True:
        try:
            question = await asyncio.to_thread(
                Prompt.ask, "[bold cyan]Ask a question[/bold cyan]"
            )
            if question.lower() in ["exit", "quit"]:
                break
            if not question.strip():
                continue

            with console.status("[bold green]Thinking...[/bold green]"):
                response = await rag_agent.run(question, message_history=message_history)

            console.print(Panel(response.output, title="[bold green]Final Answer[/bold green]", border_style="green"))
            message_history.extend(response.new_messages())

        except KeyboardInterrupt:
            break
        except Exception as e:
            logger.error(f"An unexpected error occurred: {e}", exc_info=True)
            console.print(f"[bold red]An unexpected error occurred: {e}[/bold red]")


def _update_model_settings(
    llm_provider: Optional[str],
    orchestrator_model: Optional[str],
    cypher_model: Optional[str],
):
    """Update model settings based on command-line arguments."""
    if llm_provider:
        settings.LLM_PROVIDER = llm_provider

    provider = settings.LLM_PROVIDER
    if orchestrator_model:
        if provider == "gemini":
            settings.GEMINI_MODEL_ID = orchestrator_model
        else:
            settings.LOCAL_ORCHESTRATOR_MODEL_ID = orchestrator_model
    if cypher_model:
        if provider == "gemini":
            settings.MODEL_CYPHER_ID = cypher_model
        else:
            settings.LOCAL_CYPHER_MODEL_ID = cypher_model


async def main_async(repo_path: str):
    """Initializes services and runs the main application loop."""
    logger.remove()
    logger.add(sys.stdout, format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {message}")

    table = Table(title="[bold green]Graph-Code Initializing...[/bold green]")
    table.add_column("Configuration", style="cyan")
    table.add_column("Value", style="magenta")
    table.add_row("LLM Provider", settings.LLM_PROVIDER)
    if settings.LLM_PROVIDER == "gemini":
        table.add_row("Orchestrator Model", settings.GEMINI_MODEL_ID)
        table.add_row("Cypher Model", settings.MODEL_CYPHER_ID)
    else:
        table.add_row("Orchestrator Model", settings.LOCAL_ORCHESTRATOR_MODEL_ID)
        table.add_row("Cypher Model", settings.LOCAL_CYPHER_MODEL_ID)
        table.add_row("Local Model Endpoint", settings.LOCAL_MODEL_ENDPOINT)
    table.add_row("Target Repository", repo_path)
    console.print(table)

    with MemgraphIngestor(host=settings.MEMGRAPH_HOST, port=settings.MEMGRAPH_PORT) as ingestor:
        console.print("[bold green]Successfully connected to Memgraph.[/bold green]")
        console.print(Panel(
            "[bold yellow]Ask questions about your codebase graph. Type 'exit' or 'quit' to end.[/bold yellow]",
            border_style="yellow"
        ))

        cypher_generator = CypherGenerator()
        code_retriever = CodeRetriever(project_root=repo_path, ingestor=ingestor)
        file_reader = FileReader(project_root=repo_path)
        file_writer = FileWriter(project_root=repo_path)
        file_editor = FileEditor(project_root=repo_path)

        query_tool = create_query_tool(ingestor, cypher_generator)
        code_tool = create_code_retrieval_tool(code_retriever)
        file_reader_tool = create_file_reader_tool(file_reader)
        file_writer_tool = create_file_writer_tool(file_writer)
        file_editor_tool = create_file_editor_tool(file_editor)

        rag_agent = create_rag_orchestrator(
            tools=[query_tool, code_tool, file_reader_tool, file_writer_tool, file_editor_tool]
        )

        await run_chat_loop(rag_agent, [])


@app.command()
def start(
    repo_path: Optional[str] = typer.Option(
        None, "--repo-path", help="Path to the target repository for code retrieval"
    ),
    update_graph: bool = typer.Option(
        False, "--update-graph", help="Update the knowledge graph by parsing the repository"
    ),
    clean: bool = typer.Option(
        False, "--clean", help="Clean the database before updating (use when adding first repo)"
    ),
    llm_provider: Optional[str] = typer.Option(
        None, "--llm-provider", help="Choose the LLM provider: 'gemini' or 'local'"
    ),
    orchestrator_model: Optional[str] = typer.Option(
        None, "--orchestrator-model", help="Specify the orchestrator model ID"
    ),
    cypher_model: Optional[str] = typer.Option(
        None, "--cypher-model", help="Specify the Cypher generator model ID"
    ),
):
    """Starts the Codebase RAG CLI."""
    target_repo_path = repo_path or settings.TARGET_REPO_PATH
    
    _update_model_settings(llm_provider, orchestrator_model, cypher_model)

    if update_graph:
        from pathlib import Path
        
        repo_to_update = Path(target_repo_path)
        console.print(f"[bold green]Updating knowledge graph for: {repo_to_update}[/bold green]")

        with MemgraphIngestor(host=settings.MEMGRAPH_HOST, port=settings.MEMGRAPH_PORT) as ingestor:
            if clean:
                console.print("[bold yellow]Cleaning database...[/bold yellow]")
                ingestor.clean_database()
            ingestor.ensure_constraints()
            updater = GraphUpdater(ingestor, repo_to_update)
            updater.run()
        
        console.print("[bold green]Graph update completed![/bold green]")
        return

    try:
        asyncio.run(main_async(target_repo_path))
    except KeyboardInterrupt:
        console.print("\n[bold red]Application terminated by user.[/bold red]")
    except ValueError as e:
        console.print(f"[bold red]Startup Error: {e}[/bold red]")


if __name__ == "__main__":
    app()
