"""Where a name is written in the project's code (issue #2564).

A rename's plan comes from graph edges, and the indexer misses sites in
known ways (#2542 Rust re-exports, #2544 Java static imports, #2546 method
references, ...). Applying only the sites the graph knows then leaves the
others under the old name and breaks the build while reporting success. This
reads the source, so the rename can cross-check its plan against what is
actually written.

Only identifier tokens count, as the patcher defines them: an occurrence in
a comment or a string is prose, and anything the patcher would refuse to
rewrite could not be renamed as a guessed site either.
"""

import re
from pathlib import Path
from typing import NamedTuple

from tree_sitter import Node

from .. import constants as cs
from ..config import load_ignore_patterns
from ..language_spec import get_language_for_extension, language_family
from ..parser_loader import load_parsers
from ..utils.path_utils import walk_eligible_files
from .patcher import _identifier_at

_WORD = rb"(?<![\w])%s(?![\w])"
_CALL_OPEN = re.compile(rb"\s*" + re.escape(cs.CHAR_PAREN_OPEN.encode()))
_SCOPE = cs.SEPARATOR_DOUBLE_COLON.encode()


class Occurrence(NamedTuple):
    path: str  # repo-relative, as the graph spells it
    line: int  # 1-based
    col: int  # byte column, as tree-sitter and the patcher count it
    called: bool


def find_occurrences(
    repo_root: Path, language: cs.SupportedLanguage, name: str, callable_only: bool
) -> list[Occurrence]:
    """Every identifier token `name` in the project's sources that share
    `language`'s family, in walk order.

    The walk is the indexer's own, under the repository's ignore files, so
    a vendored copy the index leaves out does not count. With
    `callable_only` a token counts only where a function or method is
    named: called (`name(`, `.name(`) or reached through a scope
    (`Type::name`, a method reference). A bare `name` there is a local, a
    parameter or a field, which share a callable's name all the time.
    """
    family = language_family(language)
    ignore = load_ignore_patterns(repo_root)
    parsers, _queries = load_parsers()
    needle = name.encode(cs.ENCODING_UTF8)
    pattern = re.compile(_WORD % re.escape(needle))
    found: list[Occurrence] = []
    for directory, filename, rel_path in walk_eligible_files(
        repo_root, ignore.exclude or None, ignore.unignore or None
    ):
        file_language = get_language_for_extension(Path(filename).suffix)
        if (
            file_language not in family
            or (parser := parsers.get(file_language)) is None
        ):
            continue
        try:
            source = (Path(directory) / filename).read_bytes()
        except OSError:
            continue
        # A plain substring test first: most files never mention the name,
        # and they are not worth a parse.
        if needle not in source:
            continue
        root = parser.parse(source).root_node
        for match in pattern.finditer(source):
            occurrence = _occurrence(root, source, match, rel_path, callable_only)
            if occurrence is not None:
                found.append(occurrence)
    return found


def _occurrence(
    root: Node,
    source: bytes,
    match: re.Match[bytes],
    rel_path: str,
    callable_only: bool,
) -> Occurrence | None:
    node = _identifier_at(root, match.start(), match.end())
    if node is None:
        return None
    called = _CALL_OPEN.match(source, match.end()) is not None
    if callable_only and not called and not _scoped(source, match.start()):
        return None
    return Occurrence(rel_path, node.start_point[0] + 1, node.start_point[1], called)


def _scoped(source: bytes, start: int) -> bool:
    # Walked back by hand: slicing the whole prefix per match would make a
    # file that names the symbol often quadratic.
    end = start
    while end > 0 and source[end - 1 : end].isspace():
        end -= 1
    return source[max(0, end - len(_SCOPE)) : end] == _SCOPE
