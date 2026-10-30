"""What the call pass's collaborators reach on the `CallProcessor` that
builds them, declared once for their host Protocols to take.

The processor looks a file's language up to its call strategy
(`calls_for`), knows where the definition pass recorded each function node
(`recorded_caller`), and notes how surely a resolver answer binds the call
node being scanned (`noted`), the verdict that node's edges carry. It holds
the collaborators every language shares as its own attributes, and a
language's collaborator calls their methods directly: the reference
walkers (`references`) and the dispatch-table walk (`tables`), which still
give a function used as a value rather than called (stored by an
assignment, handed back by a return, held in a collection literal or a
parameter default) its edge; the callback edges (`callbacks`) a function
handed over as a value writes; and the binding (`binding`) whose ingest of
a bound call writes the edges every resolved call writes. A collaborator's
host Protocol takes the members it uses from here, and the edge writer from
`edge_site`, rather than spelling them out again."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from tree_sitter import Node

from ..types_defs import FunctionLocation
from .language_calls import LanguageCalls

if TYPE_CHECKING:
    from .call_binding import CallBinding
    from .call_callbacks import CallbackEdges
    from .call_references import ReferenceWalkers
    from .call_tables import TableReferences


class CallStrategyHost(Protocol):
    """The call strategy of the language a node is written in; the base,
    which adds nothing, for a language with none or no language at all."""

    def calls_for(self, language: str | None) -> LanguageCalls: ...


class RecordedCallerHost(Protocol):
    """Where the definition pass recorded a function node, for a caller the
    registry still holds."""

    def recorded_caller(
        self, func_node: Node, module_qn: str
    ) -> FunctionLocation | None: ...


class VerdictHost(Protocol):
    """How surely the call node being scanned binds its callee (issue
    #1526), and the note that sets it from a resolver answer. Reset per
    call node and read when its edges are emitted; the judgments that follow
    a resolve raise or lower it."""

    verdict: str

    def noted[R: tuple[str, str] | None](self, resolved: R) -> R: ...


class ValueReferenceHost(Protocol):
    """The walkers for a function a caller's scope holds as a value: the
    reference walkers, and the walk over the scope's literals."""

    @property
    def references(self) -> ReferenceWalkers: ...

    @property
    def tables(self) -> TableReferences: ...


class CallbackEdgeHost(Protocol):
    """The callback edges, which resolve a function handed over as a value
    and write the edge that keeps it reachable."""

    @property
    def callbacks(self) -> CallbackEdges: ...


class ResolvedCallHost(Protocol):
    """The binding of the call nodes a caller's scan visits, whose ingest of a
    call bound to its callee writes the edges every resolved call writes."""

    @property
    def binding(self) -> CallBinding: ...
