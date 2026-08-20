"""The C# (Roslyn) `LanguageFrontend` adapter (issue #1178). A thin projection of
the existing `run_csharp_frontend` fact bundle onto the generic `SemanticFacts`
contract -- zero behaviour change; the Roslyn tool contract is untouched."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from ...constants.languages import SupportedLanguage
from ..csharp_frontend import (
    CSharpSemanticFacts,
    csharp_frontend_available,
    find_csharp_project,
    run_csharp_frontend,
)
from .protocol import QueryCall, ResolvedCallSite, SemanticFacts
from .registry import register_frontend


def _adapt_csharp_semantic_facts(facts: CSharpSemanticFacts) -> SemanticFacts:
    # CSharpCallSite and ResolvedCallSite are structurally identical (name +
    # target_file/line/col); QueryCall likewise. The remaining families copy across.
    return SemanticFacts(
        resolved_call_sites={
            key: ResolvedCallSite(
                site.name, site.target_file, site.target_line, site.target_col
            )
            for key, site in facts.call_sites.items()
        },
        external_sites=set(facts.external_sites),
        arg_flows=facts.arg_flows,
        bind_flows=facts.bind_flows,
        base_kinds=facts.base_kinds,
        partial_groups=facts.partial_groups,
        query_calls=[
            QueryCall(
                q.caller_file,
                q.caller_line,
                q.caller_col,
                q.target_file,
                q.target_line,
                q.target_col,
            )
            for q in facts.query_calls
        ],
    )


class CSharpFrontend:
    """Roslyn fact provider for C#."""

    language: SupportedLanguage = SupportedLanguage.CSHARP

    def available(self) -> bool:
        return csharp_frontend_available()

    def applies(self, repo_path: Path) -> bool:
        return find_csharp_project(repo_path) is not None

    def run(self, repo_path: Path, files: Sequence[Path]) -> SemanticFacts:
        return _adapt_csharp_semantic_facts(run_csharp_frontend(repo_path))


register_frontend(CSharpFrontend())
