"""The class pass (`ClassIngest`) and the pieces of it each language's class
collaborator shares.

The language packages (`parsers/<language>/class_ingest.py`) import this
package's leaf modules (`identity`, `base_targets`, `relationships`, ...),
and `ingest` imports every language package, so `ClassIngest` loads on first
use rather than with the package: importing it here closed an import cycle
through each of those languages."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .ingest import ClassIngest

__all__ = ["ClassIngest"]


def __getattr__(name: str) -> type[ClassIngest]:
    if name == "ClassIngest":
        from .ingest import ClassIngest

        return ClassIngest
    raise AttributeError(name)
