"""`receiver_rebinding`: whether a method's `self = Other()` can run before a
given call, on every path or on some."""

from __future__ import annotations

from codebase_rag import constants as cs
from codebase_rag.parsers.python.receiver_rebinding import (
    ReceiverRebinding,
    receiver_rebinding,
)
from codebase_rag.tests.collaborator_support import nodes_of, parse


def _rebinding(source: str, receiver: str = "self") -> ReceiverRebinding:
    root = parse(cs.SupportedLanguage.PYTHON, source)
    calls = [c for c in nodes_of(root, cs.TS_PY_CALL) if c.text.startswith(b"self.run")]
    assert len(calls) == 1, source
    return receiver_rebinding(calls[0], receiver)


def _method(body: str) -> str:
    return "class C:\n    def m(self):\n" + body


def test_an_assignment_earlier_in_the_calls_block_always_runs_first() -> None:
    source = _method("        self = Other()\n        self.run()\n")
    assert _rebinding(source) == ReceiverRebinding(always=True, sometimes=True)


def test_an_assignment_in_a_branch_may_run_first() -> None:
    source = _method(
        "        if flag:\n            self = Other()\n        self.run()\n"
    )
    assert _rebinding(source) == ReceiverRebinding(always=False, sometimes=True)


def test_an_assignment_after_the_call_never_runs_first() -> None:
    source = _method("        self.run()\n        self = Other()\n")
    assert _rebinding(source) == ReceiverRebinding(always=False, sometimes=False)


def test_an_assignment_after_the_call_in_a_shared_loop_may_run_first() -> None:
    source = _method(
        "        for _ in range(2):\n"
        "            self.run()\n"
        "            self = Other()\n"
    )
    assert _rebinding(source) == ReceiverRebinding(always=False, sometimes=True)


def test_a_nested_scopes_assignment_rebinds_nothing() -> None:
    source = _method(
        "        def inner():\n            self = Other()\n"
        "        f = lambda: None\n"
        "        self.run()\n"
    )
    assert _rebinding(source) == ReceiverRebinding(always=False, sometimes=False)


def test_another_name_or_a_call_outside_a_function_rebinds_nothing() -> None:
    source = _method("        other = Other()\n        self.run()\n")
    assert _rebinding(source) == ReceiverRebinding(always=False, sometimes=False)
    assert _rebinding("self = Other()\nself.run()\n") == ReceiverRebinding(
        always=False, sometimes=False
    )
