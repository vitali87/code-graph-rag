from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger

from .. import logs as ls
from ..constants import SEPARATOR_DOT

if TYPE_CHECKING:
    from tree_sitter import Node

    from ..language_spec import FQNSpec


def scoped_name_parts(
    start: Node | None, fqn_config: FQNSpec, module_qn: str, file_path: Path | None
) -> list[str]:
    """Names of the scope nodes from `start` outwards, outermost first.

    `start` is the first node considered: a definition's parent, or a scope
    node whose own name is wanted. The spec's `fold_scopes` may drop names
    the module already spells (a C# namespace mirroring the directory,
    issue #1629). Every walk that builds a qualified name goes through here,
    so the graph, the recovered-orphan path and `get_function_source` agree.
    """
    typed: list[tuple[str, str]] = []
    current = start
    while current is not None:
        if current.type in fqn_config.scope_node_types and (
            name := fqn_config.get_name(current)
        ):
            typed.append((current.type, name))
        current = current.parent
    typed.reverse()
    if fqn_config.fold_scopes is None:
        return [name for _node_type, name in typed]
    return fqn_config.fold_scopes(typed, module_qn, file_path)


def resolve_fqn_from_ast(
    func_node: Node,
    file_path: Path,
    repo_root: Path,
    project_name: str,
    fqn_config: FQNSpec,
) -> str | None:
    try:
        func_name = fqn_config.get_name(func_node)
        if not func_name:
            return None
        module_parts = fqn_config.file_to_module_parts(file_path, repo_root)
        module_qn = SEPARATOR_DOT.join([project_name, *module_parts])
        parts = scoped_name_parts(func_node.parent, fqn_config, module_qn, file_path)
        return SEPARATOR_DOT.join([module_qn, *parts, func_name])

    except Exception as e:
        logger.debug(ls.FQN_RESOLVE_FAILED, path=file_path, error=e)
        return None


def find_function_source_by_fqn(
    root_node: Node,
    target_fqn: str,
    file_path: Path,
    repo_root: Path,
    project_name: str,
    fqn_config: FQNSpec,
) -> str | None:
    from ..parsers.utils import safe_decode_text

    try:

        def walk(node: Node) -> str | None:
            if node.type in fqn_config.function_node_types:
                actual_fqn = resolve_fqn_from_ast(
                    node, file_path, repo_root, project_name, fqn_config
                )
                if actual_fqn == target_fqn:
                    return safe_decode_text(node)

            for child in node.children:
                result = walk(child)
                if result is not None:
                    return result
            return None

        return walk(root_node)

    except Exception as e:
        logger.debug(ls.FQN_FIND_FAILED, fqn=target_fqn, path=file_path, error=e)
        return None


def extract_function_fqns(
    root_node: Node,
    file_path: Path,
    repo_root: Path,
    project_name: str,
    fqn_config: FQNSpec,
) -> list[tuple[str, Node]]:
    functions: list[tuple[str, Node]] = []

    try:

        def walk(node: Node) -> None:
            if node.type in fqn_config.function_node_types:
                fqn = resolve_fqn_from_ast(
                    node, file_path, repo_root, project_name, fqn_config
                )
                if fqn:
                    functions.append((fqn, node))

            for child in node.children:
                walk(child)

        walk(root_node)

    except Exception as e:
        logger.debug(ls.FQN_EXTRACT_FAILED, path=file_path, error=e)

    return functions
