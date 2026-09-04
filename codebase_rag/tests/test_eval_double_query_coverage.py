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

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
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
        assert "proj.util.helper" in by_qn, sorted(by_qn)
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
        assert "proj.app.Widget.draw" in by_qn, sorted(by_qn)
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
        assert "proj.pkg.types.T" in by_qn, sorted(by_qn)
        assert by_qn["proj.pkg.types.T"][cs.KEY_LABEL] in {
            cs.NodeLabel.CLASS.value,
            cs.NodeLabel.TYPE.value,
        }


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
