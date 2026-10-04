"""Issue #2855: a name a Python module lists in `__all__` is exported.

`__all__` is the module's declared API (`from mod import *` exports exactly
those names), yet export was decided by the leading underscore alone, so an
underscore-named symbol listed there (networkx's `_dispatchable`,
`_clear_cache`, `_lazy_import`) was no dead-code root and was reported dead.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag.tests.test_is_exported_roots import _one, _run

LIB = """\
import sys

__all__ = ["public_api", "_explicitly_exported", "_Exported"]
__all__ += ("_added",)
__all__.extend(["_extended"])
__all__.append("_appended")

if sys.version_info >= (3, 8):
    __all__ += ["_conditional"]


def public_api():
    return 1


def _explicitly_exported():
    return 2


def _added():
    return 3


def _extended():
    return 4


def _appended():
    return 5


def _conditional():
    return 6


class _Exported:
    def _helper(self):
        return 7


def _truly_private():
    return 8


def outer():
    def _explicitly_exported():
        return 9

    return _explicitly_exported


class Holder:
    def _added(self):
        return 10
"""
OTHER = """\
def _explicitly_exported():
    return 11
"""


@pytest.fixture
def exported(tmp_path: Path) -> dict[str, bool]:
    return _run(tmp_path, {"lib.py": LIB, "other.py": OTHER})


@pytest.mark.parametrize(
    "symbol",
    [
        "lib._explicitly_exported",
        "lib._added",
        "lib._extended",
        "lib._appended",
        "lib._conditional",
    ],
)
def test_an_underscore_name_in_dunder_all_is_exported(
    exported: dict[str, bool], symbol: str
) -> None:
    assert exported[_qn(exported, symbol)] is True


# Negative: what must not change.


@pytest.mark.parametrize(
    ("symbol", "is_exported"),
    [
        ("lib.public_api", True),
        ("lib._truly_private", False),
        ("lib._Exported._helper", False),
        ("lib.outer._explicitly_exported", False),
        ("lib.Holder._added", False),
        ("other._explicitly_exported", False),
    ],
)
def test_names_dunder_all_does_not_list_keep_their_rule(
    exported: dict[str, bool], symbol: str, is_exported: bool
) -> None:
    assert exported[_qn(exported, symbol)] is is_exported


def _qn(exported: dict[str, bool], symbol: str) -> str:
    (qn,) = [qn for qn in exported if qn.endswith(f".{symbol}")]
    assert _one(exported, f".{symbol}") is exported[qn]
    return qn
