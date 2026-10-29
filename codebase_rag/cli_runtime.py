"""Process-wide CLI state and helpers that must not import the LLM stack.

`codebase_rag.cli` is imported by every `cgr` invocation, so anything it needs
at import time lives here rather than in `codebase_rag.agent_loop`, whose provider
and agent imports cost over a second (issue #2253). `main` re-exports these
names, so `main.app_context` and `cli.app_context` are the same object.
"""

from . import constants as cs
from .models import AppContext
from .services.graph_service import MemgraphIngestor


def style(
    text: str, color: cs.Color, modifier: cs.StyleModifier = cs.StyleModifier.BOLD
) -> str:
    if modifier == cs.StyleModifier.NONE:
        return f"[{color}]{text}[/{color}]"
    return f"[{modifier} {color}]{text}[/{modifier} {color}]"


def dim(text: str) -> str:
    return f"[{cs.StyleModifier.DIM}]{text}[/{cs.StyleModifier.DIM}]"


app_context = AppContext()


def connect_memgraph(batch_size: int) -> MemgraphIngestor:
    return MemgraphIngestor(batch_size=batch_size)
