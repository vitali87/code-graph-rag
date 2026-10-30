"""`PhpFileIngest` stores the namespace a PHP file declares on its Module
node."""

from __future__ import annotations

from pathlib import Path

from codebase_rag import constants as cs
from codebase_rag.parsers.php.file_ingest import PhpFileIngest
from codebase_rag.tests.collaborator_support import index, parse
from codebase_rag.tests.conftest import get_nodes
from codebase_rag.types_defs import PropertyDict
from evals.recorded_graph import RecordingIngestor


def _module_props(source: str) -> PropertyDict:
    props: PropertyDict = {}
    PhpFileIngest().record_module_props(parse(cs.SupportedLanguage.PHP, source), props)
    return props


def test_a_file_records_the_namespace_it_declares() -> None:
    props = _module_props("<?php\nnamespace App\\Http\\Controllers;\n")
    assert props == {cs.KEY_PHP_NAMESPACE: "App.Http.Controllers"}


def test_a_file_without_a_namespace_records_none() -> None:
    assert _module_props("<?php\n$x = 1;\n") == {}


def test_the_module_node_carries_the_namespace(
    temp_repo: Path, mock_ingestor: RecordingIngestor
) -> None:
    index(
        temp_repo / "proj",
        mock_ingestor,
        {
            "src/a.php": "<?php\nnamespace App\\A;\nfunction f() {}\n",
            "src/b.php": "<?php\nfunction g() {}\n",
        },
    )
    namespaces = {
        n.properties[cs.KEY_QUALIFIED_NAME]: n.properties.get(cs.KEY_PHP_NAMESPACE)
        for n in get_nodes(mock_ingestor, cs.NodeLabel.MODULE)
    }
    assert namespaces == {"proj.src.a": "App.A", "proj.src.b": None}
