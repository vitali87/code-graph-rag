"""`*args` detection must parse the header, not scan it.

`_is_variadic` is the SOLE suppressor of a too-many-arguments verdict, so
both error directions do damage: a false positive hides a real arity
error, and a false negative makes a CORRECT edit fail its own
postcondition and roll back.

Scanning raw text got both wrong. A `*` inside a default (`b=2*3`) or a
string (`doc='a*b'`) reads as `*args`, and the header window was cut at
the first `)`, which can fall inside a default -- hiding a real `*rest`
that comes after it.

`ast` is the ground truth here: it is the same parser that decides
whether the call actually raises at runtime.
"""

from __future__ import annotations

import ast

import pytest

from codebase_rag.structural_delta import _header_is_variadic


def _truth(header: str) -> bool:
    """What Python itself says about the header."""
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


HEADERS = [
    pytest.param("def f(a):", id="plain"),
    pytest.param("def f(a, *rest):", id="star-args"),
    pytest.param("def f(a, *, k):", id="bare-star-keyword-only"),
    pytest.param("def f(*args, **kw):", id="star-and-kwargs"),
    # The regex read the `*` in the default as `*args`.
    pytest.param("def f(a, b=2*3):", id="star-in-arithmetic-default"),
    pytest.param("def f(a, doc='a*b'):", id="star-inside-a-string"),
    # `find(")")` stopped inside the default, hiding the real `*rest`.
    pytest.param("def f(a=(1, 2), *rest):", id="star-after-tuple-default"),
    pytest.param("def f(a=g(), *rest):", id="star-after-call-default"),
    pytest.param("def f(a, cb=lambda x: (x)):", id="lambda-default"),
]


@pytest.mark.parametrize("header", HEADERS)
def test_it_agrees_with_python(header: str) -> None:
    assert _header_is_variadic(header) is _truth(header)


def test_a_star_in_a_default_is_not_varargs() -> None:
    """False positive: silently suppressed a real too-many-arguments verdict."""
    assert _header_is_variadic("def f(a, b=2*3):") is False


def test_a_star_after_a_tuple_default_is_varargs() -> None:
    """False negative: made a correct edit roll back."""
    assert _header_is_variadic("def f(a=(1, 2), *rest):") is True


def test_a_multibyte_identifier_is_handled() -> None:
    """A valid non-ASCII name must not break the parse.

    `café` is a legal Python identifier whose `é` is two UTF-8 bytes,
    neither valid alone -- so any byte-wise handling of the header
    misreads it. (`©` is NOT a legal identifier character, so it tests
    the parser's rejection rather than multi-byte handling.)
    """
    assert _header_is_variadic("def café(a, *rest):") is True
    assert _header_is_variadic("def café(a):") is False


def test_an_unparseable_header_is_not_variadic() -> None:
    # Refuse to guess: not-variadic keeps the arity check ACTIVE, which
    # is the safe direction for a suppressor.
    assert _header_is_variadic("def f(a, ") is False


def test_keyword_only_params_do_not_absorb_an_extra_positional() -> None:
    """`*` is not `*args`: it absorbs no surplus positional."""
    assert _header_is_variadic("def f(a, *, b=1):") is False
    assert _header_is_variadic("def f(a, *, k):") is False
    assert _header_is_variadic("def f(a, *rest, k=1):") is True
