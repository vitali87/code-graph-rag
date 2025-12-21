#!/usr/bin/env python3

import os
import platform
import subprocess
import sys
from pathlib import Path

import toml
from loguru import logger

from codebase_rag import logs
from codebase_rag.constants import (
    BINARY_FILE_PERMISSION,
    BINARY_NAME_TEMPLATE,
    BYTES_PER_MB_FLOAT,
    DIST_DIR,
    PYINSTALLER_PACKAGES,
    PYPROJECT_PATH,
    TREESITTER_EXTRA_KEY,
    TREESITTER_PKG_PREFIX,
    Architecture,
)
from codebase_rag.types_defs import PyInstallerPackage


def _get_treesitter_packages() -> list[str]:
    pyproject = toml.load(PYPROJECT_PATH)
    extras = pyproject.get("project", {}).get("optional-dependencies", {})
    treesitter_deps = extras.get(TREESITTER_EXTRA_KEY, [])

    packages: list[str] = []
    for dep in treesitter_deps:
        pkg_name = dep.split(">=")[0].split("==")[0].split("<")[0].strip()
        if pkg_name.startswith(TREESITTER_PKG_PREFIX):
            module_name = pkg_name.replace("-", "_")
            packages.append(module_name)
    return packages


def _build_package_args(pkg: PyInstallerPackage) -> list[str]:
    args: list[str] = []
    if pkg.get("collect_all"):
        args.extend(["--collect-all", pkg["name"]])
    if pkg.get("collect_data"):
        args.extend(["--collect-data", pkg["name"]])
    if hidden := pkg.get("hidden_import"):
        args.extend(["--hidden-import", hidden])
    return args


def build_binary() -> bool:
    system = platform.system().lower()
    machine = platform.machine().lower()
    match machine:
        case Architecture.X86_64:
            machine = Architecture.AMD64
        case Architecture.AARCH64 | Architecture.ARM64:
            machine = Architecture.ARM64

    binary_name = BINARY_NAME_TEMPLATE.format(system=system, machine=machine)

    cmd = [
        "pyinstaller",
        "--name",
        binary_name,
        "--onefile",
        "--noconfirm",
        "--clean",
    ]

    for ts_pkg in _get_treesitter_packages():
        cmd.extend(["--collect-all", ts_pkg])

    for pkg in PYINSTALLER_PACKAGES:
        cmd.extend(_build_package_args(pkg))

    cmd.append("main.py")

    logger.info(logs.BUILD_BINARY.format(name=binary_name))
    logger.info(logs.BUILD_PROGRESS)

    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
        logger.success(logs.BUILD_SUCCESS)

        binary_path = Path(DIST_DIR) / binary_name
        if binary_path.exists():
            size_mb = binary_path.stat().st_size / BYTES_PER_MB_FLOAT
            logger.info(logs.BINARY_INFO.format(path=binary_path))
            logger.info(logs.BINARY_SIZE.format(size=size_mb))

            os.chmod(binary_path, BINARY_FILE_PERMISSION)
            logger.success(logs.BUILD_READY)

        return True

    except subprocess.CalledProcessError as e:
        logger.error(
            logs.BUILD_FAILED.format(lang=binary_name, stdout=e.stdout, stderr=e.stderr)
        )
        logger.error(logs.BUILD_STDOUT.format(stdout=e.stdout))
        logger.error(logs.BUILD_STDERR.format(stderr=e.stderr))
        return False


if __name__ == "__main__":
    success = build_binary()
    sys.exit(0 if success else 1)
