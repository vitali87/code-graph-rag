"""The postcondition contract of an applied rename (issue #1531): measure
the rename through the structural delta, undo it when the contract fails,
and say whether the tree really is back when the undo cannot. Split from
`rename.py`.
"""

import re
from pathlib import Path

from loguru import logger

from .. import constants as cs
from .. import cypher_queries as cq
from .. import graph_query
from ..exceptions import ReingestAborted
from ..types_defs import QueryFn
from .contract import Reingest, measure, rename_expectation, verify
from .imports import _imported
from .rename_spec import RenameReport, RenameSite
from .transaction import TransactionConflict, load_history, undo_transaction

# The same shapes `ImportRewriter.rename_in_all` rewrites. Kept identical so
# the restoration check and the rewrite cannot disagree about what an
# `__all__` entry is (PR #1547).
_ALL_BLOCK = cs.PY_DUNDER_ALL_BLOCK_PATTERN
_ALL_ENTRY = cs.PY_DUNDER_ALL_ENTRY_PATTERN

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

# Per file, the `(offset, block_start, block_end)` of each `__all__` entry a
# rename rewrote, as `ImportRewriter.rename_in_all` returns them.
AllRewrites = dict[str, list[tuple[int, int, int]]]


def undo_state(repo_root: Path, transaction_id: str) -> str:
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
    reported as one; the caller keeps `undone=False` and is told the state
    could not be determined.

    Whether the files were actually restored is settled by
    `RestorationCheck.old_name_is_back`, which reads the tree rather than the
    history.
    """
    try:
        entries = load_history(repo_root)
    # An unreadable history cannot confirm a completed rollback.
    except Exception:  # noqa: BLE001
        return cs.RENAME_UNDO_STACKED
    if any(str(entry.get(cs.EDIT_KEY_ID, "")) == transaction_id for entry in entries):
        return cs.RENAME_UNDO_STACKED
    return cs.RENAME_UNDO_UNKNOWN


class RestorationCheck:
    """Whether the working tree shows a rename reversed, read at the sites
    and `__all__` entries it rewrote."""

    def __init__(
        self, repo_root: Path, all_rewrites: AllRewrites | None = None
    ) -> None:
        self.repo_root = repo_root
        # `__all__` entries are rewritten by `rename_in_all`, not as sites.
        self.all_rewrites: AllRewrites = (
            all_rewrites if all_rewrites is not None else {}
        )

    def old_name_is_back(self, report: RenameReport) -> bool:
        """Whether the old name has returned AT THE SITES this rename edited.

        `undone` is a claim about the working tree, so the tree settles it,
        not the history: an entry can be evicted by later edits while its
        rename stands, and once those later entries are themselves undone the
        history is short again and the eviction leaves no trace.

        Checked at the recorded SITES, not by searching the file. A
        whole-file token search for the old name matches any unrelated
        symbol that happens to share it -- an independent `helper` elsewhere
        in a touched file read as proof that this rename had been reversed
        (PR #1547). The sites are the exact positions this rename
        rewrote, so they are the only places its reversal can show.

        The test is the OLD name, not the new one: the new name is routinely
        present either way, since renaming onto an existing symbol is exactly
        what makes the contract fail. A site that cannot be read counts as
        NOT reverted, keeping `undone=False` rather than claiming a
        rollback that may not have happened.
        """
        by_path: dict[str, list[RenameSite]] = {}
        import_sites: list[RenameSite] = []
        for site in report.sites:
            # An `unlocatable` site has no usable position at all, so nothing
            # can be read at it either way.
            if site.kind == cs.RenameSiteKind.UNLOCATABLE:
                continue
            # An `import` site IS rewritten -- by `ImportRewriter.retarget`,
            # not by the identifier patcher -- so it must show a reversal like
            # any other. `_stage_sites` skips it only because its recorded
            # column points at the import STATEMENT, which the patcher cannot
            # use; reading a slice there yields `from p` and never the name.
            # Excluding it outright let a tree whose import still carried the
            # NEW name report as fully reversed (PR #1547), so it is
            # checked by LINE CONTENT instead of by column.
            if site.kind == cs.RenameSiteKind.IMPORT:
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
        # site is a PARTIAL undo, and reporting `undone=True` for it tells
        # the caller every file is back when other definitions and references
        # are still renamed (PR #1547).
        if not all(
            self.file_has_old_name_at(relative, sites, old_bytes)
            for relative, sites in by_path.items()
        ):
            return False
        if not all(
            self.import_names_the_old_name(site, report.old_name)
            for site in import_sites
        ):
            return False
        # `__all__` entries are rewritten by `rename_in_all` and are NOT
        # recorded as sites, so nothing above can see them. A tree with every
        # definition, reference and import restored but an export still
        # naming the NEW symbol is not fully reversed (PR #1547).
        return all(
            self.all_rewrites_are_undone(path, report.old_name, offsets)
            for path, offsets in sorted(self.all_rewrites.items())
        )

    def all_rewrites_are_undone(
        self, path: str, old_name: str, offsets: list[tuple[int, int, int]]
    ) -> bool:
        """Whether the exact `__all__` entries this rename rewrote read the old name.

        Checked at the recorded offsets, not by scanning the file: `__all__`
        entries are not `RenameSite`s, and only the offsets tell an entry this
        rename rewrote from one that merely matches (an aggregate over the
        file accepts partial undos or rejects complete ones; PR #1547).

        A rollback restores the file byte for byte, so each offset must still
        read the old name inside an `__all__` block at the bounds it was
        staged from. An unrelated edit that shifts a block therefore answers
        False even for a genuine restoration: the caller reports the rollback
        UNKNOWN, which is the safe error on this already-degraded path.
        """
        try:
            text = (self.repo_root / path).read_text(encoding="utf-8")
        except OSError:
            return False
        blocks = {
            (block.start(1), block.end(1))
            for block in re.finditer(_ALL_BLOCK, text, re.S)
        }
        return all(
            text[offset : offset + len(old_name)] == old_name
            and (block_start, block_end) in blocks
            and block_start <= offset < block_end
            for offset, block_start, block_end in offsets
        )

    def import_names_the_old_name(self, site: RenameSite, old_name: str) -> bool:
        """Whether the statement IMPORTS the old name again.

        Read per ENTRY rather than as a token anywhere on the line. The
        recorded column is the statement's start, so a column read is
        useless here; but a line-level search accepts a binding that is not
        the target at all -- in
        `from util import assist, helper_of_other as helper` the symbol is
        still imported as `assist`, and the `helper` on that line is an
        unrelated alias (PR #1547).

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
        # restored (PR #1547). That direction is safe -- it refuses
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

    def file_has_old_name_at(
        self, relative: str, sites: list[RenameSite], old_bytes: bytes
    ) -> bool:
        """Whether every site in one file carries the old name at its column.

        An unreadable file and a site whose line is gone both answer False:
        `undone=True` claims EVERY site is back, so anything that cannot be
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
            # lands mid-token (PR #1547).
            text = lines[site.line - 1].encode("utf-8")
            # The token must sit at the recorded column: another occurrence
            # on the same line is a different symbol.
            if text[site.col : site.col + len(old_bytes)] != old_bytes:
                return False
        return True


def declared_renames(
    fetch_all: QueryFn, project_name: str, report: RenameReport, new_name: str
) -> list[tuple[str, str]]:
    """The (old, new) qualified names the rename declares: each renamed
    member and companion, and every definition nested under one in the
    files it touched."""
    parents = [
        (
            member,
            member.rsplit(cs.SEPARATOR_DOT, 1)[0] + cs.SEPARATOR_DOT + new_name,
        )
        for member in (*report.hierarchy, *report.companions)
    ]
    pairs = list(parents)
    longer_project_prefixes = graph_query.projects_nested_under(fetch_all, project_name)
    for row in fetch_all(
        cq.CYPHER_DELTA_DEFINITIONS,
        {
            cs.KEY_PROJECT_PREFIX: f"{project_name}{cs.SEPARATOR_DOT}",
            cs.KEY_LONGER_PROJECT_PREFIXES: list(longer_project_prefixes),
            cs.CYPHER_PARAM_PATHS: list(report.files),
        },
    ):
        qn = row.get(cs.KEY_QUALIFIED_NAME)
        if not isinstance(qn, str) or row.get(cs.KEY_AST_FINGERPRINT):
            continue
        for old, new in parents:
            if qn.startswith(old + cs.SEPARATOR_DOT):
                pairs.append((qn, new + qn[len(old) :]))
    return pairs


def enforce_contract(
    report: RenameReport,
    new_name: str,
    allow_heuristic: bool,
    *,
    fetch_all: QueryFn,
    project_name: str,
    repo_root: Path,
    reingest: Reingest | None,
    all_rewrites: AllRewrites,
) -> RenameReport:
    """Measure an applied rename and undo it when its postcondition fails.

    `all_rewrites` is what staging rewrote in `__all__` lists, so a
    rollback that cannot be confirmed from the history is read off the tree.
    """
    assert reingest is not None
    try:
        pairs = declared_renames(fetch_all, project_name, report, new_name)
        delta = measure(
            fetch_all,
            project_name,
            repo_root,
            report.files,
            reingest,
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
            # Only when the re-ingest may have WRITTEN. The ROLLBACK path
            # already reported this; the initial path did not, and it is
            # the one the caller reaches while `applied` is still True --
            # so `_run_rename` saw a clean report and re-marked nothing,
            # leaving no record that the graph might be partial.
            # `verdict=None` says "could not measure", which is not the
            # same claim as "the graph may be damaged" (found by a peer
            # review session, PR #1547).
            #
            # Classified, not unconditional. `reingest` converts every
            # prologue failure to `ReingestAborted` and rejects bad paths
            # with `ValueError` before touching anything, so those two
            # mean nothing was written -- the same rule `_reingest_sync`
            # and the guarded callback apply. Setting the flag for them
            # made the caller re-mark a graph the failure never touched,
            # and every later scoped re-ingest was refused until a full
            # update (PR #1547). Matching the callback matters:
            # it CLEARS its marker on that path, so an unconditional flag
            # here re-created the marker it had just correctly removed.
            graph_incomplete=not isinstance(error, ValueError | ReingestAborted),
            message=cs.RENAME_CONTRACT_UNMEASURED.format(error=error),
        )
    verdict = verify(
        rename_expectation(pairs, allow_heuristic),
        delta,
        rewritten=[
            (f"{site.path}:{site.line}", site.resolution)
            for site in report.sites
            if site.kind != cs.RenameSiteKind.UNLOCATABLE
        ],
    )
    if verdict.ok:
        return report._replace(verdict=verdict)
    reasons = "; ".join(verdict.failures)
    report = report._replace(applied=False, verdict=verdict, undone=False)
    try:
        # This rename's own transaction, not whatever is newest: a
        # later edit stacked on it refuses the rollback instead.
        outcome = undo_transaction(repo_root, report.transaction_id)
    except TransactionConflict as conflict:
        logger.warning(str(conflict))
        # The conflict covers two opposite situations needing opposite
        # answers: a NEWER edit stacked on top (still applied) versus
        # another actor having already reversed this one (restored).
        if undo_state(repo_root, report.transaction_id) == cs.RENAME_UNDO_STACKED:
            return report._replace(
                message=cs.RENAME_ROLLBACK_REFUSED.format(
                    reasons=reasons, error=conflict
                ),
            )
        # The entry is absent, which proves nothing on its own -- a
        # bounded history evicts entries whose rename is still on disk.
        # So ask the TREE, which is the thing `undone` describes, rather
        # than reasoning about history bookkeeping (PR #1547).
        if RestorationCheck(repo_root, all_rewrites).old_name_is_back(report):
            return report._replace(
                undone=True,
                message=cs.RENAME_ROLLBACK_ALREADY_UNDONE.format(reasons=reasons),
            )
        return report._replace(
            message=cs.RENAME_ROLLBACK_UNKNOWN.format(reasons=reasons),
        )
    if not outcome.applied:
        return report._replace(
            message=cs.RENAME_ROLLBACK_REFUSED.format(
                reasons=reasons, error=outcome.message
            ),
        )
    try:
        reingest(list(report.files))
    # The files are restored; the graph may have lost the subtree the
    # re-ingest deleted before failing. Say so, never raise.
    except Exception as error:  # noqa: BLE001
        # The tree is back; the graph may have lost the subtree the
        # re-ingest deleted before failing. Say so, never raise.
        logger.warning(
            cs.RENAME_ROLLBACK_UNMEASURED.format(reasons=reasons, error=error)
        )
        return report._replace(
            undone=True,
            graph_incomplete=True,
            message=cs.RENAME_ROLLBACK_UNMEASURED.format(reasons=reasons, error=error),
        )
    return report._replace(
        undone=True,
        message=cs.RENAME_CONTRACT_FAILED.format(reasons=reasons),
    )
