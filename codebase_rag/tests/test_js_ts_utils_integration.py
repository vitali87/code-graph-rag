from pathlib import Path

import pytest
from tree_sitter import Language, Parser, Tree

from codebase_rag.parsers.js_ts.utils import (
    _extract_class_qn,
    analyze_return_expression,
    extract_constructor_name,
    extract_method_call,
    find_method_in_ast,
    find_method_in_class_body,
    find_return_statements,
)

try:
    import tree_sitter_javascript as tsjs

    JS_AVAILABLE = True
except ImportError:
    JS_AVAILABLE = False

try:
    import tree_sitter_typescript as tsts

    TS_AVAILABLE = True
except ImportError:
    TS_AVAILABLE = False


@pytest.fixture
def js_parser() -> Parser | None:
    if not JS_AVAILABLE:
        return None
    language = Language(tsjs.language())
    return Parser(language)


@pytest.fixture
def ts_parser() -> Parser | None:
    if not TS_AVAILABLE:
        return None
    language = Language(tsts.language_typescript())
    return Parser(language)


@pytest.fixture
def sample_js_project(tmp_path: Path) -> Path:
    project = tmp_path / "js_project"
    project.mkdir()

    (project / "singleton.js").write_text("""
class DatabaseConnection {
    static instance = null;

    constructor(config) {
        this.config = config;
        this.connected = false;
    }

    static getInstance() {
        if (!DatabaseConnection.instance) {
            DatabaseConnection.instance = new DatabaseConnection({});
        }
        return DatabaseConnection.instance;
    }

    connect() {
        this.connected = true;
        return this;
    }

    query(sql) {
        if (!this.connected) {
            return null;
        }
        return { rows: [], sql: sql };
    }
}

class UserRepository {
    constructor() {
        this.db = DatabaseConnection.getInstance();
    }

    findById(id) {
        return this.db.query('SELECT * FROM users WHERE id = ' + id);
    }

    save(user) {
        return this.db.query('INSERT INTO users VALUES (...)');
    }
}
""")

    (project / "factory.js").write_text("""
class Animal {
    constructor(name) {
        this.name = name;
    }

    speak() {
        return this.name + ' makes a sound';
    }
}

class Dog extends Animal {
    constructor(name, breed) {
        super(name);
        this.breed = breed;
    }

    speak() {
        return this.name + ' barks';
    }

    fetch() {
        return this;
    }
}

class Cat extends Animal {
    speak() {
        return this.name + ' meows';
    }

    scratch() {
        return new Cat(this.name);
    }
}

class AnimalFactory {
    static createAnimal(type, name) {
        if (type === 'dog') {
            return new Dog(name, 'mixed');
        } else if (type === 'cat') {
            return new Cat(name);
        }
        return new Animal(name);
    }

    static createDog(name, breed) {
        return new Dog(name, breed);
    }
}
""")

    (project / "complex_returns.js").write_text("""
class Builder {
    constructor() {
        this.options = {};
    }

    setName(name) {
        this.options.name = name;
        return this;
    }

    setAge(age) {
        this.options.age = age;
        return this;
    }

    setActive(active) {
        if (active) {
            this.options.active = true;
            return this;
        } else {
            this.options.active = false;
            return this;
        }
    }

    build() {
        if (!this.options.name) {
            return null;
        }
        return new Result(this.options);
    }

    clone() {
        const newBuilder = new Builder();
        newBuilder.options = { ...this.options };
        return newBuilder;
    }
}

class Result {
    constructor(data) {
        this.data = data;
    }
}

class ChainedService {
    static instance = null;

    static getInstance() {
        if (!ChainedService.instance) {
            ChainedService.instance = new ChainedService();
        }
        return ChainedService.instance;
    }

    process() {
        return this.validate().transform().output();
    }

    validate() {
        return this;
    }

    transform() {
        return this;
    }

    output() {
        return { result: 'success' };
    }
}
""")

    return project


@pytest.fixture
def sample_ts_project(tmp_path: Path) -> Path:
    project = tmp_path / "ts_project"
    project.mkdir()

    (project / "generics.ts").write_text("""
class Container<T> {
    private value: T;

    constructor(value: T) {
        this.value = value;
    }

    getValue(): T {
        return this.value;
    }

    map<U>(fn: (value: T) => U): Container<U> {
        return new Container(fn(this.value));
    }

    flatMap<U>(fn: (value: T) => Container<U>): Container<U> {
        return fn(this.value);
    }

    static of<T>(value: T): Container<T> {
        return new Container(value);
    }
}

class Repository<T> {
    private items: T[] = [];

    add(item: T): this {
        this.items.push(item);
        return this;
    }

    find(predicate: (item: T) => boolean): T | undefined {
        return this.items.find(predicate);
    }

    getAll(): T[] {
        return [...this.items];
    }
}
""")

    (project / "nested_classes.ts").write_text("""
class Outer {
    private inner: Inner;

    constructor() {
        this.inner = new Inner();
    }

    getInner(): Inner {
        return this.inner;
    }

    process(): Result {
        return this.inner.compute();
    }

    static createWithValue(value: number): Outer {
        const outer = new Outer();
        outer.inner.setValue(value);
        return outer;
    }
}

class Inner {
    private value: number = 0;

    setValue(v: number): this {
        this.value = v;
        return this;
    }

    getValue(): number {
        return this.value;
    }

    compute(): Result {
        return new Result(this.value * 2);
    }
}

class Result {
    constructor(public readonly value: number) {}

    isPositive(): boolean {
        return this.value > 0;
    }
}
""")

    return project


def parse_file(parser: Parser, file_path: Path) -> Tree:
    content = file_path.read_bytes()
    return parser.parse(content)


@pytest.mark.skipif(not JS_AVAILABLE, reason="tree-sitter-javascript not available")
class TestExtractMethodCallIntegration:
    def test_chained_method_calls_in_singleton(
        self, js_parser: Parser, sample_js_project: Path
    ) -> None:
        tree = parse_file(js_parser, sample_js_project / "singleton.js")

        member_exprs = []
        stack = [tree.root_node]
        while stack:
            node = stack.pop()
            if node.type == "member_expression":
                member_exprs.append(node)
            stack.extend(reversed(node.children))

        extracted_calls = [extract_method_call(expr) for expr in member_exprs]
        extracted_calls = [c for c in extracted_calls if c]

        assert any("DatabaseConnection.instance" in c for c in extracted_calls)
        assert any("DatabaseConnection.getInstance" in c for c in extracted_calls)

    def test_method_calls_in_factory(
        self, js_parser: Parser, sample_js_project: Path
    ) -> None:
        tree = parse_file(js_parser, sample_js_project / "factory.js")

        member_exprs = []
        stack = [tree.root_node]
        while stack:
            node = stack.pop()
            if node.type == "member_expression":
                member_exprs.append(node)
            stack.extend(reversed(node.children))

        extracted_calls = [extract_method_call(expr) for expr in member_exprs]
        extracted_calls = [c for c in extracted_calls if c]

        assert any("this.name" in c for c in extracted_calls)
        assert any("this.breed" in c for c in extracted_calls)


@pytest.mark.skipif(not JS_AVAILABLE, reason="tree-sitter-javascript not available")
class TestFindMethodInAstIntegration:
    def test_find_singleton_methods(
        self, js_parser: Parser, sample_js_project: Path
    ) -> None:
        tree = parse_file(js_parser, sample_js_project / "singleton.js")

        get_instance = find_method_in_ast(
            tree.root_node, "DatabaseConnection", "getInstance"
        )
        assert get_instance is not None
        assert get_instance.type == "method_definition"

        connect = find_method_in_ast(tree.root_node, "DatabaseConnection", "connect")
        assert connect is not None

        query = find_method_in_ast(tree.root_node, "DatabaseConnection", "query")
        assert query is not None

    def test_find_methods_in_inheritance_hierarchy(
        self, js_parser: Parser, sample_js_project: Path
    ) -> None:
        tree = parse_file(js_parser, sample_js_project / "factory.js")

        animal_speak = find_method_in_ast(tree.root_node, "Animal", "speak")
        assert animal_speak is not None

        dog_speak = find_method_in_ast(tree.root_node, "Dog", "speak")
        assert dog_speak is not None

        dog_fetch = find_method_in_ast(tree.root_node, "Dog", "fetch")
        assert dog_fetch is not None

        cat_speak = find_method_in_ast(tree.root_node, "Cat", "speak")
        assert cat_speak is not None

        cat_scratch = find_method_in_ast(tree.root_node, "Cat", "scratch")
        assert cat_scratch is not None

    def test_find_static_factory_methods(
        self, js_parser: Parser, sample_js_project: Path
    ) -> None:
        tree = parse_file(js_parser, sample_js_project / "factory.js")

        create_animal = find_method_in_ast(
            tree.root_node, "AnimalFactory", "createAnimal"
        )
        assert create_animal is not None

        create_dog = find_method_in_ast(tree.root_node, "AnimalFactory", "createDog")
        assert create_dog is not None

    def test_nonexistent_method_returns_none(
        self, js_parser: Parser, sample_js_project: Path
    ) -> None:
        tree = parse_file(js_parser, sample_js_project / "factory.js")

        result = find_method_in_ast(tree.root_node, "Dog", "nonexistent")
        assert result is None

        result = find_method_in_ast(tree.root_node, "NonexistentClass", "speak")
        assert result is None


@pytest.mark.skipif(not JS_AVAILABLE, reason="tree-sitter-javascript not available")
class TestFindMethodInClassBodyIntegration:
    def test_find_all_builder_methods(
        self, js_parser: Parser, sample_js_project: Path
    ) -> None:
        tree = parse_file(js_parser, sample_js_project / "complex_returns.js")

        builder_class = None
        stack = [tree.root_node]
        while stack:
            node = stack.pop()
            if node.type == "class_declaration":
                name_node = node.child_by_field_name("name")
                if name_node and name_node.text == b"Builder":
                    builder_class = node
                    break
            stack.extend(reversed(node.children))

        assert builder_class is not None
        class_body = builder_class.child_by_field_name("body")
        assert class_body is not None

        expected_methods = [
            "constructor",
            "setName",
            "setAge",
            "setActive",
            "build",
            "clone",
        ]
        for method_name in expected_methods:
            method = find_method_in_class_body(class_body, method_name)
            assert method is not None, f"Method {method_name} not found"


@pytest.mark.skipif(not JS_AVAILABLE, reason="tree-sitter-javascript not available")
class TestFindReturnStatementsIntegration:
    def test_multiple_returns_in_conditional(
        self, js_parser: Parser, sample_js_project: Path
    ) -> None:
        tree = parse_file(js_parser, sample_js_project / "complex_returns.js")

        set_active = find_method_in_ast(tree.root_node, "Builder", "setActive")
        assert set_active is not None

        return_nodes: list = []
        find_return_statements(set_active, return_nodes)
        assert len(return_nodes) == 2

    def test_returns_in_factory_method(
        self, js_parser: Parser, sample_js_project: Path
    ) -> None:
        tree = parse_file(js_parser, sample_js_project / "factory.js")

        create_animal = find_method_in_ast(
            tree.root_node, "AnimalFactory", "createAnimal"
        )
        assert create_animal is not None

        return_nodes: list = []
        find_return_statements(create_animal, return_nodes)
        assert len(return_nodes) == 3

    def test_single_return_in_simple_method(
        self, js_parser: Parser, sample_js_project: Path
    ) -> None:
        tree = parse_file(js_parser, sample_js_project / "singleton.js")

        connect = find_method_in_ast(tree.root_node, "DatabaseConnection", "connect")
        assert connect is not None

        return_nodes: list = []
        find_return_statements(connect, return_nodes)
        assert len(return_nodes) == 1


@pytest.mark.skipif(not JS_AVAILABLE, reason="tree-sitter-javascript not available")
class TestExtractConstructorNameIntegration:
    def test_extract_from_factory_returns(
        self, js_parser: Parser, sample_js_project: Path
    ) -> None:
        tree = parse_file(js_parser, sample_js_project / "factory.js")

        new_expressions = []
        stack = [tree.root_node]
        while stack:
            node = stack.pop()
            if node.type == "new_expression":
                new_expressions.append(node)
            stack.extend(reversed(node.children))

        constructor_names = [extract_constructor_name(expr) for expr in new_expressions]
        constructor_names = [n for n in constructor_names if n]

        assert "Dog" in constructor_names
        assert "Cat" in constructor_names
        assert "Animal" in constructor_names

    def test_extract_from_singleton_pattern(
        self, js_parser: Parser, sample_js_project: Path
    ) -> None:
        tree = parse_file(js_parser, sample_js_project / "singleton.js")

        new_expressions = []
        stack = [tree.root_node]
        while stack:
            node = stack.pop()
            if node.type == "new_expression":
                new_expressions.append(node)
            stack.extend(reversed(node.children))

        constructor_names = [extract_constructor_name(expr) for expr in new_expressions]
        constructor_names = [n for n in constructor_names if n]

        assert "DatabaseConnection" in constructor_names


@pytest.mark.skipif(not JS_AVAILABLE, reason="tree-sitter-javascript not available")
class TestAnalyzeReturnExpressionIntegration:
    def test_builder_pattern_returns_this(
        self, js_parser: Parser, sample_js_project: Path
    ) -> None:
        tree = parse_file(js_parser, sample_js_project / "complex_returns.js")

        set_name = find_method_in_ast(tree.root_node, "Builder", "setName")
        assert set_name is not None

        return_nodes: list = []
        find_return_statements(set_name, return_nodes)
        assert len(return_nodes) == 1

        return_expr = (
            return_nodes[0].children[1] if len(return_nodes[0].children) > 1 else None
        )
        assert return_expr is not None

        result = analyze_return_expression(return_expr, "project.Builder.setName")
        assert result == "project.Builder"

    def test_factory_returns_new_instance(
        self, js_parser: Parser, sample_js_project: Path
    ) -> None:
        tree = parse_file(js_parser, sample_js_project / "factory.js")

        create_dog = find_method_in_ast(tree.root_node, "AnimalFactory", "createDog")
        assert create_dog is not None

        return_nodes: list = []
        find_return_statements(create_dog, return_nodes)
        assert len(return_nodes) == 1

        return_expr = (
            return_nodes[0].children[1] if len(return_nodes[0].children) > 1 else None
        )
        assert return_expr is not None

        result = analyze_return_expression(
            return_expr, "project.AnimalFactory.createDog"
        )
        assert result == "project.AnimalFactory"

    def test_singleton_getInstance_returns_static_instance(
        self, js_parser: Parser, sample_js_project: Path
    ) -> None:
        tree = parse_file(js_parser, sample_js_project / "singleton.js")

        get_instance = find_method_in_ast(
            tree.root_node, "DatabaseConnection", "getInstance"
        )
        assert get_instance is not None

        return_nodes: list = []
        find_return_statements(get_instance, return_nodes)
        assert len(return_nodes) >= 1

        last_return = return_nodes[-1]
        return_expr = last_return.children[1] if len(last_return.children) > 1 else None
        assert return_expr is not None

        result = analyze_return_expression(
            return_expr, "project.DatabaseConnection.getInstance"
        )
        assert result == "project.DatabaseConnection"


@pytest.mark.skipif(not JS_AVAILABLE, reason="tree-sitter-javascript not available")
class TestExtractClassQnIntegration:
    def test_extract_from_method_qn(self) -> None:
        assert _extract_class_qn("project.MyClass.myMethod") == "project.MyClass"
        assert _extract_class_qn("pkg.subpkg.Service.process") == "pkg.subpkg.Service"
        assert _extract_class_qn("MyClass.method") == "MyClass"

    def test_single_part_returns_none(self) -> None:
        assert _extract_class_qn("method") is None

    def test_deeply_nested_qn(self) -> None:
        result = _extract_class_qn("a.b.c.d.e.method")
        assert result == "a.b.c.d.e"


@pytest.mark.skipif(not TS_AVAILABLE, reason="tree-sitter-typescript not available")
class TestTypeScriptIntegration:
    def test_find_generic_class_methods(
        self, ts_parser: Parser, sample_ts_project: Path
    ) -> None:
        tree = parse_file(ts_parser, sample_ts_project / "generics.ts")

        get_value = find_method_in_ast(tree.root_node, "Container", "getValue")
        assert get_value is not None

        map_method = find_method_in_ast(tree.root_node, "Container", "map")
        assert map_method is not None

        flat_map = find_method_in_ast(tree.root_node, "Container", "flatMap")
        assert flat_map is not None

        of_method = find_method_in_ast(tree.root_node, "Container", "of")
        assert of_method is not None

    def test_find_repository_methods(
        self, ts_parser: Parser, sample_ts_project: Path
    ) -> None:
        tree = parse_file(ts_parser, sample_ts_project / "generics.ts")

        add = find_method_in_ast(tree.root_node, "Repository", "add")
        assert add is not None

        find = find_method_in_ast(tree.root_node, "Repository", "find")
        assert find is not None

        get_all = find_method_in_ast(tree.root_node, "Repository", "getAll")
        assert get_all is not None

    def test_nested_class_interactions(
        self, ts_parser: Parser, sample_ts_project: Path
    ) -> None:
        tree = parse_file(ts_parser, sample_ts_project / "nested_classes.ts")

        outer_process = find_method_in_ast(tree.root_node, "Outer", "process")
        assert outer_process is not None

        inner_compute = find_method_in_ast(tree.root_node, "Inner", "compute")
        assert inner_compute is not None

        create_with_value = find_method_in_ast(
            tree.root_node, "Outer", "createWithValue"
        )
        assert create_with_value is not None

    def test_return_types_in_typescript(
        self, ts_parser: Parser, sample_ts_project: Path
    ) -> None:
        tree = parse_file(ts_parser, sample_ts_project / "nested_classes.ts")

        compute = find_method_in_ast(tree.root_node, "Inner", "compute")
        assert compute is not None

        return_nodes: list = []
        find_return_statements(compute, return_nodes)
        assert len(return_nodes) == 1

        return_expr = (
            return_nodes[0].children[1] if len(return_nodes[0].children) > 1 else None
        )
        assert return_expr is not None

        constructor_name = extract_constructor_name(return_expr)
        assert constructor_name == "Result"
