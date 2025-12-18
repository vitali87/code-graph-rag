#!/usr/bin/env python3

import os
import platform
import subprocess
import sys
from enum import StrEnum
from pathlib import Path

from loguru import logger


class Architecture(StrEnum):
    X86_64 = "x86_64"
    AARCH64 = "aarch64"
    ARM64 = "arm64"
    AMD64 = "amd64"


def build_binary() -> bool:
    system = platform.system().lower()
    machine = platform.machine().lower()
    match machine:
        case Architecture.X86_64:
            machine = Architecture.AMD64
        case Architecture.AARCH64 | Architecture.ARM64:
            machine = Architecture.ARM64

    binary_name = f"graph-code-{system}-{machine}"

    cmd = [
        "pyinstaller",
        "--name",
        binary_name,
        "--onefile",
        "--noconfirm",
        "--clean",
        "--collect-all",
        "tree_sitter_python",
        "--collect-all",
        "tree_sitter_javascript",
        "--collect-all",
        "tree_sitter_typescript",
        "--collect-all",
        "tree_sitter_rust",
        "--collect-all",
        "tree_sitter_go",
        "--collect-all",
        "tree_sitter_scala",
        "--collect-all",
        "tree_sitter_java",
        "--collect-all",
        "tree_sitter_cpp",
        "--collect-all",
        "pydantic_ai",
        "--collect-data",
        "pydantic_ai",
        "--hidden-import",
        "pydantic_ai_slim",
        "--collect-all",
        "rich",
        "--collect-all",
        "typer",
        "--collect-all",
        "loguru",
        "--collect-all",
        "toml",
        "--collect-all",
        "protobuf",
        "main.py",
    ]

    logger.info(f"Building binary: {binary_name}")
    logger.info("This may take a few minutes...")

    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
        logger.success("Binary built successfully!")

        binary_path = Path("dist") / binary_name
        if binary_path.exists():
            size_mb = binary_path.stat().st_size / (1024 * 1024)
            logger.info(f"Binary: {binary_path}")
            logger.info(f"Size: {size_mb:.1f} MB")

            os.chmod(binary_path, 0o755)
            logger.success("Binary is ready for distribution!")

        return True

    except subprocess.CalledProcessError as e:
        logger.error(f"Build failed: {e}")
        logger.error(f"STDOUT: {e.stdout}")
        logger.error(f"STDERR: {e.stderr}")
        return False


if __name__ == "__main__":
    success = build_binary()
    sys.exit(0 if success else 1)
