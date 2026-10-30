"""A source file's tree, and the combined captures the definition pass walks
it by.

`DefinitionProcessor.process_file` parses a file here unless the graph
updater handed it a tree already, and runs the language's combined
function, class and import query over it once. The function, class and call
captures that yields are left in the run's capture cache, which the call
pass reads back instead of querying the tree again."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger
from tree_sitter import QueryCursor

from .. import constants as cs
from .. import logs as ls
from ..node_text import sorted_captures
from ..types_defs import ASTNode
from ..utils.source_encoding import grammar_bytes
from .cpp.preproc_recovery import parse_with_preproc_recovery

if TYPE_CHECKING:
    from ..types_defs import LanguageQueries


def parse_file_root(
    file_path: Path,
    language: cs.SupportedLanguage,
    queries: Mapping[cs.SupportedLanguage, LanguageQueries],
    source_bytes: bytes | None,
    pre_parsed: tuple[ASTNode, dict[str, list] | None] | None,
) -> tuple[ASTNode, dict[str, list] | None] | None:
    """The file's root node and the combined captures that came with a
    pre-parsed tree; None when its language has no parser."""
    if pre_parsed is not None:
        root_node, pre_combined_captures = pre_parsed
    else:
        if source_bytes is None:
            source_bytes = file_path.read_bytes()
        parser = queries[language].get(cs.KEY_PARSER)
        if parser is None:
            logger.warning(ls.DEF_NO_PARSER.format(language=language))
            return None
        tree = parse_with_preproc_recovery(
            parser, grammar_bytes(source_bytes, language, file_path), language
        )
        root_node = tree.root_node
        pre_combined_captures = None
    return root_node, pre_combined_captures


def file_combined_captures(
    captures_cache: dict[Path, dict] | None,
    file_path: Path,
    root_node: ASTNode,
    language: cs.SupportedLanguage,
    queries: Mapping[cs.SupportedLanguage, LanguageQueries],
    pre_combined_captures: dict[str, list] | None,
) -> dict[str, list] | None:
    """The file's combined captures, run now unless they came with the
    tree, and left in `captures_cache` for the call pass."""
    if pre_combined_captures is not None:
        combined_captures = pre_combined_captures
    else:
        combined_captures = None
        combined_query = queries[language][cs.QUERY_FUNC_CLASS_IMPORT]
        if combined_query:
            cursor = QueryCursor(combined_query)
            combined_captures = sorted_captures(cursor, root_node)
    # An UNAVAILABLE query is not an empty result. `combined_captures`
    # stays None when the language has no combined query (or building
    # it raised), and caching {} for that would tell the call walk
    # "this file has no functions" when the truth is "nobody looked".
    # Measured: it attributed a call to the MODULE alongside the
    # correct function-owned edge, so the graph gained a spurious
    # `proj.pkg.caller CALLS ...` beside `proj.pkg.caller.run CALLS
    # ...` (PR #1833). Absent is the honest state there, and
    # it is what the reader already falls back on.
    if captures_cache is not None and combined_captures is not None:
        # Write unconditionally, including the EMPTY entry. The two
        # truthiness guards this replaces both skipped the write when
        # the file yielded nothing, which LEFT THE PREVIOUS PARSE'S
        # ENTRY in place -- captures holding nodes from a tree that
        # has since been discarded.
        #
        # A file emptied (or made unparseable) between runs is exactly
        # that case: its AST cache is correctly refreshed to the empty
        # tree, but `_process_function_calls` reads these captures, so
        # it walked the OLD call sites and re-emitted a CALLS edge out
        # of a function that no longer exists in the source (#1794).
        # `FileParser.load_ast_from_disk` already pops this cache on eviction for
        # the same reason; the re-parse path has to keep it true too.
        #
        # An absent entry and an empty one are not the same thing to
        # the reader: absent means "not parsed this run, look at the
        # AST", empty means "parsed, and it has nothing".
        cache_entry: dict[str, list] = {}
        for key in (cs.CAPTURE_FUNCTION, cs.CAPTURE_CLASS, cs.CAPTURE_CALL):
            if combined_captures and key in combined_captures:
                cache_entry[key] = combined_captures[key]
        captures_cache[file_path] = cache_entry
    return combined_captures
