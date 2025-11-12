import json
import os
import pathlib
import re
import shutil
import subprocess

import click
import diff_match_patch as dmp
from rich.console import Console
from rich.table import Table

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
        error_output = e.stderr or str(e)
        if "already exists in the index" in error_output:
            click.secho(
                f"⚠️  Submodule already exists at {grammar_path}. Forcing re-installation...",
                fg="yellow",
            )
            try:
                # Force remove and re-add
                click.echo("   -> Removing existing submodule entry...")
                subprocess.run(
                    ["git", "submodule", "deinit", "-f", grammar_path],
                    check=True,
                    capture_output=True,
                    text=True,
                )
                subprocess.run(
                    ["git", "rm", "-f", grammar_path],
                    check=True,
                    capture_output=True,
                    text=True,
                )

                # Clean up .git/modules directory
                modules_path = f".git/modules/{grammar_path}"
                if os.path.exists(modules_path):
                    shutil.rmtree(modules_path)

                click.echo("   -> Re-adding submodule...")
                subprocess.run(
                    [
                        "git",
                        "submodule",
                        "add",
                        "--force",
                        grammar_url,
                        grammar_path,
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                )
                click.echo(f"✅ Successfully re-installed submodule at {grammar_path}")
            except (subprocess.CalledProcessError, OSError) as reinstall_e:
                error_msg = (
                    reinstall_e.stderr
                    if hasattr(reinstall_e, "stderr")
                    else str(reinstall_e)
                )
                click.secho(f"❌ Failed to reinstall submodule: {error_msg}", fg="red")
                click.echo("💡 You may need to remove it manually and try again:")
                click.echo(f"   git submodule deinit -f {grammar_path}")
                click.echo(f"   git rm -f {grammar_path}")
                click.echo(f"   rm -rf .git/modules/{grammar_path}")
                return
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
            raw_extensions = grammar_info.get("file-types", [])
            # Ensure all extensions start with a dot
            file_extension = [
                ext if ext.startswith(".") else f".{ext}" for ext in raw_extensions
            ]

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

        functions = [
            node.strip()
            for node in click.prompt(
                "Select nodes representing FUNCTIONS (comma-separated)", type=str
            ).split(",")
        ]
        classes = [
            node.strip()
            for node in click.prompt(
                "Select nodes representing CLASSES (comma-separated)", type=str
            ).split(",")
        ]
        modules = [
            node.strip()
            for node in click.prompt(
                "Select nodes representing MODULES (comma-separated)", type=str
            ).split(",")
        ]
        calls = [
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

            # Use tree-sitter's official semantic hierarchy
            def extract_semantic_categories(
                node_types_json: list[dict],
            ) -> dict[str, list[str]]:
                """Extract semantic categories from tree-sitter's official hierarchy."""
                categories: dict[str, list[str]] = {}

                for node in node_types_json:
                    if isinstance(node, dict) and "type" in node:
                        node_type = node["type"]

                        # If this node has subtypes, it's a semantic category
                        if "subtypes" in node:
                            subtypes = [
                                subtype["type"]
                                for subtype in node["subtypes"]
                                if "type" in subtype
                            ]
                            if node_type in categories:
                                categories[node_type].extend(subtypes)
                            else:
                                categories[node_type] = subtypes

                # Remove duplicates
                for category in categories:
                    categories[category] = list(set(categories[category]))

                return categories

            semantic_categories = extract_semantic_categories(node_types)

            # Debug: show all available node types
            click.echo(f"📊 Found {len(all_node_names)} total node types in grammar")

            # Show tree-sitter's official semantic categories
            click.echo("🌳 Tree-sitter semantic categories:")
            for category, subtypes in semantic_categories.items():
                click.echo(
                    f"  {category}: {subtypes[:5]}{'...' if len(subtypes) > 5 else ''} ({len(subtypes)} total)"
                )

            # Map to our simplified categories - search ALL semantic categories
            functions = []
            classes = []
            modules = []
            calls = []

            # Search ALL semantic categories for relevant types
            for category, subtypes in semantic_categories.items():
                for subtype in subtypes:
                    subtype_lower = subtype.lower()

                    # Function-like patterns
                    if (
                        any(
                            kw in subtype_lower
                            for kw in [
                                "function",
                                "method",
                                "constructor",
                                "destructor",
                                "lambda",
                                "arrow_function",
                                "anonymous_function",
                                "closure",
                            ]
                        )
                        and "call" not in subtype_lower
                    ):
                        functions.append(subtype)

                    # Class-like patterns
                    elif (
                        any(
                            kw in subtype_lower
                            for kw in [
                                "class",
                                "interface",
                                "struct",
                                "enum",
                                "trait",
                                "object",
                                "type",
                                "impl",
                                "union",
                            ]
                        )
                        and "access" not in subtype_lower
                        and "call" not in subtype_lower
                    ):
                        classes.append(subtype)

                    # Call patterns
                    elif any(
                        kw in subtype_lower for kw in ["call", "invoke", "invocation"]
                    ):
                        calls.append(subtype)

                    # Module patterns
                    elif any(
                        kw in subtype_lower
                        for kw in [
                            "program",
                            "source_file",
                            "compilation_unit",
                            "module",
                            "chunk",
                        ]
                    ):
                        modules.append(subtype)

            # Also check root nodes for modules
            root_nodes = [
                node["type"]
                for node in node_types
                if isinstance(node, dict) and node.get("root")
            ]
            modules.extend(root_nodes)

            # Remove duplicates
            functions = list(set(functions))
            classes = list(set(classes))
            modules = list(set(modules))
            calls = list(set(calls))

            click.echo("\n🎯 Mapped to our categories:")
            click.echo(f"Functions: {functions}")
            click.echo(f"Classes: {classes}")
            click.echo(f"Modules: {modules}")
            click.echo(f"Calls: {calls}")

        except Exception as e:
            click.echo(f"Error parsing node-types.json: {e}")
            # Fallback to manual input
            functions = ["method_declaration"]
            classes = ["class_declaration"]
            modules = ["compilation_unit"]
            calls = ["invocation_expression"]

    # Step 4: Generate LanguageConfig object
    new_language_config = LanguageConfig(
        name=language_name,
        file_extensions=file_extension,
        function_node_types=functions,
        class_node_types=classes,
        module_node_types=modules,
        call_node_types=calls,
    )

    LANGUAGE_CONFIGS[language_name] = new_language_config

    # Step 5: Automatically add to language_config.py
    config_file_path = "codebase_rag/language_config.py"
    try:
        config_content = pathlib.Path(config_file_path).read_text()
        # Generate the new config entry
        config_entry = f"""    "{language_name}": LanguageConfig(
        name="{new_language_config.name}",
        file_extensions={new_language_config.file_extensions},
        function_node_types={new_language_config.function_node_types},
        class_node_types={new_language_config.class_node_types},
        module_node_types={new_language_config.module_node_types},
        call_node_types={new_language_config.call_node_types},
    ),"""

        # Find the end of the LANGUAGE_CONFIGS dictionary
        language_configs_pos = config_content.find("LANGUAGE_CONFIGS = {")
        closing_brace_pos = config_content.find("\n}", language_configs_pos)
        if closing_brace_pos != -1:
            # Move pos to the closing brace
            closing_brace_pos += 1

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

            # User verification note
            click.echo()
            click.echo(
                click.style(
                    "📋 Please review the detected node types:", bold=True, fg="yellow"
                )
            )
            click.echo("   The auto-detection is good but may need manual adjustments.")
            click.echo(f"   Edit the configuration in: {config_file_path}")
            click.echo()
            click.echo("🎯 Look for these common issues:")
            click.echo(
                "   • Remove misclassified types (e.g., table_constructor in functions)"
            )
            click.echo("   • Add missing types that should be included")
            click.echo(
                "   • Verify class_node_types includes all relevant class-like constructs"
            )
            click.echo("   • Check call_node_types covers all function call patterns")
            click.echo()
            click.echo(
                "💡 You can run 'python -m codebase_rag.tools.language list-languages' to see the current config."
            )
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
    console = Console()

    table = Table(
        title="📋 Configured Languages", show_header=True, header_style="bold magenta"
    )
    table.add_column("Language", style="cyan", width=12)
    table.add_column("Extensions", style="green", width=20)
    table.add_column("Function Types", style="yellow", width=30)
    table.add_column("Class Types", style="blue", width=35)
    table.add_column("Call Types", style="red", width=30)

    for lang_name, config in LANGUAGE_CONFIGS.items():
        extensions = ", ".join(config.file_extensions)
        function_types = (
            ", ".join(config.function_node_types) if config.function_node_types else "—"
        )
        class_types = (
            ", ".join(config.class_node_types) if config.class_node_types else "—"
        )
        call_types = (
            ", ".join(config.call_node_types) if config.call_node_types else "—"
        )

        table.add_row(lang_name, extensions, function_types, class_types, call_types)

    console.print(table)


@cli.command()
@click.argument("language_name")
@click.option(
    "--keep-submodule", is_flag=True, help="Keep the git submodule (default: remove it)"
)
def remove_language(language_name: str, keep_submodule: bool = False) -> None:
    """Remove a language from the project."""
    if language_name not in LANGUAGE_CONFIGS:
        available_langs = ", ".join(LANGUAGE_CONFIGS.keys())
        click.echo(f"❌ Language '{language_name}' not found.")
        click.echo(f"📋 Available languages: {available_langs}")
        return

    # Step 1: Remove from config file using diff-match-patch
    config_file = "codebase_rag/language_config.py"
    try:
        original_content = pathlib.Path(config_file).read_text()
        # Find and remove the language config entry with better pattern
        # Match the entire language config entry including multiline content
        pattern = rf'    "{language_name}": LanguageConfig\([\s\S]*?\),\n'
        new_content = re.sub(pattern, "", original_content)

        # Use diff-match-patch for safer editing
        dmp_obj = dmp.diff_match_patch()
        patches = dmp_obj.patch_make(original_content, new_content)
        result, _ = dmp_obj.patch_apply(patches, original_content)

        with open(config_file, "w") as f:
            f.write(result)

        click.echo(f"✅ Removed language '{language_name}' from configuration file.")

    except Exception as e:
        click.echo(f"❌ Failed to update config file: {e}")
        return

    # Step 2: Remove git submodule automatically (unless --keep-submodule flag is used)
    if not keep_submodule:
        submodule_path = f"grammars/tree-sitter-{language_name}"
        if os.path.exists(submodule_path):
            try:
                click.echo(f"🔄 Removing git submodule '{submodule_path}'...")
                # Remove submodule completely
                subprocess.run(
                    ["git", "submodule", "deinit", "-f", submodule_path],
                    check=True,
                    capture_output=True,
                )
                subprocess.run(
                    ["git", "rm", "-f", submodule_path], check=True, capture_output=True
                )

                # Clean up .git/modules directory (this is crucial!)
                modules_path = f".git/modules/{submodule_path}"
                if os.path.exists(modules_path):
                    shutil.rmtree(modules_path)
                    click.echo(f"🧹 Cleaned up git modules directory: {modules_path}")

                click.echo(f"🗑️  Successfully removed submodule '{submodule_path}'")
            except subprocess.CalledProcessError as e:
                click.echo(f"❌ Failed to remove submodule: {e}")
                click.echo("💡 You may need to remove it manually:")
                click.echo(f"   git submodule deinit -f {submodule_path}")
                click.echo(f"   git rm -f {submodule_path}")
        else:
            click.echo(f"ℹ️  No submodule found at '{submodule_path}'")
    else:
        click.echo("ℹ️  Keeping submodule (--keep-submodule flag used)")

    click.echo(f"🎉 Language '{language_name}' has been removed successfully!")


@cli.command()
def cleanup_orphaned_modules() -> None:
    """Clean up orphaned git modules that weren't properly removed."""
    modules_dir = ".git/modules/grammars"
    if not os.path.exists(modules_dir):
        click.echo("📂 No grammars modules directory found.")
        return

    # Read .gitmodules to see what should exist
    gitmodules_submodules = set()
    try:
        with open(".gitmodules") as f:
            content = f.read()
            # Find all submodule paths
            paths = re.findall(r"path = (grammars/tree-sitter-[^\\n]+)", content)
            gitmodules_submodules = set(paths)
    except FileNotFoundError:
        click.echo("📄 No .gitmodules file found.")

    # Check what modules exist
    orphaned = []
    for item in os.listdir(modules_dir):
        module_path = f"grammars/{item}"
        if module_path not in gitmodules_submodules:
            orphaned.append(item)

    if not orphaned:
        click.echo("✨ No orphaned modules found!")
        return

    click.echo(f"🔍 Found {len(orphaned)} orphaned module(s): {', '.join(orphaned)}")

    if click.confirm("Do you want to remove these orphaned modules?"):
        for module in orphaned:
            module_path = os.path.join(modules_dir, module)
            shutil.rmtree(module_path)
            click.echo(f"🗑️  Removed orphaned module: {module}")
        click.echo("🎉 Cleanup complete!")
    else:
        click.echo("❌ Cleanup cancelled.")


if __name__ == "__main__":
    cli()
