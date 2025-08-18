"""Utilities for processing Java code with tree-sitter."""

from typing import TypedDict

from tree_sitter import Node

from .utils import safe_decode_text

# Constants for delimiter tokens used in argument parsing
DELIMITER_TOKENS = ["(", ")", ","]


class JavaClassInfo(TypedDict):
    """Type definition for Java class information."""

    name: str | None
    type: str
    superclass: str | None
    interfaces: list[str]
    modifiers: list[str]
    type_parameters: list[str]


class JavaMethodInfo(TypedDict):
    """Type definition for Java method information."""

    name: str | None
    type: str
    return_type: str | None
    parameters: list[str]
    modifiers: list[str]
    type_parameters: list[str]
    annotations: list[str]


class JavaFieldInfo(TypedDict):
    """Type definition for Java field information."""

    name: str | None
    type: str | None
    modifiers: list[str]
    annotations: list[str]


class JavaAnnotationInfo(TypedDict):
    """Type definition for Java annotation information."""

    name: str | None
    arguments: list[str]


def extract_java_package_name(package_node: Node) -> str | None:
    """Extract the package name from a Java package declaration.

    Handles patterns like:
    - package com.example.app;
    - package java.util;

    Args:
        package_node: The package_declaration node.

    Returns:
        The full package name, or None if not found.
    """
    if package_node.type != "package_declaration":
        return None

    # Look for scoped_identifier containing the package path
    for child in package_node.children:
        if child.type == "scoped_identifier":
            return safe_decode_text(child)
        elif child.type == "identifier":
            return safe_decode_text(child)

    return None


def extract_java_import_path(import_node: Node) -> dict[str, str]:
    """Extract imports from a Java import declaration.

    Handles patterns like:
    - import java.util.List; -> {"List": "java.util.List"}
    - import java.util.*; -> {"*java.util": "java.util"}
    - import static java.lang.Math.PI; -> {"PI": "java.lang.Math.PI"}
    - import static java.util.Collections.*; -> {"*java.util.Collections": "java.util.Collections"}

    Args:
        import_node: The import_declaration node.

    Returns:
        Dictionary mapping imported names to their full paths.
    """
    if import_node.type != "import_declaration":
        return {}

    imports: dict[str, str] = {}
    imported_path = None
    is_wildcard = False

    # Parse import declaration
    for child in import_node.children:
        if child.type == "static":
            pass
        elif child.type == "scoped_identifier":
            imported_path = safe_decode_text(child)
        elif child.type == "identifier":
            imported_path = safe_decode_text(child)
        elif child.type == "asterisk":
            is_wildcard = True

    if not imported_path:
        return imports

    if is_wildcard:
        # Wildcard import: import java.util.*; or import static java.util.Collections.*;
        wildcard_key = f"*{imported_path}"
        imports[wildcard_key] = imported_path
    else:
        # Regular import: import java.util.List; or import static java.lang.Math.PI;
        parts = imported_path.split(".")
        if parts:
            imported_name = parts[-1]  # Last part is class/method name
            imports[imported_name] = imported_path

    return imports


def extract_java_class_info(class_node: Node) -> JavaClassInfo:
    """Extract information from a Java class declaration.

    Args:
        class_node: The class_declaration, interface_declaration, enum_declaration,
                   or annotation_type_declaration node.

    Returns:
        Dictionary containing class information:
        - name: Class name
        - type: "class", "interface", "enum", or "annotation"
        - superclass: Superclass name (for classes)
        - interfaces: List of implemented interface names
        - modifiers: List of access modifiers
        - type_parameters: List of generic type parameters
    """
    if class_node.type not in [
        "class_declaration",
        "interface_declaration",
        "enum_declaration",
        "annotation_type_declaration",
        "record_declaration",
    ]:
        return JavaClassInfo(
            name=None,
            type="",
            superclass=None,
            interfaces=[],
            modifiers=[],
            type_parameters=[],
        )

    info: JavaClassInfo = {
        "name": None,
        "type": class_node.type.replace("_declaration", ""),
        "superclass": None,
        "interfaces": [],
        "modifiers": [],
        "type_parameters": [],
    }

    # Extract class name
    name_node = class_node.child_by_field_name("name")
    if name_node:
        info["name"] = safe_decode_text(name_node)

    # Extract superclass and interfaces
    superclass_node = class_node.child_by_field_name("superclass")
    if superclass_node:
        if superclass_node.type == "type_identifier":
            info["superclass"] = safe_decode_text(superclass_node)
        elif superclass_node.type == "generic_type":
            # Handle generic superclass like Class<T>
            for child in superclass_node.children:
                if child.type == "type_identifier":
                    info["superclass"] = safe_decode_text(child)
                    break

    # Extract interfaces (interfaces field contains the super_interfaces node)
    interfaces_node = class_node.child_by_field_name("interfaces")
    if interfaces_node:
        # Look for type_list containing the interface types
        for child in interfaces_node.children:
            if child.type == "type_list":
                for type_child in child.children:
                    interface_name = None
                    if type_child.type == "type_identifier":
                        interface_name = safe_decode_text(type_child)
                    elif type_child.type == "generic_type":
                        # Handle generic interfaces like Comparable<T>
                        for sub_child in type_child.children:
                            if sub_child.type == "type_identifier":
                                interface_name = safe_decode_text(sub_child)
                                break
                    if interface_name:
                        info["interfaces"].append(interface_name)

    # Extract type parameters
    type_params_node = class_node.child_by_field_name("type_parameters")
    if type_params_node:
        for child in type_params_node.children:
            if child.type == "type_parameter":
                param_name = safe_decode_text(child.child_by_field_name("name"))
                if param_name:
                    info["type_parameters"].append(param_name)

    # Extract modifiers using correct tree-sitter traversal
    for child in class_node.children:
        if child.type == "modifiers":
            # Look inside the modifiers node for actual modifier tokens
            for modifier_child in child.children:
                if modifier_child.type in [
                    "public",
                    "private",
                    "protected",
                    "static",
                    "final",
                    "abstract",
                ]:
                    modifier = safe_decode_text(modifier_child)
                    if modifier:
                        info["modifiers"].append(modifier)

    return info


def extract_java_method_info(method_node: Node) -> JavaMethodInfo:
    """Extract information from a Java method or constructor declaration.

    Args:
        method_node: The method_declaration or constructor_declaration node.

    Returns:
        Dictionary containing method information:
        - name: Method name
        - type: "method" or "constructor"
        - return_type: Return type (for methods)
        - parameters: List of parameter types
        - modifiers: List of access modifiers
        - type_parameters: List of generic type parameters
        - annotations: List of annotations
    """
    if method_node.type not in ["method_declaration", "constructor_declaration"]:
        return JavaMethodInfo(
            name=None,
            type="",
            return_type=None,
            parameters=[],
            modifiers=[],
            type_parameters=[],
            annotations=[],
        )

    info: JavaMethodInfo = {
        "name": None,
        "type": "constructor"
        if method_node.type == "constructor_declaration"
        else "method",
        "return_type": None,
        "parameters": [],
        "modifiers": [],
        "type_parameters": [],
        "annotations": [],
    }

    # Extract method name
    name_node = method_node.child_by_field_name("name")
    if name_node:
        info["name"] = safe_decode_text(name_node)

    # Extract return type (for methods)
    if method_node.type == "method_declaration":
        type_node = method_node.child_by_field_name("type")
        if type_node:
            info["return_type"] = safe_decode_text(type_node)

    # Extract parameters using tree-sitter field access
    params_node = method_node.child_by_field_name("parameters")
    if params_node:
        for child in params_node.children:
            if child.type == "formal_parameter":
                param_type_node = child.child_by_field_name("type")
                if param_type_node:
                    param_type = safe_decode_text(param_type_node)
                    if param_type:
                        info["parameters"].append(param_type)
            elif child.type == "spread_parameter":
                # Handle varargs (String... args) using tree-sitter traversal
                for subchild in child.children:
                    if subchild.type == "type_identifier":
                        param_type_text = safe_decode_text(subchild)
                        if param_type_text:
                            param_type = param_type_text + "..."
                            info["parameters"].append(param_type)
                        break

    # Extract modifiers and annotations using correct tree-sitter traversal
    for child in method_node.children:
        if child.type == "modifiers":
            # Look inside the modifiers node for actual modifier tokens
            for modifier_child in child.children:
                if modifier_child.type in [
                    "public",
                    "private",
                    "protected",
                    "static",
                    "final",
                    "abstract",
                    "synchronized",
                ]:
                    modifier = safe_decode_text(modifier_child)
                    if modifier:
                        info["modifiers"].append(modifier)
                elif modifier_child.type == "annotation":
                    annotation = safe_decode_text(modifier_child)
                    if annotation:
                        info["annotations"].append(annotation)

    return info


def extract_java_field_info(field_node: Node) -> JavaFieldInfo:
    """Extract information from a Java field declaration.

    Args:
        field_node: The field_declaration node.

    Returns:
        Dictionary containing field information:
        - name: Field name
        - type: Field type
        - modifiers: List of access modifiers
        - annotations: List of annotations
    """
    if field_node.type != "field_declaration":
        return JavaFieldInfo(
            name=None,
            type=None,
            modifiers=[],
            annotations=[],
        )

    info: JavaFieldInfo = {
        "name": None,
        "type": None,
        "modifiers": [],
        "annotations": [],
    }

    # Extract field type
    type_node = field_node.child_by_field_name("type")
    if type_node:
        info["type"] = safe_decode_text(type_node)

    # Extract field name from variable declarator
    declarator_node = field_node.child_by_field_name("declarator")
    if declarator_node and declarator_node.type == "variable_declarator":
        name_node = declarator_node.child_by_field_name("name")
        if name_node:
            info["name"] = safe_decode_text(name_node)

    # Extract modifiers and annotations using correct tree-sitter traversal
    for child in field_node.children:
        if child.type == "modifiers":
            # Look inside the modifiers node for actual modifier tokens
            for modifier_child in child.children:
                if modifier_child.type in [
                    "public",
                    "private",
                    "protected",
                    "static",
                    "final",
                    "transient",
                    "volatile",
                ]:
                    modifier = safe_decode_text(modifier_child)
                    if modifier:
                        info["modifiers"].append(modifier)
                elif modifier_child.type == "annotation":
                    annotation = safe_decode_text(modifier_child)
                    if annotation:
                        info["annotations"].append(annotation)

    return info


def extract_java_method_call_info(call_node: Node) -> dict[str, str | int | None]:
    """Extract information from a Java method invocation.

    Handles patterns like:
    - methodName() -> {"name": "methodName", "object": None}
    - obj.methodName() -> {"name": "methodName", "object": "obj"}
    - this.methodName() -> {"name": "methodName", "object": "this"}
    - super.methodName() -> {"name": "methodName", "object": "super"}
    - ClassName.staticMethod() -> {"name": "staticMethod", "object": "ClassName"}

    Args:
        call_node: The method_invocation node.

    Returns:
        Dictionary containing call information:
        - name: Method name being called
        - object: Object/class the method is called on (None for local calls)
        - arguments: Number of arguments (count)
    """
    if call_node.type != "method_invocation":
        return {}

    info: dict[str, str | int | None] = {"name": None, "object": None, "arguments": 0}

    # Extract method name
    name_node = call_node.child_by_field_name("name")
    if name_node:
        info["name"] = safe_decode_text(name_node)

    # Extract object/class name
    object_node = call_node.child_by_field_name("object")
    if object_node:
        if object_node.type == "identifier":
            info["object"] = safe_decode_text(object_node)
        elif object_node.type == "this":
            info["object"] = "this"
        elif object_node.type == "super":
            info["object"] = "super"
        elif object_node.type == "field_access":
            # Handle chained method calls like obj.field.method()
            info["object"] = safe_decode_text(object_node)

    # Count arguments
    args_node = call_node.child_by_field_name("arguments")
    if args_node:
        argument_count = 0
        for child in args_node.children:
            if child.type not in DELIMITER_TOKENS:
                argument_count += 1
        info["arguments"] = argument_count

    return info


def is_java_main_method(method_node: Node) -> bool:
    """Check if a Java method is a main method using tree-sitter analysis.

    Validates the complete Java main method signature:
    public static void main(String[] args)

    Args:
        method_node: The method_declaration node.

    Returns:
        True if the method is a main method, False otherwise.
    """
    if method_node.type != "method_declaration":
        return False

    # Check method name using correct tree-sitter field access
    name_node = method_node.child_by_field_name("name")
    if not name_node or safe_decode_text(name_node) != "main":
        return False

    # Check return type using correct tree-sitter field access
    type_node = method_node.child_by_field_name("type")
    if not type_node or type_node.type != "void_type":
        return False

    # Check modifiers using tree-sitter AST traversal
    has_public = False
    has_static = False

    for child in method_node.children:
        if child.type == "modifiers":
            # Look inside the modifiers node using tree-sitter traversal
            for modifier_child in child.children:
                if modifier_child.type == "public":
                    has_public = True
                elif modifier_child.type == "static":
                    has_static = True

    if not (has_public and has_static):
        return False

    # Check parameter signature using correct tree-sitter field access
    parameters_node = method_node.child_by_field_name("parameters")
    if not parameters_node:
        return False

    # Should have exactly one parameter: String[] args (or String... args)
    param_count = 0
    valid_param = False

    for child in parameters_node.children:
        if child.type == "formal_parameter":
            param_count += 1

            # Use tree-sitter field access to get parameter type
            type_node = child.child_by_field_name("type")
            if type_node:
                type_text = safe_decode_text(type_node)
                # Accept String[], String..., or variations like java.lang.String[]
                if type_text and (
                    "String[]" in type_text
                    or "String..." in type_text
                    or type_text.endswith("String[]")
                    or type_text.endswith("String...")
                ):
                    valid_param = True

        elif child.type == "spread_parameter":
            # Handle varargs (String... args) using tree-sitter traversal
            param_count += 1

            # Check if it contains String type
            for subchild in child.children:
                if subchild.type == "type_identifier":
                    type_text = safe_decode_text(subchild)
                    if type_text == "String":
                        valid_param = True
                        break

    return param_count == 1 and valid_param


def get_java_visibility(node: Node) -> str:
    """Get the visibility modifier of a Java element.

    Args:
        node: Any Java node that can have visibility modifiers.

    Returns:
        The visibility level: "public", "protected", "private", or "package".
    """
    for child in node.children:
        if child.type == "public":
            return "public"
        elif child.type == "protected":
            return "protected"
        elif child.type == "private":
            return "private"

    return "package"  # Default package visibility


def build_java_qualified_name(
    node: Node,
    include_classes: bool = True,
    include_methods: bool = False,
) -> list[str]:
    """Build a qualified name path for a Java node.

    Traverses up the AST from the given node to find all containing classes,
    interfaces, and optionally methods.

    Args:
        node: The tree-sitter node to start from.
        include_classes: If True, include containing class types in the path.
        include_methods: If True, include containing method names in the path.

    Returns:
        List of path components from outermost to innermost.
        For example: ["com.example", "OuterClass", "InnerClass"] for a nested class.
    """
    path_parts = []
    current = node.parent

    while current and current.type != "program":
        if (
            current.type
            in [
                "class_declaration",
                "interface_declaration",
                "enum_declaration",
                "annotation_type_declaration",
                "record_declaration",
            ]
            and include_classes
        ):
            name_node = current.child_by_field_name("name")
            if name_node:
                class_name = safe_decode_text(name_node)
                if class_name:
                    path_parts.append(class_name)
        elif (
            current.type in ["method_declaration", "constructor_declaration"]
            and include_methods
        ):
            name_node = current.child_by_field_name("name")
            if name_node:
                method_name = safe_decode_text(name_node)
                if method_name:
                    path_parts.append(method_name)

        current = current.parent

    path_parts.reverse()
    return path_parts


def extract_java_annotation_info(
    annotation_node: Node,
) -> JavaAnnotationInfo:
    """Extract information from a Java annotation.

    Handles patterns like:
    - @Override
    - @SuppressWarnings("unchecked")
    - @RequestMapping(value="/api", method=RequestMethod.GET)

    Args:
        annotation_node: The annotation node.

    Returns:
        Dictionary containing annotation information:
        - name: Annotation name
        - arguments: List of argument values
    """
    if annotation_node.type != "annotation":
        return JavaAnnotationInfo(name=None, arguments=[])

    info: JavaAnnotationInfo = {"name": None, "arguments": []}

    # Extract annotation name
    name_node = annotation_node.child_by_field_name("name")
    if name_node:
        info["name"] = safe_decode_text(name_node)

    # Extract arguments
    args_node = annotation_node.child_by_field_name("arguments")
    if args_node:
        for child in args_node.children:
            if child.type not in DELIMITER_TOKENS:
                arg_value = safe_decode_text(child)
                if arg_value:
                    info["arguments"].append(arg_value)

    return info
