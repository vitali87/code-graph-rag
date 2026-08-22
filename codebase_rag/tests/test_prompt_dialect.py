import pytest

from codebase_rag.exceptions import LLMGenerationError
from codebase_rag.prompts import build_graph_schema_and_rules
from codebase_rag.services.graph.memgraph import MemgraphDialect
from codebase_rag.services.llm import _validate_call_procedures


class _FakeDialect(MemgraphDialect):
    @property
    def procedure_catalog(self) -> str:
        return "- **PageRank**: `CALL algo.pageRank() YIELD node, score`"

    @property
    def allowed_proc_prefixes(self) -> frozenset[str]:
        return frozenset({"algo."})


def test_prompt_embeds_the_dialect_catalog() -> None:
    out = build_graph_schema_and_rules(_FakeDialect())
    assert "CALL algo.pageRank() YIELD node, score" in out
    assert "nxalg." not in out


def test_prompt_does_not_name_a_specific_engine() -> None:
    out = build_graph_schema_and_rules(MemgraphDialect())
    assert "Memgraph" not in out


def test_validate_call_procedures_allows_dialect_prefix() -> None:
    _validate_call_procedures(
        "MATCH (n) CALL algo.pageRank() YIELD node RETURN node", _FakeDialect()
    )


def test_validate_call_procedures_rejects_outside_dialect_prefix() -> None:
    with pytest.raises(LLMGenerationError, match="pagerank.get"):
        _validate_call_procedures(
            "MATCH (n) CALL pagerank.get() YIELD node RETURN node", _FakeDialect()
        )
