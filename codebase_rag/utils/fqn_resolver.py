from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger

from ..constants import SEPARATOR_DOT

if TYPE_CHECKING:
    from tree_sitter import Node

    from ..language_config import FQNConfig
    from ..parsers.utils import safe_decode_text


def resolve_fqn_from_ast(
    func_node: "Node",
    file_path: Path,
    repo_root: Path,
    project_name: str,
    fqn_config: "FQNConfig",
) -> str | None:
    """Reconstruct FQN by walking up AST scopes using language config.

    Args:
        func_node: The function/method node from tree-sitter AST
        file_path: Path to the source file
        repo_root: Root path of the repository
        project_name: Name of the project (used as FQN prefix)
        fqn_config: Language-specific configuration for FQN resolution

    Returns:
        Fully qualified name string or None if extraction fails
    """
    try:
        parts = []

        func_name = fqn_config.get_name(func_node)
        if not func_name:
            return None
        parts.append(func_name)

        current = func_node.parent
        while current:
            if current.type in fqn_config.scope_node_types:
                scope_name = fqn_config.get_name(current)
                if scope_name:
                    parts.append(scope_name)
            current = current.parent

        parts.reverse()

        module_parts = fqn_config.file_to_module_parts(file_path, repo_root)
        full_parts = [project_name] + module_parts + parts
        return SEPARATOR_DOT.join(full_parts)

    except Exception as e:
        logger.debug(f"Failed to resolve FQN for node at {file_path}: {e}")
        return None


def find_function_source_by_fqn(
    root_node: "Node",
    target_fqn: str,
    file_path: Path,
    repo_root: Path,
    project_name: str,
    fqn_config: "FQNConfig",
) -> str | None:
    """Traverse AST to find function node matching target FQN.

    Args:
        root_node: Root node of the AST
        target_fqn: The FQN to search for
        file_path: Path to the source file
        repo_root: Root path of the repository
        project_name: Name of the project
        fqn_config: Language-specific configuration for FQN resolution

    Returns:
        Source code string of the matching function or None if not found
    """
    try:

        def walk(node: "Node") -> str | None:
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
        logger.debug(f"Failed to find function by FQN {target_fqn} in {file_path}: {e}")
        return None


def extract_function_fqns(
    root_node: "Node",
    file_path: Path,
    repo_root: Path,
    project_name: str,
    fqn_config: "FQNConfig",
) -> list[tuple[str, "Node"]]:
    """Extract all function FQNs from an AST.

    Args:
        root_node: Root node of the AST
        file_path: Path to the source file
        repo_root: Root path of the repository
        project_name: Name of the project
        fqn_config: Language-specific configuration for FQN resolution

    Returns:
        List of (fqn, node) tuples for all functions found
    """
    functions = []

    try:

        def walk(node: "Node") -> None:
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
        logger.debug(f"Failed to extract function FQNs from {file_path}: {e}")

    return functions
