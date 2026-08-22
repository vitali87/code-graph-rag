from codebase_rag.services.graph import GraphIngestor
from codebase_rag.services.graph.memgraph import MemgraphIngestor


def test_memgraph_ingestor_satisfies_graph_ingestor_protocol() -> None:
    assert isinstance(MemgraphIngestor(host="h", port=1), GraphIngestor)


def test_shim_still_exports_memgraph_ingestor() -> None:
    from codebase_rag.services.graph_service import MemgraphIngestor as Shimmed

    assert Shimmed is MemgraphIngestor


def test_cgr_public_api_still_exports_memgraph_ingestor() -> None:
    import cgr

    assert "MemgraphIngestor" in cgr.__all__
    assert cgr.MemgraphIngestor is MemgraphIngestor


def test_eval_capturing_ingestor_satisfies_the_sink_protocol() -> None:
    from codebase_rag.services import IngestorProtocol
    from evals.cgr_graph import _CapturingIngestor

    assert isinstance(_CapturingIngestor(), IngestorProtocol)
