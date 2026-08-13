"""Go semantic frontend (issue #1179): a bundled go/packages tool projected onto
the generic `LanguageFrontend` contract. Re-exports the public surface so callers
import from the package, not the build/run module."""

from __future__ import annotations

from .frontend import (
    CallSiteKey,
    GoCallSite,
    GoSemanticFacts,
    find_go_module,
    go_frontend_available,
    resolve_go_frontend,
    run_go_frontend,
)

__all__ = [
    "CallSiteKey",
    "GoCallSite",
    "GoSemanticFacts",
    "find_go_module",
    "go_frontend_available",
    "resolve_go_frontend",
    "run_go_frontend",
]
