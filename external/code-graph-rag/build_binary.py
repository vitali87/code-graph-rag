#!/usr/bin/env python3

import os
import platform
import subprocess
import sys
from pathlib import Path

import toml
from loguru import logger

from codebase_rag import constants as cs
from codebase_rag import logs
from codebase_rag.constants import PyInstallerPackage


def _get_treesitter_packages() -> list[str]:
    pyproject = toml.load(cs.PYPROJECT_PATH)
    extras = pyproject.get(cs.TOML_KEY_PROJECT, {}).get(cs.TOML_KEY_OPTIONAL_DEPS, {})
    treesitter_deps = extras.get(cs.TREESITTER_EXTRA_KEY, [])

    packages: list[str] = []
    for dep in treesitter_deps:
        pkg_name = (
            dep.split(cs.VERSION_SPLIT_GTE)[0]
            .split(cs.VERSION_SPLIT_EQ)[0]
            .split(cs.VERSION_SPLIT_LT)[0]
            .strip()
        )
        if pkg_name.startswith(cs.TREESITTER_PKG_PREFIX):
            module_name = pkg_name.replace(cs.CHAR_HYPHEN, cs.CHAR_UNDERSCORE)
            packages.append(module_name)
    return packages


def _build_package_args(pkg: PyInstallerPackage) -> list[str]:
    args: list[str] = []
    if pkg.collect_all:
        args.extend([cs.PYINSTALLER_ARG_COLLECT_ALL, pkg.name])
    if pkg.collect_data:
        args.extend([cs.PYINSTALLER_ARG_COLLECT_DATA, pkg.name])
    if pkg.hidden_import:
        args.extend([cs.PYINSTALLER_ARG_HIDDEN_IMPORT, pkg.hidden_import])
    return args


def build_binary() -> bool:
    system = platform.system().lower()
    machine = platform.machine().lower()
    match machine:
        case cs.Architecture.X86_64:
            machine = cs.Architecture.AMD64
        case cs.Architecture.AARCH64 | cs.Architecture.ARM64:
            machine = cs.Architecture.ARM64

    binary_name = cs.BINARY_NAME_TEMPLATE.format(system=system, machine=machine)

    cmd = [
        cs.PYINSTALLER_CMD,
        cs.PYINSTALLER_ARG_NAME,
        binary_name,
        cs.PYINSTALLER_ARG_ONEFILE,
        cs.PYINSTALLER_ARG_NOCONFIRM,
        cs.PYINSTALLER_ARG_CLEAN,
    ]

    for ts_pkg in _get_treesitter_packages():
        cmd.extend([cs.PYINSTALLER_ARG_COLLECT_ALL, ts_pkg])

    for pkg in cs.PYINSTALLER_PACKAGES:
        cmd.extend(_build_package_args(pkg))

    for mod in cs.PYINSTALLER_EXCLUDED_MODULES:
        cmd.extend([cs.PYINSTALLER_ARG_EXCLUDE_MODULE, mod])

    cmd.append(cs.PYINSTALLER_ENTRY_POINT)

    logger.info(logs.BUILD_BINARY.format(name=binary_name))
    logger.info(logs.BUILD_PROGRESS)

    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
        logger.success(logs.BUILD_SUCCESS)

        binary_path = Path(cs.DIST_DIR) / binary_name
        if binary_path.exists():
            size_mb = binary_path.stat().st_size / cs.BYTES_PER_MB_FLOAT
            logger.info(logs.BINARY_INFO.format(path=binary_path))
            logger.info(logs.BINARY_SIZE.format(size=size_mb))

            os.chmod(binary_path, cs.BINARY_FILE_PERMISSION)
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
