from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag.tests.conftest import get_nodes, get_relationships, run_updater

PROJECT = "c_macro_fn"
MODULE_QN = f"{PROJECT}.api"

HELPER = "static void helper(void) {}\n\n"


def _index(temp_repo: Path, mock_ingestor: MagicMock, source: str) -> None:
    project = temp_repo / PROJECT
    project.mkdir()
    (project / "Makefile").write_text("all:\n\tgcc -o api api.c\n")
    (project / "api.c").write_text(source)
    run_updater(project, mock_ingestor, skip_if_missing="c")


def _functions(mock_ingestor: MagicMock) -> dict[str, dict]:
    return {
        call[0][1][cs.KEY_QUALIFIED_NAME]: call[0][1]
        for call in get_nodes(mock_ingestor, cs.NodeLabel.FUNCTION)
    }


def _calls(mock_ingestor: MagicMock) -> set[tuple[str, str]]:
    return {
        (c.args[0][2], c.args[2][2])
        for c in get_relationships(mock_ingestor, cs.RelationshipType.CALLS)
    }


def _assert_named_with_calls(
    mock_ingestor: MagicMock, name: str, callee: str = "helper"
) -> None:
    functions = _functions(mock_ingestor)
    qn = f"{MODULE_QN}.{name}"
    assert qn in functions, sorted(functions)
    assert functions[qn][cs.KEY_NAME] == name
    assert not [q for q in functions if cs.PREFIX_ANONYMOUS in q], sorted(functions)
    assert (qn, f"{MODULE_QN}.{callee}") in _calls(mock_ingestor)


# tree-sitter-c recovers `int CJSON_CDECL main2(void)` by closing a
# `declaration` (`int CJSON_CDECL` + a MISSING `;`) and parsing the rest as a
# definition whose `type` is the real name and whose declarator is the
# parameter list read as a parenthesized declarator.
@pytest.mark.parametrize(
    ("header", "name"),
    [
        pytest.param("int CJSON_CDECL main2(void)\n{", "main2", id="brace-next-line"),
        pytest.param("int CJSON_CDECL main2(void) {", "main2", id="brace-same-line"),
        pytest.param("int CJSON_CDECL\nmain2(void)\n{", "main2", id="gnu-name-line"),
        pytest.param(
            "static int CJSON_CDECL internal(void)\n{", "internal", id="static"
        ),
        pytest.param(
            "char * CJSON_CDECL make_name(void)\n{", "make_name", id="pointer-return"
        ),
        pytest.param(
            "int CJSON_CDECL unnamed_param(int)\n{", "unnamed_param", id="type-param"
        ),
        pytest.param(
            "int VERY_LONG_EXPORT_MACRO_NAME exported(void) {",
            "exported",
            id="long-macro-same-line",
        ),
        pytest.param(
            "int CJSON_CDECL /* cdecl */ commented(void)\n{",
            "commented",
            id="comment-after-macro",
        ),
    ],
)
def test_macro_between_type_and_name_is_named_and_keeps_calls(
    temp_repo: Path, mock_ingestor: MagicMock, header: str, name: str
) -> None:
    _index(
        temp_repo,
        mock_ingestor,
        f"{HELPER}{header}\n    helper();\n    return 0;\n}}\n",
    )
    _assert_named_with_calls(mock_ingestor, name)


def test_macro_split_function_takes_return_type_from_split_declaration(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    _index(
        temp_repo,
        mock_ingestor,
        f"{HELPER}int CJSON_CDECL main2(void)\n{{\n    helper();\n    return 0;\n}}\n",
    )
    props = _functions(mock_ingestor)[f"{MODULE_QN}.main2"]
    assert props[cs.KEY_RETURN_TYPE] == "int"
    assert props[cs.KEY_START_LINE] == 3
    assert props[cs.KEY_END_LINE] == 7


def test_cjson_test_main_is_named_main_and_calls_its_helpers(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # DaveGamble/cJSON test.c: `main` went anonymous, so it was no entry
    # point and both of its callees were reported as dead code.
    _index(
        temp_repo,
        mock_ingestor,
        "static void print_preallocated(void) {}\n"
        "static void create_objects(void) {}\n"
        "\n"
        "int CJSON_CDECL main(void)\n"
        "{\n"
        "    create_objects();\n"
        "    print_preallocated();\n"
        "    return 0;\n"
        "}\n",
    )
    _assert_named_with_calls(mock_ingestor, "main", "create_objects")
    assert (f"{MODULE_QN}.main", f"{MODULE_QN}.print_preallocated") in _calls(
        mock_ingestor
    )


# Shapes tree-sitter-c already parses into one definition (the macro lands in
# an ERROR node or a type position); they must keep their names and calls.
@pytest.mark.parametrize(
    ("header", "name"),
    [
        pytest.param("EXPORT int before(void)\n{", "before", id="before-type-next"),
        pytest.param("EXPORT int before(void) {", "before", id="before-type-same"),
        pytest.param("int MYAPI same_line(void) {", "same_line", id="short-macro"),
        pytest.param(
            "int CJSON_CDECL named_params(int a, char *b)\n{",
            "named_params",
            id="named-params",
        ),
        pytest.param(
            "int EXPORT CJSON_CDECL two_macros(void)\n{", "two_macros", id="two-macros"
        ),
        pytest.param("int plain(void) {", "plain", id="plain-same-line"),
        pytest.param("int plain(void)\n{", "plain", id="plain-next-line"),
    ],
)
def test_already_named_definitions_are_unchanged(
    temp_repo: Path, mock_ingestor: MagicMock, header: str, name: str
) -> None:
    _index(
        temp_repo,
        mock_ingestor,
        f"{HELPER}{header}\n    helper();\n    return 0;\n}}\n",
    )
    _assert_named_with_calls(mock_ingestor, name)


def test_plain_function_keeps_its_return_type(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    _index(temp_repo, mock_ingestor, "long plain(void)\n{\n    return 0;\n}\n")
    props = _functions(mock_ingestor)[f"{MODULE_QN}.plain"]
    assert props[cs.KEY_RETURN_TYPE] == "long"


def test_macro_invocation_statement_is_not_a_function(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    _index(
        temp_repo,
        mock_ingestor,
        "DECLARE_THING(widget);\nREGISTER_HOOK(on_start)\nint counter;\n",
    )
    assert _functions(mock_ingestor) == {}


@pytest.mark.parametrize(
    "source",
    [
        pytest.param(
            "START_TEST(test_open)\n{\n    helper();\n}\nEND_TEST\n",
            id="block-macro-first-in-file",
        ),
        pytest.param(
            "int counter;\nSTART_TEST(test_open)\n{\n    helper();\n}\nEND_TEST\n",
            id="block-macro-after-complete-declaration",
        ),
    ],
)
def test_macro_with_block_is_not_named_after_the_macro(
    temp_repo: Path, mock_ingestor: MagicMock, source: str
) -> None:
    # `START_TEST(name) { ... }` parses as the same definition shape as the
    # macro-split one, but nothing was split off before it: no MISSING `;`
    # declaration precedes it, so neither the macro nor its argument is a
    # function name.
    _index(temp_repo, mock_ingestor, f"{HELPER}{source}")
    names = {props[cs.KEY_NAME] for props in _functions(mock_ingestor).values()}
    assert "START_TEST" not in names
    assert "test_open" not in names
    assert "counter" not in names


def test_function_pointer_declarations_are_not_functions(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    _index(
        temp_repo,
        mock_ingestor,
        "int (*on_event)(void);\n"
        "typedef int (*callback_t)(void);\n"
        "int CJSON_CDECL (*hook)(void);\n"
        "static int (*handlers[4])(int);\n",
    )
    assert _functions(mock_ingestor) == {}
