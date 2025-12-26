from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

from rich.console import Console

from .constants import SupportedLanguage
from .types_defs import MCPHandlerType, MCPInputSchemaDict, PropertyValue

if TYPE_CHECKING:
    from tree_sitter import Node


@dataclass
class SessionState:
    confirm_edits: bool = True
    log_file: Path | None = None
    cancelled: bool = False

    def reset_cancelled(self) -> None:
        self.cancelled = False


def _default_console() -> Console:
    return Console(width=None, force_terminal=True)


@dataclass
class AppContext:
    session: SessionState = field(default_factory=SessionState)
    console: Console = field(default_factory=_default_console)


@dataclass
class GraphNode:
    node_id: int
    labels: list[str]
    properties: dict[str, PropertyValue]


@dataclass
class GraphRelationship:
    from_id: int
    to_id: int
    type: str
    properties: dict[str, PropertyValue]


class FQNSpec(NamedTuple):
    scope_node_types: frozenset[str]
    function_node_types: frozenset[str]
    get_name: Callable[["Node"], str | None]
    file_to_module_parts: Callable[[Path, Path], list[str]]


@dataclass(frozen=True)
class LanguageSpec:
    language: SupportedLanguage | str
    file_extensions: tuple[str, ...]
    function_node_types: tuple[str, ...]
    class_node_types: tuple[str, ...]
    module_node_types: tuple[str, ...]
    call_node_types: tuple[str, ...] = ()
    import_node_types: tuple[str, ...] = ()
    import_from_node_types: tuple[str, ...] = ()
    name_field: str = "name"
    body_field: str = "body"
    package_indicators: tuple[str, ...] = ()
    function_query: str | None = None
    class_query: str | None = None
    call_query: str | None = None


@dataclass
class Dependency:
    name: str
    spec: str
    properties: dict[str, str] = field(default_factory=dict)


@dataclass
class MethodModifiersAndAnnotations:
    modifiers: list[str] = field(default_factory=list)
    annotations: list[str] = field(default_factory=list)


@dataclass
class ToolMetadata:
    name: str
    description: str
    input_schema: MCPInputSchemaDict
    handler: MCPHandlerType
    returns_json: bool
