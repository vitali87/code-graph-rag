"""A mutable default built by a call is the same shared-default trap.

`def f(x=list())` shares one list across calls exactly as `def f(x=[])`
does, but the rules matched only the `[]`/`{}` literal nodes, and there was
no rule for sets at all, though `set()` is the only way to write an empty
set default (issue #2864).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag.tests.test_python_smell_rules_precise import _hits

pytest.importorskip("ast_grep_py")

LIST = "mutable_default_list"
DICT = "mutable_default_dict"
SET = "mutable_default_set"
_RULES = (LIST, DICT, SET)

_ISSUE = """\
def list_literal(x=[]):       return x
def list_call(x=list()):      return x
def dict_literal(x={}):       return x
def dict_call(x=dict()):      return x
def set_call(x=set()):        return x
def none_default(x=None):     return x
def tuple_default(x=()):      return x
"""


def _by_rule(tmp_path: Path, src: str) -> dict[str, list[int]]:
    return {rule: _hits(tmp_path, rule, src) for rule in _RULES}


def test_the_issue_flags_every_mutable_default(tmp_path: Path) -> None:
    assert _by_rule(tmp_path, _ISSUE) == {LIST: [1, 2], DICT: [3, 4], SET: [5]}


@pytest.mark.parametrize(
    ("rule", "param"),
    [
        (LIST, "items=list(seed)"),
        (LIST, "items: list[int] = list()"),
        (DICT, "opts=dict(a=1)"),
        (DICT, "opts: dict = dict()"),
        (DICT, "counts=defaultdict(int)"),
        (DICT, "counts=collections.defaultdict(list)"),
        (DICT, "order=OrderedDict()"),
        (DICT, "order=collections.OrderedDict()"),
        (SET, "seen={1, 2}"),
        (SET, "seen: set[str] = set()"),
    ],
)
def test_every_spelling_is_flagged_once(tmp_path: Path, rule: str, param: str) -> None:
    src = f"def f({param}):\n    return 1\n"
    assert _by_rule(tmp_path, src) == {r: [1] if r == rule else [] for r in _RULES}


@pytest.mark.parametrize(
    "param",
    [
        "x=tuple()",
        "x=frozenset()",
        "x=str()",
        "x=make_list()",
        "x=factory.list()",
        "x=sorted(items)",
        "x=None",
    ],
)
def test_an_immutable_or_unknown_call_is_no_smell(tmp_path: Path, param: str) -> None:
    # Negatives: only the mutable builtins' constructors are the trap.
    src = f"def f({param}):\n    return 1\n"
    assert _by_rule(tmp_path, src) == {r: [] for r in _RULES}


def test_a_call_in_the_body_is_no_default(tmp_path: Path) -> None:
    # Negative: `x = list()` inside the function makes a fresh list per call.
    src = "def f(x=None):\n    x = list() if x is None else x\n    return x\n"
    assert _by_rule(tmp_path, src) == {r: [] for r in _RULES}
