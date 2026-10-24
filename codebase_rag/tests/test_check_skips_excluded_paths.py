"""`cgr check` considers only the files a sync would index (issue #3264).

The check took its file list straight from git -- the diff plus every
untracked file `.gitignore` does not cover -- and applied none of cgr's own
exclusions. In a repo with no `.gitignore` yet, an in-repo `.venv` made it
crash (`.venv/bin/python` links outside the repository), and an untracked
`node_modules/` put thousands of vendored files into `paths` and into both
snapshot queries.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner, Result

from codebase_rag.cli import app
from codebase_rag.config import load_ignore_patterns
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from evals.cgr_graph import _StatefulIngestor

PROJECT = "pyv"
UTIL = 'def pad(s):\n    return s + " "\n'
APP = 'from util import pad\n\n\ndef main():\n    return pad("x")\n'


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


@pytest.fixture
def repo(temp_repo: Path) -> tuple[Path, _StatefulIngestor]:
    # Indexed and committed, then `pad` gains a parameter: the edit the
    # check is meant to report, whatever else sits untracked beside it.
    root = temp_repo / PROJECT
    root.mkdir()
    (root / "util.py").write_text(UTIL)
    (root / "app.py").write_text(APP)
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "b")
    store = _StatefulIngestor()
    parsers, queries = load_parsers()
    GraphUpdater(
        ingestor=store,
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name=PROJECT,
    ).run(force=True)
    (root / "util.py").write_text(UTIL.replace("def pad(s):", "def pad(s, width):"))
    return root, store


def _run_check(root: Path, store: _StatefulIngestor) -> Result:
    context = MagicMock()
    context.__enter__.return_value = store
    context.__exit__.return_value = False
    with (
        patch("codebase_rag.cli.connect_memgraph", return_value=context),
        patch.object(store, "list_projects", create=True, return_value=[PROJECT]),
    ):
        return CliRunner().invoke(
            app, ["check", "--repo-path", str(root), "--project", PROJECT]
        )


def _delta(result: Result) -> dict:
    assert result.exit_code == 0, result.output
    text = result.stdout
    return json.loads(text[text.index("{") :])


def _write(root: Path, rel: str, text: str = "x = 1\n") -> None:
    (root / rel).parent.mkdir(parents=True, exist_ok=True)
    (root / rel).write_text(text)


def test_an_untracked_virtualenv_linking_outside_the_repo_is_skipped(
    repo: tuple[Path, _StatefulIngestor],
) -> None:
    root, store = repo
    # What `python3 -m venv .venv` leaves: an interpreter symlink out of
    # the repository, and site-packages full of Python.
    _write(root, ".venv/lib/python3.12/site-packages/six.py")
    (root / ".venv/bin").mkdir(parents=True)
    os.symlink(sys.executable, root / ".venv/bin/python")

    delta = _delta(_run_check(root, store))

    assert delta["paths"] == ["util.py"]
    assert delta["symbols"]["changed"] == [f"{PROJECT}.util.pad"]


@pytest.mark.parametrize(
    "rel",
    [
        "node_modules/pkg0/lib/m0.js",
        "src/__pycache__/util.cpython-312.pyc",
        "build/gen.py",
    ],
    ids=["node-modules", "pycache", "build-output"],
)
def test_an_untracked_file_the_indexer_skips_is_not_checked(
    repo: tuple[Path, _StatefulIngestor], rel: str
) -> None:
    root, store = repo
    _write(root, rel)

    delta = _delta(_run_check(root, store))

    assert delta["paths"] == ["util.py"]
    assert delta["reparsed"] == ["util.py"]


def test_a_deleted_file_the_indexer_skips_is_not_checked(temp_repo: Path) -> None:
    # A vendored file committed by mistake, then removed: git reports the
    # deletion, but the graph never held it.
    root = temp_repo / PROJECT
    root.mkdir()
    _write(root, "util.py", UTIL)
    _write(root, "node_modules/left/index.js", "module.exports = 1;\n")
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "b")
    store = _StatefulIngestor()
    parsers, queries = load_parsers()
    GraphUpdater(
        ingestor=store,
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name=PROJECT,
    ).run(force=True)
    _write(root, "util.py", UTIL.replace("def pad(s):", "def pad(s, width):"))
    _git(root, "rm", "-q", "node_modules/left/index.js")

    delta = _delta(_run_check(root, store))

    assert delta["paths"] == ["util.py"]
    assert delta["removed_files"] == []


def test_a_cgrignore_exclusion_is_not_checked(temp_repo: Path) -> None:
    # The project is indexed under its `.cgrignore`, as `cgr start` does,
    # so the stamped scope the check reads excludes `generated/` too.
    root = temp_repo / PROJECT
    root.mkdir()
    _write(root, "util.py", UTIL)
    _write(root, ".cgrignore", "generated/\n")
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "b")
    scope = load_ignore_patterns(root)
    store = _StatefulIngestor()
    parsers, queries = load_parsers()
    GraphUpdater(
        ingestor=store,
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name=PROJECT,
        exclude_paths=scope.exclude,
        unignore_paths=scope.unignore,
    ).run(force=True)
    _write(root, "util.py", UTIL.replace("def pad(s):", "def pad(s, width):"))
    _write(root, "generated/models.py")

    delta = _delta(_run_check(root, store))

    assert delta["paths"] == ["util.py"]


# --- negatives: the files a sync indexes are still the check's ---------------


def test_an_untracked_source_file_is_still_checked(
    repo: tuple[Path, _StatefulIngestor],
) -> None:
    root, store = repo
    _write(root, "extra.py", "def spare():\n    return 0\n")

    delta = _delta(_run_check(root, store))

    assert delta["paths"] == ["extra.py", "util.py"]
    assert f"{PROJECT}.extra.spare" in delta["symbols"]["added"]


def test_a_deleted_tracked_file_is_still_checked(
    repo: tuple[Path, _StatefulIngestor],
) -> None:
    root, store = repo
    (root / "app.py").unlink()

    delta = _delta(_run_check(root, store))

    assert delta["paths"] == ["app.py", "util.py"]
    assert delta["removed_files"] == ["app.py"]
