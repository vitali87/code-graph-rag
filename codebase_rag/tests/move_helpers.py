from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from evals.cgr_graph import _StatefulIngestor

PROJECT = "move_fixture"
FIXTURE: dict[str, str] = {
    "pkg/__init__.py": "",
    "pkg/util.py": (
        "import os\n"
        "from pathlib import Path\n\n\n"
        "def other():\n    return Path('.')\n\n\n"
        "# joins with the platform separator\n"
        'def helper(a):\n    """Join."""\n    return os.sep.join(a)\n\n\n'
        "def unrelated():\n    return 1\n"
    ),
    "pkg/a.py": "from pkg.util import helper\n\n\ndef run():\n    return helper(['x', 'y'])\n",
    "pkg/b.py": "from pkg.util import helper, other\n\n\ndef go():\n    return helper([]) + str(other())\n",
    "pkg/c.py": "import pkg.util\n\n\ndef via_module():\n    return pkg.util.helper(['q'])\n",
    "tests/__init__.py": "",
    "tests/test_app.py": (
        "import os\n\nfrom pkg.a import run\n\n\ndef test_run():\n    assert run() == 'x' + os.sep + 'y'\n"
    ),
}


def write_file(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def index_repo(root: Path) -> tuple[_StatefulIngestor, GraphUpdater]:
    parsers, queries = load_parsers()
    store = _StatefulIngestor()
    updater = GraphUpdater(
        ingestor=store,
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name=PROJECT,
    )
    updater.run(force=True)
    return store, updater


def materialise(temp_repo: Path, fixture: dict[str, str]) -> Path:
    root = temp_repo / PROJECT
    root.mkdir()
    for rel, text in fixture.items():
        write_file(root, rel, text)
    return root


@pytest.fixture(name="move_repo")
def _move_repo(temp_repo: Path) -> tuple[Path, _StatefulIngestor, GraphUpdater]:
    root = materialise(temp_repo, FIXTURE)
    store, updater = index_repo(root)
    return root, store, updater


def qn(rel: str) -> str:
    return f"{PROJECT}.{rel}"


def smoke(root: Path) -> None:
    subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests"],
        cwd=root,
        check=True,
        capture_output=True,
    )
