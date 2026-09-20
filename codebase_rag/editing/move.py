"""`move(qn, target_module)`: edit-algebra operation 3 (issue #1534).

Moving a symbol out of a shared dumping ground into the one package that
uses it is the refactor that shrinks affected sets, and by hand it is
tedious: the definition, its own imports, every importer, re-exports. The
graph knows the definition's span, the imports its module binds, and every
importer's statement (issue #1522), so the whole move is one transaction:

- cut the definition (decorators, docstring, adjacent comments included)
  and paste it into the target with the imports it needs;
- rewrite every importer through the import rewriter;
- give the old module an import of the moved name when it still uses it,
  and optionally a deprecation re-export (`keep_alias=True`);
- refuse before touching a file when the move would create an import
  cycle, naming the cycle.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from ..graph_query import QueryFn
from .contract import Reingest
from .move_scope import _module_bound
from .move_types import MoveRefused, MoveReport
from .transaction import StagedTree, VerificationResult


def __getattr__(name: str):
    if name == "Mover":
        from .move_operation import Mover

        return Mover
    raise AttributeError(name)


def move(
    repo_root: Path,
    fetch_all: QueryFn,
    project_name: str,
    qualified_name: str,
    target_module: str,
    keep_alias: bool = False,
    dry_run: bool = False,
    verify: Callable[[StagedTree], VerificationResult | bool | None] | None = None,
    reingest: Reingest | None = None,
) -> MoveReport:
    """The op: plan (refusing on a cycle) or plan and apply."""
    from .move_operation import move as _move

    return _move(
        repo_root,
        fetch_all,
        project_name,
        qualified_name,
        target_module,
        keep_alias=keep_alias,
        dry_run=dry_run,
        verify=verify,
        reingest=reingest,
    )


__all__ = ["MoveRefused", "MoveReport", "move", "_module_bound"]
