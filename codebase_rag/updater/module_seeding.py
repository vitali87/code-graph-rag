"""Seed an incremental run's module-qn map from the graph, and hand the bare qn
of a stem in flux to the file a clean index gives it (issue #1569)."""

from collections.abc import Callable
from pathlib import Path

from loguru import logger

from .. import constants as cs
from .. import logs as ls
from ..parsers.factory import ProcessorFactory
from ..services import QueryProtocol
from ..sync.state import FileHashCache
from ..types_defs import ResultRow
from ..utils.path_utils import cached_relative_path, is_inline_module_path
from .frontends import FrontendRunner
from .types_defs import FileStateRemover


def _opens_for_reading(path: Path) -> bool:
    try:
        with open(path, "rb"):
            return True
    except OSError:
        return False


def stem_key(file_key: str) -> str:
    """The relative path without its extension: what same-stem siblings share."""
    return Path(file_key).with_suffix("").as_posix()


def flux_stems_of(file_key: str) -> set[str]:
    """The stems a file added or deleted puts in flux.

    Its own, and for a Python package initializer the stem of the module it
    defines: `x/__init__.py` names `proj.x` as `x.py` and `x.pyi` do, so adding
    or deleting it hands that module between them although the stems differ.
    Without this, an added package left the stub holding `proj.x` and itself
    suffixed to a twin, and a deleted one left `proj.x` with no Module, the
    stub never re-parsed to take it back (issue #2445).
    """
    stems = {stem_key(file_key)}
    path = Path(file_key)
    if path.name in cs.PY_PACKAGE_INIT_FILES and path.parent != Path("."):
        stems.add(path.parent.as_posix())
    return stems


class ModuleQnSeeding:
    """The module-qn claims an incremental run starts from.

    Disambiguation only sees the files processed this run, so the graph's
    claims are seeded before the parse; a survivor on a stem in flux
    re-parses unseeded, so the bare qn goes to whichever file the clean rule
    (first in walk order) gives it rather than to whichever was indexed
    first. `parsed_files` is the updater's record of what the current run
    parsed, and `remover` the updater whose state a forgotten claim's file
    leaves.
    """

    def __init__(
        self,
        graph: QueryProtocol | None,
        factory: ProcessorFactory,
        repo_path: Path,
        project_name: str,
        owns: Callable[[str], bool],
        owned_rows: Callable[[list[ResultRow], str], list[ResultRow]],
        parsed_files: list[tuple[Path, cs.SupportedLanguage]],
        rehydrated_module_qns: set[str],
        frontend_runner: FrontendRunner,
        remover: FileStateRemover,
    ) -> None:
        self._graph = graph
        self._factory = factory
        self._repo_path = repo_path
        self._project_name = project_name
        self._owns = owns
        self._owned_rows = owned_rows
        self._parsed_files = parsed_files
        self._rehydrated_module_qns = rehydrated_module_qns
        self._frontend_runner = frontend_runner
        self._remover = remover
        # Module-qn claims `_forget_flux_stem_qns` dropped this run, by file
        # key, so a survivor that turns out unreadable gets its claim back.
        self._forgotten_flux_claims: dict[str, tuple[str, Path]] = {}

    def prepare_flux_stems(
        self,
        eligible_files: list[tuple[Path, str]],
        old_hashes: FileHashCache,
        is_full_build: bool,
    ) -> set[str]:
        """The stems in flux this run, with their claims forgotten and the
        rest of the module qns seeded from the graph."""
        # Stems with a same-stem sibling added or deleted this run: their
        # survivors are not seeded and re-parse below, so the bare qn goes to
        # whichever file the clean rule (first in walk order) gives it,
        # rather than to whichever file was indexed first (issue #1569).
        eligible_keys = {key for _fp, key in eligible_files}
        flux_stems = (
            set()
            if is_full_build
            else {
                stem
                for key in (eligible_keys - old_hashes.keys())
                | (old_hashes.keys() - eligible_keys)
                for stem in flux_stems_of(key)
            }
        )
        # Per run: a full build forgets nothing, and a reused updater must not
        # restore a claim an earlier run recorded.
        self._forgotten_flux_claims = {}
        if not is_full_build:
            self._forget_flux_stem_qns(flux_stems)
            self.seed_module_qns_from_graph(eligible_keys, flux_stems)
        return flux_stems

    def seed_module_qns_from_graph(
        self,
        eligible_paths: set[str],
        flux_stems: frozenset[str] | set[str] = frozenset(),
    ) -> None:
        # Cross-language module-qn disambiguation (definition_processor.
        # _disambiguate_module_qn) only sees files processed this run. On an
        # incremental ADD of a file whose basename collides with an already-
        # indexed sibling of another language (shapes.rs owns proj.shapes,
        # then shapes.cpp is added), the added file would re-claim the bare qn
        # and overwrite the existing module under the qualified_name
        # constraint. Seed the qn->file map from the graph BEFORE processing so
        # the disambiguator sees taken qns; a re-parsed file keeps its bare qn.
        graph = self._graph
        if graph is None:
            return
        module_map = self._factory.definition_processor.module_qn_to_file_path
        # Only incremental runs seed (full builds process every file), so a
        # failed read here always aborts: a missing seed can silently let an
        # added file overwrite a sibling module's qn.
        for row in graph.fetch_all(cs.CYPHER_ALL_MODULE_PATHS_INTERNAL):
            qn = row.get(cs.KEY_QUALIFIED_NAME)
            path = row.get(cs.KEY_PATH)
            if not isinstance(qn, str) or not isinstance(path, str) or not path:
                continue
            # The read is unscoped, so a nested project's row reaches here.
            # Paths are RELATIVE, so `svc.v2`'s `api.py` passes the
            # eligible_paths guard below and would seed `svc.v2.api` onto
            # THIS project's api.py -- the cross-project ownership bug this
            # change exists to close (issue #1970).
            if not self._owns(qn):
                continue
            if is_inline_module_path(path):
                continue
            # Only seed modules whose file survives this run (still eligible).
            # A file deleted OR newly excluded this cycle is gone from
            # eligible_paths, so a same-basename ADD (delete shapes.rs + add
            # shapes.cpp) takes the bare qn a clean index would give it.
            if path not in eligible_paths:
                continue
            # A survivor of a stem in flux re-parses unseeded (issue #1569),
            # on the same terms `_forget_flux_stem_qns` applies to an
            # in-memory claim: one that cannot be opened is not re-parsed and
            # keeps its qn, and one that opens has its claim recorded so a
            # failed read can restore it. A fresh updater holds no in-memory
            # claim, so without this the graph's was the only copy, and an
            # added sibling took the bare qn from under the unread survivor's
            # subtree (issue #2232).
            if stem_key(path) in flux_stems:
                self._seed_flux_survivor(qn, path)
                continue
            module_map.setdefault(qn, self._repo_path / path)

    def _seed_flux_survivor(self, qn: str, path: str) -> None:
        # An unopenable survivor is not re-parsed and keeps its graph claim;
        # one that opens re-parses unseeded, its claim recorded for
        # `restore_unreadable_flux_claims` should its read fail later.
        survivor = self._repo_path / path
        if not _opens_for_reading(survivor):
            self._factory.definition_processor.module_qn_to_file_path.setdefault(
                qn, survivor
            )
        else:
            self._forgotten_flux_claims.setdefault(path, (qn, survivor))
            # A scoped re-ingest read the module qns back before this, and the
            # survivor's old one need not come back: beside a sibling in another
            # language neither file keeps the bare qn, so left here it is
            # offered to import verification as a live module the re-parse
            # just deleted (issue #2586). A re-parse that keeps the qn records
            # it again; an unreadable survivor gets it back with its claim.
            self._rehydrated_module_qns.discard(qn)

    def _forget_flux_stem_qns(self, flux_stems: set[str]) -> None:
        """Drop the module-qn claims of every file on a stem in flux.

        Seeding skips a flux-stem survivor so it re-parses unseeded, but a
        reused updater (the watcher, the MCP server) still holds the claim
        its previous run wrote: `util.h` keeps `proj.util`, so an added
        `util.c` is suffixed to `proj.util.c` where a clean index gives it
        the bare qn (issue #2022). Its definitions stay registered under the
        old qn too, so the re-parse marks the new owner's same-named
        function as a duplicate. Every file on the stem re-parses this run
        and writes its state again in walk order, so the old state goes
        first; `remove_file_from_state` finds it through the recorded qn,
        so it runs before the claim is dropped.

        Survivors only: a file deleted this run keeps its state until the
        deletion step, since the foreign-definer lookup reads it to find the
        files that define into the departing module (issue #1660). A
        survivor that cannot be opened keeps its claim as well: the run
        leaves its graph subtree in place, and the added sibling would
        otherwise take the bare qn over a Module still defining the
        survivor's functions.

        In LIBCLANG mode the frontend has already registered a covered
        file's current definitions and the file pass will skip it, so its
        frontend registrations are kept, as in the re-parse loop.
        """
        if not flux_stems:
            return
        module_map = self._factory.definition_processor.module_qn_to_file_path
        on_stem: dict[Path, str] = {}
        for path in module_map.values():
            # A module recorded outside this checkout has no key the walk
            # could produce, so it is on no stem of this run.
            if not path.is_relative_to(self._repo_path):
                continue
            key = cached_relative_path(path, self._repo_path).as_posix()
            if stem_key(key) in flux_stems and _opens_for_reading(path):
                on_stem[path] = key
        # Recorded first: `remove_file_from_state` drops the claim itself.
        for qn, path in module_map.items():
            if path in on_stem:
                self._forgotten_flux_claims[on_stem[path]] = (qn, path)
        covered = self._frontend_runner.cpp_covered
        for path in sorted(on_stem):
            self._remover.remove_file_from_state(
                path, frontend_current=on_stem[path] in covered
            )
        for qn in [qn for qn, path in module_map.items() if path in on_stem]:
            del module_map[qn]

    def restore_unreadable_flux_claims(self, unreadable_keys: set[str]) -> None:
        """Give an unreadable flux-stem survivor back the claim it lost.

        `_forget_flux_stem_qns` checks that a survivor opens, but the read
        that decides whether it is parsed comes later and can still fail. The
        run then leaves the survivor's subtree in place, and without its
        claim an added same-stem sibling would take the bare qn over a Module
        still defining the survivor's functions. Runs before any file is
        parsed, so the sibling is suffixed as it is when the survivor does
        not open at all.
        """
        module_map = self._factory.definition_processor.module_qn_to_file_path
        for key in unreadable_keys & self._forgotten_flux_claims.keys():
            qn, path = self._forgotten_flux_claims[key]
            module_map.setdefault(qn, path)

    def _project_module_rows(self) -> list[ResultRow]:
        """This project's Module paths and qns, read from the graph.

        Scoped to the project: the seeded-map prune read every module of every
        project in the shared graph on each sync, which made a three-file
        repo sync ten times slower beside thirty other projects (issue
        #2404). Raises when the read fails; callers decide what that means.
        """
        graph = self._graph
        if graph is None:
            return []
        return self._owned_rows(
            graph.fetch_all(
                cs.CYPHER_PROJECT_MODULE_PATHS,
                {
                    cs.KEY_PROJECT_PREFIX: f"{self._project_name}{cs.SEPARATOR_DOT}",
                    cs.KEY_PROJECT_NAME: self._project_name,
                },
            ),
            cs.KEY_QUALIFIED_NAME,
        )

    def prune_stale_seeded_module_qns(
        self, exempt_paths: set[Path] | None = None
    ) -> None:
        """Drop map entries for modules the graph no longer holds.

        `seed_module_qns_from_graph` only ever adds, so on a reused updater
        (the watcher, the MCP server) a qn whose Module was deleted by another
        writer -- a second updater, `delete_project` then a re-index, a run in
        another clone -- stays in the map for the life of the process and keeps
        being offered by `known_module_paths`, where it carries a real path and
        so wins the Rust sub-scope arbitration outright.

        An entry absent from the graph is dropped unless this run parsed its
        file: a re-parsed file writes its own entry directly and its Module
        node may not be flushed yet, so the graph read cannot see it. Anything
        else the next run that parses the file re-adds.

        Not folded into the seed helper: `reingest` calls that twice with
        different path sets, the second covering only the gone files, so a
        prune running inside it would delete what the first call seeded.

        `reingest` calls this too (issue #1712), passing the paths IT
        re-parsed as `exempt_paths`. It cannot use the default: the exemption
        means "parsed by this operation, so the Module node may be
        unflushed", and `_parsed_files` only carries that meaning on `run()`,
        which clears it per run. `reingest` never clears it -- deliberately,
        because `_hydrate_for_reingest` reads it as a LIVENESS PROXY
        (non-empty means "this updater already has live state, skip the
        rehydration read"). So on a retained updater the default would exempt
        every file parsed since the process started, and the map would never
        shrink -- the accumulation #1712 is about, in a fix that looks
        correct and does nothing from the second call onwards.
        """
        graph = self._graph
        if graph is None:
            return
        dp = self._factory.definition_processor
        module_map = dp.module_qn_to_file_path
        parsed_this_run = (
            exempt_paths
            if exempt_paths is not None
            else {path for path, _lang in self._parsed_files}
        )
        # Only an entry this run did not parse can be dropped. On a first
        # sync every entry is one it parsed, so there is nothing to decide
        # and no read to make; reading anyway found the brand-new graph empty
        # and warned on the first run every new user makes (issue #2404).
        if not any(path not in parsed_this_run for path in module_map.values()):
            return
        # No rows is "no verdict", never "the graph is empty". A read that
        # fails or comes back empty against a populated map is the unflushed
        # or transient case, and pruning on it would drop every entry except
        # the handful this run re-parsed -- on a one-file incremental run over
        # a large repo, almost the whole map. Same posture as
        # `GraphRehydrator.rehydrate_registry_from_graph`, where a failed query
        # leaves the previous state intact.
        try:
            rows = self._project_module_rows()
        except Exception:  # noqa: BLE001 -- any engine's driver error; the read degrades
            logger.warning(ls.SEED_PRUNE_NO_VERDICT)
            return
        in_graph = {
            qn for row in rows if isinstance(qn := row.get(cs.KEY_QUALIFIED_NAME), str)
        }
        if not in_graph:
            logger.warning(ls.SEED_PRUNE_NO_VERDICT)
            return
        for qn in [qn for qn in module_map if qn not in in_graph]:
            # A file this run parsed writes its own entry and its Module node
            # may still be unflushed, so the read above cannot see it.
            if module_map[qn] not in parsed_this_run:
                del module_map[qn]
