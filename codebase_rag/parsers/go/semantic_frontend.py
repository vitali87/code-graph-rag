"""The Go (go/packages) `LanguageFrontend` registration (issue #1179): the
bundled gotypes fact provider, whose run already answers in `SemanticFacts`."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from ...constants.languages import SupportedLanguage
from ...utils.path_utils import frontend_ignored_dirs
from ..frontends.protocol import SemanticFacts
from ..frontends.registry import register_frontend
from .frontend import find_go_module, go_frontend_available, run_go_frontend


class GoFrontend:
    """go/packages fact provider for Go."""

    language: SupportedLanguage = SupportedLanguage.GO

    def available(self) -> bool:
        return go_frontend_available()

    def applies(self, repo_path: Path) -> bool:
        return find_go_module(repo_path) is not None

    def run(
        self,
        repo_path: Path,
        files: Sequence[Path],
        rescued_files: frozenset[str] = frozenset(),
    ) -> SemanticFacts:
        # The frontend loads the whole project in one run: a narrowed file
        # list cannot bind calls whose targets live in the files it omits.
        del files
        return run_go_frontend(
            repo_path, ignored_dirs=frontend_ignored_dirs(rescued_files)
        )


register_frontend(GoFrontend())
