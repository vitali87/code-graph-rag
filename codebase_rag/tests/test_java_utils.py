from dataclasses import dataclass, field

from codebase_rag.parsers.java_utils import (
    JavaAnnotationInfo,
    JavaClassInfo,
    JavaFieldInfo,
    JavaMethodInfo,
    build_java_qualified_name,
    extract_java_annotation_info,
    extract_java_class_info,
    extract_java_field_info,
    extract_java_import_path,
    extract_java_method_call_info,
    extract_java_method_info,
    extract_java_package_name,
    find_java_package_start_index,
    get_java_visibility,
    is_java_main_method,
)


@dataclass
class MockNode:
    type: str
    children: list["MockNode"] = field(default_factory=list)
    parent: "MockNode | None" = None
    node_fields: dict[str, "MockNode | None"] = field(default_factory=dict)
    text: bytes = b""

    def child_by_field_name(self, name: str) -> "MockNode | None":
        return self.node_fields.get(name)


def create_mock_node(
    node_type: str,
    text: str = "",
    fields: dict[str, "MockNode | None"] | None = None,
    children: list["MockNode"] | None = None,
    parent: "MockNode | None" = None,
) -> MockNode:
    node = MockNode(
        type=node_type,
        children=children or [],
        parent=parent,
        node_fields=fields or {},
        text=text.encode(),
    )
    for child in node.children:
        child.parent = node
    return node


class TestExtractJavaPackageName:
    def test_scoped_identifier_package(self) -> None:
        scoped_id = create_mock_node("scoped_identifier", "com.example.app")
        package_node = create_mock_node("package_declaration", children=[scoped_id])
        result = extract_java_package_name(package_node)
        assert result == "com.example.app"

    def test_simple_identifier_package(self) -> None:
        identifier = create_mock_node("identifier", "mypackage")
        package_node = create_mock_node("package_declaration", children=[identifier])
        result = extract_java_package_name(package_node)
        assert result == "mypackage"

    def test_invalid_node_type(self) -> None:
        node = create_mock_node("class_declaration")
        result = extract_java_package_name(node)
        assert result is None

    def test_empty_package_declaration(self) -> None:
        package_node = create_mock_node("package_declaration", children=[])
        result = extract_java_package_name(package_node)
        assert result is None


class TestExtractJavaImportPath:
    def test_regular_import(self) -> None:
        scoped_id = create_mock_node("scoped_identifier", "java.util.List")
        import_node = create_mock_node("import_declaration", children=[scoped_id])
        result = extract_java_import_path(import_node)
        assert result == {"List": "java.util.List"}

    def test_wildcard_import(self) -> None:
        scoped_id = create_mock_node("scoped_identifier", "java.util")
        asterisk = create_mock_node("asterisk", "*")
        import_node = create_mock_node(
            "import_declaration", children=[scoped_id, asterisk]
        )
        result = extract_java_import_path(import_node)
        assert result == {"*java.util": "java.util"}

    def test_static_import(self) -> None:
        static_kw = create_mock_node("static", "static")
        scoped_id = create_mock_node("scoped_identifier", "java.lang.Math.PI")
        import_node = create_mock_node(
            "import_declaration", children=[static_kw, scoped_id]
        )
        result = extract_java_import_path(import_node)
        assert result == {"PI": "java.lang.Math.PI"}

    def test_simple_identifier_import(self) -> None:
        identifier = create_mock_node("identifier", "MyClass")
        import_node = create_mock_node("import_declaration", children=[identifier])
        result = extract_java_import_path(import_node)
        assert result == {"MyClass": "MyClass"}

    def test_invalid_node_type(self) -> None:
        node = create_mock_node("class_declaration")
        result = extract_java_import_path(node)
        assert result == {}

    def test_empty_import_declaration(self) -> None:
        import_node = create_mock_node("import_declaration", children=[])
        result = extract_java_import_path(import_node)
        assert result == {}


class TestExtractJavaClassInfo:
    def test_simple_class(self) -> None:
        name_node = create_mock_node("identifier", "MyClass")
        class_node = create_mock_node(
            "class_declaration",
            fields={"name": name_node},
        )
        result = extract_java_class_info(class_node)
        assert result["name"] == "MyClass"
        assert result["type"] == "class"
        assert result["superclass"] is None
        assert result["interfaces"] == []

    def test_class_with_superclass(self) -> None:
        name_node = create_mock_node("identifier", "Child")
        superclass_node = create_mock_node("type_identifier", "Parent")
        class_node = create_mock_node(
            "class_declaration",
            fields={"name": name_node, "superclass": superclass_node},
        )
        result = extract_java_class_info(class_node)
        assert result["name"] == "Child"
        assert result["superclass"] == "Parent"

    def test_class_with_generic_superclass(self) -> None:
        name_node = create_mock_node("identifier", "MyList")
        type_id = create_mock_node("type_identifier", "ArrayList")
        generic_type = create_mock_node("generic_type", children=[type_id])
        class_node = create_mock_node(
            "class_declaration",
            fields={"name": name_node, "superclass": generic_type},
        )
        result = extract_java_class_info(class_node)
        assert result["superclass"] == "ArrayList"

    def test_interface_declaration(self) -> None:
        name_node = create_mock_node("identifier", "MyInterface")
        interface_node = create_mock_node(
            "interface_declaration",
            fields={"name": name_node},
        )
        result = extract_java_class_info(interface_node)
        assert result["name"] == "MyInterface"
        assert result["type"] == "interface"

    def test_enum_declaration(self) -> None:
        name_node = create_mock_node("identifier", "Status")
        enum_node = create_mock_node(
            "enum_declaration",
            fields={"name": name_node},
        )
        result = extract_java_class_info(enum_node)
        assert result["name"] == "Status"
        assert result["type"] == "enum"

    def test_annotation_type_declaration(self) -> None:
        name_node = create_mock_node("identifier", "MyAnnotation")
        annotation_node = create_mock_node(
            "annotation_type_declaration",
            fields={"name": name_node},
        )
        result = extract_java_class_info(annotation_node)
        assert result["name"] == "MyAnnotation"
        assert result["type"] == "annotation_type"

    def test_record_declaration(self) -> None:
        name_node = create_mock_node("identifier", "Person")
        record_node = create_mock_node(
            "record_declaration",
            fields={"name": name_node},
        )
        result = extract_java_class_info(record_node)
        assert result["name"] == "Person"
        assert result["type"] == "record"

    def test_class_with_modifiers(self) -> None:
        name_node = create_mock_node("identifier", "MyClass")
        public_mod = create_mock_node("public", "public")
        abstract_mod = create_mock_node("abstract", "abstract")
        modifiers = create_mock_node("modifiers", children=[public_mod, abstract_mod])
        class_node = create_mock_node(
            "class_declaration",
            fields={"name": name_node},
            children=[modifiers],
        )
        result = extract_java_class_info(class_node)
        assert "public" in result["modifiers"]
        assert "abstract" in result["modifiers"]

    def test_class_with_type_parameters(self) -> None:
        name_node = create_mock_node("identifier", "Container")
        param_name = create_mock_node("identifier", "T")
        type_param = create_mock_node("type_parameter", fields={"name": param_name})
        type_params = create_mock_node("type_parameters", children=[type_param])
        class_node = create_mock_node(
            "class_declaration",
            fields={"name": name_node, "type_parameters": type_params},
        )
        result = extract_java_class_info(class_node)
        assert "T" in result["type_parameters"]

    def test_invalid_node_type(self) -> None:
        node = create_mock_node("method_declaration")
        result = extract_java_class_info(node)
        assert result == JavaClassInfo(
            name=None,
            type="",
            superclass=None,
            interfaces=[],
            modifiers=[],
            type_parameters=[],
        )


class TestExtractJavaMethodInfo:
    def test_simple_method(self) -> None:
        name_node = create_mock_node("identifier", "process")
        type_node = create_mock_node("type_identifier", "void")
        method_node = create_mock_node(
            "method_declaration",
            fields={"name": name_node, "type": type_node},
        )
        result = extract_java_method_info(method_node)
        assert result["name"] == "process"
        assert result["type"] == "method"
        assert result["return_type"] == "void"

    def test_constructor(self) -> None:
        name_node = create_mock_node("identifier", "MyClass")
        constructor_node = create_mock_node(
            "constructor_declaration",
            fields={"name": name_node},
        )
        result = extract_java_method_info(constructor_node)
        assert result["name"] == "MyClass"
        assert result["type"] == "constructor"
        assert result["return_type"] is None

    def test_method_with_parameters(self) -> None:
        name_node = create_mock_node("identifier", "setName")
        type_node = create_mock_node("void_type", "void")
        param_type = create_mock_node("type_identifier", "String")
        param = create_mock_node("formal_parameter", fields={"type": param_type})
        params = create_mock_node("formal_parameters", children=[param])
        method_node = create_mock_node(
            "method_declaration",
            fields={"name": name_node, "type": type_node, "parameters": params},
        )
        result = extract_java_method_info(method_node)
        assert "String" in result["parameters"]

    def test_method_with_varargs(self) -> None:
        name_node = create_mock_node("identifier", "format")
        type_node = create_mock_node("type_identifier", "String")
        param_type = create_mock_node("type_identifier", "Object")
        spread_param = create_mock_node("spread_parameter", children=[param_type])
        params = create_mock_node("formal_parameters", children=[spread_param])
        method_node = create_mock_node(
            "method_declaration",
            fields={"name": name_node, "type": type_node, "parameters": params},
        )
        result = extract_java_method_info(method_node)
        assert "Object..." in result["parameters"]

    def test_method_with_modifiers_and_annotations(self) -> None:
        name_node = create_mock_node("identifier", "run")
        type_node = create_mock_node("void_type", "void")
        public_mod = create_mock_node("public", "public")
        static_mod = create_mock_node("static", "static")
        annotation = create_mock_node("annotation", "@Override")
        modifiers = create_mock_node(
            "modifiers", children=[public_mod, static_mod, annotation]
        )
        method_node = create_mock_node(
            "method_declaration",
            fields={"name": name_node, "type": type_node},
            children=[modifiers],
        )
        result = extract_java_method_info(method_node)
        assert "public" in result["modifiers"]
        assert "static" in result["modifiers"]
        assert "@Override" in result["annotations"]

    def test_invalid_node_type(self) -> None:
        node = create_mock_node("class_declaration")
        result = extract_java_method_info(node)
        assert result == JavaMethodInfo(
            name=None,
            type="",
            return_type=None,
            parameters=[],
            modifiers=[],
            type_parameters=[],
            annotations=[],
        )


class TestExtractJavaFieldInfo:
    def test_simple_field(self) -> None:
        type_node = create_mock_node("type_identifier", "String")
        name_node = create_mock_node("identifier", "name")
        declarator = create_mock_node("variable_declarator", fields={"name": name_node})
        field_node = create_mock_node(
            "field_declaration",
            fields={"type": type_node, "declarator": declarator},
        )
        result = extract_java_field_info(field_node)
        assert result["name"] == "name"
        assert result["type"] == "String"

    def test_field_with_modifiers(self) -> None:
        type_node = create_mock_node("type_identifier", "int")
        name_node = create_mock_node("identifier", "count")
        declarator = create_mock_node("variable_declarator", fields={"name": name_node})
        private_mod = create_mock_node("private", "private")
        static_mod = create_mock_node("static", "static")
        final_mod = create_mock_node("final", "final")
        modifiers = create_mock_node(
            "modifiers", children=[private_mod, static_mod, final_mod]
        )
        field_node = create_mock_node(
            "field_declaration",
            fields={"type": type_node, "declarator": declarator},
            children=[modifiers],
        )
        result = extract_java_field_info(field_node)
        assert "private" in result["modifiers"]
        assert "static" in result["modifiers"]
        assert "final" in result["modifiers"]

    def test_field_with_annotation(self) -> None:
        type_node = create_mock_node("type_identifier", "String")
        name_node = create_mock_node("identifier", "id")
        declarator = create_mock_node("variable_declarator", fields={"name": name_node})
        annotation = create_mock_node("annotation", "@Id")
        modifiers = create_mock_node("modifiers", children=[annotation])
        field_node = create_mock_node(
            "field_declaration",
            fields={"type": type_node, "declarator": declarator},
            children=[modifiers],
        )
        result = extract_java_field_info(field_node)
        assert "@Id" in result["annotations"]

    def test_invalid_node_type(self) -> None:
        node = create_mock_node("method_declaration")
        result = extract_java_field_info(node)
        assert result == JavaFieldInfo(
            name=None,
            type=None,
            modifiers=[],
            annotations=[],
        )


class TestExtractJavaMethodCallInfo:
    def test_simple_method_call(self) -> None:
        name_node = create_mock_node("identifier", "process")
        args_node = create_mock_node("argument_list", children=[])
        call_node = create_mock_node(
            "method_invocation",
            fields={"name": name_node, "arguments": args_node},
        )
        result = extract_java_method_call_info(call_node)
        assert result["name"] == "process"
        assert result["object"] is None
        assert result["arguments"] == 0

    def test_method_call_on_object(self) -> None:
        name_node = create_mock_node("identifier", "getName")
        object_node = create_mock_node("identifier", "user")
        args_node = create_mock_node("argument_list", children=[])
        call_node = create_mock_node(
            "method_invocation",
            fields={"name": name_node, "object": object_node, "arguments": args_node},
        )
        result = extract_java_method_call_info(call_node)
        assert result["name"] == "getName"
        assert result["object"] == "user"

    def test_method_call_on_this(self) -> None:
        name_node = create_mock_node("identifier", "validate")
        this_node = create_mock_node("this", "this")
        args_node = create_mock_node("argument_list", children=[])
        call_node = create_mock_node(
            "method_invocation",
            fields={"name": name_node, "object": this_node, "arguments": args_node},
        )
        result = extract_java_method_call_info(call_node)
        assert result["object"] == "this"

    def test_method_call_on_super(self) -> None:
        name_node = create_mock_node("identifier", "init")
        super_node = create_mock_node("super", "super")
        args_node = create_mock_node("argument_list", children=[])
        call_node = create_mock_node(
            "method_invocation",
            fields={"name": name_node, "object": super_node, "arguments": args_node},
        )
        result = extract_java_method_call_info(call_node)
        assert result["object"] == "super"

    def test_method_call_with_arguments(self) -> None:
        name_node = create_mock_node("identifier", "add")
        arg1 = create_mock_node("identifier", "item")
        arg2 = create_mock_node("integer_literal", "1")
        open_paren = create_mock_node("(", "(")
        close_paren = create_mock_node(")", ")")
        comma = create_mock_node(",", ",")
        args_node = create_mock_node(
            "argument_list",
            children=[open_paren, arg1, comma, arg2, close_paren],
        )
        call_node = create_mock_node(
            "method_invocation",
            fields={"name": name_node, "arguments": args_node},
        )
        result = extract_java_method_call_info(call_node)
        assert result["arguments"] == 2

    def test_invalid_node_type(self) -> None:
        node = create_mock_node("class_declaration")
        result = extract_java_method_call_info(node)
        assert result == {}


class TestIsJavaMainMethod:
    def test_valid_main_method_with_array(self) -> None:
        name_node = create_mock_node("identifier", "main")
        void_type = create_mock_node("void_type", "void")
        public_mod = create_mock_node("public", "public")
        static_mod = create_mock_node("static", "static")
        modifiers = create_mock_node("modifiers", children=[public_mod, static_mod])
        param_type = create_mock_node("array_type", "String[]")
        param = create_mock_node("formal_parameter", fields={"type": param_type})
        params = create_mock_node("formal_parameters", children=[param])
        method_node = create_mock_node(
            "method_declaration",
            fields={"name": name_node, "type": void_type, "parameters": params},
            children=[modifiers],
        )
        result = is_java_main_method(method_node)
        assert result is True

    def test_valid_main_method_with_varargs(self) -> None:
        name_node = create_mock_node("identifier", "main")
        void_type = create_mock_node("void_type", "void")
        public_mod = create_mock_node("public", "public")
        static_mod = create_mock_node("static", "static")
        modifiers = create_mock_node("modifiers", children=[public_mod, static_mod])
        type_id = create_mock_node("type_identifier", "String")
        spread_param = create_mock_node("spread_parameter", children=[type_id])
        params = create_mock_node("formal_parameters", children=[spread_param])
        method_node = create_mock_node(
            "method_declaration",
            fields={"name": name_node, "type": void_type, "parameters": params},
            children=[modifiers],
        )
        result = is_java_main_method(method_node)
        assert result is True

    def test_not_main_wrong_name(self) -> None:
        name_node = create_mock_node("identifier", "run")
        void_type = create_mock_node("void_type", "void")
        method_node = create_mock_node(
            "method_declaration",
            fields={"name": name_node, "type": void_type},
        )
        result = is_java_main_method(method_node)
        assert result is False

    def test_not_main_not_void(self) -> None:
        name_node = create_mock_node("identifier", "main")
        int_type = create_mock_node("integral_type", "int")
        public_mod = create_mock_node("public", "public")
        static_mod = create_mock_node("static", "static")
        modifiers = create_mock_node("modifiers", children=[public_mod, static_mod])
        method_node = create_mock_node(
            "method_declaration",
            fields={"name": name_node, "type": int_type},
            children=[modifiers],
        )
        result = is_java_main_method(method_node)
        assert result is False

    def test_not_main_missing_public(self) -> None:
        name_node = create_mock_node("identifier", "main")
        void_type = create_mock_node("void_type", "void")
        static_mod = create_mock_node("static", "static")
        modifiers = create_mock_node("modifiers", children=[static_mod])
        param_type = create_mock_node("array_type", "String[]")
        param = create_mock_node("formal_parameter", fields={"type": param_type})
        params = create_mock_node("formal_parameters", children=[param])
        method_node = create_mock_node(
            "method_declaration",
            fields={"name": name_node, "type": void_type, "parameters": params},
            children=[modifiers],
        )
        result = is_java_main_method(method_node)
        assert result is False

    def test_not_main_missing_static(self) -> None:
        name_node = create_mock_node("identifier", "main")
        void_type = create_mock_node("void_type", "void")
        public_mod = create_mock_node("public", "public")
        modifiers = create_mock_node("modifiers", children=[public_mod])
        param_type = create_mock_node("array_type", "String[]")
        param = create_mock_node("formal_parameter", fields={"type": param_type})
        params = create_mock_node("formal_parameters", children=[param])
        method_node = create_mock_node(
            "method_declaration",
            fields={"name": name_node, "type": void_type, "parameters": params},
            children=[modifiers],
        )
        result = is_java_main_method(method_node)
        assert result is False

    def test_invalid_node_type(self) -> None:
        node = create_mock_node("constructor_declaration")
        result = is_java_main_method(node)
        assert result is False


class TestGetJavaVisibility:
    def test_public_visibility(self) -> None:
        public_node = create_mock_node("public", "public")
        node = create_mock_node("method_declaration", children=[public_node])
        result = get_java_visibility(node)
        assert result == "public"

    def test_protected_visibility(self) -> None:
        protected_node = create_mock_node("protected", "protected")
        node = create_mock_node("method_declaration", children=[protected_node])
        result = get_java_visibility(node)
        assert result == "protected"

    def test_private_visibility(self) -> None:
        private_node = create_mock_node("private", "private")
        node = create_mock_node("method_declaration", children=[private_node])
        result = get_java_visibility(node)
        assert result == "private"

    def test_package_private_visibility(self) -> None:
        node = create_mock_node("method_declaration", children=[])
        result = get_java_visibility(node)
        assert result == "package"


class TestBuildJavaQualifiedName:
    def test_nested_class(self) -> None:
        outer_name = create_mock_node("identifier", "Outer")
        outer_class = create_mock_node(
            "class_declaration",
            fields={"name": outer_name},
        )
        inner_name = create_mock_node("identifier", "Inner")
        inner_class = create_mock_node(
            "class_declaration",
            fields={"name": inner_name},
            parent=outer_class,
        )
        program = create_mock_node("program", children=[outer_class])
        outer_class.parent = program
        method_name = create_mock_node("identifier", "process")
        method = create_mock_node(
            "method_declaration",
            fields={"name": method_name},
            parent=inner_class,
        )
        result = build_java_qualified_name(method)
        assert result == ["Outer", "Inner"]

    def test_include_methods(self) -> None:
        class_name = create_mock_node("identifier", "MyClass")
        class_node = create_mock_node(
            "class_declaration",
            fields={"name": class_name},
        )
        program = create_mock_node("program", children=[class_node])
        class_node.parent = program
        method_name = create_mock_node("identifier", "process")
        method = create_mock_node(
            "method_declaration",
            fields={"name": method_name},
            parent=class_node,
        )
        inner_node = create_mock_node("identifier", "x", parent=method)
        result = build_java_qualified_name(inner_node, include_methods=True)
        assert result == ["MyClass", "process"]

    def test_exclude_classes(self) -> None:
        class_name = create_mock_node("identifier", "MyClass")
        class_node = create_mock_node(
            "class_declaration",
            fields={"name": class_name},
        )
        program = create_mock_node("program", children=[class_node])
        class_node.parent = program
        method_name = create_mock_node("identifier", "process")
        method = create_mock_node(
            "method_declaration",
            fields={"name": method_name},
            parent=class_node,
        )
        result = build_java_qualified_name(method, include_classes=False)
        assert result == []

    def test_empty_path(self) -> None:
        program = create_mock_node("program")
        node = create_mock_node("identifier", "x", parent=program)
        result = build_java_qualified_name(node)
        assert result == []


class TestExtractJavaAnnotationInfo:
    def test_simple_annotation(self) -> None:
        name_node = create_mock_node("identifier", "Override")
        annotation_node = create_mock_node(
            "annotation",
            fields={"name": name_node},
        )
        result = extract_java_annotation_info(annotation_node)
        assert result["name"] == "Override"
        assert result["arguments"] == []

    def test_annotation_with_arguments(self) -> None:
        name_node = create_mock_node("identifier", "SuppressWarnings")
        arg = create_mock_node("string_literal", '"unchecked"')
        open_paren = create_mock_node("(", "(")
        close_paren = create_mock_node(")", ")")
        args_node = create_mock_node(
            "annotation_argument_list",
            children=[open_paren, arg, close_paren],
        )
        annotation_node = create_mock_node(
            "annotation",
            fields={"name": name_node, "arguments": args_node},
        )
        result = extract_java_annotation_info(annotation_node)
        assert result["name"] == "SuppressWarnings"
        assert '"unchecked"' in result["arguments"]

    def test_invalid_node_type(self) -> None:
        node = create_mock_node("identifier")
        result = extract_java_annotation_info(node)
        assert result == JavaAnnotationInfo(name=None, arguments=[])


class TestFindJavaPackageStartIndex:
    def test_standard_maven_layout(self) -> None:
        parts = ["project", "src", "main", "java", "com", "example", "Helper"]
        result = find_java_package_start_index(parts)
        assert result == 4

    def test_non_standard_layout_with_main(self) -> None:
        parts = ["project", "src", "main", "com", "example", "Helper"]
        result = find_java_package_start_index(parts)
        assert result == 2

    def test_simple_src_layout(self) -> None:
        parts = ["project", "src", "com", "example", "Helper"]
        result = find_java_package_start_index(parts)
        assert result == 2

    def test_kotlin_layout(self) -> None:
        parts = ["project", "src", "main", "kotlin", "com", "example", "Helper"]
        result = find_java_package_start_index(parts)
        assert result == 4

    def test_scala_layout(self) -> None:
        parts = ["project", "src", "main", "scala", "com", "example", "Helper"]
        result = find_java_package_start_index(parts)
        assert result == 4

    def test_test_folder_layout(self) -> None:
        parts = ["project", "src", "test", "java", "com", "example", "HelperTest"]
        result = find_java_package_start_index(parts)
        assert result == 4

    def test_no_package_structure(self) -> None:
        parts = ["project", "build", "classes", "Helper"]
        result = find_java_package_start_index(parts)
        assert result is None

    def test_java_at_start(self) -> None:
        parts = ["java", "com", "example", "Helper"]
        result = find_java_package_start_index(parts)
        assert result is None

    def test_empty_parts(self) -> None:
        parts: list[str] = []
        result = find_java_package_start_index(parts)
        assert result is None
