"""`PhpClassIngest` records the namespace around a PHP class's declaration,
which other files `use` the class by."""

from __future__ import annotations

from pathlib import Path

from codebase_rag import constants as cs
from codebase_rag.parsers.php.class_ingest import PhpClassIngest
from codebase_rag.tests.collaborator_support import first, index, parse
from codebase_rag.tests.conftest import get_nodes
from codebase_rag.types_defs import PropertyDict
from evals.recorded_graph import RecordingIngestor


def _class_props(source: str) -> PropertyDict:
    root = parse(cs.SupportedLanguage.PHP, source)
    props: PropertyDict = {}
    PhpClassIngest().record_type_props(
        first(root, "class_declaration"), "p.Box.Box", "Box", cs.NodeLabel.CLASS, props
    )
    return props


def test_a_class_records_the_namespace_around_its_declaration() -> None:
    source = "<?php\nnamespace App\\Models;\n\nclass Box {}\n"
    assert _class_props(source) == {cs.KEY_NAMESPACE: "App.Models"}


def test_a_braced_namespace_block_is_the_classs_namespace() -> None:
    source = (
        "<?php\nnamespace App\\A { class One {} }\nnamespace App\\B { class Two {} }\n"
    )
    root = parse(cs.SupportedLanguage.PHP, source)
    recorded = []
    for block in (n for n in root.children if n.type == "namespace_definition"):
        props: PropertyDict = {}
        PhpClassIngest().record_type_props(
            first(block, "class_declaration"),
            "p.f.C",
            "C",
            cs.NodeLabel.CLASS,
            props,
        )
        recorded.append(props.get(cs.KEY_NAMESPACE))
    assert recorded == ["App.A", "App.B"]


def test_a_class_outside_any_namespace_records_none() -> None:
    assert _class_props("<?php\nclass Box {}\n") == {}


def test_the_class_node_carries_the_namespace(
    temp_repo: Path, mock_ingestor: RecordingIngestor
) -> None:
    index(
        temp_repo / "proj",
        mock_ingestor,
        {"src/Box.php": "<?php\nnamespace App\\Models;\nclass Box {}\n"},
    )
    (box,) = get_nodes(mock_ingestor, cs.NodeLabel.CLASS)
    assert box.properties[cs.KEY_QUALIFIED_NAME] == "proj.src.Box.Box"
    assert box.properties[cs.KEY_NAMESPACE] == "App.Models"
