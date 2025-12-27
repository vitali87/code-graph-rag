import pytest
from tree_sitter import Node, Parser

from codebase_rag.constants import SupportedLanguage
from codebase_rag.parser_loader import load_parsers
from codebase_rag.parsers.lua.utils import (
    extract_assigned_name,
    extract_pcall_second_identifier,
    find_ancestor_statement,
)


@pytest.fixture(scope="module")
def lua_parser() -> Parser:
    parsers, _ = load_parsers()
    if SupportedLanguage.LUA not in parsers:
        pytest.skip("Lua parser not available")
    return parsers[SupportedLanguage.LUA]


def parse_lua(parser: Parser, code: str) -> Node:
    tree = parser.parse(code.encode())
    return tree.root_node


def find_node_by_type(root: Node, node_type: str) -> Node | None:
    if root.type == node_type:
        return root
    for child in root.children:
        result = find_node_by_type(child, node_type)
        if result:
            return result
    return None


def find_all_nodes_by_type(root: Node, node_type: str) -> list[Node]:
    results: list[Node] = []
    if root.type == node_type:
        results.append(root)
    for child in root.children:
        results.extend(find_all_nodes_by_type(child, node_type))
    return results


def find_function_definition(root: Node) -> Node | None:
    return find_node_by_type(root, "function_definition")


def find_function_call(root: Node) -> Node | None:
    return find_node_by_type(root, "function_call")


class TestExtractAssignedName:
    def test_simple_assignment(self, lua_parser: Parser) -> None:
        code = "local myFunc = function() end"
        root = parse_lua(lua_parser, code)
        func_node = find_function_definition(root)
        assert func_node is not None
        result = extract_assigned_name(func_node)
        assert result == "myFunc"

    def test_assignment_without_local(self, lua_parser: Parser) -> None:
        code = "myFunc = function() end"
        root = parse_lua(lua_parser, code)
        func_node = find_function_definition(root)
        assert func_node is not None
        result = extract_assigned_name(func_node)
        assert result == "myFunc"

    def test_multiple_assignment_first_value(self, lua_parser: Parser) -> None:
        code = "local a, b = function() end, function() end"
        root = parse_lua(lua_parser, code)
        func_nodes = find_all_nodes_by_type(root, "function_definition")
        assert len(func_nodes) == 2
        result = extract_assigned_name(func_nodes[0])
        assert result == "a"

    def test_multiple_assignment_second_value(self, lua_parser: Parser) -> None:
        code = "local a, b = function() end, function() end"
        root = parse_lua(lua_parser, code)
        func_nodes = find_all_nodes_by_type(root, "function_definition")
        assert len(func_nodes) == 2
        result = extract_assigned_name(func_nodes[1])
        assert result == "b"

    def test_nested_function_in_table(self, lua_parser: Parser) -> None:
        code = "local M = { func = function() end }"
        root = parse_lua(lua_parser, code)
        func_node = find_function_definition(root)
        assert func_node is not None
        result = extract_assigned_name(func_node)
        assert result == "M"

    def test_no_assignment_context(self, lua_parser: Parser) -> None:
        code = "(function() end)()"
        root = parse_lua(lua_parser, code)
        func_node = find_function_definition(root)
        assert func_node is not None
        result = extract_assigned_name(func_node)
        assert result is None

    def test_function_as_argument(self, lua_parser: Parser) -> None:
        code = "someFunc(function() end)"
        root = parse_lua(lua_parser, code)
        func_node = find_function_definition(root)
        assert func_node is not None
        result = extract_assigned_name(func_node)
        assert result is None

    def test_dot_index_expression_rejected(self, lua_parser: Parser) -> None:
        code = "M.func = function() end"
        root = parse_lua(lua_parser, code)
        func_node = find_function_definition(root)
        assert func_node is not None
        result = extract_assigned_name(func_node)
        assert result is None

    def test_dot_index_accepted_with_custom_types(self, lua_parser: Parser) -> None:
        code = "M.func = function() end"
        root = parse_lua(lua_parser, code)
        func_node = find_function_definition(root)
        assert func_node is not None
        result = extract_assigned_name(
            func_node, ("identifier", "dot_index_expression")
        )
        assert result is not None

    def test_deeply_nested_assignment(self, lua_parser: Parser) -> None:
        code = """
local outer = {
    inner = {
        deep = function()
            return 1
        end
    }
}
"""
        root = parse_lua(lua_parser, code)
        func_node = find_function_definition(root)
        assert func_node is not None
        result = extract_assigned_name(func_node)
        assert result == "outer"

    def test_return_statement_function(self, lua_parser: Parser) -> None:
        code = "return function() end"
        root = parse_lua(lua_parser, code)
        func_node = find_function_definition(root)
        assert func_node is not None
        result = extract_assigned_name(func_node)
        assert result is None


class TestFindAncestorStatement:
    def test_finds_local_statement(self, lua_parser: Parser) -> None:
        code = "local x = 1"
        root = parse_lua(lua_parser, code)
        identifier = find_node_by_type(root, "identifier")
        assert identifier is not None
        stmt = find_ancestor_statement(identifier)
        assert stmt is not None
        assert stmt.type == "assignment_statement"

    def test_finds_assignment_statement(self, lua_parser: Parser) -> None:
        code = "x = 1"
        root = parse_lua(lua_parser, code)
        identifier = find_node_by_type(root, "identifier")
        assert identifier is not None
        stmt = find_ancestor_statement(identifier)
        assert stmt is not None
        assert stmt.type == "assignment_statement"

    def test_function_call_not_a_statement(self, lua_parser: Parser) -> None:
        code = "print('hello')"
        root = parse_lua(lua_parser, code)
        string_node = find_node_by_type(root, "string")
        assert string_node is not None
        stmt = find_ancestor_statement(string_node)
        assert stmt is None

    def test_finds_expression_statement(self, lua_parser: Parser) -> None:
        code = "x = print('hello')"
        root = parse_lua(lua_parser, code)
        string_node = find_node_by_type(root, "string")
        assert string_node is not None
        stmt = find_ancestor_statement(string_node)
        assert stmt is not None
        assert stmt.type == "assignment_statement"

    def test_finds_if_statement(self, lua_parser: Parser) -> None:
        code = "if x then y = 1 end"
        root = parse_lua(lua_parser, code)
        identifiers = find_all_nodes_by_type(root, "identifier")
        y_node = next((n for n in identifiers if n.text == b"y"), None)
        assert y_node is not None
        stmt = find_ancestor_statement(y_node)
        assert stmt is not None
        assert "statement" in stmt.type

    def test_finds_for_statement(self, lua_parser: Parser) -> None:
        code = "for i = 1, 10 do x = i end"
        root = parse_lua(lua_parser, code)
        stmt = find_node_by_type(root, "for_statement")
        assert stmt is not None

    def test_no_statement_ancestor(self, lua_parser: Parser) -> None:
        code = "local x = 1"
        root = parse_lua(lua_parser, code)
        result = find_ancestor_statement(root)
        assert result is None

    def test_nested_in_function(self, lua_parser: Parser) -> None:
        code = """
function test()
    local x = 1
    return x
end
"""
        root = parse_lua(lua_parser, code)
        return_stmt = find_node_by_type(root, "return_statement")
        assert return_stmt is not None
        identifier = find_node_by_type(return_stmt, "identifier")
        assert identifier is not None
        stmt = find_ancestor_statement(identifier)
        assert stmt is not None
        assert stmt.type == "return_statement"


class TestExtractPcallSecondIdentifier:
    def test_basic_pcall_require(self, lua_parser: Parser) -> None:
        code = "local ok, json = pcall(require, 'json')"
        root = parse_lua(lua_parser, code)
        call_node = find_function_call(root)
        assert call_node is not None
        result = extract_pcall_second_identifier(call_node)
        assert result == "json"

    def test_pcall_with_different_names(self, lua_parser: Parser) -> None:
        code = "local success, myModule = pcall(require, 'mymodule')"
        root = parse_lua(lua_parser, code)
        call_node = find_function_call(root)
        assert call_node is not None
        result = extract_pcall_second_identifier(call_node)
        assert result == "myModule"

    def test_pcall_single_return_value(self, lua_parser: Parser) -> None:
        code = "local ok = pcall(require, 'json')"
        root = parse_lua(lua_parser, code)
        call_node = find_function_call(root)
        assert call_node is not None
        result = extract_pcall_second_identifier(call_node)
        assert result is None

    def test_pcall_three_return_values(self, lua_parser: Parser) -> None:
        code = "local a, b, c = pcall(require, 'json')"
        root = parse_lua(lua_parser, code)
        call_node = find_function_call(root)
        assert call_node is not None
        result = extract_pcall_second_identifier(call_node)
        assert result == "b"

    def test_pcall_not_in_assignment(self, lua_parser: Parser) -> None:
        code = "pcall(require, 'json')"
        root = parse_lua(lua_parser, code)
        call_node = find_function_call(root)
        assert call_node is not None
        result = extract_pcall_second_identifier(call_node)
        assert result is None

    def test_pcall_with_non_identifier_target(self, lua_parser: Parser) -> None:
        code = "local ok, M.json = pcall(require, 'json')"
        root = parse_lua(lua_parser, code)
        call_node = find_function_call(root)
        assert call_node is not None
        result = extract_pcall_second_identifier(call_node)
        assert result is None

    def test_xpcall_pattern(self, lua_parser: Parser) -> None:
        code = "local ok, result = xpcall(require, errorHandler, 'module')"
        root = parse_lua(lua_parser, code)
        call_node = find_function_call(root)
        assert call_node is not None
        result = extract_pcall_second_identifier(call_node)
        assert result == "result"

    def test_nested_pcall(self, lua_parser: Parser) -> None:
        code = """
local function safe_require(mod)
    local ok, result = pcall(require, mod)
    return ok, result
end
"""
        root = parse_lua(lua_parser, code)
        call_nodes = find_all_nodes_by_type(root, "function_call")
        pcall_node = next(
            (n for n in call_nodes if n.text and b"pcall" in n.text), None
        )
        assert pcall_node is not None
        result = extract_pcall_second_identifier(pcall_node)
        assert result == "result"

    def test_pcall_in_if_block(self, lua_parser: Parser) -> None:
        code = """
if condition then
    local ok, mod = pcall(require, 'optional')
end
"""
        root = parse_lua(lua_parser, code)
        call_nodes = find_all_nodes_by_type(root, "function_call")
        pcall_node = next(
            (n for n in call_nodes if n.text and b"pcall" in n.text), None
        )
        assert pcall_node is not None
        result = extract_pcall_second_identifier(pcall_node)
        assert result == "mod"


class TestEdgeCases:
    def test_empty_function_body(self, lua_parser: Parser) -> None:
        code = "local f = function() end"
        root = parse_lua(lua_parser, code)
        func_node = find_function_definition(root)
        assert func_node is not None
        result = extract_assigned_name(func_node)
        assert result == "f"

    def test_complex_expression_assignment(self, lua_parser: Parser) -> None:
        code = "local result = (condition and function() end or other)"
        root = parse_lua(lua_parser, code)
        func_node = find_function_definition(root)
        assert func_node is not None
        result = extract_assigned_name(func_node)
        assert result == "result"

    def test_method_syntax_function(self, lua_parser: Parser) -> None:
        code = "function M:method() end"
        root = parse_lua(lua_parser, code)
        func_node = find_node_by_type(root, "function_declaration")
        assert func_node is not None

    def test_unicode_identifier(self, lua_parser: Parser) -> None:
        code = "local функция = function() end"
        root = parse_lua(lua_parser, code)
        func_node = find_function_definition(root)
        if func_node:
            result = extract_assigned_name(func_node)
            assert result is not None

    def test_multiline_assignment(self, lua_parser: Parser) -> None:
        code = """
local handler = function(
    arg1,
    arg2
)
    return arg1 + arg2
end
"""
        root = parse_lua(lua_parser, code)
        func_node = find_function_definition(root)
        assert func_node is not None
        result = extract_assigned_name(func_node)
        assert result == "handler"
