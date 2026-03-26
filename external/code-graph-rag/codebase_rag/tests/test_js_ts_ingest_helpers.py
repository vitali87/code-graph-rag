from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from tree_sitter import Language, Parser

from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.tests.conftest import run_updater

try:
    import tree_sitter_javascript as tsjs

    JS_AVAILABLE = True
except ImportError:
    JS_AVAILABLE = False


@pytest.fixture
def temp_js_project(temp_repo: Path) -> Path:
    project_path = temp_repo / "js_helpers_test"
    project_path.mkdir()
    return project_path


@pytest.fixture
def js_parser() -> Parser | None:
    if not JS_AVAILABLE:
        return None
    language = Language(tsjs.language())
    return Parser(language)


@pytest.fixture
def definition_processor(
    temp_js_project: Path, mock_ingestor: MagicMock
) -> GraphUpdater:
    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=temp_js_project,
        parsers=parsers,
        queries=queries,
    )
    return updater


@pytest.mark.skipif(not JS_AVAILABLE, reason="tree-sitter-javascript not available")
class TestIsStaticMethodInClass:
    def test_static_method_returns_true(self, js_parser: Parser) -> None:
        code = b"""
class MyClass {
    static staticMethod() {
        return 'static';
    }
}
"""
        tree = js_parser.parse(code)
        class_body = tree.root_node.children[0].child_by_field_name("body")
        assert class_body is not None
        method_node = class_body.children[1]

        from codebase_rag.parsers.js_ts.ingest import JsTsIngestMixin

        mixin = JsTsIngestMixin()
        result = mixin._is_static_method_in_class(method_node)
        assert result is True

    def test_instance_method_returns_false(self, js_parser: Parser) -> None:
        code = b"""
class MyClass {
    instanceMethod() {
        return 'instance';
    }
}
"""
        tree = js_parser.parse(code)
        class_body = tree.root_node.children[0].child_by_field_name("body")
        assert class_body is not None
        method_node = class_body.children[1]

        from codebase_rag.parsers.js_ts.ingest import JsTsIngestMixin

        mixin = JsTsIngestMixin()
        result = mixin._is_static_method_in_class(method_node)
        assert result is False

    def test_non_method_returns_false(self, js_parser: Parser) -> None:
        code = b"function standalone() { return 'standalone'; }"
        tree = js_parser.parse(code)
        func_node = tree.root_node.children[0]

        from codebase_rag.parsers.js_ts.ingest import JsTsIngestMixin

        mixin = JsTsIngestMixin()
        result = mixin._is_static_method_in_class(func_node)
        assert result is False


@pytest.mark.skipif(not JS_AVAILABLE, reason="tree-sitter-javascript not available")
class TestIsMethodInClass:
    def test_method_in_class_returns_true(self, js_parser: Parser) -> None:
        code = b"""
class MyClass {
    myMethod() {
        return 'method';
    }
}
"""
        tree = js_parser.parse(code)
        class_body = tree.root_node.children[0].child_by_field_name("body")
        assert class_body is not None
        method_node = class_body.children[1]

        from codebase_rag.parsers.js_ts.ingest import JsTsIngestMixin

        mixin = JsTsIngestMixin()
        result = mixin._is_method_in_class(method_node)
        assert result is True

    def test_standalone_function_returns_false(self, js_parser: Parser) -> None:
        code = b"function standalone() { return 'standalone'; }"
        tree = js_parser.parse(code)
        func_node = tree.root_node.children[0]

        from codebase_rag.parsers.js_ts.ingest import JsTsIngestMixin

        mixin = JsTsIngestMixin()
        result = mixin._is_method_in_class(func_node)
        assert result is False

    def test_object_method_returns_false(self, js_parser: Parser) -> None:
        code = b"""
const obj = {
    method() {
        return 'object method';
    }
};
"""
        tree = js_parser.parse(code)
        var_decl = tree.root_node.children[0]
        declarator = var_decl.children[1]
        obj = declarator.child_by_field_name("value")
        assert obj is not None
        method_node = obj.children[1]

        from codebase_rag.parsers.js_ts.ingest import JsTsIngestMixin

        mixin = JsTsIngestMixin()
        result = mixin._is_method_in_class(method_node)
        assert result is False


@pytest.mark.skipif(not JS_AVAILABLE, reason="tree-sitter-javascript not available")
class TestIsClassMethod:
    def test_class_method_returns_true(self, js_parser: Parser) -> None:
        code = b"""
class MyClass {
    myMethod() {
        return 'method';
    }
}
"""
        tree = js_parser.parse(code)
        class_body = tree.root_node.children[0].child_by_field_name("body")
        assert class_body is not None
        method_node = class_body.children[1]

        from codebase_rag.parsers.handlers.js_ts import JsTsHandler

        handler = JsTsHandler()
        result = handler.is_class_method(method_node)
        assert result is True

    def test_standalone_function_returns_false(self, js_parser: Parser) -> None:
        code = b"function standalone() { return 'standalone'; }"
        tree = js_parser.parse(code)
        func_node = tree.root_node.children[0]

        from codebase_rag.parsers.handlers.js_ts import JsTsHandler

        handler = JsTsHandler()
        result = handler.is_class_method(func_node)
        assert result is False


@pytest.mark.skipif(not JS_AVAILABLE, reason="tree-sitter-javascript not available")
class TestIsExportInsideFunction:
    def test_export_at_module_level_returns_false(self, js_parser: Parser) -> None:
        code = b"export function myFunc() { return 'exported'; }"
        tree = js_parser.parse(code)
        export_node = tree.root_node.children[0]

        from codebase_rag.parsers.handlers.js_ts import JsTsHandler

        handler = JsTsHandler()
        result = handler.is_export_inside_function(export_node)
        assert result is False

    def test_export_inside_function_returns_true(self, js_parser: Parser) -> None:
        code = b"""
function outer() {
    module.exports.inner = function() { return 'inner'; };
}
"""
        tree = js_parser.parse(code)
        func_node = tree.root_node.children[0]
        body = func_node.child_by_field_name("body")
        assert body is not None
        expr_stmt = body.children[1]

        from codebase_rag.parsers.handlers.js_ts import JsTsHandler

        handler = JsTsHandler()
        result = handler.is_export_inside_function(expr_stmt)
        assert result is True


@pytest.mark.skipif(not JS_AVAILABLE, reason="tree-sitter-javascript not available")
class TestFindObjectNameForMethod:
    def test_finds_object_name_from_variable_declarator(
        self, js_parser: Parser
    ) -> None:
        code = b"""
const myObject = {
    myMethod() {
        return 'method';
    }
};
"""
        tree = js_parser.parse(code)
        var_decl = tree.root_node.children[0]
        declarator = var_decl.children[1]
        obj = declarator.child_by_field_name("value")
        assert obj is not None
        pair = obj.children[1]
        method_name_node = pair.children[0]

        from codebase_rag.parsers.js_ts.ingest import JsTsIngestMixin

        mixin = JsTsIngestMixin()
        result = mixin._find_object_name_for_method(method_name_node)
        assert result == "myObject"

    def test_returns_none_for_anonymous_object(self, js_parser: Parser) -> None:
        code = b"""
doSomething({
    method() {
        return 'anonymous';
    }
});
"""
        tree = js_parser.parse(code)
        expr_stmt = tree.root_node.children[0]
        call_expr = expr_stmt.children[0]
        args = call_expr.child_by_field_name("arguments")
        assert args is not None
        obj = args.children[1]
        pair = obj.children[1]
        method_name_node = pair.children[0]

        from codebase_rag.parsers.js_ts.ingest import JsTsIngestMixin

        mixin = JsTsIngestMixin()
        result = mixin._find_object_name_for_method(method_name_node)
        assert result is None


@pytest.mark.skipif(not JS_AVAILABLE, reason="tree-sitter-javascript not available")
class TestIsInsideMethodWithObjectLiterals:
    def test_object_in_class_method_returns_true(self, js_parser: Parser) -> None:
        code = b"""
class MyClass {
    myMethod() {
        return {
            nested() {
                return 'nested';
            }
        };
    }
}
"""
        tree = js_parser.parse(code)
        class_body = tree.root_node.children[0].child_by_field_name("body")
        assert class_body is not None
        method_def = class_body.children[1]
        method_body = method_def.child_by_field_name("body")
        assert method_body is not None
        return_stmt = method_body.children[1]
        obj = return_stmt.children[1]
        pair = obj.children[1]
        nested_func = pair.children[0]

        from codebase_rag.parsers.handlers.js_ts import JsTsHandler

        handler = JsTsHandler()
        result = handler.is_inside_method_with_object_literals(nested_func)
        assert result is True

    def test_standalone_object_returns_false(self, js_parser: Parser) -> None:
        code = b"""
const obj = {
    method() {
        return 'method';
    }
};
"""
        tree = js_parser.parse(code)
        var_decl = tree.root_node.children[0]
        declarator = var_decl.children[1]
        obj = declarator.child_by_field_name("value")
        assert obj is not None
        pair = obj.children[1]

        from codebase_rag.parsers.handlers.js_ts import JsTsHandler

        handler = JsTsHandler()
        result = handler.is_inside_method_with_object_literals(pair)
        assert result is False


@pytest.mark.skipif(not JS_AVAILABLE, reason="tree-sitter-javascript not available")
class TestPrototypeMethodIngestion:
    def test_prototype_methods_are_ingested(
        self, temp_js_project: Path, mock_ingestor: MagicMock
    ) -> None:
        (temp_js_project / "prototype_test.js").write_text(
            encoding="utf-8",
            data="""
function Person(name) {
    this.name = name;
}

Person.prototype.greet = function() {
    return 'Hello, ' + this.name;
};

Person.prototype.farewell = function() {
    return 'Goodbye, ' + this.name;
};
""",
        )

        run_updater(temp_js_project, mock_ingestor)

        calls = mock_ingestor.ensure_node_batch.call_args_list
        function_qns = [
            call[0][1]["qualified_name"]
            for call in calls
            if call[0][0] == "Function"
            and "prototype_test" in call[0][1].get("qualified_name", "")
        ]

        assert any("Person" in qn for qn in function_qns)
        assert any("greet" in qn for qn in function_qns)
        assert any("farewell" in qn for qn in function_qns)

    def test_prototype_inheritance_creates_relationship(
        self, temp_js_project: Path, mock_ingestor: MagicMock
    ) -> None:
        (temp_js_project / "inheritance_test.js").write_text(
            encoding="utf-8",
            data="""
function Animal(name) {
    this.name = name;
}

Animal.prototype.speak = function() {
    return this.name + ' makes a sound';
};

function Dog(name, breed) {
    Animal.call(this, name);
    this.breed = breed;
}

Dog.prototype = Object.create(Animal.prototype);
Dog.prototype.constructor = Dog;

Dog.prototype.bark = function() {
    return 'Woof!';
};
""",
        )

        run_updater(temp_js_project, mock_ingestor)

        rel_calls = mock_ingestor.ensure_relationship_batch.call_args_list
        inherits_rels = [call for call in rel_calls if call.args[1] == "INHERITS"]

        assert len(inherits_rels) >= 1


@pytest.mark.skipif(not JS_AVAILABLE, reason="tree-sitter-javascript not available")
class TestObjectLiteralMethodIngestion:
    def test_object_literal_methods_are_ingested(
        self, temp_js_project: Path, mock_ingestor: MagicMock
    ) -> None:
        (temp_js_project / "object_methods.js").write_text(
            encoding="utf-8",
            data="""
const calculator = {
    add(a, b) {
        return a + b;
    },
    subtract(a, b) {
        return a - b;
    },
    multiply: function(a, b) {
        return a * b;
    }
};
""",
        )

        run_updater(temp_js_project, mock_ingestor)

        calls = mock_ingestor.ensure_node_batch.call_args_list
        function_qns = [
            call[0][1]["qualified_name"]
            for call in calls
            if call[0][0] == "Function"
            and "object_methods" in call[0][1].get("qualified_name", "")
        ]

        assert any("add" in qn for qn in function_qns)
        assert any("subtract" in qn for qn in function_qns)
        assert any("multiply" in qn for qn in function_qns)


@pytest.mark.skipif(not JS_AVAILABLE, reason="tree-sitter-javascript not available")
class TestArrowFunctionIngestion:
    def test_arrow_functions_in_objects_are_ingested(
        self, temp_js_project: Path, mock_ingestor: MagicMock
    ) -> None:
        (temp_js_project / "arrow_functions.js").write_text(
            encoding="utf-8",
            data="""
const utils = {
    double: (x) => x * 2,
    triple: (x) => x * 3,
    square: x => x * x
};
""",
        )

        run_updater(temp_js_project, mock_ingestor)

        calls = mock_ingestor.ensure_node_batch.call_args_list
        function_qns = [
            call[0][1]["qualified_name"]
            for call in calls
            if call[0][0] == "Function"
            and "arrow_functions" in call[0][1].get("qualified_name", "")
        ]

        assert any("double" in qn for qn in function_qns)
        assert any("triple" in qn for qn in function_qns)
        assert any("square" in qn for qn in function_qns)

    def test_assignment_arrow_functions_are_ingested(
        self, temp_js_project: Path, mock_ingestor: MagicMock
    ) -> None:
        (temp_js_project / "assignment_arrows.js").write_text(
            encoding="utf-8",
            data="""
const obj = {};
obj.method1 = () => 'method1';
obj.method2 = (x) => x * 2;
""",
        )

        run_updater(temp_js_project, mock_ingestor)

        calls = mock_ingestor.ensure_node_batch.call_args_list
        function_qns = [
            call[0][1]["qualified_name"]
            for call in calls
            if call[0][0] == "Function"
            and "assignment_arrows" in call[0][1].get("qualified_name", "")
        ]

        assert any("method1" in qn for qn in function_qns)
        assert any("method2" in qn for qn in function_qns)
