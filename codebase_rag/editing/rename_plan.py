"""Planning a rename (issue #1532): every site of each member of the
symbol's hierarchy, refused when one is ambiguous or cannot be rewritten,
and cross-checked against the source for occurrences the graph never saw
(issue #2564). Split from `rename.py`.
"""

import re
from pathlib import Path, PurePosixPath
from typing import NamedTuple

from loguru import logger

from .. import constants as cs
from .. import cypher_queries as cq
from .. import graph_query
from .. import logs as ls
from ..language_spec import get_language_for_extension
from ..types_defs import QueryFn
from ..utils.path_utils import base_module_qn, declaration_implementations
from .imports import ImportSite
from .occurrences import (
    Occurrence,
    Target,
    binding_scope,
    declares_target,
    find_occurrences,
)
from .patcher import PatcherError
from .rename_hierarchy import go_implementers_unknown, rename_hierarchy
from .rename_sites import SiteCollector
from .rename_spec import (
    CHAIN,
    IDENTIFIER_RE,
    RECEIVER_UNKNOWN,
    STRUCTURAL,
    TS_LANGUAGES,
    TYPE_SPACE_LABELS,
    UNPLANNED,
    RenameRefused,
    RenameReport,
    RenameSite,
)
from .sites import AMBIGUOUS

# A file and the (line, col) start and exclusive end of a statement in it.
_Span = tuple[str, tuple[int, int], tuple[int, int]]


class _Namesakes(NamedTuple):
    positions: set[tuple[str, int, int]]
    statements: list[_Span]
    # Where an import binds the bare name to the other symbol.
    bound: list[_Span]
    # The symbols that are the target's own declaration in a `.d.ts`, each
    # with where it is declared.
    companions: dict[str, tuple[str, int, int]]


class RenamePlanner:
    """Plan one rename against a project's graph: what it rewrites, or why
    it refuses."""

    def __init__(
        self,
        fetch_all: QueryFn,
        project_name: str,
        repo_root: Path,
        *,
        heuristic_opt_in: str = cs.MCPParamName.ALLOW_HEURISTIC,
    ) -> None:
        self.repo_root = repo_root.resolve()
        self.fetch_all = fetch_all
        self.project_name = project_name
        # How the caller spells the opt-in the heuristic refusal tells the
        # user to pass: a CLI flag is not an MCP parameter (issue #2886).
        self.heuristic_opt_in = heuristic_opt_in
        self.sites = SiteCollector(fetch_all, project_name, self.repo_root)

    def _refuse_twin_bound_imports(self, report: RenameReport) -> None:
        """Refuse when an import the rename rewrites also binds a type.

        A TS `type input` and `function input` share one name (issue #2520),
        and `import { input }` binds both. Rewriting it to the function's new
        name leaves the file's `input<number>` annotation unbound, while
        keeping the old name beside it would need an import the rewriter
        does not write; the rename refuses instead.
        """
        bound = [
            site
            for member in report.hierarchy
            for site in self._twin_bound_imports(member, report.old_name)
        ]
        if bound:
            raise RenameRefused(
                cs.RENAME_TWIN_BOUND_IMPORT.format(
                    qn=report.qualified_name,
                    name=report.old_name,
                    paths=", ".join(sorted({site.path for site in bound})),
                ),
                bound,
                list(report.unlocatable),
            )

    def _twin_bound_imports(self, qn: str, old_name: str) -> list[RenameSite]:
        """The imports of `qn` in TS files that also use its type-space twin."""
        typed = [
            RenameSite(
                cs.RenameSiteKind.IMPORT,
                site.path,
                site.line,
                site.col,
                module,
                cs.EdgeResolution.EXACT,
            )
            for site, module in self.sites.import_sites(qn, old_name)
            if get_language_for_extension(Path(site.path).suffix) in TS_LANGUAGES
        ]
        if not typed:
            return []
        label = graph_query.definition(self.fetch_all, self.project_name, qn, None)[
            "label"
        ]
        if label is None or label in TYPE_SPACE_LABELS:
            return []
        owns = graph_query.owner_check(self.fetch_all, self.project_name)
        paths: set[str] = set()
        for twin_label in TYPE_SPACE_LABELS:
            params = {
                cs.KEY_PROJECT_PREFIX: f"{self.project_name}{cs.SEPARATOR_DOT}",
                cs.KEY_QN: qn,
                cs.KEY_LABEL: twin_label,
            }
            for query in (cq.CYPHER_GRAPH_TYPE_EDGES, cq.CYPHER_GRAPH_REFERENCES):
                paths.update(
                    path
                    for row in self.fetch_all(query, params)
                    if owns(str(row.get(cs.KEY_QUALIFIED_NAME) or ""))
                    and isinstance(path := row.get(cs.KEY_PATH), str)
                )
        # `implements X` names only the type side, and that edge is not one of
        # the label-scoped reads above.
        paths.update(
            row["path"]
            for row in graph_query.implementors(self.fetch_all, self.project_name, qn)
            if row["relationship"] == cs.RelationshipType.IMPLEMENTS
            and row["path"] is not None
        )
        return [site for site in typed if site.path in paths]

    def _doc_mentions(self, old_name: str) -> list[str]:
        pattern = re.compile(IDENTIFIER_RE % re.escape(old_name))
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
            except OSError as error:
                # Mentions are reported, not rewritten, so an unreadable
                # document only leaves the report short, and says so.
                logger.warning(
                    ls.RENAME_DOC_UNREADABLE.format(
                        path=path, name=old_name, error=error
                    )
                )
                continue
            for number, text in enumerate(lines, 1):
                if pattern.search(text):
                    found.append(
                        f"{path.relative_to(self.repo_root).as_posix()}:{number}"
                    )
        return found

    def plan(
        self, qn: str, new_name: str, allow_heuristic: bool = False
    ) -> RenameReport:
        """Collect everything a rename touches; refuse on ambiguity."""
        if not re.fullmatch(r"[A-Za-z_]\w*", new_name):
            raise RenameRefused(cs.RENAME_BAD_NAME.format(name=new_name), [], [])
        found = rename_hierarchy(self.fetch_all, self.project_name, qn, self.repo_root)
        members = found.members
        self._refuse_go_implementers_unknown(qn, members)
        sites, unlocatable, old_name, label = self._collect_members(
            members, found.guessed
        )
        _refuse_unrewritable(qn, sites, unlocatable)
        ambiguous = [
            s for s in sites if s.resolution in AMBIGUOUS or s.resolution == CHAIN
        ]
        imports = [
            (site, module)
            for member in members
            for site, module in self.sites.import_sites(member, old_name)
        ]
        unplanned, companions = self._unplanned(
            sites, [site for site, _module in imports], members, old_name, label
        )
        unknown = [s for s in unplanned if s.resolution == RECEIVER_UNKNOWN]
        if unknown and allow_heuristic:
            raise RenameRefused(
                cs.RENAME_RECEIVER_UNKNOWN.format(
                    qn=qn,
                    count=len(unknown),
                    name=old_name,
                    locations=_locations(unknown),
                    option=self.heuristic_opt_in,
                ),
                ambiguous,
                unlocatable,
                unplanned,
            )
        if (ambiguous or unplanned) and not allow_heuristic:
            raise RenameRefused(
                _refusal_message(
                    qn,
                    old_name,
                    ambiguous,
                    unplanned,
                    len(unknown),
                    self.heuristic_opt_in,
                ),
                ambiguous,
                unlocatable,
                unplanned,
            )
        sites.extend(unplanned)
        sites.extend(
            RenameSite(
                cs.RenameSiteKind.IMPORT,
                site.path,
                site.line,
                site.col,
                module,
                cs.EdgeResolution.EXACT,
            )
            for site, module in imports
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
            hierarchy=tuple(members),
            diff="",
            message=cs.RENAME_PLANNED.format(count=len(sites)),
            unplanned=tuple(unplanned),
            companions=companions,
        )

    def _refuse_go_implementers_unknown(self, qn: str, members: list[str]) -> None:
        """Refuse when a Go interface whose implementers cgr cannot tell
        declares the members' name (#3360): the hierarchy cannot show what
        the rename must take along."""
        unknown = go_implementers_unknown(self.fetch_all, self.project_name, members)
        if unknown is None:
            return
        interface, why, name = unknown
        raise RenameRefused(
            cs.RENAME_GO_IMPLEMENTERS_UNKNOWN.format(
                qn=qn,
                interface=interface,
                name=name,
                why=cs.RENAME_GO_IMPLEMENTERS_UNKNOWN_WHY.get(
                    why, cs.RENAME_GO_IMPLEMENTERS_UNKNOWN_WHY_OTHER
                ),
            ),
            [],
            [],
        )

    def _collect_members(
        self, members: list[str], guessed: frozenset[str]
    ) -> tuple[list[RenameSite], list[str], str, str | None]:
        """Every member's sites and unlocatable reasons, with the name and
        label the members share; a chain link another member's row covers
        is that row's, not a guess."""
        # `rename_hierarchy` lists `qn` first, so the first member names the
        # symbol; a later one stands in only where the ones before had none.
        first, *others = members
        sites, unlocatable, old_name, label = self.sites.collect(first, guessed)
        for member in others:
            member_sites, member_unlocatable, member_name, member_label = (
                self.sites.collect(member, guessed)
            )
            old_name = old_name or member_name
            label = label or member_label
            sites.extend(member_sites)
            unlocatable.extend(member_unlocatable)
        covered = {(s.path, s.line, s.col) for s in sites if s.resolution != CHAIN}
        sites = [
            s
            for s in sites
            if s.resolution != CHAIN or (s.path, s.line, s.col) not in covered
        ]
        return sites, unlocatable, old_name, label

    def _unplanned(
        self,
        sites: list[RenameSite],
        imports: list[ImportSite],
        hierarchy: list[str],
        old_name: str,
        label: str | None,
    ) -> tuple[list[RenameSite], tuple[str, ...]]:
        """Occurrences of `old_name` in code that the plan does not cover,
        and the graph's symbols for the target's declarations among them
        (see `RenameReport.companions`).

        The graph's sites are the plan, so an occurrence none of them covers
        is one the index never saw: renaming around it leaves it under the
        old name, and the build breaks while the rename reports success
        (issue #2564). An occurrence the graph gives to ANOTHER symbol of the
        same name is not one: that symbol keeps its name.
        """
        definitions = [s for s in sites if s.kind == cs.RenameSiteKind.DEFINITION]
        language = get_language_for_extension(Path(definitions[0].path).suffix)
        if language is None:
            return [], ()
        target = self._target(definitions, hierarchy, old_name, label, language)
        planned = {(s.path, s.line, s.col) for s in sites}
        statements = [_statement(site) for site in imports]
        leftover = [
            occurrence
            for occurrence in find_occurrences(self.repo_root, target)
            if (occurrence.path, occurrence.line, occurrence.col) not in planned
            and not _inside(occurrence, statements)
        ]
        if not leftover:
            return [], ()
        foreign = self._foreign_paths(hierarchy, label)
        chosen = (
            self._chosen_paths(hierarchy, old_name)
            if language in cs.JS_TS_LANGUAGES
            and target.kind is not cs.RenameTargetKind.METHOD
            else set()
        )
        namesakes = self._namesakes(old_name, hierarchy, target)
        unplanned = [
            _unplanned_site(occurrence, self.project_name)
            for occurrence in leftover
            if _unclaimed(occurrence, foreign, namesakes, chosen)
        ]
        positions = {(s.path, s.line, s.col) for s in unplanned}
        companions = tuple(
            other
            for other, position in namesakes.companions.items()
            if position in positions
        )
        return unplanned, companions

    def _target(
        self,
        definitions: list[RenameSite],
        hierarchy: list[str],
        old_name: str,
        label: str | None,
        language: cs.SupportedLanguage,
    ) -> Target:
        """What the cross-check looks for: `old_name` in `language` as the
        target's kind uses it, declared where the hierarchy's definitions
        are and, for a method, in classes other symbols may be named like."""
        kind = _target_kind(label)
        owners = _owners(hierarchy)
        owner_paths = frozenset(s.path for s in definitions)
        return Target(
            old_name,
            language,
            kind,
            definitions[0].path,
            owners,
            owner_paths,
            self._rivals(hierarchy, owner_paths)
            if kind is cs.RenameTargetKind.METHOD
            else frozenset(),
            frozenset((s.path, s.line, s.col) for s in definitions),
        )

    def _rivals(
        self, hierarchy: list[str], owner_paths: frozenset[str]
    ) -> frozenset[tuple[str, str]]:
        """Every other symbol of the project named like a class a method is
        declared in, with its file: what `Cache` may mean when it is not
        the method's class."""
        classes = {_owner_class(member) for member in hierarchy}
        rivals: set[tuple[str, str]] = set()
        for name in _owners(hierarchy):
            for symbol in graph_query.resolve(self.fetch_all, self.project_name, name):
                path = symbol["path"]
                if (
                    path is not None
                    and path not in owner_paths
                    and symbol["qualified_name"] not in classes
                    and not _declares_one_of(path, owner_paths)
                ):
                    rivals.add((name, path))
        return frozenset(rivals)

    def _chosen_paths(self, hierarchy: list[str], old_name: str) -> set[str]:
        """JS/TS files whose bare `old_name` is a name of their own for the
        target: a default import (`import mul from`), or a barrel's chosen
        name for it (`export { default as Widget } from`) imported from
        that barrel. The plan leaves those files' sites as they are
        (`_js_import_bound`), so the cross-check leaves their bare
        occurrences too; a file that also imports the target by its own
        name is still held to it."""
        paths: set[str] = set()
        for member in hierarchy:
            by_name, aliased, _star_bound = self.sites.js_name_bindings(
                member, old_name
            )
            paths.update(aliased - by_name)
        return paths

    def _foreign_paths(self, hierarchy: list[str], label: str | None) -> set[str]:
        """Files whose sites the graph gives to a project whose name extends
        this one: the plan leaves them alone (#1989), so its cross-check
        does too. The reads are scoped to the target's label as the plan's
        are, since a TS type and a value share one name (issue #2520)."""
        owns = graph_query.owner_check(self.fetch_all, self.project_name)
        paths: set[str] = set()
        for member in hierarchy:
            params = {
                cs.KEY_PROJECT_PREFIX: f"{self.project_name}{cs.SEPARATOR_DOT}",
                cs.KEY_QN: member,
                cs.KEY_LABEL: label,
            }
            for query in (
                cq.CYPHER_GRAPH_CALLERS,
                cq.CYPHER_GRAPH_REFERENCES,
                cq.CYPHER_GRAPH_TYPE_EDGES,
            ):
                paths.update(
                    path
                    for row in self.fetch_all(query, params)
                    if not owns(str(row.get(cs.KEY_QUALIFIED_NAME) or ""))
                    and isinstance(path := row.get(cs.KEY_PATH), str)
                )
        return paths

    def _namesakes(
        self, old_name: str, hierarchy: list[str], target: Target
    ) -> _Namesakes:
        """Where the graph writes `old_name` for a symbol outside `hierarchy`:
        each same-named definition's own name, every site the graph gives it
        and the import statements binding it, as a rename of that symbol
        would collect them. A symbol that is the target's declaration in a
        `.d.ts` beside its module is the target's, not another one."""
        namesakes = _Namesakes(set(), [], [], {})
        for symbol in graph_query.resolve(self.fetch_all, self.project_name, old_name):
            other = symbol["qualified_name"]
            if other in hierarchy:
                continue
            try:
                other_sites, _unlocatable, _name, _label = self.sites.collect(other)
                other_imports = self.sites.import_sites(other, old_name)
            except (RenameRefused, PatcherError) as error:
                # Its definition or one of its sites cannot be located in the
                # tree: what it would cover stays unplanned, since refusing
                # over a namesake's stale graph is safer than crediting it
                # with an occurrence that may be ours.
                logger.debug(ls.RENAME_NAMESAKE_UNLOCATED.format(qn=other, error=error))
                continue
            declared = next(
                (s for s in other_sites if s.kind == cs.RenameSiteKind.DEFINITION), None
            )
            if declared is not None and declares_target(
                self.repo_root, target, declared.path, declared.line, declared.col
            ):
                namesakes.companions[other] = (
                    declared.path,
                    declared.line,
                    declared.col,
                )
                continue
            namesakes.positions.update((s.path, s.line, s.col) for s in other_sites)
            for site, _module in other_imports:
                namesakes.statements.append(_statement(site))
                # `from other import helper` makes every bare `helper` where
                # it binds the other symbol's: the whole file at module
                # level, the function around it inside one.
                if site.alias == old_name and (
                    scope := binding_scope(
                        self.repo_root, site.path, site.line, site.col
                    )
                ):
                    namesakes.bound.append((site.path, scope[0], scope[1]))
        return namesakes


def _declares_one_of(path: str, implementations: frozenset[str]) -> bool:
    """Whether `path` is the declaration file (`.pyi`, `.d.ts`) of one of
    `implementations`."""
    return any(
        candidate.as_posix() in implementations
        for candidate in declaration_implementations(PurePosixPath(path))
    )


def _target_kind(label: str | None) -> cs.RenameTargetKind:
    match label:
        case cs.NodeLabel.FUNCTION.value:
            return cs.RenameTargetKind.FUNCTION
        case cs.NodeLabel.METHOD.value:
            return cs.RenameTargetKind.METHOD
        case _:
            return cs.RenameTargetKind.TYPE


def _owners(hierarchy: list[str]) -> frozenset[str]:
    # The class segment of each member's name; a Java or C# signature may
    # hold dots of its own (`put(Map.Entry)`), so it is cut off first.
    owners: set[str] = set()
    for member in hierarchy:
        segments = member.partition(cs.CHAR_PAREN_OPEN)[0].split(cs.SEPARATOR_DOT)
        if len(segments) > 1:
            owners.add(segments[-2])
    return frozenset(owners)


def _owner_class(member: str) -> str:
    return member.partition(cs.CHAR_PAREN_OPEN)[0].rpartition(cs.SEPARATOR_DOT)[0]


def _unclaimed(
    occurrence: Occurrence,
    foreign: set[str],
    namesakes: _Namesakes,
    chosen: set[str],
) -> bool:
    """Whether nothing else claims an occurrence the plan does not cover: a
    project whose name extends this one, a same-named symbol's site,
    statement or binding, or a JS/TS file's own name for the target."""
    return (
        occurrence.path not in foreign
        and (occurrence.path, occurrence.line, occurrence.col)
        not in namesakes.positions
        and not _inside(occurrence, namesakes.statements)
        and not (occurrence.bare and _inside(occurrence, namesakes.bound))
        and not (occurrence.bare and occurrence.path in chosen)
    )


def _unplanned_site(occurrence: Occurrence, project_name: str) -> RenameSite:
    """An occurrence no graph site covers, as the guessed site it is
    rewritten as under `allow_heuristic`."""
    return RenameSite(
        cs.RenameSiteKind.CALL if occurrence.called else cs.RenameSiteKind.REFERENCE,
        occurrence.path,
        occurrence.line,
        occurrence.col,
        base_module_qn(Path(occurrence.path), project_name),
        UNPLANNED if occurrence.certain else RECEIVER_UNKNOWN,
    )


def _refuse_unrewritable(
    qn: str, sites: list[RenameSite], unlocatable: list[str]
) -> None:
    """Refuse a plan holding a site no rewrite can reach: a structural edge
    with no position, or any other graph-known site without one."""
    structural = [s for s in sites if s.resolution == STRUCTURAL]
    if structural:
        raise RenameRefused(
            cs.RENAME_STRUCTURAL_UNLOCATABLE.format(qn=qn, count=len(structural)),
            structural,
            unlocatable,
        )
    # Every graph-known occurrence without a rewrite location blocks,
    # whatever bound it: `allow_heuristic` accepts rewriting through a
    # guessed site, not leaving a known one under the old name.
    siteless = [s for s in sites if s.kind == cs.RenameSiteKind.UNLOCATABLE]
    if siteless:
        raise RenameRefused(
            cs.RENAME_SITELESS.format(qn=qn, count=len(siteless)),
            siteless,
            unlocatable,
        )


def _statement(site: ImportSite) -> _Span:
    # The statement's own span, not its lines: `import x; helper(1)` puts a
    # call on the import's line that the import does not cover.
    return site.path, (site.line, site.col), (site.end_line, site.end_col)


def _inside(occurrence: Occurrence, spans: list[_Span]) -> bool:
    position = (occurrence.line, occurrence.col)
    return any(
        path == occurrence.path and start <= position < end
        for path, start, end in spans
    )


def _refusal_message(
    qn: str,
    old_name: str,
    ambiguous: list[RenameSite],
    unplanned: list[RenameSite],
    unknown: int,
    option: str,
) -> str:
    # `option` is how the caller spells the opt-in: a CLI flag is not an MCP
    # parameter (issue #2886).
    if not unplanned:
        return cs.RENAME_AMBIGUOUS.format(qn=qn, count=len(ambiguous), option=option)
    message = cs.RENAME_UNPLANNED.format(
        qn=qn,
        count=len(unplanned),
        name=old_name,
        locations=_locations(unplanned),
        option=option,
    )
    if unknown:
        message += cs.RENAME_UNPLANNED_RECEIVER_UNKNOWN.format(
            count=unknown, option=option
        )
    if ambiguous:
        message += cs.RENAME_UNPLANNED_ALSO_GUESSED.format(count=len(ambiguous))
    return message


def _locations(sites: list[RenameSite]) -> str:
    locations = list(dict.fromkeys(f"{s.path}:{s.line}" for s in sites))
    shown = cs.SEPARATOR_COMMA_SPACE.join(locations[: cs.RENAME_UNPLANNED_SHOWN])
    if len(locations) > cs.RENAME_UNPLANNED_SHOWN:
        shown = cs.RENAME_UNPLANNED_MORE.format(
            shown=shown, more=len(locations) - cs.RENAME_UNPLANNED_SHOWN
        )
    return shown
