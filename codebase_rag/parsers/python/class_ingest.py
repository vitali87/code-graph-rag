"""Python's share of the class pass: the plain names a file's classes
reserve ahead of its functions, the other member names a class body binds a
method to and the names it binds to no method, a checker-only class, and
the method names an external stdlib base defines.

`ClassIngest` owns what every language shares (the scan of a file's class
captures, a class's node, members and relationships) and hands Python's
part to the `PythonClassIngest` it builds."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from tree_sitter import Node

from ... import constants as cs
from ...node_text import safe_decode_text
from ...types_defs import PropertyDict
from ..class_ingest import identity as id_
from ..enum_variants import is_python_enum
from ..language_definitions import LanguageClassIngest
from .ast_analyzer import under_type_checking
from .class_aliases import scan_class_body_aliases
from .utils import external_stdlib_base_method_names

if TYPE_CHECKING:
    from ...language_spec import LanguageSpec
    from ...types_defs import FunctionRegistryTrieProtocol
    from ..definition_facts import DefinitionFacts


class PythonClassIngest(LanguageClassIngest):
    """Reserves a Python class's plain name ahead of the function pass, and
    reads what a Python class body binds beside the shared class pass.

    Built and owned by `ClassIngest`. The registry and the definition facts
    are the pass's own, held by reference."""

    __slots__ = ("function_registry", "facts")

    def __init__(
        self, function_registry: FunctionRegistryTrieProtocol, facts: DefinitionFacts
    ) -> None:
        self.function_registry = function_registry
        self.facts = facts

    def reserve_class_qns(
        self,
        combined_captures: dict[str, list] | None,
        module_qn: str,
        lang_config: LanguageSpec,
        file_path: Path | None,
    ) -> None:
        # A file's functions and methods register before its classes, so a
        # `def Tool` shim written below `class Tool` took the plain name the
        # class is owed as the first definition (issue #2621). Only a class
        # sharing its name with a def can lose it, which spares every other
        # class a second identity pass (and a second truncated-name warning).
        reservations: dict[str, tuple[int, int]] = {}
        captures = combined_captures or {}
        def_names = {
            safe_decode_text(func_node.child_by_field_name(cs.FIELD_NAME))
            for func_node in captures.get(cs.CAPTURE_FUNCTION, [])
        }
        for class_node in captures.get(cs.CAPTURE_CLASS, []):
            name = safe_decode_text(class_node.child_by_field_name(cs.FIELD_NAME))
            if name is None or name not in def_names:
                continue
            identity = id_.resolve_class_identity(
                class_node,
                module_qn,
                cs.SupportedLanguage.PYTHON,
                lang_config,
                file_path,
            )
            if identity is None:
                continue
            class_qn = identity[0]
            start = (class_node.start_point[0] + 1, class_node.start_point[1])
            reservations[class_qn] = min(reservations.get(class_qn, start), start)
        self.function_registry.reserve_qns(reservations)

    @staticmethod
    def member_aliases(body_node: Node) -> dict[int, tuple[str, ...]]:
        # Method node id -> the other member names the class body binds it to
        # (`run = _plain`, issue #2620), so the method registers under them too.
        names: dict[int, list[str]] = {}
        for alias, methods in scan_class_body_aliases(body_node).members.items():
            for method in methods:
                names.setdefault(method.id, []).append(alias)
        return {method_id: tuple(aliases) for method_id, aliases in names.items()}

    @staticmethod
    def non_method_members(class_node: Node) -> list[str]:
        # The names the class body binds to a value that is no method (`run =
        # None`): they hide a base's `run` from attribute lookup (issue #2620).
        if (body := class_node.child_by_field_name(cs.FIELD_BODY)) is None:
            return []
        return sorted(scan_class_body_aliases(body).non_method)

    @staticmethod
    def record_type_props(
        class_node: Node,
        class_qn: str,
        class_name: str,
        label: cs.NodeLabel,
        class_props: PropertyDict,
    ) -> None:
        # A checker-only class types `self` in mixins whose calls resolve
        # to the real implementation, so no edge ever reaches it; only the
        # parse can tell dead code it is a declaration.
        if under_type_checking(class_node):
            class_props[cs.KEY_TYPE_CHECKING_ONLY] = True

    def is_enum(self, class_qn: str) -> bool:
        return is_python_enum(self.facts.class_inheritance.get(class_qn, ()))

    def external_override_names(self, class_qn: str) -> frozenset[str]:
        # Names defined by an EXTERNAL stdlib base of this class (click's
        # `class TextWrapper(textwrap.TextWrapper)`): a method matching one
        # overrides the base and is invoked by its machinery, so mark it for
        # the dead-code root property. Only unregistered (external) parents
        # contribute; first-party bases resolve via OVERRIDES edges.
        external_parents = [
            p
            for p in self.facts.class_inheritance.get(class_qn, [])
            if self.function_registry.get(p) is None
        ]
        if not external_parents:
            return frozenset()
        return external_stdlib_base_method_names(external_parents)
