#!/usr/bin/env python3
"""
Build script for creating Graph-Code binaries using PyInstaller.
This handles the complex dependencies and package metadata issues.
"""

import os
import platform
import subprocess
import sys
from pathlib import Path


def build_binary() -> bool:
    """Build the Graph-Code binary using PyInstaller."""

    # Get platform info for naming
    system = platform.system().lower()
    machine = platform.machine().lower()
    if machine == "x86_64":
        machine = "amd64"
    elif machine in ["aarch64", "arm64"]:
        machine = "arm64"

    binary_name = f"graph-code-{system}-{machine}"

    # PyInstaller command with all necessary options
    cmd = [
        "pyinstaller",
        "--name",
        binary_name,
        "--onefile",
        "--noconfirm",
        "--clean",
        # Include all tree-sitter languages
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
        # Include pydantic-ai and dependencies
        "--collect-all",
        "pydantic_ai",
        "--collect-data",
        "pydantic_ai",
        "--hidden-import",
        "pydantic_ai_slim",
        # Include other critical dependencies
        "--collect-all",
        "rich",
        "--collect-all",
        "typer",
        "--collect-all",
        "loguru",
        "--collect-all",
        "toml",
        # Entry point
        "main.py",
    ]

    print(f"Building binary: {binary_name}")
    print("This may take a few minutes...")

    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
        print("Binary built successfully!")

        # Show binary info
        binary_path = Path("dist") / binary_name
        if binary_path.exists():
            size_mb = binary_path.stat().st_size / (1024 * 1024)
            print(f"Binary: {binary_path}")
            print(f"Size: {size_mb:.1f} MB")

            # Make sure it's executable
            os.chmod(binary_path, 0o755)
            print("Binary is ready for distribution!")

        return True

    except subprocess.CalledProcessError as e:
        print(f"Build failed: {e}")
        print("STDOUT:", e.stdout)
        print("STDERR:", e.stderr)
        return False


if __name__ == "__main__":
    success = build_binary()
    sys.exit(0 if success else 1)
