from __future__ import annotations

import ast
import io
import json
import os
import pathlib
import re
import shutil
import stat
import subprocess
import sys
import tokenize
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from typing import NamedTuple, NoReturn

import click
from rich.console import Console

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
    # False when the submodule was already in the index and only re-added: a
    # later failure must not roll back a grammar this run did not introduce.
    newly_added: bool = True


def _package_dir() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parent.parent


def _source_checkout_root() -> pathlib.Path | None:
    # Found from the running package, never from the cwd: an installed cgr run
    # inside someone's project must not take that project for the checkout
    # (issue #2422). The project name rules out a repo that merely vendors
    # the package.
    root = _package_dir().parent
    if not (root / cs.LANG_CONFIG_FILE).is_file():
        return None
    if not (root / cs.LANG_GIT_DIR).exists():
        return None
    try:
        with open(root / cs.PYPROJECT_PATH, "rb") as f:
            project = tomllib.load(f).get(cs.TOML_KEY_PROJECT)
    except (OSError, tomllib.TOMLDecodeError):
        return None
    if not isinstance(project, dict):
        return None
    if project.get(cs.LANG_TOML_KEY_NAME) != cs.PACKAGE_NAME:
        return None
    return root


def _error(message: str) -> None:
    click.secho(f"Error: {message}", fg=cs.Color.RED, err=True)


def _fail(message: str) -> NoReturn:
    _error(message)
    sys.exit(1)


def _require_source_checkout() -> pathlib.Path:
    if (root := _source_checkout_root()) is not None:
        return root
    _fail(
        cs.LANG_ERR_NOT_SOURCE_CHECKOUT.format(
            command=click.get_current_context().info_name,
            package_dir=_package_dir(),
            repo_url=cs.LANG_REPO_URL,
            issues_url=cs.LANG_ISSUES_URL,
        )
    )


def _run_git(root: pathlib.Path, *args: str) -> subprocess.CompletedProcess[str]:
    # A hook or wrapper may export another repository's GIT_DIR, which git
    # obeys over `cwd`; the checkout found above must be the one edited.
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in cs.GIT_LOCATION_ENV_VARS
    }
    return subprocess.run(
        ["git", *args],
        cwd=root,
        env=env,
        check=True,
        capture_output=True,
        text=True,
        encoding=cs.ENCODING_UTF8,
    )


def _git_dir(root: pathlib.Path) -> pathlib.Path:
    # In a linked worktree, or a checkout that is itself a submodule, `.git`
    # is a file naming the real git dir, relative to the checkout or not.
    dot_git = root / cs.LANG_GIT_DIR
    if not dot_git.is_file():
        return dot_git
    try:
        text = dot_git.read_text(encoding=cs.ENCODING_UTF8).strip()
    except (OSError, UnicodeDecodeError):
        return dot_git
    if not text.startswith(cs.LANG_GITFILE_PREFIX):
        return dot_git
    return root / text.removeprefix(cs.LANG_GITFILE_PREFIX).strip()


def _modules_path(root: pathlib.Path, path: str) -> pathlib.Path:
    # `modules/` is per worktree, as `git rev-parse --git-path modules`
    # reports: a linked worktree's grammars are not the main checkout's.
    return _git_dir(root) / cs.LANG_GIT_MODULES_DIR / path


def _retry_writable(func: Callable[[str], None], target: str, _: BaseException) -> None:
    # Git stores objects read-only, and Windows refuses to unlink a read-only
    # file, so a submodule's git dir only goes once the bit is cleared.
    os.chmod(target, stat.S_IWRITE)
    func(target)


def _rmtree(path: pathlib.Path | str) -> None:
    shutil.rmtree(path, onexc=_retry_writable)


def _add_git_submodule(
    grammar_url: str, grammar_path: str, root: pathlib.Path
) -> SubmoduleResult | None:
    try:
        click.echo(f"Adding submodule: {grammar_url}")
        _run_git(root, "submodule", "add", grammar_url, grammar_path)
        click.echo(f"OK Submodule added at: {grammar_path}")
        return SubmoduleResult(success=True, grammar_path=grammar_path)
    except subprocess.CalledProcessError as e:
        return _handle_submodule_error(e, grammar_url, grammar_path, root)


def _handle_submodule_error(
    error: subprocess.CalledProcessError,
    grammar_url: str,
    grammar_path: str,
    root: pathlib.Path,
) -> SubmoduleResult | None:
    error_output = error.stderr or str(error)

    if "already exists in the index" in error_output:
        return _reinstall_existing_submodule(grammar_url, grammar_path, root)

    if "does not exist" in error_output or "not found" in error_output:
        _error(cs.LANG_ERR_REPO_NOT_FOUND.format(url=grammar_url))
        click.echo(f"Hint: {cs.LANG_ERR_CUSTOM_URL_HINT}")
        return None

    _error(cs.LANG_ERR_GIT.format(error=error_output.strip()))
    return None


def _reinstall_existing_submodule(
    grammar_url: str, grammar_path: str, root: pathlib.Path
) -> SubmoduleResult | None:
    click.secho(
        f"Warning: {cs.LANG_MSG_SUBMODULE_EXISTS.format(path=grammar_path)}",
        fg=cs.Color.YELLOW,
    )
    try:
        click.echo(cs.LANG_MSG_REMOVING_ENTRY)
        _run_git(root, "submodule", "deinit", "-f", grammar_path)
        _run_git(root, "rm", "-f", grammar_path)

        modules_path = _modules_path(root, grammar_path)
        if modules_path.exists():
            _rmtree(modules_path)

        click.echo(cs.LANG_MSG_READDING_SUBMODULE)
        _run_git(root, "submodule", "add", "--force", grammar_url, grammar_path)
        click.echo(f"OK {cs.LANG_MSG_REINSTALL_SUCCESS.format(path=grammar_path)}")
        return SubmoduleResult(
            success=True, grammar_path=grammar_path, newly_added=False
        )
    except (subprocess.CalledProcessError, OSError) as reinstall_e:
        return _handle_reinstall_failure(reinstall_e, grammar_path, root)


def _handle_reinstall_failure(
    error: subprocess.CalledProcessError | OSError,
    grammar_path: str,
    root: pathlib.Path,
) -> None:
    error_msg = error.stderr if hasattr(error, "stderr") else str(error)
    _error(cs.LANG_ERR_REINSTALL_FAILED.format(error=error_msg))
    click.echo(f"Hint: {cs.LANG_ERR_MANUAL_REMOVE_HINT}")
    click.echo(f"   git -C {root} submodule deinit -f {grammar_path}")
    click.echo(f"   git -C {root} rm -f {grammar_path}")
    click.echo(f"   rm -rf {_modules_path(root, grammar_path)}")


def _parse_tree_sitter_json(
    json_path: str, grammar_dir_name: str, language_name: str | None
) -> LanguageInfo | None:
    if not os.path.exists(json_path):
        return None

    with open(json_path, encoding="utf-8") as f:
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


def _subtype_bucket(
    subtype_lower: str,
    functions: list[str],
    classes: list[str],
    calls: list[str],
    modules: list[str],
) -> list[str] | None:
    # First keyword family that claims the subtype, in priority order.
    if (
        any(kw in subtype_lower for kw in cs.LANG_FUNCTION_KEYWORDS)
        and cs.LANG_CALL_KEYWORD_EXCLUDE not in subtype_lower
    ):
        return functions
    if any(kw in subtype_lower for kw in cs.LANG_CLASS_KEYWORDS) and all(
        kw not in subtype_lower for kw in cs.LANG_EXCLUSION_KEYWORDS
    ):
        return classes
    if any(kw in subtype_lower for kw in cs.LANG_CALL_KEYWORDS):
        return calls
    if any(kw in subtype_lower for kw in cs.LANG_MODULE_KEYWORDS):
        return modules
    return None


def _categorize_node_types(
    semantic_categories: dict[str, list[str]], node_types: list[dict]
) -> NodeCategories:
    functions: list[str] = []
    classes: list[str] = []
    modules: list[str] = []
    calls: list[str] = []

    for subtypes in semantic_categories.values():
        for subtype in subtypes:
            bucket = _subtype_bucket(
                subtype.lower(), functions, classes, calls, modules
            )
            if bucket is not None:
                bucket.append(subtype)

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


def _collect_type_names(obj: dict | list, out: set[str]) -> None:
    # Every string `type` value anywhere in a node-types.json structure.
    children = obj.values() if isinstance(obj, dict) else obj
    if isinstance(obj, dict) and isinstance(obj.get("type"), str):
        out.add(obj["type"])
    for child in children:
        if isinstance(child, dict | list):
            _collect_type_names(child, out)


def _parse_node_types_file(node_types_path: str) -> NodeCategories | None:
    try:
        with open(node_types_path, encoding="utf-8") as f:
            node_types = json.load(f)

        all_node_names: set[str] = set()
        _collect_type_names(node_types, all_node_names)

        semantic_categories = _extract_semantic_categories(node_types)

        click.echo(
            f"Stats: {cs.LANG_MSG_FOUND_NODE_TYPES.format(count=len(all_node_names))}"
        )
        click.echo(f"Tree: {cs.LANG_MSG_SEMANTIC_CATEGORIES}")

        for category, subtypes in semantic_categories.items():
            preview = f"{subtypes[:5]}{cs.LANG_ELLIPSIS if len(subtypes) > 5 else ''}"
            click.echo(
                cs.LANG_MSG_CATEGORY_FORMAT.format(
                    category=category, subtypes=preview, count=len(subtypes)
                )
            )

        categories = _categorize_node_types(semantic_categories, node_types)

        click.echo(f"Target: {cs.LANG_MSG_MAPPED_CATEGORIES}")
        click.echo(cs.LANG_MSG_FUNCTIONS.format(nodes=categories.functions))
        click.echo(cs.LANG_MSG_CLASSES.format(nodes=categories.classes))
        click.echo(cs.LANG_MSG_MODULES.format(nodes=categories.modules))
        click.echo(cs.LANG_MSG_CALLS.format(nodes=categories.calls))

        return categories

    except Exception as e:
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


def _update_config_file(
    config_file: pathlib.Path, language_name: str, spec: LanguageSpec
) -> bool:
    config_entry = f"""    "{language_name}": LanguageSpec(
        language="{spec.language}",
        file_extensions={spec.file_extensions},
        function_node_types={spec.function_node_types},
        class_node_types={spec.class_node_types},
        module_node_types={spec.module_node_types},
        call_node_types={spec.call_node_types},
    ),"""

    try:
        return _write_language_config(config_file, config_entry, language_name)
    except Exception as e:
        _error(
            cs.LANG_ERR_UPDATE_CONFIG.format(
                name=language_name, path=config_file, error=e
            )
        )
        return False


def _read_config_text(config_file: pathlib.Path) -> tuple[str, str]:
    raw = config_file.read_bytes()
    newline = "\r\n" if b"\r\n" in raw else "\n"
    content = raw.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
    return content, newline


def _write_config_atomically(
    config_file: pathlib.Path, new_content: str, newline: str
) -> None:
    temp_path = f"{config_file}{cs.LANG_CONFIG_TMP_SUFFIX}"
    try:
        with open(temp_path, "w", encoding="utf-8", newline=newline) as f:
            f.write(new_content)
        os.replace(temp_path, config_file)
    except Exception:
        pathlib.Path(temp_path).unlink(missing_ok=True)
        raise


def _specs_assignment(node: ast.stmt) -> tuple[list[ast.expr], ast.expr | None]:
    if isinstance(node, ast.Assign):
        return node.targets, node.value
    if isinstance(node, ast.AnnAssign):
        return [node.target], node.value
    return [], None


def _specs_dict(config_content: str) -> ast.Dict:
    try:
        module = ast.parse(config_content)
    except SyntaxError as exc:
        raise ValueError(cs.LANG_ERR_CONFIG_NOT_FOUND) from exc
    found: ast.Dict | None = None
    for node in module.body:
        targets, value = _specs_assignment(node)
        is_specs = any(
            isinstance(target, ast.Name) and target.id == cs.LANG_SPECS_NAME
            for target in targets
        )
        if is_specs and isinstance(value, ast.Dict):
            found = value
    if found is None:
        raise ValueError(cs.LANG_ERR_CONFIG_NOT_FOUND)
    return found


def _content_offset(config_content: str, lineno: int, byte_col: int) -> int:
    lines = config_content.split("\n")
    prefix = sum(len(line) + 1 for line in lines[: lineno - 1])
    line = lines[lineno - 1]
    return prefix + len(line.encode("utf-8")[:byte_col].decode("utf-8"))


def _specs_dict_span(config_content: str) -> tuple[int, int]:
    dict_node = _specs_dict(config_content)
    end_lineno = dict_node.end_lineno
    end_col_offset = dict_node.end_col_offset
    if end_lineno is None or end_col_offset is None:
        raise ValueError(cs.LANG_ERR_CONFIG_NOT_FOUND)
    start = _content_offset(config_content, dict_node.lineno, dict_node.col_offset)
    end = _content_offset(config_content, end_lineno, end_col_offset - 1)
    return start, end


def _entry_key_matches(key: ast.expr, language_name: str) -> bool:
    if isinstance(key, ast.Constant) and key.value == language_name:
        return True
    try:
        member = cs.SupportedLanguage(language_name).name
    except ValueError:
        return False
    return isinstance(key, ast.Attribute) and ast.unparse(
        key
    ) == cs.LANG_ENUM_KEY_TEMPLATE.format(member=member)


class _TokenScanner:
    def __init__(self, config_content: str) -> None:
        self.content = config_content
        self.tokens = list(
            tokenize.generate_tokens(io.StringIO(config_content).readline)
        )
        offsets = [0]
        for line in config_content.split("\n"):
            offsets.append(offsets[-1] + len(line) + 1)
        self._line_offsets = offsets

    def offset(self, position: tuple[int, int]) -> int:
        row, col = position
        return self._line_offsets[row - 1] + col

    def index_at_or_after(self, offset: int) -> int:
        for i, token in enumerate(self.tokens):
            if self.offset(token.start) >= offset:
                return i
        return len(self.tokens)

    def _seek_op(self, i: int) -> int:
        while i < len(self.tokens) and self.tokens[i].type in (
            tokenize.NL,
            tokenize.COMMENT,
            tokenize.INDENT,
            tokenize.DEDENT,
        ):
            i += 1
        return i

    def extend_start(self, start: int, key_end: int) -> int:
        wrap = 0
        i = self.index_at_or_after(key_end)
        while i < len(self.tokens):
            token = self.tokens[i]
            if token.type == tokenize.OP and token.string == ":":
                break
            if token.type == tokenize.OP and token.string == ")":
                wrap += 1
            i += 1
        i = self.index_at_or_after(start) - 1
        while wrap and i >= 0:
            token = self.tokens[i]
            if token.type in (
                tokenize.NL,
                tokenize.COMMENT,
                tokenize.INDENT,
                tokenize.DEDENT,
            ):
                i -= 1
            elif token.type == tokenize.OP and token.string == "(":
                start = self.offset(token.start)
                wrap -= 1
                i -= 1
            else:
                break
        return start

    def extend_end(self, end: int, *, own_line: bool) -> int:
        i = self.index_at_or_after(end)
        probe = self._seek_op(i)
        while (
            probe < len(self.tokens)
            and self.tokens[probe].type == tokenize.OP
            and self.tokens[probe].string == ")"
        ):
            end = self.offset(self.tokens[probe].end)
            i = probe + 1
            probe = self._seek_op(i)
        if (
            probe < len(self.tokens)
            and self.tokens[probe].type == tokenize.OP
            and self.tokens[probe].string == ","
        ):
            end = self.offset(self.tokens[probe].end)
            i = probe + 1
        if not own_line:
            return end
        if i < len(self.tokens) and self.tokens[i].type == tokenize.COMMENT:
            end = self.offset(self.tokens[i].end)
            i += 1
        if i < len(self.tokens) and self.tokens[i].type in (
            tokenize.NL,
            tokenize.NEWLINE,
        ):
            end = self.offset(self.tokens[i].end)
        return end


def _specs_entry_spans(
    config_content: str, language_name: str
) -> list[tuple[int, int]]:
    dict_node = _specs_dict(config_content)
    scanner = _TokenScanner(config_content)
    spans: list[tuple[int, int]] = []
    for key, value in zip(dict_node.keys, dict_node.values, strict=True):
        if key is None or not _entry_key_matches(key, language_name):
            continue
        end_lineno = value.end_lineno
        end_col_offset = value.end_col_offset
        key_end_lineno = key.end_lineno
        key_end_col_offset = key.end_col_offset
        if (
            end_lineno is None
            or end_col_offset is None
            or key_end_lineno is None
            or key_end_col_offset is None
        ):
            raise ValueError(cs.LANG_ERR_CONFIG_NOT_FOUND)
        start = _content_offset(config_content, key.lineno, key.col_offset)
        key_end = _content_offset(config_content, key_end_lineno, key_end_col_offset)
        start = scanner.extend_start(start, key_end)
        line_start = config_content.rfind("\n", 0, start) + 1
        own_line = not config_content[line_start:start].strip()
        if own_line:
            start = line_start
        end = _content_offset(config_content, end_lineno, end_col_offset)
        spans.append((start, scanner.extend_end(end, own_line=own_line)))
    if not spans:
        raise ValueError(cs.LANG_ERR_ENTRY_NOT_IN_CONFIG.format(name=language_name))
    return spans


def _write_language_config(
    config_file: pathlib.Path, config_entry: str, language_name: str
) -> bool:
    config_content, newline = _read_config_text(config_file)
    closing_brace_pos = _specs_dict_span(config_content)[1]

    scanner = _TokenScanner(config_content)
    last = None
    for token in scanner.tokens:
        if token.type in (
            tokenize.COMMENT,
            tokenize.NL,
            tokenize.NEWLINE,
            tokenize.INDENT,
            tokenize.DEDENT,
            tokenize.ENCODING,
            tokenize.ENDMARKER,
        ):
            continue
        if scanner.offset(token.start) >= closing_brace_pos:
            break
        last = token
    if last is not None and not (
        last.type == tokenize.OP and last.string in ("{", ",")
    ):
        comma_pos = scanner.offset(last.end)
        config_content = config_content[:comma_pos] + "," + config_content[comma_pos:]
        closing_brace_pos += 1

    new_content = (
        config_content[:closing_brace_pos]
        + config_entry
        + "\n"
        + config_content[closing_brace_pos:]
    )

    compile(new_content, str(config_file), "exec")

    _write_config_atomically(config_file, new_content, newline)

    click.echo(f"OK {cs.LANG_MSG_LANG_ADDED.format(name=language_name)}")
    click.echo(f"Note: {cs.LANG_MSG_UPDATED_CONFIG.format(path=config_file)}")
    _show_review_hints(config_file)
    return True


def _show_review_hints(config_file: pathlib.Path) -> None:
    click.echo()
    click.echo(
        click.style(
            f"Review: {cs.LANG_MSG_REVIEW_PROMPT}", bold=True, fg=cs.Color.YELLOW
        )
    )
    click.echo(cs.LANG_MSG_REVIEW_HINT)
    click.echo(cs.LANG_MSG_EDIT_HINT.format(path=config_file))
    click.echo()
    click.echo(f"Target: {cs.LANG_MSG_COMMON_ISSUES}")
    click.echo(f"   • {cs.LANG_MSG_ISSUE_MISCLASSIFIED.strip()}")
    click.echo(f"   • {cs.LANG_MSG_ISSUE_MISSING.strip()}")
    click.echo(f"   • {cs.LANG_MSG_ISSUE_CLASS_TYPES.strip()}")
    click.echo(f"   • {cs.LANG_MSG_ISSUE_CALL_TYPES.strip()}")
    click.echo()
    click.echo(f"Hint: {cs.LANG_MSG_LIST_HINT}")


@click.group(
    help=ch.CMD_LANGUAGE_GROUP,
    short_help=ch.CMD_LANGUAGE_GROUP,
    epilog=ch.EPILOG_LANGUAGE,
    no_args_is_help=True,
)
def cli() -> None:
    # A click group: the subcommands registered on it do the work.
    pass


@cli.command(
    help=ch.CMD_LANGUAGE_ADD_HELP,
    short_help=ch.CMD_LANGUAGE_ADD,
    epilog=ch.EXAMPLES_LANGUAGE_ADD,
)
@click.argument("language_name", required=False)
@click.option(
    "--grammar-url",
    help=ch.HELP_GRAMMAR_URL,
)
def add_grammar(
    language_name: str | None = None, grammar_url: str | None = None
) -> None:
    root = _require_source_checkout()

    if not language_name and not grammar_url:
        language_name = click.prompt(cs.LANG_PROMPT_LANGUAGE_NAME)

    if not grammar_url:
        if not language_name:
            _fail(cs.LANG_ERR_MISSING_ARGS)
        grammar_url = cs.LANG_DEFAULT_GRAMMAR_URL.format(name=language_name)
        click.echo(f"Search: {cs.LANG_MSG_USING_DEFAULT_URL.format(url=grammar_url)}")

    if grammar_url and cs.LANG_TREE_SITTER_URL_MARKER not in grammar_url:
        click.secho(
            f"Warning: {cs.LANG_MSG_CUSTOM_URL_WARNING}",
            fg=cs.Color.YELLOW,
            bold=True,
        )
        if not click.confirm(cs.LANG_PROMPT_CONTINUE):
            return

    grammars_dir = root / cs.LANG_GRAMMARS_DIR
    grammars_dir_existed = grammars_dir.exists()
    gitmodules_existed = (root / cs.LANG_GITMODULES_FILE).exists()
    grammars_dir.mkdir(exist_ok=True)

    grammar_dir_name = os.path.basename(grammar_url).removesuffix(cs.LANG_GIT_SUFFIX)
    grammar_path = f"{cs.LANG_GRAMMARS_DIR}/{grammar_dir_name}"

    result = _add_git_submodule(grammar_url, grammar_path, root)
    registered = False
    try:
        if result is not None:
            registered = _register_language(
                root, grammar_path, grammar_dir_name, language_name
            )
    finally:
        # Also on an abort at a prompt: a grammar the config never learned
        # about is dead weight staged in the checkout.
        if result is not None and result.newly_added and not registered:
            _discard_added_submodule(
                root,
                grammar_path,
                language_name or grammar_dir_name,
                gitmodules_existed=gitmodules_existed,
            )
        if not grammars_dir_existed:
            _remove_if_empty(grammars_dir)
    if not registered:
        sys.exit(1)


def _register_language(
    root: pathlib.Path,
    grammar_path: str,
    grammar_dir_name: str,
    language_name: str | None,
) -> bool:
    grammar_dir = root / grammar_path
    tree_sitter_json_path = str(grammar_dir / cs.LANG_TREE_SITTER_JSON)

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

    categories = _resolve_node_categories(str(grammar_dir), language_name)
    new_language_spec = LanguageSpec(
        language=language_name,
        file_extensions=tuple(file_extension),
        function_node_types=tuple(categories.functions),
        class_node_types=tuple(categories.classes),
        module_node_types=tuple(categories.modules),
        call_node_types=tuple(categories.calls),
    )

    return _update_config_file(
        root / cs.LANG_CONFIG_FILE, language_name, new_language_spec
    )


def _discard_added_submodule(
    root: pathlib.Path,
    grammar_path: str,
    language_name: str,
    *,
    gitmodules_existed: bool,
) -> None:
    click.secho(cs.LANG_MSG_ROLLING_BACK.format(name=language_name), fg=cs.Color.YELLOW)
    removed = _remove_language_submodule(root, grammar_path)
    if not removed or gitmodules_existed:
        return
    # `git rm` of the only submodule leaves behind the empty .gitmodules that
    # the add created, staged in the index.
    try:
        _run_git(root, "rm", "-q", "-f", "--ignore-unmatch", cs.LANG_GITMODULES_FILE)
    except subprocess.CalledProcessError as e:
        _error(cs.LANG_ERR_REMOVE_SUBMODULE.format(error=e.stderr or e))


def _remove_if_empty(directory: pathlib.Path) -> None:
    if directory.is_dir() and not any(directory.iterdir()):
        directory.rmdir()


def _resolve_node_categories(grammar_path: str, language_name: str) -> NodeCategories:
    node_types_path = _find_node_types_path(grammar_path, language_name)
    if not node_types_path:
        click.echo(cs.LANG_ERR_NODE_TYPES_WARNING.format(name=language_name))
        return _prompt_for_node_categories()
    if categories := _parse_node_types_file(node_types_path):
        return categories
    return NodeCategories(
        functions=[cs.LANG_FALLBACK_METHOD_NODE],
        classes=list(cs.LANG_DEFAULT_CLASS_NODES),
        modules=list(cs.LANG_DEFAULT_MODULE_NODES),
        calls=list(cs.LANG_DEFAULT_CALL_NODES),
    )


@cli.command(help=ch.CMD_LANGUAGE_LIST, short_help=ch.CMD_LANGUAGE_LIST)
@click.option("--verbose", "-v", is_flag=True, help=ch.HELP_LANGUAGE_LIST_VERBOSE)
def list_languages(verbose: bool = False) -> None:
    # Deferred: the catalog loads the parsers package, which every other
    # `cgr` invocation must not pay for at start-up.
    from .language_catalog import print_language_catalog

    print_language_catalog(Console(), verbose=verbose)


@cli.command(
    help=ch.CMD_LANGUAGE_REMOVE_HELP,
    short_help=ch.CMD_LANGUAGE_REMOVE,
    epilog=ch.EXAMPLES_LANGUAGE_REMOVE,
)
@click.argument("language_name")
@click.option("--keep-submodule", is_flag=True, help=ch.HELP_KEEP_SUBMODULE)
def remove_language(language_name: str, keep_submodule: bool = False) -> None:
    root = _require_source_checkout()

    if language_name not in LANGUAGE_SPECS:
        available_langs = ", ".join(LANGUAGE_SPECS.keys())
        _error(cs.LANG_MSG_LANG_NOT_FOUND.format(name=language_name))
        click.echo(f"List: {cs.LANG_MSG_AVAILABLE_LANGS.format(langs=available_langs)}")
        sys.exit(1)

    if not _remove_language_from_config(root / cs.LANG_CONFIG_FILE, language_name):
        sys.exit(1)

    if keep_submodule:
        click.echo(f"Info: {cs.LANG_MSG_KEEPING_SUBMODULE}")
    else:
        submodule_path = (
            f"{cs.LANG_GRAMMARS_DIR}/{cs.TREE_SITTER_PREFIX}{language_name}"
        )
        if not (root / submodule_path).exists():
            click.echo(f"Info: {cs.LANG_MSG_NO_SUBMODULE.format(path=submodule_path)}")
        elif not _remove_language_submodule(root, submodule_path):
            sys.exit(1)

    click.echo(f"Done: {cs.LANG_MSG_LANG_REMOVED.format(name=language_name)}")


def _remove_language_from_config(config_file: pathlib.Path, language_name: str) -> bool:
    try:
        original_content, newline = _read_config_text(config_file)
        spans = _specs_entry_spans(original_content, language_name)
        new_content = original_content
        for entry_start, entry_end in sorted(spans, reverse=True):
            new_content = new_content[:entry_start] + new_content[entry_end:]
        compile(new_content, str(config_file), "exec")

        _write_config_atomically(config_file, new_content, newline)

        click.echo(f"OK {cs.LANG_MSG_REMOVED_FROM_CONFIG.format(name=language_name)}")
        return True

    except Exception as e:
        _error(cs.LANG_ERR_REMOVE_CONFIG.format(error=e))
        return False


def _remove_language_submodule(root: pathlib.Path, submodule_path: str) -> bool:
    try:
        click.echo(
            f"Removing: {cs.LANG_MSG_REMOVING_SUBMODULE.format(path=submodule_path)}"
        )
        _run_git(root, "submodule", "deinit", "-f", submodule_path)
        _run_git(root, "rm", "-f", submodule_path)

        modules_path = _modules_path(root, submodule_path)
        if modules_path.exists():
            _rmtree(modules_path)
            click.echo(
                f"Cleaned: {cs.LANG_MSG_CLEANED_MODULES.format(path=modules_path)}"
            )

        click.echo(
            f"Deleted: {cs.LANG_MSG_SUBMODULE_REMOVED.format(path=submodule_path)}"
        )
        return True
    except subprocess.CalledProcessError as e:
        _error(cs.LANG_ERR_REMOVE_SUBMODULE.format(error=e.stderr or e))
        click.echo(f"Hint: {cs.LANG_ERR_MANUAL_REMOVE_HINT}")
        click.echo(f"   git -C {root} submodule deinit -f {submodule_path}")
        click.echo(f"   git -C {root} rm -f {submodule_path}")
        return False


@cli.command(help=ch.CMD_LANGUAGE_CLEANUP_HELP, short_help=ch.CMD_LANGUAGE_CLEANUP)
def cleanup_orphaned_modules() -> None:
    root = _require_source_checkout()
    modules_dir = _modules_path(root, cs.LANG_GRAMMARS_DIR)
    if not modules_dir.exists():
        click.echo(f"Info: {cs.LANG_MSG_NO_MODULES_DIR}")
        return

    gitmodules_submodules: set[str] = set()
    try:
        with open(root / cs.LANG_GITMODULES_FILE, encoding="utf-8") as f:
            content = f.read()
            paths = re.findall(cs.LANG_GITMODULES_REGEX, content)
            gitmodules_submodules = set(paths)
    except FileNotFoundError:
        click.echo(f"Info: {cs.LANG_MSG_NO_GITMODULES}")

    orphaned = []
    for item in os.listdir(modules_dir):
        module_path = f"{cs.LANG_GRAMMARS_DIR}/{item}"
        if module_path not in gitmodules_submodules:
            orphaned.append(item)

    if not orphaned:
        click.echo(f"Clean: {cs.LANG_MSG_NO_ORPHANS}")
        return

    click.echo(
        f"Search: {cs.LANG_MSG_FOUND_ORPHANS.format(count=len(orphaned), modules=', '.join(orphaned))}"
    )

    if click.confirm(cs.LANG_PROMPT_REMOVE_ORPHANS):
        for module in orphaned:
            module_path = os.path.join(modules_dir, module)
            _rmtree(module_path)
            click.echo(f"Deleted: {cs.LANG_MSG_REMOVED_ORPHAN.format(module=module)}")
        click.echo(f"Done: {cs.LANG_MSG_CLEANUP_COMPLETE}")
    else:
        click.echo(f"Cancelled: {cs.LANG_MSG_CLEANUP_CANCELLED}")


if __name__ == "__main__":
    cli()
