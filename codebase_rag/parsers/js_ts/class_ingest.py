"""JS/TS's share of the class pass: the class a base reaches through its
module's exports, or as a member of a namespace import.

`ClassIngest` owns what every language shares (the scan of a file's class
captures, a class's node, members and relationships, and the deferred
INHERITS/IMPLEMENTS resolution) and hands JS/TS's part to the
`JsTsClassIngest` it builds."""

from __future__ import annotations

from typing import TYPE_CHECKING

from tree_sitter import Node

from ... import constants as cs
from ...types_defs import DeferredInherit
from ..class_ingest import base_targets as bt
from ..class_ingest import parent_extraction as pe
from ..language_definitions import LanguageClassIngest
from . import utils as js_ts_utils

if TYPE_CHECKING:
    from ..definition_context import DefinitionContext


class JsTsClassIngest(LanguageClassIngest):
    """Binds a deferred JS/TS base the module system's exports say it names.

    Built and owned by `ClassIngest`. The registry, the module paths and the
    import processor are the pass's own, held by reference."""

    __slots__ = ("function_registry", "module_qn_to_file_path", "import_processor")

    # TypeScript keeps a type and a value of one name apart (issue #2520).
    separate_declaration_spaces = True

    def __init__(self, context: DefinitionContext) -> None:
        self.function_registry = context.function_registry
        self.module_qn_to_file_path = context.module_qn_to_file_path
        self.import_processor = context.import_processor

    def repeats_member_signature(
        self, method_node: Node, seen_signatures: set[str]
    ) -> bool:
        # JS/TS alone: Dart spells its ordinary members `method_signature`.
        return js_ts_utils.repeats_member_signature(method_node, seen_signatures)

    def externalized_base(self, raw_name: str) -> str | None:
        if raw_name in cs.JS_GLOBAL_CLASS_NAMES:
            return f"{cs.BUILTIN_PREFIX}{cs.SEPARATOR_DOT}{raw_name}"
        return None

    def reexport_target(self, entry: DeferredInherit) -> str | None:
        """The class a JS/TS base reaches through its module's exports.

        `export { Animal as AnimalBase }` publishes the class under a name
        nothing is registered under, so `extends AnimalBase` imported from
        that module names `base.AnimalBase` (issue #2894). The export table
        says which binding the name stands for, through any barrels that
        rename it again; only what a class can extend (a type, or in JS a
        constructor function) is a base at the end.
        """
        followed = self.import_processor.js_ts.follow_js_reexports(
            entry.parent_qn, self.module_qn_to_file_path, self.function_registry
        )
        # A subclass may take the base's declared name (hono's `Hono extends
        # HonoBase`); a chain ending on the child itself names no base.
        if followed != entry.child_qn and self.function_registry.get(
            followed
        ) in bt.base_target_kinds(entry.language):
            return followed
        return None

    def namespace_member(self, raw_name: str, entry: DeferredInherit) -> str | None:
        # `implements r.Plain` or `extends r.Base` after `import * as r from
        # './router'` (issue #2560): the head is an import binding, so the
        # member is declared in the module it names, which outranks the name
        # sweep below. Only a registered type answers. A head bound outside
        # the project (`React` from a default import is `react.default`)
        # registers nothing, so `React.Component` keeps the written name it is
        # externalized under, which React dead-code rooting matches on.
        candidate = pe.js_ts_namespace_member_qn(
            raw_name, self.import_processor.import_mapping.get(entry.module_qn, {})
        )
        if candidate is None:
            return None
        # `r` is often a barrel (`export { Router } from './router'`): the
        # member is what the barrel EXPORTS under that name, followed on to
        # its declaring module. A name the barrel only imports for its own
        # use is no member: tsc rejects `implements r.Router` then.
        member = self.import_processor.js_ts.follow_js_reexports(
            candidate, self.module_qn_to_file_path, self.function_registry
        )
        if (
            member == entry.child_qn
            or self.function_registry.get(member)
            not in bt.base_target_kinds(entry.language)
            # The module's own declaration is a member only when exported.
            or (
                member == candidate and self.function_registry.is_module_private(member)
            )
        ):
            return None
        return member
