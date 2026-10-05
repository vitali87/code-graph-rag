"""`cgr start --update-graph -o`: the file is the synced project's (#2440).

The option wrote the whole shared graph, every project on the machine, to a
file the command presented as the repository's graph, and the sync summary
counted the export's time as the sync's: a no-op sync of a 6-file repo read
"already in sync (25.41s)".
"""

from __future__ import annotations

from collections.abc import Callable, Generator
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import Result
from rich.console import Console
from typer.testing import CliRunner

from codebase_rag import cli
from codebase_rag import constants as cs
from codebase_rag.cli import app
from codebase_rag.types_defs import GraphData, GraphMetadata
from codebase_rag.utils.path_utils import derive_project_name

GRAPH = GraphData(
    nodes=[{"node_id": 1, "labels": ["Project"], "properties": {"name": "alpha"}}],
    relationships=[],
    metadata=GraphMetadata(
        total_nodes=1, total_relationships=0, exported_at="2026-09-30T00:00:00+00:00"
    ),
)
SYNC_SECONDS = 2.0
EXPORT_SECONDS = 50.0


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture(autouse=True)
def _wide_console(monkeypatch: pytest.MonkeyPatch) -> None:
    # Keeps each message on one line whatever the tmp path's length.
    monkeypatch.setattr(cli.app_context, "console", Console(width=1000))


@pytest.fixture
def clock() -> Generator[FakeClock, None, None]:
    fake = FakeClock()
    with patch("codebase_rag.cli.time", fake):
        yield fake


@pytest.fixture
def ingestor(clock: FakeClock) -> MagicMock:
    store = MagicMock()

    def export(project_names: tuple[str, ...] | list[str] = ()) -> GraphData:
        clock.advance(EXPORT_SECONDS)
        return GRAPH

    store.export_graph_to_dict.side_effect = export
    return store


def _updater_factory(clock: FakeClock, in_sync: bool) -> Callable[..., MagicMock]:
    def build(**kwargs: str) -> MagicMock:
        updater = MagicMock()
        # The name the updater writes on the Project node.
        updater.project_name = kwargs["project_name"].strip()
        updater.skipped_because_in_sync = in_sync
        updater.run.side_effect = lambda: clock.advance(SYNC_SECONDS)
        return updater

    return build


def _start(
    ingestor: MagicMock, clock: FakeClock, args: list[str], *, in_sync: bool = False
) -> Result:
    connection = MagicMock()
    connection.__enter__.return_value = ingestor
    connection.__exit__.return_value = False
    with (
        patch("codebase_rag.cli.connect_memgraph", return_value=connection),
        patch(
            "codebase_rag.graph_updater.GraphUpdater",
            side_effect=_updater_factory(clock, in_sync),
        ),
        patch("codebase_rag.cli.load_parsers", return_value=({}, {})),
        patch("codebase_rag.cli._update_and_validate_models"),
    ):
        return CliRunner().invoke(app, ["start", "--no-start-stack", *args])


def _scope(ingestor: MagicMock) -> list[str]:
    call = ingestor.export_graph_to_dict.call_args
    if call.args:
        return list(call.args[0])
    return list(call.kwargs.get("project_names", []))


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    return repo


# --- the export holds the synced project -------------------------------------


def test_the_export_holds_only_the_synced_project(
    ingestor: MagicMock, clock: FakeClock, tmp_path: Path
) -> None:
    out = tmp_path / "graph.json"

    result = _start(
        ingestor,
        clock,
        [
            "--repo-path",
            str(_repo(tmp_path)),
            "--project-name",
            "alpha",
            "--update-graph",
            "-o",
            str(out),
        ],
    )

    assert result.exit_code == 0, result.output
    assert _scope(ingestor) == ["alpha"]
    assert out.exists()
    (announce,) = [
        line for line in result.output.splitlines() if line.startswith("Exporting")
    ]
    assert "'alpha'" in announce


def test_a_derived_project_name_is_the_export_scope(
    ingestor: MagicMock, clock: FakeClock, tmp_path: Path
) -> None:
    repo = _repo(tmp_path)

    result = _start(
        ingestor,
        clock,
        ["--repo-path", str(repo), "--update-graph", "-o", str(tmp_path / "g.json")],
    )

    assert result.exit_code == 0, result.output
    assert _scope(ingestor) == [derive_project_name(repo)]


def test_the_scope_is_the_name_on_the_project_node(
    ingestor: MagicMock, clock: FakeClock, tmp_path: Path
) -> None:
    # The updater strips the name it writes; the export must ask for that one.
    result = _start(
        ingestor,
        clock,
        [
            "--repo-path",
            str(_repo(tmp_path)),
            "--project-name",
            " alpha ",
            "--update-graph",
            "-o",
            str(tmp_path / "g.json"),
        ],
    )

    assert result.exit_code == 0, result.output
    assert _scope(ingestor) == ["alpha"]


# --- the sync summary is the sync's time -------------------------------------


def test_the_sync_summary_leaves_out_the_export(
    ingestor: MagicMock, clock: FakeClock, tmp_path: Path
) -> None:
    result = _start(
        ingestor,
        clock,
        [
            "--repo-path",
            str(_repo(tmp_path)),
            "--project-name",
            "alpha",
            "--update-graph",
            "-o",
            str(tmp_path / "g.json"),
        ],
    )

    assert result.exit_code == 0, result.output
    assert (
        cs.CLI_MSG_SYNC_DONE.format(project="alpha", elapsed=SYNC_SECONDS)
        in result.output
    )


def test_an_in_sync_summary_leaves_out_the_export(
    ingestor: MagicMock, clock: FakeClock, tmp_path: Path
) -> None:
    result = _start(
        ingestor,
        clock,
        [
            "--repo-path",
            str(_repo(tmp_path)),
            "--project-name",
            "alpha",
            "--update-graph",
            "-o",
            str(tmp_path / "g.json"),
        ],
        in_sync=True,
    )

    assert result.exit_code == 0, result.output
    assert (
        cs.CLI_MSG_SYNC_SKIPPED.format(project="alpha", elapsed=SYNC_SECONDS)
        in result.output
    )


# --- what stays as it was ----------------------------------------------------


def test_without_output_nothing_is_exported_and_the_sync_is_timed(
    ingestor: MagicMock, clock: FakeClock, tmp_path: Path
) -> None:
    result = _start(
        ingestor,
        clock,
        [
            "--repo-path",
            str(_repo(tmp_path)),
            "--project-name",
            "alpha",
            "--update-graph",
        ],
    )

    assert result.exit_code == 0, result.output
    ingestor.export_graph_to_dict.assert_not_called()
    assert (
        cs.CLI_MSG_SYNC_DONE.format(project="alpha", elapsed=SYNC_SECONDS)
        in result.output
    )


def test_output_still_requires_update_graph(
    ingestor: MagicMock, clock: FakeClock, tmp_path: Path
) -> None:
    result = _start(
        ingestor,
        clock,
        ["--repo-path", str(_repo(tmp_path)), "-o", str(tmp_path / "g.json")],
    )

    assert result.exit_code == 1
    assert cs.CLI_ERR_OUTPUT_REQUIRES_UPDATE in result.output
    ingestor.export_graph_to_dict.assert_not_called()


def test_a_failed_export_still_fails_the_command(
    ingestor: MagicMock, clock: FakeClock, tmp_path: Path
) -> None:
    ingestor.export_graph_to_dict.side_effect = RuntimeError("boom")

    result = _start(
        ingestor,
        clock,
        [
            "--repo-path",
            str(_repo(tmp_path)),
            "--project-name",
            "alpha",
            "--update-graph",
            "-o",
            str(tmp_path / "g.json"),
        ],
    )

    assert result.exit_code == 1
    assert "Failed to export graph: boom" in result.output
