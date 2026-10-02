"""Safe `row` / `column` accessors on tree-sitter's `Point` (issue #2393).

py-tree-sitter 0.26.0 implements `Point.row` and `Point.column` as getters
that return `PyTuple_GetItem(self, i)`, a BORROWED reference, where a getter
must hand back a new one. The interpreter releases what a getter returns, so
every read drops a reference the tuple still owns. Ints below 257 are
immortal in CPython 3.12 and survive it; a row or column of 257 or more is
freed while the Point still holds it, and the heap corruption crashed the Go
call pass on spf13/cobra with SIGSEGV. Upstream fixed the getters after the
0.26.0 release.

`Point` subclasses `tuple`, so reading the item is exactly what the getters
mean. Replacing them once, on import, keeps any caller safe, including code
written after this that reaches for the attribute rather than the index.
"""

from __future__ import annotations

from operator import itemgetter

from tree_sitter import Point

_ACCESSORS = (("row", 0), ("column", 1))


def install() -> None:
    if not issubclass(Point, tuple):
        return
    for name, index in _ACCESSORS:
        try:
            setattr(Point, name, property(itemgetter(index)))
        except TypeError:
            # An immutable Point type comes from a binding that rebuilt the
            # class, which is where the getters were fixed.
            return


install()
