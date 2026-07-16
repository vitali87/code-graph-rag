#!/usr/bin/env python3
import hashlib
import subprocess
import sys
from pathlib import Path

repo_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(repo_root))

from scripts.generate_readme import TARGET_FILES  # noqa: E402


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


targets = [repo_root / relative for relative in TARGET_FILES]
before = [digest(target) for target in targets]

result = subprocess.run(
    ["uv", "run", "python", "scripts/generate_readme.py"],
    check=False,
    cwd=repo_root,
    capture_output=True,
    text=True,
)

if result.returncode != 0:
    sys.stderr.write(result.stderr)
    sys.exit(result.returncode)

changed = [
    str(target.relative_to(repo_root))
    for target, old in zip(targets, before, strict=True)
    if digest(target) != old
]

if changed:
    subprocess.run(["git", "add", *changed], cwd=repo_root, check=True)
    sys.exit(1)
sys.exit(0)
