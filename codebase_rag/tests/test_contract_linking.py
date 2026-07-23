# Every artefact of one contract operation meets at one CONTRACT resource
# (issue #912, phase 2): the RPC node a generated client and server already
# share, and the ENDPOINT the generated server registers. The join key is the
# contract's own name for the operation, so it holds even where no URL
# literal exists on the client side and where the two sides disagree on the
# path they use.
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from codebase_rag.parsers.contract_linking import link_contracts
from codebase_rag.types_defs import PropertyDict, PropertyValue, ResultRow

_SPEC = json.dumps(
    {
        "openapi": "3.0.0",
        "paths": {
            "/v2/things": {"post": {"operationId": "createThing"}},
            "/v2/things/{thingId}": {"get": {"operationId": "getThing"}},
            "/v2/unserved": {"get": {"operationId": "unservedThing"}},
        },
    }
)
_PROTO = (
    'syntax = "proto3";\n\n'
    "service ThingService {\n"
    "    rpc CreateThing(Req) returns (Res) {}\n"
    "}\n"
)


class _FakeIngestor:
    """Serves the live-resource queries and records what was written."""

    def __init__(self, rows: dict[str, list[ResultRow]]) -> None:
        self._rows = rows
        self.nodes: list[tuple[str, PropertyDict]] = []
        self.rels: list[tuple[PropertyValue, str, PropertyValue]] = []
        self.writes: list[str] = []

    def fetch_all(
        self, query: str, params: PropertyDict | None = None
    ) -> list[ResultRow]:
        for marker, rows in self._rows.items():
            if marker in query:
                return rows
        if "(f:File)" in query and params is not None:
            # Default: every contract file this repo declares is indexed.
            return [{"absolute_path": path} for path in params["paths"]]
        return []

    def execute_write(self, query: str, params: PropertyDict | None = None) -> None:
        self.writes.append(query)

    def ensure_node_batch(self, label: str, properties: PropertyDict) -> None:
        self.nodes.append((str(label), properties))

    def ensure_relationship_batch(
        self,
        from_spec: tuple[str, str, PropertyValue],
        rel_type: str,
        to_spec: tuple[str, str, PropertyValue],
        properties: PropertyDict | None = None,
    ) -> None:
        self.rels.append((from_spec[2], str(rel_type), to_spec[2]))

    def flush_all(self) -> None:
        return None


def _repo(tmp_path: Path) -> Path:
    (tmp_path / "schemas").mkdir()
    (tmp_path / "schemas/things.json").write_text(_SPEC, encoding="utf-8")
    (tmp_path / "schemas/things.proto").write_text(_PROTO, encoding="utf-8")
    return tmp_path


def _ingestor(**rows: list[dict[str, Any]]) -> _FakeIngestor:
    return _FakeIngestor(
        {
            "'ENDPOINT'": rows.get("endpoints", []),
            "'RPC'": rows.get("rpcs", []),
        }
    )


def _endpoint_row(name: str) -> dict[str, Any]:
    return {"qualified_name": f"resource::ENDPOINT::proj::{name}", "name": name}


def _rpc_row(name: str) -> dict[str, Any]:
    return {"qualified_name": f"resource::RPC::{name}", "name": name}


def _links(ingestor: _FakeIngestor) -> set[tuple[PropertyValue, PropertyValue]]:
    return {(src, dst) for src, rel, dst in ingestor.rels if rel == "RESOLVES_TO"}


class TestContractNodes:
    def test_every_operation_becomes_a_resource(self, tmp_path: Path) -> None:
        ingestor = _ingestor()
        link_contracts(ingestor, _repo(tmp_path))
        names = {str(props["name"]) for _label, props in ingestor.nodes}
        assert names == {
            "things.createThing",
            "things.getThing",
            "things.unservedThing",
            "ThingService.CreateThing",
        }

    def test_nodes_carry_the_contract_kind(self, tmp_path: Path) -> None:
        ingestor = _ingestor()
        link_contracts(ingestor, _repo(tmp_path))
        assert {str(props["kind"]) for _label, props in ingestor.nodes} == {"CONTRACT"}


class TestRpcAnchoring:
    def test_rpc_resource_resolves_to_its_contract(self, tmp_path: Path) -> None:
        ingestor = _ingestor(rpcs=[_rpc_row("ThingService.CreateThing")])
        link_contracts(ingestor, _repo(tmp_path))
        assert (
            "resource::RPC::ThingService.CreateThing",
            "resource::CONTRACT::ThingService.CreateThing",
        ) in _links(ingestor)

    def test_undeclared_rpc_resource_stays_unlinked(self, tmp_path: Path) -> None:
        ingestor = _ingestor(rpcs=[_rpc_row("OtherService.Ping")])
        link_contracts(ingestor, _repo(tmp_path))
        assert not _links(ingestor)


class TestEndpointAnchoring:
    def test_endpoint_resolves_to_the_operation_it_serves(
        self, tmp_path: Path
    ) -> None:
        ingestor = _ingestor(endpoints=[_endpoint_row("POST /v2/things")])
        link_contracts(ingestor, _repo(tmp_path))
        assert (
            "resource::ENDPOINT::proj::POST /v2/things",
            "resource::CONTRACT::things.createThing",
        ) in _links(ingestor)

    def test_path_parameter_spelling_does_not_matter(self, tmp_path: Path) -> None:
        # The server registers `:thingId`, the spec declares `{thingId}`.
        ingestor = _ingestor(endpoints=[_endpoint_row("GET /v2/things/:thingId")])
        link_contracts(ingestor, _repo(tmp_path))
        assert (
            "resource::ENDPOINT::proj::GET /v2/things/:thingId",
            "resource::CONTRACT::things.getThing",
        ) in _links(ingestor)

    def test_unknown_mount_lead_still_anchors(self, tmp_path: Path) -> None:
        # A generated registration whose mount prefix could not be resolved
        # carries the unknown lead; the operation it serves is still known.
        ingestor = _ingestor(endpoints=[_endpoint_row("POST /**/v2/things")])
        link_contracts(ingestor, _repo(tmp_path))
        assert (
            "resource::ENDPOINT::proj::POST /**/v2/things",
            "resource::CONTRACT::things.createThing",
        ) in _links(ingestor)

    def test_method_must_match(self, tmp_path: Path) -> None:
        ingestor = _ingestor(endpoints=[_endpoint_row("DELETE /v2/things")])
        link_contracts(ingestor, _repo(tmp_path))
        assert not _links(ingestor)

    def test_unrelated_path_does_not_anchor(self, tmp_path: Path) -> None:
        ingestor = _ingestor(endpoints=[_endpoint_row("POST /v2/other")])
        link_contracts(ingestor, _repo(tmp_path))
        assert not _links(ingestor)

    def test_method_agnostic_endpoint_anchors_every_method_of_its_path(
        self, tmp_path: Path
    ) -> None:
        # `http.HandleFunc("/v2/things", h)` registers ANY; it serves whatever
        # the contract declares at that path.
        ingestor = _ingestor(endpoints=[_endpoint_row("ANY /v2/things")])
        link_contracts(ingestor, _repo(tmp_path))
        assert (
            "resource::ENDPOINT::proj::ANY /v2/things",
            "resource::CONTRACT::things.createThing",
        ) in _links(ingestor)


class TestSweepOwnership:
    def test_the_sweep_is_scoped_to_contract_targets(self, tmp_path: Path) -> None:
        # RESOLVES_TO has other owners (URL to endpoint, dispatch suffix);
        # relinking contracts must not touch them.
        ingestor = _ingestor()
        link_contracts(ingestor, _repo(tmp_path))
        assert ingestor.writes
        assert all("'CONTRACT'" in query for query in ingestor.writes), ingestor.writes

    def test_unindexed_contract_file_declares_nothing(self, tmp_path: Path) -> None:
        # A contract the graph does not hold cannot anchor its operations,
        # and claiming otherwise would hang them off a File node that does
        # not exist.
        ingestor = _FakeIngestor({"'ENDPOINT'": [], "'RPC'": [], "(f:File)": []})
        assert link_contracts(ingestor, _repo(tmp_path)) == 0
        assert not ingestor.nodes

    def test_no_contract_files_writes_nothing(self, tmp_path: Path) -> None:
        ingestor = _ingestor(endpoints=[_endpoint_row("POST /v2/things")])
        assert link_contracts(ingestor, tmp_path) == 0
        assert not ingestor.nodes
