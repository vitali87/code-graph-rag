#!/usr/bin/env python3
import hashlib
import subprocess
import sys
from pathlib import Path

repo_root = Path(__file__).parent.parent.parent
readme_path = repo_root / "README.md"

before = hashlib.sha256(readme_path.read_bytes()).hexdigest()

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

after = hashlib.sha256(readme_path.read_bytes()).hexdigest()

if before != after:
    subprocess.run(["git", "add", "README.md"], cwd=repo_root, check=True)
    sys.exit(1)
sys.exit(0)
