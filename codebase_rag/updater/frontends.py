"""Run the compiler frontends a sync layers over Tree-sitter, and join the macro
uses the hybrid libclang pass hands back to Tree-sitter's definition spans."""

import os
from collections.abc import Callable, Collection, Iterable, Mapping
from pathlib import Path
from typing import NamedTuple

from loguru import logger

from .. import constants as cs
from .. import logs as ls
from ..config import settings
from ..function_registry import FunctionRegistryTrie
from ..parsers.cpp.frontend import (
    cpp_frontend_available,
    find_compile_commands,
    run_cpp_frontend,
    run_cpp_frontend_hybrid,
)
from ..parsers.csharp.frontend import find_csharp_project
from ..parsers.factory import ProcessorFactory
from ..parsers.frontends import SemanticFacts
from ..parsers.frontends.builtin import FRONTENDS
from ..parsers.go.frontend import find_go_module
from ..services import IngestorProtocol
from ..types_defs import (
    CppDefinitionSpan,
    PendingExpansionCall,
    PendingMacroCall,
    SimpleNameLookup,
)
from ..utils.path_utils import rescued_files, walk_eligible_files
from .types_defs import WalkScope


class _FrontendLaunch(NamedTuple):
    """How a run switches one language's frontend on, and what it reports."""

    off: bool
    unavailable_log: str
    auto: bool = False
    find_root: Callable[[Path], Path | None] | None = None
    auto_fallback_log: str | None = None
    running_log: str | None = None


class FrontendRunner:
    """The compiler frontends of a sync, and the C/C++ joins they leave for
    after the definition pass.

    Each `run_<lang>_frontend` stores what its compiler supplied in the
    factory's `frontend_facts`, where that language's resolver reads it.
    `cpp_covered` names the files LIBCLANG emitted itself, which the
    definition pass skips; HYBRID instead hands back macro uses, joined to
    Tree-sitter's spans once Pass 2 recorded them. `owned_qns` is the
    updater's per-file record of what a frontend registered, and
    `rehydrated_spans` the definition spans an incremental run read back
    from the graph.
    """

    def __init__(
        self,
        sink: IngestorProtocol,
        factory: ProcessorFactory,
        function_registry: FunctionRegistryTrie,
        simple_name_lookup: SimpleNameLookup,
        repo_path: Path,
        project_name: str,
        scope: WalkScope,
        owned_qns: dict[str, set[str]],
        rehydrated_spans: Mapping[str, list[CppDefinitionSpan]],
    ) -> None:
        self._sink = sink
        self._factory = factory
        self._function_registry = function_registry
        self._simple_name_lookup = simple_name_lookup
        self._repo_path = repo_path
        self._project_name = project_name
        self._scope = scope
        self._owned_qns = owned_qns
        self._rehydrated_spans = rehydrated_spans
        self.cpp_covered: frozenset[str] = frozenset()
        # Hybrid-mode macro uses awaiting a caller: attribution needs the
        # tree-sitter definition spans, which exist only after Pass 2.
        self.pending_macro_calls: list[PendingMacroCall] = []
        # Hybrid-mode expansion-produced calls (call text lives inside a
        # macro body): BOTH ends join to tree-sitter spans after Pass 2.
        self.pending_expansion_calls: list[PendingExpansionCall] = []

    def run_cpp_frontend(self) -> None:
        # Optional libclang C++ pre-pass when a compile_commands.json is
        # discoverable. LIBCLANG: emit macro-accurate C/C++ nodes/edges
        # directly (tree-sitter cannot expand macros) and skip covered files
        # in the definition pass. HYBRID: tree-sitter stays the backbone
        # (nothing skipped); libclang layers on only macro Function nodes and
        # #include IMPORTS, whose qns are scheme-identical, and hands back
        # macro uses for span attribution after Pass 2. Missing either
        # condition falls back to tree-sitter.
        # Both pending lists, not just the macro one: an early return below
        # (mode off, libclang absent, no compile_commands.json) would otherwise
        # leave a previous run's expansion calls in place, and the Pass-3
        # consumer would emit CALLS edges for expansions that no longer apply.
        self.cpp_covered = frozenset()
        self.pending_macro_calls = []
        self.pending_expansion_calls = []
        if settings.CPP_FRONTEND not in (
            cs.CppFrontend.LIBCLANG,
            cs.CppFrontend.HYBRID,
        ):
            return
        if not self._repo_has_c_or_cpp_files():
            # HYBRID is the default, so a repo with no C/C++ sources must
            # skip silently instead of warning about libclang or a missing
            # compile_commands.json on every index of a Python/Go project.
            return
        if not cpp_frontend_available():
            logger.warning(ls.CPP_FRONTEND_UNAVAILABLE)
            return
        compdb_dir = find_compile_commands(self._repo_path)
        if compdb_dir is None:
            logger.warning(ls.CPP_FRONTEND_NO_COMPDB)
            return
        logger.info(ls.CPP_FRONTEND_RUNNING.format(path=compdb_dir))
        if settings.CPP_FRONTEND == cs.CppFrontend.HYBRID:
            pending_macro_calls, pending_expansion_calls = run_cpp_frontend_hybrid(
                self._sink,
                self._repo_path,
                self._project_name,
                compdb_dir,
                function_registry=self._function_registry,
                simple_name_lookup=self._simple_name_lookup,
                structural_elements=(
                    self._factory.structure_processor.structural_elements
                ),
                owned_qns=self._owned_qns,
                exclude_paths=self._scope.exclude_paths,
                unignore_paths=self._scope.unignore_paths,
            )
            self.pending_macro_calls = list(pending_macro_calls)
            self.pending_expansion_calls = list(pending_expansion_calls)
            logger.info(
                ls.CPP_FRONTEND_HYBRID_PENDING.format(
                    count=len(self.pending_macro_calls)
                    + len(self.pending_expansion_calls)
                )
            )
            return
        self.cpp_covered = frozenset(
            run_cpp_frontend(
                self._sink,
                self._repo_path,
                self._project_name,
                compdb_dir,
                function_registry=self._function_registry,
                simple_name_lookup=self._simple_name_lookup,
                structural_elements=(
                    self._factory.structure_processor.structural_elements
                ),
                exclude_paths=self._scope.exclude_paths,
                unignore_paths=self._scope.unignore_paths,
                owned_qns=self._owned_qns,
            )
        )
        logger.info(ls.CPP_FRONTEND_COVERED.format(count=len(self.cpp_covered)))

    def run_csharp_frontend(self) -> None:
        # Roslyn before Pass 2: its exact base kinds (INHERITS vs IMPLEMENTS)
        # are read while each type's bases are ingested; its call targets in
        # Pass 3; its partial groups and LINQ query calls join after Pass 2
        # and Pass 3. No dotnet, no project, or a failed build or restore
        # leaves the tree-sitter graph.
        mode = settings.CSHARP_FRONTEND
        facts = self._run_frontend(
            cs.SupportedLanguage.CSHARP,
            _FrontendLaunch(
                off=mode == cs.CSharpFrontend.TREESITTER,
                auto=mode == cs.CSharpFrontend.AUTO,
                find_root=find_csharp_project,
                auto_fallback_log=ls.CSHARP_FRONTEND_AUTO_FALLBACK,
                unavailable_log=ls.CSHARP_FRONTEND_UNAVAILABLE,
                running_log=ls.CSHARP_FRONTEND_RUNNING,
            ),
        )
        if facts is None:
            return
        logger.info(ls.CSHARP_FRONTEND_TYPES.format(count=len(facts.base_kinds)))
        logger.info(
            ls.CSHARP_FRONTEND_FACTS.format(
                calls=len(facts.resolved_call_sites),
                partials=len(facts.partial_groups),
                queries=len(facts.query_calls),
                externals=len(facts.external_sites),
            )
        )

    def run_go_frontend(self) -> None:
        # go/types before Pass 2 (issue #1179): exact first-party call targets
        # for Pass 3, the sites that leave the module, and the implements
        # pairs the interface link joins once Pass 2 located every type.
        mode = settings.GO_FRONTEND
        facts = self._run_frontend(
            cs.SupportedLanguage.GO,
            _FrontendLaunch(
                off=mode == cs.GoFrontend.TREESITTER,
                auto=mode == cs.GoFrontend.AUTO,
                find_root=find_go_module,
                auto_fallback_log=ls.GO_FRONTEND_AUTO_FALLBACK,
                unavailable_log=ls.GO_FRONTEND_UNAVAILABLE,
                running_log=ls.GO_FRONTEND_RUNNING,
            ),
        )
        if facts is not None:
            logger.info(
                ls.GO_FRONTEND_FACTS.format(
                    calls=len(facts.resolved_call_sites),
                    externals=len(facts.external_sites),
                )
            )

    def run_java_frontend(self) -> None:
        # javac AFTER Pass 2 (issue #1181), like Jedi: its call facts join
        # against the function_locations Pass 2 just filled, including the
        # name-token alias the Java method registration adds.
        facts = self._run_frontend(
            cs.SupportedLanguage.JAVA,
            _FrontendLaunch(
                off=settings.JAVA_FRONTEND == cs.JavaFrontend.HEURISTIC,
                unavailable_log=ls.JAVA_FRONTEND_UNAVAILABLE,
            ),
        )
        if facts is not None:
            logger.info(
                ls.JAVA_FRONTEND_FACTS.format(
                    calls=len(facts.resolved_call_sites),
                    externals=len(facts.external_sites),
                )
            )

    def run_python_frontend(
        self, parsed_files: Iterable[tuple[Path, cs.SupportedLanguage]]
    ) -> None:
        # In-process Jedi facts after Pass 2 (issue #1183), over the Python
        # files this run parsed: exact first-party callees through
        # re-exports and decorators, and proofs that a call leaves the repo.
        files = tuple(
            fp for fp, lang in parsed_files if lang == cs.SupportedLanguage.PYTHON
        )
        facts = self._run_frontend(
            cs.SupportedLanguage.PYTHON,
            _FrontendLaunch(
                off=settings.PYTHON_FRONTEND == cs.PythonFrontend.HEURISTIC
                or not files,
                unavailable_log=ls.PY_FRONTEND_UNAVAILABLE,
                running_log=ls.PY_FRONTEND_RUNNING,
            ),
            files=files,
        )
        if facts is not None:
            logger.info(
                ls.PY_FRONTEND_FACTS.format(
                    calls=len(facts.resolved_call_sites),
                    externals=len(facts.external_sites),
                )
            )

    def _run_frontend(
        self,
        language: cs.SupportedLanguage,
        launch: _FrontendLaunch,
        files: tuple[Path, ...] = (),
    ) -> SemanticFacts | None:
        """Run a registered `LanguageFrontend` and store what it supplied.

        The language's bundle is replaced first, so a reused updater (watch
        mode) whose frontend ran before never applies a previous run's facts
        once it is off. Returns the facts, or None when the frontend is off,
        has nothing to augment, or cannot run; the processors then fall back
        to the tree-sitter heuristics.
        """
        store = self._factory.facts.frontend_facts
        store[language] = SemanticFacts()
        if launch.off:
            return None
        frontend = FRONTENDS.get(language)
        if frontend is None or not frontend.applies(self._repo_path):
            # Skip silently when the frontend has nothing to augment (no C#
            # project, no go.mod): applicability is the frontend's own rule
            # (issue #1178), so a registered replacement can define its own.
            return None
        if not frontend.available():
            # AUTO promises the compiler only where the toolchain exists, so a
            # missing toolchain is the expected fallback (info); an EXPLICIT
            # request that cannot run stays a warning.
            if launch.auto and launch.auto_fallback_log is not None:
                logger.info(launch.auto_fallback_log)
            else:
                logger.warning(launch.unavailable_log)
            return None
        if launch.running_log is not None:
            root = (
                launch.find_root(self._repo_path)
                if launch.find_root is not None
                else None
            )
            logger.info(launch.running_log.format(path=root))
        facts = frontend.run(
            self._repo_path, files, rescued_files=self._rescued_files()
        )
        store[language] = facts
        return facts

    def _rescued_files(self) -> frozenset[str]:
        # The compiler tools skip the default-excluded names on their own; the
        # files this walk keeps under one (a tracked `pkg/out/out.go`, a `!`
        # line) must reach them, or they are parsed without their facts.
        return rescued_files(
            self._repo_path, self._scope.exclude_paths, self._scope.unignore_paths
        )

    def _tightest_containing_span(
        self, rel_path: str, line: int, reparsed_keys: Collection[str]
    ) -> CppDefinitionSpan | None:
        spans = self._factory.definition_processor.cpp_definition_spans
        candidates = spans.get(rel_path)
        if candidates is None and rel_path not in reparsed_keys:
            # An unchanged file on an incremental run has no fresh spans;
            # its definitions (and their lines) are unchanged too, so the
            # graph-rehydrated spans are exact.
            candidates = self._rehydrated_spans.get(rel_path)
        containing = [s for s in candidates or () if s.start_line <= line <= s.end_line]
        if not containing:
            return None
        return min(containing, key=lambda s: s.end_line - s.start_line)

    def resolve_hybrid_macro_calls(self, reparsed_keys: Collection[str]) -> None:
        # Attribute each hybrid macro use to the tightest enclosing
        # TREE-SITTER definition span (recorded during Pass 2), falling back
        # to the use site's Module: the mirror of the libclang frontend's own
        # span resolution, but against the qn scheme the rest of the graph
        # actually uses.
        if not self.pending_macro_calls:
            return
        spans = self._factory.definition_processor.cpp_definition_spans
        emitted = 0
        for call in self.pending_macro_calls:
            # The frontend parses every TU each run, but an incremental run
            # records spans only for re-parsed files. An unchanged file has no
            # spans here AND already carries its caller->macro edges, so
            # resolving it would re-attribute in-function uses to the Module.
            if call.rel_path not in reparsed_keys:
                continue
            containing = [
                s
                for s in spans.get(call.rel_path, ())
                if s.start_line <= call.line <= s.end_line
                and s.qualified_name != call.callee_qn
            ]
            if containing:
                tightest = min(containing, key=lambda s: s.end_line - s.start_line)
                caller_label, caller_qn = tightest.label, tightest.qualified_name
            else:
                caller_label = cs.NodeLabel.MODULE.value
                caller_qn = call.fallback_module_qn
            self._sink.ensure_relationship_batch(
                (caller_label, cs.KEY_QUALIFIED_NAME, caller_qn),
                cs.RelationshipType.CALLS,
                (cs.NodeLabel.FUNCTION, cs.KEY_QUALIFIED_NAME, call.callee_qn),
            )
            emitted += 1
        logger.info(ls.CPP_FRONTEND_MACRO_CALLS.format(count=emitted))

    def resolve_hybrid_expansion_calls(self, reparsed_keys: Collection[str]) -> None:
        # A call whose text lives inside a macro body exists only after
        # expansion, so tree-sitter never emits it. Join BOTH ends to
        # tree-sitter definition spans: the caller by expansion site (Module
        # fallback, like macro uses), the callee by its referenced
        # definition's location (dropped when no span contains it, since an
        # unindexed or template-only definition has no tree-sitter node to
        # target).
        if not self.pending_expansion_calls:
            return
        emitted = 0
        for call in self.pending_expansion_calls:
            if call.caller_rel_path not in reparsed_keys:
                continue
            callee = self._tightest_containing_span(
                call.callee_rel_path, call.callee_line, reparsed_keys
            )
            if callee is None:
                continue
            caller = self._tightest_containing_span(
                call.caller_rel_path, call.caller_line, reparsed_keys
            )
            if caller is not None:
                caller_label: str = caller.label
                caller_qn = caller.qualified_name
            else:
                caller_label = cs.NodeLabel.MODULE.value
                caller_qn = call.fallback_module_qn
            self._sink.ensure_relationship_batch(
                (caller_label, cs.KEY_QUALIFIED_NAME, caller_qn),
                cs.RelationshipType.CALLS,
                (callee.label, cs.KEY_QUALIFIED_NAME, callee.qualified_name),
            )
            emitted += 1
        logger.info(ls.CPP_FRONTEND_EXPANSION_CALLS.format(count=emitted))

    def _repo_has_c_or_cpp_files(self) -> bool:
        # Early-exit scan: the frontend (and its warnings) only make sense
        # when there is C/C++ to index. The indexer's own walk, not the raw
        # tree: a `.c` under an excluded `vendor/` or a default-ignored
        # `node_modules/` is never parsed, so there is no fidelity to lose
        # and nothing to warn about (issue #2883).
        extensions = set(cs.CPP_EXTENSIONS) | set(cs.C_EXTENSIONS)
        # splitext, not Path(): no object allocation per repo file.
        return any(
            os.path.splitext(fname)[1].lower() in extensions
            for _dirpath, fname, _rel in walk_eligible_files(
                self._repo_path, self._scope.exclude_paths, self._scope.unignore_paths
            )
        )
