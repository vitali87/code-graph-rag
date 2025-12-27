from collections import defaultdict
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

from codebase_rag.parsers.import_processor import ImportProcessor
from codebase_rag.parsers.py.type_inference import PythonTypeInferenceEngine

if TYPE_CHECKING:
    from tree_sitter import Parser

try:
    import tree_sitter_python as tspython
    from tree_sitter import Language, Parser

    PYTHON_AVAILABLE = True
except ImportError:
    PYTHON_AVAILABLE = False


pytestmark = pytest.mark.skipif(
    not PYTHON_AVAILABLE, reason="tree-sitter-python not installed"
)


@pytest.fixture
def python_parser() -> "Parser":
    parser = Parser(Language(tspython.language()))
    return parser


@pytest.fixture
def import_processor() -> MagicMock:
    processor = MagicMock(spec=ImportProcessor)
    processor.import_mapping = {}
    return processor


@pytest.fixture
def mock_function_registry() -> MagicMock:
    registry = MagicMock()
    registry.__contains__ = MagicMock(return_value=False)
    registry.__getitem__ = MagicMock(return_value=None)
    registry.get = MagicMock(return_value=None)
    registry.find_with_prefix = MagicMock(return_value=[])
    registry.items = MagicMock(return_value=[])
    return registry


@pytest.fixture
def mock_ast_cache() -> MagicMock:
    cache = MagicMock()
    cache.__contains__ = MagicMock(return_value=False)
    cache.__getitem__ = MagicMock(return_value=(None, None))
    return cache


@pytest.fixture
def engine(
    import_processor: MagicMock,
    mock_function_registry: MagicMock,
    mock_ast_cache: MagicMock,
) -> PythonTypeInferenceEngine:
    return PythonTypeInferenceEngine(
        import_processor=import_processor,
        function_registry=mock_function_registry,
        repo_path=Path("/test/repo"),
        project_name="test_project",
        ast_cache=mock_ast_cache,
        queries={},
        module_qn_to_file_path={},
        class_inheritance={},
        simple_name_lookup=defaultdict(set),
        js_type_inference_getter=lambda: MagicMock(),
    )


class TestParameterAnalysisWithRealParsing:
    def test_function_with_typed_parameters(
        self,
        python_parser: "Parser",
        engine: PythonTypeInferenceEngine,
    ) -> None:
        python_code = b"""
def process_user(name: str, age: int, active: bool) -> None:
    print(name, age, active)
"""
        tree = python_parser.parse(python_code)
        func_node = self._find_function_node(tree.root_node, "process_user")

        result = engine.build_local_variable_type_map(func_node, "test.module")

        assert result["name"] == "str"
        assert result["age"] == "int"
        assert result["active"] == "bool"

    def test_function_with_default_values(
        self,
        python_parser: "Parser",
        engine: PythonTypeInferenceEngine,
    ) -> None:
        python_code = b"""
def greet(name: str = "World", count: int = 1) -> None:
    for _ in range(count):
        print(f"Hello, {name}")
"""
        tree = python_parser.parse(python_code)
        func_node = self._find_function_node(tree.root_node, "greet")

        result = engine.build_local_variable_type_map(func_node, "test.module")

        assert result["name"] == "str"
        assert result["count"] == "int"

    def test_method_with_self_parameter(
        self,
        python_parser: "Parser",
        engine: PythonTypeInferenceEngine,
    ) -> None:
        python_code = b"""
class UserService:
    def get_user(self, user_id: int) -> None:
        pass
"""
        tree = python_parser.parse(python_code)
        method_node = self._find_method_in_class(
            tree.root_node, "UserService", "get_user"
        )

        result = engine.build_local_variable_type_map(method_node, "test.module")

        assert result["user_id"] == "int"

    def test_classmethod_with_cls_parameter(
        self,
        python_parser: "Parser",
        engine: PythonTypeInferenceEngine,
    ) -> None:
        python_code = b"""
class Factory:
    @classmethod
    def create(cls, name: str) -> None:
        pass
"""
        tree = python_parser.parse(python_code)
        method_node = self._find_method_in_class(tree.root_node, "Factory", "create")

        result = engine.build_local_variable_type_map(method_node, "test.module")

        assert result["name"] == "str"

    def test_function_with_args_kwargs(
        self,
        python_parser: "Parser",
        engine: PythonTypeInferenceEngine,
    ) -> None:
        python_code = b"""
def flexible_func(required: str, *args, **kwargs) -> None:
    pass
"""
        tree = python_parser.parse(python_code)
        func_node = self._find_function_node(tree.root_node, "flexible_func")

        result = engine.build_local_variable_type_map(func_node, "test.module")

        assert result["required"] == "str"

    def _find_function_node(self, root_node, func_name: str):
        return self._find_node_recursive(root_node, "function_definition", func_name)

    def _find_method_in_class(self, root_node, class_name: str, method_name: str):
        class_node = self._find_node_recursive(
            root_node, "class_definition", class_name
        )
        if class_node:
            return self._find_node_recursive(
                class_node, "function_definition", method_name
            )
        return None

    def _find_node_recursive(self, node, node_type: str, name: str):
        if node.type == node_type:
            name_node = node.child_by_field_name("name")
            if name_node and name_node.text.decode() == name:
                return node
        for child in node.children:
            result = self._find_node_recursive(child, node_type, name)
            if result:
                return result
        return None


class TestForLoopAnalysisWithRealParsing:
    def test_for_loop_with_list_literal(
        self,
        python_parser: "Parser",
        engine: PythonTypeInferenceEngine,
    ) -> None:
        python_code = b"""
def process() -> None:
    for item in [User(), User()]:
        print(item)
"""
        tree = python_parser.parse(python_code)
        func_node = self._find_function_node(tree.root_node, "process")

        result = engine.build_local_variable_type_map(func_node, "test.module")

        assert result.get("item") == "User"

    def test_for_loop_with_range(
        self,
        python_parser: "Parser",
        engine: PythonTypeInferenceEngine,
    ) -> None:
        python_code = b"""
def count_up() -> None:
    for i in range(10):
        print(i)
"""
        tree = python_parser.parse(python_code)
        func_node = self._find_function_node(tree.root_node, "count_up")

        result = engine.build_local_variable_type_map(func_node, "test.module")

        assert isinstance(result, dict)

    def test_nested_for_loops(
        self,
        python_parser: "Parser",
        engine: PythonTypeInferenceEngine,
    ) -> None:
        python_code = b"""
def process_matrix() -> None:
    for row in [Row(), Row()]:
        for cell in [Cell(), Cell()]:
            print(row, cell)
"""
        tree = python_parser.parse(python_code)
        func_node = self._find_function_node(tree.root_node, "process_matrix")

        result = engine.build_local_variable_type_map(func_node, "test.module")

        assert result.get("row") == "Row"
        assert result.get("cell") == "Cell"

    def test_for_loop_with_tuple_unpacking(
        self,
        python_parser: "Parser",
        engine: PythonTypeInferenceEngine,
    ) -> None:
        python_code = b"""
def process_pairs() -> None:
    for key, value in items:
        print(key, value)
"""
        tree = python_parser.parse(python_code)
        func_node = self._find_function_node(tree.root_node, "process_pairs")

        result = engine.build_local_variable_type_map(func_node, "test.module")

        assert isinstance(result, dict)

    def _find_function_node(self, root_node, func_name: str):
        return self._find_node_recursive(root_node, "function_definition", func_name)

    def _find_node_recursive(self, node, node_type: str, name: str):
        if node.type == node_type:
            name_node = node.child_by_field_name("name")
            if name_node and name_node.text.decode() == name:
                return node
        for child in node.children:
            result = self._find_node_recursive(child, node_type, name)
            if result:
                return result
        return None


class TestListComprehensionAnalysisWithRealParsing:
    def test_list_comprehension_variable(
        self,
        python_parser: "Parser",
        engine: PythonTypeInferenceEngine,
    ) -> None:
        python_code = b"""
def process() -> None:
    result = [item.name for item in [User(), User()]]
"""
        tree = python_parser.parse(python_code)
        func_node = self._find_function_node(tree.root_node, "process")

        result = engine.build_local_variable_type_map(func_node, "test.module")

        assert isinstance(result, dict)

    def test_nested_list_comprehension(
        self,
        python_parser: "Parser",
        engine: PythonTypeInferenceEngine,
    ) -> None:
        python_code = b"""
def flatten() -> None:
    result = [cell for row in matrix for cell in row]
"""
        tree = python_parser.parse(python_code)
        func_node = self._find_function_node(tree.root_node, "flatten")

        result = engine.build_local_variable_type_map(func_node, "test.module")

        assert isinstance(result, dict)

    def _find_function_node(self, root_node, func_name: str):
        return self._find_node_recursive(root_node, "function_definition", func_name)

    def _find_node_recursive(self, node, node_type: str, name: str):
        if node.type == node_type:
            name_node = node.child_by_field_name("name")
            if name_node and name_node.text.decode() == name:
                return node
        for child in node.children:
            result = self._find_node_recursive(child, node_type, name)
            if result:
                return result
        return None


class TestSelfAssignmentAnalysisWithRealParsing:
    def test_init_with_self_assignments(
        self,
        python_parser: "Parser",
        engine: PythonTypeInferenceEngine,
    ) -> None:
        python_code = b"""
class User:
    def __init__(self, name: str, age: int) -> None:
        self.name = name
        self.age = age
"""
        tree = python_parser.parse(python_code)
        init_node = self._find_method_in_class(tree.root_node, "User", "__init__")

        result = engine.build_local_variable_type_map(init_node, "test.module")

        assert result["name"] == "str"
        assert result["age"] == "int"

    def test_method_accessing_instance_vars(
        self,
        python_parser: "Parser",
        engine: PythonTypeInferenceEngine,
    ) -> None:
        python_code = b"""
class Calculator:
    def __init__(self) -> None:
        self.result = 0

    def add(self, value: int) -> None:
        self.result += value
"""
        tree = python_parser.parse(python_code)
        add_node = self._find_method_in_class(tree.root_node, "Calculator", "add")

        result = engine.build_local_variable_type_map(add_node, "test.module")

        assert result["value"] == "int"

    def _find_method_in_class(self, root_node, class_name: str, method_name: str):
        class_node = self._find_node_recursive(
            root_node, "class_definition", class_name
        )
        if class_node:
            return self._find_node_recursive(
                class_node, "function_definition", method_name
            )
        return None

    def _find_node_recursive(self, node, node_type: str, name: str):
        if node.type == node_type:
            name_node = node.child_by_field_name("name")
            if name_node and name_node.text.decode() == name:
                return node
        for child in node.children:
            result = self._find_node_recursive(child, node_type, name)
            if result:
                return result
        return None


class TestComplexScenariosWithRealParsing:
    def test_method_with_all_variable_types(
        self,
        python_parser: "Parser",
        engine: PythonTypeInferenceEngine,
    ) -> None:
        python_code = b"""
class ComplexService:
    def __init__(self) -> None:
        self.items = []

    def process(self, input_data: str, count: int = 10) -> None:
        for item in [Item(), Item()]:
            result = item.transform()
            self.items.append(result)
"""
        tree = python_parser.parse(python_code)
        process_node = self._find_method_in_class(
            tree.root_node, "ComplexService", "process"
        )

        result = engine.build_local_variable_type_map(process_node, "test.module")

        assert result["input_data"] == "str"
        assert result["count"] == "int"
        assert result.get("item") == "Item"

    def test_async_function_parameters(
        self,
        python_parser: "Parser",
        engine: PythonTypeInferenceEngine,
    ) -> None:
        python_code = b"""
async def fetch_user(user_id: int, timeout: float = 30.0) -> None:
    pass
"""
        tree = python_parser.parse(python_code)
        func_node = self._find_function_node(tree.root_node, "fetch_user")

        result = engine.build_local_variable_type_map(func_node, "test.module")

        assert result["user_id"] == "int"
        assert result["timeout"] == "float"

    def test_decorated_function(
        self,
        python_parser: "Parser",
        engine: PythonTypeInferenceEngine,
    ) -> None:
        python_code = b"""
@decorator
@another_decorator
def decorated_func(value: str) -> None:
    pass
"""
        tree = python_parser.parse(python_code)
        func_node = self._find_function_node(tree.root_node, "decorated_func")

        result = engine.build_local_variable_type_map(func_node, "test.module")

        assert result["value"] == "str"

    def test_function_with_complex_type_hints(
        self,
        python_parser: "Parser",
        engine: PythonTypeInferenceEngine,
    ) -> None:
        python_code = b"""
def process_data(
    items: list[str],
    mapping: dict[str, int],
    callback: Callable[[int], bool],
) -> None:
    pass
"""
        tree = python_parser.parse(python_code)
        func_node = self._find_function_node(tree.root_node, "process_data")

        result = engine.build_local_variable_type_map(func_node, "test.module")

        assert "items" in result
        assert "mapping" in result
        assert "callback" in result

    def _find_function_node(self, root_node, func_name: str):
        return self._find_node_recursive(root_node, "function_definition", func_name)

    def _find_method_in_class(self, root_node, class_name: str, method_name: str):
        class_node = self._find_node_recursive(
            root_node, "class_definition", class_name
        )
        if class_node:
            return self._find_node_recursive(
                class_node, "function_definition", method_name
            )
        return None

    def _find_node_recursive(self, node, node_type: str, name: str):
        if node.type == node_type:
            name_node = node.child_by_field_name("name")
            if name_node and name_node.text.decode() == name:
                return node
        for child in node.children:
            result = self._find_node_recursive(child, node_type, name)
            if result:
                return result
        return None


class TestEdgeCasesWithRealParsing:
    def test_empty_function_body(
        self,
        python_parser: "Parser",
        engine: PythonTypeInferenceEngine,
    ) -> None:
        python_code = b"""
def empty_func() -> None:
    pass
"""
        tree = python_parser.parse(python_code)
        func_node = self._find_function_node(tree.root_node, "empty_func")

        result = engine.build_local_variable_type_map(func_node, "test.module")

        assert result == {}

    def test_function_with_only_docstring(
        self,
        python_parser: "Parser",
        engine: PythonTypeInferenceEngine,
    ) -> None:
        python_code = b'''
def documented_func() -> None:
    """This function does nothing."""
    pass
'''
        tree = python_parser.parse(python_code)
        func_node = self._find_function_node(tree.root_node, "documented_func")

        result = engine.build_local_variable_type_map(func_node, "test.module")

        assert result == {}

    def test_lambda_in_function(
        self,
        python_parser: "Parser",
        engine: PythonTypeInferenceEngine,
    ) -> None:
        python_code = b"""
def use_lambda(items: list) -> None:
    filtered = filter(lambda x: x > 0, items)
"""
        tree = python_parser.parse(python_code)
        func_node = self._find_function_node(tree.root_node, "use_lambda")

        result = engine.build_local_variable_type_map(func_node, "test.module")

        assert result["items"] == "list"

    def test_function_with_walrus_operator(
        self,
        python_parser: "Parser",
        engine: PythonTypeInferenceEngine,
    ) -> None:
        python_code = b"""
def use_walrus(data: list) -> None:
    if (n := len(data)) > 10:
        print(n)
"""
        tree = python_parser.parse(python_code)
        func_node = self._find_function_node(tree.root_node, "use_walrus")

        result = engine.build_local_variable_type_map(func_node, "test.module")

        assert result["data"] == "list"

    def test_generator_function(
        self,
        python_parser: "Parser",
        engine: PythonTypeInferenceEngine,
    ) -> None:
        python_code = b"""
def generate_items(count: int) -> None:
    for i in range(count):
        yield i
"""
        tree = python_parser.parse(python_code)
        func_node = self._find_function_node(tree.root_node, "generate_items")

        result = engine.build_local_variable_type_map(func_node, "test.module")

        assert result["count"] == "int"

    def _find_function_node(self, root_node, func_name: str):
        return self._find_node_recursive(root_node, "function_definition", func_name)

    def _find_node_recursive(self, node, node_type: str, name: str):
        if node.type == node_type:
            name_node = node.child_by_field_name("name")
            if name_node and name_node.text.decode() == name:
                return node
        for child in node.children:
            result = self._find_node_recursive(child, node_type, name)
            if result:
                return result
        return None
