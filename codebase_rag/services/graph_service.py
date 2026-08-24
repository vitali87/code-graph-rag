"""Backwards-compatible re-export.

`MemgraphIngestor` is public API via `cgr.__all__`, so this import path
must keep resolving even though the implementation moved to
`codebase_rag.services.graph.memgraph`.
"""

from .graph.memgraph import MemgraphIngestor

__all__ = ["MemgraphIngestor"]
