"""The hooks the call pass reaches a language's call behaviour through.

`CallProcessor` owns the language-neutral pass (the scan of a caller's call
nodes, the site and resolution every edge carries, the name-based resolver
and what languages share) and looks a file's language up to the
`LanguageCalls` that language's package builds: the
`<Language>CallResolution` in `parsers/<language>/calls.py`, or this base
class for a language that adds nothing. A hook is a point where the pass asks
the language, named for what it asks, and by default answers as if the
language added nothing there; a capability is a rule several languages
share, read off the class."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

from tree_sitter import Node

from .. import constants as cs
from ..types_defs import LanguageQueries, ScalaBinding

if TYPE_CHECKING:
    from .php import class_names as php_class_names


class LanguageCalls:
    """A language's share of the call pass, as the answers the pass asks for.

    Subclassed by each language's call collaborator; the base itself serves
    a language that adds nothing to the pass."""

    __slots__ = ()

    # Interprocedural callable-parameter flow: a callback passed to a
    # first-party function whose parameter is invoked (directly or in a
    # nested closure) is traced to the concrete callback.
    flows_callables: ClassVar[bool] = False
    # A function handed over as an argument is a first-class value whose
    # target stays reachable; every flow language references it, and a
    # language may take this half without the flow.
    references_arguments: ClassVar[bool] = False
    # A file with no call node still gets the caller passes: the language's
    # callers make edges without one, which the class pass writes.
    scans_callers_without_calls: ClassVar[bool] = False
    # An anonymous function gets no caller pass, so a call nested only in
    # anonymous scopes is the module's rather than nobody's.
    anonymous_calls_reach_module: ClassVar[bool] = False
    # A pre-pass over every file records the functions a construction binds
    # to a class's callable fields (`collect_callable_field_bindings`).
    binds_callable_fields: ClassVar[bool] = False
    # A pre-pass over every file records the members a symbol key installs
    # (`collect_symbol_bindings`).
    keys_members_by_symbol: ClassVar[bool] = False

    def collect_callable_field_bindings(
        self, root_node: Node, module_qn: str, call_nodes: list[Node]
    ) -> None:
        """Record the functions a file's constructions bind to callable
        fields, resolved once every file's constructors are known."""

    def collect_symbol_bindings(self, root_node: Node, module_qn: str) -> None:
        """Record the symbol constants a file declares and the members it
        installs under them, resolved once every file is swept."""

    def ingest_module_references(
        self,
        root_node: Node,
        module_qn: str,
        language: cs.SupportedLanguage,
        queries: Mapping[cs.SupportedLanguage, LanguageQueries],
        combined_captures: dict[str, list[Node]],
        sorted_func_nodes: list[Node] | None,
    ) -> None:
        """Write the edges a file makes at module scope without a call node,
        before the pass's early return for a file with no call: a function
        stored as a value, a construction that runs a constructor."""


@dataclass(slots=True)
class CallScan:
    # Per-invocation state _ingest_function_calls hands every call node it
    # scans: the caller, its language's call strategy and the language flags
    # not yet read off it, the bound resolvers, and the lazily-built alias
    # maps shared across the caller's call nodes.
    caller_node: Node
    caller_qn: str
    caller_spec: tuple[str, str, str]
    module_qn: str
    language: cs.SupportedLanguage
    queries: Mapping[cs.SupportedLanguage, LanguageQueries]
    calls: LanguageCalls
    class_context: str | None
    call_name_cache: dict[int, str | None] | None
    local_var_types: dict[str, str] | None
    span_bindings: list[tuple[int, int, str, str]]
    caller_params: frozenset[str]
    is_java: bool
    is_csharp: bool
    is_js_ts: bool
    is_cpp: bool
    is_python: bool
    cpp_template_params: frozenset[str]
    resolve_func: Callable[..., tuple[str, str] | None]
    resolve_builtin: Callable[[str], tuple[str, str] | None] | None
    resolve_cpp_op: Callable[[str, str], tuple[str, str] | None] | None
    ensure_rel: Callable[..., None]
    arg_ref_rel: cs.RelationshipType
    scala_bindings: dict[str, list[ScalaBinding]] | None = None
    alias_map: dict[str, str] | None = None
    factory_aliases: dict[str, str] | None = None
    cpp_local_aliases: dict[str, list[tuple[str, int, int]]] | None = None
    php_scope_classes: php_class_names.ScopeCache | None = None
    cpp_indirection: dict[str, int] | None = None
