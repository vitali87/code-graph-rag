"""What several languages' call collaborators reach on the call pass that
builds them, declared once for their host Protocols to take.

`CallProcessor` owns the reference walkers every language shares: a function
used as a value rather than called (stored by an assignment, handed back by
a return, held in a collection literal or a parameter default) still gets an
edge, and each walk stops at the nested scopes its language's spec names. It
also notes how surely a resolver answer binds the call node being scanned,
the verdict that node's edge carries. A collaborator's host Protocol takes
the members it uses from here rather than spelling them out again."""

from collections.abc import Mapping
from typing import Protocol

from tree_sitter import Node

from .. import constants as cs
from ..language_spec import LanguageSpec
from ..types_defs import LanguageQueries


class VerdictNotingHost(Protocol):
    """The note of how surely a resolver answer binds the call node being
    scanned."""

    def _noted[R: tuple[str, str] | None](self, resolved: R) -> R: ...


class ValueReferenceHost(Protocol):
    """The walkers for a function a caller stores by assignment or hands back
    by a return, and the scope boundaries they stop at."""

    @staticmethod
    def _flow_scope_boundaries(lang_config: LanguageSpec) -> frozenset[str]: ...

    def _ingest_assignment_function_references(
        self,
        caller_node: Node,
        caller_spec: tuple[str, str, str],
        module_qn: str,
        local_var_types: dict[str, str] | None,
        class_context: str | None,
        boundary_types: frozenset[str],
        caller_qn: str | None = None,
        language: cs.SupportedLanguage | None = None,
    ) -> None: ...

    def _ingest_returned_function_references(
        self,
        caller_node: Node,
        caller_spec: tuple[str, str, str],
        module_qn: str,
        local_var_types: dict[str, str] | None,
        class_context: str | None,
        boundary_types: frozenset[str],
        caller_qn: str | None = None,
        language: cs.SupportedLanguage | None = None,
    ) -> None: ...


class PyJsValueReferenceHost(ValueReferenceHost, Protocol):
    """The walkers Python and JS/TS add: a function held in a collection
    literal or a parameter default, and a module-level (or class-level)
    dispatch table."""

    def _ingest_py_js_module_references(
        self,
        root_node: Node,
        module_qn: str,
        language: cs.SupportedLanguage,
        queries: Mapping[cs.SupportedLanguage, LanguageQueries],
        class_field_assignments: bool = False,
    ) -> None: ...

    def _ingest_default_param_references(
        self,
        caller_node: Node,
        caller_spec: tuple[str, str, str],
        module_qn: str,
        local_var_types: dict[str, str] | None,
        class_context: str | None,
        caller_qn: str | None,
    ) -> None: ...

    def _ingest_collection_function_references(
        self,
        caller_node: Node,
        caller_spec: tuple[str, str, str],
        module_qn: str,
        local_var_types: dict[str, str] | None,
        class_context: str | None,
        boundary_types: frozenset[str],
        language: cs.SupportedLanguage | None = None,
    ) -> None: ...
