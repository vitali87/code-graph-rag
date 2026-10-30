"""The edge site the call pass's collaborators write through their host.

`CallProcessor` stamps every edge it emits with the node that produced it
and the confidence of the binding (issues #1522, #1526). The pass's
collaborators, shared and per-language, set both on the processor that owns
them as they walk, and `host_site_scoped` puts them back when a pass
returns, so a nested pass cannot leak its last node into the edges its
caller emits next; the edges themselves they write through the processor's
own writer (`emit_rel`)."""

from collections.abc import Callable
from functools import wraps
from typing import Concatenate, Protocol

from tree_sitter import Node

from ..types_defs import PropertyDict


class EdgeSiteHost(Protocol):
    """The node whose span the next edge records, and the resolution it
    carries."""

    site_node: Node | None
    resolution: str


class EdgeWriterHost(EdgeSiteHost, Protocol):
    """The writer every CALLS, REFERENCES and INSTANTIATES edge of the pass
    goes through, which stamps it with the edge site."""

    def emit_rel(
        self,
        from_spec: tuple[str, str, str],
        rel_type: str,
        to_spec: tuple[str, str, str],
        properties: PropertyDict | None = None,
    ) -> None: ...


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
        prev = host.site_node
        prev_resolution = host.resolution
        try:
            return fn(owner, *args, **kwargs)
        finally:
            host.site_node = prev
            host.resolution = prev_resolution

    return wrapper
