"""A repo cgr indexes cannot make it build or import a grammar.

When no grammar package is installed for a language, the loader falls back
to a grammar submodule. It used to look for one under `grammars/` in the
working directory, and an MCP client starts cgr inside the repo it indexes,
so a repo carrying `grammars/tree-sitter-go/` had its `setup.py` run and its
binding imported the first time cgr met a `.go` file. The fallback now reads
only cgr's own checkout, where `cgr language add-grammar` puts grammars.

Every planted file writes a marker when Python runs it, so these tests catch
the code running, not merely the loader returning nothing.
"""

from __future__ import annotations

import importlib.machinery
import sys
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest

from codebase_rag import constants as cs
from codebase_rag import parser_loader

GO = cs.SupportedLanguage.GO


def _marker_script(marker: Path) -> str:
    return f"open({str(marker)!r}, 'w').close()\n"


def _plant_grammar(repo: Path, lang: str, markers: Path) -> list[Path]:
    """Plant a grammar submodule under `repo`; return the markers it can write.

    Both setup.py spots are covered: the submodule root, where the loader
    runs one, and bindings/python. The binding is a built-looking package
    whose __init__.py runs on import.
    """
    submodule = repo / "grammars" / f"tree-sitter-{lang}"
    bindings = submodule / "bindings" / "python"
    package = bindings / f"tree_sitter_{lang.replace('-', '_')}"
    package.mkdir(parents=True)
    planted = {
        submodule / "setup.py": markers / f"{lang}-root-setup-ran",
        bindings / "setup.py": markers / f"{lang}-bindings-setup-ran",
        package / "__init__.py": markers / f"{lang}-binding-imported",
    }
    for script, marker in planted.items():
        body = _marker_script(marker)
        if script.name == "__init__.py":
            body += "def language():\n    return 0\n"
        script.write_text(body, encoding="utf-8")
    suffix = importlib.machinery.EXTENSION_SUFFIXES[0]
    (package / f"_binding{suffix}").write_bytes(b"")
    return list(planted.values())


@pytest.fixture
def markers(tmp_path: Path) -> Path:
    path = tmp_path / "markers"
    path.mkdir()
    return path


@pytest.fixture
def untrusted_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    repo = tmp_path / "cloned-repo"
    (repo / "cmd").mkdir(parents=True)
    (repo / "cmd" / "main.go").write_text("package main\n", encoding="utf-8")
    monkeypatch.chdir(repo)
    return repo


@pytest.fixture
def installed_package(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    # What pip, pipx and `uvx` leave: the package in site-packages, with no
    # checkout around it. That is how the MCP registry entry runs cgr.
    package = tmp_path / "site-packages" / "codebase_rag"
    package.mkdir(parents=True)
    # raising=False keeps the fixture usable against a build without the
    # hook, so the regression tests fail on behaviour rather than on setup.
    monkeypatch.setattr(parser_loader, "_package_dir", lambda: package, raising=False)
    return package


@pytest.fixture(params=["installed", "checkout"], ids=["installed-cgr", "cgr-checkout"])
def cgr_location(request: pytest.FixtureRequest) -> Path:
    # Neither an installed cgr nor one run from its own checkout may read
    # the working directory's grammars.
    fixture = "installed_package" if request.param == "installed" else "cgr_checkout"
    return request.getfixturevalue(fixture)


@pytest.fixture
def fresh_loader() -> Iterator[None]:
    # As in a new process: no grammar loaded yet, no grammar module imported.
    with (
        patch.dict(parser_loader._loader_cache, clear=True),
        patch.dict(sys.modules),
    ):
        sys.modules.pop("tree_sitter_go", None)
        yield


def _ran(markers: list[Path]) -> list[str]:
    return [marker.name for marker in markers if marker.exists()]


@pytest.mark.usefixtures("cgr_location", "fresh_loader")
def test_a_grammar_planted_in_the_working_directory_is_never_run(
    untrusted_repo: Path, markers: Path
) -> None:
    planted = _plant_grammar(untrusted_repo, GO, markers)
    # A base install has no Go grammar package, so the loader falls back.
    with patch.object(parser_loader, "_import_pip_grammar", return_value=None):
        assert parser_loader._get_language_library(GO) is None

    assert _ran(planted) == []
    module = sys.modules.get("tree_sitter_go")
    assert module is None or not str(getattr(module, "__file__", "")).startswith(
        str(untrusted_repo)
    )


@pytest.mark.usefixtures("cgr_location", "fresh_loader")
def test_a_grammar_planted_in_the_working_directory_is_not_reported_installed(
    untrusted_repo: Path, markers: Path
) -> None:
    planted = _plant_grammar(untrusted_repo, GO, markers)
    with patch.object(parser_loader, "_import_pip_grammar", return_value=None):
        assert parser_loader.grammar_installed(GO) is False

    assert _ran(planted) == []


@pytest.mark.usefixtures("installed_package", "fresh_loader")
def test_an_installed_cgr_never_builds_a_submodule(
    untrusted_repo: Path, markers: Path
) -> None:
    # Not even by way of the subprocess the build would start.
    _plant_grammar(untrusted_repo, GO, markers)
    with (
        patch.object(parser_loader, "_import_pip_grammar", return_value=None),
        patch.object(parser_loader.subprocess, "run") as run,
    ):
        assert parser_loader._get_language_library(GO) is None
    run.assert_not_called()


@pytest.mark.usefixtures("untrusted_repo")
def test_a_grammar_in_cgrs_own_checkout_still_loads(cgr_checkout: Path) -> None:
    # What `cgr language add-grammar` registers keeps working, whatever the
    # working directory is. The name is cgr's own, so no installed grammar
    # package answers instead.
    lang = "cgrlocal"
    package = (
        cgr_checkout
        / "grammars"
        / f"tree-sitter-{lang}"
        / "bindings"
        / "python"
        / f"tree_sitter_{lang}"
    )
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(
        "def language():\n    return 42\n", encoding="utf-8"
    )
    with (
        patch.dict(parser_loader._loader_cache, clear=True),
        patch.dict(sys.modules),
    ):
        loader = parser_loader._get_language_library(lang)
        assert loader is not None
        assert loader() == 42
        assert parser_loader._submodule_path(lang) == package.parents[2]
