"""The qualified name of a Resource node an access or a flow writes."""

from __future__ import annotations

from urllib.parse import urlparse

from .constants import RESOURCE_PROJECT_IDENTITY, RESOURCE_QN_FORMAT, ResourceKind


def resource_qn(kind: ResourceKind, identity: str, project: str | None) -> str:
    """`resource::<kind>::<identity>`, scoped to `project` where only it is
    reached.

    A rootful relative URL (`/carts/7`) is a same-origin request: it reaches
    the issuing project's own backend. Keyed by the URL alone, every project
    requesting `/carts/7` shared one node, and endpoint linking took each of
    them as a caller of every project's `/carts/:id` (issue #3190). So its
    node is scoped to the project, as an ENDPOINT's already is; the node's
    name stays the URL. A URL naming its host (`http://…`, `//cdn…`) is one
    shared service and stays shared.
    """
    if project and kind == ResourceKind.NETWORK and _is_rootful(identity):
        identity = RESOURCE_PROJECT_IDENTITY.format(project=project, identity=identity)
    return RESOURCE_QN_FORMAT.format(kind=kind.value, identity=identity)


def _is_rootful(url: str) -> bool:
    # The same test endpoint linking applies to a NETWORK resource's name.
    return url.startswith("/") and not urlparse(url).netloc
