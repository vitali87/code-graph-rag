"""Shared declaration join for the semantic frontends (C# Roslyn, Go go/types).

A frontend fact carries a target declaration POSITION; both resolvers turn it
into the ingested node's (label, qn) through the exact `function_locations`
record Pass 2 registered. The C# and Go engines held byte-identical copies of
this join, so it lives here once (issue #1179)."""

from __future__ import annotations

from pathlib import Path

from tree_sitter import Node

from ..types_defs import FunctionLocation, FunctionSpanKey
from ..utils.path_utils import cached_relative_path

# (rel_file, name_token_line, name_token_byte_col, simple_name).
CallSiteKey = tuple[str, int, int, str]


def call_site_key(
    name_node: Node,
    simple_name: str,
    module_qn: str,
    module_qn_to_file_path: dict[str, Path],
    repo_path: Path,
) -> CallSiteKey | None:
    # The location join key: the callee NAME token's (rel_file, line, byte_col)
    # plus its simple name. Nested calls share an expression start but never a
    # name token. The caller supplies simple_name so each language applies its
    # own normalisation (C# strips generic arguments; Go passes it through).
    file_path = module_qn_to_file_path.get(module_qn)
    if file_path is None:
        return None
    rel = cached_relative_path(file_path, repo_path).as_posix()
    return (rel, name_node.start_point[0] + 1, name_node.start_point[1], simple_name)


def module_qn_for_rel_file(
    rel_file: str,
    module_qn_to_file_path: dict[str, Path],
    repo_path: Path,
    rel_to_module: dict[str, str],
) -> str | None:
    # Lazy inverse of module_qn_to_file_path (which Pass 2 fills after the engine
    # is constructed); the passed-in cache is rebuilt in place when new modules
    # appeared so the caller's reference stays valid.
    if len(rel_to_module) != len(module_qn_to_file_path):
        rel_to_module.clear()
        rel_to_module.update(
            {
                cached_relative_path(path, repo_path).as_posix(): qn
                for qn, path in module_qn_to_file_path.items()
            }
        )
    return rel_to_module.get(rel_file)


def declared_location(
    rel_file: str,
    line: int,
    col: int,
    function_locations: dict[FunctionSpanKey, FunctionLocation],
    module_qn_to_file_path: dict[str, Path],
    repo_path: Path,
    rel_to_module: dict[str, str],
) -> tuple[str, str] | None:
    # The fact's target position resolves through the exact (module_qn,
    # start_line, start_col) record Pass 2 registered, so the returned label/qn
    # are the ingested node's, signature included.
    target_module = module_qn_for_rel_file(
        rel_file, module_qn_to_file_path, repo_path, rel_to_module
    )
    if target_module is None:
        return None
    location = function_locations.get((target_module, line, col))
    if location is None:
        return None
    return location.label, location.qualified_name
