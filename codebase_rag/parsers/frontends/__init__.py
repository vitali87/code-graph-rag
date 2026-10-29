"""Language semantic frontends (issue #1178, roadmap #1191).

Tree-sitter is the universal backbone; each language's own compiler is layered on
top as an OPTIONAL fact provider: a `LanguageFrontend` returns a `SemanticFacts`
bundle, the compiler's answers keyed by source position, consumed by the existing
processors with silent per-miss fallback to the tree-sitter heuristics. libclang
for C/C++, which writes graph elements the backbone cannot produce (macro-expanded
nodes, `#include` edges), is called by the graph builder directly.

The invariant every frontend preserves: the compiler is an oracle, tree-sitter is
the backbone, and a missing toolchain degrades to the pure tree-sitter graph --
never worse.
"""

from __future__ import annotations

# The bundled frontends register from `builtin`, not from here: each one loads
# its language package, which imports this package's protocol, so registering
# on package import closed a cycle whenever a language package loaded first.
from .protocol import (
    CallSiteKey,
    LanguageFrontend,
    ResolvedCallSite,
    SemanticFacts,
    empty_facts,
)
from .registry import (
    FRONTENDS,
    register_frontend,
)

__all__ = [
    "FRONTENDS",
    "CallSiteKey",
    "LanguageFrontend",
    "ResolvedCallSite",
    "SemanticFacts",
    "empty_facts",
    "register_frontend",
]
