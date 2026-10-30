"""SQL's share of the file pass: the source a SQL file is read from once,
the functions even a statement-isolated parse cannot read, and the bytes a
recovered tree was parsed from.

`DefinitionProcessor` owns what every language shares (a file's parse, its
Module node, and the function and class passes over it) and hands SQL's
part to the `SqlFileIngest` it builds."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger
from tree_sitter import Node

from ... import constants as cs
from ... import logs as ls
from ...utils.source_encoding import grammar_bytes
from ..language_definitions import LanguageFileIngest
from .recovery import lost_sql_functions

if TYPE_CHECKING:
    from ..definition_facts import DefinitionFacts


class SqlFileIngest(LanguageFileIngest):
    """Reads a SQL file once, warns of the functions its parse lost, and
    keeps the bytes a recovered tree's text is not.

    Built and owned by `DefinitionProcessor`. The definition facts are the
    pass's own, held by reference: the call pass reads the kept bytes."""

    __slots__ = ("facts",)

    def __init__(self, facts: DefinitionFacts) -> None:
        self.facts = facts

    def read_source(self, file_path: Path, source_bytes: bytes | None) -> bytes | None:
        # Read once: the call pass reads DDL from the bytes parsed (#3181).
        if source_bytes is None:
            return file_path.read_bytes()
        return source_bytes

    def settle_parse(
        self,
        file_path: Path,
        relative_path: str,
        root_node: Node,
        source_bytes: bytes | None,
    ) -> None:
        if source_bytes is None:
            return
        self._warn_lost_functions(file_path, relative_path, root_node, source_bytes)
        self.keep_source(
            file_path,
            root_node,
            grammar_bytes(source_bytes, cs.SupportedLanguage.SQL, file_path),
        )

    def keep_source(self, file_path: Path, root_node: Node, parsed: bytes) -> None:
        """Note the bytes a SQL tree was parsed from, when its text is not them."""
        start, end = root_node.start_byte, root_node.end_byte
        # A blanked statement that opens or closes the file lies outside the
        # root's span, where an unrecovered parse leaves only whitespace.
        if (
            root_node.text == parsed[start:end]
            and not parsed[:start].strip()
            and not parsed[end:].strip()
        ):
            self.facts.sql_sources.pop(file_path, None)
        else:
            self.facts.sql_sources[file_path] = parsed

    @staticmethod
    def _warn_lost_functions(
        file_path: Path, relative_path: str, root_node: Node, source: bytes | None
    ) -> None:
        # A function even a statement-isolated parse cannot read is missing
        # from the graph with its calls; the sync says so (issue #3180).
        if source is None:
            source = file_path.read_bytes()
        if count := lost_sql_functions(source, root_node):
            logger.warning(ls.SQL_FUNCTIONS_UNPARSED, path=relative_path, count=count)
