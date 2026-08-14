"""The per-language frontend registry (issue #1178). A free module dict, not a
`ProcessorFactory` responsibility -- the factory builds processors, and the graph
builder owns the frontend dispatch loop."""

from __future__ import annotations

from ...constants.languages import SupportedLanguage
from .protocol import EmittingFrontend, LanguageFrontend

# Fact-provider frontends keyed by language (C# / Roslyn). A language may register
# at most one.
FRONTENDS: dict[SupportedLanguage, LanguageFrontend] = {}

# Emitting frontends keyed by language (C / C++ / libclang). Separate map because
# they run in a declared phase and write graph elements directly, rather than
# returning a fact bundle.
EMITTING_FRONTENDS: dict[SupportedLanguage, EmittingFrontend] = {}


def register_frontend(frontend: LanguageFrontend) -> LanguageFrontend:
    FRONTENDS[frontend.language] = frontend
    return frontend


def register_emitting_frontend(frontend: EmittingFrontend) -> EmittingFrontend:
    EMITTING_FRONTENDS[frontend.language] = frontend
    return frontend
