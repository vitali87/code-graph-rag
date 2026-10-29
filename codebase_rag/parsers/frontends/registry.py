"""The per-language frontend registry (issue #1178). A free module dict, not a
`ProcessorFactory` responsibility -- the factory builds processors, and the graph
builder owns the frontend dispatch loop."""

from __future__ import annotations

from ...constants.languages import SupportedLanguage
from .protocol import LanguageFrontend

# Fact-provider frontends keyed by language. A language may register at most
# one. C/C++ (libclang) is not here: it writes graph elements itself rather
# than returning facts, and the graph builder calls it directly.
FRONTENDS: dict[SupportedLanguage, LanguageFrontend] = {}


def register_frontend(frontend: LanguageFrontend) -> LanguageFrontend:
    FRONTENDS[frontend.language] = frontend
    return frontend
