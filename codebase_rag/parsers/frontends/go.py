"""The Go (go/packages) `LanguageFrontend` adapter (issue #1179). A thin
projection of the `run_go_frontend` fact bundle onto the generic `SemanticFacts`
contract -- zero behaviour change; the gotypes tool contract is untouched."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from ...constants.languages import SupportedLanguage
from ..go_frontend import (
    GoSemanticFacts,
    find_go_module,
    go_frontend_available,
    run_go_frontend,
)
from .protocol import ResolvedCallSite, SemanticFacts
from .registry import register_frontend


def _adapt_go_semantic_facts(facts: GoSemanticFacts) -> SemanticFacts:
    # GoCallSite and ResolvedCallSite are structurally identical (name +
    # target_file/line/col). The Go frontend fills only the two call families;
    # base_kinds/partial_groups/query_calls stay at their empty defaults.
    return SemanticFacts(
        resolved_call_sites={
            key: ResolvedCallSite(
                site.name, site.target_file, site.target_line, site.target_col
            )
            for key, site in facts.call_sites.items()
        },
        external_sites=set(facts.external_sites),
    )


class GoFrontend:
    """go/packages fact provider for Go."""

    language: SupportedLanguage = SupportedLanguage.GO

    def available(self) -> bool:
        return go_frontend_available()

    def applies(self, repo_path: Path) -> bool:
        return find_go_module(repo_path) is not None

    def run(self, repo_path: Path, files: Sequence[Path]) -> SemanticFacts:
        return _adapt_go_semantic_facts(run_go_frontend(repo_path))


register_frontend(GoFrontend())
