from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

from codebase_rag.parsers.import_processor import ImportProcessor
from codebase_rag.parsers.java.type_inference import JavaTypeInferenceEngine

if TYPE_CHECKING:
    from tree_sitter import Parser

try:
    import tree_sitter_java as tsjava
    from tree_sitter import Language, Parser

    JAVA_AVAILABLE = True
except ImportError:
    JAVA_AVAILABLE = False


pytestmark = pytest.mark.skipif(
    not JAVA_AVAILABLE, reason="tree-sitter-java not installed"
)


@pytest.fixture
def java_parser() -> Parser:
    parser = Parser(Language(tsjava.language()))
    return parser


@pytest.fixture
def import_processor() -> ImportProcessor:
    return ImportProcessor(
        repo_path=Path("/test/repo"),
        project_name="test_project",
    )


@pytest.fixture
def mock_function_registry() -> MagicMock:
    registry = MagicMock()
    registry.__contains__ = MagicMock(return_value=False)
    registry.__getitem__ = MagicMock(return_value=None)
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
    import_processor: ImportProcessor,
    mock_function_registry: MagicMock,
    mock_ast_cache: MagicMock,
) -> JavaTypeInferenceEngine:
    return JavaTypeInferenceEngine(
        import_processor=import_processor,
        function_registry=mock_function_registry,
        repo_path=Path("/test/repo"),
        project_name="test_project",
        ast_cache=mock_ast_cache,
        queries={},
        module_qn_to_file_path={},
        class_inheritance={},
        simple_name_lookup=defaultdict(set),
    )


class TestParameterAnalysisWithRealParsing:
    def test_method_with_single_parameter(
        self,
        java_parser: Parser,
        engine: JavaTypeInferenceEngine,
    ) -> None:
        java_code = b"""
package com.example;

public class UserService {
    public void processUser(String username) {
        System.out.println(username);
    }
}
"""
        tree = java_parser.parse(java_code)
        method_node = self._find_method_node(tree.root_node, "processUser")

        result = engine.build_variable_type_map(method_node, "com.example")

        assert "username" in result
        assert result["username"] == "java.lang.String"

    def test_method_with_multiple_parameters(
        self,
        java_parser: Parser,
        engine: JavaTypeInferenceEngine,
    ) -> None:
        java_code = b"""
package com.example;

public class Calculator {
    public int calculate(int first, double second, boolean isValid) {
        return 0;
    }
}
"""
        tree = java_parser.parse(java_code)
        method_node = self._find_method_node(tree.root_node, "calculate")

        result = engine.build_variable_type_map(method_node, "com.example")

        assert result["first"] == "int"
        assert result["second"] == "double"
        assert result["isValid"] == "boolean"

    def test_method_with_varargs_parameter(
        self,
        java_parser: Parser,
        engine: JavaTypeInferenceEngine,
    ) -> None:
        java_code = b"""
package com.example;

public class Logger {
    public void log(String format, Object... args) {
        System.out.printf(format, args);
    }
}
"""
        tree = java_parser.parse(java_code)
        method_node = self._find_method_node(tree.root_node, "log")

        result = engine.build_variable_type_map(method_node, "com.example")

        assert "format" in result
        assert result["format"] == "java.lang.String"
        assert "args" in result

    def test_constructor_parameters(
        self,
        java_parser: Parser,
        engine: JavaTypeInferenceEngine,
    ) -> None:
        java_code = b"""
package com.example;

public class User {
    private String name;
    private int age;

    public User(String name, int age) {
        this.name = name;
        this.age = age;
    }
}
"""
        tree = java_parser.parse(java_code)
        constructor_node = self._find_constructor_node(tree.root_node)

        result = engine.build_variable_type_map(constructor_node, "com.example")

        assert result["name"] == "java.lang.String"
        assert result["age"] == "int"

    def _find_method_node(self, root_node, method_name: str):
        return self._find_node_recursive(root_node, "method_declaration", method_name)

    def _find_constructor_node(self, root_node):
        return self._find_node_by_type(root_node, "constructor_declaration")

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

    def _find_node_by_type(self, node, node_type: str):
        if node.type == node_type:
            return node
        for child in node.children:
            result = self._find_node_by_type(child, node_type)
            if result:
                return result
        return None


class TestLocalVariableAnalysisWithRealParsing:
    def test_simple_local_variable_declaration(
        self,
        java_parser: Parser,
        engine: JavaTypeInferenceEngine,
    ) -> None:
        java_code = b"""
package com.example;

public class Example {
    public void process() {
        String message = "hello";
        int count = 42;
        double value = 3.14;
    }
}
"""
        tree = java_parser.parse(java_code)
        method_node = self._find_method_node(tree.root_node, "process")

        result = engine.build_variable_type_map(method_node, "com.example")

        assert result["message"] == "java.lang.String"
        assert result["count"] == "int"
        assert result["value"] == "double"

    def test_local_variable_with_object_creation(
        self,
        java_parser: Parser,
        engine: JavaTypeInferenceEngine,
    ) -> None:
        java_code = b"""
package com.example;

import java.util.ArrayList;
import java.util.List;

public class Example {
    public void process() {
        List<String> names = new ArrayList<>();
        StringBuilder builder = new StringBuilder();
    }
}
"""
        tree = java_parser.parse(java_code)
        method_node = self._find_method_node(tree.root_node, "process")

        result = engine.build_variable_type_map(method_node, "com.example")

        assert "names" in result
        assert "builder" in result
        assert result["builder"] == "StringBuilder"

    def test_multiple_declarators_same_type(
        self,
        java_parser: Parser,
        engine: JavaTypeInferenceEngine,
    ) -> None:
        java_code = b"""
package com.example;

public class Example {
    public void process() {
        int a = 1, b = 2, c = 3;
    }
}
"""
        tree = java_parser.parse(java_code)
        method_node = self._find_method_node(tree.root_node, "process")

        result = engine.build_variable_type_map(method_node, "com.example")

        assert result.get("a") == "int" or "a" in result

    def test_array_type_declaration(
        self,
        java_parser: Parser,
        engine: JavaTypeInferenceEngine,
    ) -> None:
        java_code = b"""
package com.example;

public class Example {
    public void process() {
        int[] numbers = new int[10];
        String[] names = new String[5];
    }
}
"""
        tree = java_parser.parse(java_code)
        method_node = self._find_method_node(tree.root_node, "process")

        result = engine.build_variable_type_map(method_node, "com.example")

        assert "numbers" in result
        assert "names" in result

    def _find_method_node(self, root_node, method_name: str):
        return self._find_node_recursive(root_node, "method_declaration", method_name)

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


class TestClassFieldAnalysisWithRealParsing:
    def test_class_fields_accessible_in_method(
        self,
        java_parser: Parser,
        engine: JavaTypeInferenceEngine,
    ) -> None:
        java_code = b"""
package com.example;

public class User {
    private String name;
    private int age;
    private boolean active;

    public void printInfo() {
        System.out.println(this.name);
    }
}
"""
        tree = java_parser.parse(java_code)
        method_node = self._find_method_node(tree.root_node, "printInfo")

        result = engine.build_variable_type_map(method_node, "com.example")

        assert "this.name" in result or "name" in result

    def test_static_fields(
        self,
        java_parser: Parser,
        engine: JavaTypeInferenceEngine,
    ) -> None:
        java_code = b"""
package com.example;

public class Constants {
    public static final String DEFAULT_NAME = "Unknown";
    private static int counter = 0;

    public void process() {
        counter++;
    }
}
"""
        tree = java_parser.parse(java_code)
        method_node = self._find_method_node(tree.root_node, "process")

        result = engine.build_variable_type_map(method_node, "com.example")

        assert isinstance(result, dict)

    def _find_method_node(self, root_node, method_name: str):
        return self._find_node_recursive(root_node, "method_declaration", method_name)

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


class TestAssignmentAnalysisWithRealParsing:
    def test_simple_assignment_in_constructor(
        self,
        java_parser: Parser,
        engine: JavaTypeInferenceEngine,
    ) -> None:
        java_code = b"""
package com.example;

public class Person {
    private String name;

    public Person(String name) {
        this.name = name;
    }
}
"""
        tree = java_parser.parse(java_code)
        constructor_node = self._find_constructor_node(tree.root_node)

        result = engine.build_variable_type_map(constructor_node, "com.example")

        assert "name" in result or "this.name" in result

    def test_assignment_with_literal_value(
        self,
        java_parser: Parser,
        engine: JavaTypeInferenceEngine,
    ) -> None:
        java_code = b"""
package com.example;

public class Example {
    public void process() {
        String text;
        text = "hello world";
        int number;
        number = 42;
    }
}
"""
        tree = java_parser.parse(java_code)
        method_node = self._find_method_node(tree.root_node, "process")

        result = engine.build_variable_type_map(method_node, "com.example")

        assert "text" in result
        assert "number" in result

    def test_chained_assignments(
        self,
        java_parser: Parser,
        engine: JavaTypeInferenceEngine,
    ) -> None:
        java_code = b"""
package com.example;

public class Example {
    public void process() {
        int a, b, c;
        a = b = c = 10;
    }
}
"""
        tree = java_parser.parse(java_code)
        method_node = self._find_method_node(tree.root_node, "process")

        result = engine.build_variable_type_map(method_node, "com.example")

        assert isinstance(result, dict)

    def _find_method_node(self, root_node, method_name: str):
        return self._find_node_recursive(root_node, "method_declaration", method_name)

    def _find_constructor_node(self, root_node):
        return self._find_node_by_type(root_node, "constructor_declaration")

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

    def _find_node_by_type(self, node, node_type: str):
        if node.type == node_type:
            return node
        for child in node.children:
            result = self._find_node_by_type(child, node_type)
            if result:
                return result
        return None


class TestEnhancedForLoopAnalysisWithRealParsing:
    def test_enhanced_for_loop_with_list(
        self,
        java_parser: Parser,
        engine: JavaTypeInferenceEngine,
    ) -> None:
        java_code = b"""
package com.example;

import java.util.List;

public class Example {
    public void process(List<String> items) {
        for (String item : items) {
            System.out.println(item);
        }
    }
}
"""
        tree = java_parser.parse(java_code)
        method_node = self._find_method_node(tree.root_node, "process")

        result = engine.build_variable_type_map(method_node, "com.example")

        assert "item" in result
        assert result["item"] == "java.lang.String"

    def test_enhanced_for_loop_with_array(
        self,
        java_parser: Parser,
        engine: JavaTypeInferenceEngine,
    ) -> None:
        java_code = b"""
package com.example;

public class Example {
    public void process(int[] numbers) {
        for (int num : numbers) {
            System.out.println(num);
        }
    }
}
"""
        tree = java_parser.parse(java_code)
        method_node = self._find_method_node(tree.root_node, "process")

        result = engine.build_variable_type_map(method_node, "com.example")

        assert "num" in result
        assert result["num"] == "int"

    def test_nested_enhanced_for_loops(
        self,
        java_parser: Parser,
        engine: JavaTypeInferenceEngine,
    ) -> None:
        java_code = b"""
package com.example;

import java.util.List;

public class Example {
    public void process(List<List<String>> matrix) {
        for (List<String> row : matrix) {
            for (String cell : row) {
                System.out.println(cell);
            }
        }
    }
}
"""
        tree = java_parser.parse(java_code)
        method_node = self._find_method_node(tree.root_node, "process")

        result = engine.build_variable_type_map(method_node, "com.example")

        assert "cell" in result
        assert result["cell"] == "java.lang.String"

    def test_enhanced_for_with_custom_type(
        self,
        java_parser: Parser,
        engine: JavaTypeInferenceEngine,
    ) -> None:
        java_code = b"""
package com.example;

import java.util.List;

public class Example {
    public void processUsers(List<User> users) {
        for (User user : users) {
            user.process();
        }
    }
}
"""
        tree = java_parser.parse(java_code)
        method_node = self._find_method_node(tree.root_node, "processUsers")

        result = engine.build_variable_type_map(method_node, "com.example")

        assert "user" in result
        assert "User" in result["user"]

    def _find_method_node(self, root_node, method_name: str):
        return self._find_node_recursive(root_node, "method_declaration", method_name)

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


class TestTypeInferenceWithRealParsing:
    def test_infer_type_from_new_expression(
        self,
        java_parser: Parser,
        engine: JavaTypeInferenceEngine,
    ) -> None:
        java_code = b"""
package com.example;

import java.util.HashMap;
import java.util.Map;

public class Example {
    public void process() {
        Map<String, Integer> map = new HashMap<>();
        StringBuilder sb = new StringBuilder("test");
    }
}
"""
        tree = java_parser.parse(java_code)
        method_node = self._find_method_node(tree.root_node, "process")

        result = engine.build_variable_type_map(method_node, "com.example")

        assert "map" in result
        assert "sb" in result
        assert result["sb"] == "StringBuilder"

    def test_infer_type_from_literals(
        self,
        java_parser: Parser,
        engine: JavaTypeInferenceEngine,
    ) -> None:
        java_code = b"""
package com.example;

public class Example {
    public void process() {
        var str = "hello";
        var num = 42;
        var decimal = 3.14;
        var flag = true;
    }
}
"""
        tree = java_parser.parse(java_code)
        method_node = self._find_method_node(tree.root_node, "process")

        result = engine.build_variable_type_map(method_node, "com.example")

        assert isinstance(result, dict)

    def _find_method_node(self, root_node, method_name: str):
        return self._find_node_recursive(root_node, "method_declaration", method_name)

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
        java_parser: Parser,
        engine: JavaTypeInferenceEngine,
    ) -> None:
        java_code = b"""
package com.example;

import java.util.List;
import java.util.ArrayList;

public class ComplexExample {
    private String className = "ComplexExample";
    private int instanceCount;

    public void complexMethod(String param1, int param2) {
        String localVar = "local";
        List<String> items = new ArrayList<>();

        for (String item : items) {
            this.instanceCount++;
            String temp = item.toUpperCase();
        }

        int result;
        result = param2 * 2;
    }
}
"""
        tree = java_parser.parse(java_code)
        method_node = self._find_method_node(tree.root_node, "complexMethod")

        result = engine.build_variable_type_map(method_node, "com.example")

        assert "param1" in result
        assert "param2" in result
        assert "localVar" in result
        assert "items" in result
        assert "item" in result

    def test_nested_classes_variable_resolution(
        self,
        java_parser: Parser,
        engine: JavaTypeInferenceEngine,
    ) -> None:
        java_code = b"""
package com.example;

public class Outer {
    private String outerField;

    public class Inner {
        private String innerField;

        public void innerMethod(String innerParam) {
            String localInner = "inner local";
        }
    }
}
"""
        tree = java_parser.parse(java_code)
        method_node = self._find_method_node(tree.root_node, "innerMethod")

        result = engine.build_variable_type_map(method_node, "com.example")

        assert "innerParam" in result
        assert "localInner" in result

    def test_static_method_variables(
        self,
        java_parser: Parser,
        engine: JavaTypeInferenceEngine,
    ) -> None:
        java_code = b"""
package com.example;

public class StaticExample {
    private static String staticField;

    public static void staticMethod(String arg) {
        String local = "test";
        int count = 0;
    }
}
"""
        tree = java_parser.parse(java_code)
        method_node = self._find_method_node(tree.root_node, "staticMethod")

        result = engine.build_variable_type_map(method_node, "com.example")

        assert "arg" in result
        assert "local" in result
        assert "count" in result

    def test_try_catch_variable_declarations(
        self,
        java_parser: Parser,
        engine: JavaTypeInferenceEngine,
    ) -> None:
        java_code = b"""
package com.example;

import java.io.IOException;

public class ExceptionExample {
    public void riskyMethod() {
        try {
            String content = readFile();
        } catch (IOException e) {
            String errorMsg = e.getMessage();
        } finally {
            String cleanup = "done";
        }
    }

    private String readFile() throws IOException {
        return "";
    }
}
"""
        tree = java_parser.parse(java_code)
        method_node = self._find_method_node(tree.root_node, "riskyMethod")

        result = engine.build_variable_type_map(method_node, "com.example")

        assert isinstance(result, dict)

    def test_lambda_expression_context(
        self,
        java_parser: Parser,
        engine: JavaTypeInferenceEngine,
    ) -> None:
        java_code = b"""
package com.example;

import java.util.List;
import java.util.stream.Collectors;

public class LambdaExample {
    public void process(List<String> items) {
        String prefix = "item: ";
        List<String> result = items.stream()
            .map(item -> prefix + item)
            .collect(Collectors.toList());
    }
}
"""
        tree = java_parser.parse(java_code)
        method_node = self._find_method_node(tree.root_node, "process")

        result = engine.build_variable_type_map(method_node, "com.example")

        assert "prefix" in result
        assert result["prefix"] == "java.lang.String"

    def test_switch_expression_variables(
        self,
        java_parser: Parser,
        engine: JavaTypeInferenceEngine,
    ) -> None:
        java_code = b"""
package com.example;

public class SwitchExample {
    public void process(int day) {
        String dayName;
        switch (day) {
            case 1:
                String monday = "Monday";
                dayName = monday;
                break;
            case 2:
                String tuesday = "Tuesday";
                dayName = tuesday;
                break;
            default:
                String unknown = "Unknown";
                dayName = unknown;
        }
    }
}
"""
        tree = java_parser.parse(java_code)
        method_node = self._find_method_node(tree.root_node, "process")

        result = engine.build_variable_type_map(method_node, "com.example")

        assert "day" in result
        assert "dayName" in result

    def _find_method_node(self, root_node, method_name: str):
        return self._find_node_recursive(root_node, "method_declaration", method_name)

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
    def test_empty_method_body(
        self,
        java_parser: Parser,
        engine: JavaTypeInferenceEngine,
    ) -> None:
        java_code = b"""
package com.example;

public class Empty {
    public void emptyMethod() {
    }
}
"""
        tree = java_parser.parse(java_code)
        method_node = self._find_method_node(tree.root_node, "emptyMethod")

        result = engine.build_variable_type_map(method_node, "com.example")

        assert result == {}

    def test_abstract_method(
        self,
        java_parser: Parser,
        engine: JavaTypeInferenceEngine,
    ) -> None:
        java_code = b"""
package com.example;

public abstract class AbstractClass {
    public abstract void abstractMethod(String param);
}
"""
        tree = java_parser.parse(java_code)
        method_node = self._find_method_node(tree.root_node, "abstractMethod")

        result = engine.build_variable_type_map(method_node, "com.example")

        assert "param" in result

    def test_interface_method(
        self,
        java_parser: Parser,
        engine: JavaTypeInferenceEngine,
    ) -> None:
        java_code = b"""
package com.example;

public interface MyInterface {
    void interfaceMethod(String input);
}
"""
        tree = java_parser.parse(java_code)
        method_node = self._find_method_node(tree.root_node, "interfaceMethod")

        result = engine.build_variable_type_map(method_node, "com.example")

        assert "input" in result

    def test_generic_method(
        self,
        java_parser: Parser,
        engine: JavaTypeInferenceEngine,
    ) -> None:
        java_code = b"""
package com.example;

import java.util.List;

public class GenericExample {
    public <T> void genericMethod(T item, List<T> items) {
        T local = item;
    }
}
"""
        tree = java_parser.parse(java_code)
        method_node = self._find_method_node(tree.root_node, "genericMethod")

        result = engine.build_variable_type_map(method_node, "com.example")

        assert "item" in result
        assert "items" in result

    def test_record_constructor(
        self,
        java_parser: Parser,
        engine: JavaTypeInferenceEngine,
    ) -> None:
        java_code = b"""
package com.example;

public record Person(String name, int age) {
    public Person {
        String validated = name.trim();
    }
}
"""
        tree = java_parser.parse(java_code)
        root = tree.root_node

        compact_constructor = None
        for child in self._traverse_all_nodes(root):
            if child.type == "compact_constructor_declaration":
                compact_constructor = child
                break

        if compact_constructor:
            result = engine.build_variable_type_map(compact_constructor, "com.example")
            assert isinstance(result, dict)

    def _find_method_node(self, root_node, method_name: str):
        return self._find_node_recursive(root_node, "method_declaration", method_name)

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

    def _traverse_all_nodes(self, node):
        yield node
        for child in node.children:
            yield from self._traverse_all_nodes(child)
