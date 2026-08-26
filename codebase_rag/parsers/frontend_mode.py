"""Shared frontend-mode resolution.

A leaf module by design: the semantic frontends import it, and it must not
pull in ``parsers.frontends``, whose package __init__ registers every
language frontend and would close an import cycle.
"""

from __future__ import annotations

from collections.abc import Callable
from enum import Enum
from typing import TypeVar

_FrontendMode = TypeVar("_FrontendMode", bound=Enum)


def resolve_frontend_mode(
    mode: _FrontendMode,
    fallback: _FrontendMode,
    available: Callable[[], bool],
    *,
    auto: _FrontendMode | None = None,
    auto_resolves_to: _FrontendMode | None = None,
) -> _FrontendMode:
    """The EFFECTIVE frontend mode for a configured one.

    Every language frontend resolves the same way: the explicitly disabled
    mode stays as configured, any toolchain-backed mode degrades to
    `fallback` when the toolchain is absent, and AUTO means the preferred
    backend when it is present. Languages with only two modes pass
    `auto=None` and name the single backed mode as `auto_resolves_to`. The
    parser fingerprint records the RESOLVED mode, so a toolchain-backed graph
    and a heuristic one never share an identity.
    """
    if mode == fallback:
        return mode
    if not available():
        return fallback
    if auto_resolves_to is not None and (auto is None or mode == auto):
        return auto_resolves_to
    return mode
