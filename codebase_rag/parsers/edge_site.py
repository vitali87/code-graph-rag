"""The edge site a language's call collaborator writes through its host.

`CallProcessor` stamps every edge it emits with the node that produced it
and the confidence of the binding (issues #1522, #1526): a pass sets both as
it walks, and `_site_scoped` puts them back when the pass returns, so a
nested pass cannot leak its last node into the edges its caller emits next.
A language's call collaborator sets the same two on the processor that owns
it, and `host_site_scoped` does that restoring for the collaborator's
passes."""

from collections.abc import Callable
from functools import wraps
from typing import Concatenate, Protocol

from tree_sitter import Node


class EdgeSiteHost(Protocol):
    """The node whose span the next edge records, and the resolution it
    carries."""

    _site_node: Node | None
    _resolution: str


class EdgeSiteOwner(Protocol):
    @property
    def _host(self) -> EdgeSiteHost: ...


def host_site_scoped[O: EdgeSiteOwner, **P, R](
    fn: Callable[Concatenate[O, P], R],
) -> Callable[Concatenate[O, P], R]:
    """Restore the host's edge site and resolution when the pass returns."""

    @wraps(fn)
    def wrapper(owner: O, /, *args: P.args, **kwargs: P.kwargs) -> R:
        host = owner._host
        prev = host._site_node
        prev_resolution = host._resolution
        try:
            return fn(owner, *args, **kwargs)
        finally:
            host._site_node = prev
            host._resolution = prev_resolution

    return wrapper
