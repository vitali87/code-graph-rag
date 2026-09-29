"""A deferred import must fail before `start --clean` wipes anything (#2253).

`cli` imports `graph_updater` and `vector_store` lazily to keep start-up fast.
If either cannot be imported (a broken or partial install), the command must
stop before `clean_database`, not after it with the graph already gone.
"""

from __future__ import annotations

import sys
from collections.abc import Generator
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from codebase_rag.cli import app
from codebase_rag.config import CgrignorePatterns

runner = CliRunner()


@pytest.fixture
def ingestor() -> Generator[MagicMock, None, None]:
    with patch("codebase_rag.cli.connect_memgraph") as connect:
        mock_ingestor = MagicMock()
        connect.return_value.__enter__ = MagicMock(return_value=mock_ingestor)
        connect.return_value.__exit__ = MagicMock(return_value=False)
        yield mock_ingestor


def _unimportable(module: str) -> object:
    # A None entry makes `import module` raise ImportError.
    return patch.dict(sys.modules, {module: None})


@pytest.fixture
def no_ignores() -> Generator[None, None, None]:
    with patch(
        "codebase_rag.cli.load_ignore_patterns",
        return_value=CgrignorePatterns(exclude=frozenset(), unignore=frozenset()),
    ):
        yield


class TestCleanOnly:
    def test_the_graph_is_wiped_when_every_import_resolves(
        self, ingestor: MagicMock, tmp_path: Path
    ) -> None:
        # Control: the same invocation does reach the wipe.
        with patch("codebase_rag.cli.clear_all_embeddings"):
            result = runner.invoke(
                app, ["start", "--clean", "--repo-path", str(tmp_path)]
            )

        assert result.exit_code == 0, result.output
        ingestor.clean_database.assert_called_once()

    def test_an_unimportable_vector_store_stops_before_the_wipe(
        self, ingestor: MagicMock, tmp_path: Path
    ) -> None:
        with _unimportable("codebase_rag.vector_store"):
            result = runner.invoke(
                app, ["start", "--clean", "--repo-path", str(tmp_path)]
            )

        assert result.exit_code != 0
        ingestor.clean_database.assert_not_called()


@pytest.mark.usefixtures("no_ignores")
class TestCleanWithUpdateGraph:
    def test_the_graph_is_wiped_when_every_import_resolves(
        self, ingestor: MagicMock, tmp_path: Path
    ) -> None:
        with (
            patch("codebase_rag.graph_updater.GraphUpdater"),
            patch("codebase_rag.cli.load_parsers", return_value=({}, {})),
            patch("codebase_rag.cli.clear_all_embeddings"),
        ):
            result = runner.invoke(
                app,
                ["start", "--clean", "--update-graph", "--repo-path", str(tmp_path)],
            )

        assert result.exit_code == 0, result.output
        ingestor.clean_database.assert_called_once()

    @pytest.mark.parametrize(
        "module", ["codebase_rag.graph_updater", "codebase_rag.vector_store"]
    )
    def test_an_unimportable_module_stops_before_the_wipe(
        self, ingestor: MagicMock, tmp_path: Path, module: str
    ) -> None:
        with (
            _unimportable(module),
            patch("codebase_rag.cli.load_parsers", return_value=({}, {})),
        ):
            result = runner.invoke(
                app,
                ["start", "--clean", "--update-graph", "--repo-path", str(tmp_path)],
            )

        assert result.exit_code != 0
        ingestor.clean_database.assert_not_called()
