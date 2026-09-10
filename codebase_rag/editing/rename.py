"""Edit algebra op 1: `rename(qn, new_name)` end to end (issue #1532).

A repo-wide rename by text replacement is what agents get wrong: partial
renames, same-named symbols of another language, dynamic sites nobody saw.
The graph knows every site, so a rename is a graph operation:

1. collect the definition's name token, every call, reference and
   construction site (with its `resolution`, issue #1526), every import
   statement binding the symbol (issue #1522) and, for a method on a
   hierarchy, the same for each overriding and overridden method;
2. refuse when a site is ambiguous (`heuristic`, `overload`, `dynamic`)
   unless the caller accepts the risk with `allow_heuristic`; a graph-known
   site with no rewrite location refuses regardless, it cannot be rewritten
   at all and applying would leave it under the old name;
3. rewrite the identifier at every site through the span patcher (issue
   #1529) and the import statements through the import rewriter (issue
   #1530), stage the results in a transaction (issue #1528), verify that
   every patched file still parses (plus any verifier the caller adds, the
   postcondition contract of issue #1531 once it lands) and commit, or
   roll back and report why.

Documentation mentions (`Section` nodes, docstrings) are reported, not
rewritten: prose is not a graph edge.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import NamedTuple

from loguru import logger
from tree_sitter import Node

from .. import constants as cs
from .. import cypher_queries as cq
from .. import graph_query
from ..language_spec import get_language_for_extension
from ..parser_loader import load_parsers
from ..types_defs import PropertyDict, ResultRow
from ..utils.path_utils import base_module_qn
from .contract import Reingest, Verdict, measure, rename_expectation, verify
from .imports import ANY_MODULE, ImportRewriter, ImportSite, SymbolMove, _imported
from .patcher import Patcher, PatcherError, line_col_to_byte
from .transaction import (
    EditTransaction,
    StagedTree,
    TransactionConflict,
    VerificationResult,
    load_history,
    undo_transaction,
)

QueryFn = Callable[[str, PropertyDict | None], list[ResultRow]]

_AMBIGUOUS = frozenset(
    {
        cs.EdgeResolution.HEURISTIC.value,
        cs.EdgeResolution.OVERLOAD.value,
        cs.EdgeResolution.DYNAMIC.value,
    }
)
_IDENTIFIER_RE = r"(?<![\w])%s(?![\w])"


_STRUCTURAL = "structural"
_SITELESS = "siteless"
# A same-named link of a fluent chain the index has no row for.
_CHAIN = "chain"
_MEMBER_NAME_TYPES = frozenset(
    {cs.TS_IDENTIFIER, cs.TS_PROPERTY_IDENTIFIER, "field_identifier"}
)


# The same shapes `ImportRewriter.rename_in_all` rewrites. Kept identical so
# the restoration check and the rewrite cannot disagree about what an
# `__all__` entry is (Greptile, PR #1547).
_ALL_BLOCK = r"__all__\s*(?::[^=]+)?=\s*[\[(]([^\])]*)[\])]"
_ALL_ENTRY = r"""(['"])(?P<name>[A-Za-z_]\w*)\1"""

# How far a parenthesised import is followed looking for its closing bracket.
# Bounded so a file with an unbalanced paren cannot make this walk the rest of
# the file; a real import list is far shorter than this.
#
# No test pins the bound, and removing it reddens nothing: on well-formed
# input the loop stops at the closing bracket either way, so the cap only
# changes behaviour on a file that does not parse as Python at all. It is
# defensive, not load-bearing -- do not read the suite's greenness as evidence
# that it works.
_MAX_IMPORT_LINES = 200


class RenameSite(NamedTuple):
    """One place the old name is written and must become the new one."""

    kind: str  # definition | call | reference | import
    path: str
    line: int
    col: int
    owner: str  # the qualified name the site belongs to
    resolution: str | None


class RenameRefused(ValueError):
    """The rename would rewrite through a guess; nothing was changed."""

    def __init__(
        self, message: str, ambiguous: list[RenameSite], unlocatable: list[str]
    ) -> None:
        super().__init__(message)
        self.ambiguous = ambiguous
        self.unlocatable = unlocatable


class RenameReport(NamedTuple):
    qualified_name: str
    old_name: str
    new_name: str
    applied: bool
    transaction_id: str
    files: tuple[str, ...]
    sites: tuple[RenameSite, ...]
    ambiguous: tuple[RenameSite, ...]
    unlocatable: tuple[str, ...]
    doc_mentions: tuple[str, ...]
    hierarchy: tuple[str, ...]
    diff: str
    message: str
    verdict: Verdict | None = None
    # True when a rollback re-ingest failed after the files were restored:
    # the graph may hold a partial picture and must be rebuilt.
    graph_incomplete: bool = False


# --- site collection -----------------------------------------------------------


def _hierarchy(fetch_all: QueryFn, project: str, qn: str) -> list[str]:
    """`qn` plus every method it overrides or is overridden by, transitively."""
    seen: list[str] = [qn]
    frontier = [qn]
    while frontier:
        current = frontier.pop()
        for row in graph_query.overrides(fetch_all, project, current):
            other = row["qualified_name"]
            if other not in seen:
                seen.append(other)
                frontier.append(other)
    return seen


def _name_token(
    source: bytes,
    language: cs.SupportedLanguage | None,
    start_line: int,
    end_line: int,
    name: str,
) -> tuple[int, int] | None:
    """(line, col) of the definition's own name identifier inside its span."""
    parser = None
    if language is not None:
        parsers, _queries = load_parsers()
        parser = parsers.get(language)
    if parser is not None:
        root = parser.parse(source).root_node
        stack: list[Node] = [root]
        while stack:
            node = stack.pop()
            if node.end_point[0] + 1 < start_line or node.start_point[0] + 1 > end_line:
                continue
            named = node.child_by_field_name(cs.FIELD_NAME)
            if (
                named is not None
                and named.text is not None
                and named.text.decode(cs.ENCODING_UTF8, errors="replace") == name
                and named.start_point[0] + 1 >= start_line
            ):
                return named.start_point[0] + 1, named.start_point[1]
            stack.extend(node.children)
        return None
    # No grammar: the first whole-word occurrence inside the span.
    text = source.decode(cs.ENCODING_UTF8, errors="replace")
    lines = text.split("\n")
    for number in range(start_line, min(end_line, len(lines)) + 1):
        match = re.search(_IDENTIFIER_RE % re.escape(name), lines[number - 1])
        if match:
            return number, len(
                lines[number - 1][: match.start()].encode(cs.ENCODING_UTF8)
            )
    return None


def _best_call_at(
    root: Node, line: int, col: int, recorded_end: tuple[int, int] | None
) -> Node | None:
    """The call node at (line, col) that the graph site refers to.

    Several calls can share a start point -- `helper(helper(1))`,
    `helper(2).upper()`, and both links of `obj.helper(1).helper(2)` -- so the
    right one is the call ending where the site recorded its end, or the
    outermost when no end was recorded.

    Extracted from `_callee_span` to keep it under the cognitive complexity
    limit (S3776). This walk is where that complexity lives: a loop with
    three levels of nested branching, and nesting multiplies the cost.
    Extracting the straight-line setup around it would not have helped.
    """
    best: Node | None = None
    stack: list[Node] = [root]
    while stack:
        node = stack.pop()
        if node.start_point == (line - 1, col):
            func = node.child_by_field_name(cs.FIELD_FUNCTION)
            if func is not None:
                if node.end_point == recorded_end:
                    return node
                if best is None or (
                    best.end_point != recorded_end and node.end_byte > best.end_byte
                ):
                    best = node
        if node.start_point[0] <= line - 1 <= node.end_point[0]:
            stack.extend(node.children)
    return best


def _callee_span(
    source: bytes,
    language: cs.SupportedLanguage | None,
    line: int,
    col: int,
    end_line: int | None = None,
    end_col: int | None = None,
) -> tuple[int, int] | None:
    """Byte span of the callee expression of the site's call at (line, col).

    `helper(helper(1))` and `helper(2).upper()` both start at the same point
    as an inner call, and so do both links of `obj.helper(1).helper(2)`: the
    site's call is the one ending where the graph recorded the site's end,
    or, without a recorded end, the outermost one.
    """
    if language is None:
        return None
    parsers, _queries = load_parsers()
    parser = parsers.get(language)
    if parser is None:
        return None
    root = parser.parse(source).root_node
    recorded_end = (
        (end_line - 1, end_col)
        if end_line is not None and end_col is not None
        else None
    )
    best = _best_call_at(root, line, col, recorded_end)
    if best is None:
        return None
    func = best.child_by_field_name(cs.FIELD_FUNCTION)
    assert func is not None
    return func.start_byte, func.end_byte


def _chain_links(
    source: bytes,
    language: cs.SupportedLanguage | None,
    line: int,
    col: int,
    name: str,
) -> list[tuple[int, int]]:
    """(line, col) of `name` as the member called by EVERY call starting at
    (line, col): the links of a fluent chain `obj.name(1).name(2)`.

    The index keeps one call row per start position, so the chain's later
    links have no row of their own; the caller decides what to do with them.
    """
    if language is None:
        return []
    parsers, _queries = load_parsers()
    parser = parsers.get(language)
    if parser is None:
        return []
    root = parser.parse(source).root_node
    found: list[tuple[int, int]] = []
    stack: list[Node] = [root]
    while stack:
        node = stack.pop()
        if node.start_point == (line - 1, col):
            func = node.child_by_field_name(cs.FIELD_FUNCTION)
            member = func.children[-1] if func is not None and func.children else None
            if (
                member is not None
                and member.type in _MEMBER_NAME_TYPES
                and member.text is not None
                and member.text.decode(cs.ENCODING_UTF8, errors="replace") == name
            ):
                found.append((member.start_point[0] + 1, member.start_point[1]))
        if node.start_point[0] <= line - 1 <= node.end_point[0]:
            stack.extend(node.children)
    return sorted(found)


def _last_identifier(
    source: bytes,
    line: int,
    col: int,
    end_line: int,
    end_col: int,
    name: str,
    language: cs.SupportedLanguage | None = None,
) -> tuple[int, int] | None:
    """(line, col) of the token to rename inside a site span.

    For a call site the token is the rightmost `name` inside the OUTERMOST
    call's callee expression (`pkg.helper`, `Circle().area`), never inside
    its arguments; for any other site it is the rightmost `name` in the span.
    """
    start = line_col_to_byte(source, line, col)
    end = line_col_to_byte(source, end_line, end_col)
    callee = _callee_span(source, language, line, col, end_line, end_col)
    if callee is not None and callee[0] == start:
        start, end = callee
    text = source[start:end].decode(cs.ENCODING_UTF8, errors="replace")
    if callee is None:
        # No grammar: cut at the last opening parenthesis so the arguments
        # of a plain call are excluded (`helper(helper=2)`).
        paren = text.rfind("(")
        if paren >= 0:
            text = text[:paren]
    matches = list(re.finditer(_IDENTIFIER_RE % re.escape(name), text))
    if not matches:
        return None
    offset = start + len(text[: matches[-1].start()].encode(cs.ENCODING_UTF8))
    from .patcher import byte_to_line_col

    return byte_to_line_col(source, offset)


class Renamer:
    """Plan and apply one rename against a project's graph."""

    def __init__(
        self,
        repo_root: Path,
        fetch_all: QueryFn,
        project_name: str,
        verify: Callable[[StagedTree], VerificationResult | bool | None] | None = None,
        after_apply: Callable[[list[str]], None] | None = None,
        reingest: Reingest | None = None,
    ) -> None:
        self.repo_root = repo_root.resolve()
        self.fetch_all = fetch_all
        self.project = project_name
        self.verify = verify
        self.after_apply = after_apply
        # With a re-ingest the rename is held to its postcondition contract
        # (issue #1531): the delta of what it wrote is measured and the
        # transaction undone when the contract fails.
        self.reingest = reingest

    def _module_of(self, qn: str) -> tuple[str, str | None]:
        # The defining module's qn and path, from the definition's own path:
        # a method's qn nests under its class, so stripping segments would
        # name the class, not the module.
        definition = graph_query.definition(self.fetch_all, self.project, qn, None)
        path = definition["path"]
        if not path:
            return qn.rsplit(cs.SEPARATOR_DOT, 1)[0], None
        return base_module_qn(Path(path), self.project), path

    def _collect(self, qn: str) -> tuple[list[RenameSite], list[str], str, str | None]:
        definition = graph_query.definition(
            self.fetch_all, self.project, qn, self.repo_root
        )
        if not definition["found"] or not definition["path"]:
            raise RenameRefused(cs.RENAME_UNKNOWN.format(qn=qn), [], [])
        old_name = definition["name"] or qn.rsplit(cs.SEPARATOR_DOT, 1)[-1]
        sites: list[RenameSite] = []
        unlocatable: list[str] = []
        patcher = Patcher(self.repo_root)
        # Definition name token.
        path = definition["path"]
        try:
            source = patcher.source(path)
        except PatcherError as error:
            # The graph names a file the tree no longer has, or cannot read.
            # Every other refusal on this path is a RenameRefused, and the
            # MCP handler turns that into a payload; letting a PatcherError
            # escape makes a stale index an unhandled error instead of the
            # documented refusal (the site-level read below already does
            # this).
            raise RenameRefused(
                cs.RENAME_DEFINITION_UNREADABLE.format(qn=qn, path=path, error=error),
                [],
                [],
            ) from error
        start = definition["start_line"] or 1
        end = definition["end_line"] or start
        token = _name_token(
            source, get_language_for_extension(Path(path).suffix), start, end, old_name
        )
        if token is None:
            raise RenameRefused(
                cs.RENAME_NO_DEFINITION_TOKEN.format(qn=qn, path=path), [], []
            )
        sites.append(
            RenameSite(
                "definition", path, token[0], token[1], qn, cs.EdgeResolution.EXACT
            )
        )
        # Calls, references and constructions.
        for row in graph_query.callers(self.fetch_all, self.project, qn):
            self._add_site(sites, unlocatable, "call", row, old_name, patcher)
        params = {
            cs.KEY_PROJECT_PREFIX: f"{self.project}{cs.SEPARATOR_DOT}",
            cs.KEY_QN: qn,
        }
        for row in self.fetch_all(cq.CYPHER_GRAPH_REFERENCES, params):
            self._add_site(sites, unlocatable, "reference", row, old_name, patcher)
        # A base-class list or an annotation names the symbol without a
        # call; an edge without a site cannot be rewritten and must refuse,
        # or the applied rename would leave `class Circle(Base)` dangling.
        for row in self.fetch_all(cq.CYPHER_GRAPH_TYPE_EDGES, params):
            if not isinstance(row.get("path"), str) or not isinstance(
                row.get("line"), int
            ):
                sites.append(
                    RenameSite(
                        "unlocatable",
                        str(row.get("path") or ""),
                        0,
                        0,
                        str(row.get("qualified_name") or ""),
                        _STRUCTURAL,
                    )
                )
                continue
            self._add_site(sites, unlocatable, "reference", row, old_name, patcher)
        return sites, unlocatable, old_name, definition["label"]

    @staticmethod
    def _record_unlocatable(
        sites: list[RenameSite],
        unlocatable: list[str],
        *,
        owner: str,
        path: str,
        line: int,
        col: int,
        resolution: str,
        site_resolution: str,
    ) -> None:
        """Record a graph-known occurrence that cannot be rewritten.

        Both refusal paths in `_add_site` -- a row with no usable position,
        and a file the patcher cannot read -- append the same pair. Sharing
        them keeps `_add_site` under the cognitive complexity limit (S3776)
        by removing a whole branch body rather than straight-line setup,
        which is the part that actually counts.
        """
        unlocatable.append(
            cs.RENAME_UNLOCATABLE_SITE.format(owner=owner, resolution=resolution)
        )
        sites.append(RenameSite("unlocatable", path, line, col, owner, site_resolution))

    def _add_site(
        self,
        sites: list[RenameSite],
        unlocatable: list[str],
        kind: str,
        row: ResultRow | graph_query.CallSiteRow,
        old_name: str,
        patcher: Patcher,
    ) -> None:
        owner = str(row.get("qualified_name") or "")
        path = row.get("path")
        line, col = row.get("line"), row.get("col")
        end_line, end_col = row.get("end_line"), row.get("end_col")
        resolution = row.get("resolution")
        resolution_text = str(resolution) if isinstance(resolution, str) else None
        if (
            not isinstance(path, str)
            or not isinstance(line, int)
            or not isinstance(col, int)
        ):
            # A graph-known edge with no site cannot be rewritten, whatever
            # bound it: applying anyway would leave that caller under the
            # old name, so it blocks like a guess does.
            self._record_unlocatable(
                sites,
                unlocatable,
                owner=owner,
                path=path if isinstance(path, str) else "",
                line=0,
                col=0,
                resolution=resolution_text or "unknown",
                site_resolution=resolution_text or _SITELESS,
            )
            return
        try:
            source = patcher.source(path)
        except PatcherError:
            # The graph knows this occurrence but its file cannot be read:
            # renaming around it would leave it under the old name.
            self._record_unlocatable(
                sites,
                unlocatable,
                owner=owner,
                path=path,
                line=line,
                col=col,
                resolution="missing file",
                site_resolution=_SITELESS,
            )
            return
        token = _last_identifier(
            source,
            line,
            col,
            end_line if isinstance(end_line, int) else line,
            end_col if isinstance(end_col, int) else col + len(old_name),
            old_name,
            get_language_for_extension(Path(path).suffix),
        )
        if token is None:
            # The site spells the symbol under an alias (`h(1, 2)` for
            # `import helper as h`); the alias keeps binding, so nothing to
            # rewrite here.
            return
        sites.append(RenameSite(kind, path, token[0], token[1], owner, resolution_text))
        if kind != "call":
            return
        # `obj.helper(1).helper(2)`: a link of the chain the index has no
        # row for (the receiver's type was not inferred) binds to nobody
        # the graph knows. It is renamed only as a guess the caller opts
        # into with allow_heuristic; `plan` drops it where a row covers it.
        language = get_language_for_extension(Path(path).suffix)
        for link in _chain_links(source, language, line, col, old_name):
            if link != token:
                sites.append(RenameSite(kind, path, link[0], link[1], owner, _CHAIN))

    def _import_sites(self, qn: str, old_name: str) -> list[tuple[ImportSite, str]]:
        module_qn, _path = self._module_of(qn)
        out: list[tuple[ImportSite, str]] = []
        if qn != f"{module_qn}{cs.SEPARATOR_DOT}{old_name}":
            # Only a module-level member is imported by name; a method or a
            # nested definition shares its name with nothing an importer
            # can bind, so `from pkg.util import get` stays as it is.
            return out
        # A module that imports the name re-exports it (`pkg/__init__.py`
        # as a barrel): its own importers of the name bind through it and
        # are rewritten too, transitively.
        pending = [module_qn]
        seen = {module_qn}
        while pending:
            source_qn = pending.pop()
            for row in graph_query.importers(self.fetch_all, self.project, source_qn):
                if (
                    row["imported_name"] != old_name
                    or row["path"] is None
                    or row["line"] is None
                ):
                    continue
                out.append(
                    (
                        ImportSite(
                            row["path"],
                            row["line"],
                            row["col"] or 0,
                            row["end_line"] or row["line"],
                            row["end_col"] or 0,
                            row["alias"],
                            row["imported_name"],
                        ),
                        row["module"],
                    )
                )
                if row["module"] not in seen:
                    seen.add(row["module"])
                    pending.append(row["module"])
        return out

    def _doc_mentions(self, old_name: str) -> list[str]:
        pattern = re.compile(_IDENTIFIER_RE % re.escape(old_name))
        found: list[str] = []
        for path in sorted(self.repo_root.rglob("*.md")):
            if any(
                part in cs.IGNORE_PATTERNS
                for part in path.relative_to(self.repo_root).parts
            ):
                continue
            try:
                lines = path.read_text(
                    encoding=cs.ENCODING_UTF8, errors="replace"
                ).splitlines()
            except OSError:
                continue
            for number, text in enumerate(lines, 1):
                if pattern.search(text):
                    found.append(
                        f"{path.relative_to(self.repo_root).as_posix()}:{number}"
                    )
        return found

    # --- the operation -------------------------------------------------------------

    def plan(
        self, qn: str, new_name: str, allow_heuristic: bool = False
    ) -> RenameReport:
        """Collect everything a rename touches; refuse on ambiguity."""
        if not re.fullmatch(r"[A-Za-z_]\w*", new_name):
            raise RenameRefused(cs.RENAME_BAD_NAME.format(name=new_name), [], [])
        hierarchy = _hierarchy(self.fetch_all, self.project, qn)
        sites: list[RenameSite] = []
        unlocatable: list[str] = []
        old_name: str | None = None
        for member in hierarchy:
            member_sites, member_unlocatable, member_name, _label = self._collect(
                member
            )
            old_name = old_name or member_name
            sites.extend(member_sites)
            unlocatable.extend(member_unlocatable)
        assert old_name is not None
        covered = {(s.path, s.line, s.col) for s in sites if s.resolution != _CHAIN}
        sites = [
            s
            for s in sites
            if s.resolution != _CHAIN or (s.path, s.line, s.col) not in covered
        ]
        structural = [s for s in sites if s.resolution == _STRUCTURAL]
        if structural:
            raise RenameRefused(
                cs.RENAME_STRUCTURAL_UNLOCATABLE.format(qn=qn, count=len(structural)),
                structural,
                unlocatable,
            )
        # Every graph-known occurrence without a rewrite location blocks,
        # whatever bound it: `allow_heuristic` accepts rewriting through a
        # guessed site, not leaving a known one under the old name.
        siteless = [s for s in sites if s.kind == "unlocatable"]
        if siteless:
            raise RenameRefused(
                cs.RENAME_SITELESS.format(qn=qn, count=len(siteless)),
                siteless,
                unlocatable,
            )
        ambiguous = [
            s for s in sites if s.resolution in _AMBIGUOUS or s.resolution == _CHAIN
        ]
        if ambiguous and not allow_heuristic:
            raise RenameRefused(
                cs.RENAME_AMBIGUOUS.format(qn=qn, count=len(ambiguous)),
                ambiguous,
                unlocatable,
            )
        for member in hierarchy:
            for site, module in self._import_sites(member, old_name):
                sites.append(
                    RenameSite(
                        "import",
                        site.path,
                        site.line,
                        site.col,
                        module,
                        cs.EdgeResolution.EXACT,
                    )
                )
        return RenameReport(
            qualified_name=qn,
            old_name=old_name,
            new_name=new_name,
            applied=False,
            transaction_id="",
            # `preview` and `apply` both replace this with the files the
            # transaction actually staged, and a refusal raises rather than
            # returning, so computing it here would run `_all_paths` (one
            # graph query per hierarchy member) for a value nothing reads.
            files=(),
            sites=tuple(sites),
            ambiguous=tuple(ambiguous),
            unlocatable=tuple(unlocatable),
            doc_mentions=tuple(self._doc_mentions(old_name)),
            hierarchy=tuple(hierarchy),
            diff="",
            message=cs.RENAME_PLANNED.format(count=len(sites)),
        )

    def _all_paths(self, hierarchy: list[str], old_name: str) -> set[str]:
        """Python modules whose `__all__` may list the name: the defining
        module of each module-level member, plus the modules importing it."""
        paths: set[str] = set()
        for member in hierarchy:
            module_qn, module_path = self._module_of(member)
            if member != f"{module_qn}{cs.SEPARATOR_DOT}{old_name}":
                continue
            if module_path:
                paths.add(module_path)
            paths.update(site.path for site, _m in self._import_sites(member, old_name))
        return {
            path
            for path in paths
            if get_language_for_extension(Path(path).suffix)
            == cs.SupportedLanguage.PYTHON
        }

    def _stage(
        self, report: RenameReport, new_name: str
    ) -> tuple[EditTransaction, dict[str, object], list[str]]:
        """Patch every site into a transaction; nothing touches the tree."""
        old_name = report.old_name
        patcher = Patcher(self.repo_root)
        done: set[tuple[str, int, int]] = set()
        for site in report.sites:
            key = (site.path, site.line, site.col)
            if key in done or site.kind in ("unlocatable", "import"):
                continue
            done.add(key)
            patcher.replace_identifier_at(
                site.path, site.line, site.col, old_name, new_name
            )
        rewriter = ImportRewriter(self.repo_root, patcher)
        import_sites = [
            site
            for member in report.hierarchy
            for site, _module in self._import_sites(member, old_name)
        ]
        rewriter.retarget(
            import_sites,
            SymbolMove(
                old_name, ANY_MODULE, ANY_MODULE, new_name=new_name, rebind=True
            ),
        )
        # `__all__` entries live in the defining module and in any Python
        # module re-exporting the name (a package `__init__`); only a
        # module-level member can be listed there.
        for path in sorted(self._all_paths(list(report.hierarchy), old_name)):
            rewriter.rename_in_all(path, old_name, new_name)
        tx = EditTransaction(self.repo_root)
        results = patcher.stage_into(tx)
        broken = [key for key, result in results.items() if result.parses is False]
        return tx, dict(results), broken

    def preview(
        self, qn: str, new_name: str, allow_heuristic: bool = False
    ) -> RenameReport:
        """Plan and stage, return the diff, and leave the tree untouched."""
        report = self.plan(qn, new_name, allow_heuristic)
        tx, results, broken = self._stage(report, new_name)
        try:
            diff = tx.diff()
        finally:
            tx.rollback()
        message = (
            cs.RENAME_PARSE_FAILED.format(files=", ".join(broken))
            if broken
            else report.message
        )
        return report._replace(files=tuple(sorted(results)), diff=diff, message=message)

    def apply(
        self, qn: str, new_name: str, allow_heuristic: bool = False
    ) -> RenameReport:
        """Plan, patch, verify and commit; the tree is untouched on failure."""
        report = self.plan(qn, new_name, allow_heuristic)
        tx, results, broken = self._stage(report, new_name)
        if broken:
            tx.rollback()
            return report._replace(
                files=tuple(sorted(results)),
                message=cs.RENAME_PARSE_FAILED.format(files=", ".join(broken)),
            )

        def verify(tree: StagedTree) -> VerificationResult | bool | None:
            return self.verify(tree) if self.verify is not None else True

        outcome = tx.commit(verify)
        report = report._replace(
            applied=outcome.applied,
            transaction_id=outcome.transaction_id,
            files=outcome.files,
            diff=outcome.diff,
            message=outcome.message,
        )
        if outcome.applied and self.reingest is not None:
            report = self._enforce_contract(report, new_name, allow_heuristic)
        if report.applied and self.after_apply is not None:
            self.after_apply(list(report.files))
        return report

    def _undo_state(self, transaction_id: str) -> str:
        """What a `TransactionConflict` on this transaction actually means.

        `"stacked"` - the entry is still in the history, so a LATER edit sits
        on top of this rename and it remains applied.

        `"unknown"` - the entry is absent. That is NOT evidence of an undo:
        the history keeps `EDIT_HISTORY_LIMIT` entries, so enough later edits
        evict an entry whose rename is still on disk. An earlier version of
        this method inferred "undone" whenever the history was below its
        limit, which fails once those later entries are themselves undone --
        the history shrinks and the eviction becomes invisible (Greptile,
        PR #1547). Absence can never prove a reversal, so it is never
        reported as one; the caller keeps `applied` and is told the state
        could not be determined.

        Whether the files were actually restored is settled by
        `_rename_is_on_disk`, which reads the tree rather than the history.
        """
        try:
            entries = load_history(self.repo_root)
        # An unreadable history is an unknown state: keep `applied` as it
        # was rather than guessing in either direction.
        except Exception:  # noqa: BLE001
            return cs.RENAME_UNDO_STACKED
        if any(
            str(entry.get(cs.EDIT_KEY_ID, "")) == transaction_id for entry in entries
        ):
            return cs.RENAME_UNDO_STACKED
        return cs.RENAME_UNDO_UNKNOWN

    def _old_name_is_back(self, report: RenameReport) -> bool:
        """Whether the old name has returned AT THE SITES this rename edited.

        `applied` is a claim about the working tree, so the tree settles it,
        not the history: an entry can be evicted by later edits while its
        rename stands, and once those later entries are themselves undone the
        history is short again and the eviction leaves no trace.

        Checked at the recorded SITES, not by searching the file. A
        whole-file token search for the old name matches any unrelated
        symbol that happens to share it -- an independent `helper` elsewhere
        in a touched file read as proof that this rename had been reversed
        (Greptile, PR #1547). The sites are the exact positions this rename
        rewrote, so they are the only places its reversal can show.

        The test is the OLD name, not the new one: the new name is routinely
        present either way, since renaming onto an existing symbol is exactly
        what makes the contract fail. A site that cannot be read counts as
        NOT reverted, keeping `applied` unchanged rather than claiming a
        rollback that may not have happened.
        """
        by_path: dict[str, list[RenameSite]] = {}
        import_sites: list[RenameSite] = []
        for site in report.sites:
            # An `unlocatable` site has no usable position at all, so nothing
            # can be read at it either way.
            if site.kind == "unlocatable":
                continue
            # An `import` site IS rewritten -- by `ImportRewriter.retarget`,
            # not by the identifier patcher -- so it must show a reversal like
            # any other. `_stage_sites` skips it only because its recorded
            # column points at the import STATEMENT, which the patcher cannot
            # use; reading a slice there yields `from p` and never the name.
            # Excluding it outright let a tree whose import still carried the
            # NEW name report as fully reversed (Greptile, PR #1547), so it is
            # checked by LINE CONTENT instead of by column.
            if site.kind == "import":
                import_sites.append(site)
                continue
            by_path.setdefault(site.path, []).append(site)
        if not by_path:
            # Nothing was rewritten, so there is nothing to find restored.
            # Claiming a full undo here would report every rename with no
            # recorded sites as reversed.
            return False
        old_bytes = report.old_name.encode("utf-8")
        # EVERY rewritten site must carry the old name again: one restored
        # site is a PARTIAL undo, and reporting `applied=False` for it tells
        # the caller every file is back when other definitions and references
        # are still renamed (Greptile, PR #1547).
        if not all(
            self._file_has_old_name_at(relative, sites, old_bytes)
            for relative, sites in by_path.items()
        ):
            return False
        if not all(
            self._import_names_the_old_name(site, report.old_name)
            for site in import_sites
        ):
            return False
        # `__all__` entries are rewritten by `rename_in_all` and are NOT
        # recorded as sites, so nothing above can see them. A tree with every
        # definition, reference and import restored but an export still
        # naming the NEW symbol is not fully reversed (Greptile, PR #1547).
        return all(
            self._no_all_entry_names_the_new_name(path, report.new_name)
            for path in sorted({site.path for site in report.sites})
        )

    def _no_all_entry_names_the_new_name(self, path: str, new_name: str) -> bool:
        """Whether no `__all__` in `path` still exports the NEW name.

        Phrased against the new name rather than the old one, because that is
        what `rename_in_all` actually did: it rewrites EVERY matching literal
        in EVERY `__all__` block in the file, so a complete undo leaves none
        of them reading `new_name`.

        The previous phrasing -- "some entry reads `old_name`" -- accepted a
        file where ONE block had been restored and another had not, since a
        single restored entry satisfied it. A module with a second `__all__`
        under `if TYPE_CHECKING:` is exactly that shape, and it reported a
        complete rollback with an export still naming the renamed symbol
        (Greptile, PR #1547, third round on this function).

        Absence is still not evidence of failure: a file with no `__all__`,
        or whose entries never mentioned either name, answers True. The check
        uses the same literal scan `rename_in_all` uses to rewrite, so the two
        agree by construction.
        """
        try:
            text = (self.repo_root / path).read_text(encoding="utf-8")
        except OSError:
            return False
        return not any(
            literal.group("name") == new_name
            for block in re.finditer(_ALL_BLOCK, text, re.S)
            for literal in re.finditer(_ALL_ENTRY, block.group(1))
        )

    def _import_names_the_old_name(self, site: RenameSite, old_name: str) -> bool:
        """Whether the statement IMPORTS the old name again.

        Read per ENTRY rather than as a token anywhere on the line. The
        recorded column is the statement's start, so a column read is
        useless here; but a line-level search accepts a binding that is not
        the target at all -- in
        `from util import assist, helper_of_other as helper` the symbol is
        still imported as `assist`, and the `helper` on that line is an
        unrelated alias (Greptile, PR #1547).

        What each entry IMPORTS is the question, and what it binds locally is
        not: `helper as h` does import the old name. `_imported` is the same
        parse `ImportRewriter` uses to decide what to rewrite, so the check
        and the rewrite agree by construction.
        """
        try:
            lines = (
                (self.repo_root / site.path).read_text(encoding="utf-8").splitlines()
            )
        except OSError:
            return False
        if not 0 < site.line <= len(lines):
            return False
        line = lines[site.line - 1]
        # A parenthesised import spans lines, and the recorded line is the
        # STATEMENT's first one, so reading it alone never sees an entry on a
        # continuation line: a fully restored multiline import read as not
        # restored (Greptile, PR #1547). That direction is safe -- it refuses
        # to claim a complete undo -- but it makes a restored tree and a
        # partly-restored one indistinguishable, which is the property this
        # check exists to provide.
        if "(" in line and ")" not in line[line.index("(") :]:
            for extra in lines[site.line : site.line + _MAX_IMPORT_LINES]:
                line += " " + extra.strip()
                if ")" in extra:
                    break
        _head, _sep, tail = line.partition("import ")
        # No `import` on the line: fall back to a word match rather than
        # claiming restored, since a language whose form this cannot parse
        # must not be read as evidence either way.
        if not _sep:
            return bool(re.search(rf"\b{re.escape(old_name)}\b", line))
        # Brackets stripped per entry: a joined parenthesised import yields
        # `( helper` and `)` as entries, and neither parses as a name.
        return any(
            _imported(entry.strip(" ()\t")) == old_name
            for entry in tail.split(",")
            if entry.strip(" ()\t")
        )

    def _file_has_old_name_at(
        self, relative: str, sites: list[RenameSite], old_bytes: bytes
    ) -> bool:
        """Whether every site in one file carries the old name at its column.

        An unreadable file and a site whose line is gone both answer False:
        `applied=False` claims EVERY site is back, so anything that cannot be
        SHOWN restored is a no rather than a skip.
        """
        try:
            lines = (self.repo_root / relative).read_text(encoding="utf-8").splitlines()
        except OSError:
            return False
        for site in sites:
            if not 0 < site.line <= len(lines):
                return False
            # Compared as BYTES: `site.col` is a tree-sitter byte column,
            # while a decoded line is indexed by code points, so any multibyte
            # character earlier on the line shifts the two apart and the slice
            # lands mid-token (Greptile, PR #1547).
            text = lines[site.line - 1].encode("utf-8")
            # The token must sit at the recorded column: another occurrence
            # on the same line is a different symbol.
            if text[site.col : site.col + len(old_bytes)] != old_bytes:
                return False
        return True

    def _enforce_contract(
        self, report: RenameReport, new_name: str, allow_heuristic: bool
    ) -> RenameReport:
        assert self.reingest is not None
        # The pairs this rename applied, computed here rather than after the
        # measurement: the delta needs them to recognise an empty container,
        # whose identity nothing in the two snapshots can show.
        pairs = [
            (
                member,
                member.rsplit(cs.SEPARATOR_DOT, 1)[0] + cs.SEPARATOR_DOT + new_name,
            )
            for member in report.hierarchy
        ]
        try:
            delta = measure(
                self.fetch_all,
                self.project,
                self.repo_root,
                report.files,
                self.reingest,
                declared_renames=pairs,
            )
        # The transaction has landed; a graph that cannot be measured is
        # reported, never raised past the committed edit.
        except Exception as error:  # noqa: BLE001
            # The files are renamed and recorded; a graph that cannot be
            # measured is reported, never raised past the committed edit.
            logger.warning(cs.RENAME_CONTRACT_UNMEASURED.format(error=error))
            return report._replace(
                verdict=None,
                message=cs.RENAME_CONTRACT_UNMEASURED.format(error=error),
            )
        verdict = verify(
            rename_expectation(pairs, allow_heuristic),
            delta,
            rewritten=[
                (f"{site.path}:{site.line}", site.resolution)
                for site in report.sites
                if site.kind != "unlocatable"
            ],
        )
        if verdict.ok:
            return report._replace(verdict=verdict)
        reasons = "; ".join(verdict.failures)
        try:
            # This rename's own transaction, not whatever is newest: a
            # later edit stacked on it refuses the rollback instead.
            undo_transaction(self.repo_root, report.transaction_id)
        except TransactionConflict as conflict:
            logger.warning(str(conflict))
            # The conflict covers two opposite situations needing opposite
            # answers: a NEWER edit stacked on top (still applied) versus
            # another actor having already reversed this one (restored).
            if self._undo_state(report.transaction_id) == cs.RENAME_UNDO_STACKED:
                return report._replace(
                    verdict=verdict,
                    message=cs.RENAME_ROLLBACK_REFUSED.format(reasons=reasons),
                )
            # The entry is absent, which proves nothing on its own -- a
            # bounded history evicts entries whose rename is still on disk.
            # So ask the TREE, which is the thing `applied` describes, rather
            # than reasoning about history bookkeeping (Greptile, PR #1547).
            if self._old_name_is_back(report):
                return report._replace(
                    applied=False,
                    verdict=verdict,
                    message=cs.RENAME_ROLLBACK_ALREADY_UNDONE.format(reasons=reasons),
                )
            return report._replace(
                verdict=verdict,
                message=cs.RENAME_ROLLBACK_UNKNOWN.format(reasons=reasons),
            )
        try:
            self.reingest(list(report.files))
        # The files are restored; the graph may have lost the subtree the
        # re-ingest deleted before failing. Say so, never raise.
        except Exception as error:  # noqa: BLE001
            # The tree is back; the graph may have lost the subtree the
            # re-ingest deleted before failing. Say so, never raise.
            logger.warning(
                cs.RENAME_ROLLBACK_UNMEASURED.format(reasons=reasons, error=error)
            )
            return report._replace(
                applied=False,
                verdict=verdict,
                graph_incomplete=True,
                message=cs.RENAME_ROLLBACK_UNMEASURED.format(
                    reasons=reasons, error=error
                ),
            )
        return report._replace(
            applied=False,
            verdict=verdict,
            message=cs.RENAME_CONTRACT_FAILED.format(reasons=reasons),
        )


def rename(
    repo_root: Path,
    fetch_all: QueryFn,
    project_name: str,
    qualified_name: str,
    new_name: str,
    allow_heuristic: bool = False,
    dry_run: bool = False,
    verify: Callable[[StagedTree], VerificationResult | bool | None] | None = None,
    after_apply: Callable[[list[str]], None] | None = None,
    reingest: Reingest | None = None,
) -> RenameReport:
    """The op: plan (and refuse on ambiguity) or plan and apply.

    With `reingest` the applied rename is measured through the structural
    delta and undone when its postcondition contract fails (issue #1531).
    """
    renamer = Renamer(
        repo_root,
        fetch_all,
        project_name,
        verify=verify,
        after_apply=after_apply,
        reingest=reingest,
    )
    if dry_run:
        return renamer.preview(qualified_name, new_name, allow_heuristic)
    return renamer.apply(qualified_name, new_name, allow_heuristic)


def sites_for(sites: Iterable[RenameSite]) -> list[dict[str, object]]:
    return [dict(site._asdict()) for site in sites]
