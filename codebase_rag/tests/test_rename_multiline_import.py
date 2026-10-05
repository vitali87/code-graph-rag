"""A rename touches only the renamed name of a parenthesised Python import.

The import rewriter split the names on commas, stripped each piece and joined
them with ", ", so line breaks and comments travelled inside the names. Every
line of a commented list was reflowed, a one-name-per-line list collapsed onto
one line, and a name after a commented line was never matched (the piece read
"# rounding\\n    compute_vat"): the import was left alone while the definition
and the calls were renamed, and the module stopped importing (issue #2875).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.editing.imports import SymbolMove, _py_rewrite
from codebase_rag.editing.rename import rename
from codebase_rag.tests.test_rename_op import _index

_RENAME = SymbolMove(
    symbol="compute_vat",
    old_module="pkg.tax",
    new_module="pkg.tax",
    new_name="vat_amount",
    rebind=True,
)


@pytest.mark.parametrize(
    ("statement", "expected"),
    [
        # Case 3: the name follows a commented line.
        (
            "from pkg.tax import (\n"
            "    round_cents,   # rounding\n"
            "    compute_vat,   # VAT on the net amount\n"
            ")",
            "from pkg.tax import (\n"
            "    round_cents,   # rounding\n"
            "    vat_amount,   # VAT on the net amount\n"
            ")",
        ),
        # Case 1: the name comes first, both lines commented.
        (
            "from pkg.tax import (\n"
            "    compute_vat,   # VAT on the net amount\n"
            "    round_cents,   # rounding\n"
            ")",
            "from pkg.tax import (\n"
            "    vat_amount,   # VAT on the net amount\n"
            "    round_cents,   # rounding\n"
            ")",
        ),
        # Case 2: one name per line with a trailing comma.
        (
            "from pkg.tax import (\n    compute_vat,\n    round_cents,\n)",
            "from pkg.tax import (\n    vat_amount,\n    round_cents,\n)",
        ),
        # An aliased entry keeps its alias and layout.
        (
            "from pkg.tax import (\n    round_cents,\n    compute_vat as cv,  # x\n)",
            "from pkg.tax import (\n    round_cents,\n    vat_amount as cv,  # x\n)",
        ),
    ],
)
def test_only_the_renamed_name_changes(statement: str, expected: str) -> None:
    assert _py_rewrite(statement, _RENAME) == expected


@pytest.mark.parametrize(
    ("statement", "expected"),
    [
        # Negatives: the forms that already worked are unchanged.
        ("from pkg.tax import compute_vat", "from pkg.tax import vat_amount"),
        (
            "from pkg.tax import round_cents, compute_vat",
            "from pkg.tax import round_cents, vat_amount",
        ),
        ("from pkg.tax import (compute_vat)", "from pkg.tax import (vat_amount)"),
    ],
)
def test_single_line_imports_rename_as_before(statement: str, expected: str) -> None:
    assert _py_rewrite(statement, _RENAME) == expected


def test_a_commented_name_is_found_for_a_move_too() -> None:
    # The same matching drives moves: the name after a commented line moves.
    out = _py_rewrite(
        "from pkg.tax import (\n    round_cents,   # rounding\n    compute_vat,\n)",
        SymbolMove("compute_vat", "pkg.tax", "pkg.vat"),
    )
    assert out is not None and "from pkg.vat import compute_vat" in out, out
    assert "round_cents" in out.split("from pkg.vat")[0], out


def test_a_name_only_in_a_comment_is_not_renamed() -> None:
    # Negative: `compute_vat` mentioned in a comment is not an imported name.
    statement = "from pkg.tax import (\n    round_cents,  # not compute_vat\n)"
    assert _py_rewrite(statement, _RENAME) is None


_INVOICE = """\
from pkg.tax import (
    round_cents,   # rounding
    compute_vat,   # VAT on the net amount
)


def total(net):
    return round_cents(net + compute_vat(net))
"""


def test_the_issue_repo_still_imports_after_the_rename(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    pkg = temp_repo / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "tax.py").write_text(
        "def compute_vat(net):\n    return net * 0.2\n\n\n"
        "def round_cents(x):\n    return round(x, 2)\n"
    )
    (pkg / "invoice.py").write_text(_INVOICE)
    graph = _index(temp_repo, mock_ingestor)

    rename(
        temp_repo,
        graph.fetch_all,
        graph.project,
        f"{graph.project}.pkg.tax.compute_vat",
        "vat_amount",
        dry_run=False,
    )

    assert (pkg / "invoice.py").read_text() == _INVOICE.replace(
        "compute_vat", "vat_amount"
    )
    result = subprocess.run(
        [sys.executable, "-c", "import pkg.invoice as i; print(i.total(10))"],
        cwd=temp_repo,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "12.0"
