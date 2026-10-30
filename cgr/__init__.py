"""The SDK's public names.

`load_graph` and `GraphLoader` read an exported graph and need nothing more;
`embed_code` and `CypherGenerator` bring the embedding and LLM stacks, which
take seconds to import, so they load on first access (PEP 562) rather than
with the package.
"""

# Imported under private names: every public name here is part of the SDK
# and listed in __all__.
import importlib as _importlib
import typing as _typing

from codebase_rag.config import settings
from codebase_rag.graph_loader import GraphLoader, load_graph
from codebase_rag.services.graph_service import GraphIngestor

if _typing.TYPE_CHECKING:
    from codebase_rag.services.llm import CypherGenerator
    from codebase_rag.vectors.embedder import embed_code

# The name this class shipped under before it served every graph engine;
# kept so existing SDK code keeps importing it.
MemgraphIngestor = GraphIngestor

_LAZY_EXPORTS = {
    "CypherGenerator": "codebase_rag.services.llm",
    "embed_code": "codebase_rag.vectors.embedder",
}

__all__ = [
    "CypherGenerator",
    "GraphIngestor",
    "GraphLoader",
    "MemgraphIngestor",
    "embed_code",
    "load_graph",
    "settings",
]


def __getattr__(name: str) -> object:
    if (module := _LAZY_EXPORTS.get(name)) is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(_importlib.import_module(module), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted({*globals(), *_LAZY_EXPORTS})
