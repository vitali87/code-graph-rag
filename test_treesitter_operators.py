#!/usr/bin/env python3
"""
Test Tree-sitter's native operator handling capabilities for C++.
This test explores how Tree-sitter C++ grammar natively handles operators
compared to the custom operator definitions in the codebase.
"""

from typing import Any

import tree_sitter_cpp as ts_cpp
from tree_sitter import Language, Parser

# Sample C++ code with various operators
CPP_CODE = """
class MyClass {
public:
    MyClass operator+(const MyClass& other) const;
    MyClass& operator=(const MyClass& other);
    bool operator==(const MyClass& other) const;
    MyClass& operator++();
    MyClass operator++(int);
    bool operator<(const MyClass& other) const;
    void operator()() const;
    int& operator[](int index);
};

int main() {
    MyClass a, b, c;

    // Binary expressions
    c = a + b;
    bool equal = a == b;
    bool less = a < b;

    // Unary expressions
    ++a;
    a++;

    // Assignment expressions
    a = b;
    a += b;

    // Call expressions
    a();
    int value = a[0];

    return 0;
}
"""


def explore_operator_nodes() -> None:
    """Explore how Tree-sitter handles operators in C++ code."""

    # Initialize parser
    CPP_LANGUAGE = Language(ts_cpp.language())
    parser = Parser(CPP_LANGUAGE)

    tree = parser.parse(CPP_CODE.encode("utf8"))

    print("=== Tree-sitter C++ Operator Analysis ===\n")

    # 1. Explore operator name nodes
    print("1. OPERATOR NAME NODES:")
    print("-" * 40)

    # Find operator_name nodes manually
    def find_operator_names(node: Any) -> list[tuple[Any, str]]:
        results = []
        if node.type == "operator_name":
            results.append((node, "operator_name"))
        for child in node.children:
            results.extend(find_operator_names(child))
        return results

    captures = find_operator_names(tree.root_node)
    for node, capture_name in captures:
        text = node.text.decode("utf8") if node.text else "None"
        print(f"  Operator name: '{text}'")
        print(f"    Node type: {node.type}")
        print(f"    Children: {[child.type for child in node.children]}")

        # Look for the actual operator symbol
        for child in node.children:
            if child.text and child.text.decode("utf8") not in ["operator"]:
                print(f"    Symbol: '{child.text.decode('utf8')}'")
                print(f"    Symbol type: {child.type}")
        print()

    # 2. Explore binary expressions
    print("2. BINARY EXPRESSIONS:")
    print("-" * 40)

    # Find binary expressions manually
    def find_binary_expressions(node: Any) -> None:
        if node.type == "binary_expression":
            # Try to extract operator information
            operator_text = "N/A"
            for child in node.children:
                if child.text and len(child.text.decode("utf8")) <= 3:
                    text = child.text.decode("utf8")
                    if text in ["+", "-", "*", "/", "==", "!=", "<", ">", "<=", ">="]:
                        operator_text = text
                        break

            expr_text = node.text.decode("utf8") if node.text else "N/A"
            print(f"  Binary expression: {expr_text}")
            print(f"    Detected operator: '{operator_text}'")
            print()

        for child in node.children:
            find_binary_expressions(child)

    find_binary_expressions(tree.root_node)

    # 3. Explore unary expressions
    print("3. UNARY EXPRESSIONS:")
    print("-" * 40)

    # Find unary/update expressions manually
    def find_unary_expressions(node: Any) -> None:
        if node.type in ["unary_expression", "update_expression"]:
            operator_text = "N/A"
            for child in node.children:
                if child.text:
                    text = child.text.decode("utf8")
                    if text in ["++", "--", "+", "-", "!", "~", "*", "&"]:
                        operator_text = text
                        break

            expr_text = node.text.decode("utf8") if node.text else "N/A"
            print(f"  {node.type}: {expr_text}")
            print(f"    Detected operator: '{operator_text}'")
            print()

        for child in node.children:
            find_unary_expressions(child)

    find_unary_expressions(tree.root_node)

    # 4. Explore assignment expressions
    print("4. ASSIGNMENT EXPRESSIONS:")
    print("-" * 40)

    # Find assignment expressions manually
    def find_assignment_expressions(node: Any) -> None:
        if node.type == "assignment_expression":
            operator_text = "N/A"
            for child in node.children:
                if child.text:
                    text = child.text.decode("utf8")
                    if text in [
                        "=",
                        "+=",
                        "-=",
                        "*=",
                        "/=",
                        "%=",
                        "&=",
                        "|=",
                        "^=",
                        "<<=",
                        ">>=",
                    ]:
                        operator_text = text
                        break

            expr_text = node.text.decode("utf8") if node.text else "N/A"
            print(f"  Assignment expression: {expr_text}")
            print(f"    Detected operator: '{operator_text}'")
            print()

        for child in node.children:
            find_assignment_expressions(child)

    find_assignment_expressions(tree.root_node)

    # 5. Other operator expressions (already covered above)


def compare_operator_extraction() -> None:
    """Compare Tree-sitter native vs custom operator extraction."""

    print("\n=== COMPARISON: Tree-sitter vs Custom Implementation ===\n")

    CPP_LANGUAGE = Language(ts_cpp.language())
    parser = Parser(CPP_LANGUAGE)

    # Test specific operator expressions
    test_expressions = [
        "a + b",
        "a == b",
        "++a",
        "a++",
        "a = b",
        "a += b",
        "a[0]",
        "a()",
    ]

    for expr in test_expressions:
        print(f"Expression: {expr}")
        print("-" * 20)

        tree = parser.parse(expr.encode("utf8"))
        root = tree.root_node

        def traverse_and_find_operators(node: Any, depth: int = 0) -> None:
            indent = "  " * depth

            # Check if this node has an operator field
            if hasattr(node, "child_by_field_name"):
                operator_node = node.child_by_field_name("operator")
                if operator_node:
                    operator_text = (
                        operator_node.text.decode("utf8")
                        if operator_node.text
                        else "None"
                    )
                    print(f"{indent}Tree-sitter operator field: '{operator_text}'")
                    print(f"{indent}  Node type: {node.type}")
                    print(f"{indent}  Operator node type: {operator_node.type}")

            # Recursively check children
            for child in node.children:
                traverse_and_find_operators(child, depth + 1)

        traverse_and_find_operators(root)
        print()


def performance_test() -> None:
    """Simple performance comparison."""

    import time

    print("\n=== PERFORMANCE COMPARISON ===\n")

    CPP_LANGUAGE = Language(ts_cpp.language())
    parser = Parser(CPP_LANGUAGE)

    # Parse a larger code sample
    large_code = CPP_CODE * 100  # Repeat the code 100 times

    # Time Tree-sitter field access approach
    start_time = time.time()
    tree = parser.parse(large_code.encode("utf8"))

    binary_count = 0

    def count_operators_treesitter(node: Any) -> None:
        nonlocal binary_count

        # Use Tree-sitter field access
        if hasattr(node, "child_by_field_name"):
            operator_node = node.child_by_field_name("operator")
            if operator_node:
                binary_count += 1

        for child in node.children:
            count_operators_treesitter(child)

    count_operators_treesitter(tree.root_node)
    ts_time = time.time() - start_time

    # Time custom manual traversal approach (simulated)
    start_time = time.time()
    tree = parser.parse(large_code.encode("utf8"))

    custom_count = 0

    def count_operators_custom(node: Any) -> None:
        nonlocal custom_count

        # Simulate the current custom approach
        if node.type in [
            "binary_expression",
            "unary_expression",
            "assignment_expression",
        ]:
            # Manual child traversal to find operators
            for child in node.children:
                if child.text and child.text.decode("utf8") in [
                    "+",
                    "-",
                    "*",
                    "/",
                    "=",
                    "==",
                    "!=",
                    "<",
                    ">",
                    "<=",
                    ">=",
                ]:
                    custom_count += 1
                    break

        for child in node.children:
            count_operators_custom(child)

    count_operators_custom(tree.root_node)
    custom_time = time.time() - start_time

    print(f"Tree-sitter field access: {ts_time:.4f}s ({binary_count} operators)")
    print(f"Custom manual traversal: {custom_time:.4f}s ({custom_count} operators)")
    print(f"Speedup: {custom_time / ts_time:.2f}x faster with Tree-sitter fields")


if __name__ == "__main__":
    explore_operator_nodes()
    compare_operator_extraction()
    performance_test()
