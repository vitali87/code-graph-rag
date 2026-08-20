"""Language semantic frontends (issue #1178, roadmap #1191).

Tree-sitter is the universal backbone; each language's own compiler is layered on
top as an OPTIONAL fact provider. Two shapes are supported:

- A `LanguageFrontend` returns a `SemanticFacts` bundle (the Roslyn/C# shape): the
  compiler's answers keyed by source position, consumed by the existing processors
  with silent per-miss fallback to the tree-sitter heuristics.
- An `EmittingFrontend` also writes graph elements the backbone cannot produce
  (the libclang/C++ shape: macro-expanded nodes, `#include` edges), in a declared
  run phase.

The invariant every frontend preserves: the compiler is an oracle, tree-sitter is
the backbone, and a missing toolchain degrades to the pure tree-sitter graph --
never worse.
"""

from __future__ import annotations

# Import for side effect: each frontend module self-registers into the registry.
from . import csharp as _csharp  # noqa: E402,F401  (registers CSharpFrontend)
from . import go as _go  # noqa: E402,F401  (registers GoFrontend)
from . import java as _java  # noqa: E402,F401  (registers JavaJavacFrontend)
from . import python as _python  # noqa: E402,F401  (registers PythonJediFrontend)
from .protocol import (
    CallSiteKey,
    EmittingFrontend,
    FrontendEmitContext,
    FrontendEmitResult,
    FrontendPhase,
    LanguageFrontend,
    ResolvedCallSite,
    SemanticFacts,
    empty_facts,
)
from .registry import (
    EMITTING_FRONTENDS,
    FRONTENDS,
    register_emitting_frontend,
    register_frontend,
)

__all__ = [
    "EMITTING_FRONTENDS",
    "FRONTENDS",
    "CallSiteKey",
    "EmittingFrontend",
    "FrontendEmitContext",
    "FrontendEmitResult",
    "FrontendPhase",
    "LanguageFrontend",
    "ResolvedCallSite",
    "SemanticFacts",
    "empty_facts",
    "register_emitting_frontend",
    "register_frontend",
]
