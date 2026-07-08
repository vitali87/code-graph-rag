# (H) Structural integrity audit over recorded graph batches. Codifies the
# (H) checks from the gdotv knowledge-graph analysis (issue #646): orphan
# (H) nodes, required-property completeness, and conformance of labels,
# (H) properties, and relationship endpoint triples against the documented
# (H) schema in types_defs.NODE_SCHEMAS / RELATIONSHIP_SCHEMAS.
from __future__ import annotations

from codebase_rag import constants as cs
from codebase_rag import graph_audit as ga
from codebase_rag.types_defs import GraphNodeRecord, GraphRelRecord, PropertyDict

QN = cs.UniqueKeyType.QUALIFIED_NAME.value


def _project(name: str = "proj") -> GraphNodeRecord:
    return GraphNodeRecord(cs.NodeLabel.PROJECT.value, {"name": name})


def _module(qn: str) -> GraphNodeRecord:
    props: PropertyDict = {
        "qualified_name": qn,
        "name": qn.rsplit(".", 1)[-1],
        "path": "src/mod.py",
        "absolute_path": "/repo/src/mod.py",
    }
    return GraphNodeRecord(cs.NodeLabel.MODULE.value, props)


def _function(qn: str) -> GraphNodeRecord:
    props: PropertyDict = {
        "qualified_name": qn,
        "name": qn.rsplit(".", 1)[-1],
        "decorators": [],
        "path": "src/mod.py",
        "absolute_path": "/repo/src/mod.py",
    }
    return GraphNodeRecord(cs.NodeLabel.FUNCTION.value, props)


def _rel(
    from_label: str, from_qn: str, rel_type: str, to_label: str, to_qn: str
) -> GraphRelRecord:
    return GraphRelRecord((from_label, QN, from_qn), rel_type, (to_label, QN, to_qn))


def _contains_module(project_name: str, module_qn: str) -> GraphRelRecord:
    return GraphRelRecord(
        (cs.NodeLabel.PROJECT.value, cs.UniqueKeyType.NAME.value, project_name),
        cs.RelationshipType.CONTAINS_MODULE.value,
        (cs.NodeLabel.MODULE.value, QN, module_qn),
    )


def _clean_graph() -> tuple[list[GraphNodeRecord], list[GraphRelRecord]]:
    nodes = [_project(), _module("proj.mod"), _function("proj.mod.fn")]
    rels = [
        _contains_module("proj", "proj.mod"),
        _rel(
            cs.NodeLabel.MODULE.value,
            "proj.mod",
            cs.RelationshipType.DEFINES.value,
            cs.NodeLabel.FUNCTION.value,
            "proj.mod.fn",
        ),
    ]
    return nodes, rels


class TestCleanGraph:
    def test_no_violations(self) -> None:
        nodes, rels = _clean_graph()
        assert ga.collect_violations(nodes, rels) == []


class TestOrphans:
    def test_node_without_relationships_is_flagged(self) -> None:
        nodes, rels = _clean_graph()
        nodes.append(_function("proj.mod.stranded"))
        violations = ga.collect_violations(nodes, rels)
        assert [v.check for v in violations] == [cs.AuditCheck.ORPHAN_NODE]
        assert "proj.mod.stranded" in violations[0].detail

    def test_relationship_endpoint_counts_as_connected(self) -> None:
        nodes, rels = _clean_graph()
        assert ga.find_orphans(nodes, rels) == []

    def test_project_only_graph_is_valid(self) -> None:
        # (H) An empty repo indexes to just its Project root; the recorder
        # (H) stores labels as plain strings, so the exemption must hold for
        # (H) the string form.
        assert ga.collect_violations([_project()], []) == []


class TestLabelConformance:
    def test_undocumented_label_is_flagged(self) -> None:
        nodes, rels = _clean_graph()
        nodes.append(GraphNodeRecord("Widget", {"qualified_name": "proj.mod.w"}))
        checks = {v.check for v in ga.collect_violations(nodes, rels)}
        assert cs.AuditCheck.UNDOCUMENTED_LABEL in checks


class TestPropertyConformance:
    def test_undocumented_property_is_flagged(self) -> None:
        nodes, rels = _clean_graph()
        nodes[0].properties["is_external"] = True
        violations = ga.collect_violations(nodes, rels)
        assert [v.check for v in violations] == [cs.AuditCheck.UNDOCUMENTED_PROPERTY]
        assert "is_external" in violations[0].detail

    def test_missing_required_property_is_flagged(self) -> None:
        nodes, rels = _clean_graph()
        del nodes[1].properties["name"]
        violations = ga.collect_violations(nodes, rels)
        assert [v.check for v in violations] == [
            cs.AuditCheck.MISSING_REQUIRED_PROPERTY
        ]

    def test_null_required_property_is_flagged(self) -> None:
        nodes, rels = _clean_graph()
        nodes[2].properties["name"] = None
        checks = [v.check for v in ga.collect_violations(nodes, rels)]
        assert checks == [cs.AuditCheck.MISSING_REQUIRED_PROPERTY]

    def test_optional_property_may_be_absent(self) -> None:
        file_node = GraphNodeRecord(
            cs.NodeLabel.FILE.value,
            {
                "path": "LICENSE",
                "name": "LICENSE",
                "absolute_path": "/repo/LICENSE",
            },
        )
        contains = GraphRelRecord(
            (cs.NodeLabel.PROJECT.value, cs.UniqueKeyType.NAME.value, "proj"),
            cs.RelationshipType.CONTAINS_FILE.value,
            (cs.NodeLabel.FILE.value, cs.UniqueKeyType.PATH.value, "LICENSE"),
        )
        nodes, rels = _clean_graph()
        nodes.append(file_node)
        rels.append(contains)
        assert ga.collect_violations(nodes, rels) == []


class TestPropertyMerging:
    def test_repeated_ensures_merge_properties(self) -> None:
        nodes, rels = _clean_graph()
        partial = GraphNodeRecord(
            cs.NodeLabel.MODULE.value, {"qualified_name": "proj.mod"}
        )
        nodes.insert(0, partial)
        assert ga.collect_violations(nodes, rels) == []


class TestRelationshipConformance:
    def test_undocumented_endpoint_triple_is_flagged(self) -> None:
        nodes, rels = _clean_graph()
        rels.append(
            _rel(
                cs.NodeLabel.MODULE.value,
                "proj.mod",
                cs.RelationshipType.CALLS.value,
                cs.NodeLabel.CLASS.value,
                "proj.mod.Klass",
            )
        )
        violations = ga.collect_violations(nodes, rels)
        checks = [v.check for v in violations]
        assert cs.AuditCheck.UNDOCUMENTED_RELATIONSHIP in checks
        flagged = next(
            v for v in violations if v.check == cs.AuditCheck.UNDOCUMENTED_RELATIONSHIP
        )
        assert cs.RelationshipType.CALLS.value in flagged.detail


JS_EXPORT_PATTERNS = """
const fs = require("fs");

function helper() {
    return fs.constants;
}

const obj = {
    getValue() {
        return 1;
    },
    computed: () => 2,
};

function Person(name) {
    this.name = name;
}

Person.prototype.getAge = function() {
    return 30;
};

module.exports.helper = helper;
exports.inline = function inlineExport() {
    return obj.getValue();
};

export function es6Helper() {
    return helper();
}
"""


class TestIndexedJsGraphIntegrity:
    def test_js_side_channel_functions_are_complete(
        self, temp_repo, mock_ingestor
    ) -> None:
        from codebase_rag.tests.conftest import run_updater

        (temp_repo / "app.js").write_text(JS_EXPORT_PATTERNS)
        run_updater(temp_repo, mock_ingestor)

        nodes = [
            GraphNodeRecord(str(c.args[0]), c.args[1])
            for c in mock_ingestor.ensure_node_batch.call_args_list
        ]
        rels = [
            GraphRelRecord(c.args[0], str(c.args[1]), c.args[2])
            for c in mock_ingestor.ensure_relationship_batch.call_args_list
        ]
        assert ga.collect_violations(nodes, rels) == []


class TestLiveGraphAudit:
    def _fetch_factory(self, rows_by_query_marker: dict[str, list[dict]]):
        def fetch_all(query: str) -> list[dict]:
            for marker, rows in rows_by_query_marker.items():
                if marker in query:
                    return rows
            return []

        return fetch_all

    def test_clean_live_graph_passes(self) -> None:
        fetch = self._fetch_factory(
            {
                "NOT (n)--()": [],
                "UNWIND labels(n)": [],
                "type(r)": [],
            }
        )
        assert ga.collect_live_violations(fetch) == []

    def test_live_orphans_flagged(self) -> None:
        fetch = self._fetch_factory(
            {"NOT (n)--()": [{"label": "Method", "orphans": 427}]}
        )
        violations = ga.collect_live_violations(fetch)
        checks = [v.check for v in violations]
        assert cs.AuditCheck.ORPHAN_NODE in checks
        assert any("427" in v.detail for v in violations)

    def test_live_undocumented_label_and_triple_flagged(self) -> None:
        fetch = self._fetch_factory(
            {
                "UNWIND labels(n) AS label RETURN DISTINCT label": [
                    {"label": "Widget"}
                ],
                "type(r)": [
                    {"src": "Module", "rel": "CALLS", "dst": "Class"},
                ],
            }
        )
        checks = {v.check for v in ga.collect_live_violations(fetch)}
        assert cs.AuditCheck.UNDOCUMENTED_LABEL in checks
        assert cs.AuditCheck.UNDOCUMENTED_RELATIONSHIP in checks

    def test_live_undocumented_property_flagged(self) -> None:
        fetch = self._fetch_factory(
            {
                "UNWIND keys(n)": [{"label": "Module", "key": "is_external"}],
            }
        )
        violations = ga.collect_live_violations(fetch)
        assert [v.check for v in violations] == [cs.AuditCheck.UNDOCUMENTED_PROPERTY]

    def test_live_missing_required_flagged(self) -> None:
        def fetch_all(query: str) -> list[dict]:
            if "IS NULL" in query and "(n:Module)" in query:
                return [{"missing": 3}]
            if "IS NULL" in query:
                return [{"missing": 0}]
            return []

        violations = ga.collect_live_violations(fetch_all)
        checks = [v.check for v in violations]
        assert checks == [cs.AuditCheck.MISSING_REQUIRED_PROPERTY]
        assert "Module" in violations[0].detail


class TestSchemaParsing:
    def test_every_documented_label_has_parsed_properties(self) -> None:
        parsed = ga.documented_node_properties()
        assert set(parsed) == {label.value for label in cs.NodeLabel}
        for props in parsed.values():
            assert props

    def test_file_extension_is_optional(self) -> None:
        parsed = ga.documented_node_properties()
        file_props = parsed[cs.NodeLabel.FILE.value]
        assert file_props["extension"] is False
        assert file_props["path"] is True
