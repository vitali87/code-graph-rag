"""Utilities for processing Rust code with tree-sitter."""

from tree_sitter import Node

from .utils import safe_decode_text


def extract_rust_impl_target(impl_node: Node) -> str | None:
    """Extract the type being implemented for in an impl block.

    Handles patterns like:
    - impl MyStruct { ... }
    - impl<T> MyStruct<T> { ... }
    - impl Display for MyStruct { ... }
    - impl<T: Clone> Display for MyStruct<T> { ... }

    Args:
        impl_node: The impl_item node.

    Returns:
        The name of the type being implemented for, or None if not found.
    """
    if impl_node.type != "impl_item":
        return None

    # Look for the type field which contains the type being implemented
    for i in range(impl_node.child_count):
        if impl_node.field_name_for_child(i) == "type":
            type_node = impl_node.child(i)
            # Handle generic types by extracting the base type name
            if type_node.type == "generic_type":
                # Look for the type identifier within the generic
                for child in type_node.children:
                    if child.type == "type_identifier":
                        return safe_decode_text(child)
            elif type_node.type == "type_identifier":
                return safe_decode_text(type_node)
            elif type_node.type == "scoped_type_identifier":
                # For paths like std::fmt::Display, get the last component
                for child in type_node.children:
                    if child.type == "type_identifier":
                        name = safe_decode_text(child)
                        if name:
                            return name

    return None


def extract_rust_trait_name(impl_node: Node) -> str | None:
    """Extract the trait name from an impl block implementing a trait.

    Handles patterns like:
    - impl Display for MyStruct { ... }
    - impl<T> Clone for MyStruct<T> { ... }

    Args:
        impl_node: The impl_item node.

    Returns:
        The name of the trait being implemented, or None if not found.
    """
    if impl_node.type != "impl_item":
        return None

    # Look for the trait field which contains the trait being implemented
    for i in range(impl_node.child_count):
        if impl_node.field_name_for_child(i) == "trait":
            trait_node = impl_node.child(i)
            # Handle generic traits
            if trait_node.type == "generic_type":
                for child in trait_node.children:
                    if child.type == "type_identifier":
                        return safe_decode_text(child)
            elif trait_node.type == "type_identifier":
                return safe_decode_text(trait_node)
            elif trait_node.type == "scoped_type_identifier":
                # For paths like std::fmt::Display
                for child in trait_node.children:
                    if child.type == "type_identifier":
                        name = safe_decode_text(child)
                        if name:
                            return name

    return None


def is_rust_async_function(func_node: Node) -> bool:
    """Check if a Rust function is async.

    Args:
        func_node: The function_item node.

    Returns:
        True if the function is async, False otherwise.
    """
    if func_node.type != "function_item":
        return False

    # Check for async modifier
    for child in func_node.children:
        if child.type == "async" or (
            child.type == "identifier" and safe_decode_text(child) == "async"
        ):
            return True

    return False


def extract_rust_macro_name(macro_node: Node) -> str | None:
    """Extract the name of a macro invocation.

    Handles patterns like:
    - println!("Hello")
    - vec![1, 2, 3]
    - assert_eq!(a, b)

    Args:
        macro_node: The macro_invocation node.

    Returns:
        The name of the macro, or None if not found.
    """
    if macro_node.type != "macro_invocation":
        return None

    # Look for the macro field which contains the macro name
    for i in range(macro_node.child_count):
        if macro_node.field_name_for_child(i) == "macro":
            macro_name_node = macro_node.child(i)
            if macro_name_node.type == "identifier":
                return safe_decode_text(macro_name_node)
            elif macro_name_node.type == "scoped_identifier":
                # For macros like std::println!
                for child in macro_name_node.children:
                    if child.type == "identifier":
                        name = safe_decode_text(child)
                        if name:
                            return name

    return None


def extract_rust_use_imports(use_node: Node) -> dict[str, str]:
    """Extract imports from a Rust use declaration with proper path mapping.

    Handles patterns like:
    - use std::collections::HashMap; -> {"HashMap": "std::collections::HashMap"}
    - use std::io::{self, Read, Write}; -> {"self": "std::io", "Read": "std::io::Read", "Write": "std::io::Write"}
    - use std::collections::HashMap as Map; -> {"Map": "std::collections::HashMap"}
    - use crate::utils::*; -> {"*crate::utils": "crate::utils"}

    Args:
        use_node: The use_declaration node.

    Returns:
        Dictionary mapping imported names to their full paths.
    """
    if use_node.type != "use_declaration":
        return {}

    imports = {}

    def extract_path_from_node(node: Node) -> str:
        """Extract the full path from a scoped identifier or identifier."""
        if node.type == "identifier" or node.type == "type_identifier":
            return safe_decode_text(node) or ""
        elif node.type in ("scoped_identifier", "scoped_type_identifier"):
            # For scoped identifiers, we need to recursively build the path
            parts = []

            def collect_path_parts(n: Node) -> None:
                if n.type in ("identifier", "type_identifier"):
                    part = safe_decode_text(n)
                    if part:
                        parts.append(part)
                elif n.type in ("scoped_identifier", "scoped_type_identifier"):
                    # Recursively process nested scoped identifiers
                    for child in n.children:
                        if child.type != "::":
                            collect_path_parts(child)
                elif n.type in ("crate", "super", "self"):
                    part = safe_decode_text(n)
                    if part:
                        parts.append(part)

            collect_path_parts(node)
            return "::".join(parts)
        elif node.type in ("crate", "super", "self"):
            return safe_decode_text(node) or ""
        return ""

    def process_use_tree(node: Node, base_path: str = "") -> None:
        """Process a use tree node and extract imports."""
        if node.type in ("identifier", "type_identifier"):
            # Simple identifier import
            name = safe_decode_text(node)
            if name:
                full_path = f"{base_path}::{name}" if base_path else name
                imports[name] = full_path

        elif node.type in ("scoped_identifier", "scoped_type_identifier"):
            # Scoped identifier - this is the final import
            full_path = extract_path_from_node(node)
            if full_path:
                parts = full_path.split("::")
                if parts:
                    imported_name = parts[-1]
                    imports[imported_name] = full_path

        elif node.type == "use_as_clause":
            # Handle aliases: use path as alias
            original_path = ""
            alias_name = ""

            # The structure is: path "as" alias
            children = [c for c in node.children if c.type != "as"]
            if len(children) == 2:
                path_node, alias_node = children

                # Handle special case of "self as Alias"
                if path_node.type == "self":
                    original_path = base_path if base_path else "self"
                else:
                    original_path = extract_path_from_node(path_node)
                    if base_path and original_path:
                        original_path = f"{base_path}::{original_path}"
                    elif base_path:
                        original_path = base_path

                alias_name = safe_decode_text(alias_node) or ""

            if alias_name and original_path:
                imports[alias_name] = original_path

        elif node.type == "use_wildcard":
            # Wildcard import: use path::*
            # Extract the base path from the wildcard node
            wildcard_base = ""
            for child in node.children:
                if child.type != "*":
                    wildcard_base = extract_path_from_node(child)
                    break

            if wildcard_base:
                wildcard_key = f"*{wildcard_base}"
                imports[wildcard_key] = wildcard_base
            elif base_path:
                wildcard_key = f"*{base_path}"
                imports[wildcard_key] = base_path

        elif node.type == "use_list":
            # Process items in a use list: {item1, item2, ...}
            for child in node.children:
                if child.type not in ("{", "}", ","):
                    process_use_tree(child, base_path)

        elif node.type == "scoped_use_list":
            # Handle scoped use list: path::{items}
            new_base_path = ""

            # Find the base path and the use list
            for child in node.children:
                if child.type in (
                    "identifier",
                    "scoped_identifier",
                    "crate",
                    "super",
                    "self",
                ):
                    new_base_path = extract_path_from_node(child)
                elif child.type == "use_list":
                    # Process the list with the new base path
                    final_base = (
                        f"{base_path}::{new_base_path}" if base_path else new_base_path
                    )
                    process_use_tree(child, final_base)

        elif node.type == "self":
            # Handle 'self' import
            imports["self"] = base_path if base_path else "self"

        else:
            # Recursively process children
            for child in node.children:
                process_use_tree(child, base_path)

    # Find the argument field of the use declaration
    argument_node = use_node.child_by_field_name("argument")
    if argument_node:
        process_use_tree(argument_node)

    return imports


def extract_rust_use_path(use_node: Node) -> list[str]:
    """Legacy function - use extract_rust_use_imports instead.

    This function is deprecated and may not handle complex import patterns correctly.
    """
    import_dict = extract_rust_use_imports(use_node)
    return list(import_dict.keys())


def get_rust_visibility(node: Node) -> str:
    """Get the visibility modifier of a Rust item.

    Args:
        node: Any Rust item node (function, struct, enum, etc.).

    Returns:
        The visibility level: "public", "crate", "super", "private", or "module".
    """
    # Check for visibility_modifier child
    for child in node.children:
        if child.type == "visibility_modifier":
            text = safe_decode_text(child)
            if text:
                if "pub(crate)" in text:
                    return "crate"
                elif "pub(super)" in text:
                    return "super"
                elif "pub(in" in text:
                    return "module"
                elif "pub" in text:
                    return "public"

    return "private"


def build_rust_module_path(
    node: Node,
    include_impl_targets: bool = False,
    include_classes: bool = False,
    class_node_types: list[str] | None = None,
) -> list[str]:
    """Build a path of containing modules/types for a Rust node.

    Traverses up the AST from the given node to find all containing modules,
    impl blocks, and optionally classes.

    Args:
        node: The tree-sitter node to start from.
        include_impl_targets: If True, include impl block targets in the path.
        include_classes: If True, include containing class types in the path.
        class_node_types: List of node types to consider as classes (for include_classes).

    Returns:
        List of path components from outermost to innermost (excluding source_file).
        For example: ["outer_mod", "inner_mod", "MyStruct"] for a method inside
        nested modules and an impl block.
    """
    path_parts = []
    current = node.parent

    while current and current.type != "source_file":
        if current.type == "mod_item":
            # This is an inline module
            if name_node := current.child_by_field_name("name"):
                text = name_node.text
                if text is not None:
                    path_parts.append(text.decode("utf8"))
        elif include_impl_targets and current.type == "impl_item":
            # This is inside an impl block - get the target type
            impl_target = extract_rust_impl_target(current)
            if impl_target:
                path_parts.append(impl_target)
        elif include_classes and class_node_types and current.type in class_node_types:
            # This is inside a class-like structure
            if current.type != "impl_item":  # Skip impl_item as it's handled above
                if name_node := current.child_by_field_name("name"):
                    text = name_node.text
                    if text is not None:
                        path_parts.append(text.decode("utf8"))

        current = current.parent

    path_parts.reverse()
    return path_parts
