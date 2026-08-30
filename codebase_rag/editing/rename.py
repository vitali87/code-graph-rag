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
from .contract import Reingest, Verdict, measure, rename_expectation, verify
from .imports import ANY_MODULE, ImportRewriter, ImportSite, SymbolMove
from .patcher import Patcher, PatcherError, line_col_to_byte
from .transaction import (
    EditTransaction,
    StagedTree,
    VerificationResult,
    undo_last,
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


def _last_identifier(
    source: bytes, line: int, col: int, end_line: int, end_col: int, name: str
) -> tuple[int, int] | None:
    """(line, col) of the LAST `name` token inside a site span.

    A call site spans `pkg.helper(1)` or `obj.method(x)`; the token to
    rename is the rightmost occurrence of the name before the arguments.
    """
    start = line_col_to_byte(source, line, col)
    end = line_col_to_byte(source, end_line, end_col)
    text = source[start:end].decode(cs.ENCODING_UTF8, errors="replace")
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
        for row in self.fetch_all(
            cq.CYPHER_GRAPH_REFERENCES,
            {cs.KEY_PROJECT_PREFIX: f"{self.project}{cs.SEPARATOR_DOT}", cs.KEY_QN: qn},
        ):
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
            if resolution_text in _AMBIGUOUS:
                # A trace-only or guessed edge with no site: it cannot be
                # rewritten, so it must refuse like any other guess.
                sites.append(
                    RenameSite(
                        "unlocatable",
                        path if isinstance(path, str) else "",
                        0,
                        0,
                        owner,
                        resolution_text,
                    )
                )
            return
        try:
            source = patcher.source(path)
        except PatcherError:
            unlocatable.append(
                cs.RENAME_UNLOCATABLE_SITE.format(
                    owner=owner, resolution="missing file"
                )
            )
            return
        token = _last_identifier(
            source,
            line,
            col,
            end_line if isinstance(end_line, int) else line,
            end_col if isinstance(end_col, int) else col + len(old_name),
            old_name,
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
        ambiguous = [s for s in sites if s.resolution in _AMBIGUOUS]
        if ambiguous and not allow_heuristic:
            raise RenameRefused(
                cs.RENAME_AMBIGUOUS.format(qn=qn, count=len(ambiguous)),
                ambiguous,
                unlocatable,
            )
        return RenameReport(
            qualified_name=qn,
            old_name=old_name,
            new_name=new_name,
            applied=False,
            transaction_id="",
            files=tuple(sorted({s.path for s in sites})),
            sites=tuple(sites),
            ambiguous=tuple(ambiguous),
            unlocatable=tuple(unlocatable),
            doc_mentions=tuple(self._doc_mentions(old_name)),
            hierarchy=tuple(hierarchy),
            diff="",
            message=cs.RENAME_PLANNED.format(count=len(sites)),
        )

    def apply(
        self, qn: str, new_name: str, allow_heuristic: bool = False
    ) -> RenameReport:
        """Plan, patch, verify and commit; the tree is untouched on failure."""
        report = self.plan(qn, new_name, allow_heuristic)
        old_name = report.old_name
        patcher = Patcher(self.repo_root)
        done: set[tuple[str, int, int]] = set()
        for site in report.sites:
            key = (site.path, site.line, site.col)
            if key in done or site.kind == "unlocatable":
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
        # module re-exporting the name (a package `__init__`).
        all_paths = {site.path for site in import_sites}
        for member in report.hierarchy:
            _module_qn, module_path = self._module_of(member)
            if module_path:
                all_paths.add(module_path)
        for path in sorted(all_paths):
            if (
                get_language_for_extension(Path(path).suffix)
                == cs.SupportedLanguage.PYTHON
            ):
                rewriter.rename_in_all(path, old_name, new_name)
        tx = EditTransaction(self.repo_root)
        results = patcher.stage_into(tx)
        broken = [key for key, result in results.items() if result.parses is False]
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

    def _enforce_contract(
        self, report: RenameReport, new_name: str, allow_heuristic: bool
    ) -> RenameReport:
        assert self.reingest is not None
        delta = measure(
            self.fetch_all, self.project, self.repo_root, report.files, self.reingest
        )
        pairs = [
            (
                member,
                member.rsplit(cs.SEPARATOR_DOT, 1)[0] + cs.SEPARATOR_DOT + new_name,
            )
            for member in report.hierarchy
        ]
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
        undo_last(self.repo_root)
        self.reingest(list(report.files))
        return report._replace(
            applied=False,
            verdict=verdict,
            message=cs.RENAME_CONTRACT_FAILED.format(
                reasons="; ".join(verdict.failures)
            ),
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
        return renamer.plan(qualified_name, new_name, allow_heuristic)
    return renamer.apply(qualified_name, new_name, allow_heuristic)


def sites_for(sites: Iterable[RenameSite]) -> list[dict[str, object]]:
    return [dict(site._asdict()) for site in sites]
