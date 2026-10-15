"""`except BaseException:` is a broad except, broader than `except Exception:`.

On top of everything `Exception` catches it swallows `KeyboardInterrupt`,
`SystemExit` and `GeneratorExit`, so Ctrl-C and interpreter shutdown stop
working (flake8-bugbear B036). `broad_except` matched only `Exception` and
let the worse handler through (issue #2865).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag.tests.test_python_smell_rules_precise import _handler, _hits

pytest.importorskip("ast_grep_py")

BROAD_EXCEPT = "broad_except"

_HANDLERS = """\
def catch_exception(x):
    try:
        return int(x)
    except Exception:
        return 0

def catch_tuple(x):
    try:
        return int(x)
    except (ValueError, Exception):
        return 0

def catch_baseexception(x):
    try:
        return int(x)
    except BaseException:
        return 0

def catch_specific(x):
    try:
        return int(x)
    except KeyboardInterrupt:
        return 0
"""


def test_the_issue_flags_every_broad_handler(tmp_path: Path) -> None:
    assert _hits(tmp_path, BROAD_EXCEPT, _HANDLERS) == [4, 10, 16]


@pytest.mark.parametrize(
    "clause",
    [
        "except BaseException as e:",
        "except (OSError, BaseException):",
        "except (OSError, BaseException) as e:",
    ],
)
def test_every_spelling_of_baseexception_is_broad(tmp_path: Path, clause: str) -> None:
    assert _hits(tmp_path, BROAD_EXCEPT, _handler(clause)) == [3]


@pytest.mark.parametrize(
    "clause",
    [
        "except BaseExceptionGroup:",
        "except MyBaseException:",
        "except KeyboardInterrupt:",
        "except GeneratorExit:",
    ],
)
def test_a_name_that_only_contains_baseexception_is_not_broad(
    tmp_path: Path, clause: str
) -> None:
    # Negatives: a specific class, however it is named, is not broad.
    assert _hits(tmp_path, BROAD_EXCEPT, _handler(clause)) == []
