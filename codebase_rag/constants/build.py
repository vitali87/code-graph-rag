# (H) PyInstaller binary build constants.

from enum import StrEnum
from typing import NamedTuple


class PyInstallerPackage(NamedTuple):
    name: str
    collect_all: bool = False
    collect_data: bool = False
    hidden_import: str | None = None


class Architecture(StrEnum):
    X86_64 = "x86_64"
    AARCH64 = "aarch64"
    ARM64 = "arm64"
    AMD64 = "amd64"


BINARY_NAME_TEMPLATE = "code-graph-rag-{system}-{machine}"
BINARY_FILE_PERMISSION = 0o755
DIST_DIR = "dist"
BYTES_PER_MB_FLOAT = 1024 * 1024

PYPROJECT_PATH = "pyproject.toml"
PYPROJECT_KEY_TOOL = "tool"
PYPROJECT_KEY_SETUPTOOLS = "setuptools"
PYPROJECT_KEY_PACKAGE_DIR = "package-dir"
TREESITTER_EXTRA_KEY = "treesitter-full"
TREESITTER_PKG_PREFIX = "tree-sitter-"

# (H) PyInstaller CLI constants
PYINSTALLER_CMD = "pyinstaller"
PYINSTALLER_ARG_NAME = "--name"
PYINSTALLER_ARG_ONEFILE = "--onefile"
PYINSTALLER_ARG_NOCONFIRM = "--noconfirm"
PYINSTALLER_ARG_CLEAN = "--clean"
PYINSTALLER_ARG_COLLECT_ALL = "--collect-all"
PYINSTALLER_ARG_COLLECT_DATA = "--collect-data"
PYINSTALLER_ARG_HIDDEN_IMPORT = "--hidden-import"
PYINSTALLER_ARG_EXCLUDE_MODULE = "--exclude-module"
PYINSTALLER_ARG_COPY_METADATA = "--copy-metadata"
PYINSTALLER_ENTRY_POINT = "main.py"

PYINSTALLER_EXCLUDED_MODULES = ["logfire"]

# (H) TOML parsing constants
TOML_KEY_PROJECT = "project"
TOML_KEY_OPTIONAL_DEPS = "optional-dependencies"

# (H) Version string parsing
VERSION_SPLIT_GTE = ">="
VERSION_SPLIT_EQ = "=="
VERSION_SPLIT_LT = "<"

PYINSTALLER_PACKAGES: list["PyInstallerPackage"] = [
    PyInstallerPackage(
        name="pydantic_ai",
        collect_all=True,
        collect_data=True,
        hidden_import="pydantic_ai_slim",
    ),
    PyInstallerPackage(name="rich", collect_all=True),
    PyInstallerPackage(name="typer", collect_all=True),
    PyInstallerPackage(name="loguru", collect_all=True),
    PyInstallerPackage(name="toml", collect_all=True),
    PyInstallerPackage(name="protobuf", collect_all=True),
    PyInstallerPackage(name="genai_prices", collect_all=True),
]
