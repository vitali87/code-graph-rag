"""Two-repo cross-project retrieval against a real graph (issue #425).

Two separately indexed repositories share one Memgraph instance; a retriever
rooted in one repository must read source from the other via the stored
absolute paths, and every project must record its indexed root.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.tools.code_retrieval import CodeRetriever

if TYPE_CHECKING:
    from codebase_rag.services.graph_service import MemgraphIngestor

pytestmark = [pytest.mark.integration]

_ORDER_SOURCE = '''def create_order(user_id):
    """Create an order after resolving the user."""
    return {"user": user_id}
'''

_USER_SOURCE = '''def get_user(user_id):
    """Resolve a user by id."""
    return {"id": user_id}
'''


@pytest.fixture
def two_repos(tmp_path: Path) -> tuple[Path, Path]:
    order_repo = tmp_path / "order-service"
    order_repo.mkdir()
    (order_repo / "orders.py").write_text(_ORDER_SOURCE, encoding="utf-8")

    user_repo = tmp_path / "user-service"
    user_repo.mkdir()
    (user_repo / "handlers.py").write_text(_USER_SOURCE, encoding="utf-8")
    return order_repo, user_repo


def _index(ingestor: MemgraphIngestor, repo: Path) -> None:
    parsers, queries = load_parsers()
    GraphUpdater(
        ingestor=ingestor, repo_path=repo, parsers=parsers, queries=queries
    ).run()
    ingestor.flush_all()


class TestCrossProjectRetrievalE2E:
    def test_snippet_from_other_project_resolves_via_absolute_path(
        self, memgraph_ingestor: MemgraphIngestor, two_repos: tuple[Path, Path]
    ) -> None:
        order_repo, user_repo = two_repos
        _index(memgraph_ingestor, order_repo)
        _index(memgraph_ingestor, user_repo)

        retriever = CodeRetriever(str(order_repo), memgraph_ingestor)
        result = asyncio.run(
            retriever.find_code_snippet("user-service.handlers.get_user")
        )

        assert result.found is True
        assert "Resolve a user by id" in result.source_code

    def test_projects_record_their_indexed_roots(
        self, memgraph_ingestor: MemgraphIngestor, two_repos: tuple[Path, Path]
    ) -> None:
        order_repo, user_repo = two_repos
        _index(memgraph_ingestor, order_repo)
        _index(memgraph_ingestor, user_repo)

        roots = memgraph_ingestor.list_project_roots()

        assert roots["order-service"] == str(order_repo.resolve())
        assert roots["user-service"] == str(user_repo.resolve())
