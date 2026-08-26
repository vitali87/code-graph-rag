"""The C++ (libclang) `EmittingFrontend` adapter (issue #1178).

Unlike the fact-provider frontends, libclang contributes graph elements the
tree-sitter backbone cannot produce at all -- expanded macros and `#include`
edges -- so it writes through the ingestor rather than returning a bundle.
That is what `EmittingFrontend` exists for.

Two modes, and the phase is what distinguishes them:

- HYBRID (the default) layers macro `Function` nodes and `#include` IMPORTS
  onto the tree-sitter index and hands back macro uses to be joined to
  tree-sitter spans, so it must run AFTER_DEFINITIONS -- there is nothing to
  attribute to before that pass.
- LIBCLANG emits its own definition nodes and reports the files it covered so
  the definition pass can skip them, so it must run BEFORE_DEFINITIONS.

Getting the phase wrong does not raise. HYBRID running early would find no
spans and silently attribute nothing, which is why the phase is asserted in
`test_cpp_emitting_frontend.py` rather than left to review.
"""

from __future__ import annotations

from pathlib import Path

from ... import constants as cs
from ...config import settings
from ..cpp_frontend import (
    cpp_frontend_available,
    find_compile_commands,
    run_cpp_frontend,
    run_cpp_frontend_hybrid,
)
from .protocol import FrontendEmitContext, FrontendEmitResult, FrontendPhase
from .registry import register_emitting_frontend

_EMITTING_MODES = (cs.CppFrontend.LIBCLANG, cs.CppFrontend.HYBRID)


class CppLibclangFrontend:
    """libclang macro/include provider for C and C++."""

    language: cs.SupportedLanguage = cs.SupportedLanguage.CPP

    @property
    def phase(self) -> FrontendPhase:
        # Read at access time rather than bound at import: the mode is a
        # setting, and binding it here would freeze whichever value happened
        # to be loaded when this module was first imported.
        if settings.CPP_FRONTEND == cs.CppFrontend.LIBCLANG:
            return FrontendPhase.BEFORE_DEFINITIONS
        return FrontendPhase.AFTER_DEFINITIONS

    def available(self) -> bool:
        """Whether libclang can run at all.

        Answers rather than raises: a missing libclang must degrade to the
        tree-sitter backbone, which covers every file anyway.
        """
        return settings.CPP_FRONTEND in _EMITTING_MODES and cpp_frontend_available()

    def applies(self, repo_path: Path) -> bool:
        """True only when a compilation database is discoverable.

        libclang needs the real compile commands; without them there is
        nothing to parse, and the caller falls back rather than warning on
        every index of a repository with no C or C++ in it.
        """
        return find_compile_commands(repo_path) is not None

    def emit(self, ctx: FrontendEmitContext) -> FrontendEmitResult:
        if settings.CPP_FRONTEND == cs.CppFrontend.HYBRID:
            pending_macro_calls, pending_expansion_calls = run_cpp_frontend_hybrid(
                ctx.ingestor,
                ctx.repo_path,
                ctx.project_name,
                ctx.compdb_dir,
                function_registry=ctx.function_registry,
                simple_name_lookup=ctx.simple_name_lookup,
                structural_elements=ctx.structural_elements,
                owned_qns=ctx.owned_qns,
                exclude_paths=ctx.exclude_paths,
                unignore_paths=ctx.unignore_paths,
            )
            return FrontendEmitResult(
                pending_macro_calls=list(pending_macro_calls),
                pending_expansion_calls=list(pending_expansion_calls),
            )
        covered = run_cpp_frontend(
            ctx.ingestor,
            ctx.repo_path,
            ctx.project_name,
            ctx.compdb_dir,
            function_registry=ctx.function_registry,
            simple_name_lookup=ctx.simple_name_lookup,
            structural_elements=ctx.structural_elements,
            exclude_paths=ctx.exclude_paths,
            unignore_paths=ctx.unignore_paths,
            owned_qns=ctx.owned_qns,
        )
        return FrontendEmitResult(covered_files=frozenset(covered))


register_emitting_frontend(CppLibclangFrontend())
