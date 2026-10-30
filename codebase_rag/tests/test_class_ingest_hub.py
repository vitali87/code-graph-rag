"""`ClassIngest`, the class pass hub: what writing a class records (its
node, registry entry, simple names and declaring module), the bases it
holds back until `resolve_deferred_inherits`, a Rust file's inline modules,
the override walk with the implementer pairs read back from the graph, and
the collaborator each language is routed to."""

from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parsers.class_ingest.ingest import ClassIngest
from codebase_rag.parsers.language_definitions import LanguageClassIngest
from codebase_rag.tests.collaborator_support import edges, index, parse
from codebase_rag.tests.conftest import get_nodes, get_relationships
from evals.recorded_graph import RecordingIngestor

_FILES = {
    "pkg/__init__.py": "",
    "pkg/base.py": "class Base:\n    def run(self):\n        return 0\n",
    "pkg/child.py": (
        "from pkg.base import Base\n\n"
        "class Child(Base):\n"
        "    def run(self):\n"
        "        return 1\n"
    ),
    "Runner.java": "interface Runner { void run(); }\n",
    "Impl.java": "class Impl implements Runner { public void run() {} }\n",
    # No in-class declaration of Inner: only the qualified name names it.
    "shapes.cpp": "class Outer {};\nclass Outer::Inner { int x; };\n",
}


@pytest.fixture
def updater(temp_repo: Path, mock_ingestor: RecordingIngestor) -> GraphUpdater:
    return index(temp_repo / "proj", mock_ingestor, _FILES)


def _class_ingest(updater: GraphUpdater) -> ClassIngest:
    ingest = updater.factory.definition_processor.class_ingest
    assert isinstance(ingest, ClassIngest)
    return ingest


def test_a_written_class_is_registered_indexed_and_owned_by_its_module(
    updater: GraphUpdater, mock_ingestor: RecordingIngestor
) -> None:
    ingest = _class_ingest(updater)
    qn = "proj.pkg.child.Child"
    (node,) = [
        n
        for n in get_nodes(mock_ingestor, cs.NodeLabel.CLASS)
        if n.properties[cs.KEY_QUALIFIED_NAME] == qn
    ]
    assert {
        key: node.properties[key]
        for key in (cs.KEY_NAME, cs.KEY_PATH, cs.KEY_START_LINE, cs.KEY_END_LINE)
    } == {
        cs.KEY_NAME: "Child",
        cs.KEY_PATH: "pkg/child.py",
        cs.KEY_START_LINE: 3,
        cs.KEY_END_LINE: 5,
    }
    assert ingest.function_registry.get(qn) == cs.NodeLabel.CLASS
    assert ingest.simple_name_lookup["Child"] == {qn}
    # The class-keyed maps are pruned by this when the file goes away.
    assert ingest.class_owner_module[qn] == "proj.pkg.child"
    assert ("proj.pkg.child", qn) in edges(mock_ingestor, cs.RelationshipType.DEFINES)


def test_an_out_of_class_cpp_definition_is_indexed_under_its_leaf_too(
    updater: GraphUpdater,
) -> None:
    lookup = _class_ingest(updater).simple_name_lookup
    # `bool Inner::m()` (or a `using Inner = Outer::Inner;`) looks the leaf up.
    assert lookup["Outer::Inner"] == {"proj.shapes.Outer::Inner"}
    assert lookup["Inner"] == {"proj.shapes.Outer::Inner"}


def test_a_base_waits_for_the_deferred_pass_and_then_replaces_its_guess(
    updater: GraphUpdater, mock_ingestor: RecordingIngestor
) -> None:
    ingest = _class_ingest(updater)
    # The run drained its own queue.
    assert ingest.resolve_deferred_inherits() == 0
    mock_ingestor.reset_mock()
    root = parse(
        cs.SupportedLanguage.PYTHON,
        "from pkg.base import Base\nclass Late(Base):\n    def run(self):\n        return 2\n",
    )
    ingest.ingest_classes_and_methods(
        root, "proj.late", cs.SupportedLanguage.PYTHON, updater.queries
    )
    # Parse time sees no import map for the module: the base is a guess.
    assert ingest.facts.class_inheritance["proj.late.Late"] == ["proj.late.Base"]
    assert get_relationships(mock_ingestor, cs.RelationshipType.INHERITS) == []
    assert ingest.resolve_deferred_inherits() == 1
    assert ingest.resolve_deferred_inherits() == 0
    assert edges(mock_ingestor, cs.RelationshipType.INHERITS) == {
        ("proj.late.Late", "proj.pkg.base.Base")
    }
    assert ingest.facts.class_inheritance["proj.late.Late"] == ["proj.pkg.base.Base"]


def test_a_rust_inline_module_is_written_before_the_items_in_it(
    updater: GraphUpdater, mock_ingestor: RecordingIngestor
) -> None:
    mock_ingestor.reset_mock()
    root = parse(cs.SupportedLanguage.RUST, "mod inner { pub struct S; }\n")
    _class_ingest(updater).ingest_inline_modules(
        root, "proj.lib", cs.SupportedLanguage.RUST, updater.queries
    )
    assert [n.properties[cs.KEY_QUALIFIED_NAME] for n in get_nodes(mock_ingestor)] == [
        "proj.lib.inner"
    ]
    # A language with no inline modules writes nothing.
    mock_ingestor.reset_mock()
    _class_ingest(updater).ingest_inline_modules(
        parse(cs.SupportedLanguage.PYTHON, "class A: ...\n"),
        "proj.a",
        cs.SupportedLanguage.PYTHON,
        updater.queries,
    )
    assert get_nodes(mock_ingestor) == []


def test_an_override_is_linked_to_the_base_method_it_replaces(
    mock_ingestor: RecordingIngestor, updater: GraphUpdater
) -> None:
    assert ("proj.pkg.child.Child.run", "proj.pkg.base.Base.run") in edges(
        mock_ingestor, cs.RelationshipType.OVERRIDES
    )


def test_read_back_implementer_pairs_join_the_walk_unless_their_language_opts_out(
    updater: GraphUpdater, mock_ingestor: RecordingIngestor
) -> None:
    ingest = _class_ingest(updater)
    override = ("proj.Impl.Impl.run()", "proj.Runner.Runner.run()")
    # As if Impl.java were unchanged: its pair is known only from the graph.
    ingest.facts.interface_implementers.clear()
    ingest.facts.rehydrated_interface_implementers["proj.Runner.Runner"] = {
        "proj.Impl.Impl"
    }
    for path, joined in (("Impl.java", True), ("impl.go", False)):
        ingest.facts.rehydrated_definition_paths["proj.Impl.Impl"] = path
        mock_ingestor.reset_mock()
        ingest.process_all_method_overrides()
        assert (
            override in edges(mock_ingestor, cs.RelationshipType.OVERRIDES)
        ) is joined, path


def test_each_language_is_routed_to_its_own_collaborator(updater: GraphUpdater) -> None:
    ingest = _class_ingest(updater)
    assert ingest._for(cs.SupportedLanguage.PYTHON) is ingest.python
    # One collaborator serves every JS/TS grammar.
    assert {id(ingest._for(lang)) for lang in cs.JS_TS_LANGUAGES} == {id(ingest.js_ts)}
    neutral = ingest._for(None)
    assert type(neutral) is LanguageClassIngest
    assert ingest._for(cs.SupportedLanguage.LUA) is neutral
