"""SQL's share of the call pass: the name a routine's body calls are
attributed under, and the routines a file's DDL runs without an
`invocation` (a trigger, aggregate, cast, type or handler clause names one),
read from the source as written.

`CallProcessor` owns what every language shares (the scan of a caller's call
nodes, the site and resolution each edge carries) and the link a SQL routine
reference takes, and hands SQL's part to the `SqlCallResolution` it
builds."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Protocol

from tree_sitter import Node

from ... import constants as cs
from ...language_spec import sql_object_reference_name
from ...types_defs import LanguageQueries, PropertyDict
from ..definition_facts import DefinitionFacts
from ..edge_site import EdgeSiteHost, host_site_scoped
from ..language_calls import CallName, CallScan, LanguageCalls
from .ddl import ddl_routine_references


class SqlCallHost(EdgeSiteHost, Protocol):
    """What SQL's call passes read and call on the call pass that builds them:
    the file being walked, and the link a routine reference takes to every
    routine its normalized name can run."""

    _call_file: Path | None

    def _link_sql_routine(
        self,
        module_qn: str,
        caller_spec: tuple[str, str, str],
        call_name: str,
        site: PropertyDict | None = None,
    ) -> None: ...


class SqlCallResolution(LanguageCalls):
    """Names a routine as the definition pass does and writes the routine
    references a SQL file's DDL makes.

    Built and owned by `CallProcessor`. The definition facts are the pass's
    own, held by reference: the definition pass records each SQL file's
    source there as it parses it."""

    __slots__ = ("_host", "_facts")

    def __init__(self, host: SqlCallHost, facts: DefinitionFacts) -> None:
        self._host = host
        self._facts = facts

    def caller_name(self, func_node: Node, module_qn: str) -> str | None:
        # `create_function` has no `name` field either; without its name
        # the routine got no caller pass and its body's calls went to the
        # module (issue #2449).
        return (
            super().caller_name(func_node, module_qn)
            or sql_object_reference_name(func_node)
            or None
        )

    def call_name(self, call_node: Node) -> CallName:
        # A SQL `invocation` names its routine through an unnamed
        # `object_reference` child (no `function` or `name` field), so every
        # SQL call site fell through nameless and was dropped (issue #2449).
        # Normalized like the definition, so `TAX(x)` reaches `tax`.
        return CallName(True, sql_object_reference_name(call_node))

    def ingest_own_call(
        self,
        ctx: CallScan,
        call_node: Node,
        call_name: str,
        call_var_types: dict[str, str] | None,
    ) -> bool:
        self._host._link_sql_routine(ctx.module_qn, ctx.caller_spec, call_name)
        return True

    def ingest_module_references(
        self,
        root_node: Node,
        module_qn: str,
        language: cs.SupportedLanguage,
        queries: Mapping[cs.SupportedLanguage, LanguageQueries],
        combined_captures: dict[str, list[Node]],
        sorted_func_nodes: list[Node] | None,
    ) -> None:
        # A trigger, aggregate, cast, type or handler clause runs the
        # routine it names without an `invocation`; a schema file often
        # holds no call at all (issue #3181).
        self._ingest_ddl_references(root_node, module_qn)

    @host_site_scoped
    def _ingest_ddl_references(self, root_node: Node, module_qn: str) -> None:
        # The module runs the DDL, so it is the caller, as for a CHECK
        # constraint's `invocation`. The site is the routine's name in the
        # clause; no node spans it when the statement did not parse.
        text, row, col = self._source_as_written(root_node)
        if not text:
            return
        host = self._host
        host._site_node = None
        module_spec = (cs.NodeLabel.MODULE, cs.KEY_QUALIFIED_NAME, module_qn)
        for reference in ddl_routine_references(text, row, col):
            host._resolution = cs.EdgeResolution.EXACT
            host._link_sql_routine(
                module_qn, module_spec, reference.name, reference.site
            )

    def _source_as_written(self, root_node: Node) -> tuple[bytes | None, int, int]:
        # The text to scan and the point it starts at. The tree's own text can
        # be the statement-isolated re-parse's copy, which blanks every
        # statement the grammar cannot read on its own (#3180), and most DDL
        # that names a routine is such a statement. The bytes that tree was
        # parsed from hold them, whole: a blanked statement that opens or
        # closes the file lies outside the root's span.
        path = self._host._call_file
        source = self._facts.sql_sources.get(path) if path is not None else None
        if source is None:
            row, col = root_node.start_point
            return root_node.text, row, col
        return source, 0, 0
