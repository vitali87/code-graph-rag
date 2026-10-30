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
   at all and applying would leave it under the old name. The index misses
   sites too, so the plan is cross-checked against the source: an
   occurrence of the name in code that no site covers refuses the same way
   (issue #2564);
3. rewrite the identifier at every site through the span patcher (issue
   #1529) and the import statements through the import rewriter (issue
   #1530), stage the results in a transaction (issue #1528), verify that
   every patched file still parses (plus any verifier the caller adds, the
   postcondition contract of issue #1531 once it lands) and commit, or
   roll back and report why.

Documentation mentions (`Section` nodes, docstrings) are reported, not
rewritten: prose is not a graph edge.

This module stages and applies. The phases before it live beside it: what
a rename takes along (`rename_hierarchy`), each member's sites
(`rename_sites`, their tokens in `rename_tokens`), the plan and its
refusals (`rename_plan`), and the postcondition contract
(`rename_contract`).
"""

from collections.abc import Callable, Iterable
from pathlib import Path, PurePosixPath

from .. import constants as cs
from ..language_spec import get_language_for_extension
from ..types_defs import QueryFn
from ..utils.path_utils import python_stub_implementations
from .contract import Reingest
from .imports import ANY_MODULE, ImportRewriter, SymbolMove
from .patcher import Patcher, PatcherError
from .rename_contract import AllRewrites, enforce_contract
from .rename_plan import RenamePlanner
from .rename_spec import RenameRefused, RenameReport, RenameSite
from .transaction import EditTransaction, StagedTree, VerificationResult

__all__ = [
    "RenameRefused",
    "RenameReport",
    "RenameSite",
    "Renamer",
    "rename",
    "sites_for",
]


class Renamer(RenamePlanner):
    """Plan and apply one rename against a project's graph."""

    def __init__(
        self,
        fetch_all: QueryFn,
        project_name: str,
        repo_root: Path,
        verify: Callable[[StagedTree], VerificationResult | bool | None] | None = None,
        after_apply: Callable[[list[str]], None] | None = None,
        reingest: Reingest | None = None,
        heuristic_opt_in: str = cs.MCPParamName.ALLOW_HEURISTIC,
    ) -> None:
        super().__init__(fetch_all, project_name, repo_root, heuristic_opt_in)
        self.verify = verify
        self.after_apply = after_apply
        # With a re-ingest the rename is held to its postcondition contract
        # (issue #1531): the delta of what it wrote is measured and the
        # transaction undone when the contract fails.
        self.reingest = reingest
        # Per file, the `__all__` entries the last staging rewrote (see
        # `_stage`), which a rollback the history cannot confirm is read at.
        self._all_rewrites: AllRewrites = {}

    def _all_paths(self, hierarchy: list[str], old_name: str) -> set[str]:
        """Python modules whose `__all__` may list the name: the defining
        module of each module-level member and its `.pyi` stubs, plus the
        modules importing it."""
        paths: set[str] = set()
        for member in hierarchy:
            module_qn, module_path = self.sites.module_of(member)
            if member != f"{module_qn}{cs.SEPARATOR_DOT}{old_name}":
                continue
            if module_path:
                paths.add(module_path)
                paths.update(_stubs_of(self.repo_root, module_path))
            paths.update(
                site.path for site, _m in self.sites.import_sites(member, old_name)
            )
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
        # Beside the import rewrite it guards, before anything is patched.
        self._refuse_twin_bound_imports(report)
        patcher = Patcher(self.repo_root)
        done: set[tuple[str, int, int]] = set()
        for site in report.sites:
            key = (site.path, site.line, site.col)
            if key in done or site.kind in (
                cs.RenameSiteKind.UNLOCATABLE,
                cs.RenameSiteKind.IMPORT,
            ):
                continue
            done.add(key)
            patcher.replace_identifier_at(
                site.path, site.line, site.col, old_name, new_name
            )
        rewriter = ImportRewriter(self.repo_root, patcher)
        import_sites = [
            site
            for member in report.hierarchy
            for site, _module in self.sites.import_sites(member, old_name)
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
        # Record HOW MANY entries each file had rewritten. The restoration
        # check needs to know what this rename touched, not what the file
        # contains: a whole-file existence question cannot answer a per-site
        # one, and both earlier phrasings of that check failed for exactly
        # that reason -- "does the old name appear anywhere" accepted a
        # partial undo, and "does the new name appear anywhere" rejected a
        # complete one when the new name was ALREADY exported before the
        # rename ran (PR #1547).
        self._all_rewrites = {}
        for path in sorted(self._all_paths(list(report.hierarchy), old_name)):
            offsets = rewriter.rename_in_all(path, old_name, new_name)
            if offsets:
                self._all_rewrites[path] = offsets
        tx = EditTransaction(self.repo_root)
        try:
            results = patcher.stage_into(tx)
        except PatcherError as error:
            # A new name the file's declared encoding cannot hold (#2901).
            tx.rollback()
            raise RenameRefused(str(error), [], []) from error
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
        """Plan, patch, verify and commit; report contract rollback separately."""
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
        if outcome.applied and (reingest := self.reingest) is not None:
            report = enforce_contract(
                report,
                new_name,
                allow_heuristic,
                fetch_all=self.fetch_all,
                project_name=self.project_name,
                repo_root=self.repo_root,
                reingest=reingest,
                all_rewrites=self._all_rewrites,
            )
        if report.applied and self.after_apply is not None:
            self.after_apply(list(report.files))
        return report


def _stubs_of(repo_root: Path, path: str) -> set[str]:
    """The `.pyi` stubs on disk that declare the module `path` implements:
    `x.pyi` for `x.py`, and `x/__init__.pyi` or `x.pyi` for `x/__init__.py`."""
    implementation = PurePosixPath(path)
    candidates = [implementation.with_suffix(cs.EXT_PYI)]
    if implementation.parent.name:
        candidates.append(implementation.parent.with_suffix(cs.EXT_PYI))
    return {
        stub.as_posix()
        for stub in candidates
        if implementation in python_stub_implementations(stub)
        and (repo_root / stub).is_file()
    }


def rename(
    fetch_all: QueryFn,
    project_name: str,
    repo_root: Path,
    qualified_name: str,
    new_name: str,
    allow_heuristic: bool = False,
    dry_run: bool = False,
    verify: Callable[[StagedTree], VerificationResult | bool | None] | None = None,
    after_apply: Callable[[list[str]], None] | None = None,
    reingest: Reingest | None = None,
    heuristic_opt_in: str = cs.MCPParamName.ALLOW_HEURISTIC,
) -> RenameReport:
    """The op: plan (and refuse on ambiguity) or plan and apply.

    With `reingest` the applied rename is measured through the structural
    delta and undone when its postcondition contract fails (issue #1531).
    """
    renamer = Renamer(
        fetch_all,
        project_name,
        repo_root,
        verify=verify,
        after_apply=after_apply,
        reingest=reingest,
        heuristic_opt_in=heuristic_opt_in,
    )
    if dry_run:
        return renamer.preview(qualified_name, new_name, allow_heuristic)
    return renamer.apply(qualified_name, new_name, allow_heuristic)


def sites_for(sites: Iterable[RenameSite]) -> list[dict[str, object]]:
    return [dict(site._asdict()) for site in sites]
