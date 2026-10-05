"""`cgr edits undo` re-ingests what it restored, or says how to (#2515).

The forward edit (`cgr rename`) re-ingests the files it rewrites; the undo
reverted the files and left the graph on the edited state, with nothing
telling the user to resync. The graph is faked here so every path runs
without a database; the real-Memgraph round trip lives in
`integration/test_edits_undo_graph_e2e.py`.
"""

from __future__ import annotations

import socket
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner, Result

from codebase_rag import constants as cs
from codebase_rag.config import settings
from codebase_rag.editing import EditTransaction
from codebase_rag.editing.cli import cli as edits_cli
from codebase_rag.editing.transaction import load_history
from codebase_rag.utils.path_utils import derive_project_name

_A = "def a():\n    return 1\n"
_B = "def b():\n    return 2\n"


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "a.py").write_text(_A)
    (root / "pkg" / "b.py").write_text(_B)
    return root


class _Graph:
    """The two things the undo touches: the connection and the updater."""

    def __init__(self, projects: list[str]) -> None:
        self.ingestor = MagicMock()
        self.ingestor.__enter__.return_value = self.ingestor
        self.ingestor.__exit__.return_value = None
        self.ingestor.list_projects.return_value = projects
        self.connect = MagicMock(return_value=self.ingestor)
        self.updater_cls = MagicMock()
        self.updater = self.updater_cls.return_value

    @property
    def reingested(self) -> list[list[str]]:
        return [list(c.args[0]) for c in self.updater.reingest.call_args_list]

    @property
    def project(self) -> str:
        return str(self.updater_cls.call_args.kwargs["project_name"])


@pytest.fixture
def graph(repo: Path) -> Iterator[_Graph]:
    fake = _Graph([derive_project_name(repo)])
    with (
        patch("codebase_rag.cli_runtime.connect_memgraph", fake.connect),
        patch("codebase_rag.graph_updater.GraphUpdater", fake.updater_cls),
    ):
        yield fake


def _commit(repo: Path, files: dict[str, str]) -> None:
    tx = EditTransaction(repo)
    for rel, content in files.items():
        tx.stage(rel, content)
    assert tx.commit().applied


def _undo(repo: Path, *args: str) -> Result:
    return CliRunner().invoke(edits_cli, ["undo", "--repo-path", str(repo), *args])


def _resync_hint(repo: Path) -> str:
    return f"cgr start --repo-path {repo} --update-graph"


def test_undo_reingests_every_restored_file_into_the_repos_project(
    repo: Path, graph: _Graph
) -> None:
    _commit(repo, {"pkg/a.py": "def renamed():\n    return 1\n", "pkg/b.py": "x = 1\n"})

    result = _undo(repo)

    assert result.exit_code == 0, result.output
    assert (repo / "pkg" / "a.py").read_text() == _A
    assert graph.reingested == [["pkg/a.py", "pkg/b.py"]]
    assert graph.project == derive_project_name(repo)
    assert graph.updater_cls.call_args.kwargs["repo_path"] == repo.resolve()


def test_a_file_the_undo_deletes_is_handed_to_the_reingest(
    repo: Path, graph: _Graph
) -> None:
    # The edit created pkg/new.py and the undo removes it: the re-ingest
    # treats a missing path as deleted, which drops its nodes from the graph.
    _commit(repo, {"pkg/new.py": "def fresh():\n    return 3\n"})

    result = _undo(repo)

    assert result.exit_code == 0, result.output
    assert not (repo / "pkg" / "new.py").exists()
    assert graph.reingested == [["pkg/new.py"]]


def test_undo_reingests_into_the_project_it_is_told(repo: Path, graph: _Graph) -> None:
    graph.ingestor.list_projects.return_value = ["custom"]
    _commit(repo, {"pkg/a.py": "def renamed():\n    return 1\n"})

    result = _undo(repo, "--project", "custom")

    assert result.exit_code == 0, result.output
    assert graph.project == "custom"
    assert graph.reingested == [["pkg/a.py"]]


def test_several_undos_are_reingested_together(repo: Path, graph: _Graph) -> None:
    _commit(repo, {"pkg/a.py": "v1\n"})
    _commit(repo, {"pkg/b.py": "v2\n"})

    result = _undo(repo, "-n", "2")

    assert result.exit_code == 0, result.output
    assert graph.reingested == [["pkg/a.py", "pkg/b.py"]]


def test_an_undo_run_stopped_by_a_conflict_still_reingests_what_it_restored(
    repo: Path, graph: _Graph
) -> None:
    # The newer undo lands, the older one refuses on a hand edit: the file
    # the first one restored must not be left stale in the graph.
    _commit(repo, {"pkg/a.py": "v1\n"})
    _commit(repo, {"pkg/b.py": "v2\n"})
    (repo / "pkg" / "a.py").write_text("hand edit\n")

    result = _undo(repo, "-n", "2")

    assert result.exit_code == 1
    assert (repo / "pkg" / "b.py").read_text() == _B
    assert (repo / "pkg" / "a.py").read_text() == "hand edit\n"
    assert graph.reingested == [["pkg/b.py"]]
    assert "pkg/a.py" in result.stderr


def test_an_unreachable_graph_is_reported_with_the_command_that_fixes_it(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A real connection attempt to a port nothing listens on.
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    monkeypatch.setattr(settings, "MEMGRAPH_HOST", "127.0.0.1")
    monkeypatch.setattr(settings, "MEMGRAPH_PORT", port)
    _commit(repo, {"pkg/a.py": "def renamed():\n    return 1\n"})

    result = _undo(repo)

    # The files are back and the history entry is gone: the undo happened,
    # so a retry must not undo the next entry too.
    assert result.exit_code == 0, result.output
    assert (repo / "pkg" / "a.py").read_text() == _A
    assert load_history(repo) == []
    assert _resync_hint(repo.resolve()) in result.stderr
    assert "Traceback" not in result.output


def test_a_failing_reingest_is_reported_with_the_command_that_fixes_it(
    repo: Path, graph: _Graph
) -> None:
    graph.updater.reingest.side_effect = RuntimeError("graph went away")
    _commit(repo, {"pkg/a.py": "def renamed():\n    return 1\n"})

    result = _undo(repo)

    assert result.exit_code == 0, result.output
    assert (repo / "pkg" / "a.py").read_text() == _A
    assert "graph went away" in result.stderr
    assert _resync_hint(repo.resolve()) in result.stderr


def test_the_fix_command_names_a_project_that_is_not_the_default(
    repo: Path, graph: _Graph
) -> None:
    graph.ingestor.list_projects.side_effect = ConnectionError("down")
    _commit(repo, {"pkg/a.py": "def renamed():\n    return 1\n"})

    result = _undo(repo, "--project", "custom")

    assert result.exit_code == 0, result.output
    assert f"{_resync_hint(repo.resolve())} --project-name custom" in result.stderr


# --- negative: what the undo must NOT do to the graph ------------------------


def test_nothing_to_undo_does_not_touch_the_graph(repo: Path, graph: _Graph) -> None:
    result = _undo(repo)

    assert result.exit_code == 0
    assert cs.EDIT_UNDO_NONE in result.output
    graph.connect.assert_not_called()


def test_a_refused_undo_does_not_touch_the_graph(repo: Path, graph: _Graph) -> None:
    _commit(repo, {"pkg/a.py": "v1\n"})
    (repo / "pkg" / "a.py").write_text("hand edit\n")

    result = _undo(repo)

    assert result.exit_code == 1
    assert (repo / "pkg" / "a.py").read_text() == "hand edit\n"
    graph.connect.assert_not_called()


def test_a_project_that_is_not_indexed_is_not_created_by_the_undo(
    repo: Path, graph: _Graph
) -> None:
    # No graph for this checkout means nothing is stale; a scoped re-ingest
    # must not plant a partial project that was never indexed.
    graph.ingestor.list_projects.return_value = ["someone_else"]
    _commit(repo, {"pkg/a.py": "v1\n"})

    result = _undo(repo)

    assert result.exit_code == 0, result.output
    assert (repo / "pkg" / "a.py").read_text() == _A
    graph.updater.reingest.assert_not_called()
    assert _resync_hint(repo.resolve()) not in result.stderr


def test_show_does_not_touch_the_graph(repo: Path, graph: _Graph) -> None:
    _commit(repo, {"pkg/a.py": "v1\n"})

    result = CliRunner().invoke(edits_cli, ["show", "--repo-path", str(repo)])

    assert result.exit_code == 0, result.output
    graph.connect.assert_not_called()
