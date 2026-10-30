"""What the language importers reach on the `ImportProcessor` that builds
them, declared once for their host Protocols to take.

Every importer records where each binding was written through the
processor (issue #1522), whose flush puts the site on the IMPORTS edge; the
importers that queue an edge outside the shared deferral, read the
indexer's walk policy or note a name they could not resolve do so through
it too. Each importer is built from one `ImporterContext`. This module
imports no importer, so each can import it."""

from pathlib import Path
from typing import NamedTuple, Protocol

from tree_sitter import Node

from .. import constants as cs
from ..types_defs import PropertyDict


class ImporterContext(NamedTuple):
    """The repository and project a language's importer derives module qns
    from, and the processor's own import map, which the importer writes its
    bindings into: shared rather than copied, so every later pass reads what
    an import binds."""

    repo_path: Path
    project_name: str
    import_mapping: dict[str, dict[str, str]]


class ImportSiteHost(Protocol):
    """The import-site record every language's bindings go through, whose
    sites ride on the IMPORTS edges the processor's flush emits."""

    def record_import_site(
        self,
        scope_qn: str,
        local_name: str,
        node: Node | None,
        imported_name: str | None = None,
        owner_qn: str | None = None,
    ) -> PropertyDict | None: ...


class ImportQueueHost(ImportSiteHost, Protocol):
    """The deferred-edge queue an importer adds to itself, for an edge its
    language decides outside the shared deferral."""

    def defer_import_edge(
        self,
        module_qn: str,
        full_name: str,
        language: cs.SupportedLanguage,
        site: PropertyDict | None = None,
    ) -> None: ...


class WalkPolicyHost(Protocol):
    """The indexer's walk policy, read where it is used rather than copied
    in: the factory re-resolves `unignore_paths` on processors it has
    already built (`propagate_unignore_paths`), so a copy goes stale and
    reads files the indexer skips, or misses a tree it rescues (issue
    #1088)."""

    exclude_paths: frozenset[str] | None
    unignore_paths: frozenset[str] | None


class UnresolvedNameHost(Protocol):
    """The record of a name a module referenced and could not resolve (issue
    #1568), which the updater matches an added file against to find the
    modules that wait on it. The import processor owns it; every pass notes
    into it."""

    def note_unresolved(self, module_qn: str, name: str) -> None: ...
