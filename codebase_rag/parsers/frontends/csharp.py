"""The C# (Roslyn) `LanguageFrontend` registration (issue #1178): the bundled
Roslyn fact provider, whose run already answers in `SemanticFacts`."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from ...constants.languages import SupportedLanguage
from ...utils.path_utils import frontend_ignored_dirs
from ..csharp_frontend import (
    csharp_frontend_available,
    find_csharp_project,
    run_csharp_frontend,
)
from .protocol import SemanticFacts
from .registry import register_frontend


class CSharpFrontend:
    """Roslyn fact provider for C#."""

    language: SupportedLanguage = SupportedLanguage.CSHARP

    def available(self) -> bool:
        return csharp_frontend_available()

    def applies(self, repo_path: Path) -> bool:
        return find_csharp_project(repo_path) is not None

    def run(
        self,
        repo_path: Path,
        files: Sequence[Path],
        rescued_files: frozenset[str] = frozenset(),
    ) -> SemanticFacts:
        # The frontend loads the whole project in one run: a narrowed file
        # list cannot bind calls whose targets live in the files it omits.
        del files
        return run_csharp_frontend(
            repo_path, ignored_dirs=frontend_ignored_dirs(rescued_files)
        )


register_frontend(CSharpFrontend())
