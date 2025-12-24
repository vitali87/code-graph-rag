from pathlib import Path
from unittest.mock import MagicMock

import pytest
from tree_sitter import Node, Parser

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.parsers.definition_processor import DefinitionProcessor
from codebase_rag.tests.conftest import get_node_names, run_updater


def parse_code(
    code: str,
    language: cs.SupportedLanguage,
    parsers: dict[cs.SupportedLanguage, Parser],
) -> Node:
    parser = parsers[language]
    tree = parser.parse(code.encode(cs.ENCODING_UTF8))
    return tree.root_node


def find_first_node_of_type(root: Node, node_type: str) -> Node | None:
    if root.type == node_type:
        return root
    for child in root.children:
        if result := find_first_node_of_type(child, node_type):
            return result
    return None


def find_node_by_name(root: Node, name: str, node_type: str) -> Node | None:
    if root.type == node_type:
        name_node = root.child_by_field_name("name")
        if name_node and name_node.text == name.encode():
            return root
    for child in root.children:
        if result := find_node_by_name(child, name, node_type):
            return result
    return None


@pytest.fixture
def parsers_and_queries() -> tuple:
    return load_parsers()


@pytest.fixture
def definition_processor(
    temp_repo: Path, mock_ingestor: MagicMock
) -> DefinitionProcessor:
    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=temp_repo,
        parsers=parsers,
        queries=queries,
    )
    return updater.factory.definition_processor


class TestExtractFunctionName:
    def test_named_function(
        self,
        definition_processor: DefinitionProcessor,
        parsers_and_queries: tuple,
    ) -> None:
        parsers, _ = parsers_and_queries
        if cs.SupportedLanguage.PYTHON not in parsers:
            pytest.skip("Python parser not available")

        code = "def my_function(): pass"
        root = parse_code(code, cs.SupportedLanguage.PYTHON, parsers)
        func_node = find_first_node_of_type(root, "function_definition")
        assert func_node is not None

        result = definition_processor._extract_function_name(func_node)
        assert result == "my_function"

    def test_javascript_arrow_function_with_variable(
        self,
        definition_processor: DefinitionProcessor,
        parsers_and_queries: tuple,
    ) -> None:
        parsers, _ = parsers_and_queries
        if cs.SupportedLanguage.JS not in parsers:
            pytest.skip("JavaScript parser not available")

        code = "const myArrow = (x) => x * 2;"
        root = parse_code(code, cs.SupportedLanguage.JS, parsers)
        arrow_node = find_first_node_of_type(root, "arrow_function")
        assert arrow_node is not None

        result = definition_processor._extract_function_name(arrow_node)
        assert result == "myArrow"

    def test_anonymous_function_returns_none(
        self,
        definition_processor: DefinitionProcessor,
        parsers_and_queries: tuple,
    ) -> None:
        parsers, _ = parsers_and_queries
        if cs.SupportedLanguage.JS not in parsers:
            pytest.skip("JavaScript parser not available")

        code = "(function() { return 1; })();"
        root = parse_code(code, cs.SupportedLanguage.JS, parsers)
        func_node = find_first_node_of_type(root, "function")
        assert func_node is not None

        result = definition_processor._extract_function_name(func_node)
        assert result is None


class TestGenerateAnonymousFunctionName:
    def test_iife_parenthesized(
        self,
        definition_processor: DefinitionProcessor,
        parsers_and_queries: tuple,
    ) -> None:
        parsers, _ = parsers_and_queries
        if cs.SupportedLanguage.JS not in parsers:
            pytest.skip("JavaScript parser not available")

        code = "(function() { return 1; })();"
        root = parse_code(code, cs.SupportedLanguage.JS, parsers)
        func_node = find_first_node_of_type(root, "function_expression")
        assert func_node is not None

        result = definition_processor._generate_anonymous_function_name(
            func_node, "proj.module"
        )
        assert result.startswith("iife_func_")

    def test_iife_arrow(
        self,
        definition_processor: DefinitionProcessor,
        parsers_and_queries: tuple,
    ) -> None:
        parsers, _ = parsers_and_queries
        if cs.SupportedLanguage.JS not in parsers:
            pytest.skip("JavaScript parser not available")

        code = "(() => { return 1; })();"
        root = parse_code(code, cs.SupportedLanguage.JS, parsers)
        arrow_node = find_first_node_of_type(root, "arrow_function")
        assert arrow_node is not None

        result = definition_processor._generate_anonymous_function_name(
            arrow_node, "proj.module"
        )
        assert result.startswith("iife_arrow_")

    def test_regular_anonymous(
        self,
        definition_processor: DefinitionProcessor,
        parsers_and_queries: tuple,
    ) -> None:
        parsers, _ = parsers_and_queries
        if cs.SupportedLanguage.JS not in parsers:
            pytest.skip("JavaScript parser not available")

        code = "arr.map(function(x) { return x; });"
        root = parse_code(code, cs.SupportedLanguage.JS, parsers)
        func_node = find_first_node_of_type(root, "function")
        assert func_node is not None

        result = definition_processor._generate_anonymous_function_name(
            func_node, "proj.module"
        )
        assert result.startswith("anonymous_")


class TestIsMethod:
    def test_function_not_in_class(
        self,
        definition_processor: DefinitionProcessor,
        parsers_and_queries: tuple,
    ) -> None:
        parsers, queries = parsers_and_queries
        if cs.SupportedLanguage.PYTHON not in parsers:
            pytest.skip("Python parser not available")

        code = "def standalone_func(): pass"
        root = parse_code(code, cs.SupportedLanguage.PYTHON, parsers)
        func_node = find_first_node_of_type(root, "function_definition")
        assert func_node is not None

        lang_config = queries[cs.SupportedLanguage.PYTHON]["config"]
        result = definition_processor._is_method(func_node, lang_config)
        assert result is False

    def test_function_inside_class(
        self,
        definition_processor: DefinitionProcessor,
        parsers_and_queries: tuple,
    ) -> None:
        parsers, queries = parsers_and_queries
        if cs.SupportedLanguage.PYTHON not in parsers:
            pytest.skip("Python parser not available")

        code = """
class MyClass:
    def my_method(self):
        pass
"""
        root = parse_code(code, cs.SupportedLanguage.PYTHON, parsers)
        func_node = find_first_node_of_type(root, "function_definition")
        assert func_node is not None

        lang_config = queries[cs.SupportedLanguage.PYTHON]["config"]
        result = definition_processor._is_method(func_node, lang_config)
        assert result is True

    def test_nested_function_in_method(
        self,
        definition_processor: DefinitionProcessor,
        parsers_and_queries: tuple,
    ) -> None:
        parsers, queries = parsers_and_queries
        if cs.SupportedLanguage.PYTHON not in parsers:
            pytest.skip("Python parser not available")

        code = """
class MyClass:
    def my_method(self):
        def inner_func():
            pass
"""
        root = parse_code(code, cs.SupportedLanguage.PYTHON, parsers)
        inner_func = find_node_by_name(root, "inner_func", "function_definition")
        assert inner_func is not None

        lang_config = queries[cs.SupportedLanguage.PYTHON]["config"]
        result = definition_processor._is_method(inner_func, lang_config)
        assert result is True


class TestFormatNestedQn:
    def test_with_path_parts(
        self,
        definition_processor: DefinitionProcessor,
    ) -> None:
        result = definition_processor._format_nested_qn(
            "proj.module", ["outer", "middle"], "inner"
        )
        assert result == "proj.module.outer.middle.inner"

    def test_empty_path_parts(
        self,
        definition_processor: DefinitionProcessor,
    ) -> None:
        result = definition_processor._format_nested_qn("proj.module", [], "func")
        assert result == "proj.module.func"

    def test_single_path_part(
        self,
        definition_processor: DefinitionProcessor,
    ) -> None:
        result = definition_processor._format_nested_qn(
            "proj.module", ["wrapper"], "nested"
        )
        assert result == "proj.module.wrapper.nested"


class TestExtractNodeName:
    def test_function_with_name(
        self,
        definition_processor: DefinitionProcessor,
        parsers_and_queries: tuple,
    ) -> None:
        parsers, _ = parsers_and_queries
        if cs.SupportedLanguage.PYTHON not in parsers:
            pytest.skip("Python parser not available")

        code = "def my_function(): pass"
        root = parse_code(code, cs.SupportedLanguage.PYTHON, parsers)
        func_node = find_first_node_of_type(root, "function_definition")
        assert func_node is not None

        result = definition_processor._extract_node_name(func_node)
        assert result == "my_function"

    def test_class_with_name(
        self,
        definition_processor: DefinitionProcessor,
        parsers_and_queries: tuple,
    ) -> None:
        parsers, _ = parsers_and_queries
        if cs.SupportedLanguage.PYTHON not in parsers:
            pytest.skip("Python parser not available")

        code = "class MyClass: pass"
        root = parse_code(code, cs.SupportedLanguage.PYTHON, parsers)
        class_node = find_first_node_of_type(root, "class_definition")
        assert class_node is not None

        result = definition_processor._extract_node_name(class_node)
        assert result == "MyClass"


class TestDetermineFunctionParent:
    def test_top_level_function(
        self,
        definition_processor: DefinitionProcessor,
        parsers_and_queries: tuple,
    ) -> None:
        parsers, queries = parsers_and_queries
        if cs.SupportedLanguage.PYTHON not in parsers:
            pytest.skip("Python parser not available")

        code = "def my_function(): pass"
        root = parse_code(code, cs.SupportedLanguage.PYTHON, parsers)
        func_node = find_first_node_of_type(root, "function_definition")
        assert func_node is not None

        lang_config = queries[cs.SupportedLanguage.PYTHON]["config"]
        parent_type, parent_qn = definition_processor._determine_function_parent(
            func_node, "proj.module", lang_config
        )
        assert parent_type == "Module"
        assert parent_qn == "proj.module"

    def test_nested_function(
        self,
        definition_processor: DefinitionProcessor,
        parsers_and_queries: tuple,
    ) -> None:
        parsers, queries = parsers_and_queries
        if cs.SupportedLanguage.PYTHON not in parsers:
            pytest.skip("Python parser not available")

        code = """
def outer():
    def inner():
        pass
"""
        root = parse_code(code, cs.SupportedLanguage.PYTHON, parsers)
        inner_func = find_node_by_name(root, "inner", "function_definition")
        assert inner_func is not None

        lang_config = queries[cs.SupportedLanguage.PYTHON]["config"]
        parent_type, parent_qn = definition_processor._determine_function_parent(
            inner_func, "proj.module", lang_config
        )
        assert parent_type == "Function"
        assert parent_qn == "proj.module.outer"


class TestBuildNestedQualifiedName:
    def test_top_level_function(
        self,
        definition_processor: DefinitionProcessor,
        parsers_and_queries: tuple,
    ) -> None:
        parsers, queries = parsers_and_queries
        if cs.SupportedLanguage.PYTHON not in parsers:
            pytest.skip("Python parser not available")

        code = "def my_func(): pass"
        root = parse_code(code, cs.SupportedLanguage.PYTHON, parsers)
        func_node = find_first_node_of_type(root, "function_definition")
        assert func_node is not None

        lang_config = queries[cs.SupportedLanguage.PYTHON]["config"]
        result = definition_processor._build_nested_qualified_name(
            func_node, "proj.module", "my_func", lang_config
        )
        assert result == "proj.module.my_func"

    def test_nested_function(
        self,
        definition_processor: DefinitionProcessor,
        parsers_and_queries: tuple,
    ) -> None:
        parsers, queries = parsers_and_queries
        if cs.SupportedLanguage.PYTHON not in parsers:
            pytest.skip("Python parser not available")

        code = """
def outer():
    def inner():
        pass
"""
        root = parse_code(code, cs.SupportedLanguage.PYTHON, parsers)
        inner_func = find_node_by_name(root, "inner", "function_definition")
        assert inner_func is not None

        lang_config = queries[cs.SupportedLanguage.PYTHON]["config"]
        result = definition_processor._build_nested_qualified_name(
            inner_func, "proj.module", "inner", lang_config
        )
        assert result == "proj.module.outer.inner"

    def test_deeply_nested_function(
        self,
        definition_processor: DefinitionProcessor,
        parsers_and_queries: tuple,
    ) -> None:
        parsers, queries = parsers_and_queries
        if cs.SupportedLanguage.PYTHON not in parsers:
            pytest.skip("Python parser not available")

        code = """
def level1():
    def level2():
        def level3():
            pass
"""
        root = parse_code(code, cs.SupportedLanguage.PYTHON, parsers)
        level3_func = find_node_by_name(root, "level3", "function_definition")
        assert level3_func is not None

        lang_config = queries[cs.SupportedLanguage.PYTHON]["config"]
        result = definition_processor._build_nested_qualified_name(
            level3_func, "proj.module", "level3", lang_config
        )
        assert result == "proj.module.level1.level2.level3"

    def test_method_in_class_returns_none(
        self,
        definition_processor: DefinitionProcessor,
        parsers_and_queries: tuple,
    ) -> None:
        parsers, queries = parsers_and_queries
        if cs.SupportedLanguage.PYTHON not in parsers:
            pytest.skip("Python parser not available")

        code = """
class MyClass:
    def my_method(self):
        pass
"""
        root = parse_code(code, cs.SupportedLanguage.PYTHON, parsers)
        method_node = find_first_node_of_type(root, "function_definition")
        assert method_node is not None

        lang_config = queries[cs.SupportedLanguage.PYTHON]["config"]
        result = definition_processor._build_nested_qualified_name(
            method_node, "proj.module", "my_method", lang_config
        )
        assert result is None


class TestBuildFunctionProps:
    def test_basic_function_props(
        self,
        definition_processor: DefinitionProcessor,
        parsers_and_queries: tuple,
    ) -> None:
        parsers, _ = parsers_and_queries
        if cs.SupportedLanguage.PYTHON not in parsers:
            pytest.skip("Python parser not available")

        code = "def my_function(): pass"
        root = parse_code(code, cs.SupportedLanguage.PYTHON, parsers)
        func_node = find_first_node_of_type(root, "function_definition")
        assert func_node is not None

        from codebase_rag.parsers.function_ingest import FunctionResolution

        resolution = FunctionResolution(
            qualified_name="proj.module.my_function",
            name="my_function",
            is_exported=False,
        )

        result = definition_processor._build_function_props(func_node, resolution)

        assert result["qualified_name"] == "proj.module.my_function"
        assert result["name"] == "my_function"
        assert result["is_exported"] is False
        assert "start_line" in result
        assert "end_line" in result
        assert "decorators" in result

    def test_exported_function_props(
        self,
        definition_processor: DefinitionProcessor,
        parsers_and_queries: tuple,
    ) -> None:
        parsers, _ = parsers_and_queries
        if cs.SupportedLanguage.PYTHON not in parsers:
            pytest.skip("Python parser not available")

        code = "def exported_func(): pass"
        root = parse_code(code, cs.SupportedLanguage.PYTHON, parsers)
        func_node = find_first_node_of_type(root, "function_definition")
        assert func_node is not None

        from codebase_rag.parsers.function_ingest import FunctionResolution

        resolution = FunctionResolution(
            qualified_name="proj.module.exported_func",
            name="exported_func",
            is_exported=True,
        )

        result = definition_processor._build_function_props(func_node, resolution)

        assert result["is_exported"] is True


class TestFunctionResolution:
    def test_named_tuple_fields(self) -> None:
        from codebase_rag.parsers.function_ingest import FunctionResolution

        resolution = FunctionResolution(
            qualified_name="proj.module.func",
            name="func",
            is_exported=True,
        )

        assert resolution.qualified_name == "proj.module.func"
        assert resolution.name == "func"
        assert resolution.is_exported is True

    def test_immutability(self) -> None:
        from codebase_rag.parsers.function_ingest import FunctionResolution

        resolution = FunctionResolution(
            qualified_name="proj.module.func",
            name="func",
            is_exported=False,
        )

        with pytest.raises(AttributeError):
            resolution.name = "new_name"


class TestIntegrationFunctionIngestion:
    @pytest.fixture
    def python_functions_project(self, temp_repo: Path) -> Path:
        project_path = temp_repo / "python_functions_test"
        project_path.mkdir()

        main_file = project_path / "main.py"
        main_file.write_text(
            """
def top_level_function():
    pass

def outer_function():
    def inner_function():
        pass
    return inner_function

def deeply_nested():
    def level2():
        def level3():
            pass
        return level3
    return level2
"""
        )

        return project_path

    def test_top_level_functions_ingested(
        self, python_functions_project: Path, mock_ingestor: MagicMock
    ) -> None:
        run_updater(python_functions_project, mock_ingestor, skip_if_missing="python")

        project_name = python_functions_project.name
        functions = get_node_names(mock_ingestor, "Function")

        expected_functions = [
            f"{project_name}.main.top_level_function",
            f"{project_name}.main.outer_function",
            f"{project_name}.main.outer_function.inner_function",
            f"{project_name}.main.deeply_nested",
            f"{project_name}.main.deeply_nested.level2",
            f"{project_name}.main.deeply_nested.level2.level3",
        ]

        for expected in expected_functions:
            assert expected in functions, f"Missing function: {expected}"

    @pytest.fixture
    def javascript_functions_project(self, temp_repo: Path) -> Path:
        project_path = temp_repo / "js_functions_test"
        project_path.mkdir()

        package_json = project_path / "package.json"
        package_json.write_text('{"name": "js-functions-test", "version": "1.0.0"}')

        main_file = project_path / "main.js"
        main_file.write_text(
            """
function topLevel() {
    return 1;
}

const arrowFunc = (x) => x * 2;

function withNested() {
    function inner() {
        return 2;
    }
    return inner;
}

const factory = function createFactory() {
    const helper = (data) => data.map(x => x);
    return { helper };
};
"""
        )

        return project_path

    def test_javascript_functions_ingested(
        self, javascript_functions_project: Path, mock_ingestor: MagicMock
    ) -> None:
        run_updater(
            javascript_functions_project, mock_ingestor, skip_if_missing="javascript"
        )

        project_name = javascript_functions_project.name
        functions = get_node_names(mock_ingestor, "Function")

        expected_functions = [
            f"{project_name}.main.topLevel",
            f"{project_name}.main.arrowFunc",
            f"{project_name}.main.withNested",
            f"{project_name}.main.withNested.inner",
        ]

        for expected in expected_functions:
            assert expected in functions, f"Missing function: {expected}"


class TestCollectAncestorPathParts:
    def test_no_ancestors(
        self,
        definition_processor: DefinitionProcessor,
        parsers_and_queries: tuple,
    ) -> None:
        parsers, queries = parsers_and_queries
        if cs.SupportedLanguage.PYTHON not in parsers:
            pytest.skip("Python parser not available")

        code = "def my_func(): pass"
        root = parse_code(code, cs.SupportedLanguage.PYTHON, parsers)
        func_node = find_first_node_of_type(root, "function_definition")
        assert func_node is not None

        lang_config = queries[cs.SupportedLanguage.PYTHON]["config"]
        result = definition_processor._collect_ancestor_path_parts(
            func_node, func_node.parent, lang_config, skip_classes=False
        )
        assert result == []

    def test_one_function_ancestor(
        self,
        definition_processor: DefinitionProcessor,
        parsers_and_queries: tuple,
    ) -> None:
        parsers, queries = parsers_and_queries
        if cs.SupportedLanguage.PYTHON not in parsers:
            pytest.skip("Python parser not available")

        code = """
def outer():
    def inner():
        pass
"""
        root = parse_code(code, cs.SupportedLanguage.PYTHON, parsers)
        inner_func = find_node_by_name(root, "inner", "function_definition")
        assert inner_func is not None

        lang_config = queries[cs.SupportedLanguage.PYTHON]["config"]
        result = definition_processor._collect_ancestor_path_parts(
            inner_func, inner_func.parent, lang_config, skip_classes=False
        )
        assert result == ["outer"]

    def test_multiple_function_ancestors(
        self,
        definition_processor: DefinitionProcessor,
        parsers_and_queries: tuple,
    ) -> None:
        parsers, queries = parsers_and_queries
        if cs.SupportedLanguage.PYTHON not in parsers:
            pytest.skip("Python parser not available")

        code = """
def level1():
    def level2():
        def level3():
            pass
"""
        root = parse_code(code, cs.SupportedLanguage.PYTHON, parsers)
        level3_func = find_node_by_name(root, "level3", "function_definition")
        assert level3_func is not None

        lang_config = queries[cs.SupportedLanguage.PYTHON]["config"]
        result = definition_processor._collect_ancestor_path_parts(
            level3_func, level3_func.parent, lang_config, skip_classes=False
        )
        assert result == ["level1", "level2"]


class TestRustFunctionQualifiedName:
    def test_top_level_rust_function(
        self,
        definition_processor: DefinitionProcessor,
        parsers_and_queries: tuple,
    ) -> None:
        parsers, _ = parsers_and_queries
        if cs.SupportedLanguage.RUST not in parsers:
            pytest.skip("Rust parser not available")

        code = "fn my_function() {}"
        root = parse_code(code, cs.SupportedLanguage.RUST, parsers)
        func_node = find_first_node_of_type(root, "function_item")
        assert func_node is not None

        result = definition_processor._build_rust_function_qualified_name(
            func_node, "crate.module", "my_function"
        )
        assert result == "crate.module.my_function"

    def test_rust_function_in_mod(
        self,
        definition_processor: DefinitionProcessor,
        parsers_and_queries: tuple,
    ) -> None:
        parsers, _ = parsers_and_queries
        if cs.SupportedLanguage.RUST not in parsers:
            pytest.skip("Rust parser not available")

        code = """
mod submodule {
    fn inner_func() {}
}
"""
        root = parse_code(code, cs.SupportedLanguage.RUST, parsers)
        func_node = find_first_node_of_type(root, "function_item")
        assert func_node is not None

        result = definition_processor._build_rust_function_qualified_name(
            func_node, "crate.module", "inner_func"
        )
        assert "submodule" in result
        assert "inner_func" in result
