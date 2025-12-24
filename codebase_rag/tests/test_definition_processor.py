from pathlib import Path
from unittest.mock import MagicMock

import pytest
from tree_sitter import Language, Parser

from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.tests.conftest import create_mock_node

try:
    import tree_sitter_python as tspython

    PY_AVAILABLE = True
except ImportError:
    PY_AVAILABLE = False


@pytest.fixture
def py_parser() -> Parser | None:
    if not PY_AVAILABLE:
        return None
    language = Language(tspython.language())
    return Parser(language)


@pytest.fixture
def definition_processor(temp_repo: Path, mock_ingestor: MagicMock) -> GraphUpdater:
    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=temp_repo,
        parsers=parsers,
        queries=queries,
    )
    return updater


@pytest.mark.skipif(not PY_AVAILABLE, reason="tree-sitter-python not available")
class TestGetDocstring:
    def test_double_quoted_docstring(self, py_parser: Parser) -> None:
        code = b"""
def my_func():
    "This is a docstring"
    pass
"""
        tree = py_parser.parse(code)
        func_node = tree.root_node.children[0]

        from codebase_rag.parsers.definition_processor import DefinitionProcessor

        processor = DefinitionProcessor.__new__(DefinitionProcessor)
        result = processor._get_docstring(func_node)
        assert result == "This is a docstring"

    def test_single_quoted_docstring(self, py_parser: Parser) -> None:
        code = b"""
def my_func():
    'Single quoted docstring'
    pass
"""
        tree = py_parser.parse(code)
        func_node = tree.root_node.children[0]

        from codebase_rag.parsers.definition_processor import DefinitionProcessor

        processor = DefinitionProcessor.__new__(DefinitionProcessor)
        result = processor._get_docstring(func_node)
        assert result == "Single quoted docstring"

    def test_triple_double_quoted_docstring(self, py_parser: Parser) -> None:
        code = b'''
def my_func():
    """Triple double quoted docstring"""
    pass
'''
        tree = py_parser.parse(code)
        func_node = tree.root_node.children[0]

        from codebase_rag.parsers.definition_processor import DefinitionProcessor

        processor = DefinitionProcessor.__new__(DefinitionProcessor)
        result = processor._get_docstring(func_node)
        assert result == "Triple double quoted docstring"

    def test_triple_single_quoted_docstring(self, py_parser: Parser) -> None:
        code = b"""
def my_func():
    '''Triple single quoted docstring'''
    pass
"""
        tree = py_parser.parse(code)
        func_node = tree.root_node.children[0]

        from codebase_rag.parsers.definition_processor import DefinitionProcessor

        processor = DefinitionProcessor.__new__(DefinitionProcessor)
        result = processor._get_docstring(func_node)
        assert result == "Triple single quoted docstring"

    def test_multiline_docstring(self, py_parser: Parser) -> None:
        code = b'''
def my_func():
    """
    This is a multiline
    docstring with
    multiple lines
    """
    pass
'''
        tree = py_parser.parse(code)
        func_node = tree.root_node.children[0]

        from codebase_rag.parsers.definition_processor import DefinitionProcessor

        processor = DefinitionProcessor.__new__(DefinitionProcessor)
        result = processor._get_docstring(func_node)
        assert result is not None
        assert "multiline" in result
        assert "multiple lines" in result

    def test_no_docstring(self, py_parser: Parser) -> None:
        code = b"""
def my_func():
    x = 1
    return x
"""
        tree = py_parser.parse(code)
        func_node = tree.root_node.children[0]

        from codebase_rag.parsers.definition_processor import DefinitionProcessor

        processor = DefinitionProcessor.__new__(DefinitionProcessor)
        result = processor._get_docstring(func_node)
        assert result is None

    def test_empty_function_body(self, py_parser: Parser) -> None:
        code = b"""
def my_func():
    pass
"""
        tree = py_parser.parse(code)
        func_node = tree.root_node.children[0]

        from codebase_rag.parsers.definition_processor import DefinitionProcessor

        processor = DefinitionProcessor.__new__(DefinitionProcessor)
        result = processor._get_docstring(func_node)
        assert result is None

    def test_class_docstring(self, py_parser: Parser) -> None:
        code = b'''
class MyClass:
    """Class level docstring"""
    def method(self):
        pass
'''
        tree = py_parser.parse(code)
        class_node = tree.root_node.children[0]

        from codebase_rag.parsers.definition_processor import DefinitionProcessor

        processor = DefinitionProcessor.__new__(DefinitionProcessor)
        result = processor._get_docstring(class_node)
        assert result == "Class level docstring"


@pytest.mark.skipif(not PY_AVAILABLE, reason="tree-sitter-python not available")
class TestExtractDecorators:
    def test_single_decorator(self, py_parser: Parser) -> None:
        code = b"""
@decorator
def my_func():
    pass
"""
        tree = py_parser.parse(code)
        decorated_def = tree.root_node.children[0]
        func_node = None
        for child in decorated_def.children:
            if child.type == "function_definition":
                func_node = child
                break

        from codebase_rag.parsers.definition_processor import DefinitionProcessor

        processor = DefinitionProcessor.__new__(DefinitionProcessor)
        result = processor._extract_decorators(func_node)
        assert "decorator" in result

    def test_multiple_decorators(self, py_parser: Parser) -> None:
        code = b"""
@first_decorator
@second_decorator
@third_decorator
def my_func():
    pass
"""
        tree = py_parser.parse(code)
        decorated_def = tree.root_node.children[0]
        func_node = None
        for child in decorated_def.children:
            if child.type == "function_definition":
                func_node = child
                break

        from codebase_rag.parsers.definition_processor import DefinitionProcessor

        processor = DefinitionProcessor.__new__(DefinitionProcessor)
        result = processor._extract_decorators(func_node)
        assert "first_decorator" in result
        assert "second_decorator" in result
        assert "third_decorator" in result

    def test_decorator_with_arguments(self, py_parser: Parser) -> None:
        code = b"""
@decorator_with_args(arg1, arg2)
def my_func():
    pass
"""
        tree = py_parser.parse(code)
        decorated_def = tree.root_node.children[0]
        func_node = None
        for child in decorated_def.children:
            if child.type == "function_definition":
                func_node = child
                break

        from codebase_rag.parsers.definition_processor import DefinitionProcessor

        processor = DefinitionProcessor.__new__(DefinitionProcessor)
        result = processor._extract_decorators(func_node)
        assert "decorator_with_args" in result

    def test_dotted_decorator(self, py_parser: Parser) -> None:
        code = b"""
@module.submodule.decorator
def my_func():
    pass
"""
        tree = py_parser.parse(code)
        decorated_def = tree.root_node.children[0]
        func_node = None
        for child in decorated_def.children:
            if child.type == "function_definition":
                func_node = child
                break

        from codebase_rag.parsers.definition_processor import DefinitionProcessor

        processor = DefinitionProcessor.__new__(DefinitionProcessor)
        result = processor._extract_decorators(func_node)
        assert any("module.submodule.decorator" in d for d in result)

    def test_no_decorators(self, py_parser: Parser) -> None:
        code = b"""
def my_func():
    pass
"""
        tree = py_parser.parse(code)
        func_node = tree.root_node.children[0]

        from codebase_rag.parsers.definition_processor import DefinitionProcessor

        processor = DefinitionProcessor.__new__(DefinitionProcessor)
        result = processor._extract_decorators(func_node)
        assert result == []

    def test_class_decorator(self, py_parser: Parser) -> None:
        code = b"""
@dataclass
class MyClass:
    x: int
"""
        tree = py_parser.parse(code)
        decorated_def = tree.root_node.children[0]
        class_node = None
        for child in decorated_def.children:
            if child.type == "class_definition":
                class_node = child
                break

        from codebase_rag.parsers.definition_processor import DefinitionProcessor

        processor = DefinitionProcessor.__new__(DefinitionProcessor)
        result = processor._extract_decorators(class_node)
        assert "dataclass" in result

    def test_builtin_decorators(self, py_parser: Parser) -> None:
        code = b"""
class MyClass:
    @staticmethod
    def static_method():
        pass

    @classmethod
    def class_method(cls):
        pass

    @property
    def my_property(self):
        return self._value
"""
        tree = py_parser.parse(code)
        class_node = tree.root_node.children[0]
        class_body = class_node.child_by_field_name("body")
        assert class_body is not None

        from codebase_rag.parsers.definition_processor import DefinitionProcessor

        processor = DefinitionProcessor.__new__(DefinitionProcessor)

        for child in class_body.children:
            if child.type == "decorated_definition":
                for sub in child.children:
                    if sub.type == "function_definition":
                        result = processor._extract_decorators(sub)
                        assert len(result) >= 1


@pytest.mark.skipif(not PY_AVAILABLE, reason="tree-sitter-python not available")
class TestGetDecoratorName:
    def test_simple_identifier_decorator(self) -> None:
        node = create_mock_node(
            "decorator",
            children=[
                create_mock_node("@", text="@"),
                create_mock_node("identifier", text="my_decorator"),
            ],
        )

        from codebase_rag.parsers.definition_processor import DefinitionProcessor

        processor = DefinitionProcessor.__new__(DefinitionProcessor)
        result = processor._get_decorator_name(node)
        assert result == "my_decorator"

    def test_call_decorator(self) -> None:
        func_node = create_mock_node("identifier", text="decorator_factory")
        call_node = create_mock_node(
            "call",
            fields={"function": func_node},
            children=[func_node],
        )
        node = create_mock_node(
            "decorator",
            children=[
                create_mock_node("@", text="@"),
                call_node,
            ],
        )

        from codebase_rag.parsers.definition_processor import DefinitionProcessor

        processor = DefinitionProcessor.__new__(DefinitionProcessor)
        result = processor._get_decorator_name(node)
        assert result == "decorator_factory"

    def test_attribute_decorator(self) -> None:
        node = create_mock_node(
            "decorator",
            children=[
                create_mock_node("@", text="@"),
                create_mock_node("attribute", text="module.decorator"),
            ],
        )

        from codebase_rag.parsers.definition_processor import DefinitionProcessor

        processor = DefinitionProcessor.__new__(DefinitionProcessor)
        result = processor._get_decorator_name(node)
        assert result == "module.decorator"

    def test_empty_decorator_returns_none(self) -> None:
        node = create_mock_node(
            "decorator",
            children=[create_mock_node("@", text="@")],
        )

        from codebase_rag.parsers.definition_processor import DefinitionProcessor

        processor = DefinitionProcessor.__new__(DefinitionProcessor)
        result = processor._get_decorator_name(node)
        assert result is None


class TestProcessDependencies:
    def test_pyproject_toml_dependencies(
        self, temp_repo: Path, definition_processor: GraphUpdater
    ) -> None:
        pyproject = temp_repo / "pyproject.toml"
        pyproject.write_text("""
[project]
dependencies = [
    "requests>=2.28.0",
    "pydantic>=2.0",
    "loguru",
]

[project.optional-dependencies]
dev = ["pytest>=7.0", "ruff"]
""")

        definition_processor.factory.definition_processor.process_dependencies(
            pyproject
        )
        definition_processor.ingestor.flush_all()

        node_calls = definition_processor.ingestor.ensure_node_batch.call_args_list
        external_packages = [
            c[0][1]["name"] for c in node_calls if c[0][0] == "ExternalPackage"
        ]

        assert "requests" in external_packages
        assert "pydantic" in external_packages
        assert "loguru" in external_packages
        assert "pytest" in external_packages
        assert "ruff" in external_packages

    def test_requirements_txt_dependencies(
        self, temp_repo: Path, definition_processor: GraphUpdater
    ) -> None:
        requirements = temp_repo / "requirements.txt"
        requirements.write_text("""
# This is a comment
requests>=2.28.0
pydantic==2.5.0
numpy
-e git+https://github.com/example/repo.git#egg=example
flask[async]>=2.0
""")

        definition_processor.factory.definition_processor.process_dependencies(
            requirements
        )
        definition_processor.ingestor.flush_all()

        node_calls = definition_processor.ingestor.ensure_node_batch.call_args_list
        external_packages = [
            c[0][1]["name"] for c in node_calls if c[0][0] == "ExternalPackage"
        ]

        assert "requests" in external_packages
        assert "pydantic" in external_packages
        assert "numpy" in external_packages
        assert "flask" in external_packages

    def test_package_json_dependencies(
        self, temp_repo: Path, definition_processor: GraphUpdater
    ) -> None:
        package_json = temp_repo / "package.json"
        package_json.write_text("""{
  "name": "test-project",
  "dependencies": {
    "express": "^4.18.0",
    "lodash": "4.17.21"
  },
  "devDependencies": {
    "jest": "^29.0.0",
    "typescript": "~5.0.0"
  }
}
""")

        definition_processor.factory.definition_processor.process_dependencies(
            package_json
        )
        definition_processor.ingestor.flush_all()

        node_calls = definition_processor.ingestor.ensure_node_batch.call_args_list
        external_packages = [
            c[0][1]["name"] for c in node_calls if c[0][0] == "ExternalPackage"
        ]

        assert "express" in external_packages
        assert "lodash" in external_packages
        assert "jest" in external_packages
        assert "typescript" in external_packages

    def test_cargo_toml_dependencies(
        self, temp_repo: Path, definition_processor: GraphUpdater
    ) -> None:
        cargo = temp_repo / "Cargo.toml"
        cargo.write_text("""
[package]
name = "test-project"
version = "0.1.0"

[dependencies]
serde = "1.0"
tokio = { version = "1.0", features = ["full"] }
reqwest = { version = "0.11", optional = true }

[dev-dependencies]
criterion = "0.5"
""")

        definition_processor.factory.definition_processor.process_dependencies(cargo)
        definition_processor.ingestor.flush_all()

        node_calls = definition_processor.ingestor.ensure_node_batch.call_args_list
        external_packages = [
            c[0][1]["name"] for c in node_calls if c[0][0] == "ExternalPackage"
        ]

        assert "serde" in external_packages
        assert "tokio" in external_packages
        assert "reqwest" in external_packages
        assert "criterion" in external_packages

    def test_go_mod_dependencies(
        self, temp_repo: Path, definition_processor: GraphUpdater
    ) -> None:
        go_mod = temp_repo / "go.mod"
        go_mod.write_text("""
module github.com/example/project

go 1.21

require (
    github.com/gin-gonic/gin v1.9.0
    github.com/stretchr/testify v1.8.0
)

require github.com/sirupsen/logrus v1.9.0
""")

        definition_processor.factory.definition_processor.process_dependencies(go_mod)
        definition_processor.ingestor.flush_all()

        node_calls = definition_processor.ingestor.ensure_node_batch.call_args_list
        external_packages = [
            c[0][1]["name"] for c in node_calls if c[0][0] == "ExternalPackage"
        ]

        assert "github.com/gin-gonic/gin" in external_packages
        assert "github.com/stretchr/testify" in external_packages
        assert "github.com/sirupsen/logrus" in external_packages

    def test_gemfile_dependencies(
        self, temp_repo: Path, definition_processor: GraphUpdater
    ) -> None:
        gemfile = temp_repo / "Gemfile"
        gemfile.write_text("""
source 'https://rubygems.org'

gem 'rails', '~> 7.0'
gem 'pg', '>= 1.0'
gem 'puma'
gem 'redis', '~> 5.0'
""")

        definition_processor.factory.definition_processor.process_dependencies(gemfile)
        definition_processor.ingestor.flush_all()

        node_calls = definition_processor.ingestor.ensure_node_batch.call_args_list
        external_packages = [
            c[0][1]["name"] for c in node_calls if c[0][0] == "ExternalPackage"
        ]

        assert "rails" in external_packages
        assert "pg" in external_packages
        assert "puma" in external_packages
        assert "redis" in external_packages

    def test_composer_json_dependencies(
        self, temp_repo: Path, definition_processor: GraphUpdater
    ) -> None:
        composer = temp_repo / "composer.json"
        composer.write_text("""{
  "require": {
    "php": ">=8.1",
    "laravel/framework": "^10.0",
    "guzzlehttp/guzzle": "^7.0"
  },
  "require-dev": {
    "phpunit/phpunit": "^10.0"
  }
}
""")

        definition_processor.factory.definition_processor.process_dependencies(composer)
        definition_processor.ingestor.flush_all()

        node_calls = definition_processor.ingestor.ensure_node_batch.call_args_list
        external_packages = [
            c[0][1]["name"] for c in node_calls if c[0][0] == "ExternalPackage"
        ]

        assert "laravel/framework" in external_packages
        assert "guzzlehttp/guzzle" in external_packages
        assert "phpunit/phpunit" in external_packages
        assert "php" not in external_packages

    def test_csproj_dependencies(
        self, temp_repo: Path, definition_processor: GraphUpdater
    ) -> None:
        csproj = temp_repo / "Project.csproj"
        csproj.write_text("""
<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup>
    <TargetFramework>net8.0</TargetFramework>
  </PropertyGroup>
  <ItemGroup>
    <PackageReference Include="Newtonsoft.Json" Version="13.0.3" />
    <PackageReference Include="Serilog" Version="3.0.1" />
    <PackageReference Include="xunit" Version="2.6.0" />
  </ItemGroup>
</Project>
""")

        definition_processor.factory.definition_processor.process_dependencies(csproj)
        definition_processor.ingestor.flush_all()

        node_calls = definition_processor.ingestor.ensure_node_batch.call_args_list
        external_packages = [
            c[0][1]["name"] for c in node_calls if c[0][0] == "ExternalPackage"
        ]

        assert "Newtonsoft.Json" in external_packages
        assert "Serilog" in external_packages
        assert "xunit" in external_packages


class TestAddDependency:
    def test_add_dependency_creates_node_and_relationship(
        self, temp_repo: Path, definition_processor: GraphUpdater
    ) -> None:
        definition_processor.factory.definition_processor._add_dependency(
            "test-package", ">=1.0.0"
        )
        definition_processor.ingestor.flush_all()

        node_calls = definition_processor.ingestor.ensure_node_batch.call_args_list
        external_packages = [c for c in node_calls if c[0][0] == "ExternalPackage"]

        assert len(external_packages) >= 1
        assert external_packages[0][0][1]["name"] == "test-package"

        rel_calls = (
            definition_processor.ingestor.ensure_relationship_batch.call_args_list
        )
        depends_on = [c for c in rel_calls if c[0][1] == "DEPENDS_ON_EXTERNAL"]

        assert len(depends_on) >= 1

    def test_add_dependency_with_properties(
        self, temp_repo: Path, definition_processor: GraphUpdater
    ) -> None:
        definition_processor.factory.definition_processor._add_dependency(
            "dev-package", "^2.0.0", {"group": "dev"}
        )
        definition_processor.ingestor.flush_all()

        rel_calls = (
            definition_processor.ingestor.ensure_relationship_batch.call_args_list
        )
        depends_on = [c for c in rel_calls if c[0][1] == "DEPENDS_ON_EXTERNAL"]

        assert len(depends_on) >= 1
        props = depends_on[0].kwargs.get("properties", {})
        assert props.get("version_spec") == "^2.0.0"
        assert props.get("group") == "dev"

    def test_add_dependency_skips_python(
        self, temp_repo: Path, definition_processor: GraphUpdater
    ) -> None:
        definition_processor.ingestor.reset_mock()

        definition_processor.factory.definition_processor._add_dependency(
            "python", ">=3.8"
        )
        definition_processor.ingestor.flush_all()

        node_calls = definition_processor.ingestor.ensure_node_batch.call_args_list
        external_packages = [c for c in node_calls if c[0][0] == "ExternalPackage"]

        assert len(external_packages) == 0

    def test_add_dependency_skips_php(
        self, temp_repo: Path, definition_processor: GraphUpdater
    ) -> None:
        definition_processor.ingestor.reset_mock()

        definition_processor.factory.definition_processor._add_dependency(
            "php", ">=8.0"
        )
        definition_processor.ingestor.flush_all()

        node_calls = definition_processor.ingestor.ensure_node_batch.call_args_list
        external_packages = [c for c in node_calls if c[0][0] == "ExternalPackage"]

        assert len(external_packages) == 0

    def test_add_dependency_skips_empty_name(
        self, temp_repo: Path, definition_processor: GraphUpdater
    ) -> None:
        definition_processor.ingestor.reset_mock()

        definition_processor.factory.definition_processor._add_dependency("", "1.0.0")
        definition_processor.ingestor.flush_all()

        node_calls = definition_processor.ingestor.ensure_node_batch.call_args_list
        external_packages = [c for c in node_calls if c[0][0] == "ExternalPackage"]

        assert len(external_packages) == 0

    def test_add_dependency_with_empty_version_spec(
        self, temp_repo: Path, definition_processor: GraphUpdater
    ) -> None:
        definition_processor.factory.definition_processor._add_dependency(
            "unversioned-package", ""
        )
        definition_processor.ingestor.flush_all()

        node_calls = definition_processor.ingestor.ensure_node_batch.call_args_list
        external_packages = [c for c in node_calls if c[0][0] == "ExternalPackage"]

        assert len(external_packages) >= 1
        assert external_packages[-1][0][1]["name"] == "unversioned-package"

        rel_calls = (
            definition_processor.ingestor.ensure_relationship_batch.call_args_list
        )
        depends_on = [c for c in rel_calls if c[0][1] == "DEPENDS_ON_EXTERNAL"]
        last_dep = depends_on[-1]
        props = last_dep.kwargs.get("properties", {})
        assert "version_spec" not in props or props.get("version_spec") == ""
