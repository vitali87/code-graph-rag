"""Java's share of the name-based callee resolution: the constructors a
`new Foo(...)` runs, which every language that names its constructors after
the class (Java, C#, C++) reaches the same way.

A Java method call itself is typed by the Java engine
(`JavaTypeInferenceEngine`). The `JavaResolver` that `CallResolver` builds
bridges the call pass to it and holds this lookup beside the bridges."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from loguru import logger
from tree_sitter import Node

from ... import constants as cs
from ... import logs as ls
from ...types_defs import FunctionRegistryTrieProtocol
from ...utils import qn_markers

if TYPE_CHECKING:
    from ..python.resolution import PythonResolver
    from ..type_inference import TypeInferenceEngine


class JavaResolverHost(Protocol):
    """What Java resolution reads on the `CallResolver` that builds it: the
    Python resolver, whose Protocol-stub redirect a callee the Java engine
    binds passes through, as one the name-based stages bind does."""

    python: PythonResolver


class JavaResolver:
    """Binds Java calls through the Java engine and constructions to the
    constructors they run.

    Built and owned by `CallResolver`. The registry and the type inference
    engine (whose Java engine types a Java call) are the resolver's own,
    held by reference and read as they stand when a call resolves."""

    __slots__ = ("_host", "function_registry", "type_inference")

    def __init__(
        self,
        host: JavaResolverHost,
        function_registry: FunctionRegistryTrieProtocol,
        type_inference: TypeInferenceEngine,
    ) -> None:
        self._host = host
        self.function_registry = function_registry
        self.type_inference = type_inference

    def reset_resolution_memos(self) -> None:
        # The Java class headers (issue #2470) were read off the trees the
        # last pass saw; a re-parsed superclass must be walked again.
        if (java := self.type_inference._java_type_inference) is not None:
            java.reset_resolution_memos()

    def java_constructor_targets(self, class_qn: str) -> set[tuple[str, str]]:
        # A Java constructor is registered as a method directly under its class whose
        # simple name equals the class's simple name (`Foo.Foo(int)`). `new Foo(...)`
        # resolves to the CLASS, so redirect a CALLS edge to each declared constructor
        # (all overloads); argument-type overload selection is not attempted, which is
        # unnecessary for reachability and never fabricates a call to a
        # non-constructor. Only constructors DIRECTLY on the class match (a nested
        # class's constructor has an extra qn segment and is excluded).
        # A duplicate-suffixed class (`Box@8` for `class Box<T>` beside `class
        # Box`) declares its constructor under its natural name, so the
        # marker is not part of the name to match (issue #2007), and nor is a
        # generic twin's CLR arity (`PB`1` declares `PB(...)`, issue #2579).
        simple = qn_markers.strip_arity_marker(
            qn_markers.strip_dup_marker(class_qn.rsplit(cs.SEPARATOR_DOT, 1)[-1])
        )
        targets: set[tuple[str, str]] = set()
        for qn, node_type in self.function_registry.find_with_prefix(class_qn):
            head = qn.split(cs.CHAR_PAREN_OPEN, 1)[0]
            parent, dot, mname = head.rpartition(cs.SEPARATOR_DOT)
            if dot and parent == class_qn and mname == simple:
                targets.add((node_type, qn))
        return targets

    def resolve_java_method_call(
        self,
        call_node: Node,
        module_qn: str,
        local_var_types: dict[str, str] | None,
        caller_qn: str | None = None,
    ) -> tuple[str, str] | None:
        java_engine = self.type_inference.java_type_inference

        result = self._host.python.redirect_protocol_method(
            java_engine.resolve_java_method_call(
                call_node, local_var_types, module_qn, caller_qn
            )
        )

        if result:
            call_text = (
                call_node.text.decode(cs.ENCODING_UTF8)
                if call_node.text
                else cs.TEXT_UNKNOWN
            )
            logger.debug(
                ls.CALL_JAVA_RESOLVED, call_text=call_text, method_qn=result[1]
            )

        return result

    def resolve_java_call_site(
        self,
        call_node: Node,
        module_qn: str,
    ) -> tuple[str, str] | None:
        return self.type_inference.java_type_inference.resolve_java_call_site(
            call_node, module_qn
        )
