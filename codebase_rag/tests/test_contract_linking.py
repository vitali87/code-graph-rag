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

from codebase_rag import constants as cs
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
        self.writes_with_params: list[tuple[str, PropertyDict | None]] = []

    def fetch_all(
        self, query: str, params: PropertyDict | None = None
    ) -> list[ResultRow]:
        for marker, rows in self._rows.items():
            if marker in query:
                if "$project_prefix" in query and params is not None:
                    prefix = str(params["project_prefix"])
                    return [r for r in rows if f"{r.get('project')}." == prefix]
                if "$project" in query and params is not None:
                    return [r for r in rows if r.get("project") == params["project"]]
                return rows
        if "(f:File)" in query and params is not None:
            # Default: every contract file this repo declares is indexed.
            return [{"absolute_path": path} for path in params["paths"]]
        return []

    def execute_write(self, query: str, params: PropertyDict | None = None) -> None:
        self.writes.append(query)
        self.writes_with_params.append((query, params))

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


_SPEC_WITH_SIBLINGS = json.dumps(
    {
        "openapi": "3.0.0",
        "paths": {
            "/v2/things/{thingId}": {"get": {"operationId": "getThing"}},
            "/v2/things/count": {"get": {"operationId": "countThings"}},
        },
    }
)


def _repo(tmp_path: Path, extra_paths: bool = False) -> Path:
    (tmp_path / "schemas").mkdir()
    spec = _SPEC_WITH_SIBLINGS if extra_paths else _SPEC
    (tmp_path / "schemas/things.json").write_text(spec, encoding="utf-8")
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
    return {
        "qualified_name": f"resource::ENDPOINT::proj::{name}",
        "name": name,
        "project": "proj",
    }


def _rpc_row(name: str, project: str = "proj") -> dict[str, Any]:
    return {
        "qualified_name": f"resource::RPC::{name}",
        "name": name,
        "project": project,
    }


def _links(ingestor: _FakeIngestor) -> set[tuple[PropertyValue, PropertyValue]]:
    return {(src, dst) for src, rel, dst in ingestor.rels if rel == "RESOLVES_TO"}


class TestContractNodes:
    def test_every_operation_becomes_a_resource(self, tmp_path: Path) -> None:
        ingestor = _ingestor()
        link_contracts(ingestor, _repo(tmp_path), project_name="proj")
        names = {str(props["name"]) for _label, props in ingestor.nodes}
        assert names == {
            "schemas/things.createThing",
            "schemas/things.getThing",
            "schemas/things.unservedThing",
            "ThingService.CreateThing",
        }

    def test_nodes_carry_the_contract_kind(self, tmp_path: Path) -> None:
        ingestor = _ingestor()
        link_contracts(ingestor, _repo(tmp_path), project_name="proj")
        assert {str(props["kind"]) for _label, props in ingestor.nodes} == {"CONTRACT"}


class TestRpcAnchoring:
    def test_rpc_resource_resolves_to_its_contract(self, tmp_path: Path) -> None:
        ingestor = _ingestor(rpcs=[_rpc_row("ThingService.CreateThing")])
        link_contracts(ingestor, _repo(tmp_path), project_name="proj")
        assert (
            "resource::RPC::ThingService.CreateThing",
            "resource::CONTRACT::proj::ThingService.CreateThing",
        ) in _links(ingestor)

    def test_an_rpc_no_local_code_touches_stays_unlinked(self, tmp_path: Path) -> None:
        # RPC resources are deliberately unscoped so a client in one project
        # meets a server in another; a same-named service in an UNRELATED
        # project must not be reported as this contract's implementation, so
        # only an RPC this project's own code participates in anchors.
        ingestor = _ingestor(
            rpcs=[_rpc_row("ThingService.CreateThing", project="elsewhere")]
        )
        link_contracts(ingestor, _repo(tmp_path), project_name="proj")
        assert not _links(ingestor)

    def test_undeclared_rpc_resource_stays_unlinked(self, tmp_path: Path) -> None:
        ingestor = _ingestor(rpcs=[_rpc_row("OtherService.Ping")])
        link_contracts(ingestor, _repo(tmp_path), project_name="proj")
        assert not _links(ingestor)


class TestEndpointAnchoring:
    def test_endpoint_resolves_to_the_operation_it_serves(self, tmp_path: Path) -> None:
        ingestor = _ingestor(endpoints=[_endpoint_row("POST /v2/things")])
        link_contracts(ingestor, _repo(tmp_path), project_name="proj")
        assert (
            "resource::ENDPOINT::proj::POST /v2/things",
            "resource::CONTRACT::proj::schemas/things.createThing",
        ) in _links(ingestor)

    def test_path_parameter_spelling_does_not_matter(self, tmp_path: Path) -> None:
        # The server registers `:thingId`, the spec declares `{thingId}`.
        ingestor = _ingestor(endpoints=[_endpoint_row("GET /v2/things/:thingId")])
        link_contracts(ingestor, _repo(tmp_path), project_name="proj")
        assert (
            "resource::ENDPOINT::proj::GET /v2/things/:thingId",
            "resource::CONTRACT::proj::schemas/things.getThing",
        ) in _links(ingestor)

    def test_unknown_mount_lead_still_anchors(self, tmp_path: Path) -> None:
        # A generated registration whose mount prefix could not be resolved
        # carries the unknown lead; the operation it serves is still known.
        ingestor = _ingestor(endpoints=[_endpoint_row("POST /**/v2/things")])
        link_contracts(ingestor, _repo(tmp_path), project_name="proj")
        assert (
            "resource::ENDPOINT::proj::POST /**/v2/things",
            "resource::CONTRACT::proj::schemas/things.createThing",
        ) in _links(ingestor)

    def test_method_must_match(self, tmp_path: Path) -> None:
        ingestor = _ingestor(endpoints=[_endpoint_row("DELETE /v2/things")])
        link_contracts(ingestor, _repo(tmp_path), project_name="proj")
        assert not _links(ingestor)

    def test_unrelated_path_does_not_anchor(self, tmp_path: Path) -> None:
        ingestor = _ingestor(endpoints=[_endpoint_row("POST /v2/other")])
        link_contracts(ingestor, _repo(tmp_path), project_name="proj")
        assert not _links(ingestor)

    def test_method_agnostic_endpoint_anchors_every_method_of_its_path(
        self, tmp_path: Path
    ) -> None:
        # `http.HandleFunc("/v2/things", h)` registers ANY; it serves whatever
        # the contract declares at that path.
        ingestor = _ingestor(endpoints=[_endpoint_row("ANY /v2/things")])
        link_contracts(ingestor, _repo(tmp_path), project_name="proj")
        assert (
            "resource::ENDPOINT::proj::ANY /v2/things",
            "resource::CONTRACT::proj::schemas/things.createThing",
        ) in _links(ingestor)


class TestLinkBounds:
    def test_only_this_projects_endpoints_anchor(self, tmp_path: Path) -> None:
        # ENDPOINT nodes are project-scoped on purpose; another service in
        # the shared graph registering the same verb and path is not an
        # implementation of THIS repo's contract.
        ingestor = _ingestor(
            endpoints=[
                {
                    "qualified_name": "resource::ENDPOINT::other::POST /v2/things",
                    "name": "POST /v2/things",
                    "project": "other",
                }
            ]
        )
        link_contracts(ingestor, _repo(tmp_path), project_name="proj")
        assert not _links(ingestor)

    def test_an_endpoint_matching_several_operations_anchors_none(
        self, tmp_path: Path
    ) -> None:
        # A parameter segment swallows its literal siblings, so `GET /v2/x/:id`
        # matches every operation under that path; picking one would be a
        # guess and linking all of them is noise.
        ingestor = _ingestor(endpoints=[_endpoint_row("GET /v2/things/:id")])
        link_contracts(ingestor, _repo(tmp_path, extra_paths=True), project_name="proj")
        assert not _links(ingestor)

    def test_a_lead_only_template_anchors_nothing(self, tmp_path: Path) -> None:
        # `/**` alone is a route whose whole path is unknown; it would match
        # every operation in the spec.
        ingestor = _ingestor(endpoints=[_endpoint_row("GET /**")])
        link_contracts(ingestor, _repo(tmp_path), project_name="proj")
        assert not _links(ingestor)


class TestProjectIsolation:
    def test_contract_identities_are_project_scoped(self, tmp_path: Path) -> None:
        # Two repositories can hold the same spec at the same relative path;
        # merging their operations into one node would attribute each
        # project's implementations to the other, and the relink sweep of
        # either would delete the other's links.
        ingestor = _ingestor()
        link_contracts(ingestor, _repo(tmp_path), project_name="proj")
        names = {str(props[cs.KEY_QUALIFIED_NAME]) for _label, props in ingestor.nodes}
        assert all("::proj::" in name for name in names), names

    def test_cleanup_stops_at_the_repository_root(self, tmp_path: Path) -> None:
        # `/work/api` must not claim `/work/api-backup`: an unbounded prefix
        # would delete the sibling repository's contract anchors.
        ingestor = _ingestor()
        link_contracts(ingestor, _repo(tmp_path), project_name="proj")
        prefixes = [
            params["repo_prefix"]
            for query, params in ingestor.writes_with_params
            if "repo_prefix" in (params or {})
        ]
        assert prefixes and all(p.endswith("/") for p in prefixes), prefixes


class TestCaptureGate:
    def test_a_sink_that_drops_exposes_receives_no_contract_nodes(
        self, tmp_path: Path
    ) -> None:
        # Selective capture must not leave a Resource whose anchoring edge
        # was filtered out.
        ingestor = _ingestor()
        ingestor.rel_enabled = lambda rel: str(rel) != "EXPOSES"  # type: ignore[attr-defined]
        assert link_contracts(ingestor, _repo(tmp_path), project_name="proj") == 0
        assert not ingestor.nodes


class TestSweepOwnership:
    def test_the_sweep_is_scoped_to_contract_targets(self, tmp_path: Path) -> None:
        # RESOLVES_TO has other owners (URL to endpoint, dispatch suffix);
        # relinking contracts must not touch them.
        ingestor = _ingestor()
        link_contracts(ingestor, _repo(tmp_path), project_name="proj")
        assert ingestor.writes
        assert all("'CONTRACT'" in query for query in ingestor.writes), ingestor.writes

    def test_unindexed_contract_file_declares_nothing(self, tmp_path: Path) -> None:
        # A contract the graph does not hold cannot anchor its operations,
        # and claiming otherwise would hang them off a File node that does
        # not exist.
        ingestor = _FakeIngestor({"'ENDPOINT'": [], "'RPC'": [], "(f:File)": []})
        assert link_contracts(ingestor, _repo(tmp_path), project_name="proj") == 0
        assert not ingestor.nodes

    def test_a_file_that_stopped_declaring_operations_is_cleared(
        self, tmp_path: Path
    ) -> None:
        # An indexed spec edited to declare nothing leaves its File node
        # anchoring the operations it no longer has; the declarations of
        # every contract file in the repo are cleared before re-emission.
        ingestor = _ingestor()
        link_contracts(ingestor, tmp_path, project_name="proj")
        assert any("EXPOSES" in query for query in ingestor.writes), ingestor.writes

    def test_no_contract_files_writes_nothing(self, tmp_path: Path) -> None:
        ingestor = _ingestor(endpoints=[_endpoint_row("POST /v2/things")])
        assert link_contracts(ingestor, tmp_path, project_name="proj") == 0
        assert not ingestor.nodes
