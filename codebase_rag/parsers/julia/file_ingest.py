"""Julia's share of the file pass: the order its passes run in, types before
functions.

`DefinitionProcessor` owns what every language shares (a file's parse, its
Module node, and the function and class passes over it) and reads that
order off the `JuliaFileIngest` it builds; it holds no Julia state."""

from __future__ import annotations

from ..language_definitions import LanguageFileIngest


class JuliaFileIngest(LanguageFileIngest):
    """Runs a Julia file's class pass before its function pass.

    Built and owned by `DefinitionProcessor`."""

    __slots__ = ()

    # A struct and a free function may share a name (separate namespaces),
    # and the TYPE is the primary owner of it (issue #1882). The inline-module
    # pass is skipped: a Julia `module` block is the file's declared module
    # (an import index), not a nested module node, so a synthetic
    # `fileqn.Module` node would clobber that model.
    types_before_functions = True
