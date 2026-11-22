"""An instance call never binds a static member of the receiver's class.

JavaScript looks `c.run` up on the instance's prototype chain, which holds
no static member. With `class Child extends Base { static run() }`, a call
on `new Child()` runs the `run` Child inherits from Base, while the typed
receiver's lookup took `Child.run`, the static one, as `exact` (issue
#3672): callers went to the wrong method, and `cgr rename` of `Child.run`
would rewrite a call that belongs to `Base.run`.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.tests.test_js_member_pick_resolution import _calls, _index

_CLASSES = (
    "class Base { run() { return 1; } }\n"
    "class Child extends Base { static run() { return 2; } }\n"
    "class Mid extends Base { static run() { return 3; } }\n"
    "class Leaf extends Mid {}\n"
    "class Own extends Base { static run() { return 4; } run() { return 5; } }\n"
)
_CALLS = (
    "function made() { const c = new Child(); return c.run(); }\n"
    "function deep() { const l = new Leaf(); return l.run(); }\n"
    "function onclass() { return Child.run(); }\n"
    "function own() { const o = new Own(); return o.run(); }\n"
)


@pytest.fixture(params=["m.js", "m.ts"])
def calls(
    request: pytest.FixtureRequest, temp_repo: Path, mock_ingestor: MagicMock
) -> MagicMock:
    _index(temp_repo, mock_ingestor, {request.param: _CLASSES + _CALLS})
    return mock_ingestor


def test_an_instance_call_binds_the_inherited_instance_method(
    calls: MagicMock,
) -> None:
    assert _calls(calls, ".made") == {"m.Base.run": {"exact"}}


def test_a_static_member_up_the_chain_hides_no_base_method(
    calls: MagicMock,
) -> None:
    assert _calls(calls, ".deep") == {"m.Base.run": {"exact"}}


def test_a_call_on_the_class_still_binds_its_static_member(
    calls: MagicMock,
) -> None:
    assert _calls(calls, ".onclass") == {"m.Child.run": {"exact"}}


def test_a_class_declaring_an_instance_member_too_stops_the_walk(
    calls: MagicMock,
) -> None:
    # `Own` declares `run` for its instances as well, which hides Base's.
    assert "m.Base.run" not in _calls(calls, ".own")
