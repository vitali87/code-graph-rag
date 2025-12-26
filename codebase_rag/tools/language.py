from __future__ import annotations

import json
import os
import pathlib
import re
import shutil
import subprocess

import click
import diff_match_patch as dmp
from loguru import logger
from rich.console import Console
from rich.table import Table

from .. import constants as cs
from ..language_spec import LANGUAGE_SPECS, LanguageSpec


@click.group(help="CLI for managing language grammars")
def cli() -> None:
    pass


@cli.command(help="Add a new language grammar to the project.")
@click.argument("language_name", required=False)
@click.option(
    "--grammar-url",
    help="URL to the tree-sitter grammar repository. If not provided, will use https://github.com/tree-sitter/tree-sitter-<language_name>",
)
def add_grammar(
    language_name: str | None = None, grammar_url: str | None = None
) -> None:
    if not language_name and not grammar_url:
        language_name = click.prompt(cs.LANG_PROMPT_LANGUAGE_NAME)

    if not grammar_url:
        if not language_name:
            click.echo(f"❌ {cs.LANG_ERR_MISSING_ARGS}")
            return
        grammar_url = cs.LANG_DEFAULT_GRAMMAR_URL.format(name=language_name)
        click.echo(f"🔍 {cs.LANG_MSG_USING_DEFAULT_URL.format(url=grammar_url)}")

    if grammar_url and cs.LANG_TREE_SITTER_URL_MARKER not in grammar_url:
        click.secho(
            f"⚠️ {cs.LANG_MSG_CUSTOM_URL_WARNING}",
            fg=cs.Color.YELLOW,
            bold=True,
        )
        if not click.confirm(cs.LANG_PROMPT_CONTINUE):
            return

    if not os.path.exists(cs.LANG_GRAMMARS_DIR):
        os.makedirs(cs.LANG_GRAMMARS_DIR)

    grammar_dir_name = os.path.basename(grammar_url).removesuffix(".git")
    grammar_path = os.path.join(cs.LANG_GRAMMARS_DIR, grammar_dir_name)

    try:
        click.echo(f"🔄 {cs.LANG_MSG_ADDING_SUBMODULE.format(url=grammar_url)}")
        subprocess.run(
            ["git", "submodule", "add", grammar_url, grammar_path],
            check=True,
            capture_output=True,
            text=True,
        )
        click.echo(f"✅ {cs.LANG_MSG_SUBMODULE_SUCCESS.format(path=grammar_path)}")
    except subprocess.CalledProcessError as e:
        error_output = e.stderr or str(e)
        if "already exists in the index" in error_output:
            click.secho(
                f"⚠️  {cs.LANG_MSG_SUBMODULE_EXISTS.format(path=grammar_path)}",
                fg=cs.Color.YELLOW,
            )
            try:
                click.echo(cs.LANG_MSG_REMOVING_ENTRY)
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

                modules_path = cs.LANG_GIT_MODULES_PATH.format(path=grammar_path)
                if os.path.exists(modules_path):
                    shutil.rmtree(modules_path)

                click.echo(cs.LANG_MSG_READDING_SUBMODULE)
                subprocess.run(
                    ["git", "submodule", "add", "--force", grammar_url, grammar_path],
                    check=True,
                    capture_output=True,
                    text=True,
                )
                click.echo(
                    f"✅ {cs.LANG_MSG_REINSTALL_SUCCESS.format(path=grammar_path)}"
                )
            except (subprocess.CalledProcessError, OSError) as reinstall_e:
                error_msg = (
                    reinstall_e.stderr
                    if hasattr(reinstall_e, "stderr")
                    else str(reinstall_e)
                )
                logger.error(cs.LANG_ERR_REINSTALL_FAILED.format(error=error_msg))
                click.secho(
                    f"❌ {cs.LANG_ERR_REINSTALL_FAILED.format(error=error_msg)}",
                    fg=cs.Color.RED,
                )
                click.echo(f"💡 {cs.LANG_ERR_MANUAL_REMOVE_HINT}")
                click.echo(f"   git submodule deinit -f {grammar_path}")
                click.echo(f"   git rm -f {grammar_path}")
                click.echo(
                    f"   rm -rf {cs.LANG_GIT_MODULES_PATH.format(path=grammar_path)}"
                )
                return
        elif "does not exist" in error_output or "not found" in error_output:
            logger.error(cs.LANG_ERR_REPO_NOT_FOUND.format(url=grammar_url))
            click.echo(f"❌ {cs.LANG_ERR_REPO_NOT_FOUND.format(url=grammar_url)}")
            click.echo(f"💡 {cs.LANG_ERR_CUSTOM_URL_HINT}")
            return
        else:
            logger.error(cs.LANG_ERR_GIT.format(error=error_output))
            click.echo(f"❌ {cs.LANG_ERR_GIT.format(error=error_output)}")
            raise

    tree_sitter_json_path = os.path.join(grammar_path, cs.LANG_TREE_SITTER_JSON)

    if not os.path.exists(tree_sitter_json_path):
        click.echo(cs.LANG_ERR_TREE_SITTER_JSON_WARNING.format(path=grammar_path))
        if not language_name:
            language_name = click.prompt(cs.LANG_PROMPT_COMMON_NAME)
        file_extension = [
            ext.strip() for ext in click.prompt(cs.LANG_PROMPT_EXTENSIONS).split(",")
        ]
    else:
        with open(tree_sitter_json_path) as f:
            tree_sitter_config = json.load(f)

        if "grammars" in tree_sitter_config and len(tree_sitter_config["grammars"]) > 0:
            grammar_info = tree_sitter_config["grammars"][0]
            detected_name = grammar_info.get("name", grammar_dir_name)
            raw_extensions = grammar_info.get("file-types", [])
            file_extension = [
                ext if ext.startswith(".") else f".{ext}" for ext in raw_extensions
            ]

            if not language_name:
                language_name = detected_name

            click.echo(cs.LANG_MSG_AUTO_DETECTED_LANG.format(name=detected_name))
            click.echo(cs.LANG_MSG_USING_LANG_NAME.format(name=language_name))
            click.echo(cs.LANG_MSG_AUTO_DETECTED_EXT.format(extensions=file_extension))
        else:
            click.echo(cs.LANG_ERR_NO_GRAMMARS_WARNING)
            if not language_name:
                language_name = click.prompt(cs.LANG_PROMPT_COMMON_NAME)
            file_extension = [
                ext.strip()
                for ext in click.prompt(cs.LANG_PROMPT_EXTENSIONS).split(",")
            ]

    assert language_name is not None
    possible_paths = [
        os.path.join(grammar_path, cs.LANG_SRC_DIR, cs.LANG_NODE_TYPES_JSON),
        os.path.join(
            grammar_path, language_name, cs.LANG_SRC_DIR, cs.LANG_NODE_TYPES_JSON
        ),
        os.path.join(
            grammar_path,
            language_name.replace("-", "_"),
            cs.LANG_SRC_DIR,
            cs.LANG_NODE_TYPES_JSON,
        ),
    ]

    node_types_path = None
    for path in possible_paths:
        if os.path.exists(path):
            node_types_path = path
            break

    if not node_types_path:
        click.echo(cs.LANG_ERR_NODE_TYPES_WARNING.format(name=language_name))
        function_nodes = list(cs.LANG_DEFAULT_FUNCTION_NODES)
        class_nodes = list(cs.LANG_DEFAULT_CLASS_NODES)
        click.echo("Available nodes for mapping:")
        click.echo(cs.LANG_MSG_FUNCTIONS.format(nodes=function_nodes))
        click.echo(cs.LANG_MSG_CLASSES.format(nodes=class_nodes))

        functions = [
            node.strip()
            for node in click.prompt(cs.LANG_PROMPT_FUNCTIONS, type=str).split(",")
        ]
        classes = [
            node.strip()
            for node in click.prompt(cs.LANG_PROMPT_CLASSES, type=str).split(",")
        ]
        modules = [
            node.strip()
            for node in click.prompt(cs.LANG_PROMPT_MODULES, type=str).split(",")
        ]
        calls = [
            node.strip()
            for node in click.prompt(cs.LANG_PROMPT_CALLS, type=str).split(",")
        ]
    else:
        try:
            with open(node_types_path) as f:
                node_types = json.load(f)

            all_node_names: set[str] = set()

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

            def extract_semantic_categories(
                node_types_json: list[dict],
            ) -> dict[str, list[str]]:
                categories: dict[str, list[str]] = {}

                for node in node_types_json:
                    if isinstance(node, dict) and "type" in node:
                        node_type = node["type"]

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

                for category, values in categories.items():
                    categories[category] = list(set(values))

                return categories

            semantic_categories = extract_semantic_categories(node_types)

            click.echo(
                f"📊 {cs.LANG_MSG_FOUND_NODE_TYPES.format(count=len(all_node_names))}"
            )

            click.echo(f"🌳 {cs.LANG_MSG_SEMANTIC_CATEGORIES}")
            for category, subtypes in semantic_categories.items():
                preview = f"{subtypes[:5]}{'...' if len(subtypes) > 5 else ''}"
                click.echo(
                    cs.LANG_MSG_CATEGORY_FORMAT.format(
                        category=category, subtypes=preview, count=len(subtypes)
                    )
                )

            functions: list[str] = []
            classes: list[str] = []
            modules: list[str] = []
            calls: list[str] = []

            for category, subtypes in semantic_categories.items():
                for subtype in subtypes:
                    subtype_lower = subtype.lower()

                    if (
                        any(kw in subtype_lower for kw in cs.LANG_FUNCTION_KEYWORDS)
                        and "call" not in subtype_lower
                    ):
                        functions.append(subtype)

                    elif any(
                        kw in subtype_lower for kw in cs.LANG_CLASS_KEYWORDS
                    ) and not any(
                        kw in subtype_lower for kw in cs.LANG_EXCLUSION_KEYWORDS
                    ):
                        classes.append(subtype)

                    elif any(kw in subtype_lower for kw in cs.LANG_CALL_KEYWORDS):
                        calls.append(subtype)

                    elif any(kw in subtype_lower for kw in cs.LANG_MODULE_KEYWORDS):
                        modules.append(subtype)

            root_nodes = [
                node["type"]
                for node in node_types
                if isinstance(node, dict) and node.get("root")
            ]
            modules.extend(root_nodes)

            functions = list(set(functions))
            classes = list(set(classes))
            modules = list(set(modules))
            calls = list(set(calls))

            click.echo(f"🎯 {cs.LANG_MSG_MAPPED_CATEGORIES}")
            click.echo(cs.LANG_MSG_FUNCTIONS.format(nodes=functions))
            click.echo(cs.LANG_MSG_CLASSES.format(nodes=classes))
            click.echo(cs.LANG_MSG_MODULES.format(nodes=modules))
            click.echo(cs.LANG_MSG_CALLS.format(nodes=calls))

        except Exception as e:
            logger.error(cs.LANG_ERR_PARSE_NODE_TYPES.format(error=e))
            click.echo(cs.LANG_ERR_PARSE_NODE_TYPES.format(error=e))
            functions = [cs.LANG_FALLBACK_METHOD_NODE]
            classes = list(cs.LANG_DEFAULT_CLASS_NODES)
            modules = list(cs.LANG_DEFAULT_MODULE_NODES)
            calls = list(cs.LANG_DEFAULT_CALL_NODES)

    new_language_spec = LanguageSpec(
        language=language_name,
        file_extensions=tuple(file_extension),
        function_node_types=tuple(functions),
        class_node_types=tuple(classes),
        module_node_types=tuple(modules),
        call_node_types=tuple(calls),
    )

    config_entry = f"""    "{language_name}": LanguageSpec(
        language="{new_language_spec.language}",
        file_extensions={new_language_spec.file_extensions},
        function_node_types={new_language_spec.function_node_types},
        class_node_types={new_language_spec.class_node_types},
        module_node_types={new_language_spec.module_node_types},
        call_node_types={new_language_spec.call_node_types},
    ),"""

    try:
        config_content = pathlib.Path(cs.LANG_CONFIG_FILE).read_text()

        closing_brace_pos = config_content.rfind("}")
        if closing_brace_pos != -1:
            new_content = (
                config_content[:closing_brace_pos]
                + config_entry
                + "\n"
                + config_content[closing_brace_pos:]
            )

            with open(cs.LANG_CONFIG_FILE, "w") as f:
                f.write(new_content)

            click.echo(f"✅ {cs.LANG_MSG_LANG_ADDED.format(name=language_name)}")
            click.echo(
                f"📝 {cs.LANG_MSG_UPDATED_CONFIG.format(path=cs.LANG_CONFIG_FILE)}"
            )

            click.echo()
            click.echo(
                click.style(
                    f"📋 {cs.LANG_MSG_REVIEW_PROMPT}", bold=True, fg=cs.Color.YELLOW
                )
            )
            click.echo(cs.LANG_MSG_REVIEW_HINT)
            click.echo(cs.LANG_MSG_EDIT_HINT.format(path=cs.LANG_CONFIG_FILE))
            click.echo()
            click.echo(f"🎯 {cs.LANG_MSG_COMMON_ISSUES}")
            click.echo(f"   • {cs.LANG_MSG_ISSUE_MISCLASSIFIED.strip()}")
            click.echo(f"   • {cs.LANG_MSG_ISSUE_MISSING.strip()}")
            click.echo(f"   • {cs.LANG_MSG_ISSUE_CLASS_TYPES.strip()}")
            click.echo(f"   • {cs.LANG_MSG_ISSUE_CALL_TYPES.strip()}")
            click.echo()
            click.echo(f"💡 {cs.LANG_MSG_LIST_HINT}")
        else:
            raise ValueError(cs.LANG_ERR_CONFIG_NOT_FOUND)

    except Exception as e:
        logger.error(cs.LANG_ERR_UPDATE_CONFIG.format(error=e))
        click.echo(f"❌ {cs.LANG_ERR_UPDATE_CONFIG.format(error=e)}")
        click.echo(click.style(cs.LANG_FALLBACK_MANUAL_ADD, bold=True))
        click.echo(click.style(config_entry, fg=cs.Color.GREEN))


@cli.command(help="List all currently configured languages.")
def list_languages() -> None:
    console = Console()

    table = Table(
        title=f"📋 {cs.LANG_TABLE_TITLE}",
        show_header=True,
        header_style=f"bold {cs.Color.MAGENTA}",
    )
    table.add_column(cs.LANG_TABLE_COL_LANGUAGE, style=cs.Color.CYAN, width=12)
    table.add_column(cs.LANG_TABLE_COL_EXTENSIONS, style=cs.Color.GREEN, width=20)
    table.add_column(cs.LANG_TABLE_COL_FUNCTION_TYPES, style=cs.Color.YELLOW, width=30)
    table.add_column(cs.LANG_TABLE_COL_CLASS_TYPES, style="blue", width=35)
    table.add_column(cs.LANG_TABLE_COL_CALL_TYPES, style=cs.Color.RED, width=30)

    for lang_name, config in LANGUAGE_SPECS.items():
        extensions = ", ".join(config.file_extensions)
        function_types = (
            ", ".join(config.function_node_types)
            if config.function_node_types
            else cs.LANG_TABLE_PLACEHOLDER
        )
        class_types = (
            ", ".join(config.class_node_types)
            if config.class_node_types
            else cs.LANG_TABLE_PLACEHOLDER
        )
        call_types = (
            ", ".join(config.call_node_types)
            if config.call_node_types
            else cs.LANG_TABLE_PLACEHOLDER
        )

        table.add_row(lang_name, extensions, function_types, class_types, call_types)

    console.print(table)


@cli.command(help="Remove a language from the project.")
@click.argument("language_name")
@click.option(
    "--keep-submodule", is_flag=True, help="Keep the git submodule (default: remove it)"
)
def remove_language(language_name: str, keep_submodule: bool = False) -> None:
    if language_name not in LANGUAGE_SPECS:
        available_langs = ", ".join(LANGUAGE_SPECS.keys())
        click.echo(f"❌ {cs.LANG_MSG_LANG_NOT_FOUND.format(name=language_name)}")
        click.echo(f"📋 {cs.LANG_MSG_AVAILABLE_LANGS.format(langs=available_langs)}")
        return

    try:
        original_content = pathlib.Path(cs.LANG_CONFIG_FILE).read_text()
        pattern = rf'    "{language_name}": LanguageSpec\([\s\S]*?\),\n'
        new_content = re.sub(pattern, "", original_content)

        dmp_obj = dmp.diff_match_patch()
        patches = dmp_obj.patch_make(original_content, new_content)
        result, _ = dmp_obj.patch_apply(patches, original_content)

        with open(cs.LANG_CONFIG_FILE, "w") as f:
            f.write(result)

        click.echo(f"✅ {cs.LANG_MSG_REMOVED_FROM_CONFIG.format(name=language_name)}")

    except Exception as e:
        logger.error(cs.LANG_ERR_REMOVE_CONFIG.format(error=e))
        click.echo(f"❌ {cs.LANG_ERR_REMOVE_CONFIG.format(error=e)}")
        return

    if not keep_submodule:
        submodule_path = (
            f"{cs.LANG_GRAMMARS_DIR}/{cs.TREE_SITTER_PREFIX}{language_name}"
        )
        if os.path.exists(submodule_path):
            try:
                click.echo(
                    f"🔄 {cs.LANG_MSG_REMOVING_SUBMODULE.format(path=submodule_path)}"
                )
                subprocess.run(
                    ["git", "submodule", "deinit", "-f", submodule_path],
                    check=True,
                    capture_output=True,
                )
                subprocess.run(
                    ["git", "rm", "-f", submodule_path], check=True, capture_output=True
                )

                modules_path = cs.LANG_GIT_MODULES_PATH.format(path=submodule_path)
                if os.path.exists(modules_path):
                    shutil.rmtree(modules_path)
                    click.echo(
                        f"🧹 {cs.LANG_MSG_CLEANED_MODULES.format(path=modules_path)}"
                    )

                click.echo(
                    f"🗑️  {cs.LANG_MSG_SUBMODULE_REMOVED.format(path=submodule_path)}"
                )
            except subprocess.CalledProcessError as e:
                logger.error(cs.LANG_ERR_REMOVE_SUBMODULE.format(error=e))
                click.echo(f"❌ {cs.LANG_ERR_REMOVE_SUBMODULE.format(error=e)}")
                click.echo(f"💡 {cs.LANG_ERR_MANUAL_REMOVE_HINT}")
                click.echo(f"   git submodule deinit -f {submodule_path}")
                click.echo(f"   git rm -f {submodule_path}")
        else:
            click.echo(f"ℹ️  {cs.LANG_MSG_NO_SUBMODULE.format(path=submodule_path)}")
    else:
        click.echo(f"ℹ️  {cs.LANG_MSG_KEEPING_SUBMODULE}")

    click.echo(f"🎉 {cs.LANG_MSG_LANG_REMOVED.format(name=language_name)}")


@cli.command(help="Clean up orphaned git modules that weren't properly removed.")
def cleanup_orphaned_modules() -> None:
    modules_dir = f".git/modules/{cs.LANG_GRAMMARS_DIR}"
    if not os.path.exists(modules_dir):
        click.echo(f"📂 {cs.LANG_MSG_NO_MODULES_DIR}")
        return

    gitmodules_submodules: set[str] = set()
    try:
        with open(".gitmodules") as f:
            content = f.read()
            paths = re.findall(cs.LANG_GITMODULES_REGEX, content)
            gitmodules_submodules = set(paths)
    except FileNotFoundError:
        click.echo(f"📄 {cs.LANG_MSG_NO_GITMODULES}")

    orphaned = []
    for item in os.listdir(modules_dir):
        module_path = f"{cs.LANG_GRAMMARS_DIR}/{item}"
        if module_path not in gitmodules_submodules:
            orphaned.append(item)

    if not orphaned:
        click.echo(f"✨ {cs.LANG_MSG_NO_ORPHANS}")
        return

    click.echo(
        f"🔍 {cs.LANG_MSG_FOUND_ORPHANS.format(count=len(orphaned), modules=', '.join(orphaned))}"
    )

    if click.confirm(cs.LANG_PROMPT_REMOVE_ORPHANS):
        for module in orphaned:
            module_path = os.path.join(modules_dir, module)
            shutil.rmtree(module_path)
            click.echo(f"🗑️  {cs.LANG_MSG_REMOVED_ORPHAN.format(module=module)}")
        click.echo(f"🎉 {cs.LANG_MSG_CLEANUP_COMPLETE}")
    else:
        click.echo(f"❌ {cs.LANG_MSG_CLEANUP_CANCELLED}")


if __name__ == "__main__":
    cli()
