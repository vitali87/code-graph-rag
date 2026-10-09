"""`run_check(isolated=True)` and `cgr check --isolated`, end to end (#1718).

The isolated check measures the working tree's delta and then puts the
graph and the on-disk hash cache back, so the same edit measures the same
way on a second run. These drive the wiring on the eval emulator: the round
trip itself, the refusals that keep it from starting when it could not put
everything back, the hash-cache snapshot, and the CLI flag reaching it.
"""

from __future__ import annotations

import copy
import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from codebase_rag import constants as cs
from codebase_rag import cypher_queries as cq
from codebase_rag.capture import resolve_capture
from codebase_rag.cli import app
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.structural_check import (
    CheckError,
    _FileSnapshot,
    _refuse_project_holding_io_links,
    _refuse_unrestorable_capture,
    run_check,
)
from codebase_rag.tests.conftest import git_env
from codebase_rag.types_defs import PropertyDict, ResultRow
from evals.cgr_graph import _StatefulIngestor

PROJECT = "wiring_fixture"

FIXTURE: dict[str, str] = {
    "pkg/__init__.py": "",
    "pkg/util.py": "def helper(a):\n    return a + 1\n",
    "pkg/app.py": "from pkg.util import helper\n\n\ndef run():\n    return helper(1)\n",
    "main.py": "from pkg.app import run\n\n\ndef main():\n    run()\n",
}


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=root,
        check=True,
        capture_output=True,
        env=git_env(),
    )


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def indexed(temp_repo: Path) -> tuple[Path, _StatefulIngestor]:
    root = temp_repo / PROJECT
    root.mkdir()
    for rel, text in FIXTURE.items():
        _write(root, rel, text)
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    store = _StatefulIngestor()
    parsers, queries = load_parsers()
    GraphUpdater(
        ingestor=store,
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name=PROJECT,
    ).run(force=True)
    return root, store


def _edit(root: Path) -> None:
    """A rename whose caller dangles, a deleted module, a new module."""
    _write(root, "pkg/util.py", FIXTURE["pkg/util.py"].replace("helper", "assist"))
    (root / "main.py").unlink()
    _write(root, "lib/tool.py", "def tool():\n    return 1\n")


def _state(store: _StatefulIngestor) -> tuple[dict, set, dict]:
    return (
        copy.deepcopy(store.nodes),
        set(store.edges),
        copy.deepcopy(store.edge_props),
    )


def _delta(root: Path, store: _StatefulIngestor, *, isolated: bool) -> dict:
    parsers, queries = load_parsers()
    delta = run_check(
        root,
        "HEAD",
        PROJECT,
        store,
        parsers,
        queries,
        isolated=isolated,
        capture=resolve_capture([]),
    )
    return {k: v for k, v in delta.items() if k not in ("reingest_ms", "delta_ms")}


# --- the round trip -------------------------------------------------------------


def test_an_isolated_check_reports_the_delta_and_leaves_graph_and_cache(
    indexed: tuple[Path, _StatefulIngestor],
) -> None:
    root, store = indexed
    _edit(root)
    before = _state(store)
    cache = root / cs.HASH_CACHE_FILENAME
    cache_before = cache.read_bytes() if cache.exists() else None

    first = _delta(root, store, isolated=True)

    assert first["dangling_callers"][0]["target"] == f"{PROJECT}.pkg.util.helper"
    assert f"{PROJECT}.main.main" in first["symbols"]["removed"]
    assert _state(store) == before
    assert (cache.read_bytes() if cache.exists() else None) == cache_before
    # Nothing was kept, so the same edit measures the same way again.
    assert _delta(root, store, isolated=True) == first


def test_the_same_edit_applied_without_isolation_lands(
    indexed: tuple[Path, _StatefulIngestor],
) -> None:
    """The control: without the flag the re-ingest stays in the graph."""
    root, store = indexed
    _edit(root)
    before = _state(store)

    _delta(root, store, isolated=False)

    assert _state(store) != before


# --- refusals ------------------------------------------------------------------


@pytest.mark.parametrize("rel", ["FLOWS_TO", "RESOLVES_TO"])
def test_a_capture_holding_an_unrestorable_link_is_refused(rel: str) -> None:
    selection = resolve_capture(["all"])
    assert selection.rel_enabled(cs.RelationshipType(rel))

    with pytest.raises(CheckError, match=rel):
        _refuse_unrestorable_capture(selection)


def test_a_capture_without_io_links_is_accepted() -> None:
    _refuse_unrestorable_capture(resolve_capture(["none"]))


class _IoStore:
    def __init__(self, rows: list[ResultRow]) -> None:
        self._rows = rows
        self.queries: list[str] = []
        self.params: list[PropertyDict | None] = []

    def fetch_all(
        self, query: str, params: PropertyDict | None = None
    ) -> list[ResultRow]:
        self.queries.append(query)
        self.params.append(params)
        return self._rows


def test_a_graph_already_holding_an_io_link_is_refused() -> None:
    store = _IoStore([{cs.KEY_REL: cs.RelationshipType.FLOWS_TO.value}])

    with pytest.raises(CheckError, match=cs.RelationshipType.FLOWS_TO.value):
        _refuse_project_holding_io_links(store, PROJECT)
    assert store.queries == [cq.CYPHER_CHECK_PROJECT_IO_LINKS]
    assert store.params == [{cs.KEY_PROJECT_PREFIX: f"{PROJECT}."}]


def test_a_graph_without_io_links_is_accepted() -> None:
    _refuse_project_holding_io_links(_IoStore([]), PROJECT)


def test_an_isolated_check_refuses_before_writing_over_io_links(
    indexed: tuple[Path, _StatefulIngestor],
) -> None:
    root, store = indexed
    _edit(root)
    before = _state(store)
    parsers, queries = load_parsers()
    capture = resolve_capture(["all"])

    with pytest.raises(CheckError):
        run_check(
            root,
            "HEAD",
            PROJECT,
            store,
            parsers,
            queries,
            isolated=True,
            capture=capture,
        )

    assert _state(store) == before


def test_an_unreadable_hash_cache_refuses_the_isolated_check(
    indexed: tuple[Path, _StatefulIngestor],
) -> None:
    """A cache that cannot be read cannot be put back after the re-ingest
    rewrites it, so the run refuses before writing anything."""
    root, store = indexed
    cache = root / cs.HASH_CACHE_FILENAME
    cache.unlink(missing_ok=True)
    cache.mkdir()
    _edit(root)
    before = _state(store)

    with pytest.raises(CheckError, match=cs.HASH_CACHE_FILENAME):
        _delta(root, store, isolated=True)

    assert _state(store) == before


# --- the hash-cache snapshot ---------------------------------------------------


def test_a_snapshot_puts_back_content_and_timestamps(tmp_path: Path) -> None:
    path = tmp_path / "cache.json"
    path.write_bytes(b"before")
    os.utime(path, ns=(1_000_000_000, 2_000_000_000))
    snapshot = _FileSnapshot(path)
    path.write_bytes(b"after")

    snapshot.put_back()

    assert not snapshot.unrestorable
    assert path.read_bytes() == b"before"
    assert path.stat().st_mtime_ns == 2_000_000_000


def test_a_snapshot_of_an_absent_file_removes_what_was_written(
    tmp_path: Path,
) -> None:
    path = tmp_path / "cache.json"
    snapshot = _FileSnapshot(path)
    path.write_bytes(b"written by the run")

    snapshot.put_back()

    assert not snapshot.unrestorable
    assert not path.exists()


def test_an_unreadable_snapshot_is_unrestorable_and_leaves_the_path_alone(
    tmp_path: Path,
) -> None:
    """Unreadable is not absent: the put-back must not delete what it could
    not read."""
    path = tmp_path / "cache.json"
    path.mkdir()
    snapshot = _FileSnapshot(path)

    snapshot.put_back()

    assert snapshot.unrestorable
    assert path.is_dir()


# --- the CLI flag ----------------------------------------------------------------


@pytest.mark.parametrize("flag", [[], ["--isolated"]])
def test_the_cli_flag_reaches_the_check(tmp_path: Path, flag: list[str]) -> None:
    store = MagicMock()
    store.list_projects.return_value = [PROJECT]
    context = MagicMock()
    context.__enter__.return_value = store
    context.__exit__.return_value = False
    with (
        patch("codebase_rag.cli.connect_memgraph", return_value=context),
        patch("codebase_rag.structural_check.indexed_scope", return_value=(None, None)),
        patch("codebase_rag.structural_check.run_check", return_value={}) as check,
    ):
        result = CliRunner().invoke(
            app,
            ["check", "--repo-path", str(tmp_path), "--project", PROJECT, *flag],
        )

    assert result.exit_code == 0, result.output
    assert check.call_args.kwargs["isolated"] is bool(flag)
