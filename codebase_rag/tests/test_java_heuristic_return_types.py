# The name-based return-type fallbacks the Java resolver uses when a call's
# declared return type is unknown.
from __future__ import annotations

import pytest

from codebase_rag import constants as cs
from codebase_rag.parsers.java.method_resolver import (
    _java_factory_return_type,
    _java_getter_return_type,
)


@pytest.mark.parametrize(
    ("method_lower", "expected"),
    [
        ("getname", cs.JAVA_TYPE_STRING_FQN),
        ("getid", cs.JAVA_TYPE_LONG),
        ("getsize", cs.JAVA_TYPE_INT),
        ("getlength", cs.JAVA_TYPE_INT),
        ("getvalue", None),
    ],
)
def test_getter_return_type(method_lower: str, expected: str | None) -> None:
    assert _java_getter_return_type(method_lower) == expected


@pytest.mark.parametrize(
    ("method_call", "expected"),
    [
        ("factory.createUser", cs.JAVA_HEURISTIC_USER),
        ("Orders.newOrder", cs.JAVA_HEURISTIC_ORDER),
        ("factory.createWidget", None),
        ("createUser", None),
    ],
)
def test_factory_return_type(method_call: str, expected: str | None) -> None:
    assert _java_factory_return_type(method_call) == expected
