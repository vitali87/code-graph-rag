from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag.parser_loader import load_parsers
from codebase_rag.parsers.import_processor import ImportProcessor
from codebase_rag.parsers.js_ts.type_inference import JsTypeInferenceEngine
from codebase_rag.types_defs import NodeType


@pytest.fixture(scope="module")
def js_parser():
    parsers, _ = load_parsers()
    if cs.SupportedLanguage.JS not in parsers:
        pytest.skip("JavaScript parser not available")
    return parsers[cs.SupportedLanguage.JS]


@pytest.fixture(scope="module")
def ts_parser():
    parsers, _ = load_parsers()
    if cs.SupportedLanguage.TS not in parsers:
        pytest.skip("TypeScript parser not available")
    return parsers[cs.SupportedLanguage.TS]


@pytest.fixture
def mock_import_processor() -> MagicMock:
    processor = MagicMock(spec=ImportProcessor)
    processor.import_mapping = {}
    return processor


@pytest.fixture
def mock_function_registry() -> MagicMock:
    registry = MagicMock()
    registry.__contains__ = MagicMock(return_value=False)
    registry.__getitem__ = MagicMock(return_value=None)
    return registry


@pytest.fixture
def mock_find_method_ast_node() -> MagicMock:
    return MagicMock(return_value=None)


@pytest.fixture
def js_type_engine(
    mock_import_processor: MagicMock,
    mock_function_registry: MagicMock,
    mock_find_method_ast_node: MagicMock,
) -> JsTypeInferenceEngine:
    return JsTypeInferenceEngine(
        import_processor=mock_import_processor,
        function_registry=mock_function_registry,
        project_name="test_project",
        find_method_ast_node_func=mock_find_method_ast_node,
    )


class TestJsTypeInferenceWithRealParsing:
    def test_simple_new_expression(
        self,
        js_parser,
        js_type_engine: JsTypeInferenceEngine,
    ) -> None:
        code = b"const person = new Person();"
        tree = js_parser.parse(code)

        result = js_type_engine.build_local_variable_type_map(
            tree.root_node, "myapp.main"
        )

        assert "person" in result
        assert result["person"] == "Person"

    def test_new_expression_with_arguments(
        self,
        js_parser,
        js_type_engine: JsTypeInferenceEngine,
    ) -> None:
        code = b'const user = new User("John", 30);'
        tree = js_parser.parse(code)

        result = js_type_engine.build_local_variable_type_map(
            tree.root_node, "myapp.main"
        )

        assert "user" in result
        assert result["user"] == "User"

    def test_multiple_variable_declarations(
        self,
        js_parser,
        js_type_engine: JsTypeInferenceEngine,
    ) -> None:
        code = b"""
const person = new Person();
const logger = new Logger();
const config = new Config();
"""
        tree = js_parser.parse(code)

        result = js_type_engine.build_local_variable_type_map(
            tree.root_node, "myapp.main"
        )

        assert "person" in result
        assert result["person"] == "Person"
        assert "logger" in result
        assert result["logger"] == "Logger"
        assert "config" in result
        assert result["config"] == "Config"

    def test_let_declaration_with_new_expression(
        self,
        js_parser,
        js_type_engine: JsTypeInferenceEngine,
    ) -> None:
        code = b"let instance = new MyClass();"
        tree = js_parser.parse(code)

        result = js_type_engine.build_local_variable_type_map(
            tree.root_node, "myapp.main"
        )

        assert "instance" in result
        assert result["instance"] == "MyClass"

    def test_var_declaration_with_new_expression(
        self,
        js_parser,
        js_type_engine: JsTypeInferenceEngine,
    ) -> None:
        code = b"var obj = new SomeObject();"
        tree = js_parser.parse(code)

        result = js_type_engine.build_local_variable_type_map(
            tree.root_node, "myapp.main"
        )

        assert "obj" in result
        assert result["obj"] == "SomeObject"

    def test_function_call_assignment(
        self,
        js_parser,
        js_type_engine: JsTypeInferenceEngine,
    ) -> None:
        code = b"const result = createInstance();"
        tree = js_parser.parse(code)

        result = js_type_engine.build_local_variable_type_map(
            tree.root_node, "myapp.main"
        )

        assert "result" in result
        assert result["result"] == "createInstance"

    def test_nested_in_function(
        self,
        js_parser,
        js_type_engine: JsTypeInferenceEngine,
    ) -> None:
        code = b"""
function process() {
    const handler = new EventHandler();
    return handler;
}
"""
        tree = js_parser.parse(code)

        result = js_type_engine.build_local_variable_type_map(
            tree.root_node, "myapp.main"
        )

        assert "handler" in result
        assert result["handler"] == "EventHandler"

    def test_nested_in_class_method(
        self,
        js_parser,
        js_type_engine: JsTypeInferenceEngine,
    ) -> None:
        code = b"""
class Service {
    process() {
        const helper = new Helper();
        return helper.run();
    }
}
"""
        tree = js_parser.parse(code)

        result = js_type_engine.build_local_variable_type_map(
            tree.root_node, "myapp.main"
        )

        assert "helper" in result
        assert result["helper"] == "Helper"

    def test_arrow_function_body(
        self,
        js_parser,
        js_type_engine: JsTypeInferenceEngine,
    ) -> None:
        code = b"""
const handler = () => {
    const client = new HttpClient();
    return client.get();
};
"""
        tree = js_parser.parse(code)

        result = js_type_engine.build_local_variable_type_map(
            tree.root_node, "myapp.main"
        )

        assert "client" in result
        assert result["client"] == "HttpClient"

    def test_resolves_imported_class(
        self,
        js_parser,
        js_type_engine: JsTypeInferenceEngine,
        mock_import_processor: MagicMock,
        mock_function_registry: MagicMock,
    ) -> None:
        mock_import_processor.import_mapping = {
            "myapp.main": {"Person": "myapp.models"}
        }
        mock_function_registry.__contains__ = MagicMock(
            side_effect=lambda x: x == "myapp.models.Person"
        )
        mock_function_registry.__getitem__ = MagicMock(return_value=NodeType.CLASS)

        code = b"const person = new Person();"
        tree = js_parser.parse(code)

        result = js_type_engine.build_local_variable_type_map(
            tree.root_node, "myapp.main"
        )

        assert "person" in result
        assert result["person"] == "myapp.models.Person"

    def test_resolves_local_class(
        self,
        js_parser,
        js_type_engine: JsTypeInferenceEngine,
        mock_function_registry: MagicMock,
    ) -> None:
        mock_function_registry.__contains__ = MagicMock(
            side_effect=lambda x: x == "myapp.main.LocalClass"
        )
        mock_function_registry.__getitem__ = MagicMock(return_value=NodeType.CLASS)

        code = b"const local = new LocalClass();"
        tree = js_parser.parse(code)

        result = js_type_engine.build_local_variable_type_map(
            tree.root_node, "myapp.main"
        )

        assert "local" in result
        assert result["local"] == "myapp.main.LocalClass"

    def test_string_literal_not_inferred(
        self,
        js_parser,
        js_type_engine: JsTypeInferenceEngine,
    ) -> None:
        code = b'const name = "John";'
        tree = js_parser.parse(code)

        result = js_type_engine.build_local_variable_type_map(
            tree.root_node, "myapp.main"
        )

        assert "name" not in result

    def test_number_literal_not_inferred(
        self,
        js_parser,
        js_type_engine: JsTypeInferenceEngine,
    ) -> None:
        code = b"const count = 42;"
        tree = js_parser.parse(code)

        result = js_type_engine.build_local_variable_type_map(
            tree.root_node, "myapp.main"
        )

        assert "count" not in result

    def test_object_literal_not_inferred(
        self,
        js_parser,
        js_type_engine: JsTypeInferenceEngine,
    ) -> None:
        code = b"const obj = { name: 'test' };"
        tree = js_parser.parse(code)

        result = js_type_engine.build_local_variable_type_map(
            tree.root_node, "myapp.main"
        )

        assert "obj" not in result

    def test_array_literal_not_inferred(
        self,
        js_parser,
        js_type_engine: JsTypeInferenceEngine,
    ) -> None:
        code = b"const items = [1, 2, 3];"
        tree = js_parser.parse(code)

        result = js_type_engine.build_local_variable_type_map(
            tree.root_node, "myapp.main"
        )

        assert "items" not in result


class TestTsTypeInferenceWithRealParsing:
    def test_typescript_new_expression(
        self,
        ts_parser,
        js_type_engine: JsTypeInferenceEngine,
    ) -> None:
        code = b"const person: Person = new Person();"
        tree = ts_parser.parse(code)

        result = js_type_engine.build_local_variable_type_map(
            tree.root_node, "myapp.main"
        )

        assert "person" in result
        assert result["person"] == "Person"

    def test_typescript_generic_new_expression(
        self,
        ts_parser,
        js_type_engine: JsTypeInferenceEngine,
    ) -> None:
        code = b"const list = new ArrayList<string>();"
        tree = ts_parser.parse(code)

        result = js_type_engine.build_local_variable_type_map(
            tree.root_node, "myapp.main"
        )

        assert "list" in result
        assert result["list"] == "ArrayList"

    def test_typescript_interface_implementation(
        self,
        ts_parser,
        js_type_engine: JsTypeInferenceEngine,
        mock_function_registry: MagicMock,
    ) -> None:
        mock_function_registry.__contains__ = MagicMock(
            side_effect=lambda x: x == "myapp.main.UserService"
        )
        mock_function_registry.__getitem__ = MagicMock(return_value=NodeType.CLASS)

        code = b"""
interface IService {
    process(): void;
}

class UserService implements IService {
    process(): void {}
}

const service: IService = new UserService();
"""
        tree = ts_parser.parse(code)

        result = js_type_engine.build_local_variable_type_map(
            tree.root_node, "myapp.main"
        )

        assert "service" in result
        assert result["service"] == "myapp.main.UserService"

    def test_typescript_multiple_declarations_in_class(
        self,
        ts_parser,
        js_type_engine: JsTypeInferenceEngine,
    ) -> None:
        code = b"""
class Controller {
    private logger: Logger;

    constructor() {
        const config = new Config();
        this.logger = new Logger();
    }

    process(): void {
        const handler = new Handler();
        handler.run();
    }
}
"""
        tree = ts_parser.parse(code)

        result = js_type_engine.build_local_variable_type_map(
            tree.root_node, "myapp.main"
        )

        assert "config" in result
        assert result["config"] == "Config"
        assert "handler" in result
        assert result["handler"] == "Handler"


class TestJsTypeInferenceEdgeCases:
    def test_destructuring_not_inferred(
        self,
        js_parser,
        js_type_engine: JsTypeInferenceEngine,
    ) -> None:
        code = b"const { name, age } = person;"
        tree = js_parser.parse(code)

        result = js_type_engine.build_local_variable_type_map(
            tree.root_node, "myapp.main"
        )

        assert "name" not in result
        assert "age" not in result

    def test_async_function_with_new_expression(
        self,
        js_parser,
        js_type_engine: JsTypeInferenceEngine,
    ) -> None:
        code = b"""
async function fetchData() {
    const client = new ApiClient();
    return await client.fetch();
}
"""
        tree = js_parser.parse(code)

        result = js_type_engine.build_local_variable_type_map(
            tree.root_node, "myapp.main"
        )

        assert "client" in result
        assert result["client"] == "ApiClient"

    def test_deeply_nested_new_expression(
        self,
        js_parser,
        js_type_engine: JsTypeInferenceEngine,
    ) -> None:
        code = b"""
function outer() {
    function middle() {
        function inner() {
            const deep = new DeepClass();
            return deep;
        }
        return inner();
    }
    return middle();
}
"""
        tree = js_parser.parse(code)

        result = js_type_engine.build_local_variable_type_map(
            tree.root_node, "myapp.main"
        )

        assert "deep" in result
        assert result["deep"] == "DeepClass"

    def test_conditional_new_expression(
        self,
        js_parser,
        js_type_engine: JsTypeInferenceEngine,
    ) -> None:
        code = b"""
function create(type) {
    if (type === 'a') {
        const objA = new TypeA();
        return objA;
    } else {
        const objB = new TypeB();
        return objB;
    }
}
"""
        tree = js_parser.parse(code)

        result = js_type_engine.build_local_variable_type_map(
            tree.root_node, "myapp.main"
        )

        assert "objA" in result
        assert result["objA"] == "TypeA"
        assert "objB" in result
        assert result["objB"] == "TypeB"

    def test_loop_with_new_expression(
        self,
        js_parser,
        js_type_engine: JsTypeInferenceEngine,
    ) -> None:
        code = b"""
function processItems(items) {
    for (let i = 0; i < items.length; i++) {
        const processor = new ItemProcessor();
        processor.process(items[i]);
    }
}
"""
        tree = js_parser.parse(code)

        result = js_type_engine.build_local_variable_type_map(
            tree.root_node, "myapp.main"
        )

        assert "processor" in result
        assert result["processor"] == "ItemProcessor"

    def test_try_catch_with_new_expression(
        self,
        js_parser,
        js_type_engine: JsTypeInferenceEngine,
    ) -> None:
        code = b"""
function safeCreate() {
    try {
        const risky = new RiskyOperation();
        return risky.execute();
    } catch (e) {
        const fallback = new FallbackHandler();
        return fallback.handle(e);
    }
}
"""
        tree = js_parser.parse(code)

        result = js_type_engine.build_local_variable_type_map(
            tree.root_node, "myapp.main"
        )

        assert "risky" in result
        assert result["risky"] == "RiskyOperation"
        assert "fallback" in result
        assert result["fallback"] == "FallbackHandler"


class TestTsAnnotationAndForOfTypes:
    # TS type annotations and for-of loop variables (issue #1303): the
    # annotation is declared truth and wins over value inference; arrays and
    # array-shaped generics carry their element in the canonical
    # `list[<element>]` marker, which only for-of unwraps.

    def test_scalar_annotation_types_the_variable(
        self, ts_parser, js_type_engine: JsTypeInferenceEngine
    ) -> None:
        code = b"const u: User = fetchOne();"
        tree = ts_parser.parse(code)
        result = js_type_engine.build_local_variable_type_map(
            tree.root_node, "myapp.main", cs.SupportedLanguage.TS
        )
        assert result["u"] == "User"

    def test_array_annotation_carries_the_element_marker(
        self, ts_parser, js_type_engine: JsTypeInferenceEngine
    ) -> None:
        code = b"const users: User[] = fetchAll();"
        tree = ts_parser.parse(code)
        result = js_type_engine.build_local_variable_type_map(
            tree.root_node, "myapp.main", cs.SupportedLanguage.TS
        )
        assert result["users"] == "list[User]"

    def test_generic_array_annotation_carries_the_element_marker(
        self, ts_parser, js_type_engine: JsTypeInferenceEngine
    ) -> None:
        code = b"const users: Array<User> = fetchAll();"
        tree = ts_parser.parse(code)
        result = js_type_engine.build_local_variable_type_map(
            tree.root_node, "myapp.main", cs.SupportedLanguage.TS
        )
        assert result["users"] == "list[User]"

    def test_nullable_union_annotation_takes_the_concrete_member(
        self, ts_parser, js_type_engine: JsTypeInferenceEngine
    ) -> None:
        code = b"const u: User | null = fetchOne();"
        tree = ts_parser.parse(code)
        result = js_type_engine.build_local_variable_type_map(
            tree.root_node, "myapp.main", cs.SupportedLanguage.TS
        )
        assert result["u"] == "User"

    def test_annotation_without_initializer_still_types(
        self, ts_parser, js_type_engine: JsTypeInferenceEngine
    ) -> None:
        code = b"let u: User;"
        tree = ts_parser.parse(code)
        result = js_type_engine.build_local_variable_type_map(
            tree.root_node, "myapp.main", cs.SupportedLanguage.TS
        )
        assert result["u"] == "User"

    def test_for_of_over_annotated_array_types_the_loop_variable(
        self, ts_parser, js_type_engine: JsTypeInferenceEngine
    ) -> None:
        code = b"""
const users: User[] = fetchAll();
for (const u of users) { u.save(); }
"""
        tree = ts_parser.parse(code)
        result = js_type_engine.build_local_variable_type_map(
            tree.root_node, "myapp.main", cs.SupportedLanguage.TS
        )
        assert result["u"] == "User"

    def test_for_in_is_not_for_of(
        self, ts_parser, js_type_engine: JsTypeInferenceEngine
    ) -> None:
        # `for (const k in users)` iterates keys, never elements.
        code = b"""
const users: User[] = fetchAll();
for (const k in users) { log(k); }
"""
        tree = ts_parser.parse(code)
        result = js_type_engine.build_local_variable_type_map(
            tree.root_node, "myapp.main", cs.SupportedLanguage.TS
        )
        assert "k" not in result

    def test_for_of_over_array_literal_of_constructions(
        self, js_parser, js_type_engine: JsTypeInferenceEngine
    ) -> None:
        # Plain JS has no annotations; a literal of constructions still names
        # its element.
        code = b"""
for (const w of [new Widget(), new Widget()]) { w.render(); }
"""
        tree = js_parser.parse(code)
        result = js_type_engine.build_local_variable_type_map(
            tree.root_node, "myapp.main"
        )
        assert result["w"] == "Widget"


def test_ts_annotations_bind_loop_method_calls_end_to_end(tmp_path, mock_ingestor):
    # Full-pipeline proof for issue #1303: the bodies return through
    # JSON.parse, so the annotations are the only typing evidence, and the
    # decoy class shares the method name so only a typed receiver can bind.
    from pathlib import Path

    from codebase_rag.graph_updater import GraphUpdater
    from codebase_rag.parser_loader import load_parsers

    project_path = Path(tmp_path) / "ts_annotation_test"
    project_path.mkdir()
    (project_path / "app.ts").write_text(
        encoding="utf-8",
        data="""
export class Widget {
    render(): string { return "w"; }
}

export class Banner {
    render(): string { return "b"; }
}

export function loadWidgets(): Widget[] {
    return JSON.parse("[]");
}

export class Screen {
    drawDirect(): void {
        for (const w of loadWidgets()) {
            w.render();
        }
    }

    drawStored(): void {
        const widgets: Widget[] = loadWidgets();
        for (const w of widgets) {
            w.render();
        }
    }

    drawOne(): void {
        const single: Widget = JSON.parse("{}");
        single.render();
    }
}
""",
    )

    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=project_path,
        parsers=parsers,
        queries=queries,
    )
    updater.run()

    project_name = project_path.name
    found = {
        (c[0][0][2], c[0][2][2])
        for c in mock_ingestor.ensure_relationship_batch.call_args_list
        if len(c[0]) >= 3 and c[0][1] == "CALLS" and c[0][2][0] == NodeType.METHOD
    }
    render = f"{project_name}.app.Widget.render"
    decoy = f"{project_name}.app.Banner.render"
    missing = [
        (caller, render)
        for method in ("drawDirect", "drawStored", "drawOne")
        if ((caller := f"{project_name}.app.Screen.{method}"), render) not in found
    ]
    if missing:
        pytest.fail(f"Missing annotation-typed calls: {missing}")
    assert not any(callee == decoy for _caller, callee in found)


class TestForOfConservativeCeilings:
    # Review round on #1306: without lexical scoping the engine must prefer
    # no binding over a possibly wrong one.

    def test_shadowed_loop_variable_with_conflicting_type_drops_the_binding(
        self, ts_parser, js_type_engine: JsTypeInferenceEngine
    ) -> None:
        # Both bindings are typed: `item` is Widget outside the loop and
        # Banner inside it; either single entry would emit wrong edges on one
        # side of the loop, so neither survives.
        code = b"""
const banners: Banner[] = fetchBanners();
const item: Widget = fetchOne();
for (const item of banners) { item.show(); }
"""
        tree = ts_parser.parse(code)
        result = js_type_engine.build_local_variable_type_map(
            tree.root_node, "myapp.main", cs.SupportedLanguage.TS
        )
        assert "item" not in result

    def test_shadowed_loop_variable_with_untypable_element_drops_the_binding(
        self, ts_parser, js_type_engine: JsTypeInferenceEngine
    ) -> None:
        # The header rebinds the name even when the element cannot be typed:
        # keeping the outer Widget would type the loop body wrongly.
        code = b"""
const item: Widget = fetchOne();
for (const item of banners) { item.show(); }
"""
        tree = ts_parser.parse(code)
        result = js_type_engine.build_local_variable_type_map(
            tree.root_node, "myapp.main", cs.SupportedLanguage.TS
        )
        assert "item" not in result

    def test_for_of_rebinding_same_type_is_kept(
        self, ts_parser, js_type_engine: JsTypeInferenceEngine
    ) -> None:
        # A pre-existing binding of the SAME type agrees with the loop
        # element, so the entry survives.
        code = b"""
const w: Widget = fetchOne();
const widgets: Widget[] = fetchAll();
for (const w of widgets) { w.render(); }
"""
        tree = ts_parser.parse(code)
        result = js_type_engine.build_local_variable_type_map(
            tree.root_node, "myapp.main", cs.SupportedLanguage.TS
        )
        assert result["w"] == "Widget"

    def test_heterogeneous_array_literal_yields_nothing(
        self, js_parser, js_type_engine: JsTypeInferenceEngine
    ) -> None:
        code = b"""
for (const x of [new Widget(), new Banner()]) { x.render(); }
"""
        tree = js_parser.parse(code)
        result = js_type_engine.build_local_variable_type_map(
            tree.root_node, "myapp.main"
        )
        assert "x" not in result

    def test_array_literal_mixing_constructions_and_names_yields_nothing(
        self, js_parser, js_type_engine: JsTypeInferenceEngine
    ) -> None:
        code = b"""
for (const x of [new Widget(), fallback]) { x.render(); }
"""
        tree = js_parser.parse(code)
        result = js_type_engine.build_local_variable_type_map(
            tree.root_node, "myapp.main"
        )
        assert "x" not in result

    def test_duplicate_function_names_are_ambiguous(
        self, ts_parser, js_type_engine: JsTypeInferenceEngine
    ) -> None:
        # A local `load` shadowing a top-level one cannot be attributed to
        # the call site without lexical scoping: no binding, never a guess.
        code = b"""
function load(): Banner[] { return JSON.parse("[]"); }

function screen(): void {
    const load = (): Widget[] => JSON.parse("[]");
    for (const x of load()) { x.render(); }
}
"""
        tree = ts_parser.parse(code)
        result = js_type_engine.build_local_variable_type_map(
            tree.root_node, "myapp.main", cs.SupportedLanguage.TS
        )
        assert "x" not in result
