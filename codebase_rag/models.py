from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

from .types_defs import PropertyValue

if TYPE_CHECKING:
    from tree_sitter import Node


class AgentLoopConfig(NamedTuple):
    status_message: str
    cancelled_log: str
    approval_prompt: str
    denial_default: str
    panel_title: str


@dataclass
class SessionState:
    confirm_edits: bool = True
    log_file: Path | None = None
    cancelled: bool = False

    def reset_cancelled(self) -> None:
        self.cancelled = False


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


class FQNConfig(NamedTuple):
    scope_node_types: frozenset[str]
    function_node_types: frozenset[str]
    get_name: Callable[["Node"], str | None]
    file_to_module_parts: Callable[[Path, Path], list[str]]


@dataclass(frozen=True)
class LanguageConfig:
    language: str
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
