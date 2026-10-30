"""`resource_qn`: a rootful relative URL is the requesting project's own
backend, so its NETWORK node is scoped to that project (issue #3190);
every other identity, and every other kind, stays one shared node."""

from __future__ import annotations

import pytest

from codebase_rag.parsers.io_access.constants import ResourceKind
from codebase_rag.parsers.io_access.resource_names import resource_qn


def test_a_rootful_relative_url_is_scoped_to_its_project() -> None:
    assert (
        resource_qn(ResourceKind.NETWORK, "/carts/7", "shop")
        == "resource::NETWORK::shop::/carts/7"
    )


@pytest.mark.parametrize(
    "url",
    [
        "http://api.example.com/carts/7",
        # Protocol-relative: names its host, so it is one shared service.
        "//cdn.example.com/lib.js",
        # Not rootful: nothing says it reaches the project's own origin.
        "carts/7",
        "<dynamic>",
    ],
)
def test_a_url_that_names_a_host_or_no_root_stays_shared(url: str) -> None:
    assert resource_qn(ResourceKind.NETWORK, url, "shop") == f"resource::NETWORK::{url}"


def test_without_a_project_a_rootful_url_stays_shared() -> None:
    assert (
        resource_qn(ResourceKind.NETWORK, "/carts/7", None)
        == "resource::NETWORK::/carts/7"
    )
    assert (
        resource_qn(ResourceKind.NETWORK, "/carts/7", "")
        == "resource::NETWORK::/carts/7"
    )


@pytest.mark.parametrize("kind", [ResourceKind.FILE, ResourceKind.ENDPOINT])
def test_only_network_identities_are_scoped(kind: ResourceKind) -> None:
    # An absolute file path is the same file whoever reads it.
    assert resource_qn(kind, "/etc/hosts", "shop") == f"resource::{kind}::/etc/hosts"
