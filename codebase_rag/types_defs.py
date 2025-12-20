from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, NamedTuple, TypedDict

from .constants import SupportedLanguage

if TYPE_CHECKING:
    from tree_sitter import Language, Parser, Query

    from .models import LanguageConfig

type LanguageLoader = Callable[[], "Language"] | None

PropertyValue = str | int | float | bool | None

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
    nodes: list[NodeData]
    relationships: list[RelationshipData]
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
    functions: "Query | None"
    classes: "Query | None"
    calls: "Query | None"
    imports: "Query | None"
    locals: "Query | None"
    config: "LanguageConfig"
    language: "Language"
    parser: "Parser"
