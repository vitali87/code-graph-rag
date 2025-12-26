from __future__ import annotations

import json
import os
import pathlib
import re
import shutil
import subprocess
from dataclasses import dataclass
from typing import NamedTuple

import click
import diff_match_patch as dmp
from loguru import logger
from rich.console import Console
from rich.table import Table

from .. import cli_help as ch
from .. import constants as cs
from ..language_spec import LANGUAGE_SPECS, LanguageSpec


class LanguageInfo(NamedTuple):
    name: str
    extensions: list[str]


class NodeCategories(NamedTuple):
    functions: list[str]
    classes: list[str]
    modules: list[str]
    calls: list[str]


@dataclass
class SubmoduleResult:
    success: bool
    grammar_path: str


def _add_git_submodule(grammar_url: str, grammar_path: str) -> SubmoduleResult | None:
    try:
        click.echo(f"🔄 {cs.LANG_MSG_ADDING_SUBMODULE.format(url=grammar_url)}")
        subprocess.run(
            ["git", "submodule", "add", grammar_url, grammar_path],
            check=True,
            capture_output=True,
            text=True,
        )
        click.echo(f"✅ {cs.LANG_MSG_SUBMODULE_SUCCESS.format(path=grammar_path)}")
        return SubmoduleResult(success=True, grammar_path=grammar_path)
    except subprocess.CalledProcessError as e:
        return _handle_submodule_error(e, grammar_url, grammar_path)


def _handle_submodule_error(
    error: subprocess.CalledProcessError, grammar_url: str, grammar_path: str
) -> SubmoduleResult | None:
    error_output = error.stderr or str(error)

    if "already exists in the index" in error_output:
        return _reinstall_existing_submodule(grammar_url, grammar_path)

    if "does not exist" in error_output or "not found" in error_output:
        logger.error(cs.LANG_ERR_REPO_NOT_FOUND.format(url=grammar_url))
        click.echo(f"❌ {cs.LANG_ERR_REPO_NOT_FOUND.format(url=grammar_url)}")
        click.echo(f"💡 {cs.LANG_ERR_CUSTOM_URL_HINT}")
        return None

    logger.error(cs.LANG_ERR_GIT.format(error=error_output))
    click.echo(f"❌ {cs.LANG_ERR_GIT.format(error=error_output)}")
    raise error


def _reinstall_existing_submodule(
    grammar_url: str, grammar_path: str
) -> SubmoduleResult | None:
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
        click.echo(f"✅ {cs.LANG_MSG_REINSTALL_SUCCESS.format(path=grammar_path)}")
        return SubmoduleResult(success=True, grammar_path=grammar_path)
    except (subprocess.CalledProcessError, OSError) as reinstall_e:
        return _handle_reinstall_failure(reinstall_e, grammar_path)


def _handle_reinstall_failure(
    error: subprocess.CalledProcessError | OSError, grammar_path: str
) -> None:
    error_msg = error.stderr if hasattr(error, "stderr") else str(error)
    logger.error(cs.LANG_ERR_REINSTALL_FAILED.format(error=error_msg))
    click.secho(
        f"❌ {cs.LANG_ERR_REINSTALL_FAILED.format(error=error_msg)}",
        fg=cs.Color.RED,
    )
    click.echo(f"💡 {cs.LANG_ERR_MANUAL_REMOVE_HINT}")
    click.echo(f"   git submodule deinit -f {grammar_path}")
    click.echo(f"   git rm -f {grammar_path}")
    click.echo(f"   rm -rf {cs.LANG_GIT_MODULES_PATH.format(path=grammar_path)}")


def _parse_tree_sitter_json(
    json_path: str, grammar_dir_name: str, language_name: str | None
) -> LanguageInfo | None:
    if not os.path.exists(json_path):
        return None

    with open(json_path) as f:
        config = json.load(f)

    if "grammars" not in config or len(config["grammars"]) == 0:
        return None

    grammar_info = config["grammars"][0]
    detected_name = grammar_info.get("name", grammar_dir_name)
    raw_extensions = grammar_info.get("file-types", [])
    extensions = [ext if ext.startswith(".") else f".{ext}" for ext in raw_extensions]

    name = language_name or detected_name

    click.echo(cs.LANG_MSG_AUTO_DETECTED_LANG.format(name=detected_name))
    click.echo(cs.LANG_MSG_USING_LANG_NAME.format(name=name))
    click.echo(cs.LANG_MSG_AUTO_DETECTED_EXT.format(extensions=extensions))

    return LanguageInfo(name=name, extensions=extensions)


def _prompt_for_language_info(language_name: str | None) -> LanguageInfo:
    if not language_name:
        language_name = click.prompt(cs.LANG_PROMPT_COMMON_NAME)
    extensions = [
        ext.strip() for ext in click.prompt(cs.LANG_PROMPT_EXTENSIONS).split(",")
    ]
    return LanguageInfo(name=language_name, extensions=extensions)


def _extract_semantic_categories(node_types_json: list[dict]) -> dict[str, list[str]]:
    categories: dict[str, list[str]] = {}

    for node in node_types_json:
        if isinstance(node, dict) and "type" in node and "subtypes" in node:
            subtypes = [
                subtype["type"] for subtype in node["subtypes"] if "type" in subtype
            ]
            categories.setdefault(node["type"], []).extend(subtypes)

    for category, values in categories.items():
        categories[category] = list(set(values))

    return categories


def _categorize_node_types(
    semantic_categories: dict[str, list[str]], node_types: list[dict]
) -> NodeCategories:
    functions: list[str] = []
    classes: list[str] = []
    modules: list[str] = []
    calls: list[str] = []

    for subtypes in semantic_categories.values():
        for subtype in subtypes:
            subtype_lower = subtype.lower()

            if (
                any(kw in subtype_lower for kw in cs.LANG_FUNCTION_KEYWORDS)
                and cs.LANG_CALL_KEYWORD_EXCLUDE not in subtype_lower
            ):
                functions.append(subtype)
            elif any(kw in subtype_lower for kw in cs.LANG_CLASS_KEYWORDS) and all(
                kw not in subtype_lower for kw in cs.LANG_EXCLUSION_KEYWORDS
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

    return NodeCategories(
        functions=list(set(functions)),
        classes=list(set(classes)),
        modules=list(set(modules)),
        calls=list(set(calls)),
    )


def _parse_node_types_file(node_types_path: str) -> NodeCategories | None:
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

        semantic_categories = _extract_semantic_categories(node_types)

        click.echo(
            f"📊 {cs.LANG_MSG_FOUND_NODE_TYPES.format(count=len(all_node_names))}"
        )
        click.echo(f"🌳 {cs.LANG_MSG_SEMANTIC_CATEGORIES}")

        for category, subtypes in semantic_categories.items():
            preview = f"{subtypes[:5]}{cs.LANG_ELLIPSIS if len(subtypes) > 5 else ''}"
            click.echo(
                cs.LANG_MSG_CATEGORY_FORMAT.format(
                    category=category, subtypes=preview, count=len(subtypes)
                )
            )

        categories = _categorize_node_types(semantic_categories, node_types)

        click.echo(f"🎯 {cs.LANG_MSG_MAPPED_CATEGORIES}")
        click.echo(cs.LANG_MSG_FUNCTIONS.format(nodes=categories.functions))
        click.echo(cs.LANG_MSG_CLASSES.format(nodes=categories.classes))
        click.echo(cs.LANG_MSG_MODULES.format(nodes=categories.modules))
        click.echo(cs.LANG_MSG_CALLS.format(nodes=categories.calls))

        return categories

    except Exception as e:
        logger.error(cs.LANG_ERR_PARSE_NODE_TYPES.format(error=e))
        click.echo(cs.LANG_ERR_PARSE_NODE_TYPES.format(error=e))
        return None


def _prompt_for_node_categories() -> NodeCategories:
    click.echo(cs.LANG_MSG_AVAILABLE_NODES)
    click.echo(cs.LANG_MSG_FUNCTIONS.format(nodes=list(cs.LANG_DEFAULT_FUNCTION_NODES)))
    click.echo(cs.LANG_MSG_CLASSES.format(nodes=list(cs.LANG_DEFAULT_CLASS_NODES)))

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
        node.strip() for node in click.prompt(cs.LANG_PROMPT_CALLS, type=str).split(",")
    ]

    return NodeCategories(functions, classes, modules, calls)


def _find_node_types_path(grammar_path: str, language_name: str) -> str | None:
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

    return next((path for path in possible_paths if os.path.exists(path)), None)


def _update_config_file(language_name: str, spec: LanguageSpec) -> bool:
    config_entry = f"""    "{language_name}": LanguageSpec(
        language="{spec.language}",
        file_extensions={spec.file_extensions},
        function_node_types={spec.function_node_types},
        class_node_types={spec.class_node_types},
        module_node_types={spec.module_node_types},
        call_node_types={spec.call_node_types},
    ),"""

    try:
        return _write_language_config(config_entry, language_name)
    except Exception as e:
        logger.error(cs.LANG_ERR_UPDATE_CONFIG.format(error=e))
        click.echo(f"❌ {cs.LANG_ERR_UPDATE_CONFIG.format(error=e)}")
        click.echo(click.style(cs.LANG_FALLBACK_MANUAL_ADD, bold=True))
        click.echo(click.style(config_entry, fg=cs.Color.GREEN))
        return False


def _write_language_config(config_entry: str, language_name: str) -> bool:
    config_content = pathlib.Path(cs.LANG_CONFIG_FILE).read_text()
    closing_brace_pos = config_content.rfind("}")

    if closing_brace_pos == -1:
        raise ValueError(cs.LANG_ERR_CONFIG_NOT_FOUND)

    new_content = (
        config_content[:closing_brace_pos]
        + config_entry
        + "\n"
        + config_content[closing_brace_pos:]
    )

    with open(cs.LANG_CONFIG_FILE, "w") as f:
        f.write(new_content)

    click.echo(f"✅ {cs.LANG_MSG_LANG_ADDED.format(name=language_name)}")
    click.echo(f"📝 {cs.LANG_MSG_UPDATED_CONFIG.format(path=cs.LANG_CONFIG_FILE)}")
    _show_review_hints()
    return True


def _show_review_hints() -> None:
    click.echo()
    click.echo(
        click.style(f"📋 {cs.LANG_MSG_REVIEW_PROMPT}", bold=True, fg=cs.Color.YELLOW)
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


@click.group(help=ch.CMD_LANGUAGE_GROUP)
def cli() -> None:
    pass


@cli.command(help=ch.CMD_LANGUAGE_ADD)
@click.argument("language_name", required=False)
@click.option(
    "--grammar-url",
    help=ch.HELP_GRAMMAR_URL,
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

    grammar_dir_name = os.path.basename(grammar_url).removesuffix(cs.LANG_GIT_SUFFIX)
    grammar_path = os.path.join(cs.LANG_GRAMMARS_DIR, grammar_dir_name)

    result = _add_git_submodule(grammar_url, grammar_path)
    if result is None:
        return

    tree_sitter_json_path = os.path.join(grammar_path, cs.LANG_TREE_SITTER_JSON)

    if lang_info := _parse_tree_sitter_json(
        tree_sitter_json_path, grammar_dir_name, language_name
    ):
        language_name = lang_info.name
        file_extension = lang_info.extensions
    else:
        click.echo(cs.LANG_ERR_TREE_SITTER_JSON_WARNING.format(path=grammar_path))
        info = _prompt_for_language_info(language_name)
        language_name = info.name
        file_extension = info.extensions

    assert language_name is not None

    if node_types_path := _find_node_types_path(grammar_path, language_name):
        if categories := _parse_node_types_file(node_types_path):
            functions = categories.functions
            classes = categories.classes
            modules = categories.modules
            calls = categories.calls
        else:
            functions = [cs.LANG_FALLBACK_METHOD_NODE]
            classes = list(cs.LANG_DEFAULT_CLASS_NODES)
            modules = list(cs.LANG_DEFAULT_MODULE_NODES)
            calls = list(cs.LANG_DEFAULT_CALL_NODES)
    else:
        click.echo(cs.LANG_ERR_NODE_TYPES_WARNING.format(name=language_name))
        categories = _prompt_for_node_categories()
        functions = categories.functions
        classes = categories.classes
        modules = categories.modules
        calls = categories.calls

    new_language_spec = LanguageSpec(
        language=language_name,
        file_extensions=tuple(file_extension),
        function_node_types=tuple(functions),
        class_node_types=tuple(classes),
        module_node_types=tuple(modules),
        call_node_types=tuple(calls),
    )

    _update_config_file(language_name, new_language_spec)


@cli.command(help=ch.CMD_LANGUAGE_LIST)
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
    table.add_column(cs.LANG_TABLE_COL_CLASS_TYPES, style=cs.Color.BLUE, width=35)
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


@cli.command(help=ch.CMD_LANGUAGE_REMOVE)
@click.argument("language_name")
@click.option("--keep-submodule", is_flag=True, help=ch.HELP_KEEP_SUBMODULE)
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


@cli.command(help=ch.CMD_LANGUAGE_CLEANUP)
def cleanup_orphaned_modules() -> None:
    modules_dir = f".git/modules/{cs.LANG_GRAMMARS_DIR}"
    if not os.path.exists(modules_dir):
        click.echo(f"📂 {cs.LANG_MSG_NO_MODULES_DIR}")
        return

    gitmodules_submodules: set[str] = set()
    try:
        with open(cs.LANG_GITMODULES_FILE) as f:
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
