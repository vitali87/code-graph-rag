import json
import os
import subprocess
from dataclasses import asdict

import click

from ..language_config import LANGUAGE_CONFIGS, LanguageConfig


@click.group()
def cli() -> None:
    """CLI for managing language grammars"""
    pass


@cli.command()
@click.argument("language_name", required=False)
@click.option(
    "--grammar-url",
    help="URL to the tree-sitter grammar repository. If not provided, will use https://github.com/tree-sitter/tree-sitter-<language_name>",
)
def add_grammar(
    language_name: str | None = None, grammar_url: str | None = None
) -> None:
    """Add a new language grammar to the project."""

    # Handle input validation and URL construction
    if not language_name and not grammar_url:
        language_name = click.prompt("Language name (e.g., 'c-sharp', 'python')")

    if not grammar_url:
        if not language_name:
            click.echo(
                "❌ Error: Either language_name or --grammar-url must be provided"
            )
            return
        grammar_url = f"https://github.com/tree-sitter/tree-sitter-{language_name}"
        click.echo(f"🔍 Using default tree-sitter URL: {grammar_url}")

    # Security check for custom URLs
    if grammar_url and "github.com/tree-sitter/tree-sitter" not in grammar_url:
        click.secho(
            "⚠️ WARNING: You are adding a grammar from a custom URL. This may execute code from the repository. Only proceed if you trust the source.",
            fg="yellow",
            bold=True,
        )
        if not click.confirm("Do you want to continue?"):
            return

    # Step 1: Clone the grammar into the grammars/ directory as a Git submodule
    grammars_dir = "grammars"
    if not os.path.exists(grammars_dir):
        os.makedirs(grammars_dir)

    grammar_dir_name = os.path.basename(grammar_url).removesuffix(".git")
    grammar_path = os.path.join(grammars_dir, grammar_dir_name)

    try:
        click.echo(f"🔄 Adding submodule from {grammar_url}...")
        subprocess.run(
            [
                "git",
                "submodule",
                "add",
                grammar_url,
                grammar_path,
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        click.echo(f"✅ Successfully added submodule at {grammar_path}")
    except subprocess.CalledProcessError as e:
        error_output = e.stderr if e.stderr else str(e)
        if "already exists in the index" in error_output:
            click.echo(
                f"⚠️  Submodule already exists at {grammar_path}. Continuing with configuration..."
            )
        elif "does not exist" in error_output or "not found" in error_output:
            click.echo(f"❌ Error: Repository not found at {grammar_url}")
            click.echo("💡 Try using a custom URL with: --grammar-url <your-repo-url>")
            return
        else:
            click.echo(f"❌ Git error: {error_output}")
            raise

    # Step 2: Auto-extract language info from tree-sitter.json
    tree_sitter_json_path = os.path.join(grammar_path, "tree-sitter.json")

    if not os.path.exists(tree_sitter_json_path):
        click.echo(f"Warning: tree-sitter.json not found in {grammar_path}")
        if not language_name:
            language_name = click.prompt("What is the common name for this language?")
        file_extension = [
            ext.strip()
            for ext in click.prompt(
                "What file extensions should be associated with this language? (comma-separated)"
            ).split(",")
        ]
    else:
        with open(tree_sitter_json_path) as f:
            tree_sitter_config = json.load(f)

        if "grammars" in tree_sitter_config and len(tree_sitter_config["grammars"]) > 0:
            grammar_info = tree_sitter_config["grammars"][0]
            detected_name = grammar_info.get("name", grammar_dir_name)
            file_extension = grammar_info.get("file-types", [])

            # Use provided language_name or fall back to detected name
            if not language_name:
                language_name = detected_name

            click.echo(f"Auto-detected language: {detected_name}")
            click.echo(f"Using language name: {language_name}")
            click.echo(f"Auto-detected file extensions: {file_extension}")
        else:
            click.echo("Warning: No grammars found in tree-sitter.json")
            if not language_name:
                language_name = click.prompt(
                    "What is the common name for this language?"
                )
            file_extension = [
                ext.strip()
                for ext in click.prompt(
                    "What file extensions should be associated with this language? (comma-separated)"
                ).split(",")
            ]

    # Step 3: Auto-detect node types from grammar
    # Try different possible locations for node-types.json
    possible_paths = [
        os.path.join(grammar_path, "src", "node-types.json"),  # Standard location
        os.path.join(
            grammar_path, language_name, "src", "node-types.json"
        ),  # Nested by language name
        os.path.join(
            grammar_path, language_name.replace("-", "_"), "src", "node-types.json"
        ),  # Underscore variant
    ]

    node_types_path = None
    for path in possible_paths:
        if os.path.exists(path):
            node_types_path = path
            break

    if not node_types_path:
        click.echo(
            f"Warning: node-types.json not found in any expected location for {language_name}"
        )
        # Fallback to manual input
        function_nodes = ["function_definition", "method_definition"]
        class_nodes = ["class_declaration"]
        click.echo("Available nodes for mapping:")
        click.echo(f"Functions: {function_nodes}")
        click.echo(f"Classes: {class_nodes}")

        selected_function_nodes = [
            node.strip()
            for node in click.prompt(
                "Select nodes representing FUNCTIONS (comma-separated)", type=str
            ).split(",")
        ]
        selected_class_nodes = [
            node.strip()
            for node in click.prompt(
                "Select nodes representing CLASSES (comma-separated)", type=str
            ).split(",")
        ]
        selected_module_nodes = [
            node.strip()
            for node in click.prompt(
                "Select nodes representing MODULES (comma-separated)", type=str
            ).split(",")
        ]
        selected_call_nodes = [
            node.strip()
            for node in click.prompt(
                "Select nodes representing FUNCTION CALLS (comma-separated)", type=str
            ).split(",")
        ]
    else:
        # Auto-detect from node-types.json
        try:
            with open(node_types_path) as f:
                node_types = json.load(f)

            # Extract all node type names
            all_node_names = set()

            def extract_types(obj: dict | list) -> None:
                if isinstance(obj, dict):
                    if "type" in obj and isinstance(obj["type"], str):
                        all_node_names.add(obj["type"])
                    for value in obj.values():
                        if isinstance(value, dict | list):
                            extract_types(value)
                elif isinstance(obj, list):
                    for item in obj:
                        if isinstance(item, dict | list):
                            extract_types(item)

            extract_types(node_types)

            # Common patterns for different node types
            function_patterns = [
                "method_declaration",
                "function_declaration",
                "constructor_declaration",
                "destructor_declaration",
            ]
            class_patterns = [
                "class_declaration",
                "interface_declaration",
                "struct_declaration",
                "enum_declaration",
            ]
            module_patterns = [
                "compilation_unit",
                "source_file",
                "program",
                "namespace_declaration",
            ]
            call_patterns = [
                "call_expression",
                "method_invocation",
                "invocation_expression",
            ]

            # Match patterns to actual node types
            selected_function_nodes = [
                name
                for name in all_node_names
                if any(pattern in name for pattern in function_patterns)
            ]
            selected_class_nodes = [
                name
                for name in all_node_names
                if any(pattern in name for pattern in class_patterns)
            ]
            selected_module_nodes = [
                name
                for name in all_node_names
                if any(pattern in name for pattern in module_patterns)
            ]
            selected_call_nodes = [
                name
                for name in all_node_names
                if any(pattern in name for pattern in call_patterns)
            ]

            click.echo("Auto-detected node types:")
            click.echo(f"Functions: {selected_function_nodes}")
            click.echo(f"Classes: {selected_class_nodes}")
            click.echo(f"Modules: {selected_module_nodes}")
            click.echo(f"Calls: {selected_call_nodes}")

        except Exception as e:
            click.echo(f"Error parsing node-types.json: {e}")
            # Fallback to manual input
            selected_function_nodes = ["method_declaration"]
            selected_class_nodes = ["class_declaration"]
            selected_module_nodes = ["compilation_unit"]
            selected_call_nodes = ["invocation_expression"]

    # Step 4: Generate LanguageConfig object
    new_language_config = LanguageConfig(
        name=language_name,
        file_extensions=file_extension,
        function_node_types=selected_function_nodes,
        class_node_types=selected_class_nodes,
        module_node_types=selected_module_nodes,
        call_node_types=selected_call_nodes,
    )

    LANGUAGE_CONFIGS[language_name] = new_language_config

    # Step 5: Automatically add to language_config.py
    config_file_path = "codebase_rag/language_config.py"
    try:
        with open(config_file_path) as f:
            config_content = f.read()

        # Generate the new config entry
        config_entry = f'''    "{language_name}": LanguageConfig(
        name="{new_language_config.name}",
        file_extensions={new_language_config.file_extensions},
        function_node_types={new_language_config.function_node_types},
        class_node_types={new_language_config.class_node_types},
        module_node_types={new_language_config.module_node_types},
        call_node_types={new_language_config.call_node_types},
    ),'''

        # Find the end of the LANGUAGE_CONFIGS dictionary
        closing_brace_pos = config_content.rfind("}")
        if closing_brace_pos != -1:
            # Insert the new config before the closing brace
            new_content = (
                config_content[:closing_brace_pos]
                + config_entry
                + "\n"
                + config_content[closing_brace_pos:]
            )

            with open(config_file_path, "w") as f:
                f.write(new_content)

            click.echo(
                f"\n✅ Language '{language_name}' has been added to the configuration!"
            )
            click.echo(f"📝 Updated {config_file_path}")
        else:
            raise ValueError("Could not find LANGUAGE_CONFIGS dictionary end")

    except Exception as e:
        click.echo(f"❌ Error updating config file: {e}")
        click.echo(
            click.style(
                "FALLBACK: Please manually add the following entry to 'LANGUAGE_CONFIGS' in 'codebase_rag/language_config.py':",
                bold=True,
            )
        )
        click.echo(click.style(config_entry, fg="green"))


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
        click.echo(f"Removed language '{language_name}' from the current session.")
        click.echo(
            click.style(
                "To permanently remove it, you must perform these steps manually:",
                bold=True,
            )
        )
        click.echo(
            f"1. Delete the '{language_name}' entry from 'LANGUAGE_CONFIGS' in 'codebase_rag/language_config.py'."
        )
        click.echo(
            "2. Manually remove the submodule. The exact path is in your '.gitmodules' file. Example commands:"
        )
        click.echo(
            click.style(
                "   git submodule deinit -f -- <path-to-submodule>", fg="yellow"
            )
        )
        click.echo(click.style("   git rm -f <path-to-submodule>", fg="yellow"))
    else:
        click.echo(f"Language not found: {language_name}")


if __name__ == "__main__":
    cli()
