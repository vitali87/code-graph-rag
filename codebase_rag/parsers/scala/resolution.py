"""Scala's share of the name-based callee resolution: the class a type name
written in a Scala file names, the callee the imports in scope at a call
bind, and the method a parameterless selection (`c.size`) runs.

`CallResolver` owns what every language shares (among it a class's own and
inherited method lookup) and builds the `ScalaResolver`, which the call pass
(`ScalaCallResolution`) asks for a Scala callee ahead of the name-based
stages."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ... import constants as cs
from ...types_defs import FunctionRegistryTrieProtocol
from ..resolver_host import ResolverHost

if TYPE_CHECKING:
    from ..import_processor import ImportProcessor


class ScalaResolver:
    """Binds Scala callees through the imports the compiler sees where they
    are written.

    Built and owned by `CallResolver`. The registry and the import processor
    (whose `ScalaImporter` records the imports of each package block and the
    position they are in scope over) are the resolver's own, held by
    reference and read as they stand when a call resolves."""

    __slots__ = ("_host", "function_registry", "import_processor")

    def __init__(
        self,
        host: ResolverHost,
        function_registry: FunctionRegistryTrieProtocol,
        import_processor: ImportProcessor,
    ) -> None:
        self._host = host
        self.function_registry = function_registry
        self.import_processor = import_processor

    def resolve_scala_selection(
        self,
        receiver_type: str,
        member: str,
        module_qn: str,
        class_context: str | None,
        position: int,
    ) -> tuple[str, str] | None:
        """The method a parameterless Scala selection `receiver.member` runs.

        A `val` read and a parameterless call are spelled alike (`c.size`), so
        only a receiver whose class is KNOWN decides: `this`, a local typed by
        its binding, or an object named outright. `receiver_type` is the type
        name written for it, or `this`. The member must be a method of that
        class or an ancestor; no name-wide fallback answers for it.
        """
        class_qn = (
            class_context
            if receiver_type == cs.SCALA_THIS
            else self.scala_class_qn(receiver_type, module_qn, position)
        )
        if class_qn is None:
            return None
        result = self._host.try_resolve_method(class_qn, member)
        if result is None or result[0] != cs.NodeLabel.METHOD:
            return None
        return result

    def scala_class_qn(
        self, written: str, module_qn: str, position: int | None = None
    ) -> str | None:
        """The registered class a type name written in a Scala file names."""
        class_qn = self.import_processor.scala.scala_type_qn(
            module_qn, written, position
        )
        if (
            class_qn is None
            or self.function_registry.get(class_qn) != cs.NodeLabel.CLASS
        ):
            return None
        return class_qn

    def resolve_scala_callee(
        self, call_name: str, module_qn: str, position: int, constructing: bool
    ) -> tuple[bool, tuple[str, str] | None]:
        """A Scala callee named through the imports in scope where it is called.

        Decided (True) for a `new C(...)` the compiler's lookup reaches, and
        for any name a package block around the call imports: that block's
        import is the only binding visible there, so the file-level map, which
        a sibling block may share a name with, must not answer. Undecided
        otherwise.
        """
        if constructing and (
            class_qn := self.scala_class_qn(call_name, module_qn, position)
        ):
            return True, (cs.NodeLabel.CLASS, class_qn)
        head, _, rest = call_name.partition(cs.SEPARATOR_DOT)
        imported = self.import_processor.scala.scala_block_import(
            module_qn, head, position
        )
        if imported is None:
            return False, None
        target = f"{imported}{cs.SEPARATOR_DOT}{rest}" if rest else imported
        kind = self.function_registry.get(target)
        if kind is None or (constructing and kind != cs.NodeLabel.CLASS):
            return True, None
        return True, (kind, target)
