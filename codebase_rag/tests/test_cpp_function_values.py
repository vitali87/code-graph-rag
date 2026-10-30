"""`cpp.function_values` (issues #2529, #2593): the identifiers a C file
stores where a function pointer goes (initialisers, assignments, call
arguments, returns), and what the file declares at file scope that decides
whether such a name can be a function defined elsewhere: its `static`
functions, its objects and its enumeration constants."""

from __future__ import annotations

from tree_sitter import Node

from codebase_rag import constants as cs
from codebase_rag.parsers.cpp.function_values import (
    CFileScope,
    c_file_scope,
    c_function_value_identifiers,
    enumerator_names,
    local_enumerator_names,
)
from codebase_rag.tests.collaborator_support import first, nodes_of, parse

_C = cs.SupportedLanguage.C

_OPS = """\
typedef int (*handler_t)(int);
struct ops { handler_t open; handler_t close; handler_t read; };
static int my_open(int x) { return x; }
static int my_close(int x);
int my_read(int x) { return x; }
int other(int x) { return x; }
int ext(int x);
int (*fp)(int);
enum color { RED, GREEN };
handler_t table[] = { my_open, &my_close };
struct ops o = { .open = my_open, .close = (handler_t)my_close };
handler_t pick(int flag) {
    struct ops local;
    local.read = flag ? my_read : other;
    register_cb(*fp, my_read(1), (my_open));
    handler_t h = other;
    return my_read;
}
"""


def _texts(nodes: list[Node]) -> list[str]:
    return sorted(n.text.decode() for n in nodes if n.text is not None)


def _function(root: Node, name: str) -> Node:
    (found,) = [
        n
        for n in nodes_of(root, "function_definition")
        if n.text is not None and f" {name}(".encode() in n.text
    ]
    return found


def test_each_value_slot_yields_the_function_it_stores() -> None:
    body = _function(parse(_C, _OPS), "pick").child_by_field_name(cs.FIELD_BODY)
    assert body is not None
    # Both branches of the conditional, a parenthesized argument, an
    # initialiser and the returned value; `flag` is only tested, `*fp` is
    # called through, and `my_read(1)` is a call, not a value.
    assert _texts(list(c_function_value_identifiers(body, frozenset()))) == [
        "my_open",
        "my_read",
        "my_read",
        "other",
        "other",
    ]


def test_initialiser_lists_casts_and_address_of_yield_their_function() -> None:
    root = parse(_C, _OPS)
    file_scope_only = c_function_value_identifiers(
        root, frozenset({"function_definition"})
    )
    # The boundary keeps every function body out of the walk.
    assert _texts(list(file_scope_only)) == [
        "my_close",
        "my_close",
        "my_open",
        "my_open",
    ]


def test_file_scope_separates_static_functions_from_objects() -> None:
    assert c_file_scope(parse(_C, _OPS)) == CFileScope(
        # A static prototype makes the later plain definition internal too.
        tu_local_functions=frozenset({"my_open", "my_close"}),
        # A function pointer is an object, however it is declared.
        objects=frozenset({"fp", "table", "o", "RED", "GREEN"}),
    )


def test_file_scope_looks_through_preprocessor_and_linkage_blocks() -> None:
    scope = c_file_scope(
        parse(
            cs.SupportedLanguage.CPP,
            "#ifdef X\nstatic int guarded(void) { return 0; }\n#endif\n"
            'extern "C" { static int linked(void) { return 1; } }\n',
        )
    )
    assert scope.tu_local_functions == frozenset({"guarded", "linked"})


_ENUMS = """\
void f(void) {
    enum { LOCAL_A } x;
    {
        enum { INNER } z;
    }
    int y = LOCAL_A;
    enum { LATER } w;
}
struct s { enum { MEMBER } m; };
"""


def test_an_enum_anywhere_outside_a_body_declares_file_scope_constants() -> None:
    root = parse(_C, _ENUMS)
    # A struct member's enum is file scope in C; a body's enums are not.
    assert enumerator_names(root) == {"MEMBER"}
    assert c_file_scope(root).objects == frozenset({"MEMBER"})


def test_a_body_sees_the_constants_declared_before_it_in_open_blocks() -> None:
    root = parse(_C, _ENUMS)
    use = [n for n in nodes_of(root, "identifier") if n.text == b"LOCAL_A"][-1]
    # INNER's block has closed and LATER comes after the use.
    assert local_enumerator_names(use) == {"LOCAL_A"}


def test_at_file_scope_no_constant_is_local() -> None:
    root = parse(_C, _ENUMS)
    assert local_enumerator_names(first(root, "struct_specifier")) == set()
