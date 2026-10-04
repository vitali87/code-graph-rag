#!/usr/bin/env python3
"""Print the start lines of the assignment captures cgr gets for Syntax._get_syntax (rich/syntax.py)."""
import sys, tree_sitter_python as tsp
from tree_sitter import Language, Parser, Query, QueryCursor
from codebase_rag.parsers.py.ast_analyzer import _PY_TRAVERSE_QUERY
lang = Language(tsp.language()); parser = Parser(lang)
src = open(sys.argv[1], "rb").read(); tree = parser.parse(src)
def find(n):
    if n.type == "function_definition" and n.child_by_field_name("name").text == b"_get_syntax": return n
    for c in n.children:
        r = find(c)
        if r: return r
fn = find(tree.root_node)
caps = QueryCursor(Query(lang, _PY_TRAVERSE_QUERY)).captures(fn)
print([a.start_point[0] + 1 for a in caps.get("assignment", [])])
