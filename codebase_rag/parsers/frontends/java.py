"""Java LanguageFrontend registration (issue #1181): the bundled javac fact
provider. Availability is a working JDK; the tool builds once into the cache
under the shared build lock."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from ...constants.languages import SupportedLanguage
from ..java_frontend import java_frontend_available, run_java_frontend
from .protocol import ResolvedCallSite, SemanticFacts
from .registry import register_frontend


def _adapt_java_semantic_facts(facts) -> SemanticFacts:
    # JavaCallSite and ResolvedCallSite are structurally identical.
    return SemanticFacts(
        resolved_call_sites={
            key: ResolvedCallSite(
                site.name, site.target_file, site.target_line, site.target_col
            )
            for key, site in facts.call_sites.items()
        },
        external_sites=set(facts.external_sites),
    )


class JavaJavacFrontend:
    """Compiler Tree API fact provider for Java."""

    language: SupportedLanguage = SupportedLanguage.JAVA

    def available(self) -> bool:
        return java_frontend_available()

    def applies(self, repo_path: Path) -> bool:
        return repo_path.exists()

    def run(self, repo_path: Path, files: Sequence[Path]) -> SemanticFacts:
        # Stage 1 attributes the whole repo in one javac run: a narrowed file
        # list cannot bind calls whose targets live in the files it omits.
        del files
        return _adapt_java_semantic_facts(run_java_frontend(repo_path))


register_frontend(JavaJavacFrontend())
