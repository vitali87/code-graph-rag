"""Issue #2536: `cgr doctor` accepts every graph cgr itself builds.

#2431 covered `LINKS_TO`, `IncompleteRun` and `CALLS->Class`. Three more
shapes still failed the structural audit on a graph only cgr had written:

* TypeScript heritage onto a `type` alias. `interface X extends Alias` and
  `class Y implements Alias` are legal and common (zod's `ZodError.ts`), and
  the indexer resolves the heritage to the alias's `Type` node, which
  `RELATIONSHIP_SCHEMAS` did not list as a target.
* The `project` property an ENDPOINT `Resource` carries, which the endpoint
  linker reads back to scope URL matches, but `NODE_SCHEMAS` did not list.
* Finding nodes (`Pattern`, `CodeSmell`, `SecurityIssue`) left behind when a
  sync drops the `findings` capture group. The module re-parse deleted their
  edges but not the nodes, so each one became an orphan.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from unittest.mock import MagicMock

import codec.schema_pb2 as pb
from codebase_rag import constants as cs
from codebase_rag import cypher_queries as cq
from codebase_rag import graph_audit as ga
from codebase_rag.capture import resolve_capture
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.services.protobuf_service import ProtobufFileIngestor
from codebase_rag.tests.conftest import get_relationships
from codebase_rag.types_defs import GraphNodeRecord, GraphRelRecord, ResultRow
from evals.cgr_graph import _StatefulIngestor

# The issue's repro, plus the ordinary interface-to-interface shapes the fix
# must leave alone.
ISSUES_TS = """export type IssueBase = { path: string[] };
export interface InvalidType extends IssueBase { expected: string }
export type ParseInput = { data: unknown };
export class LazyPath implements ParseInput { data: unknown = null; }

export interface Named { name: string }
export interface Labelled extends Named { label: string }
export class Tag implements Named { name = "tag"; }
"""

ROUTES_PY = """from flask import Flask

app = Flask(__name__)


@app.get("/items")
def list_items():
    return []
"""

# The route is an ENDPOINT resource (project-scoped); the environment read is
# an ENV resource, shared across projects and so without one.
ROUTES_WITH_ENV_PY = """import os

from flask import Flask

app = Flask(__name__)


@app.get("/items")
def list_items():
    return os.environ.get("ITEMS_DIR")
"""

# `_instance = None` in a class is a Pattern (singleton) and `eval` on a
# parameter a SecurityIssue under the shipped rules; the fixture guards below
# fail loudly if either stops matching, rather than passing on an empty set.
FINDINGS_PY = """class Config:
    _instance = None


def run(data):
    return eval(data)
"""
PLAIN_PY = "def helper():\n    return 1\n"
# The group the issue toggles. `--capture all` reaches it too, but also turns
# on I/O, whose endpoint link queries the stateful double does not emulate;
# the live-graph transition from `all` is the integration test's.
_WITH_FINDINGS = [f"{cs.CAPTURE_ADD_PREFIX}{cs.CaptureGroup.FINDINGS.value}"]

_FINDING_LABELS = frozenset(
    {
        cs.NodeLabel.PATTERN.value,
        cs.NodeLabel.CODE_SMELL.value,
        cs.NodeLabel.SECURITY_ISSUE.value,
    }
)
_FINDING_RELS = frozenset(
    rel.value for rel in cs.CAPTURE_GROUP_RELS[cs.CaptureGroup.FINDINGS]
)


def _records(
    mock_ingestor: MagicMock,
) -> tuple[list[GraphNodeRecord], list[GraphRelRecord]]:
    nodes = [
        GraphNodeRecord(str(c.args[0]), c.args[1])
        for c in mock_ingestor.ensure_node_batch.call_args_list
    ]
    rels = [
        GraphRelRecord(c.args[0], str(c.args[1]), c.args[2])
        for c in mock_ingestor.ensure_relationship_batch.call_args_list
    ]
    return nodes, rels


def _heritage(mock_ingestor: MagicMock, rel_type: str) -> set[tuple[str, str, str]]:
    """(child simple name, parent label, parent simple name) per edge."""
    return {
        (
            str(c.args[0][2]).rsplit(".", 1)[-1],
            str(c.args[2][0]),
            str(c.args[2][2]).rsplit(".", 1)[-1],
        )
        for c in get_relationships(mock_ingestor, rel_type)
    }


def _index(repo: Path, ingestor: MagicMock, tokens: list[str] | None = None) -> None:
    # GraphUpdater directly, not `run_updater`: that helper asserts the same
    # audit, and these tests want to see WHICH part of it fails.
    parsers, queries = load_parsers()
    GraphUpdater(
        ingestor=ingestor,
        repo_path=repo,
        parsers=parsers,
        queries=queries,
        capture=resolve_capture(tokens or []),
    ).run()


# --- TypeScript heritage onto a type alias ----------------------------------


def test_an_interface_extending_a_type_alias_is_documented() -> None:
    assert (
        cs.NodeLabel.INTERFACE.value,
        cs.RelationshipType.INHERITS.value,
        cs.NodeLabel.TYPE.value,
    ) in ga.documented_relationship_triples()


def test_a_class_implementing_a_type_alias_is_documented() -> None:
    assert (
        cs.NodeLabel.CLASS.value,
        cs.RelationshipType.IMPLEMENTS.value,
        cs.NodeLabel.TYPE.value,
    ) in ga.documented_relationship_triples()


def test_the_issue_typescript_repro_passes_the_audit(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    (temp_repo / "src").mkdir()
    (temp_repo / "src" / "issues.ts").write_text(ISSUES_TS)
    _index(temp_repo, mock_ingestor)

    assert ga.collect_violations(*_records(mock_ingestor)) == []


def test_the_indexer_keeps_heritage_onto_the_alias(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # Negative: the schema was too narrow, the graph was right. The edges
    # must still be written, onto the alias's Type node, and the ordinary
    # interface-to-interface shapes keep their Interface target.
    (temp_repo / "src").mkdir()
    (temp_repo / "src" / "issues.ts").write_text(ISSUES_TS)
    _index(temp_repo, mock_ingestor)

    inherits = _heritage(mock_ingestor, cs.RelationshipType.INHERITS)
    implements = _heritage(mock_ingestor, cs.RelationshipType.IMPLEMENTS)

    assert ("InvalidType", cs.NodeLabel.TYPE, "IssueBase") in inherits
    assert ("LazyPath", cs.NodeLabel.TYPE, "ParseInput") in implements
    assert ("Labelled", cs.NodeLabel.INTERFACE, "Named") in inherits
    assert ("Tag", cs.NodeLabel.INTERFACE, "Named") in implements


def test_other_heritage_onto_a_type_is_still_flagged() -> None:
    # Negative: only the two TypeScript shapes are documented. A class cannot
    # `extends` a type alias and an interface never `implements`, so neither
    # triple may ride in on the change.
    def rel(src: cs.NodeLabel, rel_type: cs.RelationshipType) -> GraphRelRecord:
        return GraphRelRecord(
            (src, cs.KEY_QUALIFIED_NAME, "proj.a.Child"),
            rel_type,
            (cs.NodeLabel.TYPE, cs.KEY_QUALIFIED_NAME, "proj.a.Alias"),
        )

    violations = ga.find_relationship_violations(
        [
            rel(cs.NodeLabel.CLASS, cs.RelationshipType.INHERITS),
            rel(cs.NodeLabel.INTERFACE, cs.RelationshipType.IMPLEMENTS),
        ]
    )

    assert [v.check for v in violations] == [
        cs.AuditCheck.UNDOCUMENTED_RELATIONSHIP,
        cs.AuditCheck.UNDOCUMENTED_RELATIONSHIP,
    ]


# --- the ENDPOINT Resource's `project` property ------------------------------


def test_the_resource_project_property_is_documented_as_optional() -> None:
    resource = ga.documented_node_properties()[cs.NodeLabel.RESOURCE.value]

    assert resource.get(cs.KEY_PROJECT) is False


def test_an_indexed_endpoint_resource_passes_the_property_audit(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    (temp_repo / "routes.py").write_text(ROUTES_PY)
    _index(temp_repo, mock_ingestor, [cs.CAPTURE_TOKEN_ALL])
    nodes, _rels = _records(mock_ingestor)
    endpoints = [
        node
        for node in nodes
        if node.label == cs.NodeLabel.RESOURCE
        and node.properties.get("kind") == "ENDPOINT"
    ]
    assert endpoints, "fixture guard: the route produced no ENDPOINT resource"
    assert all(node.properties.get(cs.KEY_PROJECT) for node in endpoints)

    assert ga.find_property_violations(endpoints) == []


def _live(rows: dict[str, list[ResultRow]]) -> Callable[[str], list[ResultRow]]:
    def fetch_all(query: str) -> list[ResultRow]:
        return rows.get(query, [])

    return fetch_all


def test_doctor_accepts_a_resource_carrying_its_project() -> None:
    fetch = _live(
        {
            cq.CYPHER_AUDIT_LABELS: [{"label": cs.NodeLabel.RESOURCE.value}],
            cq.CYPHER_AUDIT_LABEL_PROPS: [
                {"label": cs.NodeLabel.RESOURCE.value, "key": key}
                for key in (
                    cs.KEY_QUALIFIED_NAME,
                    cs.KEY_NAME,
                    "kind",
                    cs.KEY_PROJECT,
                )
            ],
        }
    )

    assert ga.collect_live_violations(fetch) == []


def test_a_resource_without_a_project_is_not_missing_one() -> None:
    # Negative: only ENDPOINT resources are project-scoped. A FILE or ENV
    # resource is shared across projects and has no `project`, so the
    # property must stay optional.
    shared = GraphNodeRecord(
        cs.NodeLabel.RESOURCE.value,
        {
            cs.KEY_QUALIFIED_NAME: "resource:ENV:HOME",
            cs.KEY_NAME: "HOME",
            "kind": "ENV",
        },
    )

    assert ga.find_property_violations([shared]) == []


def test_an_unknown_resource_property_is_still_flagged() -> None:
    # Negative: documenting `project` must not open the label to anything.
    node = GraphNodeRecord(
        cs.NodeLabel.RESOURCE.value,
        {
            cs.KEY_QUALIFIED_NAME: "resource:ENV:HOME",
            cs.KEY_NAME: "HOME",
            "kind": "ENV",
            "owner": "someone",
        },
    )

    assert [v.check for v in ga.find_property_violations([node])] == [
        cs.AuditCheck.UNDOCUMENTED_PROPERTY
    ]


def _exported_resources(repo: Path, out: Path) -> dict[str, pb.Resource]:
    # Through the real export sink, read back from the file it writes: the
    # sink copies only the properties its message declares, so a property the
    # schema lists but the message lacks is dropped here without a warning.
    out.mkdir()
    parsers, queries = load_parsers()
    GraphUpdater(
        ingestor=ProtobufFileIngestor(str(out), split_index=False),
        repo_path=repo,
        parsers=parsers,
        queries=queries,
        capture=resolve_capture([cs.CAPTURE_TOKEN_ALL]),
    ).run()
    index = pb.GraphCodeIndex()
    index.ParseFromString((out / cs.PROTOBUF_INDEX_FILE).read_bytes())
    return {
        node.resource.qualified_name: node.resource
        for node in index.nodes
        if node.WhichOneof(cs.PROTOBUF_PAYLOAD_ONEOF) == cs.ONEOF_RESOURCE
    }


def test_an_endpoint_resource_keeps_its_project_through_the_export(
    temp_repo: Path, tmp_path: Path
) -> None:
    (temp_repo / "routes.py").write_text(ROUTES_WITH_ENV_PY)
    resources = _exported_resources(temp_repo, tmp_path / "export")
    endpoints = [r for r in resources.values() if r.kind == "ENDPOINT"]
    assert endpoints, f"fixture guard: no ENDPOINT resource exported: {resources}"

    assert [r.project for r in endpoints] == [temp_repo.name]


def test_a_shared_resource_exports_without_a_project(
    temp_repo: Path, tmp_path: Path
) -> None:
    # Negative: only the endpoint is project-scoped. An ENV resource is shared
    # across projects, so nothing may be written for it, not even a project
    # borrowed from the run.
    (temp_repo / "routes.py").write_text(ROUTES_WITH_ENV_PY)
    resources = _exported_resources(temp_repo, tmp_path / "export")
    shared = [r for r in resources.values() if r.kind == "ENV"]
    assert shared, f"fixture guard: no ENV resource exported: {resources}"

    assert all(
        cs.KEY_PROJECT not in {field.name for field, _ in r.ListFields()}
        for r in shared
    )


# --- findings across a capture-set change ------------------------------------


def _write(repo: Path, files: dict[str, str]) -> None:
    for name, source in files.items():
        (repo / name).write_text(source)


def _sync(store: _StatefulIngestor, repo: Path, tokens: list[str]) -> None:
    # An incremental sync, as `cgr start --update-graph` runs one: a changed
    # capture selection changes the parser fingerprint, and that is what
    # re-parses the unchanged files.
    parsers, queries = load_parsers()
    GraphUpdater(
        ingestor=store,
        repo_path=repo,
        parsers=parsers,
        queries=queries,
        capture=resolve_capture(tokens),
    ).run()


def _findings(store: _StatefulIngestor) -> set[str]:
    return {
        str(props[cs.KEY_QUALIFIED_NAME])
        for (label, _uid), props in store.nodes.items()
        if label in _FINDING_LABELS
    }


def _attached_findings(store: _StatefulIngestor) -> set[str]:
    return {
        str(target)
        for _sl, _src, rel, target_label, target in store.edges
        if target_label in _FINDING_LABELS and rel in _FINDING_RELS
    }


def test_dropping_the_findings_group_deletes_its_nodes(tmp_path: Path) -> None:
    _write(tmp_path, {"app.py": FINDINGS_PY, "other.py": PLAIN_PY})
    store = _StatefulIngestor()
    _sync(store, tmp_path, _WITH_FINDINGS)
    labels = {label for (label, _uid) in store.nodes if label in _FINDING_LABELS}
    assert {cs.NodeLabel.PATTERN.value, cs.NodeLabel.SECURITY_ISSUE.value} <= labels, (
        "fixture guard: the findings sync produced no Pattern and SecurityIssue"
    )

    _sync(store, tmp_path, [])

    assert _findings(store) == set()


def test_a_finding_the_source_no_longer_has_is_deleted(tmp_path: Path) -> None:
    # The same leak without a capture change: an edit re-parses the module,
    # and a finding its new source no longer matches was left behind.
    _write(tmp_path, {"app.py": FINDINGS_PY})
    store = _StatefulIngestor()
    _sync(store, tmp_path, _WITH_FINDINGS)
    assert any(qn.endswith(".eval_use") for qn in _findings(store)), (
        "fixture guard: `eval(data)` produced no eval_use finding"
    )

    _write(tmp_path, {"app.py": FINDINGS_PY.replace("eval(data)", "data")})
    _sync(store, tmp_path, _WITH_FINDINGS)

    after = _findings(store)
    assert not any(qn.endswith(".eval_use") for qn in after), after
    assert after == _attached_findings(store)


def test_findings_come_back_attached_when_the_group_is_re_enabled(
    tmp_path: Path,
) -> None:
    # Negative: the drop removes the nodes, it does not stop the next
    # findings sync from writing them again, each one hanging off its module.
    _write(tmp_path, {"app.py": FINDINGS_PY})
    store = _StatefulIngestor()
    _sync(store, tmp_path, _WITH_FINDINGS)
    first = _findings(store)

    _sync(store, tmp_path, [])
    _sync(store, tmp_path, _WITH_FINDINGS)

    assert _findings(store) == first
    assert _attached_findings(store) == first


def test_an_unchanged_module_keeps_its_findings(tmp_path: Path) -> None:
    # Negative: findings go with their module only when that module is
    # re-parsed. Editing a sibling file must not take them.
    _write(tmp_path, {"app.py": FINDINGS_PY, "other.py": PLAIN_PY})
    store = _StatefulIngestor()
    _sync(store, tmp_path, _WITH_FINDINGS)
    first = _findings(store)
    assert first, "fixture guard: the findings sync produced no findings"

    _write(tmp_path, {"other.py": PLAIN_PY + "\n\ndef more():\n    return 2\n"})
    _sync(store, tmp_path, _WITH_FINDINGS)

    assert _findings(store) == first
    assert _attached_findings(store) == first


def test_the_same_finding_in_two_modules_is_two_nodes_and_outlives_either(
    tmp_path: Path,
) -> None:
    # The module delete takes a finding with its module, which is safe only
    # because no finding is shared: its qn is the module's own qn plus line,
    # column and rule id, and only that module links to it (Greptile, PR
    # #2572). Two files with the same source hold the same findings at the
    # same spots; they are separate nodes, so deleting one file must leave
    # the other's findings in place and attached.
    _write(tmp_path, {"app.py": FINDINGS_PY, "twin.py": FINDINGS_PY})
    store = _StatefulIngestor()
    _sync(store, tmp_path, _WITH_FINDINGS)
    holders: dict[str, set[str]] = {}
    for _sl, source, rel, target_label, target in store.edges:
        if target_label in _FINDING_LABELS and rel in _FINDING_RELS:
            holders.setdefault(str(target), set()).add(str(source))
    twin = f"{tmp_path.name}.twin"
    twin_findings = {qn for qn, modules in holders.items() if modules == {twin}}
    assert twin_findings, "fixture guard: twin.py produced no findings"
    assert all(len(modules) == 1 for modules in holders.values()), holders

    (tmp_path / "app.py").unlink()
    _sync(store, tmp_path, _WITH_FINDINGS)

    assert _findings(store) == twin_findings
    assert _attached_findings(store) == twin_findings
