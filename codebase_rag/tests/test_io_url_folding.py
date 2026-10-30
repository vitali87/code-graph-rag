"""`module_url_constants` and `url_target` (issue #2521): a module-level
string constant bound exactly once folds into a request URL as text, any
other `+` operand renders as a `{expr}` placeholder, and a name any other
binding may reach stays unfolded."""

from __future__ import annotations

import pytest
from tree_sitter import Node

from codebase_rag import constants as cs
from codebase_rag.parsers.io_access.constants import DYNAMIC_TARGET
from codebase_rag.parsers.io_access.models import (
    JS_TS_URL_GRAMMAR,
    PYTHON_URL_GRAMMAR,
)
from codebase_rag.parsers.io_access.url_folding import module_url_constants, url_target
from codebase_rag.tests.collaborator_support import parse

_CONSTANTS = {"BASE": "http://h"}


def _python_constants(source: str) -> dict[str, str]:
    return module_url_constants(
        parse(cs.SupportedLanguage.PYTHON, source), PYTHON_URL_GRAMMAR
    )


def _js_constants(source: str) -> dict[str, str]:
    return module_url_constants(
        parse(cs.SupportedLanguage.JS, source), JS_TS_URL_GRAMMAR
    )


def _assigned_value(language: cs.SupportedLanguage, expression: str) -> Node:
    # The right-hand side of `x = <expression>`, as a call argument would be.
    statement = parse(language, f"x = {expression}\n").named_children[0]
    value = statement.named_children[0].child_by_field_name(cs.FIELD_RIGHT)
    assert value is not None
    return value


def test_python_constants_fold_in_source_order() -> None:
    assert _python_constants(
        'BASE = "http://h"\nAPI = BASE + "/api"\nT: str = "/typed"\n'
    ) == {"BASE": "http://h", "API": "http://h/api", "T": "/typed"}


@pytest.mark.parametrize(
    "source",
    [
        'BASE = "http://h"\nBASE = "http://other"\n',
        'BASE = "http://h"\ndef f(BASE):\n    pass\n',
        'BASE = "http://h"\nfor BASE in xs:\n    pass\n',
        'BASE = "http://h"\nimport BASE\n',
    ],
    ids=["reassigned", "parameter", "loop_target", "import"],
)
def test_a_python_name_bound_twice_anywhere_is_not_folded(source: str) -> None:
    assert _python_constants(source) == {}


def test_a_python_star_import_anywhere_unfolds_every_name() -> None:
    # Which binding a call sees then depends on when it runs.
    assert _python_constants('BASE = "http://h"\nif c:\n    from m import *\n') == {}


def test_only_a_fully_static_string_value_is_a_constant() -> None:
    assert _python_constants('N = 3\nP = "/u/" + uid\nF = f"{N}/x"\nOK = "/ok"\n') == {
        "OK": "/ok"
    }


def test_js_const_declarations_fold_exported_or_not() -> None:
    assert _js_constants(
        'const BASE = "http://h";\n'
        'export const API = BASE + "/v1";\n'
        'const B = "/b", C = 3;\n'
        'let L = "/x";\n'
        'var V = "/v";\n'
    ) == {"BASE": "http://h", "API": "http://h/v1", "B": "/b"}


@pytest.mark.parametrize(
    "rebinding",
    [
        "function f({ BASE }) {}",
        "const f = BASE => BASE;",
        'import { BASE } from "./m";',
        'BASE = "/other";',
        "try {} catch (BASE) {}",
    ],
    ids=["destructured_parameter", "arrow_parameter", "import", "assigned", "catch"],
)
def test_a_js_const_any_other_binding_reaches_is_not_folded(rebinding: str) -> None:
    assert _js_constants(f'const BASE = "http://h";\n{rebinding}\n') == {}


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ('"/orders/" + id', "/orders/{id}"),
        ('BASE + "/users"', "http://h/users"),
        ("BASE", "http://h"),
        ('f"{BASE}/u/{uid}"', "http://h/u/{uid}"),
        # A parenthesised operand is one value, still split on its `+`.
        ('("/a/" + b) + "/c"', "/a/{b}/c"),
        # An operand holding a path delimiter would fabricate segments.
        ('"/files/" + (a / b)', "/files/{*}"),
    ],
)
def test_a_python_url_renders_each_operand_in_order(
    expression: str, expected: str
) -> None:
    node = _assigned_value(cs.SupportedLanguage.PYTHON, expression)
    assert url_target(node, PYTHON_URL_GRAMMAR, _CONSTANTS) == expected


@pytest.mark.parametrize(
    "expression",
    [
        # Placeholders alone carry no identity.
        "uid",
        "a + b",
        # Only `+` concatenates.
        '"/x" - y',
    ],
)
def test_a_python_url_without_literal_text_is_dynamic(expression: str) -> None:
    node = _assigned_value(cs.SupportedLanguage.PYTHON, expression)
    assert url_target(node, PYTHON_URL_GRAMMAR, _CONSTANTS) == DYNAMIC_TARGET


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ('"/orders/" + id', "/orders/{id}"),
        ('BASE + "/users"', "http://h/users"),
        ("`${BASE}/u/${id}`", "http://h/u/{id}"),
    ],
)
def test_a_js_url_folds_concatenation_and_templates(
    expression: str, expected: str
) -> None:
    node = _assigned_value(cs.SupportedLanguage.JS, expression)
    assert url_target(node, JS_TS_URL_GRAMMAR, _CONSTANTS) == expected


def test_an_unfolded_name_stays_a_placeholder_and_no_node_is_dynamic() -> None:
    node = _assigned_value(cs.SupportedLanguage.JS, 'BASE + "/users"')
    assert url_target(node, JS_TS_URL_GRAMMAR, {}) == "{BASE}/users"
    assert url_target(None, JS_TS_URL_GRAMMAR, _CONSTANTS) == DYNAMIC_TARGET
