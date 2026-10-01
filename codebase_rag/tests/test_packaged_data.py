# Issue #2399: `cgr diff-index` refused every index an installed cgr wrote.
# The provenance manifest hashes codec/schema.proto, pyproject declared
# package-data only for `codebase_rag`, so the wheel shipped no schema, every
# manifest recorded `codec_schema_sha256: null`, and diff-index worked only
# from a source checkout. These tests look at what the wheel actually holds
# and at what an installed copy resolves at runtime, which a source checkout
# can never show.
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tomllib
import zipfile
from fnmatch import fnmatch
from pathlib import Path, PurePosixPath

import pytest

from codebase_rag import constants as cs
from codebase_rag import parser_loader
from codebase_rag.analyzers import ast_grep_analyzer
from codebase_rag.parsers import ast_grep_tier
from codebase_rag.parsers.csharp_frontend import frontend as csharp_frontend
from codebase_rag.parsers.go_frontend import frontend as go_frontend
from codebase_rag.parsers.java_frontend import frontend as java_frontend
from codebase_rag.services import provenance
from codebase_rag.stack import constants as stack_cs
from codebase_rag.stack import manager as stack_manager
from codebase_rag.trace import agents as trace_agents

_ROOT = Path(__file__).resolve().parents[2]
_CONFIG = tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
_SETUPTOOLS = _CONFIG["tool"]["setuptools"]
_PYTHON_SUFFIXES = (".py", ".pyi")
_DIST_INFO = ".dist-info/"

# No [build-system] table, so every PEP 517 frontend (`uv build` included)
# falls back to setuptools' legacy backend; building through the same backend
# keeps this wheel's file list identical to the published one.
_BUILD_WHEEL = (
    "import sys\n"
    "from setuptools.build_meta import __legacy__ as backend\n"
    "backend.build_wheel(sys.argv[1])\n"
)

# Runs from an unpacked wheel, so `codec` and `codebase_rag` resolve exactly
# as they do in site-packages after `pip install`.
_INSTALLED_PROBE = (
    "import json, sys\n"
    "from pathlib import Path\n"
    "import codec\n"
    "from codebase_rag.services import provenance\n"
    "manifest = provenance.build_manifest(Path(sys.argv[1]), {}, {})\n"
    "print(json.dumps([codec.__file__, provenance.__file__,"
    " manifest['codec_schema_sha256']]))\n"
)


def _runtime_data_files() -> list[Path]:
    """Every non-Python file the package reads from its own install directory.

    Each entry comes from the constant the loading code itself uses, so a
    loader that moves or starts reading a new file changes this list with it.
    """
    highlights = Path(parser_loader.__file__).parent / "queries" / "highlights"
    compose = (
        Path(stack_manager.__file__).resolve().parent
        / stack_cs.PACKAGE_COMPOSE_RELATIVE
    )
    files = [
        provenance._SCHEMA_FILE,
        compose,
        java_frontend._TOOL_SRC / java_frontend._TOOL_SOURCE,
        *(go_frontend._TOOL_SRC / name for name in go_frontend._TOOL_SOURCES),
        *(csharp_frontend._TOOL_SRC / name for name in csharp_frontend._TOOL_SOURCES),
        *ast_grep_tier._PATTERNS_DIR.glob("*.yaml"),
        *ast_grep_analyzer._RULES_DIR.glob("*/*.yaml"),
        *highlights.glob("*.scm"),
        *trace_agents.AGENT_FILES,
    ]
    return sorted({path.resolve() for path in files})


_RUNTIME_DATA = _runtime_data_files()
_RUNTIME_WHEEL_PATHS = frozenset(
    path.relative_to(_ROOT).as_posix() for path in _RUNTIME_DATA
)


def _declares(relative: str) -> bool:
    package, _, inside = relative.partition("/")
    patterns = _SETUPTOOLS.get("package-data", {}).get(package, [])
    return any(fnmatch(inside, pattern) for pattern in patterns)


def test_the_runtime_data_walk_finds_every_loader() -> None:
    # An empty glob (a renamed directory) would make every check below pass
    # vacuously, so the walk itself must see each kind of file it claims to.
    suffixes = {path.suffix for path in _RUNTIME_DATA}
    assert {".proto", ".yaml", ".scm", ".go", ".cs", ".java"} <= suffixes
    assert all(path.is_file() for path in _RUNTIME_DATA)


@pytest.mark.parametrize("relative", sorted(_RUNTIME_WHEEL_PATHS))
def test_every_runtime_data_file_is_declared_package_data(relative: str) -> None:
    assert _declares(relative), (
        f"{relative} is read at runtime but no [tool.setuptools.package-data] "
        "pattern covers it, so an installed wheel will not contain it"
    )


def _copy_project(dest: Path) -> None:
    # A copy, not the checkout: setuptools reuses an existing build/lib, so an
    # in-place build can ship stale files a clean build would not, and it
    # litters the checkout with build/ and *.egg-info.
    project = _CONFIG["project"]
    for name in ("pyproject.toml", project["readme"], "LICENSE"):
        shutil.copy2(_ROOT / name, dest / name)
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc", "*.egg-info")
    for pattern in _SETUPTOOLS["packages"]["find"]["include"]:
        package = pattern.rstrip("*")
        shutil.copytree(_ROOT / package, dest / package, ignore=ignore)


@pytest.fixture(scope="module")
def built_wheel(tmp_path_factory: pytest.TempPathFactory) -> Path:
    pytest.importorskip("setuptools")
    project = tmp_path_factory.mktemp("project")
    dist = tmp_path_factory.mktemp("dist")
    _copy_project(project)
    subprocess.run(
        [sys.executable, "-c", _BUILD_WHEEL, str(dist)],
        cwd=project,
        check=True,
        capture_output=True,
        timeout=300,
    )
    (wheel,) = dist.glob("*.whl")
    return wheel


@pytest.fixture(scope="module")
def wheel_names(built_wheel: Path) -> frozenset[str]:
    with zipfile.ZipFile(built_wheel) as archive:
        return frozenset(archive.namelist())


def test_the_codec_schema_ships_in_the_wheel(wheel_names: frozenset[str]) -> None:
    schema = provenance._SCHEMA_FILE.resolve().relative_to(_ROOT).as_posix()
    assert schema in wheel_names, sorted(
        name for name in wheel_names if name.startswith("codec/")
    )


def test_every_runtime_data_file_ships_in_the_wheel(
    wheel_names: frozenset[str],
) -> None:
    assert not sorted(_RUNTIME_WHEEL_PATHS - wheel_names)


def test_an_installed_wheel_records_the_checkout_schema_hash(
    built_wheel: Path, tmp_path: Path
) -> None:
    site = tmp_path / "site"
    index_dir = tmp_path / "index"
    index_dir.mkdir()
    with zipfile.ZipFile(built_wheel) as archive:
        archive.extractall(site)
    # PYTHONPATH replaces the checkout on the path; cwd is kept off it too,
    # so nothing but the unpacked wheel can answer the imports.
    env = {**os.environ, "PYTHONPATH": str(site)}
    probe = subprocess.run(
        [sys.executable, "-c", _INSTALLED_PROBE, str(index_dir)],
        cwd=tmp_path,
        env=env,
        check=True,
        capture_output=True,
        text=True,
        encoding=cs.ENCODING_UTF8,
        timeout=300,
    )
    codec_file, provenance_file, schema_hash = json.loads(
        probe.stdout.strip().splitlines()[-1]
    )

    assert Path(codec_file).is_relative_to(site)
    assert Path(provenance_file).is_relative_to(site)
    # Equal to the checkout's hash, not merely non-null: an index written by
    # an installed cgr must stay diffable against one from a source checkout.
    expected = hashlib.sha256(provenance._SCHEMA_FILE.read_bytes()).hexdigest()
    assert schema_hash == expected


def test_the_wheel_ships_no_tests_or_bytecode(wheel_names: frozenset[str]) -> None:
    payload = [name for name in wheel_names if _DIST_INFO not in name]
    assert not [name for name in payload if "tests" in PurePosixPath(name).parts]
    assert not [
        name for name in payload if "__pycache__" in name or name.endswith(".pyc")
    ]


def test_the_wheel_ships_no_data_the_package_does_not_read(
    wheel_names: frozenset[str],
) -> None:
    # The fix must name the schema, not widen package-data to "*" or turn on
    # include-package-data: the package tree also holds a README and
    # .gitignore files the code never reads.
    stray = sorted(
        name
        for name in wheel_names
        if _DIST_INFO not in name
        and not name.endswith(_PYTHON_SUFFIXES)
        and name not in _RUNTIME_WHEEL_PATHS
    )
    assert not stray
