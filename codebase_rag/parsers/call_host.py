"""What several languages' call collaborators reach on the call pass that
builds them, declared once for their host Protocols to take.

`CallProcessor` holds the collaborators every language shares as its own
attributes, and a language's collaborator calls their methods directly: the
reference walkers (`references`) and the dispatch-table walk (`tables`),
which still give a function used as a value rather than called (stored by
an assignment, handed back by a return, held in a collection literal or a
parameter default) its edge; the callback edges (`callbacks`) a function
handed over as a value writes; and the binding (`binding`) whose ingest of
a bound call writes the edges every resolved call writes. The processor
also notes how surely a resolver answer binds the call node being scanned,
the verdict that node's edge carries. A collaborator's host Protocol takes
the members it uses from here rather than spelling them out again."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from .call_binding import CallBinding
    from .call_callbacks import CallbackEdges
    from .call_references import ReferenceWalkers
    from .call_tables import TableReferences


class VerdictNotingHost(Protocol):
    """The note of how surely a resolver answer binds the call node being
    scanned."""

    def _noted[R: tuple[str, str] | None](self, resolved: R) -> R: ...


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
