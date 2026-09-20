"""The eval double must answer every query cgr issues, or refuse (issue #1716).

`_StatefulIngestor` stands in for the graph in the incremental and re-ingest
tests. It matched queries by identity and answered anything unrecognised with
`[]`, which is a valid result for most of them -- so an unemulated query was
indistinguishable from a genuinely empty graph, and a test could pass because
a lookup silently returned nothing.

That was not hypothetical. `CYPHER_UNRESOLVED_IMPORTER_PATHS` (#1682) went
unemulated, its waiting-importer lookup answered `[]` for every scenario, and
that issue's tests could only ever cover the query layer. Measured over five
files at the time of writing: 136 tests passed while SIX distinct queries were
being answered `[]`.

These tests pin the two halves of the fix.

READING, not merely non-emptiness. Each emulation is checked by asserting the
CONTENT of the rows it returns -- the qualified names, the module a definition
hangs off, the container of a method, the count. "No longer `[]`" would pass
against an emulation that returned one garbage row, which is the same fail-open
shape one level up.

REFUSING. `case _` raises, so a query added to the updater and forgotten here
fails loudly instead of quietly answering nothing.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag import graph_updater as gu
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from evals import cgr_graph
from evals.cgr_graph import _StatefulIngestor

PREFIX = {cs.KEY_PROJECT_PREFIX: "proj."}

PY_SRC = {
    "util.py": "def helper():\n    return 1\n",
    "app.py": "class Widget:\n    def draw(self):\n        return 2\n",
}
CS_SRC = {"Shapes.cs": "namespace Demo;\npublic class Circle { }\n"}
GO_SRC = {"pkg/types.go": "package pkg\n\ntype T struct{}\n"}


def _indexed(root: Path, files: dict[str, str]) -> _StatefulIngestor:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    store = _StatefulIngestor()
    parsers, queries = load_parsers()
    GraphUpdater(
        ingestor=store,
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name="proj",
    ).run(force=True)
    return store


@pytest.fixture
def python_store(tmp_path: Path) -> _StatefulIngestor:
    root = tmp_path / "proj"
    root.mkdir()
    return _indexed(root, PY_SRC)


class TestTheDoubleAnswersWithRealRows:
    def test_function_locations_carry_their_module_and_qualified_name(
        self, python_store: _StatefulIngestor
    ) -> None:
        rows = python_store.fetch_all(cs.CYPHER_ALL_FUNCTION_LOCATIONS, PREFIX)

        by_qn = {row[cs.KEY_QUALIFIED_NAME]: row for row in rows}
        assert "proj.util.helper" in by_qn, sorted(map(str, by_qn))
        helper = by_qn["proj.util.helper"]
        assert helper[cs.KEY_MODULE_QN] == "proj.util"
        assert helper[cs.KEY_LABEL] == cs.NodeLabel.FUNCTION.value
        # A location with no line is useless to the rehydration it feeds.
        assert isinstance(helper[cs.KEY_START_LINE], int)

    def test_method_locations_carry_their_container(
        self, python_store: _StatefulIngestor
    ) -> None:
        rows = python_store.fetch_all(cs.CYPHER_ALL_METHOD_LOCATIONS, PREFIX)

        by_qn = {row[cs.KEY_QUALIFIED_NAME]: row for row in rows}
        assert "proj.app.Widget.draw" in by_qn, sorted(map(str, by_qn))
        draw = by_qn["proj.app.Widget.draw"]
        assert draw[cs.KEY_CONTAINER_QN] == "proj.app.Widget"
        assert draw[cs.KEY_MODULE_QN] == "proj.app"
        assert draw[cs.KEY_LABEL] == cs.NodeLabel.METHOD.value

    def test_a_method_is_not_returned_as_a_function(
        self, python_store: _StatefulIngestor
    ) -> None:
        """The two queries select different labels, and conflating them would
        rehydrate a method under its module instead of its class."""
        functions = {
            row[cs.KEY_QUALIFIED_NAME]
            for row in python_store.fetch_all(cs.CYPHER_ALL_FUNCTION_LOCATIONS, PREFIX)
        }

        assert "proj.util.helper" in functions
        assert "proj.app.Widget.draw" not in functions

    def test_the_module_count_is_the_number_of_project_modules(
        self, python_store: _StatefulIngestor
    ) -> None:
        """A COUNT query returns exactly one row, and its caller reads
        `rows[0]["count"]`. Answering `[]` raised IndexError there, the handler
        returned, and the orphaned-cache check never ran."""
        rows = python_store.fetch_all(
            cs.CYPHER_COUNT_PROJECT_MODULES,
            {cs.KEY_PROJECT_NAME: "proj", cs.KEY_PROJECT_PREFIX: "proj."},
        )

        assert len(rows) == 1
        assert rows[0][cs.KEY_COUNT] == len(PY_SRC)

    def test_the_module_count_excludes_other_projects(self, tmp_path: Path) -> None:
        """The control. A count that ignored the prefix would still be
        non-empty and still look right on a single-project fixture."""
        root = tmp_path / "proj"
        root.mkdir()
        store = _indexed(root, PY_SRC)
        store.ensure_node_batch(
            cs.NodeLabel.MODULE.value,
            {cs.KEY_QUALIFIED_NAME: "other.mod", cs.KEY_PATH: "other/mod.py"},
        )

        rows = store.fetch_all(
            cs.CYPHER_COUNT_PROJECT_MODULES,
            {cs.KEY_PROJECT_NAME: "proj", cs.KEY_PROJECT_PREFIX: "proj."},
        )

        assert rows[0][cs.KEY_COUNT] == len(PY_SRC)

    def test_csharp_type_locations_are_restricted_to_cs_files(
        self, tmp_path: Path
    ) -> None:
        """The query carries `AND n.path ENDS WITH '.cs'`. A Python class must
        not appear, or the C# partial-group join would key on it."""
        root = tmp_path / "proj"
        root.mkdir()
        parsers, _ = load_parsers()
        if cs.SupportedLanguage.CSHARP not in parsers:
            pytest.skip("c_sharp parser not available")
        store = _indexed(root, {**CS_SRC, **PY_SRC})

        rows = store.fetch_all(cs.CYPHER_ALL_CSHARP_TYPE_LOCATIONS, PREFIX)

        paths = {row[cs.KEY_PATH] for row in rows}
        assert paths, "no C# type locations returned at all"
        assert all(str(path).endswith(".cs") for path in paths), paths
        qns = {row[cs.KEY_QUALIFIED_NAME] for row in rows}
        assert any("Circle" in str(qn) for qn in qns), qns
        # The Python class in the same index must be absent.
        assert not any("Widget" in str(qn) for qn in qns), qns

    def test_go_type_locations_include_the_struct(self, tmp_path: Path) -> None:
        root = tmp_path / "proj"
        root.mkdir()
        parsers, _ = load_parsers()
        if cs.SupportedLanguage.GO not in parsers:
            pytest.skip("go parser not available")
        store = _indexed(root, GO_SRC)

        rows = store.fetch_all(cs.CYPHER_ALL_GO_TYPE_LOCATIONS, PREFIX)

        by_qn = {row[cs.KEY_QUALIFIED_NAME]: row for row in rows}
        assert "proj.pkg.types.T" in by_qn, sorted(map(str, by_qn))
        assert by_qn["proj.pkg.types.T"][cs.KEY_LABEL] in {
            cs.NodeLabel.CLASS.value,
            cs.NodeLabel.TYPE.value,
        }


class TestQueriesWhoseCallersSwallowExceptions:
    """Some readers catch the refusal, so for them emulation is not optional.

    `case _` raising only helps when the exception reaches the test. The three
    route-rehydration queries are read inside `except Exception: return []`
    (`graph_updater.py`), so a raise is caught and turned straight back into an
    empty result — the fail-closed default defeated one layer down, invisibly.
    Emulating them is the only thing that works for these (raised on #1716).

    Asserted by CONTENT, not by "did not raise": a refusal converted to `[]` by
    the caller and a faithful empty answer are indistinguishable from the
    outside, which is the whole problem.
    """

    def test_project_modules_returns_the_matching_modules(
        self, python_store: _StatefulIngestor
    ) -> None:
        rows = python_store.fetch_all(
            gu.CYPHER_PROJECT_MODULES,
            {cs.KEY_PROJECT_PREFIX: "proj.", "extensions": [".py"]},
        )

        assert {row[cs.KEY_QUALIFIED_NAME] for row in rows} == {
            "proj.util",
            "proj.app",
        }

    def test_project_modules_honours_the_extension_filter(
        self, python_store: _StatefulIngestor
    ) -> None:
        """The control. Ignoring `$extensions` would still look right above,
        because every module in that fixture is Python."""
        rows = python_store.fetch_all(
            gu.CYPHER_PROJECT_MODULES,
            {cs.KEY_PROJECT_PREFIX: "proj.", "extensions": [".ts"]},
        )

        assert rows == []

    def test_project_py_modules_returns_python_modules(
        self, python_store: _StatefulIngestor
    ) -> None:
        rows = python_store.fetch_all(
            gu.CYPHER_PROJECT_PY_MODULES, {cs.KEY_PROJECT_PREFIX: "proj."}
        )

        assert {row[cs.KEY_QUALIFIED_NAME] for row in rows} == {
            "proj.util",
            "proj.app",
        }

    def test_route_handlers_returns_only_decorated_definitions(
        self, tmp_path: Path
    ) -> None:
        root = tmp_path / "proj"
        root.mkdir()
        store = _indexed(
            root,
            {
                "api.py": (
                    "def route(path):\n"
                    "    def deco(fn):\n"
                    "        return fn\n"
                    "    return deco\n"
                    "\n"
                    "@route('/x')\n"
                    "def handler():\n"
                    "    return 1\n"
                    "\n"
                    "def plain():\n"
                    "    return 2\n"
                ),
            },
        )

        rows = store.fetch_all(
            gu.CYPHER_PROJECT_ROUTE_HANDLERS, {cs.KEY_PROJECT_PREFIX: "proj."}
        )

        qns = {row[cs.KEY_QUALIFIED_NAME] for row in rows}
        assert "proj.api.handler" in qns, qns
        # The undecorated sibling must be absent: the query selects on
        # `f.decorators IS NOT NULL`, and returning every function would make
        # a stale-route sweep touch definitions that never exposed a route.
        assert "proj.api.plain" not in qns, qns


class TestTheDoubleRefusesWhatItDoesNotModel:
    def test_an_unemulated_query_raises_rather_than_answering_nothing(self) -> None:
        store = _StatefulIngestor()

        with pytest.raises(AssertionError, match="does not emulate this query"):
            store.fetch_all("MATCH (n:Invented) RETURN n.qualified_name AS qn", {})

    def test_a_deliberately_unmodelled_query_still_answers_empty(self) -> None:
        """The embeddings pass writes vectors to Qdrant, which this double does
        not model, and its caller already treats `[]` as "nothing to embed". It
        is named in `_NOT_MODELLED` so the refusal above cannot swallow it."""
        store = _StatefulIngestor()

        assert (
            store.fetch_all(cs.CYPHER_QUERY_EMBEDDINGS, {"project_name": "proj"}) == []
        )


class TestTheModuleSubtreeWalkMirrorsTheDeleteQuery:
    """`_MODULE_SUBTREE_RELS` claims to be "what CYPHER_DELETE_MODULE walks".

    It is a hand-maintained copy of a list that lives in the query, so it
    drifts silently: nothing reads both. It had already lost CONTAINS_SECTION
    (issue #1938), which is how a document's headings hang off its Module, so
    a re-parse dropped every Section in production and kept them in the
    double. A test asserting on Section survival across a re-ingest would
    have pinned the opposite of production behaviour.

    Deriving the expectation FROM the query rather than restating it is the
    point: a restated list is the same hand-maintained copy one layer down,
    and would drift the same way the next time a relation joins the walk.
    """

    @staticmethod
    def _rels_in_delete_walk() -> frozenset[str]:
        """The relation names inside the delete query's variable-length walk.

        Anchored on the bracket that follows `OPTIONAL MATCH (m)`, so an
        unrelated relation mentioned elsewhere in the query cannot leak in.
        """
        match = re.search(
            r"OPTIONAL MATCH \(m\)-\[:([A-Z_|]+)\*", cs.CYPHER_DELETE_MODULE
        )
        assert match is not None, (
            "CYPHER_DELETE_MODULE no longer contains the walk this test reads; "
            "the emulator's subtree set cannot be checked against it"
        )
        return frozenset(match.group(1).split("|"))

    def test_the_double_walks_exactly_what_the_delete_query_walks(self) -> None:
        walked = self._rels_in_delete_walk()

        assert walked == cgr_graph._MODULE_SUBTREE_RELS, (
            "the double's module subtree drifted from CYPHER_DELETE_MODULE: "
            f"only in the query {sorted(walked - cgr_graph._MODULE_SUBTREE_RELS)}, "
            f"only in the double {sorted(cgr_graph._MODULE_SUBTREE_RELS - walked)}"
        )

    def test_the_extractor_reads_a_real_walk(self) -> None:
        """The control. If the regex matched nothing the assertion above would
        raise rather than pass, but a regex that matched a SHORTER walk than
        the real one would make the comparison vacuous in the quiet
        direction, so pin two relations known to be in it by name."""
        walked = self._rels_in_delete_walk()

        assert cs.RelationshipType.DEFINES.value in walked
        assert cs.RelationshipType.CONTAINS_SECTION.value in walked
        assert len(walked) >= 5

    def test_a_document_reparse_drops_its_sections(self) -> None:
        """The behaviour the omission changed, driven through the double.

        Without CONTAINS_SECTION in the walk the Section survives the
        re-parse here while production deletes it (issue #1426's own
        regression, seen from the emulator's side).
        """
        store = _StatefulIngestor()
        store.ensure_node_batch(
            cs.NodeLabel.MODULE.value,
            {cs.KEY_QUALIFIED_NAME: "proj.doc", cs.KEY_PATH: "doc.md"},
        )
        store.ensure_node_batch(
            cs.NodeLabel.SECTION.value,
            {cs.KEY_QUALIFIED_NAME: "proj.doc.Heading", cs.KEY_PATH: "doc.md"},
        )
        store.ensure_relationship_batch(
            (cs.NodeLabel.MODULE.value, cs.KEY_QUALIFIED_NAME, "proj.doc"),
            cs.RelationshipType.CONTAINS_SECTION.value,
            (cs.NodeLabel.SECTION.value, cs.KEY_QUALIFIED_NAME, "proj.doc.Heading"),
        )
        assert store._nodes_at_path(cs.NodeLabel.SECTION.value, "doc.md")

        store._delete_module_subtree("doc.md")

        assert not store._nodes_at_path(cs.NodeLabel.SECTION.value, "doc.md"), (
            "the Section outlived its Module's re-parse, which production's "
            "CYPHER_DELETE_MODULE would not allow"
        )

    def test_a_nested_subsection_goes_with_its_parent(self) -> None:
        """Sections nest: a Section CONTAINS_SECTION its subsections, and the
        production walk is variable-length (`*0..`), so it reaches a
        sub-heading through its parent. A single-level fixture passes even if
        the double stops after one hop, so this drives the depth."""
        store = _StatefulIngestor()
        store.ensure_node_batch(
            cs.NodeLabel.MODULE.value,
            {cs.KEY_QUALIFIED_NAME: "proj.doc", cs.KEY_PATH: "doc.md"},
        )
        for qn in ("proj.doc.Top", "proj.doc.Top.Nested"):
            store.ensure_node_batch(
                cs.NodeLabel.SECTION.value,
                {cs.KEY_QUALIFIED_NAME: qn, cs.KEY_PATH: "doc.md"},
            )
        store.ensure_relationship_batch(
            (cs.NodeLabel.MODULE.value, cs.KEY_QUALIFIED_NAME, "proj.doc"),
            cs.RelationshipType.CONTAINS_SECTION.value,
            (cs.NodeLabel.SECTION.value, cs.KEY_QUALIFIED_NAME, "proj.doc.Top"),
        )
        store.ensure_relationship_batch(
            (cs.NodeLabel.SECTION.value, cs.KEY_QUALIFIED_NAME, "proj.doc.Top"),
            cs.RelationshipType.CONTAINS_SECTION.value,
            (cs.NodeLabel.SECTION.value, cs.KEY_QUALIFIED_NAME, "proj.doc.Top.Nested"),
        )

        store._delete_module_subtree("doc.md")

        survivors = {
            uid
            for _label, uid in store._nodes_at_path(
                cs.NodeLabel.SECTION.value, "doc.md"
            )
        }
        assert survivors == set(), (
            f"subsections outlived the re-parse: {sorted(survivors)}"
        )
