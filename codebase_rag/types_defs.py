from __future__ import annotations

from collections import defaultdict
from collections.abc import Awaitable, Callable, ItemsView, KeysView, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple, Protocol, TypedDict

from prompt_toolkit.styles import Style

from .constants import AuditCheck, NodeLabel, RelationshipType, SupportedLanguage

if TYPE_CHECKING:
    from tree_sitter import Language, Node, Parser, Query

    from .models import LanguageSpec

type LanguageLoader = Callable[[], Language] | None

PropertyValue = str | int | float | bool | list[str] | None
PropertyDict = dict[str, PropertyValue]

type ResultScalar = str | int | float | bool | None
type ResultValue = ResultScalar | list[ResultScalar] | dict[str, ResultScalar]
type ResultRow = dict[str, ResultValue]


class FunctionMatch(TypedDict):
    node: Node
    simple_name: str
    qualified_name: str
    parent_class: str | None
    line_number: int


class NodeBatchRow(TypedDict):
    id: PropertyValue
    props: PropertyDict


class RelBatchRow(TypedDict):
    from_val: PropertyValue
    to_val: PropertyValue
    props: PropertyDict


BatchParams = NodeBatchRow | RelBatchRow | PropertyDict


class BatchWrapper(TypedDict):
    batch: Sequence[BatchParams]


type SimpleName = str
type QualifiedName = str
type SimpleNameLookup = defaultdict[SimpleName, set[QualifiedName]]

NodeIdentifier = tuple[NodeLabel | str, str, str | None]


type ASTNode = Node


class NodeType(StrEnum):
    FUNCTION = "Function"
    METHOD = "Method"
    CLASS = "Class"
    MODULE = "Module"
    INTERFACE = "Interface"
    PACKAGE = "Package"
    ENUM = "Enum"
    TYPE = "Type"
    UNION = "Union"


type TrieNode = dict[str, TrieNode | QualifiedName | NodeType]
type FunctionRegistry = dict[QualifiedName, NodeType]


class FunctionRegistryTrieProtocol(Protocol):
    def __contains__(self, qualified_name: QualifiedName) -> bool: ...
    def __getitem__(self, qualified_name: QualifiedName) -> NodeType: ...

    def __setitem__(
        self, qualified_name: QualifiedName, func_type: NodeType
    ) -> None: ...

    def get(
        self, qualified_name: QualifiedName, default: NodeType | None = None
    ) -> NodeType | None: ...
    def keys(self) -> KeysView[QualifiedName]: ...
    def items(self) -> ItemsView[QualifiedName, NodeType]: ...
    def find_with_prefix(self, prefix: str) -> list[tuple[QualifiedName, NodeType]]: ...

    def find_ending_with(self, suffix: str) -> list[QualifiedName]: ...

    def register_unique_qn(
        self, natural_qn: QualifiedName, start_line: int
    ) -> QualifiedName: ...

    def variants(self, qualified_name: QualifiedName) -> list[QualifiedName]: ...

    def mark_property(self, qualified_name: QualifiedName) -> None: ...

    def is_property(self, qualified_name: QualifiedName) -> bool: ...

    def property_names(self) -> set[str]: ...

    def mark_abstract(self, qualified_name: QualifiedName) -> None: ...

    def is_abstract(self, qualified_name: QualifiedName) -> bool: ...

    def mark_callable_params(
        self, qualified_name: QualifiedName, params: dict[str, int]
    ) -> None: ...

    def callable_params(
        self, qualified_name: QualifiedName
    ) -> dict[str, int] | None: ...


class ASTCacheProtocol(Protocol):
    def __setitem__(self, key: Path, value: tuple[Node, SupportedLanguage]) -> None: ...

    def __getitem__(self, key: Path) -> tuple[Node, SupportedLanguage]: ...
    def __delitem__(self, key: Path) -> None: ...
    def __contains__(self, key: Path) -> bool: ...
    def items(self) -> ItemsView[Path, tuple[Node, SupportedLanguage]]: ...
    def load(self, key: Path) -> tuple[Node, SupportedLanguage] | None: ...


class ColumnDescriptor(Protocol):
    @property
    def name(self) -> str: ...


class LoadableProtocol(Protocol):
    def _ensure_loaded(self) -> None: ...


class CursorProtocol(Protocol):
    def execute(
        self,
        query: str,
        params: dict[str, PropertyValue]
        | Sequence[BatchParams]
        | BatchWrapper
        | None = None,
    ) -> None: ...
    def close(self) -> None: ...
    @property
    def description(self) -> Sequence[ColumnDescriptor] | None: ...
    def fetchall(self) -> list[tuple[PropertyValue, ...]]: ...


class PathValidatorProtocol(Protocol):
    @property
    def project_root(self) -> Path: ...


class TreeSitterNodeProtocol(Protocol):
    @property
    def type(self) -> str: ...
    @property
    def children(self) -> list[TreeSitterNodeProtocol]: ...
    @property
    def text(self) -> bytes: ...


class ModelConfigKwargs(TypedDict, total=False):
    api_key: str | None
    endpoint: str | None
    project_id: str | None
    region: str | None
    provider_type: str | None
    thinking_budget: int | None
    service_account_file: str | None


class GraphMetadata(TypedDict):
    total_nodes: int
    total_relationships: int
    exported_at: str


class NodeData(TypedDict):
    node_id: int
    labels: list[str]
    properties: dict[str, PropertyValue]


class RelationshipData(TypedDict):
    from_id: int
    to_id: int
    type: str
    properties: dict[str, PropertyValue]


class GraphData(TypedDict):
    nodes: list[NodeData] | list[ResultRow]
    relationships: list[RelationshipData] | list[ResultRow]
    metadata: GraphMetadata


class GraphSummary(TypedDict):
    total_nodes: int
    total_relationships: int
    node_labels: dict[str, int]
    relationship_types: dict[str, int]
    metadata: GraphMetadata


class QueryJsonOutput(TypedDict):
    query: str
    response: str


class EmbeddingQueryResult(TypedDict):
    node_id: int
    qualified_name: str
    start_line: int | None
    end_line: int | None
    path: str | None


class SemanticSearchResult(TypedDict):
    node_id: int
    qualified_name: str
    name: str
    type: str
    score: float


class JavaClassInfo(TypedDict):
    name: str | None
    type: str
    superclass: str | None
    interfaces: list[str]
    modifiers: list[str]
    type_parameters: list[str]


class JavaMethodInfo(TypedDict):
    name: str | None
    type: str
    return_type: str | None
    parameters: list[str]
    modifiers: list[str]
    type_parameters: list[str]
    annotations: list[str]


class JavaFieldInfo(TypedDict):
    name: str | None
    type: str | None
    modifiers: list[str]
    annotations: list[str]


class JavaAnnotationInfo(TypedDict):
    name: str | None
    arguments: list[str]


class JavaMethodCallInfo(TypedDict):
    name: str | None
    object: str | None
    arguments: int


class CancelledResult(NamedTuple):
    cancelled: bool


class CgrignorePatterns(NamedTuple):
    exclude: frozenset[str]
    unignore: frozenset[str]


class AgentLoopUI(NamedTuple):
    status_message: str
    cancelled_log: str
    approval_prompt: str
    denial_default: str
    panel_title: str


ORANGE_STYLE = Style.from_dict(
    {
        "": "#ff8c00",
        "bottom-toolbar": "noreverse fg:#888888",
        "bottom-toolbar.text": "noreverse fg:#888888",
    }
)

OPTIMIZATION_LOOP_UI = AgentLoopUI(
    status_message="[bold green]Agent is analyzing codebase... (Press Ctrl+C to cancel)[/bold green]",
    cancelled_log="ASSISTANT: [Analysis was cancelled]",
    approval_prompt="Do you approve this optimization?",
    denial_default="User rejected this optimization without feedback",
    panel_title="[bold green]Optimization Agent[/bold green]",
)

CHAT_LOOP_UI = AgentLoopUI(
    status_message="[bold green]Thinking... (Press Ctrl+C to cancel)[/bold green]",
    cancelled_log="ASSISTANT: [Thinking was cancelled]",
    approval_prompt="Do you approve this change?",
    denial_default="User rejected this change without feedback",
    panel_title="[bold green]Assistant[/bold green]",
)


class LanguageImport(NamedTuple):
    lang_key: SupportedLanguage
    module_path: str
    attr_name: str
    submodule_name: SupportedLanguage


class ToolNames(NamedTuple):
    query_graph: str
    read_file: str
    semantic_search: str
    create_file: str
    edit_file: str
    shell_command: str


class ConfirmationToolNames(NamedTuple):
    replace_code: str
    create_file: str
    shell_command: str


class ReplaceCodeArgs(TypedDict, total=False):
    file_path: str
    target_code: str
    replacement_code: str


class CreateFileArgs(TypedDict, total=False):
    file_path: str
    content: str


class ShellCommandArgs(TypedDict, total=False):
    command: str


@dataclass
class RawToolArgs:
    file_path: str = ""
    target_code: str = ""
    replacement_code: str = ""
    content: str = ""
    command: str = ""


ToolArgs = ReplaceCodeArgs | CreateFileArgs | ShellCommandArgs


class LanguageQueries(TypedDict):
    functions: Query | None
    classes: Query | None
    calls: Query | None
    imports: Query | None
    locals: Query | None
    highlights: Query | None
    config: LanguageSpec
    language: Language
    parser: Parser


class FunctionNodeProps(TypedDict, total=False):
    qualified_name: str
    name: str | None
    start_line: int
    end_line: int
    docstring: str | None


MCPToolArguments = dict[str, str | int | None]


class MCPInputSchemaProperty(TypedDict, total=False):
    type: str
    description: str
    default: str | int


MCPInputSchemaProperties = dict[str, MCPInputSchemaProperty]


class MCPInputSchema(TypedDict):
    type: str
    properties: MCPInputSchemaProperties
    required: list[str]


class MCPToolSchema(NamedTuple):
    name: str
    description: str
    inputSchema: MCPInputSchema


class QueryResultDict(TypedDict, total=False):
    query_used: str
    results: list[ResultRow]
    summary: str
    error: str


class CodeSnippetResultDict(TypedDict, total=False):
    qualified_name: str
    source_code: str
    file_path: str
    line_start: int
    line_end: int
    docstring: str | None
    found: bool
    error_message: str | None
    error: str


class DeadCodeRow(TypedDict):
    label: str
    name: str
    qualified_name: str
    start_line: int
    end_line: int


class DeadCodeConfig(NamedTuple):
    include_tests: bool
    include_classes: bool
    root_decorators: frozenset[str]
    entry_points: tuple[str, ...]
    test_patterns: tuple[str, ...]
    exclude_patterns: tuple[str, ...] = ()


class GraphQueryClient(Protocol):
    def fetch_all(
        self, query: str, params: dict[str, PropertyValue] | None = None
    ) -> list[ResultRow]: ...


class ListProjectsSuccessResult(TypedDict):
    projects: list[str]
    count: int


class ListProjectsErrorResult(TypedDict):
    projects: list[str]
    count: int
    error: str


ListProjectsResult = ListProjectsSuccessResult | ListProjectsErrorResult


class DeleteProjectSuccessResult(TypedDict):
    success: bool
    project: str
    message: str


class DeleteProjectErrorResult(TypedDict):
    success: bool
    error: str


DeleteProjectResult = DeleteProjectSuccessResult | DeleteProjectErrorResult


MCPResultType = (
    str
    | QueryResultDict
    | CodeSnippetResultDict
    | ListProjectsResult
    | DeleteProjectResult
)
MCPHandlerType = Callable[..., Awaitable[MCPResultType]]


class NodeSchema(NamedTuple):
    label: NodeLabel
    properties: str


class GraphNodeRecord(NamedTuple):
    label: str
    properties: PropertyDict


type RelEndpointSpec = tuple[str, str, PropertyValue]


class GraphRelRecord(NamedTuple):
    from_spec: RelEndpointSpec
    rel_type: str
    to_spec: RelEndpointSpec


class AuditViolation(NamedTuple):
    check: AuditCheck
    detail: str


class DeferredParentLink(NamedTuple):
    """Containment edge whose non-module parent must exist before emission.

    Parents can be registered by a later pass than the child (methods land in
    the class pass after the function pass; forward declarations register
    last), so verification waits until every pass finishes. A parent qn that
    never registers is a phantom the database would drop, so the child
    anchors to its registered lexical fallback when one is known (a nested
    prototype assignment belongs to its enclosing function), else its module.
    """

    parent_label_guess: str
    parent_qn: str
    child_label: str
    child_qn: str
    module_qn: str
    rel_type: str = RelationshipType.DEFINES.value
    fallback_label: str | None = None
    fallback_qn: str | None = None


# (H) (module_qn, 1-based start line, 0-based start column) of a function
# (H) node; the full span identifies the function even on shared lines.
type FunctionSpanKey = tuple[str, int, int]


class FunctionLocation(NamedTuple):
    """Where the definition pass put a C++ function/method node.

    Keyed by (module_qn, start_line, start_col) so Pass-3 call attribution
    reuses the exact label and qn Pass 2 registered instead of re-deriving
    them from the AST; the two walks diverge on preprocessor-distorted class
    bodies and every divergence is a phantom caller the database drops
    (issue #652). The column in the key keeps same-line functions (a one-line
    curried arrow, minified `exports.a = ...; exports.b = ...`) from evicting
    or masking each other's records.
    """

    label: str
    qualified_name: str
    container_qn: str | None
    # (H) False when the qn was GENERATED (anonymous_row_col, iife_*): Pass-3
    # (H) lets an unnamed JS/TS function expression adopt a NAMED record (the
    # (H) node a named pass registered for `exports.f = function`), while a
    # (H) generated record keeps the historical bubble-to-module attribution.
    is_named: bool = True


class CppDefinitionSpan(NamedTuple):
    """Full line span of a C/C++ function or method the tree-sitter pass ingested.

    Recorded per relative file path so the hybrid C++ frontend can attribute a
    macro use (a TU-level preprocessing entity with only a location) to the
    tightest enclosing TREE-SITTER definition after Pass 2; libclang's own
    spans carry wrong-scheme qns wherever macros hide namespaces.
    """

    start_line: int
    end_line: int
    label: str
    qualified_name: str


class PendingMacroCall(NamedTuple):
    """A macro use the hybrid C++ frontend saw but cannot attribute yet.

    The caller is resolvable only after the tree-sitter pass has recorded its
    definition spans; a use outside every span attributes to the fallback
    Module, mirroring the module-caller rule for ordinary calls.
    """

    rel_path: str
    line: int
    callee_qn: str
    fallback_module_qn: str


class PendingExpansionCall(NamedTuple):
    """A call that exists only after macro expansion, seen by the hybrid frontend.

    The call's text lives inside a macro definition body, so tree-sitter never
    sees it at the expansion site. Both ends carry only locations: the caller
    joins to the tightest tree-sitter definition span containing the expansion
    site (falling back to the Module), the callee to the span containing the
    referenced definition (dropped when none exists) -- so the emitted CALLS
    edge is tree-sitter-scheme on both ends.
    """

    caller_rel_path: str
    caller_line: int
    callee_rel_path: str
    callee_line: int
    fallback_module_qn: str


class DeferredCppInherit(NamedTuple):
    """C++ INHERITS edge held back until every class is registered.

    A base written in another header cannot resolve at parse time, so the
    edge is emitted after Pass 2 with the base resolved namespace-scoped
    across files; an unresolvable base emits no edge rather than a phantom
    the database would drop.
    """

    child_label: str
    child_qn: str
    base_name: str
    guess_qn: str
    namespace_path: str
    base_index: int


class DeferredInherit(NamedTuple):
    """Non-C++ INHERITS/IMPLEMENTS edge held back until every class is registered.

    A parent that does not resolve at parse time is anchored to the child's
    own module qn as a guess; the edge is emitted after Pass 2 with the guess
    re-resolved against the full registry. An unresolvable parent emits no
    edge rather than a phantom the database would drop.
    """

    rel_type: RelationshipType
    child_qn: str
    parent_qn: str
    module_qn: str
    base_index: int
    language: SupportedLanguage


class DeferredImportEdge(NamedTuple):
    """IMPORTS edge held back until every file is parsed.

    An internal-looking target is only real if some file (or inline module)
    actually yields that module qn; verification happens against the full
    module registry, and a target that resolves nowhere emits no edge.
    """

    module_qn: str
    full_name: str
    language: SupportedLanguage


class RelationshipSchema(NamedTuple):
    sources: tuple[NodeLabel, ...]
    rel_type: RelationshipType
    targets: tuple[NodeLabel, ...]


NODE_SCHEMAS: tuple[NodeSchema, ...] = (
    NodeSchema(NodeLabel.PROJECT, "{name: string}"),
    NodeSchema(
        NodeLabel.PACKAGE,
        "{qualified_name: string, name: string, path: string, absolute_path: string}",
    ),
    NodeSchema(NodeLabel.FOLDER, "{path: string, name: string, absolute_path: string}"),
    NodeSchema(
        NodeLabel.FILE,
        "{path: string, name: string, extension: string?, absolute_path: string}",
    ),
    NodeSchema(
        NodeLabel.MODULE,
        "{qualified_name: string, name: string, path: string, absolute_path: string, start_line: int?, end_line: int?}",
    ),
    NodeSchema(
        NodeLabel.CLASS,
        "{qualified_name: string, name: string, modifiers: list[string], decorators: list[string], path: string, absolute_path: string, start_line: int?, end_line: int?, docstring: string?, is_exported: boolean?}",
    ),
    NodeSchema(
        NodeLabel.FUNCTION,
        "{qualified_name: string, name: string, modifiers: list[string], decorators: list[string], path: string, absolute_path: string, start_line: int?, end_line: int?, docstring: string?, is_exported: boolean?, is_macro: boolean?}",
    ),
    NodeSchema(
        NodeLabel.METHOD,
        "{qualified_name: string, name: string, modifiers: list[string], decorators: list[string], path: string, absolute_path: string, start_line: int?, end_line: int?, docstring: string?, is_exported: boolean?, is_property: boolean?, overrides_external: boolean?}",
    ),
    NodeSchema(
        NodeLabel.INTERFACE,
        "{qualified_name: string, name: string, path: string, absolute_path: string, modifiers: list[string]?, decorators: list[string]?, start_line: int?, end_line: int?, docstring: string?, is_exported: boolean?}",
    ),
    NodeSchema(
        NodeLabel.ENUM,
        "{qualified_name: string, name: string, path: string, absolute_path: string, modifiers: list[string]?, decorators: list[string]?, start_line: int?, end_line: int?, docstring: string?, is_exported: boolean?}",
    ),
    NodeSchema(
        NodeLabel.TYPE,
        "{qualified_name: string, name: string, path: string?, absolute_path: string?, modifiers: list[string]?, decorators: list[string]?, start_line: int?, end_line: int?, docstring: string?, is_exported: boolean?}",
    ),
    NodeSchema(
        NodeLabel.UNION,
        "{qualified_name: string, name: string, path: string?, absolute_path: string?, modifiers: list[string]?, decorators: list[string]?, start_line: int?, end_line: int?, docstring: string?, is_exported: boolean?}",
    ),
    NodeSchema(
        NodeLabel.MODULE_INTERFACE,
        "{qualified_name: string, name: string, path: string, absolute_path: string, module_type: string}",
    ),
    NodeSchema(
        NodeLabel.MODULE_IMPLEMENTATION,
        "{qualified_name: string, name: string, path: string, absolute_path: string, implements_module: string, module_type: string}",
    ),
    NodeSchema(NodeLabel.EXTERNAL_PACKAGE, "{name: string}"),
    NodeSchema(
        NodeLabel.EXTERNAL_MODULE,
        "{qualified_name: string, name: string, path: string}",
    ),
    NodeSchema(
        NodeLabel.RESOURCE,
        "{qualified_name: string, name: string, kind: string}",
    ),
)


RELATIONSHIP_SCHEMAS: tuple[RelationshipSchema, ...] = (
    RelationshipSchema(
        (NodeLabel.PROJECT, NodeLabel.PACKAGE, NodeLabel.FOLDER),
        RelationshipType.CONTAINS_PACKAGE,
        (NodeLabel.PACKAGE,),
    ),
    RelationshipSchema(
        (NodeLabel.PROJECT, NodeLabel.PACKAGE, NodeLabel.FOLDER),
        RelationshipType.CONTAINS_FOLDER,
        (NodeLabel.FOLDER,),
    ),
    RelationshipSchema(
        (NodeLabel.PROJECT, NodeLabel.PACKAGE, NodeLabel.FOLDER),
        RelationshipType.CONTAINS_FILE,
        (NodeLabel.FILE,),
    ),
    RelationshipSchema(
        (NodeLabel.PROJECT, NodeLabel.PACKAGE, NodeLabel.FOLDER),
        RelationshipType.CONTAINS_MODULE,
        (NodeLabel.MODULE,),
    ),
    RelationshipSchema(
        (NodeLabel.MODULE, NodeLabel.FUNCTION, NodeLabel.METHOD, NodeLabel.CLASS),
        RelationshipType.DEFINES,
        (
            NodeLabel.CLASS,
            NodeLabel.FUNCTION,
            NodeLabel.METHOD,
            NodeLabel.ENUM,
            NodeLabel.INTERFACE,
            NodeLabel.TYPE,
            NodeLabel.UNION,
            NodeLabel.MODULE,
        ),
    ),
    RelationshipSchema(
        (
            NodeLabel.CLASS,
            NodeLabel.INTERFACE,
            NodeLabel.ENUM,
            NodeLabel.TYPE,
            NodeLabel.UNION,
        ),
        RelationshipType.DEFINES_METHOD,
        (NodeLabel.METHOD,),
    ),
    RelationshipSchema(
        (NodeLabel.MODULE,),
        RelationshipType.IMPORTS,
        (NodeLabel.MODULE, NodeLabel.EXTERNAL_MODULE),
    ),
    RelationshipSchema(
        (NodeLabel.MODULE,),
        RelationshipType.EXPORTS,
        (NodeLabel.CLASS, NodeLabel.FUNCTION),
    ),
    RelationshipSchema(
        (NodeLabel.MODULE,),
        RelationshipType.EXPORTS_MODULE,
        (NodeLabel.MODULE_INTERFACE,),
    ),
    RelationshipSchema(
        (NodeLabel.MODULE,),
        RelationshipType.IMPLEMENTS_MODULE,
        (NodeLabel.MODULE_IMPLEMENTATION,),
    ),
    RelationshipSchema(
        (NodeLabel.CLASS, NodeLabel.INTERFACE, NodeLabel.FUNCTION),
        RelationshipType.INHERITS,
        # (H) ExternalModule: a positively-external base (typing.Protocol,
        # (H) js builtin.Error) keeps its edge by targeting the same external
        # (H) node the import pass mints, mirroring Module IMPORTS.
        (
            NodeLabel.CLASS,
            NodeLabel.INTERFACE,
            NodeLabel.FUNCTION,
            NodeLabel.EXTERNAL_MODULE,
        ),
    ),
    RelationshipSchema(
        (NodeLabel.CLASS, NodeLabel.ENUM),
        RelationshipType.IMPLEMENTS,
        (NodeLabel.INTERFACE, NodeLabel.EXTERNAL_MODULE),
    ),
    RelationshipSchema(
        # (H) A method-body anonymous-class override is registered as a Function node,
        # (H) so it can be the source of an OVERRIDES edge onto the base Method.
        (NodeLabel.METHOD, NodeLabel.FUNCTION),
        RelationshipType.OVERRIDES,
        (NodeLabel.METHOD,),
    ),
    RelationshipSchema(
        (NodeLabel.MODULE_IMPLEMENTATION,),
        RelationshipType.IMPLEMENTS,
        (NodeLabel.MODULE_INTERFACE,),
    ),
    RelationshipSchema(
        (NodeLabel.PROJECT,),
        RelationshipType.DEPENDS_ON_EXTERNAL,
        (NodeLabel.EXTERNAL_PACKAGE,),
    ),
    RelationshipSchema(
        (NodeLabel.MODULE, NodeLabel.FUNCTION, NodeLabel.METHOD),
        RelationshipType.CALLS,
        (NodeLabel.FUNCTION, NodeLabel.METHOD, NodeLabel.ENUM, NodeLabel.TYPE),
    ),
    RelationshipSchema(
        (NodeLabel.MODULE, NodeLabel.FUNCTION, NodeLabel.METHOD),
        RelationshipType.REFERENCES,
        (NodeLabel.FUNCTION, NodeLabel.METHOD, NodeLabel.CLASS),
    ),
    RelationshipSchema(
        (NodeLabel.MODULE, NodeLabel.FUNCTION, NodeLabel.METHOD),
        RelationshipType.INSTANTIATES,
        (NodeLabel.CLASS,),
    ),
    RelationshipSchema(
        (NodeLabel.MODULE, NodeLabel.FUNCTION, NodeLabel.METHOD),
        RelationshipType.READS_FROM,
        (NodeLabel.RESOURCE,),
    ),
    RelationshipSchema(
        (NodeLabel.MODULE, NodeLabel.FUNCTION, NodeLabel.METHOD),
        RelationshipType.WRITES_TO,
        (NodeLabel.RESOURCE,),
    ),
    RelationshipSchema(
        (NodeLabel.MODULE, NodeLabel.FUNCTION, NodeLabel.METHOD, NodeLabel.RESOURCE),
        RelationshipType.FLOWS_TO,
        (NodeLabel.MODULE, NodeLabel.FUNCTION, NodeLabel.METHOD, NodeLabel.RESOURCE),
    ),
)
