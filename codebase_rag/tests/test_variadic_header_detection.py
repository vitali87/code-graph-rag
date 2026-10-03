"""Surplus-positional absorption must parse the header, not scan it.

`_header_absorbs_extra_positionals` is the SOLE suppressor of a
too-many-arguments verdict, so
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
from pathlib import Path

import pytest

from codebase_rag.structural_delta import (
    Definition,
    _absorbs_extra_positionals,
    _header_absorbs_extra_positionals,
    _parameter_list_end_row,
)


def _truth(header: str) -> bool:
    """Whether the header really absorbs a surplus POSITIONAL argument.

    Decided by CALLING the function with one more positional than it
    declares, so the oracle is CPython's own arity rule rather than a
    restatement of the implementation. The previous oracle read
    `vararg is not None or kwonlyargs`, the exact expression under test,
    so the keyword-only case agreed with a buggy implementation.
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


HEADERS = [
    pytest.param("def f(a):", id="plain"),
    pytest.param("def f(a, *rest):", id="star-args"),
    # A bare `*` takes NO extra positional; it is not a suppressor.
    pytest.param("def f(a, *, k):", id="bare-star-keyword-only"),
    pytest.param("def f(a, *, b=1):", id="bare-star-defaulted-keyword-only"),
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
    assert _header_absorbs_extra_positionals(header) is _truth(header)


def test_a_star_in_a_default_is_not_varargs() -> None:
    """False positive: silently suppressed a real too-many-arguments verdict."""
    assert _header_absorbs_extra_positionals("def f(a, b=2*3):") is False


def test_a_star_after_a_tuple_default_is_varargs() -> None:
    """False negative: made a correct edit roll back."""
    assert _header_absorbs_extra_positionals("def f(a=(1, 2), *rest):") is True


def test_a_multibyte_identifier_is_handled() -> None:
    """A valid non-ASCII name must not break the parse.

    `café` is a legal Python identifier whose `é` is two UTF-8 bytes,
    neither valid alone -- so any byte-wise handling of the header
    misreads it. (`©` is NOT a legal identifier character, so it tests
    the parser's rejection rather than multi-byte handling.)
    """
    assert _header_absorbs_extra_positionals("def café(a, *rest):") is True
    assert _header_absorbs_extra_positionals("def café(a):") is False


def test_an_unparseable_header_absorbs_nothing() -> None:
    # Refuse to guess: not-variadic keeps the arity check ACTIVE, which
    # is the safe direction for a suppressor.
    assert _header_absorbs_extra_positionals("def f(a, ") is False


def test_a_one_line_definition_keeps_its_suite() -> None:
    """`def f(a, *rest): return a` carries its body on the header line.

    Appending the stand-in `pass` made it unparseable, so the answer was
    False and a variadic callee was reported as receiving too many
    arguments.
    """
    assert _header_absorbs_extra_positionals("def f(a, *rest): return a") is True
    assert _header_absorbs_extra_positionals("def f(a): return a") is False


def test_keyword_only_params_do_not_absorb_an_extra_positional() -> None:
    """The regression: `*` is not `*args`.

    `def helper(a, *, b=1)` called as `helper(1, 2, b=3)` raises
    "takes 1 positional argument but 2 ... were given". Counting
    keyword-only parameters as a suppressor dropped that real TOO_MANY
    verdict, reddening every layer of three pr-split stacks.
    """
    assert _header_absorbs_extra_positionals("def f(a, *, b=1):") is False
    assert _header_absorbs_extra_positionals("def f(a, *, k):") is False
    # `*args` still suppresses, and still does so alongside keyword-only.
    assert _header_absorbs_extra_positionals("def f(a, *rest, k=1):") is True


def test_a_multiline_header_is_not_cut_at_a_paren_in_a_default(
    tmp_path: Path,
) -> None:
    """The read-back must reach the `)` that ends the PARAMETER LIST.

    Drives `_absorbs_extra_positionals`, not the string helper, because the
    truncation lives in that function's line loop: a test of the helper
    alone stays green with the loop broken. The loop used to stop at the
    first line containing `)`, cutting

        def f(
            a=g(),
            *rest,
        ):

    after `a=g(),`. The fragment does not parse, so the answer was False and
    a genuine `*rest` callee was reported as receiving too many arguments --
    the false negative this function exists to prevent.
    """
    source = (
        "def g():\n    return 1\n\n\ndef f(\n    a=g(),\n    *rest,\n):\n    pass\n"
    )
    (tmp_path / "m.py").write_text(source, encoding="utf-8")
    definition = Definition(
        label="Function",
        qualified_name="m.f",
        name="f",
        path="m.py",
        start_line=5,
        end_line=9,
        positional_params=("a",),
        fingerprint="",
        fingerprint_nodes=0,
        branches=frozenset(),
    )

    assert _absorbs_extra_positionals(definition, tmp_path) is True


@pytest.mark.parametrize(
    ("header", "end_row"),
    [
        ("def f(a):", 1),
        ("def f(\n    a=g(),\n", None),
        ("def f(\n    a=g(),\n    *rest,\n):", 4),
        ("def f(a=(1, 2), *rest):", 1),
        ("def f(", None),
        # A bracket inside a string or a comment is not a delimiter.
        ('def f(\n    a=")",\n    *rest,\n):', 4),
        ("def f(\n    a=1,  # )\n    *rest,\n):", 4),
        ('    def m(\n        self, a="(",\n    ):', 3),
    ],
)
def test_the_header_cut_follows_the_brackets(header: str, end_row: int | None) -> None:
    """A `)` inside a default, a string or a comment must not end the header.

    The read-back loop used to stop at the FIRST line containing `)`. For

        def f(
            a=g(),
            *rest,
        ):

    that cut after `a=g(),`, leaving an unparseable fragment that answers
    False -- so a genuine `*rest` callee was reported as receiving too many
    arguments, which is the false negative the docstring says this function
    exists to avoid. A raw bracket count then had the same fault for
    `a=")"` and for a `)` in a comment (bot review).
    """
    assert _parameter_list_end_row(header.splitlines()) == end_row


def test_a_paren_in_a_string_default_does_not_cut_the_header(
    tmp_path: Path,
) -> None:
    """End to end through the line loop, as the multi-line test above."""
    source = 'def f(\n    a=")",\n    *rest,\n):\n    pass\n'
    (tmp_path / "m.py").write_text(source, encoding="utf-8")
    definition = Definition(
        label="Function",
        qualified_name="m.f",
        name="f",
        path="m.py",
        start_line=1,
        end_line=5,
        positional_params=("a",),
        fingerprint="",
        fingerprint_nodes=0,
        branches=frozenset(),
    )

    assert _absorbs_extra_positionals(definition, tmp_path) is True
