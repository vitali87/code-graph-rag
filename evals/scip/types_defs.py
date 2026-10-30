from typing import NamedTuple


class ScipRange(NamedTuple):
    # Zero-based, end-exclusive, as SCIP stores it.
    start_line: int
    start_char: int
    end_line: int
    end_char: int


class ScipOccurrence(NamedTuple):
    range: ScipRange
    symbol: str
    roles: int
    # The full span of the definition (body included), when the indexer
    # emits it; None otherwise.
    enclosing: ScipRange | None


class ScipRelationship(NamedTuple):
    symbol: str
    is_reference: bool
    is_implementation: bool
    is_type_definition: bool
    is_definition: bool


class ScipSymbol(NamedTuple):
    symbol: str
    kind: int
    display_name: str
    relationships: tuple[ScipRelationship, ...]
    enclosing_symbol: str


class ScipDocument(NamedTuple):
    path: str
    language: str
    occurrences: tuple[ScipOccurrence, ...]
    symbols: tuple[ScipSymbol, ...]


class ScipIndex(NamedTuple):
    tool: str
    version: str
    project_root: str
    documents: tuple[ScipDocument, ...]
    external_symbols: tuple[ScipSymbol, ...]


class Descriptor(NamedTuple):
    name: str
    suffix: str
    disambiguator: str


class ParsedSymbol(NamedTuple):
    scheme: str
    manager: str
    package: str
    version: str
    descriptors: tuple[Descriptor, ...]
    # The document-local id for a `local <id>` symbol, else "".
    local: str


class Definition(NamedTuple):
    # A definition site, keyed the way both sides can agree on it: the file,
    # the zero-based line of the defining name, and that name.
    path: str
    line: int
    name: str


class OracleEdges(NamedTuple):
    # Caller/callee pairs as definition sites; a module-level caller is the
    # file's Definition with line -1 and an empty name.
    edges: set[tuple[Definition, Definition]]
    # The same calls with the callee widened to every override SCIP records
    # (class-hierarchy analysis): what a dispatch-aware graph may also emit.
    dispatch_edges: set[tuple[Definition, Definition]]
    callables: frozenset[Definition]
