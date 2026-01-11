from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from codebase_rag.constants import NodeLabel
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers

if TYPE_CHECKING:
    from codebase_rag.services.graph_service import MemgraphIngestor

pytestmark = [pytest.mark.integration]


PYTHON_CODE = """\
class MyClass:
    def method1(self):
        pass

    def method2(self):
        self.method1()

class AnotherClass(MyClass):
    def override_method(self):
        pass

def standalone_function():
    obj = MyClass()
    obj.method1()
"""

TYPESCRIPT_CODE = """\
interface MyInterface {
    value: number;
    getValue(): number;
}

enum Status {
    Active = "active",
    Inactive = "inactive",
}

type MyType = string | number;

class MyTsClass implements MyInterface {
    value: number = 0;

    getValue(): number {
        return this.value;
    }
}

function createInstance(): MyTsClass {
    return new MyTsClass();
}
"""

JAVASCRIPT_CODE = """\
class MyJsClass {
    constructor() {
        this.value = 0;
    }

    getValue() {
        return this.value;
    }
}

function createInstance() {
    return new MyJsClass();
}

module.exports = { MyJsClass, createInstance };
"""

RUST_CODE = """\
pub struct MyStruct {
    value: i32,
}

impl MyStruct {
    pub fn new() -> Self {
        MyStruct { value: 0 }
    }

    pub fn get_value(&self) -> i32 {
        self.value
    }
}

pub enum Status {
    Active,
    Inactive,
}

pub trait MyTrait {
    fn do_something(&self);
}

impl MyTrait for MyStruct {
    fn do_something(&self) {
        println!("{}", self.value);
    }
}

pub fn standalone_fn() -> MyStruct {
    MyStruct::new()
}
"""

GO_CODE = """\
package main

type MyStruct struct {
    Value int
}

func (m *MyStruct) GetValue() int {
    return m.Value
}

type MyInterface interface {
    DoSomething()
}

func NewMyStruct() *MyStruct {
    return &MyStruct{Value: 0}
}

func main() {
    s := NewMyStruct()
    _ = s.GetValue()
}
"""

SCALA_CODE = """\
class MyScalaClass {
  def getValue: Int = 42
}

trait MyTrait {
  def doSomething(): Unit
}

object MyObject {
  def create(): MyScalaClass = new MyScalaClass()
}

sealed trait Status
case object Active extends Status
case object Inactive extends Status
"""

JAVA_CODE = """\
public class Example {
    private int value;

    public Example() {
        this.value = 0;
    }

    public int getValue() {
        return this.value;
    }
}

interface MyInterface {
    void doSomething();
}

enum Status {
    ACTIVE,
    INACTIVE
}
"""

CPP_CODE = """\
class MyCppClass {
public:
    MyCppClass() : value(0) {}

    int getValue() {
        return value;
    }

private:
    int value;
};

enum Status {
    Active,
    Inactive
};

void standaloneFunction() {
    MyCppClass obj;
    obj.getValue();
}
"""

CSHARP_CODE = """\
public class MyCSharpClass {
    private int value;

    public MyCSharpClass() {
        this.value = 0;
    }

    public int GetValue() {
        return this.value;
    }
}

public interface IMyInterface {
    void DoSomething();
}

public enum Status {
    Active,
    Inactive
}
"""

PHP_CODE = """\
<?php

class MyPhpClass {
    private $value;

    public function __construct() {
        $this->value = 0;
    }

    public function getValue() {
        return $this->value;
    }
}

interface MyInterface {
    public function doSomething();
}

enum Status {
    case Active;
    case Inactive;
}

function standaloneFunction() {
    $obj = new MyPhpClass();
    return $obj->getValue();
}
"""

LUA_CODE = """\
local MyClass = {}
MyClass.__index = MyClass

function MyClass.new()
    local self = setmetatable({}, MyClass)
    self.value = 0
    return self
end

function MyClass:getValue()
    return self.value
end

local function standaloneFunction()
    local obj = MyClass.new()
    return obj:getValue()
end

return MyClass
"""


def index_project(ingestor: MemgraphIngestor, project_path: Path) -> None:
    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=ingestor,
        repo_path=project_path,
        parsers=parsers,
        queries=queries,
    )
    updater.run()


def get_node_labels(ingestor: MemgraphIngestor) -> set[str]:
    result = ingestor.fetch_all("MATCH (n) RETURN DISTINCT labels(n) AS labels")
    labels: set[str] = set()
    for row in result:
        for label in row["labels"]:
            labels.add(label)
    return labels


def get_nodes_by_label(ingestor: MemgraphIngestor, label: str) -> list[dict]:
    return ingestor.fetch_all(f"MATCH (n:{label}) RETURN n.name AS name")


def get_relationship_types(ingestor: MemgraphIngestor) -> set[str]:
    result = ingestor.fetch_all("MATCH ()-[r]->() RETURN DISTINCT type(r) AS type")
    return {row["type"] for row in result}


@pytest.fixture
def python_project(tmp_path: Path) -> Path:
    project = tmp_path / "python_project"
    project.mkdir()
    (project / "example.py").write_text(PYTHON_CODE, encoding="utf-8")
    return project


@pytest.fixture
def typescript_project(tmp_path: Path) -> Path:
    project = tmp_path / "typescript_project"
    project.mkdir()
    (project / "example.ts").write_text(TYPESCRIPT_CODE, encoding="utf-8")
    return project


@pytest.fixture
def javascript_project(tmp_path: Path) -> Path:
    project = tmp_path / "javascript_project"
    project.mkdir()
    (project / "example.js").write_text(JAVASCRIPT_CODE, encoding="utf-8")
    return project


@pytest.fixture
def rust_project(tmp_path: Path) -> Path:
    project = tmp_path / "rust_project"
    project.mkdir()
    (project / "example.rs").write_text(RUST_CODE, encoding="utf-8")
    return project


@pytest.fixture
def go_project(tmp_path: Path) -> Path:
    project = tmp_path / "go_project"
    project.mkdir()
    (project / "example.go").write_text(GO_CODE, encoding="utf-8")
    return project


@pytest.fixture
def scala_project(tmp_path: Path) -> Path:
    project = tmp_path / "scala_project"
    project.mkdir()
    (project / "Example.scala").write_text(SCALA_CODE, encoding="utf-8")
    return project


@pytest.fixture
def java_project(tmp_path: Path) -> Path:
    project = tmp_path / "java_project"
    project.mkdir()
    (project / "Example.java").write_text(JAVA_CODE, encoding="utf-8")
    return project


@pytest.fixture
def cpp_project(tmp_path: Path) -> Path:
    project = tmp_path / "cpp_project"
    project.mkdir()
    (project / "example.cpp").write_text(CPP_CODE, encoding="utf-8")
    return project


@pytest.fixture
def csharp_project(tmp_path: Path) -> Path:
    project = tmp_path / "csharp_project"
    project.mkdir()
    (project / "Example.cs").write_text(CSHARP_CODE, encoding="utf-8")
    return project


@pytest.fixture
def php_project(tmp_path: Path) -> Path:
    project = tmp_path / "php_project"
    project.mkdir()
    (project / "example.php").write_text(PHP_CODE, encoding="utf-8")
    return project


@pytest.fixture
def lua_project(tmp_path: Path) -> Path:
    project = tmp_path / "lua_project"
    project.mkdir()
    (project / "example.lua").write_text(LUA_CODE, encoding="utf-8")
    return project


class TestPythonNodeLabels:
    def test_python_creates_class_nodes(
        self, memgraph_ingestor: MemgraphIngestor, python_project: Path
    ) -> None:
        index_project(memgraph_ingestor, python_project)

        labels = get_node_labels(memgraph_ingestor)
        assert NodeLabel.CLASS.value in labels

        classes = get_nodes_by_label(memgraph_ingestor, NodeLabel.CLASS.value)
        class_names = {n["name"] for n in classes}
        assert "MyClass" in class_names
        assert "AnotherClass" in class_names

    def test_python_creates_function_nodes(
        self, memgraph_ingestor: MemgraphIngestor, python_project: Path
    ) -> None:
        index_project(memgraph_ingestor, python_project)

        labels = get_node_labels(memgraph_ingestor)
        assert NodeLabel.FUNCTION.value in labels

        functions = get_nodes_by_label(memgraph_ingestor, NodeLabel.FUNCTION.value)
        func_names = {n["name"] for n in functions}
        assert "standalone_function" in func_names

    def test_python_creates_method_nodes(
        self, memgraph_ingestor: MemgraphIngestor, python_project: Path
    ) -> None:
        index_project(memgraph_ingestor, python_project)

        labels = get_node_labels(memgraph_ingestor)
        assert NodeLabel.METHOD.value in labels

        methods = get_nodes_by_label(memgraph_ingestor, NodeLabel.METHOD.value)
        method_names = {n["name"] for n in methods}
        assert "method1" in method_names
        assert "method2" in method_names

    def test_python_creates_defines_relationships(
        self, memgraph_ingestor: MemgraphIngestor, python_project: Path
    ) -> None:
        index_project(memgraph_ingestor, python_project)

        rel_types = get_relationship_types(memgraph_ingestor)
        assert "DEFINES" in rel_types

    def test_python_creates_inherits_relationships(
        self, memgraph_ingestor: MemgraphIngestor, python_project: Path
    ) -> None:
        index_project(memgraph_ingestor, python_project)

        rel_types = get_relationship_types(memgraph_ingestor)
        assert "INHERITS" in rel_types


class TestTypeScriptNodeLabels:
    def test_typescript_creates_interface_nodes(
        self, memgraph_ingestor: MemgraphIngestor, typescript_project: Path
    ) -> None:
        index_project(memgraph_ingestor, typescript_project)

        labels = get_node_labels(memgraph_ingestor)
        assert NodeLabel.INTERFACE.value in labels, (
            f"Interface label missing. Got labels: {labels}"
        )

        interfaces = get_nodes_by_label(memgraph_ingestor, NodeLabel.INTERFACE.value)
        interface_names = {n["name"] for n in interfaces}
        assert "MyInterface" in interface_names

    def test_typescript_creates_enum_nodes(
        self, memgraph_ingestor: MemgraphIngestor, typescript_project: Path
    ) -> None:
        index_project(memgraph_ingestor, typescript_project)

        labels = get_node_labels(memgraph_ingestor)
        assert NodeLabel.ENUM.value in labels, (
            f"Enum label missing. Got labels: {labels}"
        )

        enums = get_nodes_by_label(memgraph_ingestor, NodeLabel.ENUM.value)
        enum_names = {n["name"] for n in enums}
        assert "Status" in enum_names

    def test_typescript_creates_type_nodes(
        self, memgraph_ingestor: MemgraphIngestor, typescript_project: Path
    ) -> None:
        index_project(memgraph_ingestor, typescript_project)

        labels = get_node_labels(memgraph_ingestor)
        assert NodeLabel.TYPE.value in labels, (
            f"Type label missing. Got labels: {labels}"
        )

        types = get_nodes_by_label(memgraph_ingestor, NodeLabel.TYPE.value)
        type_names = {n["name"] for n in types}
        assert "MyType" in type_names

    def test_typescript_creates_class_nodes(
        self, memgraph_ingestor: MemgraphIngestor, typescript_project: Path
    ) -> None:
        index_project(memgraph_ingestor, typescript_project)

        labels = get_node_labels(memgraph_ingestor)
        assert NodeLabel.CLASS.value in labels

        classes = get_nodes_by_label(memgraph_ingestor, NodeLabel.CLASS.value)
        class_names = {n["name"] for n in classes}
        assert "MyTsClass" in class_names

    def test_typescript_creates_function_nodes(
        self, memgraph_ingestor: MemgraphIngestor, typescript_project: Path
    ) -> None:
        index_project(memgraph_ingestor, typescript_project)

        labels = get_node_labels(memgraph_ingestor)
        assert NodeLabel.FUNCTION.value in labels

        functions = get_nodes_by_label(memgraph_ingestor, NodeLabel.FUNCTION.value)
        func_names = {n["name"] for n in functions}
        assert "createInstance" in func_names


class TestJavaScriptNodeLabels:
    def test_javascript_creates_class_nodes(
        self, memgraph_ingestor: MemgraphIngestor, javascript_project: Path
    ) -> None:
        index_project(memgraph_ingestor, javascript_project)

        labels = get_node_labels(memgraph_ingestor)
        assert NodeLabel.CLASS.value in labels

        classes = get_nodes_by_label(memgraph_ingestor, NodeLabel.CLASS.value)
        class_names = {n["name"] for n in classes}
        assert "MyJsClass" in class_names

    def test_javascript_creates_function_nodes(
        self, memgraph_ingestor: MemgraphIngestor, javascript_project: Path
    ) -> None:
        index_project(memgraph_ingestor, javascript_project)

        labels = get_node_labels(memgraph_ingestor)
        assert NodeLabel.FUNCTION.value in labels

        functions = get_nodes_by_label(memgraph_ingestor, NodeLabel.FUNCTION.value)
        func_names = {n["name"] for n in functions}
        assert "createInstance" in func_names


class TestRustNodeLabels:
    def test_rust_creates_class_nodes_for_structs(
        self, memgraph_ingestor: MemgraphIngestor, rust_project: Path
    ) -> None:
        index_project(memgraph_ingestor, rust_project)

        labels = get_node_labels(memgraph_ingestor)
        assert NodeLabel.CLASS.value in labels

        classes = get_nodes_by_label(memgraph_ingestor, NodeLabel.CLASS.value)
        class_names = {n["name"] for n in classes}
        assert "MyStruct" in class_names

    def test_rust_creates_function_nodes(
        self, memgraph_ingestor: MemgraphIngestor, rust_project: Path
    ) -> None:
        index_project(memgraph_ingestor, rust_project)

        labels = get_node_labels(memgraph_ingestor)
        assert NodeLabel.FUNCTION.value in labels

        functions = get_nodes_by_label(memgraph_ingestor, NodeLabel.FUNCTION.value)
        func_names = {n["name"] for n in functions}
        assert "standalone_fn" in func_names


@pytest.mark.skip(reason="Go is in development status")
class TestGoNodeLabels:
    def test_go_creates_class_nodes_for_structs(
        self, memgraph_ingestor: MemgraphIngestor, go_project: Path
    ) -> None:
        index_project(memgraph_ingestor, go_project)

        labels = get_node_labels(memgraph_ingestor)
        assert NodeLabel.CLASS.value in labels

        classes = get_nodes_by_label(memgraph_ingestor, NodeLabel.CLASS.value)
        class_names = {n["name"] for n in classes}
        assert "MyStruct" in class_names

    def test_go_creates_interface_nodes(
        self, memgraph_ingestor: MemgraphIngestor, go_project: Path
    ) -> None:
        index_project(memgraph_ingestor, go_project)

        labels = get_node_labels(memgraph_ingestor)
        assert NodeLabel.INTERFACE.value in labels

        interfaces = get_nodes_by_label(memgraph_ingestor, NodeLabel.INTERFACE.value)
        interface_names = {n["name"] for n in interfaces}
        assert "MyInterface" in interface_names

    def test_go_creates_function_nodes(
        self, memgraph_ingestor: MemgraphIngestor, go_project: Path
    ) -> None:
        index_project(memgraph_ingestor, go_project)

        labels = get_node_labels(memgraph_ingestor)
        assert NodeLabel.FUNCTION.value in labels

        functions = get_nodes_by_label(memgraph_ingestor, NodeLabel.FUNCTION.value)
        func_names = {n["name"] for n in functions}
        assert "NewMyStruct" in func_names
        assert "main" in func_names


@pytest.mark.skip(reason="Scala is in development status")
class TestScalaNodeLabels:
    def test_scala_creates_class_nodes(
        self, memgraph_ingestor: MemgraphIngestor, scala_project: Path
    ) -> None:
        index_project(memgraph_ingestor, scala_project)

        labels = get_node_labels(memgraph_ingestor)
        assert NodeLabel.CLASS.value in labels

        classes = get_nodes_by_label(memgraph_ingestor, NodeLabel.CLASS.value)
        class_names = {n["name"] for n in classes}
        assert "MyScalaClass" in class_names

    def test_scala_creates_interface_nodes_for_traits(
        self, memgraph_ingestor: MemgraphIngestor, scala_project: Path
    ) -> None:
        index_project(memgraph_ingestor, scala_project)

        labels = get_node_labels(memgraph_ingestor)
        assert NodeLabel.INTERFACE.value in labels

        interfaces = get_nodes_by_label(memgraph_ingestor, NodeLabel.INTERFACE.value)
        interface_names = {n["name"] for n in interfaces}
        assert {"MyTrait", "Status"}.issubset(interface_names)


class TestJavaNodeLabels:
    def test_java_creates_class_nodes(
        self, memgraph_ingestor: MemgraphIngestor, java_project: Path
    ) -> None:
        index_project(memgraph_ingestor, java_project)

        labels = get_node_labels(memgraph_ingestor)
        assert NodeLabel.CLASS.value in labels

        classes = get_nodes_by_label(memgraph_ingestor, NodeLabel.CLASS.value)
        class_names = {n["name"] for n in classes}
        assert "Example" in class_names

    def test_java_creates_interface_nodes(
        self, memgraph_ingestor: MemgraphIngestor, java_project: Path
    ) -> None:
        index_project(memgraph_ingestor, java_project)

        labels = get_node_labels(memgraph_ingestor)
        assert NodeLabel.INTERFACE.value in labels

        interfaces = get_nodes_by_label(memgraph_ingestor, NodeLabel.INTERFACE.value)
        interface_names = {n["name"] for n in interfaces}
        assert "MyInterface" in interface_names

    def test_java_creates_enum_nodes(
        self, memgraph_ingestor: MemgraphIngestor, java_project: Path
    ) -> None:
        index_project(memgraph_ingestor, java_project)

        labels = get_node_labels(memgraph_ingestor)
        assert NodeLabel.ENUM.value in labels

        enums = get_nodes_by_label(memgraph_ingestor, NodeLabel.ENUM.value)
        enum_names = {n["name"] for n in enums}
        assert "Status" in enum_names


class TestCppNodeLabels:
    def test_cpp_creates_class_nodes(
        self, memgraph_ingestor: MemgraphIngestor, cpp_project: Path
    ) -> None:
        index_project(memgraph_ingestor, cpp_project)

        labels = get_node_labels(memgraph_ingestor)
        assert NodeLabel.CLASS.value in labels

        classes = get_nodes_by_label(memgraph_ingestor, NodeLabel.CLASS.value)
        class_names = {n["name"] for n in classes}
        assert "MyCppClass" in class_names

    def test_cpp_creates_enum_nodes(
        self, memgraph_ingestor: MemgraphIngestor, cpp_project: Path
    ) -> None:
        index_project(memgraph_ingestor, cpp_project)

        labels = get_node_labels(memgraph_ingestor)
        assert NodeLabel.ENUM.value in labels

        enums = get_nodes_by_label(memgraph_ingestor, NodeLabel.ENUM.value)
        enum_names = {n["name"] for n in enums}
        assert "Status" in enum_names

    def test_cpp_creates_function_nodes(
        self, memgraph_ingestor: MemgraphIngestor, cpp_project: Path
    ) -> None:
        index_project(memgraph_ingestor, cpp_project)

        labels = get_node_labels(memgraph_ingestor)
        assert NodeLabel.FUNCTION.value in labels

        functions = get_nodes_by_label(memgraph_ingestor, NodeLabel.FUNCTION.value)
        func_names = {n["name"] for n in functions}
        assert "standaloneFunction" in func_names


@pytest.mark.skip(reason="C# is in development status and parser not available")
class TestCSharpNodeLabels:
    def test_csharp_creates_class_nodes(
        self, memgraph_ingestor: MemgraphIngestor, csharp_project: Path
    ) -> None:
        index_project(memgraph_ingestor, csharp_project)

        labels = get_node_labels(memgraph_ingestor)
        assert NodeLabel.CLASS.value in labels

        classes = get_nodes_by_label(memgraph_ingestor, NodeLabel.CLASS.value)
        class_names = {n["name"] for n in classes}
        assert "MyCSharpClass" in class_names

    def test_csharp_creates_interface_nodes(
        self, memgraph_ingestor: MemgraphIngestor, csharp_project: Path
    ) -> None:
        index_project(memgraph_ingestor, csharp_project)

        labels = get_node_labels(memgraph_ingestor)
        assert NodeLabel.INTERFACE.value in labels

        interfaces = get_nodes_by_label(memgraph_ingestor, NodeLabel.INTERFACE.value)
        interface_names = {n["name"] for n in interfaces}
        assert "IMyInterface" in interface_names

    def test_csharp_creates_enum_nodes(
        self, memgraph_ingestor: MemgraphIngestor, csharp_project: Path
    ) -> None:
        index_project(memgraph_ingestor, csharp_project)

        labels = get_node_labels(memgraph_ingestor)
        assert NodeLabel.ENUM.value in labels

        enums = get_nodes_by_label(memgraph_ingestor, NodeLabel.ENUM.value)
        enum_names = {n["name"] for n in enums}
        assert "Status" in enum_names


@pytest.mark.skip(reason="PHP is in development status and parser not available")
class TestPhpNodeLabels:
    def test_php_creates_class_nodes(
        self, memgraph_ingestor: MemgraphIngestor, php_project: Path
    ) -> None:
        index_project(memgraph_ingestor, php_project)

        labels = get_node_labels(memgraph_ingestor)
        assert NodeLabel.CLASS.value in labels

        classes = get_nodes_by_label(memgraph_ingestor, NodeLabel.CLASS.value)
        class_names = {n["name"] for n in classes}
        assert "MyPhpClass" in class_names

    def test_php_creates_interface_nodes(
        self, memgraph_ingestor: MemgraphIngestor, php_project: Path
    ) -> None:
        index_project(memgraph_ingestor, php_project)

        labels = get_node_labels(memgraph_ingestor)
        assert NodeLabel.INTERFACE.value in labels

        interfaces = get_nodes_by_label(memgraph_ingestor, NodeLabel.INTERFACE.value)
        interface_names = {n["name"] for n in interfaces}
        assert "MyInterface" in interface_names

    def test_php_creates_function_nodes(
        self, memgraph_ingestor: MemgraphIngestor, php_project: Path
    ) -> None:
        index_project(memgraph_ingestor, php_project)

        labels = get_node_labels(memgraph_ingestor)
        assert NodeLabel.FUNCTION.value in labels

        functions = get_nodes_by_label(memgraph_ingestor, NodeLabel.FUNCTION.value)
        func_names = {n["name"] for n in functions}
        assert "standaloneFunction" in func_names


class TestLuaNodeLabels:
    def test_lua_creates_function_nodes(
        self, memgraph_ingestor: MemgraphIngestor, lua_project: Path
    ) -> None:
        index_project(memgraph_ingestor, lua_project)

        labels = get_node_labels(memgraph_ingestor)
        assert NodeLabel.FUNCTION.value in labels

        functions = get_nodes_by_label(memgraph_ingestor, NodeLabel.FUNCTION.value)
        func_names = {n["name"] for n in functions}
        assert {"new", "MyClass:getValue"}.issubset(func_names)


DEFINES_TEST_PARAMS = [
    ("python_project", None),
    ("typescript_project", None),
    ("javascript_project", None),
    ("rust_project", None),
    ("go_project", "Go is in development status"),
    ("scala_project", "Scala is in development status"),
    ("java_project", None),
    ("cpp_project", None),
    ("csharp_project", "C# is in development status"),
    ("php_project", "PHP is in development status"),
    ("lua_project", None),
]


@pytest.mark.parametrize("project_fixture,skip_reason", DEFINES_TEST_PARAMS)
def test_language_has_defines(
    project_fixture: str,
    skip_reason: str | None,
    request: pytest.FixtureRequest,
    memgraph_ingestor: MemgraphIngestor,
) -> None:
    if skip_reason:
        pytest.skip(skip_reason)

    project_path = request.getfixturevalue(project_fixture)
    index_project(memgraph_ingestor, project_path)
    rel_types = get_relationship_types(memgraph_ingestor)
    assert "DEFINES" in rel_types
