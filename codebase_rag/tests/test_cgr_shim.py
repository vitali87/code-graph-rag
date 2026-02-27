import cgr


class TestCgrShimExports:
    def test_all_symbols_importable(self) -> None:
        for name in cgr.__all__:
            assert hasattr(cgr, name), f"{name!r} listed in __all__ but not importable"

    def test_all_matches_module_exports(self) -> None:
        public_attrs = {k for k in vars(cgr) if not k.startswith("_")}
        assert set(cgr.__all__) == public_attrs

    def test_settings_is_canonical_instance(self) -> None:
        from codebase_rag.config import settings

        assert cgr.settings is settings

    def test_embed_code_is_canonical_function(self) -> None:
        from codebase_rag.embedder import embed_code

        assert cgr.embed_code is embed_code

    def test_graph_loader_is_canonical_class(self) -> None:
        from codebase_rag.graph_loader import GraphLoader

        assert cgr.GraphLoader is GraphLoader

    def test_load_graph_is_canonical_function(self) -> None:
        from codebase_rag.graph_loader import load_graph

        assert cgr.load_graph is load_graph

    def test_memgraph_ingestor_is_canonical_class(self) -> None:
        from codebase_rag.services.graph_service import MemgraphIngestor

        assert cgr.MemgraphIngestor is MemgraphIngestor

    def test_cypher_generator_is_canonical_class(self) -> None:
        from codebase_rag.services.llm import CypherGenerator

        assert cgr.CypherGenerator is CypherGenerator
