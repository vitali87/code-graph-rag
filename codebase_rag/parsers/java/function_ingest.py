"""Java's share of the function pass: the anonymous-class override a
method-body anonymous class declares, which the function pass captures as a
function and the class pass links to its base type.

`FunctionIngest` owns what every language shares (the scan of a file's
function captures, a function's identity, node and DEFINES edge) and hands
Java's part to the `JavaFunctionIngest` it builds."""

from __future__ import annotations

from typing import TYPE_CHECKING

from tree_sitter import Node

from ... import constants as cs
from ..language_definitions import LanguageFunctionIngest

if TYPE_CHECKING:
    from ...language_spec import LanguageSpec
    from ..function_identity import FunctionResolution


def _anonymous_base_type(
    func_node: Node, class_node_types: frozenset[str]
) -> str | None:
    # A Java method declared inside a method-body anonymous class
    # (`createBoundField(){ return new BoundField(){ @Override write(){} } }`) is
    # captured here as a FUNCTION (the class-method pass skips method-nested defs),
    # so it needs its base-type override link too. Walk to the nearest enclosing
    # type: an anon `class_body` (parent object_creation) before any NAMED class
    # overrides that base; return the base type name (generics stripped).
    current = func_node.parent
    while current is not None:
        if current.type in class_node_types:
            return None
        if current.type == cs.TS_CLASS_BODY:
            parent = current.parent
            if parent is not None and parent.type == cs.TS_OBJECT_CREATION_EXPRESSION:
                type_node = parent.child_by_field_name(cs.FIELD_TYPE)
                if type_node is not None and type_node.text is not None:
                    return type_node.text.decode(cs.ENCODING_UTF8).split(
                        cs.CHAR_ANGLE_OPEN, 1
                    )[0]
            return None
        current = current.parent
    return None


class JavaFunctionIngest(LanguageFunctionIngest):
    """Records the anonymous-class overrides the function pass captures.

    Built and owned by `FunctionIngest`. The anonymous-override record is the
    pass's own, held by reference: `JavaClassIngest` records into it too and
    resolves it once every type is registered."""

    __slots__ = ("anon_overrides",)

    def __init__(self, anon_overrides: list[tuple[str, str, str, str]]) -> None:
        self.anon_overrides = anon_overrides

    def record_anonymous_override(
        self,
        func_node: Node,
        resolution: FunctionResolution,
        module_qn: str,
        lang_config: LanguageSpec,
    ) -> None:
        # A method-body anonymous-class override (`new Base(){ @Override m(){} }`
        # inside a method) is captured as a function here; record it so the deferred
        # pass emits an OVERRIDES edge to Base.m, keeping the dispatch-only override
        # live (field-initialiser anon overrides are recorded in the class-method
        # pass).
        base = _anonymous_base_type(func_node, frozenset(lang_config.class_node_types))
        if base and resolution.name:
            self.anon_overrides.append(
                (resolution.qualified_name, resolution.name, base, module_qn)
            )
