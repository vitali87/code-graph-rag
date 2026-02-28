from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.services.graph_service import MemgraphIngestor
from codebase_rag.types_defs import ResultRow

MOCK_EMBEDDING = [0.1] * 768

_PATCH_DEPS = patch(
    "codebase_rag.graph_updater.has_semantic_dependencies", return_value=True
)
_PATCH_EMBED = patch("codebase_rag.embedder.embed_code", return_value=MOCK_EMBEDDING)
_PATCH_STORE_BATCH = patch(
    "codebase_rag.vector_store.store_embedding_batch", side_effect=lambda pts: len(pts)
)
_PATCH_RECONCILE = patch(
    "codebase_rag.vector_store.get_stored_point_ids", return_value=set()
)


@pytest.fixture
def query_ingestor() -> MagicMock:
    mock = MagicMock(spec=MemgraphIngestor)
    mock.fetch_all = MagicMock(return_value=[])
    mock.execute_write = MagicMock()
    return mock


@pytest.fixture
def updater_with_query(temp_repo: Path, query_ingestor: MagicMock) -> GraphUpdater:
    parsers, queries = load_parsers()
    return GraphUpdater(
        ingestor=query_ingestor,
        repo_path=temp_repo,
        parsers=parsers,
        queries=queries,
    )


class TestCypherQueryEmbeddingsStructure:
    def test_contains_starts_with_project_name(self) -> None:
        assert "STARTS WITH" in cs.CYPHER_QUERY_EMBEDDINGS
        assert "$project_name" in cs.CYPHER_QUERY_EMBEDDINGS

    def test_returns_required_columns(self) -> None:
        query = cs.CYPHER_QUERY_EMBEDDINGS.upper()
        for col in ["NODE_ID", "QUALIFIED_NAME", "START_LINE", "END_LINE", "PATH"]:
            assert col in query

    def test_dot_concatenation_is_parenthesized(self) -> None:
        assert "($project_name + '.')" in cs.CYPHER_QUERY_EMBEDDINGS

    def test_no_bare_starts_with_plus(self) -> None:
        for line in cs.CYPHER_QUERY_EMBEDDINGS.splitlines():
            stripped = line.strip()
            if "STARTS WITH" in stripped and "$project_name" in stripped:
                assert "($project_name" in stripped, (
                    f"$project_name + '.' must be parenthesized in: {stripped!r}"
                )


class TestGenerateSemanticEmbeddings:
    @_PATCH_DEPS
    @_PATCH_EMBED
    @_PATCH_STORE_BATCH
    @_PATCH_RECONCILE
    def test_passes_project_name_without_trailing_dot(
        self,
        _mock_reconcile: MagicMock,
        _mock_store_batch: MagicMock,
        _mock_embed: MagicMock,
        _mock_deps: MagicMock,
        updater_with_query: GraphUpdater,
        query_ingestor: MagicMock,
    ) -> None:
        query_ingestor.fetch_all.return_value = []
        updater_with_query._generate_semantic_embeddings()

        params = query_ingestor.fetch_all.call_args[0][1]
        project_name_param = params["project_name"]
        assert not project_name_param.endswith("."), (
            f"project_name should not have trailing dot, got: {project_name_param!r}"
        )

    @_PATCH_DEPS
    @_PATCH_EMBED
    @_PATCH_STORE_BATCH
    @_PATCH_RECONCILE
    def test_uses_cypher_query_embeddings_constant(
        self,
        _mock_reconcile: MagicMock,
        _mock_store_batch: MagicMock,
        _mock_embed: MagicMock,
        _mock_deps: MagicMock,
        updater_with_query: GraphUpdater,
        query_ingestor: MagicMock,
    ) -> None:
        query_ingestor.fetch_all.return_value = []
        updater_with_query._generate_semantic_embeddings()

        query_arg = query_ingestor.fetch_all.call_args[0][0]
        assert query_arg == cs.CYPHER_QUERY_EMBEDDINGS

    @patch("codebase_rag.graph_updater.has_semantic_dependencies", return_value=False)
    def test_skips_when_no_semantic_dependencies(
        self,
        _mock_deps: MagicMock,
        updater_with_query: GraphUpdater,
        query_ingestor: MagicMock,
    ) -> None:
        updater_with_query._generate_semantic_embeddings()
        query_ingestor.fetch_all.assert_not_called()

    @_PATCH_DEPS
    @_PATCH_EMBED
    @_PATCH_STORE_BATCH
    @_PATCH_RECONCILE
    def test_returns_early_on_empty_results(
        self,
        _mock_reconcile: MagicMock,
        mock_store_batch: MagicMock,
        _mock_embed: MagicMock,
        _mock_deps: MagicMock,
        updater_with_query: GraphUpdater,
        query_ingestor: MagicMock,
    ) -> None:
        query_ingestor.fetch_all.return_value = []
        updater_with_query._generate_semantic_embeddings()
        mock_store_batch.assert_not_called()

    @_PATCH_DEPS
    @_PATCH_EMBED
    @_PATCH_STORE_BATCH
    @_PATCH_RECONCILE
    def test_embeds_valid_function_with_source(
        self,
        _mock_reconcile: MagicMock,
        mock_store_batch: MagicMock,
        mock_embed: MagicMock,
        _mock_deps: MagicMock,
        updater_with_query: GraphUpdater,
        query_ingestor: MagicMock,
        temp_repo: Path,
    ) -> None:
        (temp_repo / "module.py").write_text("def hello():\n    return 42\n")
        row: ResultRow = {
            cs.KEY_NODE_ID: 1,
            cs.KEY_QUALIFIED_NAME: "myproject.module.hello",
            cs.KEY_START_LINE: 1,
            cs.KEY_END_LINE: 2,
            cs.KEY_PATH: "module.py",
        }
        query_ingestor.fetch_all.return_value = [row]

        updater_with_query._generate_semantic_embeddings()

        mock_embed.assert_called_once()
        mock_store_batch.assert_called_once()
        batch_arg = mock_store_batch.call_args[0][0]
        assert len(batch_arg) == 1
        assert batch_arg[0] == (1, MOCK_EMBEDDING, "myproject.module.hello")

    @_PATCH_DEPS
    @_PATCH_EMBED
    @_PATCH_STORE_BATCH
    @_PATCH_RECONCILE
    def test_skips_row_with_missing_source_info(
        self,
        _mock_reconcile: MagicMock,
        mock_store_batch: MagicMock,
        mock_embed: MagicMock,
        _mock_deps: MagicMock,
        updater_with_query: GraphUpdater,
        query_ingestor: MagicMock,
    ) -> None:
        row: ResultRow = {
            cs.KEY_NODE_ID: 1,
            cs.KEY_QUALIFIED_NAME: "myproject.module.hello",
        }
        query_ingestor.fetch_all.return_value = [row]

        updater_with_query._generate_semantic_embeddings()

        mock_embed.assert_not_called()
        mock_store_batch.assert_not_called()

    @patch("codebase_rag.graph_updater.has_semantic_dependencies", return_value=True)
    @patch("codebase_rag.embedder.embed_code", side_effect=RuntimeError("model error"))
    @_PATCH_STORE_BATCH
    @_PATCH_RECONCILE
    def test_handles_embed_failure_gracefully(
        self,
        _mock_reconcile: MagicMock,
        mock_store_batch: MagicMock,
        _mock_embed: MagicMock,
        _mock_deps: MagicMock,
        updater_with_query: GraphUpdater,
        query_ingestor: MagicMock,
        temp_repo: Path,
    ) -> None:
        (temp_repo / "module.py").write_text("def hello():\n    return 42\n")
        row: ResultRow = {
            cs.KEY_NODE_ID: 1,
            cs.KEY_QUALIFIED_NAME: "myproject.module.hello",
            cs.KEY_START_LINE: 1,
            cs.KEY_END_LINE: 2,
            cs.KEY_PATH: "module.py",
        }
        query_ingestor.fetch_all.return_value = [row]

        updater_with_query._generate_semantic_embeddings()

        mock_store_batch.assert_not_called()

    @_PATCH_DEPS
    @_PATCH_EMBED
    @_PATCH_STORE_BATCH
    @_PATCH_RECONCILE
    def test_skips_unparseable_rows(
        self,
        _mock_reconcile: MagicMock,
        mock_store_batch: MagicMock,
        mock_embed: MagicMock,
        _mock_deps: MagicMock,
        updater_with_query: GraphUpdater,
        query_ingestor: MagicMock,
    ) -> None:
        bad_row: ResultRow = {
            cs.KEY_NODE_ID: "not_an_int",
            cs.KEY_QUALIFIED_NAME: "pkg.func",
        }
        query_ingestor.fetch_all.return_value = [bad_row]

        updater_with_query._generate_semantic_embeddings()

        mock_embed.assert_not_called()
        mock_store_batch.assert_not_called()

    @_PATCH_DEPS
    @_PATCH_EMBED
    @_PATCH_STORE_BATCH
    @_PATCH_RECONCILE
    def test_counts_embedded_functions(
        self,
        _mock_reconcile: MagicMock,
        mock_store_batch: MagicMock,
        mock_embed: MagicMock,
        _mock_deps: MagicMock,
        updater_with_query: GraphUpdater,
        query_ingestor: MagicMock,
        temp_repo: Path,
    ) -> None:
        (temp_repo / "a.py").write_text("def f1():\n    pass\n")
        (temp_repo / "b.py").write_text("def f2():\n    pass\n")
        rows: list[ResultRow] = [
            {
                cs.KEY_NODE_ID: 1,
                cs.KEY_QUALIFIED_NAME: "proj.a.f1",
                cs.KEY_START_LINE: 1,
                cs.KEY_END_LINE: 2,
                cs.KEY_PATH: "a.py",
            },
            {
                cs.KEY_NODE_ID: 2,
                cs.KEY_QUALIFIED_NAME: "proj.b.f2",
                cs.KEY_START_LINE: 1,
                cs.KEY_END_LINE: 2,
                cs.KEY_PATH: "b.py",
            },
        ]
        query_ingestor.fetch_all.return_value = rows

        updater_with_query._generate_semantic_embeddings()

        assert mock_embed.call_count == 2
        mock_store_batch.assert_called_once()
        batch_arg = mock_store_batch.call_args[0][0]
        assert len(batch_arg) == 2
