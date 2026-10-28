"""The frontend registry with every bundled frontend registered.

Each frontend module registers itself when imported; this module imports them
all. The graph builder, which owns the dispatch loop, reads the registry from
here, so the frontends package itself stays free of language packages.
"""

from __future__ import annotations

from . import cpp, csharp, go, java, python
from .registry import EMITTING_FRONTENDS, FRONTENDS

BUNDLED_FRONTEND_MODULES = (cpp, csharp, go, java, python)

__all__ = ["BUNDLED_FRONTEND_MODULES", "EMITTING_FRONTENDS", "FRONTENDS"]
