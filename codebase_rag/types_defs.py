from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, NamedTuple, TypedDict

from .constants import NodeLabel, RelationshipType, SupportedLanguage

if TYPE_CHECKING:
    from tree_sitter import Language, Parser, Query

    from .models import LanguageConfig

type LanguageLoader = Callable[[], "Language"] | None

PropertyValue = str | int | float | bool | list[str] | None
PropertyDict = dict[str, PropertyValue]

type ResultScalar = str | int | float | bool | None
type ResultValue = ResultScalar | list[ResultScalar] | dict[str, ResultScalar]
type ResultRow = dict[str, ResultValue]


class NodeBatchRow(TypedDict):
    id: PropertyValue
    props: PropertyDict


class RelBatchRow(TypedDict):
    from_val: PropertyValue
    to_val: PropertyValue
    props: PropertyDict


BatchParams = NodeBatchRow | RelBatchRow | PropertyDict

type SimpleName = str
type QualifiedName = str
type SimpleNameLookup = defaultdict[SimpleName, set[QualifiedName]]


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


class CancelledResult(NamedTuple):
    cancelled: bool


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


class PyInstallerPackage(TypedDict, total=False):
    name: str
    collect_all: bool
    collect_data: bool
    hidden_import: str


@dataclass
class RawToolArgs:
    file_path: str = ""
    target_code: str = ""
    replacement_code: str = ""
    content: str = ""
    command: str = ""


ToolArgs = ReplaceCodeArgs | CreateFileArgs | ShellCommandArgs


class LanguageQueries(TypedDict):
    functions: "Query | None"
    classes: "Query | None"
    calls: "Query | None"
    imports: "Query | None"
    locals: "Query | None"
    config: "LanguageConfig"
    language: "Language"
    parser: "Parser"


class NodeSchema(NamedTuple):
    label: NodeLabel
    properties: str


class RelationshipSchema(NamedTuple):
    sources: tuple[NodeLabel, ...]
    rel_type: RelationshipType
    targets: tuple[NodeLabel, ...]


NODE_SCHEMAS: tuple[NodeSchema, ...] = (
    NodeSchema(NodeLabel.PROJECT, "{name: string}"),
    NodeSchema(
        NodeLabel.PACKAGE, "{qualified_name: string, name: string, path: string}"
    ),
    NodeSchema(NodeLabel.FOLDER, "{path: string, name: string}"),
    NodeSchema(NodeLabel.FILE, "{path: string, name: string, extension: string}"),
    NodeSchema(
        NodeLabel.MODULE, "{qualified_name: string, name: string, path: string}"
    ),
    NodeSchema(
        NodeLabel.CLASS,
        "{qualified_name: string, name: string, decorators: list[string]}",
    ),
    NodeSchema(
        NodeLabel.FUNCTION,
        "{qualified_name: string, name: string, decorators: list[string]}",
    ),
    NodeSchema(
        NodeLabel.METHOD,
        "{qualified_name: string, name: string, decorators: list[string]}",
    ),
    NodeSchema(NodeLabel.INTERFACE, "{qualified_name: string, name: string}"),
    NodeSchema(NodeLabel.ENUM, "{qualified_name: string, name: string}"),
    NodeSchema(NodeLabel.TYPE, "{qualified_name: string, name: string}"),
    NodeSchema(NodeLabel.UNION, "{qualified_name: string, name: string}"),
    NodeSchema(
        NodeLabel.MODULE_INTERFACE,
        "{qualified_name: string, name: string, path: string}",
    ),
    NodeSchema(
        NodeLabel.MODULE_IMPLEMENTATION,
        "{qualified_name: string, name: string, path: string, implements_module: string}",
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
