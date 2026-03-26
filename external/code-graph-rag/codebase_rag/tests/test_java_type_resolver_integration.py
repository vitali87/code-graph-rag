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
def type_inference_engine(
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


class TestJavaTypeResolverWithRealParsing:
    def test_get_superclass_name_with_real_ast(
        self,
        java_parser: Parser,
        type_inference_engine: JavaTypeInferenceEngine,
        mock_ast_cache: MagicMock,
    ) -> None:
        java_code = b"""
package com.example;

public class Child extends Parent {
    private String name;

    public Child(String name) {
        super();
        this.name = name;
    }
}
"""
        tree = java_parser.parse(java_code)
        root_node = tree.root_node

        file_path = Path("/test/Child.java")
        type_inference_engine.module_qn_to_file_path = {"com.example": file_path}
        mock_ast_cache.__contains__ = MagicMock(side_effect=lambda x: x == file_path)
        mock_ast_cache.__getitem__ = MagicMock(return_value=(root_node, java_code))

        result = type_inference_engine._get_superclass_name("com.example.Child")
        assert result == "Parent"

    def test_get_superclass_name_no_extends(
        self,
        java_parser: Parser,
        type_inference_engine: JavaTypeInferenceEngine,
        mock_ast_cache: MagicMock,
    ) -> None:
        java_code = b"""
package com.example;

public class Simple {
    private int value;
}
"""
        tree = java_parser.parse(java_code)
        root_node = tree.root_node

        file_path = Path("/test/Simple.java")
        type_inference_engine.module_qn_to_file_path = {"com.example": file_path}
        mock_ast_cache.__contains__ = MagicMock(side_effect=lambda x: x == file_path)
        mock_ast_cache.__getitem__ = MagicMock(return_value=(root_node, java_code))

        result = type_inference_engine._get_superclass_name("com.example.Simple")
        assert result is None

    def test_get_superclass_name_generic_extends(
        self,
        java_parser: Parser,
        type_inference_engine: JavaTypeInferenceEngine,
        mock_ast_cache: MagicMock,
    ) -> None:
        java_code = b"""
package com.example;

public class MyList extends ArrayList<String> {
    public void addItem(String item) {
        add(item);
    }
}
"""
        tree = java_parser.parse(java_code)
        root_node = tree.root_node

        file_path = Path("/test/MyList.java")
        type_inference_engine.module_qn_to_file_path = {"com.example": file_path}
        mock_ast_cache.__contains__ = MagicMock(side_effect=lambda x: x == file_path)
        mock_ast_cache.__getitem__ = MagicMock(return_value=(root_node, java_code))

        result = type_inference_engine._get_superclass_name("com.example.MyList")
        assert result == "ArrayList"

    def test_get_implemented_interfaces_single(
        self,
        java_parser: Parser,
        type_inference_engine: JavaTypeInferenceEngine,
        mock_ast_cache: MagicMock,
    ) -> None:
        java_code = b"""
package com.example;

public class Worker implements Runnable {
    @Override
    public void run() {
        System.out.println("Working...");
    }
}
"""
        tree = java_parser.parse(java_code)
        root_node = tree.root_node

        file_path = Path("/test/Worker.java")
        type_inference_engine.module_qn_to_file_path = {"com.example": file_path}
        mock_ast_cache.__contains__ = MagicMock(side_effect=lambda x: x == file_path)
        mock_ast_cache.__getitem__ = MagicMock(return_value=(root_node, java_code))

        result = type_inference_engine._get_implemented_interfaces("com.example.Worker")
        assert "Runnable" in result

    def test_get_implemented_interfaces_multiple(
        self,
        java_parser: Parser,
        type_inference_engine: JavaTypeInferenceEngine,
        mock_ast_cache: MagicMock,
    ) -> None:
        java_code = b"""
package com.example;

import java.io.Serializable;

public class Data implements Serializable, Comparable<Data>, Cloneable {
    private int id;

    @Override
    public int compareTo(Data other) {
        return Integer.compare(this.id, other.id);
    }
}
"""
        tree = java_parser.parse(java_code)
        root_node = tree.root_node

        file_path = Path("/test/Data.java")
        type_inference_engine.module_qn_to_file_path = {"com.example": file_path}
        mock_ast_cache.__contains__ = MagicMock(side_effect=lambda x: x == file_path)
        mock_ast_cache.__getitem__ = MagicMock(return_value=(root_node, java_code))

        result = type_inference_engine._get_implemented_interfaces("com.example.Data")
        assert "Serializable" in result
        assert "Cloneable" in result

    def test_get_implemented_interfaces_none(
        self,
        java_parser: Parser,
        type_inference_engine: JavaTypeInferenceEngine,
        mock_ast_cache: MagicMock,
    ) -> None:
        java_code = b"""
package com.example;

public class Plain {
    private String value;
}
"""
        tree = java_parser.parse(java_code)
        root_node = tree.root_node

        file_path = Path("/test/Plain.java")
        type_inference_engine.module_qn_to_file_path = {"com.example": file_path}
        mock_ast_cache.__contains__ = MagicMock(side_effect=lambda x: x == file_path)
        mock_ast_cache.__getitem__ = MagicMock(return_value=(root_node, java_code))

        result = type_inference_engine._get_implemented_interfaces("com.example.Plain")
        assert result == []

    def test_get_current_class_name_class(
        self,
        java_parser: Parser,
        type_inference_engine: JavaTypeInferenceEngine,
        mock_ast_cache: MagicMock,
    ) -> None:
        java_code = b"""
package com.example;

public class MyService {
    public void process() {}
}
"""
        tree = java_parser.parse(java_code)
        root_node = tree.root_node

        file_path = Path("/test/MyService.java")
        type_inference_engine.module_qn_to_file_path = {"com.example": file_path}
        mock_ast_cache.__contains__ = MagicMock(side_effect=lambda x: x == file_path)
        mock_ast_cache.__getitem__ = MagicMock(return_value=(root_node, java_code))

        result = type_inference_engine._get_current_class_name("com.example")
        assert result == "com.example.MyService"

    def test_get_current_class_name_interface(
        self,
        java_parser: Parser,
        type_inference_engine: JavaTypeInferenceEngine,
        mock_ast_cache: MagicMock,
    ) -> None:
        java_code = b"""
package com.example;

public interface Repository {
    void save(Object entity);
    Object findById(int id);
}
"""
        tree = java_parser.parse(java_code)
        root_node = tree.root_node

        file_path = Path("/test/Repository.java")
        type_inference_engine.module_qn_to_file_path = {"com.example": file_path}
        mock_ast_cache.__contains__ = MagicMock(side_effect=lambda x: x == file_path)
        mock_ast_cache.__getitem__ = MagicMock(return_value=(root_node, java_code))

        result = type_inference_engine._get_current_class_name("com.example")
        assert result == "com.example.Repository"

    def test_get_current_class_name_enum(
        self,
        java_parser: Parser,
        type_inference_engine: JavaTypeInferenceEngine,
        mock_ast_cache: MagicMock,
    ) -> None:
        java_code = b"""
package com.example;

public enum Status {
    PENDING,
    ACTIVE,
    COMPLETED
}
"""
        tree = java_parser.parse(java_code)
        root_node = tree.root_node

        file_path = Path("/test/Status.java")
        type_inference_engine.module_qn_to_file_path = {"com.example": file_path}
        mock_ast_cache.__contains__ = MagicMock(side_effect=lambda x: x == file_path)
        mock_ast_cache.__getitem__ = MagicMock(return_value=(root_node, java_code))

        result = type_inference_engine._get_current_class_name("com.example")
        assert result == "com.example.Status"

    def test_traverse_for_class_declarations_multiple_in_file(
        self,
        java_parser: Parser,
        type_inference_engine: JavaTypeInferenceEngine,
    ) -> None:
        java_code = b"""
package com.example;

public class MainClass {
    private Inner inner;
}

class Helper {
    public void help() {}
}

interface Processor {
    void process();
}

enum Type {
    A, B, C
}
"""
        tree = java_parser.parse(java_code)
        root_node = tree.root_node

        class_names: list[str] = []
        type_inference_engine._traverse_for_class_declarations(root_node, class_names)

        assert "MainClass" in class_names
        assert "Helper" in class_names
        assert "Processor" in class_names
        assert "Type" in class_names
        assert len(class_names) == 4

    def test_class_with_extends_and_implements(
        self,
        java_parser: Parser,
        type_inference_engine: JavaTypeInferenceEngine,
        mock_ast_cache: MagicMock,
    ) -> None:
        java_code = b"""
package com.example;

public class Employee extends Person implements Comparable<Employee>, Serializable {
    private String department;

    @Override
    public int compareTo(Employee other) {
        return this.getName().compareTo(other.getName());
    }
}
"""
        tree = java_parser.parse(java_code)
        root_node = tree.root_node

        file_path = Path("/test/Employee.java")
        type_inference_engine.module_qn_to_file_path = {"com.example": file_path}
        mock_ast_cache.__contains__ = MagicMock(side_effect=lambda x: x == file_path)
        mock_ast_cache.__getitem__ = MagicMock(return_value=(root_node, java_code))

        superclass = type_inference_engine._get_superclass_name("com.example.Employee")
        assert superclass == "Person"

        interfaces = type_inference_engine._get_implemented_interfaces(
            "com.example.Employee"
        )
        assert "Serializable" in interfaces

    def test_nested_class(
        self,
        java_parser: Parser,
        type_inference_engine: JavaTypeInferenceEngine,
    ) -> None:
        java_code = b"""
package com.example;

public class Outer {
    private class Inner extends BaseInner implements InnerInterface {
        public void doSomething() {}
    }
}
"""
        tree = java_parser.parse(java_code)
        root_node = tree.root_node

        result = type_inference_engine._find_superclass_using_ast(
            root_node, "Inner", "com.example"
        )
        assert result == "BaseInner"

        interfaces = type_inference_engine._find_interfaces_using_ast(
            root_node, "Inner", "com.example"
        )
        assert "InnerInterface" in interfaces
