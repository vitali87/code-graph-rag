"""Forget what one file's last parse registered: its definitions in the function
registry and simple-name index, its module qn, and the import state keyed on
it, sparing every entry another file still owns."""

from collections.abc import Callable
from pathlib import Path

from loguru import logger

from .. import constants as cs
from .. import logs as ls
from ..function_registry import FunctionRegistryTrie
from ..language_spec import get_language_for_extension
from ..parsers.definition_prune import RemovedFile
from ..parsers.factory import ProcessorFactory
from ..types_defs import SimpleNameLookup
from ..utils.path_utils import cached_relative_path


class FileStateSweep:
    """The registry side of removing a file from the updater's state.

    Run before a file is re-parsed and when it is deleted, so a re-parse
    registers its definitions afresh and a deletion leaves nothing to
    resolve into. Ownership comes from records, never from a qn's shape:
    the span records name the module that registered a definition, the
    frontends record theirs per file in `frontend_owned_qns`, and
    `rehydrated_module_qns` is the read-back set a removed module leaves.
    """

    def __init__(
        self,
        factory: ProcessorFactory,
        function_registry: FunctionRegistryTrie,
        simple_name_lookup: SimpleNameLookup,
        repo_path: Path,
        project_name: str,
        frontend_owned_qns: dict[str, set[str]],
        rehydrated_module_qns: set[str],
    ) -> None:
        self._factory = factory
        self._function_registry = function_registry
        self._simple_name_lookup = simple_name_lookup
        self._repo_path = repo_path
        self._project_name = project_name
        self._frontend_owned_qns = frontend_owned_qns
        self._rehydrated_module_qns = rehydrated_module_qns

    def sweep(self, file_path: Path, frontend_current: bool) -> None:
        """Deregister what `file_path`'s last parse recorded."""
        relative_path = cached_relative_path(file_path, self._repo_path)
        recorded_qns, module_qn_prefixes = self._module_qn_prefixes(
            file_path, relative_path
        )
        self._drop_module_import_state(file_path, recorded_qns, module_qn_prefixes)
        owned_qns, foreign_qns = self._span_ownership(
            module_qn_prefixes, relative_path, frontend_current
        )
        qns_to_remove = self._sweep_function_registry(
            file_path, module_qn_prefixes, owned_qns, foreign_qns
        )

        removed = RemovedFile(
            module_qn_prefixes=module_qn_prefixes,
            relative_posix=relative_path.as_posix(),
            qns_to_remove=qns_to_remove,
            owned_qns=owned_qns,
            foreign_qns=foreign_qns,
        )
        self._factory.type_inference.forget_go_return_types(removed)
        # Before `_drop_module_records`: the rehydrated paths it drops are how
        # the class prune finds the file's rehydrated classes.
        self._factory.definition_processor.forget_file(removed)
        self._drop_module_records(file_path, qns_to_remove)

    def _module_qn_prefixes(
        self, file_path: Path, relative_path: Path
    ) -> tuple[set[str], set[str]]:
        """The module qns the parse recorded for the file, and the prefixes
        its definitions are swept under."""
        path_parts = (
            relative_path.parent.parts
            if file_path.name in cs.PY_PACKAGE_INIT_FILES
            else relative_path.with_suffix("").parts
        )
        path_derived_qn = cs.SEPARATOR_DOT.join([self._project_name, *path_parts])
        # Sweep on the qns the parse RECORDED for this path, not on the
        # path-derived form. The two differ whenever `base_module_qn` applies
        # its mod.rs collapse (src/a/mod.rs records as proj.src.a, while the
        # path yields proj.src.a.mod) or `_disambiguate_module_qn` suffixes a
        # qn an earlier-parsed sibling already owns. Keying on the path-derived
        # form swept a qn nothing ever recorded, so a mod.rs deletion left its
        # registry and simple-name entries behind and Pass 3 kept resolving
        # calls into definitions that no longer exist. The Rust and C# import
        # blocks below already re-derive the recorded qn for this reason.
        # A source file that was never parsed recorded nothing; the
        # path-derived form is the only prefix available for it, and it owns
        # no recorded qn that this could wipe.
        #
        # Unless another file holds that qn now. A same-stem survivor re-parses
        # with its claim dropped, after a sibling parsed earlier in the run took
        # the bare qn (`util.c` beside `util.h`); sweeping it deregistered the
        # sibling's fresh definitions, still unflushed and so not read back, and
        # the sibling's calls resolved to nothing until a fresh index (reported
        # on #2586). The survivor's own old state went before the parse.
        #
        # A file no tree-sitter language parses (`shapes.txt`, trybuild's
        # `user.stderr`, a Markdown or ast-grep tier file) never records a
        # module qn, and its path-derived form is the qn of the source file
        # sharing its stem: clearing `pkg/shapes.txt` swept the definitions
        # and class records `pkg/shapes.py` holds. A dependent re-parse of
        # `shapes.py` earlier in the same run then resolved its calls against
        # a registry that had lost them, and the next sync saw nothing to
        # redo (issue #2463). Such a file owns no module state to sweep.
        module_map = self._factory.definition_processor.module_qn_to_file_path
        recorded_qns = {qn for qn, path in module_map.items() if path == file_path}
        if recorded_qns:
            return recorded_qns, recorded_qns
        if (
            get_language_for_extension(file_path.suffix) is not None
            and module_map.get(path_derived_qn) is None
        ):
            return recorded_qns, {path_derived_qn}
        return recorded_qns, set()

    def _drop_module_import_state(
        self, file_path: Path, recorded_qns: set[str], module_qn_prefixes: set[str]
    ) -> None:
        """Drop the import-side state a removed file's module qns recorded."""
        for prefix in module_qn_prefixes:
            self._factory.import_processor.commonjs_direct_exports.pop(prefix, None)
        if file_path.suffix in (*cs.C_EXTENSIONS, *cs.CPP_EXTENSIONS):
            for qn in recorded_qns:
                self._factory.import_processor.drop_cpp_module_import_state(qn)
        self._drop_rust_csharp_import_state(file_path)

    def _drop_rust_csharp_import_state(self, file_path: Path) -> None:
        """Drop the Rust and C# import state keyed on the recorded qns."""
        # A deleted file is no writer: its mod-scope registry entries must
        # not weigh in the next arbitration (a modified file re-drops this
        # in its own re-parse, harmlessly twice). The drop keys on the qn
        # the parse actually RECORDED, which can differ from any path-derived
        # form twice over: mod.rs collapses to its directory, and
        # _disambiguate_module_qn suffixes a qn whose bare form an
        # earlier-parsed file already owns (src/a/mod.rs beside src/a.rs or
        # src/a.py records as ...a.rs). Recomputing from the path would wipe
        # the bare qn's real owner, which this event does not re-parse, while
        # the deleted file's own writers kept voting forever. A Rust file
        # with no recorded qn was never parsed and owns no import state.
        if file_path.suffix == cs.EXT_RS:
            qn_to_path = self._factory.definition_processor.module_qn_to_file_path
            for qn, path in qn_to_path.items():
                if path == file_path:
                    self._factory.import_processor.rust.drop_rust_module_import_state(
                        qn
                    )

        # Same discipline for C#: a deleted file's namespaces, identifier
        # evidence, and using entries must not survive into the next flush
        # (issue #1347).
        if file_path.suffix == cs.EXT_CS:
            qn_to_path = self._factory.definition_processor.module_qn_to_file_path
            csharp = self._factory.import_processor.csharp
            for qn, path in qn_to_path.items():
                if path == file_path:
                    csharp.drop_csharp_module_import_state(qn)

    def _span_ownership(
        self,
        module_qn_prefixes: set[str],
        relative_path: Path,
        frontend_current: bool,
    ) -> tuple[set[str], set[str]]:
        """The qns this file's span records own, and those another file owns."""
        # Ownership guard for the prefix sweep: another file's inline-mod
        # chain can share this file's qn prefix (the #1017 shape), and a
        # sweep by prefix alone deregisters functions that file still owns —
        # they are never re-registered because it is not re-parsed. The span
        # records key by the REGISTERING file's module qn, so a qn recorded
        # under a module this event does not touch survives (issue #1025).
        # Uses the same set the prefix sweep uses, INCLUDING its path-derived
        # fallback. `recorded_qns` is filled only when a file is parsed
        # (`DefinitionProcessor.process_file`), so on a FRESH updater doing a
        # scoped re-ingest it is empty -- and an empty `touched_modules` makes
        # every rehydrated span foreign, which protects the whole graph from a
        # sweep that then removes nothing. The re-parse re-registers a qn the
        # rehydrated registry still holds and lands on a `@1` DUP_QN variant
        # instead of the clean name (issue #1719). The prefix sweep already
        # falls back to the path-derived qn for exactly this case; ownership
        # has to fall back with it, or the two disagree about which modules
        # this event touches.
        touched_modules = module_qn_prefixes
        foreign_qns = set()
        # The mirror of foreign_qns, and the reason the prefix sweep is not
        # sufficient on its own: a definition's qn does not have to sit under
        # the qn of the file that registered it. A Go method is keyed by its
        # RECEIVER TYPE's module, so `func (t T) M()` in pkg/methods.go
        # registers `proj.pkg.types.T.M` when `T` is declared in pkg/types.go.
        # No prefix of methods.go matches that qn, so the entry survived this
        # cleanup and the re-parse registered a second Method beside it as
        # `...T.M@3` (issue #1659). The span records already name the
        # registering module, which is exactly the ownership this needs.
        owned_qns = set()
        for (
            (
                span_module,
                _line,
                _col,
            ),
            location,
        ) in self._factory.facts.function_locations.items():
            if span_module in touched_modules:
                owned_qns.add(location.qualified_name)
            else:
                foreign_qns.add(location.qualified_name)
        # Frontend registrations (libclang C/C++) have no span records; their
        # ownership is tracked per rel path at registration time.
        touched_rel = relative_path.as_posix()
        for owner_rel, owner_qns in self._frontend_owned_qns.items():
            if owner_rel != touched_rel:
                foreign_qns |= owner_qns
        if frontend_current:
            # Incremental full run in LIBCLANG mode: the frontend ALREADY
            # re-registered this file's current state before this cleanup,
            # and the tree-sitter pass will skip it as covered — sweeping
            # here would wipe registrations nothing restores (issue #1025
            # review). Watch events keep the default: the frontend does not
            # rerun there, so its stale entries must go.
            foreign_qns |= self._frontend_owned_qns.get(touched_rel, set())
        return owned_qns, foreign_qns

    def _sweep_function_registry(
        self,
        file_path: Path,
        module_qn_prefixes: set[str],
        owned_qns: set[str],
        foreign_qns: set[str],
    ) -> set[str]:
        """Deregister the removed file's definitions; the qns removed."""
        qns_to_remove = set()

        # A directory may share a sibling FILE's stem, and then the module qns
        # nest: `proj/a.py` records `proj.a` while `proj/a/b.py` records
        # `proj.a.b`. Deleting `a.py` matches the prefix `proj.a.` and would
        # take `b.py`'s definitions with it, though `b.py` still exists and
        # this event does not re-parse it, so nothing restores them (issue
        # #1773). `foreign_qns` does not save them: it is built from function
        # SPAN records, so it protects `proj.a.b.Nested.deep` while leaving
        # the class `proj.a.b.Nested`, which records no span -- exactly the
        # asymmetry measured on `A.cs` beside `A/B.cs`, and reproduced here on
        # `a.py` beside `a/b.py`. The sweep is language-agnostic, so this is
        # not a C#-only shape.
        #
        # Ownership must come from a RECORD, not from the qn's shape. The
        # obvious rule -- keep a qn that also sits under a longer surviving
        # module -- is wrong for C#, whose class qn embeds its namespace:
        # `proj/Core.cs` with `namespace Util` yields `proj.Core.Util.Helper`,
        # sitting under the SIBLING module `proj.Core.Util` from
        # `proj/Core/Util.cs`. That rule would keep `Helper` after deleting
        # the file that declares it (measured, #1844).
        # #1769 hit the same trap and answered it the same way, with an
        # explicit owner record.
        #
        # `class_owner_module` is that record, cross-language and written by
        # every language's ingest (#1772). A qn whose declaring module is
        # still mapped to a file, and is not itself being deleted, belongs to
        # a file that still exists: keep it. A qn with no recorded owner is
        # left to the prefix rule, which is the pre-existing behaviour.
        owner_module = self._factory.definition_processor.class_owner_module
        live_modules = set(self._factory.definition_processor.module_qn_to_file_path)
        # `class_owner_module` is written at INGEST, so a definition carried
        # over by a REHYDRATE has no entry there and would fall back to the
        # prefix rule. `rehydrated_definition_paths` is the record for exactly
        # those, written when the row is read back from the graph, and it
        # already serves as ownership evidence in `definition_prune.forget_file`.
        # Consulting both means an unchanged sibling's class is protected on
        # an incremental run too, not only on the run that parsed it (raised
        # in review of #1844; pre-existing on main, which loses it either way).
        rehydrated = self._factory.facts.rehydrated_definition_paths
        deleted_rel = cached_relative_path(file_path, self._repo_path).as_posix()
        # "Not the file being deleted" is not enough: a definition recorded
        # against a file removed EARLIER also has a different path, and would
        # be preserved as a stale entry that goes on steering call resolution
        # (#1844). Require the recorded path to belong
        # to a module that is still live.
        live_paths = {
            cached_relative_path(path, self._repo_path).as_posix()
            for path in self._factory.definition_processor.module_qn_to_file_path.values()
        }

        def _owned_by_a_surviving_file(qn: str) -> bool:
            owner = owner_module.get(qn)
            if owner is not None:
                return owner not in module_qn_prefixes and owner in live_modules
            # The rehydrated half: owned by a file that is not the one being
            # deleted. Compared as a relative posix path, the form the
            # rehydrate stores.
            rehydrated_path = rehydrated.get(qn)
            if rehydrated_path is not None:
                return (
                    str(rehydrated_path) != deleted_rel
                    and str(rehydrated_path) in live_paths
                )
            return False

        self._deregister_swept_qns(
            module_qn_prefixes,
            owned_qns,
            foreign_qns,
            qns_to_remove,
            _owned_by_a_surviving_file,
        )

        if qns_to_remove:
            logger.debug(ls.REMOVING_QNS, count=len(qns_to_remove))
        return qns_to_remove

    def _deregister_swept_qns(
        self,
        module_qn_prefixes: set[str],
        owned_qns: set[str],
        foreign_qns: set[str],
        qns_to_remove: set[str],
        owned_by_a_surviving_file: Callable[[str], bool],
    ) -> None:
        """Deregister every registry qn the removed file owns."""
        for qn in list(self._function_registry.keys()):
            matched_prefix = any(
                qn.startswith(f"{prefix}.") or qn == prefix
                for prefix in module_qn_prefixes
            )
            if matched_prefix and qn not in owned_qns and owned_by_a_surviving_file(qn):
                # Declared by a file that still exists; the prefix match is an
                # accident of the shared stem. `owned_qns` still wins, since
                # that is this file's own span-record evidence.
                continue
            if (matched_prefix or qn in owned_qns) and qn not in foreign_qns:
                qns_to_remove.add(qn)
                del self._function_registry[qn]

    def _drop_module_records(self, file_path: Path, qns_to_remove: set[str]) -> None:
        """Drop the removed file's simple-name, module and rehydrated records."""
        for simple_name, qn_set in self._simple_name_lookup.items():
            original_count = len(qn_set)
            new_qn_set = qn_set - qns_to_remove
            if len(new_qn_set) < original_count:
                self._simple_name_lookup[simple_name] = new_qn_set
                # Deleting `ILog.Info(string)` invalidated `Info(string)`, not
                # `Info`: a lookup cached for the name would keep answering
                # with the removed definition on a reused updater.
                self._function_registry.invalidate_name(simple_name)
                logger.debug(ls.CLEANED_SIMPLE_NAME, name=simple_name)

        # The file no longer owns any module qn: a replacement with the same
        # stem (util.js -> util.ts) must take the bare qn a clean index gives
        # it, and a re-parse re-registers its own entry (issue #1575).
        qn_to_path = self._factory.definition_processor.module_qn_to_file_path
        # The qn read back from the graph goes with the map entry, keyed on
        # the qn the graph RECORDED (a disambiguated `util.py` beside
        # `util.js`, a `mod.rs` directory), never on the path-derived prefix,
        # which for a same-stem sibling names the SURVIVOR's qn.
        # `known_module_paths` would otherwise keep offering the deleted
        # module to deferred resolution as a live internal target on a
        # reused updater (issue #1575).
        for qn in [qn for qn, path in qn_to_path.items() if path == file_path]:
            del qn_to_path[qn]
            self._rehydrated_module_qns.discard(qn)
        # The paths read back from the graph for an earlier incremental run
        # go with the registry entries: `_module_language` falls through to
        # them for a definition qn, and a replacement in another language
        # (util.rs -> util.py) would otherwise still answer RUST for the qn
        # its new file re-registers (issue #1575).
        rehydrated = self._factory.facts.rehydrated_definition_paths
        for qn in qns_to_remove:
            rehydrated.pop(qn, None)
