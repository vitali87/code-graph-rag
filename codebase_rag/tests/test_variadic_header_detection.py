"""Surplus-positional absorption must parse the header, not scan it.

`_is_variadic` is the SOLE suppressor of a too-many-arguments verdict, so
both error directions do damage: a false positive hides a real arity
error, and a false negative makes a CORRECT edit fail its own
postcondition and roll back.

Scanning raw text got both wrong. A `*` inside a default (`b=2*3`) or a
string (`doc='a*b'`) reads as `*args`, and the header window was cut at
the first `)`, which can fall inside a default, a string or a comment --
hiding a real `*rest` that comes after it.

CPython is the ground truth here: calling the function with one more
positional than it declares is what decides whether the call raises.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from codebase_rag.structural_delta import Definition, _is_variadic


def _truth(header: str) -> bool:
    """Whether the header really absorbs a surplus POSITIONAL argument.

    Decided by CALLING the function with one more positional than it
    declares, so the oracle is CPython's own arity rule rather than a
    restatement of the implementation.
    """
    fn = ast.parse(header + "\n    pass\n").body[0]
    assert isinstance(fn, ast.FunctionDef)
    namespace: dict[str, object] = {"g": lambda: None}
    exec(header + "\n    pass\n", namespace)  # noqa: S102
    func = namespace[fn.name]
    assert callable(func)
    declared = len(fn.args.posonlyargs) + len(fn.args.args)
    required_kwonly = {
        kw.arg: None
        for kw, default in zip(fn.args.kwonlyargs, fn.args.kw_defaults, strict=True)
        if default is None
    }
    try:
        func(*range(declared + 1), **required_kwonly)
    except TypeError:
        return False
    return True


def _variadic(tmp_path: Path, source: str, name: str = "f", line: int = 1) -> bool:
    """`_is_variadic` for the def `name` starting on `line` of `source`."""
    (tmp_path / "m.py").write_text(source, encoding="utf-8")
    definition = Definition(
        label="Function",
        qualified_name=f"m.{name}",
        name=name,
        path="m.py",
        start_line=line,
        end_line=len(source.splitlines()),
        positional_params=(),
        fingerprint="",
        fingerprint_nodes=0,
        branches=frozenset(),
    )
    return _is_variadic(definition, tmp_path)


HEADERS = [
    pytest.param("def f(a):", id="plain"),
    pytest.param("def f(a, *rest):", id="star-args"),
    # A bare `*` takes NO extra positional; it is not a suppressor.
    pytest.param("def f(a, *, k):", id="bare-star-keyword-only"),
    pytest.param("def f(a, *, b=1):", id="bare-star-defaulted-keyword-only"),
    pytest.param("def f(a, *rest, k=1):", id="star-args-and-keyword-only"),
    pytest.param("def f(*args, **kw):", id="star-and-kwargs"),
    # A regex read the `*` in the default as `*args`.
    pytest.param("def f(a, b=2*3):", id="star-in-arithmetic-default"),
    pytest.param("def f(a, doc='a*b'):", id="star-inside-a-string"),
    # `find(")")` stopped inside the default, hiding the real `*rest`.
    pytest.param("def f(a=(1, 2), *rest):", id="star-after-tuple-default"),
    pytest.param("def f(a=g(), *rest):", id="star-after-call-default"),
    pytest.param("def f(a, cb=lambda x: (x)):", id="lambda-default"),
    pytest.param("def café(a, *rest):", id="multibyte-identifier"),
]


@pytest.mark.parametrize("header", HEADERS)
def test_it_agrees_with_python(tmp_path: Path, header: str) -> None:
    name = header[len("def ") : header.index("(")]
    source = f"def g():\n    return 1\n\n\n{header}\n    pass\n"
    assert _variadic(tmp_path, source, name=name, line=5) is _truth(header)


@pytest.mark.parametrize(
    "source",
    [
        # The read-back used to stop at the first line containing `)`.
        "def f(\n    a=g(),\n    *rest,\n):\n    pass\n",
        # A bracket inside a string or a comment is not a delimiter.
        'def f(\n    a=")",\n    *rest,\n):\n    pass\n',
        "def f(\n    a=1,  # )\n    *rest,\n):\n    pass\n",
        # A return annotation runs past the `)` that closes the parameters.
        "def f(a, *rest) -> tuple[\n    int,\n    str,\n]:\n    return (a, 'x')\n",
        # A one-line definition carries its suite on the header line.
        "def f(a, *rest): return a\n",
    ],
    ids=[
        "paren-in-call-default",
        "paren-in-string-default",
        "paren-in-comment",
        "multiline-return-annotation",
        "one-line-definition",
    ],
)
def test_a_star_rest_after_a_closing_bracket_is_found(
    tmp_path: Path, source: str
) -> None:
    """False negatives: each made a correct edit roll back."""
    assert _variadic(tmp_path, source) is True


def test_a_one_line_definition_without_star_rest_absorbs_nothing(
    tmp_path: Path,
) -> None:
    assert _variadic(tmp_path, "def f(a): return a\n") is False
