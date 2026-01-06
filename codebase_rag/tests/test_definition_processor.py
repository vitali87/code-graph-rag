from importlib.util import find_spec
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from tree_sitter import Language, Parser

from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers

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

        from codebase_rag.parsers.handlers.python import PythonHandler

        handler = PythonHandler()
        result = handler.extract_decorators(func_node)
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

        from codebase_rag.parsers.handlers.python import PythonHandler

        handler = PythonHandler()
        result = handler.extract_decorators(func_node)
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

        from codebase_rag.parsers.handlers.python import PythonHandler

        handler = PythonHandler()
        result = handler.extract_decorators(func_node)
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

        from codebase_rag.parsers.handlers.python import PythonHandler

        handler = PythonHandler()
        result = handler.extract_decorators(func_node)
        assert any("module.submodule.decorator" in d for d in result)

    def test_no_decorators(self, py_parser: Parser) -> None:
        code = b"""
def my_func():
    pass
"""
        tree = py_parser.parse(code)
        func_node = tree.root_node.children[0]

        from codebase_rag.parsers.handlers.python import PythonHandler

        handler = PythonHandler()
        result = handler.extract_decorators(func_node)
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

        from codebase_rag.parsers.handlers.python import PythonHandler

        handler = PythonHandler()
        result = handler.extract_decorators(class_node)
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

        from codebase_rag.parsers.handlers.python import PythonHandler

        handler = PythonHandler()

        for child in class_body.children:
            if child.type == "decorated_definition":
                for sub in child.children:
                    if sub.type == "function_definition":
                        result = handler.extract_decorators(sub)
                        assert len(result) >= 1


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


@pytest.mark.skipif(not PY_AVAILABLE, reason="tree-sitter-python not available")
class TestProcessFile:
    def test_process_file_creates_module_node(
        self, temp_repo: Path, definition_processor: GraphUpdater
    ) -> None:
        py_file = temp_repo / "example.py"
        py_file.write_text("def hello(): pass")

        from codebase_rag.constants import SupportedLanguage

        result = definition_processor.factory.definition_processor.process_file(
            py_file,
            SupportedLanguage.PYTHON,
            definition_processor.queries,
            {},
        )

        assert result is not None
        root_node, language = result
        assert language == SupportedLanguage.PYTHON

        node_calls = definition_processor.ingestor.ensure_node_batch.call_args_list
        module_nodes = [c for c in node_calls if c[0][0] == "Module"]

        assert len(module_nodes) >= 1
        module_props = module_nodes[-1][0][1]
        assert module_props["name"] == "example.py"
        assert module_props["path"] == "example.py"
        assert "qualified_name" in module_props

    def test_process_file_init_py_uses_parent_qn(
        self, temp_repo: Path, definition_processor: GraphUpdater
    ) -> None:
        pkg_dir = temp_repo / "mypackage"
        pkg_dir.mkdir()
        init_file = pkg_dir / "__init__.py"
        init_file.write_text("# package init")

        from codebase_rag.constants import SupportedLanguage

        result = definition_processor.factory.definition_processor.process_file(
            init_file,
            SupportedLanguage.PYTHON,
            definition_processor.queries,
            {},
        )

        assert result is not None

        node_calls = definition_processor.ingestor.ensure_node_batch.call_args_list
        module_nodes = [c for c in node_calls if c[0][0] == "Module"]

        assert len(module_nodes) >= 1
        module_props = module_nodes[-1][0][1]
        qn = module_props["qualified_name"]
        assert "__init__" not in qn
        assert "mypackage" in qn

    def test_process_file_nested_init_py(
        self, temp_repo: Path, definition_processor: GraphUpdater
    ) -> None:
        nested_dir = temp_repo / "pkg" / "subpkg"
        nested_dir.mkdir(parents=True)
        init_file = nested_dir / "__init__.py"
        init_file.write_text("# nested package")

        from codebase_rag.constants import SupportedLanguage

        result = definition_processor.factory.definition_processor.process_file(
            init_file,
            SupportedLanguage.PYTHON,
            definition_processor.queries,
            {},
        )

        assert result is not None

        node_calls = definition_processor.ingestor.ensure_node_batch.call_args_list
        module_nodes = [c for c in node_calls if c[0][0] == "Module"]

        assert len(module_nodes) >= 1
        module_props = module_nodes[-1][0][1]
        qn = module_props["qualified_name"]
        assert "pkg" in qn
        assert "subpkg" in qn
        assert "__init__" not in qn

    def test_process_file_unsupported_language_returns_none(
        self, temp_repo: Path, definition_processor: GraphUpdater
    ) -> None:
        txt_file = temp_repo / "readme.txt"
        txt_file.write_text("Just a text file")

        from codebase_rag.constants import SupportedLanguage

        result = definition_processor.factory.definition_processor.process_file(
            txt_file,
            SupportedLanguage.PYTHON,
            {},
            {},
        )

        assert result is None

    def test_process_file_creates_contains_module_relationship_to_project(
        self, temp_repo: Path, definition_processor: GraphUpdater
    ) -> None:
        py_file = temp_repo / "root_module.py"
        py_file.write_text("x = 1")

        from codebase_rag.constants import SupportedLanguage

        definition_processor.factory.definition_processor.process_file(
            py_file,
            SupportedLanguage.PYTHON,
            definition_processor.queries,
            {},
        )

        rel_calls = (
            definition_processor.ingestor.ensure_relationship_batch.call_args_list
        )
        contains_module = [c for c in rel_calls if c[0][1] == "CONTAINS_MODULE"]

        assert len(contains_module) >= 1
        rel = contains_module[-1]
        from_tuple = rel[0][0]
        to_tuple = rel[0][2]
        assert from_tuple[0] == "Project"
        assert to_tuple[0] == "Module"

    def test_process_file_creates_contains_module_relationship_to_package(
        self, temp_repo: Path, definition_processor: GraphUpdater
    ) -> None:
        pkg_dir = temp_repo / "mypackage"
        pkg_dir.mkdir()
        py_file = pkg_dir / "module.py"
        py_file.write_text("y = 2")

        from codebase_rag.constants import SupportedLanguage

        structural_elements = {Path("mypackage"): "test_project.mypackage"}

        definition_processor.factory.definition_processor.process_file(
            py_file,
            SupportedLanguage.PYTHON,
            definition_processor.queries,
            structural_elements,
        )

        rel_calls = (
            definition_processor.ingestor.ensure_relationship_batch.call_args_list
        )
        contains_module = [c for c in rel_calls if c[0][1] == "CONTAINS_MODULE"]

        assert len(contains_module) >= 1
        rel = contains_module[-1]
        from_tuple = rel[0][0]
        assert from_tuple[0] == "Package"
        assert from_tuple[2] == "test_project.mypackage"

    def test_process_file_creates_contains_module_relationship_to_folder(
        self, temp_repo: Path, definition_processor: GraphUpdater
    ) -> None:
        folder_dir = temp_repo / "scripts"
        folder_dir.mkdir()
        py_file = folder_dir / "util.py"
        py_file.write_text("z = 3")

        from codebase_rag.constants import SupportedLanguage

        definition_processor.factory.definition_processor.process_file(
            py_file,
            SupportedLanguage.PYTHON,
            definition_processor.queries,
            {},
        )

        rel_calls = (
            definition_processor.ingestor.ensure_relationship_batch.call_args_list
        )
        contains_module = [c for c in rel_calls if c[0][1] == "CONTAINS_MODULE"]

        assert len(contains_module) >= 1
        rel = contains_module[-1]
        from_tuple = rel[0][0]
        assert from_tuple[0] == "Folder"
        assert from_tuple[2] == "scripts"

    def test_process_file_registers_module_qn_to_file_path(
        self, temp_repo: Path, definition_processor: GraphUpdater
    ) -> None:
        py_file = temp_repo / "tracked.py"
        py_file.write_text("a = 1")

        from codebase_rag.constants import SupportedLanguage

        processor = definition_processor.factory.definition_processor
        processor.process_file(
            py_file,
            SupportedLanguage.PYTHON,
            definition_processor.queries,
            {},
        )

        assert len(processor.module_qn_to_file_path) >= 1
        found = False
        for qn, path in processor.module_qn_to_file_path.items():
            if path == py_file:
                found = True
                assert "tracked" in qn
                break
        assert found

    def test_process_file_calls_ingest_methods(
        self, temp_repo: Path, definition_processor: GraphUpdater
    ) -> None:
        py_file = temp_repo / "with_class.py"
        py_file.write_text("""
class MyClass:
    def method(self):
        pass

def standalone():
    pass
""")

        from codebase_rag.constants import SupportedLanguage

        result = definition_processor.factory.definition_processor.process_file(
            py_file,
            SupportedLanguage.PYTHON,
            definition_processor.queries,
            {},
        )

        assert result is not None

        node_calls = definition_processor.ingestor.ensure_node_batch.call_args_list
        node_types = {c[0][0] for c in node_calls}

        assert "Module" in node_types
        assert "Class" in node_types or "Function" in node_types

    def test_process_file_with_syntax_error_still_processes(
        self, temp_repo: Path, definition_processor: GraphUpdater
    ) -> None:
        py_file = temp_repo / "bad_syntax.py"
        py_file.write_text("def broken( x = 1")

        from codebase_rag.constants import SupportedLanguage

        result = definition_processor.factory.definition_processor.process_file(
            py_file,
            SupportedLanguage.PYTHON,
            definition_processor.queries,
            {},
        )

        assert result is not None

    def test_process_file_empty_file(
        self, temp_repo: Path, definition_processor: GraphUpdater
    ) -> None:
        py_file = temp_repo / "empty.py"
        py_file.write_text("")

        from codebase_rag.constants import SupportedLanguage

        result = definition_processor.factory.definition_processor.process_file(
            py_file,
            SupportedLanguage.PYTHON,
            definition_processor.queries,
            {},
        )

        assert result is not None

        node_calls = definition_processor.ingestor.ensure_node_batch.call_args_list
        module_nodes = [c for c in node_calls if c[0][0] == "Module"]
        assert len(module_nodes) >= 1


RUST_AVAILABLE = find_spec("tree_sitter_rust") is not None


@pytest.mark.skipif(not RUST_AVAILABLE, reason="tree-sitter-rust not available")
class TestProcessFileRust:
    def test_process_file_mod_rs_uses_parent_qn(
        self, temp_repo: Path, definition_processor: GraphUpdater
    ) -> None:
        rust_dir = temp_repo / "src" / "utils"
        rust_dir.mkdir(parents=True)
        mod_file = rust_dir / "mod.rs"
        mod_file.write_text("pub fn helper() {}")

        from codebase_rag.constants import SupportedLanguage

        if SupportedLanguage.RUST not in definition_processor.queries:
            pytest.skip("Rust parser not available")

        result = definition_processor.factory.definition_processor.process_file(
            mod_file,
            SupportedLanguage.RUST,
            definition_processor.queries,
            {},
        )

        assert result is not None

        node_calls = definition_processor.ingestor.ensure_node_batch.call_args_list
        module_nodes = [c for c in node_calls if c[0][0] == "Module"]

        assert len(module_nodes) >= 1
        module_props = module_nodes[-1][0][1]
        qn = module_props["qualified_name"]
        assert "mod" not in qn or "mod.rs" not in qn
        assert "utils" in qn
