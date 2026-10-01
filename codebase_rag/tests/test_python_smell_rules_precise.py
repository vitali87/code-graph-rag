"""Issue #2693: the Python `broad_except` and mutable-default smells match the
code they name.

`broad_except` ran an unanchored `except\\s+Exception` regex over the whole
`except_clause`, body included, so a specific handler whose body held a
nested `except Exception` was flagged too, and so was `except
ExceptionGroup`. The mutable-default rules matched only `default_parameter`,
and an annotated default (`items: list = []`) is a `typed_default_parameter`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("ast_grep_py")

from codebase_rag import constants as cs  # noqa: E402
from codebase_rag.tests.test_ast_grep_analyzer import _fire  # noqa: E402

NESTED = """def load(path):
    try:
        return open(path).read()
    except FileNotFoundError:
        try:
            return fallback()
        except Exception:
            return ""
"""


def _hits(tmp_path: Path, rule: str, src: str) -> list[int]:
    return sorted(
        int(p[cs.KEY_START_LINE])
        for p in _fire(tmp_path, "probe.py", src)
        if p[cs.KEY_NAME] == rule
    )


def _handler(clause: str) -> str:
    return f"try:\n    run()\n{clause}\n    pass\n"


def test_a_specific_handler_around_a_broad_one_is_not_broad(tmp_path: Path) -> None:
    assert _hits(tmp_path, "broad_except", NESTED) == [7]


@pytest.mark.parametrize(
    "clause",
    [
        "except ExceptionGroup:",
        "except ExceptionalCaseError as err:",
        "except ValueError:  # never use except Exception here",
    ],
)
def test_a_name_that_starts_with_exception_is_not_broad(
    tmp_path: Path, clause: str
) -> None:
    assert _hits(tmp_path, "broad_except", _handler(clause)) == []


@pytest.mark.parametrize(
    "clause",
    ["except (ValueError, Exception):", "except (ValueError, Exception) as e:"],
)
def test_exception_in_a_tuple_is_broad(tmp_path: Path, clause: str) -> None:
    assert _hits(tmp_path, "broad_except", _handler(clause)) == [3]


@pytest.mark.parametrize(
    ("rule", "param"),
    [
        ("mutable_default_list", "items: list = []"),
        ("mutable_default_list", "items: list[int] = [1]"),
        ("mutable_default_dict", "opts: dict = {}"),
        ("mutable_default_dict", "opts: dict[str, int] = {'a': 1}"),
    ],
)
def test_a_typed_mutable_default_is_a_smell(
    tmp_path: Path, rule: str, param: str
) -> None:
    assert _hits(tmp_path, rule, f"def f({param}):\n    return 1\n") == [1]


# Negative: what must not change.


@pytest.mark.parametrize("clause", ["except Exception:", "except Exception as e:"])
def test_except_exception_is_still_broad(tmp_path: Path, clause: str) -> None:
    assert _hits(tmp_path, "broad_except", _handler(clause)) == [3]


def test_a_bare_except_is_still_bare_and_not_broad(tmp_path: Path) -> None:
    src = _handler("except:")

    assert _hits(tmp_path, "bare_except", src) == [3]
    assert _hits(tmp_path, "broad_except", src) == []


@pytest.mark.parametrize(
    ("rule", "param"),
    [
        ("mutable_default_list", "items=[]"),
        ("mutable_default_dict", "opts={}"),
    ],
)
def test_an_untyped_mutable_default_is_still_a_smell(
    tmp_path: Path, rule: str, param: str
) -> None:
    assert _hits(tmp_path, rule, f"def f({param}):\n    return 1\n") == [1]


@pytest.mark.parametrize(
    "param",
    ["items: list | None = None", "names: tuple = ()", "label: str = '[]'"],
)
def test_an_immutable_typed_default_is_no_smell(tmp_path: Path, param: str) -> None:
    src = f"def f({param}):\n    return 1\n"

    assert _hits(tmp_path, "mutable_default_list", src) == []
    assert _hits(tmp_path, "mutable_default_dict", src) == []
