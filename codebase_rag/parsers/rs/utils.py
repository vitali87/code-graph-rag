from collections.abc import Sequence

from tree_sitter import Node

from ..utils import safe_decode_text


def extract_impl_target(impl_node: Node) -> str | None:
    if impl_node.type != "impl_item":
        return None

    for i in range(impl_node.child_count):
        if impl_node.field_name_for_child(i) == "type":
            type_node = impl_node.child(i)
            if type_node is None:
                continue
            if type_node.type == "generic_type":
                for child in type_node.children:
                    if child.type == "type_identifier":
                        return safe_decode_text(child)
            elif type_node.type == "type_identifier":
                return safe_decode_text(type_node)
            elif type_node.type == "scoped_type_identifier":
                for child in type_node.children:
                    if child.type == "type_identifier":
                        if name := safe_decode_text(child):
                            return name

    return None


def extract_use_imports(use_node: Node) -> dict[str, str]:
    if use_node.type != "use_declaration":
        return {}

    imports = {}

    def extract_path_from_node(node: Node) -> str:
        if node.type in ["identifier", "type_identifier"]:
            return safe_decode_text(node) or ""
        if node.type in ("scoped_identifier", "scoped_type_identifier"):
            parts = []

            def collect_path_parts(n: Node) -> None:
                if n.type in ("identifier", "type_identifier"):
                    part = safe_decode_text(n)
                    if part:
                        parts.append(part)
                elif n.type in ("scoped_identifier", "scoped_type_identifier"):
                    for child in n.children:
                        if child.type != "::":
                            collect_path_parts(child)
                elif n.type in ("crate", "super", "self"):
                    part = safe_decode_text(n)
                    if part:
                        parts.append(part)

            collect_path_parts(node)
            return "::".join(parts)
        if node.type in ("crate", "super", "self"):
            return safe_decode_text(node) or ""
        return ""

    def process_use_tree(node: Node, base_path: str = "") -> None:
        if node.type in ("identifier", "type_identifier"):
            name = safe_decode_text(node)
            if name:
                full_path = f"{base_path}::{name}" if base_path else name
                imports[name] = full_path

        elif node.type in ("scoped_identifier", "scoped_type_identifier"):
            full_path = extract_path_from_node(node)
            if full_path:
                parts = full_path.split("::")
                if parts:
                    imported_name = parts[-1]
                    imports[imported_name] = full_path

        elif node.type == "use_as_clause":
            original_path = ""
            alias_name = ""

            children = [c for c in node.children if c.type != "as"]
            if len(children) == 2:
                path_node, alias_node = children

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
            for child in node.children:
                if child.type not in ("{", "}", ","):
                    process_use_tree(child, base_path)

        elif node.type == "scoped_use_list":
            new_base_path = ""

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
                    final_base = (
                        f"{base_path}::{new_base_path}" if base_path else new_base_path
                    )
                    process_use_tree(child, final_base)

        elif node.type == "self":
            imports["self"] = base_path if base_path else "self"

        else:
            for child in node.children:
                process_use_tree(child, base_path)

    argument_node = use_node.child_by_field_name("argument")
    if argument_node:
        process_use_tree(argument_node)

    return imports


def build_module_path(
    node: Node,
    include_impl_targets: bool = False,
    include_classes: bool = False,
    class_node_types: Sequence[str] | None = None,
) -> list[str]:
    path_parts = []
    current = node.parent

    while current and current.type != "source_file":
        if current.type == "mod_item":
            if name_node := current.child_by_field_name("name"):
                text = name_node.text
                if text is not None:
                    path_parts.append(text.decode("utf8"))
        elif include_impl_targets and current.type == "impl_item":
            if impl_target := extract_impl_target(current):
                path_parts.append(impl_target)
        elif include_classes and class_node_types and current.type in class_node_types:
            if current.type != "impl_item":
                if name_node := current.child_by_field_name("name"):
                    text = name_node.text
                    if text is not None:
                        path_parts.append(text.decode("utf8"))

        current = current.parent

    path_parts.reverse()
    return path_parts
