"""Edit algebra op 1: `rename(qn, new_name)` end to end (issue #1532).

A repo-wide rename by text replacement is what agents get wrong: partial
renames, same-named symbols of another language, dynamic sites nobody saw.
The graph knows every site, so a rename is a graph operation:

1. collect the definition's name token, every call, reference and
   construction site (with its `resolution`, issue #1526), every import
   statement binding the symbol (issue #1522) and, for a method on a
   hierarchy, the same for each overriding and overridden method;
2. refuse when a site is ambiguous (`heuristic`, `overload`, `dynamic`)
   unless the caller accepts the risk with `allow_heuristic`; unlocatable
   dynamic sites are always listed, they cannot be rewritten at all;
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

from tree_sitter import Node

from .. import constants as cs
from .. import cypher_queries as cq
from .. import graph_query
from ..language_spec import get_language_for_extension
from ..parser_loader import load_parsers
from ..types_defs import PropertyDict, ResultRow
from ..utils.path_utils import base_module_qn
from .imports import ANY_MODULE, ImportRewriter, ImportSite, SymbolMove
from .patcher import Patcher, PatcherError, line_col_to_byte
from .transaction import EditTransaction, StagedTree, VerificationResult

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


def _callee_span(
    source: bytes, language: cs.SupportedLanguage | None, line: int, col: int
) -> tuple[int, int] | None:
    """Byte span of the callee expression of the OUTERMOST call at (line, col).

    `helper(helper(1))` and `helper(2).upper()` both start at the same point
    as an inner call; the site's call is the outermost one, and its callee
    is everything before its own argument list.
    """
    if language is None:
        return None
    parsers, _queries = load_parsers()
    parser = parsers.get(language)
    if parser is None:
        return None
    root = parser.parse(source).root_node
    best: Node | None = None
    stack: list[Node] = [root]
    while stack:
        node = stack.pop()
        if node.start_point == (line - 1, col):
            func = node.child_by_field_name(cs.FIELD_FUNCTION)
            if func is not None and (best is None or node.end_byte > best.end_byte):
                best = node
        if node.start_point[0] <= line - 1 <= node.end_point[0]:
            stack.extend(node.children)
    if best is None:
        return None
    func = best.child_by_field_name(cs.FIELD_FUNCTION)
    assert func is not None
    return func.start_byte, func.end_byte


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
    callee = _callee_span(source, language, line, col)
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
    ) -> None:
        self.repo_root = repo_root.resolve()
        self.fetch_all = fetch_all
        self.project = project_name
        self.verify = verify
        self.after_apply = after_apply

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
        source = patcher.source(path)
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
            unlocatable.append(
                cs.RENAME_UNLOCATABLE_SITE.format(
                    owner=owner, resolution=resolution_text or "unknown"
                )
            )
            # A graph-known edge with no site cannot be rewritten, whatever
            # bound it: applying anyway would leave that caller under the
            # old name, so it blocks like a guess does.
            sites.append(
                RenameSite(
                    "unlocatable",
                    path if isinstance(path, str) else "",
                    0,
                    0,
                    owner,
                    resolution_text or _SITELESS,
                )
            )
            return
        try:
            source = patcher.source(path)
        except PatcherError:
            # The graph knows this occurrence but its file cannot be read:
            # renaming around it would leave it under the old name.
            unlocatable.append(
                cs.RENAME_UNLOCATABLE_SITE.format(
                    owner=owner, resolution="missing file"
                )
            )
            sites.append(RenameSite("unlocatable", path, line, col, owner, _SITELESS))
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

    def _import_sites(self, qn: str, old_name: str) -> list[tuple[ImportSite, str]]:
        module_qn, _path = self._module_of(qn)
        out: list[tuple[ImportSite, str]] = []
        if qn != f"{module_qn}{cs.SEPARATOR_DOT}{old_name}":
            # Only a module-level member is imported by name; a method or a
            # nested definition shares its name with nothing an importer
            # can bind, so `from pkg.util import get` stays as it is.
            return out
        for row in graph_query.importers(self.fetch_all, self.project, module_qn):
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
        structural = [s for s in sites if s.resolution == _STRUCTURAL]
        if structural:
            raise RenameRefused(
                cs.RENAME_STRUCTURAL_UNLOCATABLE.format(qn=qn, count=len(structural)),
                structural,
                unlocatable,
            )
        siteless = [
            s
            for s in sites
            if s.kind == "unlocatable" and s.resolution not in _AMBIGUOUS
        ]
        if siteless:
            raise RenameRefused(
                cs.RENAME_SITELESS.format(qn=qn, count=len(siteless)),
                siteless,
                unlocatable,
            )
        ambiguous = [s for s in sites if s.resolution in _AMBIGUOUS]
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
            files=tuple(
                sorted({s.path for s in sites} | self._all_paths(hierarchy, old_name))
            ),
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
        if outcome.applied and self.after_apply is not None:
            self.after_apply(list(outcome.files))
        return report._replace(
            applied=outcome.applied,
            transaction_id=outcome.transaction_id,
            files=outcome.files,
            diff=outcome.diff,
            message=outcome.message,
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
) -> RenameReport:
    """The op: plan (and refuse on ambiguity) or plan and apply."""
    renamer = Renamer(
        repo_root, fetch_all, project_name, verify=verify, after_apply=after_apply
    )
    if dry_run:
        return renamer.preview(qualified_name, new_name, allow_heuristic)
    return renamer.apply(qualified_name, new_name, allow_heuristic)


def sites_for(sites: Iterable[RenameSite]) -> list[dict[str, object]]:
    return [dict(site._asdict()) for site in sites]
