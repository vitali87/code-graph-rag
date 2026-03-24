from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.tests.conftest import get_relationships


def test_php_use_statement_import(temp_repo: Path, mock_ingestor: MagicMock) -> None:
    project_path = temp_repo / "php_imports_test"
    project_path.mkdir()

    (project_path / "Controller.php").write_text(
        encoding="utf-8",
        data="""<?php
namespace App\\Controller;

use App\\Service\\ProductService;
use App\\Repository\\ProductRepository as Repo;

class ProductController {
    public function index() {
        $service = new ProductService();
    }
}
""",
    )

    (project_path / "ProductService.php").write_text(
        encoding="utf-8",
        data="""<?php
namespace App\\Service;

class ProductService {
    public function getAll() { return []; }
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

    import_rels = get_relationships(mock_ingestor, "IMPORTS")
    assert len(import_rels) >= 1

    controller_module = f"{project_path.name}.Controller"
    import_mapping = updater.factory.import_processor.import_mapping
    if controller_module in import_mapping:
        mapping = import_mapping[controller_module]
        assert "ProductService" in mapping
        assert mapping["ProductService"] == "App.Service.ProductService"
        assert "Repo" in mapping
        assert mapping["Repo"] == "App.Repository.ProductRepository"


def test_php_multiple_use_statements(temp_repo: Path, mock_ingestor: MagicMock) -> None:
    project_path = temp_repo / "php_multi_imports"
    project_path.mkdir()

    (project_path / "app.php").write_text(
        encoding="utf-8",
        data="""<?php
use Foo\\Bar;
use Baz\\Qux;

function run() {
    $b = new Bar();
    $q = new Qux();
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

    app_module = f"{project_path.name}.app"
    mapping = updater.factory.import_processor.import_mapping.get(app_module, {})
    assert "Bar" in mapping
    assert "Qux" in mapping
