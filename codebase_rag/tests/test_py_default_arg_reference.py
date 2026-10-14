"""A Python function named as a default argument value is referenced.

`def run(self, conv=_via_default)` binds `_via_default` into `run`, which
calls it as `conv(5)` whenever the caller omits the argument; no call names
it. The default was never walked, so `_via_default` had only its DEFINES
edge and `cgr dead-code` reported it, while JS/TS defaults are referenced
(issue #2838).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag.tests.test_js_ts_separate_export_class_members import (
    _dead,
    _index,
)

_ISSUE = """\
def _via_call(x):
    return x or 0


def _via_default(x):
    return x or 0


def take(fn):
    return fn(1)


class App:
    def run(self, conv=_via_default):
        return conv(5)


def entry():
    App().run()
    return take(_via_call)
"""


def test_the_default_value_keeps_its_function_live(tmp_path: Path) -> None:
    graph = _index(tmp_path, {"m.py": _ISSUE})
    assert {
        "from_qn": "p.m.App.run",
        "rel": "REFERENCES",
        "to_qn": "p.m._via_default",
    } in [
        {"from_qn": r["from_qn"], "rel": r["rel_type"], "to_qn": r["to_qn"]}
        for r in graph.rels
    ], graph.rels
    dead = _dead(graph)
    assert "m._via_default" not in dead, dead
    assert "m._via_call" not in dead, dead


@pytest.mark.parametrize(
    "signature",
    [
        "def run(conv: Callable[[int], int] = _helper):",
        "def run(*, conv=_helper):",
        "def run(conv=helpers._helper):",
    ],
)
def test_every_default_spelling_is_a_reference(tmp_path: Path, signature: str) -> None:
    src = (
        "from typing import Callable\n"
        "from pkg import helpers\n\n\n"
        "def _helper(x):\n    return x\n\n\n"
        f"{signature}\n    return conv(1)\n"
    )
    files = {
        "m.py": src,
        "pkg/__init__.py": "",
        "pkg/helpers.py": "def _helper(x):\n    return x\n",
    }
    dead = _dead(_index(tmp_path, files))
    used = "pkg.helpers._helper" if "helpers." in signature else "m._helper"
    assert used not in dead, dead


def test_an_unused_function_is_still_dead(tmp_path: Path) -> None:
    # Negatives: a data default and a function no default names change
    # nothing.
    src = (
        "def _unused(x):\n    return x\n\n\n"
        "LIMIT = 3\n\n\n"
        "def run(n=LIMIT, label='_unused'):\n    return n\n"
    )
    dead = _dead(_index(tmp_path, {"m.py": src}))
    assert "m._unused" in dead, dead
