"""Edit algebra op 2: `change_signature(qn, new_params, mapping)` (issue #1533).

A parameter list changed by hand is where call sites drift: one caller
missed, a positional value silently shifted into the wrong slot. The graph
knows every site, so the change is a graph operation:

1. read the definition's parameter list and, for a method on a hierarchy,
   the list of every override in both directions (they must agree);
2. work out where each new parameter's value comes from at a call site: an
   old parameter carried by name or by index, or a literal the mapping
   supplies for every site;
3. rewrite the definition(s) and every graph-known call site through the
   span patcher (issue #1529), stage the result in a transaction (issue
   #1528), and commit, or roll back and report why.

A site the mapping cannot complete -- a value the caller never passed for a
parameter without a default, a splat, a surplus argument, a keyword the
definition does not declare -- is left exactly as written and reported as
`unmapped`; so is a site the graph bound by guesswork (`heuristic`,
`overload`, `dynamic`) unless the caller accepts the risk with
`allow_heuristic`. With a re-ingest the applied change is measured through
the structural delta and undone when its postcondition contract (issue
#1531) fails: every site of the changed signature must read as mapped or
be in the unmapped list.

Python only for now: the header and the argument lists are read with the
Python grammar, and a definition in any other language refuses (issue
#1908).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path

from tree_sitter import Parser

from .. import constants as cs
from .. import graph_query
from ..graph_query import QueryFn
from ..language_spec import get_language_for_extension
from ..parser_loader import load_parsers
from .contract import Reingest
from .patcher import Patcher, PatcherError
from .signature_bind import (
    _bind_arguments,
    _body_reads,
    _check_hierarchy,
    _definition_edits,
    _find_definition,
    _params_of,
    _render_site,
    _take_receiver,
    _text,
)
from .signature_contract import enforce_contract
from .signature_spec import (
    _CALL,
    _DEFINITION,
    _IDENTIFIER_RE,
    LANGUAGES_ISSUE,
    ParamSpec,
    SignatureRefused,
    SignatureReport,
    SignatureSite,
    UnmappedSite,
    _Candidate,
    _check_literal,
    _Edit,
    _Header,
    _new_specs,
    _resolve_sources,
    _Source,
    _Unmapped,
)
from .sites import AMBIGUOUS, call_node_at, hierarchy
from .transaction import EditTransaction


class SignatureChanger:
    """Plan and apply one signature change against a project's graph."""

    def __init__(
        self,
        repo_root: Path,
        fetch_all: QueryFn,
        project_name: str,
        reingest: Reingest | None = None,
    ) -> None:
        self.repo_root = repo_root.resolve()
        self.fetch_all = fetch_all
        self.project = project_name
        # With a re-ingest the change is held to its postcondition contract
        # (issue #1531): the delta of what it wrote is measured and the
        # transaction undone when the contract fails.
        self.reingest = reingest
        parsers, _queries = load_parsers()
        self._parsers = dict(parsers)
        self._parser: Parser = self._parsers[cs.SupportedLanguage.PYTHON]

    # -- planning --

    def plan(
        self,
        qn: str,
        new_params: Sequence[str],
        mapping: Mapping[str, str] | None,
        allow_heuristic: bool,
    ) -> tuple[SignatureReport, list[_Edit]]:
        """Everything the change touches, and the edits that do it."""
        patcher = Patcher(self.repo_root, parsers=self._parsers)
        members = hierarchy(self.fetch_all, self.project, qn)
        headers = [self._header(member, patcher) for member in members]
        old = headers[0].params
        old_names = [p.name for p in old]
        _check_hierarchy(qn, headers, old_names)
        new = _new_specs(new_params, old)
        sources = _resolve_sources(new, old, mapping or {})
        for spec, source in zip(new, sources, strict=True):
            if source is not None and source.literal is not None:
                _check_literal(spec, source.literal)
        # A bare old name carries that parameter's own spelling over (each
        # override keeps its own); spelling a kept parameter out again
        # re-annotates it.
        #
        # Read from the WRITTEN text, not from spec equality: a bare name
        # resolves to the PRIMARY's spec, so an explicitly spelled `a: int`
        # that happens to match the primary compared equal and counted as
        # carried -- leaving an override declaring `a: str` un-annotated,
        # which is the opposite of what was asked (Copilot, PR #1533).
        carried = {
            text.strip()
            for text in new_params
            if _IDENTIFIER_RE.fullmatch(text.strip()) and text.strip() in old_names
        }
        # An old parameter fed to a new one of another name is renamed, and
        # the body must follow or the definition would be broken.
        renamed = [
            (old_names[source.index], spec.name)
            for spec, source in zip(new, sources, strict=True)
            if source is not None
            and source.index is not None
            and old_names[source.index] != spec.name
        ]
        # A dropped parameter the body still reads leaves an unresolved
        # name: `def f(a): return a` emptied writes `def f(): return a`,
        # which PARSES and raises NameError only when called, so neither
        # the syntax nor the arity postcondition catches it. Renamed names
        # are excluded: the body rewrite follows those (Copilot, PR #1533).
        kept = {spec.name for spec in new} | {old for old, _new in renamed}
        for header in headers:
            for dropped in (name for name in old_names if name not in kept):
                if _body_reads(header, dropped):
                    raise SignatureRefused(
                        cs.SIGNATURE_DROPPED_STILL_READ.format(
                            name=dropped, qn=header.qn
                        )
                    )
        sites: list[SignatureSite] = []
        edits: list[_Edit] = []
        for header in headers:
            edits.extend(_definition_edits(header, new, carried, renamed))
            sites.append(
                SignatureSite(
                    _DEFINITION,
                    header.path,
                    header.line,
                    header.col,
                    header.qn,
                    cs.EdgeResolution.EXACT,
                )
            )
        unmapped = self._collect_sites(
            headers, old, new, sources, allow_heuristic, patcher, sites, edits
        )
        report = SignatureReport(
            qualified_name=qn,
            old_params=tuple(old_names),
            new_params=tuple(spec.name for spec in new),
            applied=False,
            transaction_id="",
            files=(),
            sites=tuple(sites),
            unmapped=tuple(unmapped),
            hierarchy=tuple(members),
            diff="",
            message=cs.SIGNATURE_PLANNED.format(
                count=sum(1 for s in sites if s.kind == _CALL), skipped=len(unmapped)
            ),
        )
        return report, edits

    def _header(self, qn: str, patcher: Patcher) -> _Header:
        definition = graph_query.definition(self.fetch_all, self.project, qn, None)
        path = definition["path"]
        if not definition["found"] or not path:
            raise SignatureRefused(cs.RENAME_UNKNOWN.format(qn=qn))
        if get_language_for_extension(Path(path).suffix) != cs.SupportedLanguage.PYTHON:
            raise SignatureRefused(
                cs.SIGNATURE_NOT_PYTHON.format(qn=qn, path=path, issue=LANGUAGES_ISSUE)
            )
        try:
            source = patcher.source(path)
        except PatcherError as error:
            raise SignatureRefused(
                cs.SIGNATURE_DEFINITION_UNREADABLE.format(qn=qn, path=path, error=error)
            ) from error
        start = definition["start_line"] or 1
        end = definition["end_line"] or start
        name = definition["name"] or qn.rsplit(cs.SEPARATOR_DOT, 1)[-1]
        root = self._parser.parse(source).root_node
        node = _find_definition(root, name, start, end)
        params = node.child_by_field_name(cs.FIELD_PARAMETERS) if node else None
        if node is None or params is None:
            raise SignatureRefused(cs.SIGNATURE_NO_HEADER.format(qn=qn, path=path))
        specs = _params_of(params, source, qn)
        receiver = _take_receiver(qn, definition["label"], specs, node, source)
        return _Header(
            qn,
            path,
            (params.start_byte, params.end_byte),
            params.start_point[0] + 1,
            params.start_point[1],
            receiver,
            specs,
            node,
            source,
        )

    def _collect_sites(
        self,
        headers: Sequence[_Header],
        old: Sequence[ParamSpec],
        new: Sequence[ParamSpec],
        sources: Sequence[_Source | None],
        allow_heuristic: bool,
        patcher: Patcher,
        sites: list[SignatureSite],
        edits: list[_Edit],
    ) -> list[UnmappedSite]:
        unmapped: list[UnmappedSite] = []
        candidates = self._candidates(headers, old, allow_heuristic, patcher, unmapped)
        # Innermost first, so a site nested in another's arguments (or a
        # body rename inside a recursive call) is already planned when the
        # enclosing site folds it into its value text.
        order = sorted(
            range(len(candidates)),
            key=lambda i: (
                candidates[i].site.path,
                candidates[i].span[1] - candidates[i].span[0],
                candidates[i].span[0],
            ),
        )
        mapped: set[int] = set()
        for index in order:
            candidate = candidates[index]
            try:
                edit = _render_site(candidate, new, sources, edits)
            except _Unmapped as skip:
                unmapped.append(
                    UnmappedSite(
                        candidate.site.owner,
                        candidate.site.path,
                        candidate.site.line,
                        candidate.site.col,
                        str(skip),
                    )
                )
                continue
            mapped.add(index)
            # Into the plan at once: an enclosing site folds it in.
            if edit is not None:
                edits.append(edit)
        # The report lists sites in the order the graph gave them.
        sites.extend(c.site for i, c in enumerate(candidates) if i in mapped)
        return unmapped

    def _candidates(
        self,
        headers: Sequence[_Header],
        old: Sequence[ParamSpec],
        allow_heuristic: bool,
        patcher: Patcher,
        unmapped: list[UnmappedSite],
    ) -> list[_Candidate]:
        """Every graph-known call site the mapping can read, once each."""
        candidates: list[_Candidate] = []
        seen: set[tuple[object, ...]] = set()
        for header in headers:
            for row in graph_query.callers(self.fetch_all, self.project, header.qn):
                # Chained calls share a start and differ in their end, so
                # the whole span identifies a site; sites without a
                # location are each listed, since nothing tells them apart.
                key = (
                    row["path"],
                    row["line"],
                    row["col"],
                    row["end_line"],
                    row["end_col"],
                )
                if None in key[:3] or key not in seen:
                    seen.add(key)
                    self._consider(
                        row, old, allow_heuristic, patcher, candidates, unmapped
                    )
        return candidates

    def _consider(
        self,
        row: graph_query.CallSiteRow,
        old: Sequence[ParamSpec],
        allow_heuristic: bool,
        patcher: Patcher,
        candidates: list[_Candidate],
        unmapped: list[UnmappedSite],
    ) -> None:
        try:
            candidates.append(self._candidate(row, old, allow_heuristic, patcher))
        except _Unmapped as skip:
            unmapped.append(
                UnmappedSite(
                    row["qualified_name"],
                    row["path"] or "",
                    row["line"],
                    row["col"],
                    str(skip),
                )
            )

    def _candidate(
        self,
        row: graph_query.CallSiteRow,
        old: Sequence[ParamSpec],
        allow_heuristic: bool,
        patcher: Patcher,
    ) -> _Candidate:
        path, line, col = row["path"], row["line"], row["col"]
        resolution = row["resolution"]
        if path is None or line is None or col is None:
            raise _Unmapped(
                cs.SIGNATURE_SITE_NO_LOCATION.format(
                    resolution=resolution or cs.EdgeResolution.DYNAMIC.value
                )
            )
        if resolution in AMBIGUOUS and not allow_heuristic:
            raise _Unmapped(cs.SIGNATURE_SITE_GUESSED.format(resolution=resolution))
        if get_language_for_extension(Path(path).suffix) != cs.SupportedLanguage.PYTHON:
            raise _Unmapped(cs.SIGNATURE_SITE_NOT_PYTHON.format(path=path))
        try:
            source = patcher.source(path)
        except PatcherError as error:
            raise _Unmapped(
                cs.SIGNATURE_SITE_UNREADABLE_FILE.format(error=error)
            ) from error
        end_line, end_col = row["end_line"], row["end_col"]
        recorded_end = (
            (end_line - 1, end_col)
            if end_line is not None and end_col is not None
            else None
        )
        root = self._parser.parse(source).root_node
        call = call_node_at(root, line, col, recorded_end)
        if call is None:
            raise _Unmapped(cs.SIGNATURE_SITE_NO_CALL)
        args = call.child_by_field_name(cs.FIELD_ARGUMENTS)
        if args is None or args.type != cs.TS_ARGUMENT_LIST:
            # `helper(x for x in xs)`: a generator, not a list of values.
            raise _Unmapped(
                cs.SIGNATURE_SITE_UNREADABLE.format(text=_text(args or call, source))
            )
        bound = _bind_arguments(args, source, old)
        site = SignatureSite(_CALL, path, line, col, row["qualified_name"], resolution)
        return _Candidate(
            site, (args.start_byte, args.end_byte), _text(args, source), bound
        )

    # -- staging and applying --

    def _stage(
        self, edits: Iterable[_Edit]
    ) -> tuple[EditTransaction, dict[str, object], list[str]]:
        """Patch every edit into a transaction; nothing touches the tree."""
        patcher = Patcher(self.repo_root, parsers=self._parsers)
        tx = EditTransaction(self.repo_root)
        try:
            for edit in edits:
                patcher.replace_span(edit.path, edit.span, edit.text)
            results = patcher.stage_into(tx)
        except PatcherError as error:
            # A plan the patcher cannot apply (an overlap the folding did
            # not foresee) is a refusal, never a traceback past the tool.
            tx.rollback()
            raise SignatureRefused(
                cs.SIGNATURE_STAGE_FAILED.format(error=error)
            ) from error
        broken = [key for key, result in results.items() if result.parses is False]
        return tx, dict(results), broken

    def preview(
        self,
        qn: str,
        new_params: Sequence[str],
        mapping: Mapping[str, str] | None,
        allow_heuristic: bool,
    ) -> SignatureReport:
        """Plan and stage, return the diff, and leave the tree untouched."""
        report, edits = self.plan(qn, new_params, mapping, allow_heuristic)
        tx, results, broken = self._stage(edits)
        try:
            diff = tx.diff()
        finally:
            tx.rollback()
        message = (
            cs.SIGNATURE_PARSE_FAILED.format(
                files=cs.SEPARATOR_COMMA_SPACE.join(broken)
            )
            if broken
            else report.message
        )
        return report._replace(files=tuple(sorted(results)), diff=diff, message=message)

    def apply(
        self,
        qn: str,
        new_params: Sequence[str],
        mapping: Mapping[str, str] | None,
        allow_heuristic: bool,
    ) -> SignatureReport:
        """Plan, patch, verify and commit; the tree is untouched on failure."""
        report, edits = self.plan(qn, new_params, mapping, allow_heuristic)
        tx, results, broken = self._stage(edits)
        if broken:
            tx.rollback()
            return report._replace(
                files=tuple(sorted(results)),
                message=cs.SIGNATURE_PARSE_FAILED.format(
                    files=cs.SEPARATOR_COMMA_SPACE.join(broken)
                ),
            )
        outcome = tx.commit()
        report = report._replace(
            applied=outcome.applied,
            transaction_id=outcome.transaction_id,
            files=outcome.files,
            diff=outcome.diff,
            message=outcome.message,
        )
        if outcome.applied and self.reingest is not None:
            report = enforce_contract(
                report,
                allow_heuristic,
                fetch_all=self.fetch_all,
                project=self.project,
                repo_root=self.repo_root,
                reingest=self.reingest,
            )
        return report


def change_signature(
    repo_root: Path,
    fetch_all: QueryFn,
    project_name: str,
    qualified_name: str,
    new_params: Sequence[str],
    mapping: Mapping[str, str] | None = None,
    allow_heuristic: bool = False,
    dry_run: bool = False,
    reingest: Reingest | None = None,
) -> SignatureReport:
    """The op: plan (and refuse what cannot be stated) or plan and apply.

    With `reingest` the applied change is measured through the structural
    delta and undone when its postcondition contract fails (issue #1531).
    """
    changer = SignatureChanger(repo_root, fetch_all, project_name, reingest=reingest)
    if dry_run:
        return changer.preview(qualified_name, new_params, mapping, allow_heuristic)
    return changer.apply(qualified_name, new_params, mapping, allow_heuristic)
