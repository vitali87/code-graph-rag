#!/usr/bin/env python3
import hashlib
import subprocess
import sys
from pathlib import Path

from scripts.generate_readme import TARGET_FILES

repo_root = Path(__file__).parent.parent.parent
# The generator imports the whole CLI and reads PyPI (cached, 5 s a request),
# so a minute is slow; past this it is stuck, and a commit must not hang on it.
GENERATE_TIMEOUT_S = 600
GIT_ADD_TIMEOUT_S = 60


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    targets = [repo_root / relative for relative in TARGET_FILES]
    before = [digest(target) for target in targets]

    try:
        result = subprocess.run(
            [
                "uv",
                "run",
                "--frozen",
                "--no-sync",
                "python",
                "scripts/generate_readme.py",
            ],
            check=False,
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=GENERATE_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        sys.stderr.write(
            f"scripts/generate_readme.py did not finish within {GENERATE_TIMEOUT_S}s\n"
        )
        return 1

    if result.returncode != 0:
        sys.stderr.write(result.stderr)
        return result.returncode

    changed = [
        str(target.relative_to(repo_root))
        for target, old in zip(targets, before, strict=True)
        if digest(target) != old
    ]

    if changed:
        subprocess.run(
            ["git", "add", *changed],
            cwd=repo_root,
            check=True,
            timeout=GIT_ADD_TIMEOUT_S,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
