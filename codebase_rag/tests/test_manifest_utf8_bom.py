"""Issue #2921: a manifest saved with a UTF-8 byte-order mark keeps its
dependencies.

Windows Notepad and older Visual Studio defaults write a BOM, and package
managers ignore it (pip reads both lines of such a `requirements.txt`, npm
and `require()` strip it from `package.json`). cgr read manifests as plain
UTF-8, so the BOM stayed: `requirements.txt` silently lost its first
requirement, and `package.json`, `composer.json`, `pyproject.toml` and
`Cargo.toml` lost all of them.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag.parsers.dependency_parser import read_manifest
from codebase_rag.parsers.js_ts.module_paths import _read_manifest as _read_package_json
from codebase_rag.parsers.python_source_roots import _setuptools_package_dir

BOM = b"\xef\xbb\xbf"

MANIFESTS: dict[str, tuple[str, set[str]]] = {
    "requirements.txt": ("requests==2.31.0\nflask==3.0.0\n", {"requests", "flask"}),
    "package.json": (
        '{"name": "web", "dependencies": {"react": "^18.0.0", "vite": "^5.0.0"}}\n',
        {"react", "vite"},
    ),
    "composer.json": (
        '{"require": {"monolog/monolog": "^3.0", "guzzlehttp/guzzle": "^7.0"}}\n',
        {"monolog/monolog", "guzzlehttp/guzzle"},
    ),
    "pyproject.toml": (
        '[project]\nname = "app"\ndependencies = ["httpx>=0.27", "rich"]\n',
        {"httpx", "rich"},
    ),
    "Cargo.toml": (
        '[package]\nname = "app"\n\n[dependencies]\nserde = "1"\ntokio = "1"\n',
        {"serde", "tokio"},
    ),
    "go.mod": (
        "module example.com/app\n\ngo 1.21\n\nrequire github.com/pkg/errors v0.9.1\n",
        {"github.com/pkg/errors"},
    ),
    "Gemfile": ("source 'https://rubygems.org'\ngem 'rails'\n", {"rails"}),
    "pubspec.yaml": ("name: app\ndependencies:\n  http: ^1.0.0\n", {"http"}),
}

# The ones a BOM emptied or shortened.
DROPPED = [
    "requirements.txt",
    "package.json",
    "composer.json",
    "pyproject.toml",
    "Cargo.toml",
]


def _names(tmp_path: Path, manifest: str, bom: bool) -> set[str]:
    text, _expected = MANIFESTS[manifest]
    path = tmp_path / manifest
    path.write_bytes((BOM if bom else b"") + text.encode())
    parsed = read_manifest(path)
    assert parsed.unparsable is None
    return {dep.name for dep in parsed.dependencies}


@pytest.mark.parametrize("manifest", DROPPED)
def test_a_bom_prefixed_manifest_keeps_every_dependency(
    tmp_path: Path, manifest: str
) -> None:
    assert _names(tmp_path, manifest, bom=True) == MANIFESTS[manifest][1]


# Negative: what must not change.


@pytest.mark.parametrize("manifest", sorted(MANIFESTS))
def test_a_manifest_without_a_bom_reads_as_before(
    tmp_path: Path, manifest: str
) -> None:
    assert _names(tmp_path, manifest, bom=False) == MANIFESTS[manifest][1]


@pytest.mark.parametrize("manifest", sorted(set(MANIFESTS) - set(DROPPED)))
def test_a_bom_never_hurt_the_other_manifests(tmp_path: Path, manifest: str) -> None:
    assert _names(tmp_path, manifest, bom=True) == MANIFESTS[manifest][1]


# The same files read for module resolution, not dependencies.


@pytest.mark.parametrize("bom", [True, False], ids=["bom", "no-bom"])
def test_a_package_json_read_for_module_paths_keeps_its_exports(
    tmp_path: Path, bom: bool
) -> None:
    (tmp_path / "package.json").write_bytes(
        (BOM if bom else b"") + b'{"name": "lib", "exports": {".": "./src/index.js"}}'
    )
    assert _read_package_json(tmp_path) == {
        "name": "lib",
        "exports": {".": "./src/index.js"},
    }


@pytest.mark.parametrize("bom", [True, False], ids=["bom", "no-bom"])
def test_a_pyproject_read_for_source_roots_keeps_its_package_dir(
    tmp_path: Path, bom: bool
) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_bytes(
        (BOM if bom else b"") + b'[tool.setuptools.package-dir]\n"" = "src"\n'
    )
    assert _setuptools_package_dir(pyproject) == {"": "src"}
