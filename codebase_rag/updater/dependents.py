"""Find the files a sync must re-parse beside the ones that changed: the
dependents whose bindings an edit can move, the files waiting on a name an
added file or definition supplies, and the files that attach members to a
module about to be replaced."""

import posixpath
from collections.abc import Callable, Collection, Iterable, Mapping
from pathlib import Path, PurePosixPath
from typing import Protocol

from loguru import logger
from tree_sitter import Node, Parser, QueryCursor

from .. import constants as cs
from .. import cypher_queries as cq
from .. import logs as ls
from ..capture import CaptureSelection
from ..config import settings
from ..interface_skip import (
    callers_to_reparse,
    stable_interface_modules,
    without_flow_sources,
)
from ..language_spec import get_language_for_extension, sql_object_reference_name
from ..node_text import sorted_captures
from ..parsers.cpp.preproc_recovery import parse_with_preproc_recovery
from ..parsers.document_tier import broken_link_keys
from ..parsers.factory import ProcessorFactory
from ..parsers.interface_fingerprint import INTERFACE_LANGUAGES, interface_fingerprint
from ..services import QueryProtocol
from ..types_defs import LanguageQueries, PropertyParams, ResultRow
from ..utils import qn_markers
from ..utils.path_utils import base_module_qn, cached_relative_path
from ..utils.source_encoding import grammar_bytes


class DependentRun(Protocol):
    """What the lookups read of the sync run they are part of: whether it
    re-parses every file, in which case no file waits on another and a
    failed read may degrade where an incremental run must abort."""

    is_full_build: bool


def _caller_paths(rows: Iterable[ResultRow]) -> set[str]:
    """The non-empty caller paths of rows read back from the graph."""
    return {
        path
        for row in rows
        if isinstance(path := row.get(cs.KEY_CALLER_PATH), str) and path
    }


# Longest first, so `index.d.ts` loses `.d.ts` whole rather than being cut back
# to `index.d` the way a single-suffix strip does.
_MODULE_EXTS_LONGEST_FIRST: tuple[str, ...] = tuple(
    # `str.__len__` rather than `len`: the bare builtin makes the inferred
    # element type `Sized`, which does not satisfy `tuple[str, ...]`, while a
    # lambda trips PLW0108. This spelling passes both gates.
    sorted(cs.JS_TS_MODULE_EXTENSIONS, key=str.__len__, reverse=True)
)


def _module_key(path: str) -> str:
    """A path with any JS/TS module extension removed.

    Applied to BOTH sides of the waiter match, which is the point: a source
    writing `./foo.js` records the extension, while the created file arrives
    as the key `foo.js`. Normalising only one side left them as `foo.js` and
    `foo` and they never met, so an importer using an explicit extension was
    never re-parsed (raised on #1717).

    `PurePosixPath.with_suffix("")` is not enough on its own -- it strips one
    suffix, turning `index.d.ts` into `index.d` rather than `index`.

    A path with no MODULE extension is returned unchanged. Stripping whatever
    suffix it happens to carry cuts a dotted basename in half: `./foo.test`
    became `foo` while the created `foo.test.ts` became `foo.test`, so the two
    forms of the same module missed each other again -- the very asymmetry
    this function exists to remove (raised on #1717).
    """
    for ext in _MODULE_EXTS_LONGEST_FIRST:
        if path.endswith(ext):
            return path[: -len(ext)]
    return path


def _specifier_wanted_keys(present_keys: Iterable[str]) -> set[str]:
    """Extension-free names a created file can be imported by."""
    wanted: set[str] = set()
    for key in present_keys:
        module_key = _module_key(key)
        wanted.add(module_key)
        # A created `pkg/index.ts` also satisfies `./pkg`, the directory form.
        as_path = PurePosixPath(module_key)
        parent = as_path.parent.as_posix()
        if as_path.name == cs.JS_INDEX_STEM and parent not in (
            cs.PATH_CURRENT_DIR,
            "",
        ):
            wanted.add(parent)
    return wanted


def _specifier_targets(caller: str, specifiers: Iterable[object]) -> set[str]:
    """What each recorded specifier names, relative to its importer."""
    base = PurePosixPath(caller).parent
    return {
        _module_key(posixpath.normpath((base / specifier).as_posix()))
        for specifier in specifiers
        if isinstance(specifier, str)
    }


def _definition_names(node: Node) -> set[str]:
    """Simple names a captured definition node declares: its `name` field, or
    the `name` fields of its named children (a Go `type_declaration` holds
    `type_spec`s), or the identifier under a C++ declarator chain."""
    names: set[str] = set()
    if (name := node.child_by_field_name(cs.FIELD_NAME)) is not None and name.text:
        names.add(name.text.decode(cs.ENCODING_UTF8, errors="replace"))
        return names
    declarator = node.child_by_field_name(cs.FIELD_DECLARATOR)
    while declarator is not None:
        inner = declarator.child_by_field_name(cs.FIELD_DECLARATOR)
        if inner is None:
            if declarator.text:
                names.add(declarator.text.decode(cs.ENCODING_UTF8, errors="replace"))
            break
        declarator = inner
    if names:
        return names
    for child in node.named_children:
        if (name := child.child_by_field_name(cs.FIELD_NAME)) is not None and name.text:
            names.add(name.text.decode(cs.ENCODING_UTF8, errors="replace"))
    return names


def _semantic_frontend_languages() -> frozenset[cs.SupportedLanguage]:
    # A semantic frontend binds calls with its own model of the callee (Jedi
    # infers from bodies the fingerprint leaves out), so its languages'
    # callers re-parse as before.
    languages: set[cs.SupportedLanguage] = set()
    if settings.PYTHON_FRONTEND != cs.PythonFrontend.HEURISTIC:
        languages.add(cs.SupportedLanguage.PYTHON)
    if settings.JAVA_FRONTEND != cs.JavaFrontend.HEURISTIC:
        languages.add(cs.SupportedLanguage.JAVA)
    return frozenset(languages)


def module_names_for(keys: list[str]) -> list[str]:
    """Import spellings a file could be named by, from its relative path.

    `pkg/util.py` can be imported as `pkg.util` or, from a sibling, as
    `util`, and the unresolved edge records whichever the source wrote.
    Both are offered so either spelling matches.
    """
    names: set[str] = set()
    for key in keys:
        path = PurePosixPath(key)
        parts = list(path.parent.parts)
        stem = path.stem
        directory_stem = cs.DIRECTORY_MODULE_STEM_BY_EXT.get(path.suffix)
        if not stem or (directory_stem is not None and stem == directory_stem):
            # A directory-module entry point is named by its DIRECTORY:
            # `pkg`, and `a.b.pkg` from the root -- never `pkg.pkg`, which
            # is what keeping the stem in the dotted form produces.
            #
            # Matched WITH the extension: `index.py` and `mod.py` are
            # ordinary Python modules imported by those names, and treating
            # every `index`/`mod` stem as a directory module leaves them
            # with no importable name at all.
            if not parts:
                # The directory-module rule needs a DIRECTORY. At the
                # repository root there is none, so the file is an
                # ordinary module named by its own stem: a sibling writes
                # `./index`, and that spelling is what an unresolved edge
                # records. Dropping it left a root-level entry point with
                # no name at all, so a waiter on it was never re-parsed --
                # the exact failure this lookup exists to close.
                #
                # Offering a name nothing waits on costs one redundant
                # re-parse; withholding one costs the unresolved edge
                # forever, so the tie breaks towards offering it.
                if stem:
                    names.add(stem)
                continue
            stem = parts.pop()
        names.add(stem)
        dotted = cs.SEPARATOR_DOT.join([*parts, stem])
        if dotted:
            names.add(dotted)
    return sorted(names)


class DependentLookup:
    """The files a change sends back for a re-parse, read from the graph.

    One level of dependents is complete: a dependent's own definitions are
    unchanged, so bindings in ITS callers cannot move. A file waiting on a
    name is found from what it recorded it could not resolve, since a file
    that did not exist yet has no inbound edge to follow. Every read is
    scoped to this project; one that fails degrades to doing less, never
    to a wrong graph, except where noted. `tree_sitter_language` names the
    grammar the definition pass would parse a file with.
    """

    def __init__(
        self,
        graph: QueryProtocol | None,
        graph_rows: Callable[[str, PropertyParams | None], list[ResultRow]],
        factory: ProcessorFactory,
        parsers: Mapping[cs.SupportedLanguage, Parser],
        queries: Mapping[cs.SupportedLanguage, LanguageQueries],
        repo_path: Path,
        project_name: str,
        capture: CaptureSelection,
        registered_project_names: Callable[[], list[str]],
        tree_sitter_language: Callable[[Path], cs.SupportedLanguage | None],
    ) -> None:
        self._graph = graph
        self._graph_rows = graph_rows
        self._factory = factory
        self._parsers = parsers
        self._queries = queries
        self._repo_path = repo_path
        self._project_name = project_name
        self._capture = capture
        self._registered_project_names = registered_project_names
        self._tree_sitter_language = tree_sitter_language

    def affected_caller_keys(self, reindexed_keys: list[str]) -> list[str]:
        """Relative paths of files whose code depends on a re-indexed file.

        One level deep is complete: an affected caller's own DEFINITIONS are
        unchanged, so bindings in ITS callers cannot move. A failed read
        degrades to today's verbatim restore, never worse.
        """
        graph = self._graph
        if not reindexed_keys or graph is None:
            return []
        rows = self._project_read(
            cs.CYPHER_AFFECTED_CALLER_PATHS,
            "affected callers",
            {cs.CYPHER_PARAM_PATHS: reindexed_keys},
        )
        if rows is None:
            return []
        return sorted(_caller_paths(rows) | self._linking_document_keys(reindexed_keys))

    def _project_read(
        self, query: str, label: str, params: PropertyParams
    ) -> list[ResultRow] | None:
        """A read scoped to this project, its prefix added to `params`; None,
        reported, when the graph cannot answer it. Empty for a sink with no
        query surface, which holds nothing to read back."""
        graph = self._graph
        if graph is None:
            return []
        try:
            return graph.fetch_all(
                query,
                {
                    **params,
                    cs.KEY_PROJECT_PREFIX: self._project_name + cs.SEPARATOR_DOT,
                },
            )
        except Exception as exc:  # noqa: BLE001 -- any engine's driver error; the read degrades
            logger.warning(ls.PRUNE_QUERY_FAILED, label=label, error=exc)
            return None

    def dependent_keys(
        self,
        target_keys: list[str],
        stable: Mapping[str, str],
        waiters: Collection[str] = (),
    ) -> list[str]:
        """The dependents of `target_keys` the run must re-parse (issue #2531).

        All of them, except a caller of a file whose interface held (`stable`,
        file to module qn) that depends on nothing else re-indexed and holds
        only edges the inbound capture restores by name: it would bind
        exactly as it does now, so it keeps its edges, as an unchanged file
        that is no dependent at all does. `waiters` are the files this run
        re-parses because a file it adds, or a definition it gains, is what
        they were waiting for. They are targets too: unchanged on disk, but
        what they resolve against is not, so what their callers see through
        them can move (`make()` returning what a new `build()` builds).
        """
        targets = sorted({*target_keys, *waiters})
        if not stable:
            return self.affected_caller_keys(targets)
        required, settled = self._settle_stable(targets, stable, waiters)
        candidates = set(self.affected_caller_keys(sorted(settled))) - required
        reparsed = self._callers_holding_unrestorable_edges(settled, candidates)
        if kept := len(candidates) - len(reparsed):
            logger.info(ls.INCREMENTAL_INTERFACE_KEPT, count=kept)
        return sorted(required | reparsed)

    def _settle_stable(
        self,
        target_keys: list[str],
        stable: Mapping[str, str],
        waiters: Collection[str],
    ) -> tuple[set[str], dict[str, str]]:
        """The dependents the unstable targets force, and what is left of
        `stable` once every stable file among them counts as unstable too.

        A stable file's own source keeps its fingerprint, but what its callers
        see through it can still move: `make()` returns whatever `build()`
        returns, so a `build()` changed in the same run changes
        `make().method()` in every caller of `make`, none of which calls
        `build`. A stable file re-parsed because of another file of the run
        (a dependent of an unstable target, a waiter) is therefore unstable
        for its own callers, and so on down the chain until nothing moves.
        """
        settled = dict(stable)
        required = set(waiters)
        frontier = [key for key in target_keys if key not in settled]
        while frontier or required & settled.keys():
            required.update(self.affected_caller_keys(frontier))
            frontier = sorted(required & settled.keys())
            for key in frontier:
                del settled[key]
        return required, settled

    def _callers_holding_unrestorable_edges(
        self, stable: Mapping[str, str], candidates: set[str]
    ) -> set[str]:
        """The `candidates` that must re-parse anyway; all of them when the
        edges between them and the stable files cannot be read."""
        graph = self._graph
        if not candidates or graph is None:
            return candidates
        try:
            rows = graph.fetch_all(
                cs.CYPHER_INTERFACE_BOUNDARY_EDGES,
                {
                    cs.CYPHER_PARAM_PATHS: sorted(stable),
                    cs.CYPHER_PARAM_CALLERS: sorted(candidates),
                },
            )
        except Exception:  # noqa: BLE001 -- any engine's driver error; the read degrades
            logger.warning(ls.INTERFACE_BOUNDARY_UNREAD)
            return candidates
        return callers_to_reparse(rows, candidates, stable)

    def interface_stable_modules(
        self, run: DependentRun, sources: Iterable[tuple[Path, str, bytes]]
    ) -> dict[str, str]:
        """{file key: module qn} of the re-indexed `sources` whose new bytes
        keep the interface fingerprint the graph stored for them.

        Empty on a full build (every file re-parses anyway) and while a
        capture group outside the ones the fingerprint models is enabled.
        """
        if run.is_full_build or (self._capture.enabled_rels - cs.INTERFACE_SKIP_RELS):
            return {}
        fresh = {
            key: fingerprint
            for path, key, data in sources
            if (fingerprint := self._source_interface_fingerprint(path, data))
        }
        graph = self._graph
        if not fresh or graph is None:
            return {}
        prefix = self._project_name + cs.SEPARATOR_DOT
        try:
            rows = graph.fetch_all(
                cs.CYPHER_INTERFACE_FINGERPRINTS,
                {
                    cs.CYPHER_PARAM_PATHS: sorted(fresh),
                    cs.KEY_PROJECT_PREFIX: prefix,
                    cs.KEY_PROJECT_NAME: self._project_name,
                },
            )
        except Exception:  # noqa: BLE001 -- any engine's driver error; the read degrades
            logger.warning(ls.INTERFACE_FINGERPRINTS_UNREAD)
            return {}
        longer_projects = [
            name for name in self._registered_project_names() if name.startswith(prefix)
        ]
        return self._without_flow_sources(
            stable_interface_modules(rows, fresh, longer_projects)
        )

    def _without_flow_sources(self, stable: dict[str, str]) -> dict[str, str]:
        """`stable` less the files whose callback flow edges other files'
        parses write; none at all when that cannot be read."""
        graph = self._graph
        if not stable or graph is None:
            return stable
        try:
            rows = graph.fetch_all(
                cs.CYPHER_INTERFACE_FLOW_SOURCE_PATHS,
                {cs.CYPHER_PARAM_PATHS: sorted(stable)},
            )
        except Exception:  # noqa: BLE001 -- any engine's driver error; the read degrades
            logger.warning(ls.INTERFACE_FLOW_SOURCES_UNREAD)
            return {}
        return without_flow_sources(stable, rows)

    def _source_interface_fingerprint(self, path: Path, data: bytes) -> str | None:
        """The interface fingerprint of `data` parsed as the run will parse
        it, or None when its language has none."""
        language = self._tree_sitter_language(path)
        if (
            language not in INTERFACE_LANGUAGES
            or language in _semantic_frontend_languages()
            or (language_queries := self._queries.get(language)) is None
            or (parser := language_queries.get(cs.KEY_PARSER)) is None
        ):
            return None
        tree = parse_with_preproc_recovery(
            parser, grammar_bytes(data, language, path), language
        )
        return interface_fingerprint(tree.root_node, language)

    def _linking_document_keys(self, reindexed_keys: list[str]) -> set[str]:
        """Documents whose links end in a re-indexed or deleted file.

        A link that names a heading ends at its Section, which the linked
        document's re-parse recreates without the edge; a link to a deleted
        file has lost its File and is now broken. Re-parsing the linking
        document re-resolves both, as a clean index would (issue #2458). A
        link to a File that still exists needs nothing: the node, and the
        edge with it, outlive its file's re-parse.
        """
        rows = self._project_read(
            cs.CYPHER_LINKING_DOCUMENT_PATHS,
            "linking documents",
            {cs.CYPHER_PARAM_PATHS: reindexed_keys},
        )
        if rows is None:
            return set()
        return {
            path
            for row in rows
            if isinstance(path := row.get(cs.KEY_CALLER_PATH), str)
            and path
            and (
                row.get(cs.KEY_TARGET_LABEL) == cs.NodeLabel.SECTION
                or not (
                    isinstance(target := row.get(cs.KEY_TARGET_PATH), str)
                    and (self._repo_path / target).is_file()
                )
            )
        }

    def unresolved_importer_keys(self, present_keys: list[str]) -> list[str]:
        """Files whose UNRESOLVED import names one of `present_keys`.

        The inbound-edge lookup cannot see these: a file created by this call
        has no inbound edges, so an importer that referenced its path before it
        existed keeps a dangling IMPORTS edge and is never re-parsed, leaving
        the scoped path diverging from a clean index (issue #1682).

        A failed read degrades to the previous behaviour, never worse.
        """
        graph = self._graph
        if not present_keys or graph is None:
            return []
        module_names = module_names_for(present_keys)
        if not module_names:
            return []
        rows = self._project_read(
            cs.CYPHER_UNRESOLVED_IMPORTER_PATHS,
            "unresolved importers",
            {cs.CYPHER_PARAM_MODULE_NAMES: module_names},
        )
        if rows is None:
            return []
        return sorted(
            _caller_paths(rows)
            | self._specifier_waiter_keys(present_keys)
            | self._broken_link_waiter_keys(present_keys)
        )

    def dart_package_waiters(self, run: DependentRun, keys: Iterable[str]) -> set[str]:
        """Dart importers whose `package:` import a pubspec change moved
        (issue #3278).

        A pubspec's `name:` is what `package:<name>/...` resolves by, so
        adding, deleting or renaming one moves an import between a project
        module and an external package while the importer is unchanged on
        disk, and no edge leads from it to the pubspec for the dependents
        lookup to follow. Each stored `package:` edge is resolved again
        against the map on disk now; only an importer whose target moved
        re-parses, so an edit that keeps the name (a dependency bump) sends
        none. A failed read degrades to the previous behaviour, never worse.
        """
        graph = self._graph
        if run.is_full_build or graph is None:
            return set()
        if all(PurePosixPath(key).name != cs.DEP_FILE_PUBSPEC for key in keys):
            return set()
        import_processor = self._factory.import_processor
        # A scoped re-ingest resets the map only when it deletes the old
        # subtrees, after this lookup; the map judged must be the new one.
        import_processor.dart.reset_dart_path_caches()
        rows = self._project_read(
            cs.CYPHER_PACKAGE_URI_IMPORTS,
            "Dart package importers",
            {cs.CYPHER_PARAM_URI_PREFIX: cs.DART_SCHEME_PACKAGE},
        )
        if rows is None:
            return set()
        return {
            path
            for row in rows
            if isinstance(path := row.get(cs.KEY_CALLER_PATH), str)
            and path.endswith(cs.DART_EXT)
            and isinstance(uri := row.get(cs.KEY_IMPORTED_NAME), str)
            and isinstance(from_qn := row.get(cs.KEY_FROM_QN), str)
            and import_processor.dart.dart_import_target(uri, from_qn)
            != row.get(cs.KEY_TO_QN)
        }

    def unresolved_reference_waiters(
        self,
        added: list[tuple[str, bytes]],
        modified: list[tuple[str, bytes]] | None = None,
    ) -> list[str]:
        """Files whose recorded unresolved references an ADDED file, or a
        definition a MODIFIED file gained, satisfies.

        Offered per added file: its module qn (a dropped `from .base import`
        recorded the guessed qn), its import spellings, every suffix of its
        path (a quoted include is recorded as a repository path suffix), and
        the simple names it defines (a base class or a callee is recorded by
        its written name); a Rust `use` records the whole path, matched under
        the module qn as a prefix. A modified file offers only the simple
        names it defines now and did not before (a Go or Java caller of a
        same-package function has no edge into the file that gains it; bot
        review on PR #1979). A failed read degrades to the previous
        behaviour, never worse (issue #1568).
        """
        graph = self._graph
        if graph is None:
            return []
        names: set[str] = set(module_names_for([key for key, _b in added]))
        prefixes: set[str] = set()
        for key, file_bytes in added:
            module_qn = base_module_qn(Path(key), self._project_name)
            names.add(module_qn)
            prefixes.add(f"{module_qn}{cs.SEPARATOR_DOT}")
            parts = PurePosixPath(key).parts
            names.update(cs.SEPARATOR_SLASH.join(parts[i:]) for i in range(len(parts)))
            names.update(self._defined_simple_names(key, file_bytes))
        if modified:
            self._add_gained_simple_names(names, modified)
        if not names:
            return []
        rows = self._project_read(
            cs.CYPHER_UNRESOLVED_REFERENCE_WAITERS,
            "unresolved references",
            {
                cs.CYPHER_PARAM_NAMES: sorted(names),
                cs.CYPHER_PARAM_PREFIXES: sorted(prefixes),
                # The root module (`__init__.py`, `mod.rs`) IS the
                # project name, not under its prefix.
                cs.KEY_PROJECT_NAME: self._project_name,
            },
        )
        if rows is None:
            return []
        return sorted(_caller_paths(rows))

    def _add_gained_simple_names(
        self, names: set[str], modified: list[tuple[str, bytes]]
    ) -> None:
        """Offer the simple names each modified file defines and did not before."""
        known = self.known_simple_names_by_path([key for key, _b in modified])
        for key, file_bytes in modified:
            had = known.get(key, set())
            if "*" in had:
                continue
            names.update(self._defined_simple_names(key, file_bytes) - had)

    def known_simple_names_by_path(self, keys: list[str]) -> dict[str, set[str]]:
        """The simple names of the definitions the graph holds per file, so a
        modified file's GAINED definitions can be told from the ones it had.
        A failed read marks every file fully known: nothing extra is offered."""
        known: dict[str, set[str]] = {}
        prefix = self._project_name + cs.SEPARATOR_DOT
        # The query excludes rows a longer project (`proj.sub`) owns, the
        # scoping `check.delta.snapshot` passes too; without the list
        # Memgraph rejects the read on every incremental sync (issue #2392).
        longer_project_prefixes = [
            name for name in self._registered_project_names() if name.startswith(prefix)
        ]
        try:
            rows = self._graph_rows(
                cq.CYPHER_DELTA_DEFINITIONS,
                {
                    cs.KEY_PROJECT_PREFIX: prefix,
                    cs.KEY_LONGER_PROJECT_PREFIXES: longer_project_prefixes,
                    cs.CYPHER_PARAM_PATHS: keys,
                },
            )
        except Exception as exc:  # noqa: BLE001 -- any engine's driver error; the read degrades
            logger.warning(ls.PRUNE_QUERY_FAILED, label="known definitions", error=exc)
            return {key: {"*"} for key in keys}
        for row in rows:
            qn, path = row.get(cs.KEY_QUALIFIED_NAME), row.get(cs.KEY_PATH)
            if isinstance(qn, str) and isinstance(path, str):
                known.setdefault(path, set()).add(
                    qn_markers.strip_dup_marker(
                        qn.rsplit(cs.SEPARATOR_DOT, 1)[-1].split(cs.CHAR_PAREN_OPEN, 1)[
                            0
                        ]
                    )
                )
        return known

    def _defined_simple_names(self, key: str, file_bytes: bytes) -> set[str]:
        """The simple names of the functions and classes a file defines, from
        a parse of its bytes with the language's combined query: what a
        waiting module wrote as a base or a callee (issue #1568). A language
        without a parser or a combined query offers nothing."""
        language = get_language_for_extension(PurePosixPath(key).suffix)
        parser = self._parsers.get(language) if language is not None else None
        language_queries = self._queries.get(language) if language else None
        query = (
            language_queries[cs.QUERY_FUNC_CLASS_IMPORT] if language_queries else None
        )
        if parser is None or query is None or language is None:
            return set()
        try:
            # The same recovery the definition pass parses with, or an added
            # SQL file offers only the routines a mis-lexed `$1,$2` left in
            # its plain parse (issue #3180).
            tree = parse_with_preproc_recovery(
                parser,
                grammar_bytes(file_bytes, language, self._repo_path / key),
                language,
            )
            captures = sorted_captures(QueryCursor(query), tree.root_node)
        except Exception:  # noqa: BLE001 -- a module that fails to parse names no definitions
            return set()
        names: set[str] = set()
        for capture in (cs.CAPTURE_FUNCTION, cs.CAPTURE_CLASS):
            for node in captures.get(capture, ()):
                if language == cs.SupportedLanguage.SQL:
                    # A SQL caller waits on the normalized name it wrote:
                    # `fee` (any schema) or `billing.fee` (that schema only).
                    # The raw identifier (`FEE`, `"Fee"`) would match neither.
                    # The graph keeps only the last segment, so an edited file
                    # re-offers its qualified names: a caller waiting on one
                    # is either linked into the file (re-parsed anyway) or
                    # really gains the routine.
                    if routine := sql_object_reference_name(node):
                        names.update({routine, routine.rsplit(cs.SEPARATOR_DOT, 1)[-1]})
                    continue
                names.update(_definition_names(node))
        return names

    def foreign_member_keys(self, run: DependentRun, keys: list[str]) -> list[str]:
        graph = self._graph
        if not keys or graph is None:
            return []
        try:
            rows = graph.fetch_all(
                cs.CYPHER_FOREIGN_MEMBER_PATHS,
                {
                    cs.CYPHER_PARAM_PATHS: keys,
                    cs.KEY_PROJECT_NAME: self._project_name,
                    cs.KEY_PROJECT_PREFIX: self._project_name + cs.SEPARATOR_DOT,
                },
            )
        except Exception as exc:
            # The delete that follows takes these members with their type's
            # module, so an incremental run that read an outage as "none"
            # lost them for good under a completed hash commit; it aborts, as
            # the inbound capture does. A full build re-parses every file.
            if not run.is_full_build:
                raise
            logger.warning(ls.PRUNE_QUERY_FAILED, label="foreign members", error=exc)
            return []
        return sorted(
            {
                path
                for row in rows
                if isinstance(path := row.get(cs.KEY_PATH), str) and path
            }
        )

    def foreign_definer_keys(self, gone_keys: Iterable[str]) -> set[str]:
        """Files that registered definitions keyed under a module going away.

        A definition's qn need not sit under the qn of the file that defines
        it. A Go method keys by its RECEIVER TYPE's module, so
        `func (t T) M()` in pkg/methods.go registers `proj.pkg.types.T.M`
        when `T` is declared in pkg/types.go.

        Renaming types.go deletes that Module subtree, and the method node
        and its outgoing CALLS go with it because they hang off the type.
        methods.go holds no edge INTO types.go, so the caller query cannot
        find it, nothing re-parses it, and the method is never recreated:
        the reused updater ends with no CALLS at all where a clean index has
        them (issue #1660).

        The span records already name the registering module, so the files
        to re-parse are exactly those that recorded a qn under a departing
        module without being that module themselves. Read before
        `remove_file_from_state` runs, while the run's records still stand.
        """
        gone = set(gone_keys)
        if not gone:
            return set()
        dp = self._factory.definition_processor
        key_for_module = {
            qn: cached_relative_path(path, self._repo_path).as_posix()
            for qn, path in dp.module_qn_to_file_path.items()
        }
        gone_modules = {qn for qn, key in key_for_module.items() if key in gone}
        if not gone_modules:
            return set()
        definers = set()
        for (span_module, _line, _col), location in dp.facts.function_locations.items():
            if span_module in gone_modules:
                continue
            qn = location.qualified_name
            if any(
                qn.startswith(f"{module}{cs.SEPARATOR_DOT}") for module in gone_modules
            ):
                definers.add(span_module)
        return {
            key
            for module in definers
            if (key := key_for_module.get(module)) is not None and key not in gone
        }

    def _specifier_waiter_keys(self, present_keys: list[str]) -> set[str]:
        """Importers whose recorded relative specifier names one of these files.

        The row-based lookup above reads persisted IMPORTS edges, and a
        relative JS/TS import of a path that does not exist yet has no such
        row: the edge is dropped at flush as an unverifiable internal target.
        Those importers record the literal specifier on their Module node
        instead, so they stay findable from the created file's path alone
        (issue #1714).

        The specifier is relative to the IMPORTER's own directory, so it is
        resolved here rather than in Cypher, where the arithmetic against
        `importer.path` would be far harder to read and to test.
        """
        rows = self._project_read(
            cs.CYPHER_UNRESOLVED_SPECIFIER_IMPORTERS, "unresolved specifiers", {}
        )
        if rows is None:
            return set()
        wanted = _specifier_wanted_keys(present_keys)
        waiters: set[str] = set()
        for row in rows:
            caller = row.get(cs.KEY_CALLER_PATH)
            specifiers = row.get(cs.CYPHER_KEY_SPECIFIERS)
            if not isinstance(caller, str) or not caller:
                continue
            if not isinstance(specifiers, list):
                continue
            if not wanted.isdisjoint(_specifier_targets(caller, specifiers)):
                waiters.add(caller)
        return waiters

    def _broken_link_waiter_keys(self, present_keys: list[str]) -> set[str]:
        """Documents with a broken link to one of these files.

        A link to a file that did not exist yet left no edge to find the
        document by, only the path on its Module's `broken_links`, so a
        created file is matched against those (issue #2458). Without this
        the document keeps listing the link as broken, and keeps lacking the
        edge, until it is edited itself.
        """
        rows = self._project_read(cs.CYPHER_BROKEN_LINK_DOCUMENTS, "broken links", {})
        if rows is None:
            return set()
        wanted = set(present_keys)
        waiters: set[str] = set()
        for row in rows:
            document = row.get(cs.KEY_CALLER_PATH)
            links = row.get(cs.KEY_BROKEN_LINKS)
            if not isinstance(document, str) or not isinstance(links, list):
                continue
            written = [link for link in links if isinstance(link, str)]
            if not wanted.isdisjoint(broken_link_keys(document, written)):
                waiters.add(document)
        return waiters
