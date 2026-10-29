"""The frontend registry with every bundled frontend registered.

Each frontend module registers itself when imported; this module imports them
all. The graph builder, which owns the dispatch loop, reads the registry from
here, so the frontends package itself stays free of language packages.
"""

from __future__ import annotations

from ..csharp import semantic_frontend as csharp
from ..go import semantic_frontend as go
from ..java import semantic_frontend as java
from ..python import semantic_frontend as python
from .registry import FRONTENDS

BUNDLED_FRONTEND_MODULES = (csharp, go, java, python)

__all__ = ["BUNDLED_FRONTEND_MODULES", "FRONTENDS"]
