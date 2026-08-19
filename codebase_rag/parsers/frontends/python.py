"""Python LanguageFrontend registration (issue #1183): the in-process Jedi
fact provider. No subprocess, no toolchain — availability is just the
optional `jedi` import (the `python-semantics` extra)."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from ...constants.languages import SupportedLanguage
from ..py_frontend import python_frontend_available, run_python_frontend
from .protocol import SemanticFacts
from .registry import register_frontend


class PythonJediFrontend:
    """Jedi fact provider for Python."""

    language: SupportedLanguage = SupportedLanguage.PYTHON

    def available(self) -> bool:
        return python_frontend_available()

    def applies(self, repo_path: Path) -> bool:
        # Effectively always true: the updater gates on parsed .py files
        # anyway, so existence is the only meaningful precondition.
        return repo_path.exists()

    def run(self, repo_path: Path, files: Sequence[Path]) -> SemanticFacts:
        return run_python_frontend(repo_path, list(files))


register_frontend(PythonJediFrontend())
