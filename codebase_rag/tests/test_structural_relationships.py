"""Test structural relationships: CONTAINS_PACKAGE, CONTAINS_FOLDER, CONTAINS_FILE, DEPENDS_ON_EXTERNAL."""

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import toml

from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers


@pytest.fixture
def complex_project(tmp_path: Path) -> Path:
    """Create a complex project structure with nested packages, folders, and files."""
    project_path = tmp_path / "complex_project"
    project_path.mkdir()

    # Root level files
    (project_path / "README.md").write_text("# Complex Project")
    (project_path / ".gitignore").write_text("*.pyc\n__pycache__/")
    (project_path / "LICENSE").write_text("MIT License")

    # Python package structure
    python_pkg = project_path / "mypackage"
    python_pkg.mkdir()
    (python_pkg / "__init__.py").write_text("__version__ = '1.0.0'")
    (python_pkg / "core.py").write_text("def main(): pass")

    # Nested subpackage
    subpkg = python_pkg / "utils"
    subpkg.mkdir()
    (subpkg / "__init__.py").write_text("")
    (subpkg / "helpers.py").write_text("def helper(): pass")
    (subpkg / "constants.py").write_text("VERSION = '1.0'")

    # Deep nested subpackage
    deep_pkg = subpkg / "deep"
    deep_pkg.mkdir()
    (deep_pkg / "__init__.py").write_text("")
    (deep_pkg / "nested.py").write_text("class NestedClass: pass")

    # JavaScript/Node.js structure
    js_dir = project_path / "frontend"
    js_dir.mkdir()
    (js_dir / "package.json").write_text('{"name": "frontend", "version": "1.0.0"}')
    (js_dir / "index.js").write_text("console.log('hello');")

    # JS subdirectories
    js_src = js_dir / "src"
    js_src.mkdir()
    (js_src / "app.js").write_text("function app() {}")
    (js_src / "utils.js").write_text("export const util = {};")

    js_components = js_src / "components"
    js_components.mkdir()
    (js_components / "Button.jsx").write_text("export default function Button() {}")
    (js_components / "Modal.tsx").write_text(
        "interface Props {} export default function Modal() {}"
    )

    # Regular folders (not packages)
    docs_dir = project_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "api.md").write_text("# API Documentation")
    (docs_dir / "tutorial.rst").write_text("Tutorial")

    # Nested regular folders
    guides_dir = docs_dir / "guides"
    guides_dir.mkdir()
    (guides_dir / "setup.md").write_text("# Setup Guide")
    (guides_dir / "advanced.md").write_text("# Advanced Guide")

    # Config folder
    config_dir = project_path / "config"
    config_dir.mkdir()
    (config_dir / "settings.yaml").write_text("debug: true")
    (config_dir / "database.ini").write_text("[database]\nhost=localhost")

    # Mixed content folder
    assets_dir = project_path / "assets"
    assets_dir.mkdir()
    (assets_dir / "logo.png").write_text("fake png content")
    (assets_dir / "style.css").write_text("body { margin: 0; }")

    assets_images = assets_dir / "images"
    assets_images.mkdir()
    (assets_images / "hero.jpg").write_text("fake jpg content")
    (assets_images / "icon.svg").write_text("<svg></svg>")

    # Tests folder (another Python package)
    tests_dir = project_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "__init__.py").write_text("")
    (tests_dir / "test_core.py").write_text("def test_main(): pass")

    test_unit = tests_dir / "unit"
    test_unit.mkdir()
    (test_unit / "__init__.py").write_text("")
    (test_unit / "test_utils.py").write_text("def test_helper(): pass")

    return project_path


@pytest.fixture
def dependency_project(tmp_path: Path) -> Path:
    """Create a project with various dependency files."""
    project_path = tmp_path / "dependency_project"
    project_path.mkdir()

    # Python dependencies
    (project_path / "requirements.txt").write_text(
        "flask>=2.0.0\nrequests==2.28.1\npytest>=7.0\nblack~=22.0\nmypy>=1.0.0,<2.0.0\n"
    )

    (project_path / "pyproject.toml").write_text(
        toml.dumps(
            {
                "build-system": {
                    "requires": ["setuptools>=45", "wheel"],
                    "build-backend": "setuptools.build_meta",
                },
                "project": {
                    "name": "my-project",
                    "version": "0.1.0",
                    "dependencies": ["click>=8.0", "pydantic>=1.9"],
                    "optional-dependencies": {
                        "dev": ["pre-commit>=2.20", "ruff>=0.1"],
                        "test": ["coverage>=6.0"],
                    },
                },
            }
        )
    )

    # Node.js dependencies
    (project_path / "package.json").write_text(
        json.dumps(
            {
                "name": "my-app",
                "version": "1.0.0",
                "dependencies": {
                    "react": "^18.2.0",
                    "axios": "~1.4.0",
                    "lodash": "4.17.21",
                },
                "devDependencies": {
                    "typescript": "^5.0.0",
                    "eslint": ">=8.0.0",
                    "@types/react": "^18.0.0",
                },
                "peerDependencies": {"react-dom": "^18.2.0"},
            },
            indent=2,
        )
    )

    # Rust dependencies
    (project_path / "Cargo.toml").write_text(
        toml.dumps(
            {
                "package": {
                    "name": "my-rust-app",
                    "version": "0.1.0",
                    "edition": "2021",
                },
                "dependencies": {
                    "serde": {"version": "1.0", "features": ["derive"]},
                    "tokio": {"version": "1.0", "features": ["full"]},
                    "clap": "4.0",
                },
                "dev-dependencies": {"criterion": "0.5"},
            }
        )
    )

    # Go dependencies
    (project_path / "go.mod").write_text(
        "module example.com/myapp\n\n"
        "go 1.20\n\n"
        "require (\n"
        "\tgithub.com/gin-gonic/gin v1.9.1\n"
        "\tgithub.com/stretchr/testify v1.8.4\n"
        ")\n\n"
        "require (\n"
        "\tgithub.com/bytedance/sonic v1.9.1 // indirect\n"
        "\tgithub.com/gabriel-vasile/mimetype v1.4.2 // indirect\n"
        ")\n"
    )

    # Ruby dependencies
    (project_path / "Gemfile").write_text(
        'source "https://rubygems.org"\n\n'
        'ruby "3.2.0"\n\n'
        'gem "rails", "~> 7.0.0"\n'
        'gem "pg", ">= 1.1"\n'
        'gem "bootsnap", require: false\n\n'
        "group :development, :test do\n"
        '  gem "rspec-rails"\n'
        '  gem "factory_bot_rails"\n'
        "end\n\n"
        "group :development do\n"
        '  gem "rubocop"\n'
        "end\n"
    )

    # PHP dependencies
    (project_path / "composer.json").write_text(
        json.dumps(
            {
                "name": "vendor/my-php-app",
                "type": "project",
                "require": {
                    "php": ">=8.1",
                    "symfony/console": "^6.0",
                    "doctrine/orm": "~2.14",
                },
                "require-dev": {"phpunit/phpunit": "^10.0", "phpstan/phpstan": "^1.10"},
                "autoload": {"psr-4": {"App\\": "src/"}},
            },
            indent=2,
        )
    )

    # .NET dependencies
    (project_path / "MyApp.csproj").write_text(
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<Project Sdk="Microsoft.NET.Sdk">\n'
        "  <PropertyGroup>\n"
        "    <TargetFramework>net7.0</TargetFramework>\n"
        "    <ImplicitUsings>enable</ImplicitUsings>\n"
        "    <Nullable>enable</Nullable>\n"
        "  </PropertyGroup>\n"
        "  <ItemGroup>\n"
        '    <PackageReference Include="Newtonsoft.Json" Version="13.0.3" />\n'
        '    <PackageReference Include="Microsoft.Extensions.Logging" Version="7.0.0" />\n'
        '    <PackageReference Include="Serilog" Version="3.0.1" />\n'
        "  </ItemGroup>\n"
        "  <ItemGroup Condition=\"'$(Configuration)' == 'Debug'\">\n"
        '    <PackageReference Include="Microsoft.Extensions.Logging.Debug" Version="7.0.0" />\n'
        "  </ItemGroup>\n"
        "</Project>\n"
    )

    return project_path


def test_contains_package_relationships(
    complex_project: Path, mock_ingestor: MagicMock
) -> None:
    """Test that CONTAINS_PACKAGE relationships are created correctly."""
    parsers, queries = load_parsers()

    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=complex_project,
        parsers=parsers,
        queries=queries,
    )
    updater.run()

    project_name = complex_project.name

    # Expected CONTAINS_PACKAGE relationships
    expected_package_relationships = [
        # Root project contains top-level packages
        (
            ("Project", "name", project_name),
            ("Package", "qualified_name", f"{project_name}.mypackage"),
        ),
        (
            ("Project", "name", project_name),
            ("Package", "qualified_name", f"{project_name}.tests"),
        ),
        # Package contains subpackages
        (
            ("Package", "qualified_name", f"{project_name}.mypackage"),
            ("Package", "qualified_name", f"{project_name}.mypackage.utils"),
        ),
        (
            ("Package", "qualified_name", f"{project_name}.mypackage.utils"),
            ("Package", "qualified_name", f"{project_name}.mypackage.utils.deep"),
        ),
        (
            ("Package", "qualified_name", f"{project_name}.tests"),
            ("Package", "qualified_name", f"{project_name}.tests.unit"),
        ),
    ]

    # Get all CONTAINS_PACKAGE relationships
    package_relationships = [
        call
        for call in mock_ingestor.ensure_relationship_batch.call_args_list
        if len(call[0]) >= 3 and call[0][1] == "CONTAINS_PACKAGE"
    ]

    for expected_parent, expected_child in expected_package_relationships:
        found = any(
            call[0][0] == expected_parent and call[0][2] == expected_child
            for call in package_relationships
        )
        assert found, (
            f"Missing CONTAINS_PACKAGE relationship: "
            f"{expected_parent[2]} CONTAINS_PACKAGE {expected_child[2]}"
        )


def test_contains_folder_relationships(
    complex_project: Path, mock_ingestor: MagicMock
) -> None:
    """Test that CONTAINS_FOLDER relationships are created correctly."""
    parsers, queries = load_parsers()

    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=complex_project,
        parsers=parsers,
        queries=queries,
    )
    updater.run()

    project_name = complex_project.name

    # Expected CONTAINS_FOLDER relationships (for non-package directories)
    expected_folder_relationships = [
        # Root project contains top-level folders
        (
            ("Project", "name", project_name),
            ("Folder", "path", "frontend"),
        ),
        (
            ("Project", "name", project_name),
            ("Folder", "path", "docs"),
        ),
        (
            ("Project", "name", project_name),
            ("Folder", "path", "config"),
        ),
        (
            ("Project", "name", project_name),
            ("Folder", "path", "assets"),
        ),
        # Nested folder relationships
        (
            ("Folder", "path", "frontend"),
            ("Folder", "path", "frontend/src"),
        ),
        (
            ("Folder", "path", "frontend/src"),
            ("Folder", "path", "frontend/src/components"),
        ),
        (
            ("Folder", "path", "docs"),
            ("Folder", "path", "docs/guides"),
        ),
        (
            ("Folder", "path", "assets"),
            ("Folder", "path", "assets/images"),
        ),
    ]

    # Get all CONTAINS_FOLDER relationships
    folder_relationships = [
        call
        for call in mock_ingestor.ensure_relationship_batch.call_args_list
        if len(call[0]) >= 3 and call[0][1] == "CONTAINS_FOLDER"
    ]

    for expected_parent, expected_child in expected_folder_relationships:
        found = any(
            call[0][0] == expected_parent and call[0][2] == expected_child
            for call in folder_relationships
        )
        assert found, (
            f"Missing CONTAINS_FOLDER relationship: "
            f"{expected_parent[2]} CONTAINS_FOLDER {expected_child[2]}"
        )


def test_contains_file_relationships(
    complex_project: Path, mock_ingestor: MagicMock
) -> None:
    """Test that CONTAINS_FILE relationships are created correctly."""
    parsers, queries = load_parsers()

    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=complex_project,
        parsers=parsers,
        queries=queries,
    )
    updater.run()

    project_name = complex_project.name

    # Expected CONTAINS_FILE relationships
    expected_file_relationships = [
        # Root project contains root-level files
        (
            ("Project", "name", project_name),
            ("File", "path", "README.md"),
        ),
        (
            ("Project", "name", project_name),
            ("File", "path", ".gitignore"),
        ),
        (
            ("Project", "name", project_name),
            ("File", "path", "LICENSE"),
        ),
        # Packages contain module files
        (
            ("Package", "qualified_name", f"{project_name}.mypackage"),
            ("File", "path", "mypackage/__init__.py"),
        ),
        (
            ("Package", "qualified_name", f"{project_name}.mypackage"),
            ("File", "path", "mypackage/core.py"),
        ),
        (
            ("Package", "qualified_name", f"{project_name}.mypackage.utils"),
            ("File", "path", "mypackage/utils/__init__.py"),
        ),
        (
            ("Package", "qualified_name", f"{project_name}.mypackage.utils"),
            ("File", "path", "mypackage/utils/helpers.py"),
        ),
        (
            ("Package", "qualified_name", f"{project_name}.mypackage.utils"),
            ("File", "path", "mypackage/utils/constants.py"),
        ),
        (
            ("Package", "qualified_name", f"{project_name}.mypackage.utils.deep"),
            ("File", "path", "mypackage/utils/deep/__init__.py"),
        ),
        (
            ("Package", "qualified_name", f"{project_name}.mypackage.utils.deep"),
            ("File", "path", "mypackage/utils/deep/nested.py"),
        ),
        # Folders contain various files
        (
            ("Folder", "path", "frontend"),
            ("File", "path", "frontend/package.json"),
        ),
        (
            ("Folder", "path", "frontend"),
            ("File", "path", "frontend/index.js"),
        ),
        (
            ("Folder", "path", "frontend/src"),
            ("File", "path", "frontend/src/app.js"),
        ),
        (
            ("Folder", "path", "frontend/src"),
            ("File", "path", "frontend/src/utils.js"),
        ),
        (
            ("Folder", "path", "frontend/src/components"),
            ("File", "path", "frontend/src/components/Button.jsx"),
        ),
        (
            ("Folder", "path", "frontend/src/components"),
            ("File", "path", "frontend/src/components/Modal.tsx"),
        ),
        (
            ("Folder", "path", "docs"),
            ("File", "path", "docs/api.md"),
        ),
        (
            ("Folder", "path", "docs"),
            ("File", "path", "docs/tutorial.rst"),
        ),
        (
            ("Folder", "path", "docs/guides"),
            ("File", "path", "docs/guides/setup.md"),
        ),
        (
            ("Folder", "path", "docs/guides"),
            ("File", "path", "docs/guides/advanced.md"),
        ),
        (
            ("Folder", "path", "config"),
            ("File", "path", "config/settings.yaml"),
        ),
        (
            ("Folder", "path", "config"),
            ("File", "path", "config/database.ini"),
        ),
        (
            ("Folder", "path", "assets"),
            ("File", "path", "assets/logo.png"),
        ),
        (
            ("Folder", "path", "assets"),
            ("File", "path", "assets/style.css"),
        ),
        (
            ("Folder", "path", "assets/images"),
            ("File", "path", "assets/images/hero.jpg"),
        ),
        (
            ("Folder", "path", "assets/images"),
            ("File", "path", "assets/images/icon.svg"),
        ),
        # Test package files
        (
            ("Package", "qualified_name", f"{project_name}.tests"),
            ("File", "path", "tests/__init__.py"),
        ),
        (
            ("Package", "qualified_name", f"{project_name}.tests"),
            ("File", "path", "tests/test_core.py"),
        ),
        (
            ("Package", "qualified_name", f"{project_name}.tests.unit"),
            ("File", "path", "tests/unit/__init__.py"),
        ),
        (
            ("Package", "qualified_name", f"{project_name}.tests.unit"),
            ("File", "path", "tests/unit/test_utils.py"),
        ),
    ]

    # Get all CONTAINS_FILE relationships
    file_relationships = [
        call
        for call in mock_ingestor.ensure_relationship_batch.call_args_list
        if len(call[0]) >= 3 and call[0][1] == "CONTAINS_FILE"
    ]

    for expected_parent, expected_file in expected_file_relationships:
        found = any(
            call[0][0] == expected_parent and call[0][2] == expected_file
            for call in file_relationships
        )
        assert found, (
            f"Missing CONTAINS_FILE relationship: "
            f"{expected_parent[2]} CONTAINS_FILE {expected_file[2]}"
        )


def test_depends_on_external_python_requirements(
    dependency_project: Path, mock_ingestor: MagicMock
) -> None:
    """Test that DEPENDS_ON_EXTERNAL relationships are created for Python requirements.txt."""
    parsers, queries = load_parsers()

    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=dependency_project,
        parsers=parsers,
        queries=queries,
    )
    updater.run()

    project_name = dependency_project.name

    # Expected dependencies from requirements.txt
    expected_python_deps = [
        ("flask", ">=2.0.0"),
        ("requests", "==2.28.1"),
        ("pytest", ">=7.0"),
        ("black", "~=22.0"),
        ("mypy", ">=1.0.0,<2.0.0"),
    ]

    # Get all DEPENDS_ON_EXTERNAL relationships
    dependency_relationships = [
        call
        for call in mock_ingestor.ensure_relationship_batch.call_args_list
        if len(call[0]) >= 3 and call[0][1] == "DEPENDS_ON_EXTERNAL"
    ]

    for dep_name, version_spec in expected_python_deps:
        found = any(
            (
                call[0][0] == ("Project", "name", project_name)
                and call[0][2] == ("ExternalPackage", "name", dep_name)
                and call[1].get("properties", {}).get("version_spec") == version_spec
            )
            for call in dependency_relationships
        )
        assert found, (
            f"Missing Python dependency: {project_name} DEPENDS_ON_EXTERNAL {dep_name} "
            f"(version: {version_spec})"
        )


def test_depends_on_external_pyproject_toml(
    dependency_project: Path, mock_ingestor: MagicMock
) -> None:
    """Test that DEPENDS_ON_EXTERNAL relationships are created for pyproject.toml."""
    parsers, queries = load_parsers()

    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=dependency_project,
        parsers=parsers,
        queries=queries,
    )
    updater.run()

    project_name = dependency_project.name

    # Expected dependencies from pyproject.toml
    expected_pyproject_deps = [
        "click",
        "pydantic",
        "pre-commit",
        "ruff",
        "coverage",
    ]

    dependency_relationships = [
        call
        for call in mock_ingestor.ensure_relationship_batch.call_args_list
        if len(call[0]) >= 3 and call[0][1] == "DEPENDS_ON_EXTERNAL"
    ]

    for dep_name in expected_pyproject_deps:
        found = any(
            (
                call[0][0] == ("Project", "name", project_name)
                and call[0][2] == ("ExternalPackage", "name", dep_name)
            )
            for call in dependency_relationships
        )
        assert found, (
            f"Missing pyproject.toml dependency: {project_name} DEPENDS_ON_EXTERNAL {dep_name}"
        )


def test_depends_on_external_package_json(
    dependency_project: Path, mock_ingestor: MagicMock
) -> None:
    """Test that DEPENDS_ON_EXTERNAL relationships are created for package.json."""
    parsers, queries = load_parsers()

    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=dependency_project,
        parsers=parsers,
        queries=queries,
    )
    updater.run()

    project_name = dependency_project.name

    # Expected dependencies from package.json
    expected_npm_deps = [
        ("react", "^18.2.0"),
        ("axios", "~1.4.0"),
        ("lodash", "4.17.21"),
        ("typescript", "^5.0.0"),
        ("eslint", ">=8.0.0"),
        ("@types/react", "^18.0.0"),
        ("react-dom", "^18.2.0"),  # peerDependency
    ]

    dependency_relationships = [
        call
        for call in mock_ingestor.ensure_relationship_batch.call_args_list
        if len(call[0]) >= 3 and call[0][1] == "DEPENDS_ON_EXTERNAL"
    ]

    for dep_name, version_spec in expected_npm_deps:
        found = any(
            (
                call[0][0] == ("Project", "name", project_name)
                and call[0][2] == ("ExternalPackage", "name", dep_name)
                and call[1].get("properties", {}).get("version_spec") == version_spec
            )
            for call in dependency_relationships
        )
        assert found, (
            f"Missing Node.js dependency: {project_name} DEPENDS_ON_EXTERNAL {dep_name} "
            f"(version: {version_spec})"
        )


def test_depends_on_external_cargo_toml(
    dependency_project: Path, mock_ingestor: MagicMock
) -> None:
    """Test that DEPENDS_ON_EXTERNAL relationships are created for Cargo.toml."""
    parsers, queries = load_parsers()

    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=dependency_project,
        parsers=parsers,
        queries=queries,
    )
    updater.run()

    project_name = dependency_project.name

    # Expected dependencies from Cargo.toml
    expected_rust_deps = [
        "serde",
        "tokio",
        "clap",
        "criterion",  # dev-dependency
    ]

    dependency_relationships = [
        call
        for call in mock_ingestor.ensure_relationship_batch.call_args_list
        if len(call[0]) >= 3 and call[0][1] == "DEPENDS_ON_EXTERNAL"
    ]

    for dep_name in expected_rust_deps:
        found = any(
            (
                call[0][0] == ("Project", "name", project_name)
                and call[0][2] == ("ExternalPackage", "name", dep_name)
            )
            for call in dependency_relationships
        )
        assert found, (
            f"Missing Rust dependency: {project_name} DEPENDS_ON_EXTERNAL {dep_name}"
        )


def test_depends_on_external_go_mod(
    dependency_project: Path, mock_ingestor: MagicMock
) -> None:
    """Test that DEPENDS_ON_EXTERNAL relationships are created for go.mod."""
    parsers, queries = load_parsers()

    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=dependency_project,
        parsers=parsers,
        queries=queries,
    )
    updater.run()

    project_name = dependency_project.name

    # Expected dependencies from go.mod
    expected_go_deps = [
        "github.com/gin-gonic/gin",
        "github.com/stretchr/testify",
        "github.com/bytedance/sonic",
        "github.com/gabriel-vasile/mimetype",
    ]

    dependency_relationships = [
        call
        for call in mock_ingestor.ensure_relationship_batch.call_args_list
        if len(call[0]) >= 3 and call[0][1] == "DEPENDS_ON_EXTERNAL"
    ]

    for dep_name in expected_go_deps:
        found = any(
            (
                call[0][0] == ("Project", "name", project_name)
                and call[0][2] == ("ExternalPackage", "name", dep_name)
            )
            for call in dependency_relationships
        )
        assert found, (
            f"Missing Go dependency: {project_name} DEPENDS_ON_EXTERNAL {dep_name}"
        )


def test_depends_on_external_gemfile(
    dependency_project: Path, mock_ingestor: MagicMock
) -> None:
    """Test that DEPENDS_ON_EXTERNAL relationships are created for Gemfile."""
    parsers, queries = load_parsers()

    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=dependency_project,
        parsers=parsers,
        queries=queries,
    )
    updater.run()

    project_name = dependency_project.name

    # Expected dependencies from Gemfile
    expected_ruby_deps = [
        "rails",
        "pg",
        "bootsnap",
        "rspec-rails",
        "factory_bot_rails",
        "rubocop",
    ]

    dependency_relationships = [
        call
        for call in mock_ingestor.ensure_relationship_batch.call_args_list
        if len(call[0]) >= 3 and call[0][1] == "DEPENDS_ON_EXTERNAL"
    ]

    for dep_name in expected_ruby_deps:
        found = any(
            (
                call[0][0] == ("Project", "name", project_name)
                and call[0][2] == ("ExternalPackage", "name", dep_name)
            )
            for call in dependency_relationships
        )
        assert found, (
            f"Missing Ruby dependency: {project_name} DEPENDS_ON_EXTERNAL {dep_name}"
        )


def test_depends_on_external_composer_json(
    dependency_project: Path, mock_ingestor: MagicMock
) -> None:
    """Test that DEPENDS_ON_EXTERNAL relationships are created for composer.json."""
    parsers, queries = load_parsers()

    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=dependency_project,
        parsers=parsers,
        queries=queries,
    )
    updater.run()

    project_name = dependency_project.name

    # Expected dependencies from composer.json
    expected_php_deps = [
        "symfony/console",
        "doctrine/orm",
        "phpunit/phpunit",
        "phpstan/phpstan",
    ]

    dependency_relationships = [
        call
        for call in mock_ingestor.ensure_relationship_batch.call_args_list
        if len(call[0]) >= 3 and call[0][1] == "DEPENDS_ON_EXTERNAL"
    ]

    for dep_name in expected_php_deps:
        found = any(
            (
                call[0][0] == ("Project", "name", project_name)
                and call[0][2] == ("ExternalPackage", "name", dep_name)
            )
            for call in dependency_relationships
        )
        assert found, (
            f"Missing PHP dependency: {project_name} DEPENDS_ON_EXTERNAL {dep_name}"
        )


def test_depends_on_external_csproj(
    dependency_project: Path, mock_ingestor: MagicMock
) -> None:
    """Test that DEPENDS_ON_EXTERNAL relationships are created for .csproj files."""
    parsers, queries = load_parsers()

    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=dependency_project,
        parsers=parsers,
        queries=queries,
    )
    updater.run()

    project_name = dependency_project.name

    # Expected dependencies from .csproj file
    expected_dotnet_deps = [
        "Newtonsoft.Json",
        "Microsoft.Extensions.Logging",
        "Serilog",
        "Microsoft.Extensions.Logging.Debug",
    ]

    dependency_relationships = [
        call
        for call in mock_ingestor.ensure_relationship_batch.call_args_list
        if len(call[0]) >= 3 and call[0][1] == "DEPENDS_ON_EXTERNAL"
    ]

    for dep_name in expected_dotnet_deps:
        found = any(
            (
                call[0][0] == ("Project", "name", project_name)
                and call[0][2] == ("ExternalPackage", "name", dep_name)
            )
            for call in dependency_relationships
        )
        assert found, (
            f"Missing .NET dependency: {project_name} DEPENDS_ON_EXTERNAL {dep_name}"
        )


def test_mixed_structure_and_dependencies(
    complex_project: Path, mock_ingestor: MagicMock
) -> None:
    """Test that both structural and dependency relationships coexist correctly."""
    # Add some dependency files to the complex project
    (complex_project / "requirements.txt").write_text("flask>=2.0.0\nrequests==2.28.1")
    (complex_project / "package.json").write_text(
        '{"dependencies": {"react": "^18.0.0"}}'
    )

    parsers, queries = load_parsers()

    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=complex_project,
        parsers=parsers,
        queries=queries,
    )
    updater.run()

    # Verify we have all types of relationships
    all_calls = mock_ingestor.ensure_relationship_batch.call_args_list

    relationship_types = {call[0][1] for call in all_calls if len(call[0]) >= 3}

    expected_types = {
        "CONTAINS_PACKAGE",
        "CONTAINS_FOLDER",
        "CONTAINS_FILE",
        "DEPENDS_ON_EXTERNAL",
    }

    missing_types = expected_types - relationship_types
    assert not missing_types, f"Missing relationship types: {missing_types}"

    # Verify specific relationships exist
    package_calls = [
        call
        for call in all_calls
        if len(call[0]) >= 3 and call[0][1] == "CONTAINS_PACKAGE"
    ]
    folder_calls = [
        call
        for call in all_calls
        if len(call[0]) >= 3 and call[0][1] == "CONTAINS_FOLDER"
    ]
    file_calls = [
        call
        for call in all_calls
        if len(call[0]) >= 3 and call[0][1] == "CONTAINS_FILE"
    ]
    dep_calls = [
        call
        for call in all_calls
        if len(call[0]) >= 3 and call[0][1] == "DEPENDS_ON_EXTERNAL"
    ]

    assert len(package_calls) > 0, "Should have CONTAINS_PACKAGE relationships"
    assert len(folder_calls) > 0, "Should have CONTAINS_FOLDER relationships"
    assert len(file_calls) > 0, "Should have CONTAINS_FILE relationships"
    assert len(dep_calls) > 0, "Should have DEPENDS_ON_EXTERNAL relationships"


def test_edge_cases_empty_folders_and_special_files(
    tmp_path: Path, mock_ingestor: MagicMock
) -> None:
    """Test edge cases like empty folders, hidden files, and special file types."""
    project_path = tmp_path / "edge_case_project"
    project_path.mkdir()

    # Empty folder
    empty_dir = project_path / "empty"
    empty_dir.mkdir()

    # Hidden files and folders
    (project_path / ".env").write_text("SECRET=value")
    (project_path / ".dockerignore").write_text("*.pyc")

    hidden_dir = project_path / ".github"
    hidden_dir.mkdir()
    (hidden_dir / "workflows").mkdir()
    (hidden_dir / "workflows" / "ci.yml").write_text("name: CI")

    # Special file extensions
    (project_path / "Dockerfile").write_text("FROM python:3.11")
    (project_path / "Makefile").write_text("all:\n\techo hello")
    (project_path / "script.sh").write_text("#!/bin/bash\necho hello")

    # Files without extensions
    (project_path / "LICENSE").write_text("MIT License")
    (project_path / "VERSION").write_text("1.0.0")

    parsers, queries = load_parsers()

    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=project_path,
        parsers=parsers,
        queries=queries,
    )
    updater.run()

    project_name = project_path.name

    # Verify relationships are created for edge cases
    all_calls = mock_ingestor.ensure_relationship_batch.call_args_list

    # Check that empty folders are handled
    empty_folder_found = any(
        (
            call[0][0] == ("Project", "name", project_name)
            and call[0][1] == "CONTAINS_FOLDER"
            and call[0][2] == ("Folder", "path", "empty")
        )
        for call in all_calls
    )
    assert empty_folder_found, "Empty folder should be tracked"

    # Check hidden files are handled
    # Note: If project root has package indicators, it becomes a Package not Project
    hidden_file_found = any(
        (
            (
                call[0][0] == ("Project", "name", project_name)
                or call[0][0] == ("Package", "qualified_name", project_name)
            )
            and call[0][1] == "CONTAINS_FILE"
            and call[0][2] == ("File", "path", ".env")
        )
        for call in all_calls
    )
    assert hidden_file_found, "Hidden files should be tracked"

    # Check nested hidden structure
    hidden_workflow_found = any(
        (
            call[0][0] == ("Folder", "path", ".github/workflows")
            and call[0][1] == "CONTAINS_FILE"
            and call[0][2] == ("File", "path", ".github/workflows/ci.yml")
        )
        for call in all_calls
    )
    assert hidden_workflow_found, "Nested hidden files should be tracked"

    # Check special files
    special_files = ["Dockerfile", "Makefile", "script.sh", "LICENSE", "VERSION"]
    for special_file in special_files:
        special_file_found = any(
            (
                (
                    call[0][0] == ("Project", "name", project_name)
                    or call[0][0] == ("Package", "qualified_name", project_name)
                )
                and call[0][1] == "CONTAINS_FILE"
                and call[0][2] == ("File", "path", special_file)
            )
            for call in all_calls
        )
        assert special_file_found, f"Special file {special_file} should be tracked"
