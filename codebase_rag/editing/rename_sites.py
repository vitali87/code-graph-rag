"""The graph sites of one symbol a rename rewrites (issue #1532): its
definition's name token, every call, reference, construction and type-edge
site (with its `resolution`, issue #1526), and the import statements binding
it (issue #1522), as each language's bindings let them be renamed. Split
from `rename.py`.
"""

from collections.abc import Callable
from pathlib import Path

from .. import constants as cs
from .. import cypher_queries as cq
from .. import graph_query
from ..language_spec import get_language_for_extension
from ..types_defs import QueryFn, ResultRow
from ..utils.path_utils import base_module_qn
from .imports import ImportSite
from .patcher import Patcher, PatcherError, line_col_to_byte
from .rename_spec import CHAIN, SITELESS, STRUCTURAL, RenameRefused, RenameSite
from .rename_tokens import (
    STALE_CALL,
    chain_links,
    jsx_closing_token,
    last_identifier,
    name_token,
    overload_stub_sites,
)


def _add_jsx_closing_site(
    sites: list[RenameSite],
    kind: cs.RenameSiteKind,
    path: str,
    source: bytes,
    token: tuple[int, int],
    owner: str,
    resolution: str | None,
) -> None:
    """Append the closing-tag twin of a JSX opening-tag site, once (#2811).

    Kept out of `SiteCollector._add_site` so that method stays under the
    cognitive complexity limit (S3776).
    """
    closing = jsx_closing_token(
        source, get_language_for_extension(Path(path).suffix), token
    )
    if closing is None:
        return
    if any((s.path, s.line, s.col) == (path, *closing) for s in sites):
        return
    sites.append(RenameSite(kind, path, closing[0], closing[1], owner, resolution))


def _chain_link_sites(
    kind: cs.RenameSiteKind,
    path: str,
    source: bytes,
    line: int,
    col: int,
    old_name: str,
    owner: str,
    token: tuple[int, int],
) -> list[RenameSite]:
    # `obj.helper(1).helper(2)`: a link of the chain the index has no row
    # for (the receiver's type was not inferred) binds to nobody the graph
    # knows. It is renamed only as a guess the caller opts into with
    # allow_heuristic; `plan` drops it where a row covers it.
    language = get_language_for_extension(Path(path).suffix)
    return [
        RenameSite(kind, path, link[0], link[1], owner, CHAIN)
        for link in chain_links(source, language, line, col, old_name)
        if link != token
    ]


class SiteCollector:
    """Collect the sites of one symbol from a project's graph, checked
    against the tree they would be rewritten in."""

    def __init__(self, fetch_all: QueryFn, project_name: str, repo_root: Path) -> None:
        self.fetch_all = fetch_all
        self.project_name = project_name
        self.repo_root = repo_root
        # Per renamed symbol: (owner, line, col) of each CALLS site that runs
        # it through a parameter, and that parameter (issue #2459).
        self._callback_sites: dict[str, dict[tuple[str, int, int], str]] = {}

    def module_of(self, qn: str) -> tuple[str, str | None]:
        # The defining module's qn and path, from the definition's own path:
        # a method's qn nests under its class, so stripping segments would
        # name the class, not the module.
        definition = graph_query.definition(self.fetch_all, self.project_name, qn, None)
        path = definition["path"]
        if not path:
            return qn.rsplit(cs.SEPARATOR_DOT, 1)[0], None
        return base_module_qn(Path(path), self.project_name), path

    def collect(
        self, qn: str, guessed: frozenset[str] = frozenset()
    ) -> tuple[list[RenameSite], list[str], str, str | None]:
        definition = graph_query.definition(
            self.fetch_all, self.project_name, qn, self.repo_root
        )
        path = definition["path"]
        if not definition["found"] or not path:
            raise RenameRefused(cs.RENAME_UNKNOWN.format(qn=qn), [], [])
        old_name = definition["name"] or qn.rsplit(cs.SEPARATOR_DOT, 1)[-1]
        unlocatable: list[str] = []
        patcher = Patcher(self.repo_root)
        sites: list[RenameSite] = [
            self._definition_site(qn, definition, path, old_name, patcher, guessed)
        ]
        sites.extend(overload_stub_sites(sites[0], patcher))
        # Calls, references and constructions.
        for row in graph_query.callers(self.fetch_all, self.project_name, qn):
            self._add_site(
                sites, unlocatable, cs.RenameSiteKind.CALL, row, old_name, patcher
            )
        params = {
            cs.KEY_PROJECT_PREFIX: f"{self.project_name}{cs.SEPARATOR_DOT}",
            cs.KEY_QN: qn,
            cs.KEY_LABEL: definition["label"],
        }
        # Both reads below are prefix-scoped, and `foo.` selects `foo.bar`'s
        # rows too: a site in a project whose name extends this one must not
        # join this project's plan (issue #1989; the rule of #1982).
        owns = graph_query.owner_check(self.fetch_all, self.project_name)
        for row in self.fetch_all(cq.CYPHER_GRAPH_REFERENCES, params):
            if not owns(str(row.get(cs.KEY_QUALIFIED_NAME) or "")):
                continue
            self._add_site(
                sites, unlocatable, cs.RenameSiteKind.REFERENCE, row, old_name, patcher
            )
        self._add_type_edge_sites(
            sites,
            unlocatable,
            self.fetch_all(cq.CYPHER_GRAPH_TYPE_EDGES, params),
            owns,
            old_name,
            patcher,
        )
        sites = self._js_import_bound(qn, path, sites, old_name, patcher)
        return sites, unlocatable, old_name, definition["label"]

    def _js_import_bound(
        self,
        qn: str,
        path: str,
        sites: list[RenameSite],
        old_name: str,
        patcher: Patcher,
    ) -> list[RenameSite]:
        """The sites, as the JS/TS imports binding each one let it be renamed.

        The graph binds `mul(8, 9)` to `mul` through a default import, and
        `add(1, 2)` through a barrel's `export *`: the token equals the
        definition's name either way (issue #2464). A bare use in another
        file follows the rename only when its file imports the name by the
        definition's own name, so the import is rewritten with it. One whose
        file binds a name of its own is left as it is. One whose import goes
        through an `export *` refuses outright: the rename cannot rewrite that
        import, so rewriting the use would leave it naming what the barrel no
        longer exports, whatever leave the caller gives. Any other is a guess.
        """
        bindings: tuple[set[str], set[str], dict[str, str]] | None = None
        kept: list[RenameSite] = []
        starred: list[RenameSite] = []
        barrels: set[str] = set()
        for site in sites:
            if (
                site.kind not in (cs.RenameSiteKind.CALL, cs.RenameSiteKind.REFERENCE)
                or site.path == path
                or get_language_for_extension(Path(site.path).suffix)
                not in cs.JS_TS_LANGUAGES
                or self._is_member_token(patcher, site)
            ):
                kept.append(site)
                continue
            if bindings is None:
                bindings = self.js_name_bindings(qn, old_name)
            by_name, aliased, star_bound = bindings
            if site.path in by_name:
                kept.append(site)
            elif (barrel := star_bound.get(site.path)) is not None:
                starred.append(site._replace(resolution=cs.RENAME_SITE_STAR_REEXPORT))
                barrels.add(barrel)
            elif site.path not in aliased:
                kept.append(site._replace(resolution=cs.EdgeResolution.HEURISTIC.value))
        if starred:
            raise RenameRefused(
                cs.RENAME_STAR_REEXPORT.format(
                    qn=qn, count=len(starred), barrels=", ".join(sorted(barrels))
                ),
                starred,
                [],
            )
        return kept

    @staticmethod
    def _is_member_token(patcher: Patcher, site: RenameSite) -> bool:
        # `ns.helper(1)` names the member by its own name whatever binds
        # `ns`; only a bare `helper(1)` is bound by an import of that name.
        try:
            source = patcher.source(site.path)
            offset = line_col_to_byte(source, site.line, site.col)
        except PatcherError:
            return False
        return source[:offset].rstrip().endswith(b".")

    def js_name_bindings(
        self, qn: str, name: str
    ) -> tuple[set[str], set[str], dict[str, str]]:
        """(files importing `name` by the definition's own name, files whose
        `name` is a binding of their own for the definition, files importing
        it through an `export *` with that barrel's path)."""
        module_qn, _path = self.module_of(qn)
        by_name = {
            site.path
            for site, _module in self.import_sites(qn, name)
            if (site.alias or site.imported_name) == name
        }
        # `import mul from`, `const mul = require(...)` and a barrel's
        # `export { default as mul } from` name the binding as the importer
        # chose, and so does every module importing that `mul` from such a
        # barrel: the definition's rename changes none of them.
        aliased: set[str] = set()
        pending = [(module_qn, False)]
        seen = {module_qn}
        while pending:
            source, chosen = pending.pop()
            for row in graph_query.importers(self.fetch_all, self.project_name, source):
                # The definition's own name is rewritten with its import; a
                # barrel's chosen name only passes on under that same name.
                if (
                    not row["path"]
                    or row["alias"] != name
                    or (row["imported_name"] == name) != chosen
                ):
                    continue
                aliased.add(row["path"])
                if row["module"] not in seen:
                    seen.add(row["module"])
                    pending.append((row["module"], True))
        return by_name, aliased, self.js_star_bound(module_qn, name)

    def js_star_bound(self, module_qn: str, name: str) -> dict[str, str]:
        """Files importing `name` through a chain of re-exports that passes
        an `export *`, each with the path of the first such barrel.

        The chain may name the definition before the star
        (`export { foo } from "../a"` in `b`, then `export * from "../b"` in
        `c`): the import walk rewrites `b`, but not the consumer importing
        `foo` from `c`.
        """
        bound: dict[str, str] = {}
        pending = [(module_qn, "")]
        seen = {(module_qn, False)}
        while pending:
            source, barrel = pending.pop()
            for row in graph_query.importers(self.fetch_all, self.project_name, source):
                following = self._js_star_following(row, name, barrel, bound)
                if following is None:
                    continue
                step = (row["module"], bool(following))
                if step not in seen:
                    seen.add(step)
                    pending.append((row["module"], following))
        return bound

    @staticmethod
    def _js_star_following(
        row: graph_query.ImporterRow, name: str, barrel: str, bound: dict[str, str]
    ) -> str | None:
        """The first `export *` barrel on the chain once it passes `row`, empty
        while there is none, or None when `row` does not pass `name` on. A
        file binding `name` past such a barrel is recorded in `bound`."""
        path = row["path"]
        if not path:
            return None
        # `export * from` records `*` and binds no alias.
        if row["imported_name"] == cs.IMPORTED_NAME_WILDCARD and row["alias"] is None:
            return barrel or path
        if row["alias"] != name or row["imported_name"] != name:
            return None
        # A consumer, or a barrel passing the name on by name.
        if barrel:
            bound.setdefault(path, barrel)
        return barrel

    def _definition_site(
        self,
        qn: str,
        definition: graph_query.DefinitionRow,
        path: str,
        old_name: str,
        patcher: Patcher,
        guessed: frozenset[str],
    ) -> RenameSite:
        """The definition's own name token: a guessed site when only a
        guessed OVERRIDES edge reaches `qn`."""
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
        # From the `def`/`class` line, not the decorated start: a decorator
        # may spell the old name (`@register(fetch=1)`) and is not the
        # definition's name token (issue #2428).
        start = definition["name_line"] or definition["start_line"] or 1
        end = definition["end_line"] or start
        token = name_token(
            source, get_language_for_extension(Path(path).suffix), start, end, old_name
        )
        if token is None:
            raise RenameRefused(
                cs.RENAME_NO_DEFINITION_TOKEN.format(qn=qn, path=path), [], []
            )
        resolution = (
            cs.EdgeResolution.HEURISTIC if qn in guessed else cs.EdgeResolution.EXACT
        )
        return RenameSite(
            cs.RenameSiteKind.DEFINITION, path, token[0], token[1], qn, resolution
        )

    def _add_type_edge_sites(
        self,
        sites: list[RenameSite],
        unlocatable: list[str],
        rows: list[ResultRow],
        owns: Callable[[str], bool],
        old_name: str,
        patcher: Patcher,
    ) -> None:
        # A base-class list or an annotation names the symbol without a
        # call; an edge without a site cannot be rewritten and must refuse,
        # or the applied rename would leave `class Circle(Base)` dangling.
        for row in rows:
            if not owns(str(row.get(cs.KEY_QUALIFIED_NAME) or "")):
                continue
            if not isinstance(row.get("path"), str) or not isinstance(
                row.get("line"), int
            ):
                sites.append(
                    RenameSite(
                        cs.RenameSiteKind.UNLOCATABLE,
                        str(row.get("path") or ""),
                        0,
                        0,
                        str(row.get("qualified_name") or ""),
                        STRUCTURAL,
                    )
                )
                continue
            self._add_site(
                sites, unlocatable, cs.RenameSiteKind.REFERENCE, row, old_name, patcher
            )

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

        Every refusal path of a site -- a row with no usable position, a
        file the patcher cannot read, a position that file does not have, a
        class run through a parameter -- appends the same pair. Sharing
        them keeps `_add_site` under the cognitive complexity limit (S3776)
        by removing a whole branch body rather than straight-line setup,
        which is the part that actually counts.
        """
        unlocatable.append(
            cs.RENAME_UNLOCATABLE_SITE.format(owner=owner, resolution=resolution)
        )
        sites.append(
            RenameSite(
                cs.RenameSiteKind.UNLOCATABLE, path, line, col, owner, site_resolution
            )
        )

    def _via_param(self, row: ResultRow | graph_query.CallSiteRow) -> str | None:
        """The parameter the row's owner runs the renamed symbol through.

        A reference row carries it. A callers row is matched against the
        callback sites of the symbol it reaches, `through`, read once per
        symbol (issue #2459).
        """
        if isinstance(via_param := row.get(cs.KEY_VIA_PARAM), str):
            return via_param
        through = row.get("through")
        if not isinstance(through, str):
            return None
        if through not in self._callback_sites:
            params = {
                cs.KEY_PROJECT_PREFIX: f"{self.project_name}{cs.SEPARATOR_DOT}",
                cs.KEY_QN: through,
            }
            self._callback_sites[through] = {
                (str(site[cs.KEY_QUALIFIED_NAME]), line, col): str(
                    site[cs.KEY_VIA_PARAM]
                )
                for site in self.fetch_all(cq.CYPHER_GRAPH_CALLBACK_SITES, params)
                if isinstance(line := site.get(cs.KEY_LINE), int)
                and isinstance(col := site.get(cs.KEY_COL), int)
            }
        key = (str(row.get("qualified_name") or ""), row.get("line"), row.get("col"))
        return self._callback_sites[through].get(key)

    @staticmethod
    def _span_in_file(
        source: bytes, line: int, col: int, end_line: int, end_col: int
    ) -> bool:
        """Whether both ends of a site span are positions `source` has."""
        try:
            line_col_to_byte(source, line, col)
            line_col_to_byte(source, end_line, end_col)
        except PatcherError:
            return False
        return True

    @classmethod
    def _add_callback_site(
        cls,
        sites: list[RenameSite],
        unlocatable: list[str],
        row: ResultRow | graph_query.CallSiteRow,
        via_param: str,
    ) -> None:
        """A site where the owner runs what it was handed (`f(y)` in `apply(f, x)`).

        The name written there is the parameter's, so nothing is rewritten
        (issue #2459). A function is named where it is passed, a REFERENCES
        edge of its own that joins the plan. A class passed as a value is
        recorded there only against its constructor, if at all, so its rename
        cannot see that site and refuses rather than leave it under the old
        name.
        """
        if row.get(cs.KEY_REL_TYPE) != cs.RelationshipType.INSTANTIATES:
            return
        line, col = row.get("line"), row.get("col")
        cls._record_unlocatable(
            sites,
            unlocatable,
            owner=str(row.get("qualified_name") or ""),
            path=str(row.get("path") or ""),
            line=line if isinstance(line, int) else 0,
            col=col if isinstance(col, int) else 0,
            resolution=cs.RENAME_RESOLUTION_CLASS_VIA_PARAM.format(param=via_param),
            site_resolution=SITELESS,
        )

    @staticmethod
    def _site_end(
        row: ResultRow | graph_query.CallSiteRow, line: int, col: int, old_name: str
    ) -> tuple[int, int]:
        """Where a site ends: its recorded end, else `old_name` written at its start."""
        end_line, end_col = row.get("end_line"), row.get("end_col")
        return (
            end_line if isinstance(end_line, int) else line,
            end_col if isinstance(end_col, int) else col + len(old_name),
        )

    @classmethod
    def _site_source(
        cls,
        sites: list[RenameSite],
        unlocatable: list[str],
        patcher: Patcher,
        *,
        owner: str,
        path: str,
        span: tuple[int, int, int, int],
    ) -> bytes | None:
        """The file holding a site's `span`, else None once recorded unlocatable."""
        line, col = span[0], span[1]
        try:
            source = patcher.source(path)
        except PatcherError:
            # The graph knows this occurrence but its file cannot be read:
            # renaming around it would leave it under the old name.
            cls._record_unlocatable(
                sites,
                unlocatable,
                owner=owner,
                path=path,
                line=line,
                col=col,
                resolution="missing file",
                site_resolution=SITELESS,
            )
            return None
        if not cls._span_in_file(source, *span):
            # A position the file does not have: an index older than the
            # file, or a site recorded against another file (issue #2459).
            # It cannot be rewritten, so it refuses instead of escaping as a
            # PatcherError.
            cls._record_unlocatable(
                sites,
                unlocatable,
                owner=owner,
                path=path,
                line=line,
                col=col,
                resolution=cs.RENAME_RESOLUTION_BAD_POSITION,
                site_resolution=SITELESS,
            )
            return None
        return source

    def _add_site(
        self,
        sites: list[RenameSite],
        unlocatable: list[str],
        kind: cs.RenameSiteKind,
        row: ResultRow | graph_query.CallSiteRow,
        old_name: str,
        patcher: Patcher,
    ) -> None:
        if via_param := self._via_param(row):
            self._add_callback_site(sites, unlocatable, row, via_param)
            return
        owner = str(row.get("qualified_name") or "")
        path = row.get("path")
        line, col = row.get("line"), row.get("col")
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
                site_resolution=resolution_text or SITELESS,
            )
            return
        span = (line, col, *self._site_end(row, line, col, old_name))
        source = self._site_source(
            sites, unlocatable, patcher, owner=owner, path=path, span=span
        )
        if source is None:
            return
        end_line, end_col = row.get("end_line"), row.get("end_col")
        token = last_identifier(
            source,
            line,
            col,
            end_line if isinstance(end_line, int) else None,
            end_col if isinstance(end_col, int) else None,
            old_name,
            get_language_for_extension(Path(path).suffix),
            is_call=kind == cs.RenameSiteKind.CALL,
        )
        if token == STALE_CALL:
            self._record_unlocatable(
                sites,
                unlocatable,
                owner=owner,
                path=path,
                line=line,
                col=col,
                resolution="stale site",
                site_resolution=SITELESS,
            )
            return
        if token is None:
            # The site spells the symbol under an alias (`h(1, 2)` for
            # `import helper as h`); the alias keeps binding, so nothing to
            # rewrite here.
            return
        sites.append(RenameSite(kind, path, token[0], token[1], owner, resolution_text))
        _add_jsx_closing_site(sites, kind, path, source, token, owner, resolution_text)
        if kind == cs.RenameSiteKind.CALL:
            sites.extend(
                _chain_link_sites(kind, path, source, line, col, old_name, owner, token)
            )

    def import_sites(self, qn: str, old_name: str) -> list[tuple[ImportSite, str]]:
        module_qn, _path = self.module_of(qn)
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
            for row in graph_query.importers(
                self.fetch_all, self.project_name, source_qn
            ):
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
