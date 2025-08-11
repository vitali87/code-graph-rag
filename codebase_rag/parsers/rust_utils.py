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


def extract_rust_use_path(use_node: Node) -> list[str]:
    """Extract the full import path from a use declaration.

    Handles patterns like:
    - use std::collections::HashMap;
    - use std::io::{self, Read, Write};
    - use super::module;
    - use crate::utils::*;

    Args:
        use_node: The use_declaration node.

    Returns:
        List of imported items (empty if not found).
    """
    if use_node.type != "use_declaration":
        return []

    imports = []

    def traverse_use_tree(node: Node, prefix: str = "") -> None:
        """Recursively traverse use tree to extract all imports."""
        if node.type == "identifier" or node.type == "type_identifier":
            name = safe_decode_text(node)
            if name:
                full_path = f"{prefix}{name}" if prefix else name
                imports.append(full_path)
        elif node.type == "scoped_identifier" or node.type == "scoped_type_identifier":
            parts = []
            for child in node.children:
                if child.type in ("identifier", "type_identifier"):
                    part = safe_decode_text(child)
                    if part:
                        parts.append(part)
            if parts:
                imports.append("::".join(parts))
        elif node.type == "use_wildcard":
            # Handle glob imports like use module::*
            imports.append(f"{prefix}*" if prefix else "*")
        elif node.type == "use_list":
            # Handle grouped imports like {Read, Write}
            for child in node.children:
                if child.type == "use_as_clause":
                    # Handle aliased imports
                    for subchild in child.children:
                        if subchild.type in ("identifier", "type_identifier"):
                            name = safe_decode_text(subchild)
                            if name:
                                traverse_use_tree(subchild, prefix)
                                break
                elif child.type != "," and child.type != "{" and child.type != "}":
                    traverse_use_tree(child, prefix)
        elif node.type == "use_as_clause":
            # Handle 'as' aliases - extract the original name
            for child in node.children:
                if child.type in ("identifier", "type_identifier", "scoped_identifier"):
                    traverse_use_tree(child, prefix)
                    break
        else:
            # Recursively process children
            for child in node.children:
                if child.type == "scoped_use_list":
                    # Extract prefix for scoped use list
                    new_prefix = ""
                    for subchild in child.children:
                        if subchild.type in ("identifier", "scoped_identifier"):
                            path = []
                            for part in subchild.children:
                                if part.type == "identifier":
                                    p = safe_decode_text(part)
                                    if p:
                                        path.append(p)
                            if not path and subchild.type == "identifier":
                                p = safe_decode_text(subchild)
                                if p:
                                    path.append(p)
                            if path:
                                new_prefix = "::".join(path) + "::"
                        elif subchild.type == "use_list":
                            traverse_use_tree(subchild, new_prefix)
                else:
                    traverse_use_tree(child, prefix)

    # Start traversal from the use_declaration's argument
    for i in range(use_node.child_count):
        if use_node.field_name_for_child(i) == "argument":
            traverse_use_tree(use_node.child(i))

    return imports


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
