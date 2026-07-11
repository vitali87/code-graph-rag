# (H) Opting out of the semantic embedding pass (issue #690): the
# (H) --no-embeddings CLI flag, the CGR_SKIP_EMBEDDINGS setting, and the
# (H) CGR_EMBEDDING_DEVICE override for the embedder's device selection.

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from codebase_rag import constants as cs
from codebase_rag.cli import app
from codebase_rag.config import AppConfig, settings
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.services.graph_service import MemgraphIngestor
from codebase_rag.utils.dependencies import has_torch, has_transformers

runner = CliRunner()

_PATCH_DEPS = patch(
    "codebase_rag.graph_updater.has_semantic_dependencies", return_value=True
)


def _has_semantic_deps() -> bool:
    return has_torch() and has_transformers()


@pytest.fixture
def query_ingestor() -> MagicMock:
    mock = MagicMock(spec=MemgraphIngestor)
    mock.fetch_all = MagicMock(return_value=[])
    mock.execute_write = MagicMock()
    return mock


def _make_updater(
    temp_repo: Path, ingestor: MagicMock, skip_embeddings: bool | None = None
) -> GraphUpdater:
    parsers, queries = load_parsers()
    return GraphUpdater(
        ingestor=ingestor,
        repo_path=temp_repo,
        parsers=parsers,
        queries=queries,
        skip_embeddings=skip_embeddings,
    )


class TestGraphUpdaterSkipEmbeddings:
    @_PATCH_DEPS
    def test_skip_flag_skips_embedding_pass(
        self, _deps: MagicMock, temp_repo: Path, query_ingestor: MagicMock
    ) -> None:
        updater = _make_updater(temp_repo, query_ingestor, skip_embeddings=True)
        updater._generate_semantic_embeddings()
        query_ingestor.fetch_all.assert_not_called()

    @_PATCH_DEPS
    def test_default_runs_embedding_pass(
        self, _deps: MagicMock, temp_repo: Path, query_ingestor: MagicMock
    ) -> None:
        updater = _make_updater(temp_repo, query_ingestor)
        assert updater.skip_embeddings is False
        updater._generate_semantic_embeddings()
        query_ingestor.fetch_all.assert_called_once()

    @_PATCH_DEPS
    def test_env_setting_skips_by_default(
        self,
        _deps: MagicMock,
        temp_repo: Path,
        query_ingestor: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(settings, "SKIP_EMBEDDINGS", True)
        updater = _make_updater(temp_repo, query_ingestor)
        assert updater.skip_embeddings is True
        updater._generate_semantic_embeddings()
        query_ingestor.fetch_all.assert_not_called()

    @_PATCH_DEPS
    def test_explicit_false_overrides_env_setting(
        self,
        _deps: MagicMock,
        temp_repo: Path,
        query_ingestor: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(settings, "SKIP_EMBEDDINGS", True)
        updater = _make_updater(temp_repo, query_ingestor, skip_embeddings=False)
        updater._generate_semantic_embeddings()
        query_ingestor.fetch_all.assert_called_once()


class TestCliNoEmbeddingsFlag:
    def _invoke_start(self, tmp_path: Path, *extra: str) -> MagicMock:
        with (
            patch("codebase_rag.cli.GraphUpdater") as mock_updater_cls,
            patch("codebase_rag.cli.connect_memgraph") as mock_connect,
            patch("codebase_rag.cli.load_parsers", return_value=({}, {})),
            patch("codebase_rag.cli.cgr_state"),
            patch("codebase_rag.cli._update_and_validate_models"),
        ):
            mock_connect.return_value.__enter__ = MagicMock(return_value=MagicMock())
            mock_connect.return_value.__exit__ = MagicMock(return_value=False)
            result = runner.invoke(
                app,
                [
                    "start",
                    "--update-graph",
                    "--no-start-stack",
                    "--repo-path",
                    str(tmp_path),
                    *extra,
                ],
            )
        assert result.exit_code == 0, result.output
        return mock_updater_cls

    def test_no_embeddings_flag_reaches_updater(self, tmp_path: Path) -> None:
        mock_updater_cls = self._invoke_start(tmp_path, "--no-embeddings")
        assert mock_updater_cls.call_args.kwargs["skip_embeddings"] is True

    def test_without_flag_defers_to_settings(self, tmp_path: Path) -> None:
        mock_updater_cls = self._invoke_start(tmp_path)
        assert mock_updater_cls.call_args.kwargs["skip_embeddings"] is None


class TestSkipEmbeddingsConfig:
    def test_env_vars_parse(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CGR_SKIP_EMBEDDINGS", "1")
        monkeypatch.setenv("CGR_EMBEDDING_DEVICE", "cpu")
        config = AppConfig()
        assert config.SKIP_EMBEDDINGS is True
        assert config.EMBEDDING_DEVICE == cs.EmbeddingDevice.CPU

    def test_defaults(self) -> None:
        config = AppConfig()
        assert config.SKIP_EMBEDDINGS is False
        assert config.EMBEDDING_DEVICE is None


@pytest.mark.skipif(not _has_semantic_deps(), reason="torch/transformers not installed")
class TestEmbeddingDeviceOverride:
    def test_override_wins_when_available(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from codebase_rag.embedder import _select_device

        monkeypatch.setattr(settings, "EMBEDDING_DEVICE", cs.EmbeddingDevice.CPU)
        assert _select_device() == cs.EmbeddingDevice.CPU

    @pytest.mark.parametrize(
        "override", [cs.EmbeddingDevice.CUDA, cs.EmbeddingDevice.MPS]
    )
    def test_unavailable_override_falls_back_to_auto(
        self, monkeypatch: pytest.MonkeyPatch, override: cs.EmbeddingDevice
    ) -> None:
        import torch

        from codebase_rag.embedder import _select_device

        monkeypatch.setattr(settings, "EMBEDDING_DEVICE", override)
        with (
            patch.object(torch.cuda, "is_available", return_value=False),
            patch.object(torch.backends.mps, "is_available", return_value=False),
        ):
            assert _select_device() == cs.EmbeddingDevice.CPU
