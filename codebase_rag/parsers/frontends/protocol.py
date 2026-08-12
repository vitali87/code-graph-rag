"""The `LanguageFrontend` / `EmittingFrontend` contract and the `SemanticFacts`
bundle (issue #1178). Generalized from the proven Roslyn/C# integration."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Protocol, runtime_checkable

from ...constants.languages import SupportedLanguage
from ...services import IngestorProtocol
from ...types_defs import FunctionRegistryTrieProtocol, SimpleNameLookup

# Call-site join key: (rel_file, name_token_line, name_token_byte_col, simple_name).
# The NAME token, not the expression start: nested invocations (`Make().Handle(x)`)
# share a start position but their name tokens never collide. Columns are BYTE
# offsets so a compiler's UTF-16 columns must be re-measured in UTF-8 to match
# tree-sitter (issue #1178, faithful to the Roslyn contract).
CallSiteKey = tuple[str, int, int, str]

# A class's base-type kinds: (rel_file, type_start_line) -> {base_simple_name: kind}
# where kind is "class" (INHERITS) or "interface" (IMPLEMENTS).
BaseKindMap = dict[tuple[str, int], dict[str, str]]


@dataclass(frozen=True)
class ResolvedCallSite:
    """The declaration a call binds to, per the language's compiler (overloads by
    argument type, extension/receiver binding, interface dispatch)."""

    name: str
    target_file: str
    target_line: int
    target_col: int


@dataclass(frozen=True)
class QueryCall:
    """A generated/implicit operator call keyed on the enclosing member's decl
    (C# LINQ today); other frontends leave this family empty."""

    caller_file: str
    caller_line: int
    caller_col: int
    target_file: str
    target_line: int
    target_col: int


@dataclass
class SemanticFacts:
    """Everything one frontend run learned about the repo. Every family is OPTIONAL
    per language (a Phase-2 frontend fills only what its compiler models); the
    processors apply each non-empty family and fall back per-miss to the tree-sitter
    heuristics. A FRESH instance per run -- the maps are handed to mutable processor
    state, so a shared constant would alias across runs (issue #1178)."""

    # The two families every compiler frontend fills (Go/TS/Java/Rust-SCIP, #1179+).
    resolved_call_sites: dict[CallSiteKey, ResolvedCallSite] = field(
        default_factory=dict
    )
    # Sites the compiler resolved to a metadata/external declaration: proof the call
    # leaves the repo, so the name-trie fallback must not fabricate a first-party edge.
    external_sites: set[CallSiteKey] = field(default_factory=set)
    # C#-specific families today (generalization deferred until a second consumer).
    base_kinds: BaseKindMap = field(default_factory=dict)
    partial_groups: list[list[tuple[str, int]]] = field(default_factory=list)
    query_calls: list[QueryCall] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not (
            self.resolved_call_sites
            or self.external_sites
            or self.base_kinds
            or self.partial_groups
            or self.query_calls
        )


def empty_facts() -> SemanticFacts:
    """A fresh empty bundle -- the degradation result when a toolchain is absent."""
    return SemanticFacts()


@runtime_checkable
class LanguageFrontend(Protocol):
    """A compiler fact provider for one language. `available()` gates on the
    toolchain, `applies()` on the repo containing a discoverable project; a False
    from either degrades to the pure tree-sitter graph."""

    language: SupportedLanguage

    def available(self) -> bool: ...

    def applies(self, repo_path: Path) -> bool: ...

    def run(self, repo_path: Path, files: Sequence[Path]) -> SemanticFacts: ...


class FrontendPhase(StrEnum):
    # Run before the tree-sitter definition pass (libclang LIBCLANG mode emits its
    # own Function nodes, and Pass 2 skips the files it covered).
    BEFORE_DEFINITIONS = "before_definitions"
    # Run after the definition pass (libclang HYBRID mode attributes macro calls to
    # the tree-sitter spans that already exist).
    AFTER_DEFINITIONS = "after_definitions"


@dataclass
class FrontendEmitContext:
    """What an emitting frontend needs to write graph elements directly (the C++
    libclang integration): the ingestor plus the graph's already-computed
    registries and file-selection state (issue #1178)."""

    ingestor: IngestorProtocol
    repo_path: Path
    project_name: str
    compdb_dir: Path
    function_registry: FunctionRegistryTrieProtocol | None = None
    simple_name_lookup: SimpleNameLookup | None = None
    structural_elements: dict[Path, str | None] | None = None
    exclude_paths: frozenset[str] | None = None
    unignore_paths: frozenset[str] | None = None
    owned_qns: dict[str, set[str]] | None = None


@dataclass
class FrontendEmitResult:
    """What an emitting frontend returns to the graph builder: the set of files it
    fully covered (so the definition pass skips them, LIBCLANG mode) and any calls
    to be joined to tree-sitter spans in a later phase (HYBRID mode). The pending
    lists stay untyped here to avoid importing the cpp_frontend internals; the C++
    frontend adapter narrows them."""

    covered_files: frozenset[str] = frozenset()
    pending_macro_calls: list[object] = field(default_factory=list)
    pending_expansion_calls: list[object] = field(default_factory=list)


@runtime_checkable
class EmittingFrontend(Protocol):
    """A frontend that also emits graph elements the backbone cannot produce, in a
    declared run phase (the C++ libclang integration)."""

    language: SupportedLanguage
    phase: FrontendPhase

    def available(self) -> bool: ...

    def applies(self, repo_path: Path) -> bool: ...

    def emit(self, ctx: FrontendEmitContext) -> FrontendEmitResult: ...
