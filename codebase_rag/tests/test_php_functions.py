from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.tests.conftest import get_relationships
from codebase_rag.types_defs import NodeType


def test_php_function_discovery(temp_repo: Path, mock_ingestor: MagicMock) -> None:
    project_path = temp_repo / "php_functions_test"
    project_path.mkdir()

    (project_path / "example.php").write_text(
        encoding="utf-8",
        data="""<?php
class MyPhpClass {
    private $value;

    public function __construct() {
        $this->value = 0;
    }

    public function getValue() {
        return $this->value;
    }
}

interface MyInterface {
    public function doSomething();
}

enum Status {
    case Active;
    case Inactive;
}

function standaloneFunction() {
    $obj = new MyPhpClass();
    return $obj->getValue();
}
""",
    )

    parsers, queries = load_parsers()
    assert "php" in parsers, "PHP parser should be available"

    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=project_path,
        parsers=parsers,
        queries=queries,
    )
    updater.run()

    created_functions = [
        c
        for c in mock_ingestor.ensure_node_batch.call_args_list
        if c[0][0] == NodeType.FUNCTION
    ]
    fn_qns = {c[0][1]["qualified_name"] for c in created_functions}

    assert any(qn.endswith(".standaloneFunction") for qn in fn_qns), fn_qns

    call_rels = get_relationships(mock_ingestor, "CALLS")
    assert len(call_rels) >= 1


def test_php_class_discovery(temp_repo: Path, mock_ingestor: MagicMock) -> None:
    project_path = temp_repo / "php_class_test"
    project_path.mkdir()

    (project_path / "models.php").write_text(
        encoding="utf-8",
        data="""<?php
class User {
    public function getName() { return "test"; }
}

interface Serializable {
    public function serialize();
}

trait Loggable {
    public function log() {}
}

enum Role {
    case Admin;
    case User;
}
""",
    )

    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=project_path,
        parsers=parsers,
        queries=queries,
    )
    updater.run()

    created_classes = [
        c
        for c in mock_ingestor.ensure_node_batch.call_args_list
        if c[0][0] == NodeType.CLASS
    ]
    class_names = {c[0][1]["qualified_name"] for c in created_classes}
    assert any("User" in n for n in class_names), class_names

    created_interfaces = [
        c
        for c in mock_ingestor.ensure_node_batch.call_args_list
        if c[0][0] == NodeType.INTERFACE
    ]
    iface_names = {c[0][1]["qualified_name"] for c in created_interfaces}
    assert any("Serializable" in n for n in iface_names), iface_names


def test_php_method_calls(temp_repo: Path, mock_ingestor: MagicMock) -> None:
    project_path = temp_repo / "php_calls_test"
    project_path.mkdir()

    (project_path / "service.php").write_text(
        encoding="utf-8",
        data="""<?php
class Calculator {
    public function add($a, $b) { return $a + $b; }

    public function calculate() {
        return $this->add(1, 2);
    }
}

function main() {
    $calc = new Calculator();
    $calc->calculate();
}
""",
    )

    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=project_path,
        parsers=parsers,
        queries=queries,
    )
    updater.run()

    call_rels = get_relationships(mock_ingestor, "CALLS")
    assert len(call_rels) >= 2
