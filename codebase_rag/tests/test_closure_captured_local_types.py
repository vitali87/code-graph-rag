"""A nested function types the variables it captures (issue #3200).

`r = Range(); def inner(): return r.reset()` resolved `r.reset()` only while
`reset` was a project-unique name: the nested function's map held its own
bindings alone, so once `Other` also defined `reset` the call got no edge.
Inside a method, whose nested calls are not attributed to it, the call was
then linked nowhere. Python, JavaScript and TypeScript alike.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag.tests.conftest import create_and_run_updater

_PY_CLASSES = (
    "class Range:\n    def reset(self):\n        return 1\n\n\n"
    "class Other:\n    def reset(self):\n        return 2\n\n\n"
)
_JS_CLASSES = (
    "class Range { reset() { return 1; } }\nclass Other { reset() { return 2; } }\n"
)
_TS_CLASSES = (
    "class Range { reset(): number { return 1; } }\n"
    "class Other { reset(): number { return 2; } }\n"
)


def _reset_calls(repo: Path, files: dict[str, str], mock: MagicMock) -> dict[str, set]:
    # caller -> {(callee, resolution)} for every CALLS edge into a `reset`.
    for rel, text in files.items():
        (repo / rel).write_text(text, encoding="utf-8")
    create_and_run_updater(repo, mock)
    prefix = f"{repo.name}."
    out: dict[str, set] = {}
    for c in mock.ensure_relationship_batch.call_args_list:
        if c.args[1] != cs.RelationshipType.CALLS.value:
            continue
        callee = str(c.args[2][2]).removeprefix(prefix)
        if not callee.endswith(".reset"):
            continue
        props = (
            c.kwargs.get("properties") or (c.args[3] if len(c.args) > 3 else {}) or {}
        )
        out.setdefault(str(c.args[0][2]).removeprefix(prefix), set()).add(
            (callee, props.get(cs.KEY_RESOLUTION))
        )
    return out


_EXACT = cs.EdgeResolution.EXACT

_CAPTURED = {
    "py-nested-def": (
        "a.py",
        _PY_CLASSES
        + "def outer():\n    r = Range()\n\n    def inner():\n        return r.reset()\n\n"
        "    return inner\n",
        "a.outer.inner",
        "a.Range.reset",
    ),
    "py-def-in-method": (
        "a.py",
        _PY_CLASSES + "class Owner:\n    def method(self):\n        r = Range()\n\n"
        "        def cb():\n            return r.reset()\n\n        return cb\n",
        "a.Owner.method.cb",
        "a.Range.reset",
    ),
    "py-two-levels-up": (
        "a.py",
        _PY_CLASSES
        + "def outer():\n    r = Range()\n\n    def mid():\n        def inner():\n"
        "            return r.reset()\n\n        return inner\n\n    return mid\n",
        "a.outer.mid.inner",
        "a.Range.reset",
    ),
    "py-annotated-parameter": (
        "a.py",
        _PY_CLASSES
        + "def outer(r: Range):\n    def inner():\n        return r.reset()\n\n"
        "    return inner\n",
        "a.outer.inner",
        "a.Range.reset",
    ),
    "ts-nested-function": (
        "b.ts",
        _TS_CLASSES
        + "export function outer(): () => number {\n  const r = new Range();\n"
        "  function inner(): number {\n    return r.reset();\n  }\n  return inner;\n}\n",
        "b.outer.inner",
        "b.Range.reset",
    ),
    "ts-named-arrow": (
        "b.ts",
        _TS_CLASSES
        + "export function outer(): () => number {\n  const r = new Range();\n"
        "  const viaArrow = (): number => r.reset();\n  return viaArrow;\n}\n",
        "b.outer.viaArrow",
        "b.Range.reset",
    ),
    "ts-arrow-in-method": (
        "b.ts",
        _TS_CLASSES + "export class Owner {\n  method(): () => number {\n"
        "    const r = new Range();\n    const cb = (): number => r.reset();\n"
        "    return cb;\n  }\n}\n",
        "b.Owner.method.cb",
        "b.Range.reset",
    ),
    "js-nested-function": (
        "c.js",
        _JS_CLASSES + "function outer() {\n  const r = new Range();\n"
        "  function inner() { return r.reset(); }\n  return inner;\n}\n"
        "module.exports = { outer };\n",
        "c.outer.inner",
        "c.Range.reset",
    ),
    "js-sibling-declares-the-name-too": (
        # The enclosing function's OWN `r` is what `b` captures: a sibling's
        # same-named local is another variable, whatever order they come in.
        "c.js",
        _JS_CLASSES + "function outer() {\n  const r = new Range();\n"
        "  function b() { return r.reset(); }\n"
        "  function a() { const r = new Other(); return r; }\n  return [a, b];\n}\n"
        "module.exports = { outer };\n",
        "c.outer.b",
        "c.Range.reset",
    ),
}


@pytest.mark.parametrize("case", list(_CAPTURED))
def test_a_nested_function_types_a_captured_variable(
    temp_repo: Path, mock_ingestor: MagicMock, case: str
) -> None:
    rel, text, caller, target = _CAPTURED[case]
    calls = _reset_calls(temp_repo, {rel: text}, mock_ingestor)
    assert calls.get(caller) == {(target, _EXACT)}, calls


_HIDDEN = {
    "py-own-rebinding": (
        "a.py",
        _PY_CLASSES
        + "def outer():\n    r = Range()\n\n    def inner():\n        r = Other()\n"
        "        return r.reset()\n\n    return inner\n",
        "a.outer.inner",
        {("a.Other.reset", _EXACT)},
    ),
    "py-own-parameter": (
        "a.py",
        _PY_CLASSES
        + "def outer():\n    r = Range()\n\n    def inner(r):\n        return r.reset()\n\n"
        "    return inner\n",
        "a.outer.inner",
        None,
    ),
    "py-global-declaration": (
        "a.py",
        _PY_CLASSES
        + "r = None\n\n\ndef outer():\n    r = Range()\n\n    def inner():\n"
        "        global r\n        return r.reset()\n\n    return inner\n",
        "a.outer.inner",
        None,
    ),
    "py-rebound-in-between": (
        # `mid`'s untyped `r` is the one `inner` reads, not `outer`'s.
        "a.py",
        _PY_CLASSES + "def make():\n    return None\n\n\n"
        "def outer():\n    r = Range()\n\n    def mid():\n        r = make()\n\n"
        "        def inner():\n            return r.reset()\n\n        return inner\n\n"
        "    return mid\n",
        "a.outer.mid.inner",
        None,
    ),
    "py-sibling-local": (
        "a.py",
        _PY_CLASSES
        + "def outer():\n    def a():\n        r = Range()\n        return r\n\n"
        "    def b():\n        return r.reset()\n\n    return a, b\n",
        "a.outer.b",
        None,
    ),
    "js-own-declaration": (
        "c.js",
        _JS_CLASSES + "function outer() {\n  const r = new Range();\n"
        "  function inner() { const r = new Other(); return r.reset(); }\n"
        "  return inner;\n}\nmodule.exports = { outer };\n",
        "c.outer.inner",
        {("c.Other.reset", _EXACT)},
    ),
    "js-arrow-parameter": (
        "c.js",
        _JS_CLASSES + "function outer() {\n  const r = new Range();\n"
        "  const cb = (r) => r.reset();\n  return cb;\n}\nmodule.exports = { outer };\n",
        "c.outer.cb",
        None,
    ),
    "js-destructured-parameter": (
        "c.js",
        _JS_CLASSES + "function outer() {\n  const r = new Range();\n"
        "  const cb = ({ r }) => r.reset();\n  return cb;\n}\n"
        "module.exports = { outer };\n",
        "c.outer.cb",
        None,
    ),
    "js-reassigned-in-the-nested-function": (
        "c.js",
        _JS_CLASSES + "function outer() {\n  let r = new Range();\n"
        "  function inner(o) { r = o; return r.reset(); }\n  return inner;\n}\n"
        "module.exports = { outer };\n",
        "c.outer.inner",
        None,
    ),
}


@pytest.mark.parametrize("case", list(_HIDDEN))
def test_a_name_the_nested_function_binds_is_not_captured(
    temp_repo: Path, mock_ingestor: MagicMock, case: str
) -> None:
    # Negatives: a name bound by the nested function, or by a function
    # between it and the binding, is a different variable, and never takes
    # the captured `Range`. Where that variable has a type of its own, the
    # call binds it; otherwise whatever resolution it had before stands.
    rel, text, caller, expected = _HIDDEN[case]
    calls = _reset_calls(temp_repo, {rel: text}, mock_ingestor)
    targets = {callee for callee, _resolution in calls.get(caller, set())}
    module = rel.split(".", 1)[0]
    assert f"{module}.Range.reset" not in targets, calls
    if expected is not None:
        assert calls.get(caller) == expected, calls


def test_the_enclosing_functions_own_call_is_unchanged(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # Control: a call written directly in `outer` binds as before.
    text = _PY_CLASSES + "def outer():\n    r = Range()\n    return r.reset()\n"
    calls = _reset_calls(temp_repo, {"a.py": text}, mock_ingestor)
    assert calls == {"a.outer": {("a.Range.reset", _EXACT)}}, calls
