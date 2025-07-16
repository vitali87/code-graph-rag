import os
import subprocess
from dataclasses import asdict

import click

from .language_config import LANGUAGE_CONFIGS, LanguageConfig


@click.group()
def cli() -> None:
    """CLI for managing language grammars"""
    pass


@cli.command()
@click.option(
    "--grammar-url",
    prompt="Git repository URL for the grammar",
    help="URL to the tree-sitter grammar repository.",
)
def add_grammar(grammar_url: str) -> None:
    """Add a new language grammar to the project."""
    # Step 1: Clone the grammar into the grammars/ directory as a Git submodule
    grammars_dir = "grammars"
    if not os.path.exists(grammars_dir):
        os.makedirs(grammars_dir)
    subprocess.run(
        [
            "git",
            "submodule",
            "add",
            grammar_url,
            os.path.join(grammars_dir, os.path.basename(grammar_url)),
        ]
    )

    # Step 2: Interactive setup
    language_name = click.prompt("What is the common name for this language?")
    file_extension = click.prompt(
        "What file extensions should be associated with this language? (comma-separated)"
    ).split(",")

    # Step 3: Intelligent Node Suggestion
    # This would require cloning and extracting from grammar.js (not implemented yet)
    # For now, we simulate this with a placeholder
    # Suggested nodes would typically be parsed from the grammar repository
    function_nodes = ["function_definition", "method_definition"]  # Example nodes
    class_nodes = ["class_declaration"]  # Example nodes
    click.echo("Available nodes for mapping:")
    click.echo(f"Functions: {function_nodes}")
    click.echo(f"Classes: {class_nodes}")

    selected_function_nodes = click.prompt(
        "Select nodes representing FUNCTIONS (comma-separated)", type=str
    ).split(",")
    selected_class_nodes = click.prompt(
        "Select nodes representing CLASSES (comma-separated)", type=str
    ).split(",")

    # Step 4: Generate LanguageConfig object
    new_language_config = LanguageConfig(
        name=language_name,
        file_extensions=file_extension,
        function_node_types=selected_function_nodes,
        class_node_types=selected_class_nodes,
    )

    LANGUAGE_CONFIGS[language_name] = new_language_config
    click.echo(f"Language {language_name} has been configured!")


@cli.command()
def list_languages() -> None:
    """List all currently configured languages."""
    for lang_name, config in LANGUAGE_CONFIGS.items():
        click.echo(f"{lang_name}: {asdict(config)}")


@cli.command()
@click.argument("language_name")
def remove_language(language_name: str) -> None:
    """Remove a language from the project."""
    if language_name in LANGUAGE_CONFIGS:
        del LANGUAGE_CONFIGS[language_name]
        click.echo(f"Removed language: {language_name}")
    else:
        click.echo(f"Language not found: {language_name}")


if __name__ == "__main__":
    cli()
