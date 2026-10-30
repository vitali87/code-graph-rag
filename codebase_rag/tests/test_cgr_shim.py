import cgr


class TestCgrShimExports:
    def test_all_symbols_importable(self) -> None:
        for name in cgr.__all__:
            assert hasattr(cgr, name), f"{name!r} listed in __all__ but not importable"

    def test_all_matches_module_exports(self) -> None:
        # Identifier names only: pytest's assertion rewriting injects
        # "@py_builtins" and "@pytest_ar" into a module the first time it is
        # imported under rewriting, which depends on import order (#1410).
        # dir(), not vars(): the heavy exports load on first access, so
        # vars() holds them only once some earlier test has touched them.
        public_attrs = {
            k for k in dir(cgr) if not k.startswith("_") and k.isidentifier()
        }
        assert set(cgr.__all__) == public_attrs

    def test_settings_is_canonical_instance(self) -> None:
        from codebase_rag.config import settings

        assert cgr.settings is settings

    def test_embed_code_is_canonical_function(self) -> None:
        from codebase_rag.vectors.embedder import embed_code

        assert cgr.embed_code is embed_code

    def test_graph_loader_is_canonical_class(self) -> None:
        from codebase_rag.graph_loader import GraphLoader

        assert cgr.GraphLoader is GraphLoader

    def test_load_graph_is_canonical_function(self) -> None:
        from codebase_rag.graph_loader import load_graph

        assert cgr.load_graph is load_graph

    def test_memgraph_ingestor_is_canonical_class(self) -> None:
        from codebase_rag.services.graph_service import GraphIngestor

        assert cgr.GraphIngestor is GraphIngestor

    def test_cypher_generator_is_canonical_class(self) -> None:
        from codebase_rag.services.llm import CypherGenerator

        assert cgr.CypherGenerator is CypherGenerator
