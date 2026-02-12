from __future__ import annotations

from collections import defaultdict
from collections.abc import Awaitable, Callable, ItemsView, KeysView, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple, Protocol, TypedDict

from prompt_toolkit.styles import Style

from .constants import NodeLabel, RelationshipType, SupportedLanguage

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


class ASTCacheProtocol(Protocol):
    def __setitem__(self, key: Path, value: tuple[Node, SupportedLanguage]) -> None: ...

    def __getitem__(self, key: Path) -> tuple[Node, SupportedLanguage]: ...
    def __delitem__(self, key: Path) -> None: ...
    def __contains__(self, key: Path) -> bool: ...
    def items(self) -> ItemsView[Path, tuple[Node, SupportedLanguage]]: ...


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

    @property
    def allowed_roots(self) -> frozenset[Path] | None: ...


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


ORANGE_STYLE = Style.from_dict({"": "#ff8c00"})

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
    analyze_document: str
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
    default: str


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
    relative_path: str | None
    project_name: str | None
    line_start: int
    line_end: int
    docstring: str | None
    found: bool
    error_message: str | None
    error: str


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


class RelationshipSchema(NamedTuple):
    sources: tuple[NodeLabel, ...]
    rel_type: RelationshipType
    targets: tuple[NodeLabel, ...]


NODE_SCHEMAS: tuple[NodeSchema, ...] = (
    NodeSchema(
        NodeLabel.PROJECT, "{name: string, absolute_path: string, project_name: string}"
    ),
    NodeSchema(
        NodeLabel.PACKAGE,
        "{qualified_name: string, name: string, path: string, absolute_path: string, project_name: string}",
    ),
    NodeSchema(
        NodeLabel.FOLDER,
        "{path: string, name: string, absolute_path: string, project_name: string}",
    ),
    NodeSchema(
        NodeLabel.FILE,
        "{path: string, name: string, extension: string, absolute_path: string, project_name: string}",
    ),
    NodeSchema(
        NodeLabel.MODULE,
        "{qualified_name: string, name: string, path: string, absolute_path: string, project_name: string}",
    ),
    NodeSchema(
        NodeLabel.CLASS,
        "{qualified_name: string, name: string, path: string, absolute_path: string, project_name: string, decorators: list[string]}",
    ),
    NodeSchema(
        NodeLabel.FUNCTION,
        "{qualified_name: string, name: string, path: string, absolute_path: string, project_name: string, decorators: list[string]}",
    ),
    NodeSchema(
        NodeLabel.METHOD,
        "{qualified_name: string, name: string, path: string, absolute_path: string, project_name: string, decorators: list[string]}",
    ),
    NodeSchema(
        NodeLabel.INTERFACE,
        "{qualified_name: string, name: string, path: string, absolute_path: string, project_name: string}",
    ),
    NodeSchema(
        NodeLabel.ENUM,
        "{qualified_name: string, name: string, path: string, absolute_path: string, project_name: string}",
    ),
    NodeSchema(
        NodeLabel.TYPE,
        "{qualified_name: string, name: string, path: string, absolute_path: string, project_name: string}",
    ),
    NodeSchema(
        NodeLabel.UNION,
        "{qualified_name: string, name: string, path: string, absolute_path: string, project_name: string}",
    ),
    NodeSchema(
        NodeLabel.MODULE_INTERFACE,
        "{qualified_name: string, name: string, path: string, absolute_path: string, project_name: string}",
    ),
    NodeSchema(
        NodeLabel.MODULE_IMPLEMENTATION,
        "{qualified_name: string, name: string, path: string, absolute_path: string, project_name: string, implements_module: string}",
    ),
    NodeSchema(NodeLabel.EXTERNAL_PACKAGE, "{name: string, version_spec: string}"),
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
        (NodeLabel.MODULE,),
        RelationshipType.DEFINES,
        (NodeLabel.CLASS, NodeLabel.FUNCTION),
    ),
    RelationshipSchema(
        (NodeLabel.CLASS,),
        RelationshipType.DEFINES_METHOD,
        (NodeLabel.METHOD,),
    ),
    RelationshipSchema(
        (NodeLabel.MODULE,),
        RelationshipType.IMPORTS,
        (NodeLabel.MODULE,),
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
        (NodeLabel.CLASS,),
        RelationshipType.INHERITS,
        (NodeLabel.CLASS,),
    ),
    RelationshipSchema(
        (NodeLabel.CLASS,),
        RelationshipType.IMPLEMENTS,
        (NodeLabel.INTERFACE,),
    ),
    RelationshipSchema(
        (NodeLabel.METHOD,),
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
        (NodeLabel.FUNCTION, NodeLabel.METHOD),
        RelationshipType.CALLS,
        (NodeLabel.FUNCTION, NodeLabel.METHOD),
    ),
)


class PathInfo(TypedDict):
    relative_path: str
    absolute_path: str
