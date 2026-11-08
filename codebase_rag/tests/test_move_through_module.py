# move (issue #1534), Greptile TREX on PR #2907: a call that reaches the
# moved function through a name bound to its module (`util.discounted()`
# after `from shop import util`) was neither rewritten nor counted. The
# preview left the file out, and applying rolled back: the call no longer
# resolved and the import named a module the move had vacated.

from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.editing.move import Mover, MoveRefused, move
from codebase_rag.editing.transaction import load_history
from codebase_rag.tests.test_move_op import PROJECT, _index, _materialise
from codebase_rag.tests.test_move_safety import _python

SHOP: dict[str, str] = {
    "shop/__init__.py": "",
    # Only a constant stays behind, which the graph has no node for: the
    # move leaves the module empty as far as the contract can tell.
    "shop/util.py": (
        "TAX_RATE = 0.2\n\n\ndef discounted(price):\n    return price * (1 - TAX_RATE)\n"
    ),
    "shop/a.py": (
        "from shop.util import discounted\n\n\ndef first():\n    return discounted(5.0)\n"
    ),
}
CALL = "\n\ndef total():\n    return {receiver}.discounted(10.0)\n"
MAIN = "from shop.a import first; from shop.b import total; print(first() + total())"
QN = f"{PROJECT}.shop.util.discounted"


def _shop(temp_repo: Path, b: str, **extra: str):
    root = _materialise(temp_repo, {**SHOP, "shop/b.py": b, **extra})
    store, updater = _index(root)
    return root, store, updater


def _move(root: Path, store, updater, **kw):
    return move(
        root,
        store.fetch_all,
        PROJECT,
        QN,
        "shop.pricing",
        reingest=updater.reingest,
        **kw,
    )


@pytest.mark.parametrize(
    ("imports", "receiver", "expected"),
    [
        ("from shop import util", "util", "from shop import pricing\n"),
        ("import shop.util as u", "u", "from shop import pricing\n"),
        ("from . import util", "util", "from shop import pricing\n"),
        (
            "from shop import a, util",
            "util",
            "from shop import a\nfrom shop import pricing\n",
        ),
        ("import shop.util", "shop.util", "import shop.util\nimport shop.pricing\n"),
    ],
)
def test_a_call_through_the_module_follows_the_move(
    temp_repo: Path, imports: str, receiver: str, expected: str
) -> None:
    b = imports + "\n" + CALL.format(receiver=receiver)
    root, store, updater = _shop(temp_repo, b)
    preview = _move(root, store, updater, dry_run=True)
    assert preview.importers == ("shop/a.py", "shop/b.py")
    assert preview.message == cs.MOVE_PLANNED.format(importers=2, unchanged=0)
    report = _move(root, store, updater)
    assert report.applied, report.message
    assert report.verdict is not None and report.verdict.ok
    after = (root / "shop/b.py").read_text()
    new_receiver = "shop.pricing" if receiver == "shop.util" else "pricing"
    assert after == expected + CALL.format(receiver=new_receiver)
    run = _python(root, MAIN)
    assert run.returncode == 0, run.stderr
    assert run.stdout.strip() == "12.0"


GUARD = "\nif __name__ == '__main__':\n    print({receiver}.discounted(10.0))\n"
TOP = "\nTOTAL = {receiver}.discounted(10.0)\n"


@pytest.mark.parametrize(
    ("imports", "receiver", "expected", "body"),
    [
        pytest.param(
            "import shop.util",
            "shop.util",
            "import shop.util\nimport shop.pricing\n",
            TOP,
            id="dotted-assignment",
        ),
        pytest.param(
            "import shop.util",
            "shop.util",
            "import shop.util\nimport shop.pricing\n",
            GUARD,
            id="dotted-main-guard",
        ),
        pytest.param(
            "from shop import util",
            "util",
            "from shop import pricing\n",
            TOP,
            id="from-assignment",
        ),
    ],
)
def test_a_call_at_module_scope_follows_the_move(
    temp_repo: Path, imports: str, receiver: str, expected: str, body: str
) -> None:
    """The graph has no call row for a call made at module scope through
    `shop.util`, so it was neither rewritten nor counted, and the import it
    kept rolled the move back (Greptile TREX)."""
    root, store, updater = _shop(
        temp_repo, imports + "\n" + body.format(receiver=receiver)
    )
    preview = _move(root, store, updater, dry_run=True)
    assert preview.importers == ("shop/a.py", "shop/b.py")
    report = _move(root, store, updater)
    assert report.applied, report.message
    new_receiver = "shop.pricing" if receiver == "shop.util" else "pricing"
    assert (root / "shop/b.py").read_text() == expected + body.format(
        receiver=new_receiver
    )
    probe = (
        "import runpy; runpy.run_module('shop.b', run_name='__main__')"
        if body == GUARD
        else "import shop.b; print(shop.b.TOTAL)"
    )
    run = _python(root, probe)
    assert run.returncode == 0, run.stderr
    assert run.stdout.strip() == "8.0"


def test_two_names_for_the_module_in_one_statement(temp_repo: Path) -> None:
    """Both bindings go in one edit of their shared statement."""
    b = (
        "from shop import util, util as u2\n\n\n"
        "def total():\n    return util.discounted(5.0) + u2.discounted(5.0)\n"
    )
    root, store, updater = _shop(temp_repo, b)
    report = _move(root, store, updater)
    assert report.applied, report.message
    assert (root / "shop/b.py").read_text() == (
        "from shop import pricing\n\n\n"
        "def total():\n    return pricing.discounted(5.0) + pricing.discounted(5.0)\n"
    )
    run = _python(root, MAIN)
    assert run.returncode == 0, run.stderr
    assert run.stdout.strip() == "12.0"


def test_a_module_name_still_used_keeps_its_import(temp_repo: Path) -> None:
    """`util.TAX_RATE` still needs `util`: only the call is rewritten."""
    b = (
        "from shop import util\n\n\n"
        "def total():\n    return util.discounted(10.0) + util.TAX_RATE\n"
    )
    root, store, updater = _shop(temp_repo, b)
    report = _move(root, store, updater, dry_run=True)
    assert "shop/b.py" in report.importers
    assert "+from shop import pricing\n" in report.diff
    assert "+    return pricing.discounted(10.0) + util.TAX_RATE\n" in report.diff
    assert "-from shop import util\n" not in report.diff


@pytest.mark.parametrize(
    ("head", "extra", "kept"),
    [
        pytest.param("__all__ = ['util', 'total']\n", {}, True, id="listed"),
        pytest.param(
            "",
            {"shop/c.py": "from shop.b import util  # noqa: F401\n"},
            True,
            id="taken",
        ),
        pytest.param(
            "", {"shop/c.py": "from shop.b import *  # noqa: F403\n"}, True, id="star"
        ),
        pytest.param("__all__ = ['total']\n", {}, False, id="not-listed"),
    ],
)
def test_a_module_name_the_caller_exports_keeps_its_import(
    temp_repo: Path, head: str, extra: dict[str, str], kept: bool
) -> None:
    """Counting only the file's own uses dropped `util` while `__all__`
    listed it, and `from shop.b import util` elsewhere then failed; the
    contract sees neither (Greptile)."""
    b = "from shop import util\n" + head + CALL.format(receiver="util")
    root, store, updater = _shop(temp_repo, b, **extra)
    report = _move(root, store, updater, dry_run=True)
    assert "+    return pricing.discounted(10.0)\n" in report.diff
    assert ("-from shop import util\n" not in report.diff) == kept


def test_a_name_the_caller_already_uses_is_refused(temp_repo: Path) -> None:
    """Importing the destination as `pricing` would rebind the caller's own
    `pricing`; refusing beats a move that silently changes what it means."""
    b = "from shop import util\n\npricing = 'list'" + CALL.format(receiver="util")
    root, store, updater = _shop(temp_repo, b)
    before = {p: (root / p).read_text() for p in ("shop/util.py", "shop/b.py")}
    message = cs.MOVE_MODULE_NAME_TAKEN.format(
        path="shop/b.py", name="discounted", local="pricing"
    )
    with pytest.raises(MoveRefused) as refused:
        _move(root, store, updater)
    assert str(refused.value) == message
    assert {p: (root / p).read_text() for p in before} == before
    assert not (root / "shop/pricing.py").exists()
    assert load_history(root) == []


def test_a_binding_the_caller_already_has_is_reused(temp_repo: Path) -> None:
    b = "from shop import pricing, util" + CALL.format(receiver="util")
    root, store, updater = _shop(
        temp_repo, b, **{"shop/pricing.py": "def list_price():\n    return 1.0\n"}
    )
    report = _move(root, store, updater)
    assert report.applied, report.message
    assert (root / "shop/b.py").read_text() == (
        "from shop import pricing" + CALL.format(receiver="pricing")
    )
    run = _python(root, MAIN)
    assert run.returncode == 0, run.stderr
    assert run.stdout.strip() == "12.0"


@pytest.mark.parametrize(
    ("source", "found"),
    [
        (b"x = util.discounted(1)", 4),
        (b"x = myutil.discounted(1)", None),
        (b"x = a.util.discounted(1)", None),
        (b"x = _util.discounted(1)", None),
    ],
)
def test_only_the_whole_receiver_is_matched(source: bytes, found: int | None) -> None:
    """`find` alone matched `util.discounted` inside `myutil.discounted`."""
    start = source.index(b"discounted")
    end = source.index(b"(")
    assert (
        Mover._qualified_use_at(source, start, end, b"util.discounted", "discounted")
        == found
    )
