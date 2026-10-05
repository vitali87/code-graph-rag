# Issue #2754: a Python name bound by tuple/list unpacking or by the walrus
# operator takes the taint of the value it is bound to, as `a = value` does.
# The flow walk bound only an identifier target, so `user, pw = getenv(..),
# getenv(..)` and `if (tok := getenv(..))` recorded READS_FROM and WRITES_TO
# but no ENV -> STDOUT flow.
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag.capture import resolve_capture
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers

FLOWS_TO = cs.RelationshipType.FLOWS_TO.value
_CAPTURE_IO = resolve_capture([cs.CaptureGroup.IO.value])
STDOUT = "resource::STDOUT::<dynamic>"

FUNCTIONS = {
    "tuple_unpack": (
        'user, password = os.getenv("PB_USER"), os.getenv("PB_PASS")\nprint(password)'
    ),
    "paren_unpack": '(a, b) = os.getenv("PB_PAREN"), 1\nprint(a)',
    "list_unpack": '[a, b] = [os.getenv("PB_LIST"), 1]\nprint(a)',
    "starred": 'first, *rest = os.getenv("PB_STAR"), 1, 2\nprint(first)',
    "starred_rest": 'first, *rest = 1, os.getenv("PB_REST"), 2\nprint(rest)',
    "starred_last": 'head, *mid, last = 1, 2, 3, os.getenv("PB_LAST")\nprint(last)',
    "nested": 'a, (b, c) = 1, (2, os.getenv("PB_NEST"))\nprint(c)',
    "from_value": 'pair = os.getenv("PB_PAIR")\na, b = pair\nprint(b)',
    "for_tuple": 'x, y = os.getenv("PB_FOR"), 0\nfor _ in range(2):\n    print(x)',
    "walrus": 'if (tok := os.getenv("PB_WALRUS")) is not None:\n    print(tok)',
    "walrus_while": 'while (line := os.getenv("PB_WHILE")):\n    print(line)',
    "walrus_arg": 'print(t := os.getenv("PB_WARG"))',
    "walrus_later": 'n = len(v := os.getenv("PB_WLATER"))\nprint(v)',
    # Every target of a chain is bound, not only the last.
    "chained": 'a = b = os.getenv("PB_CHAIN")\nprint(a)',
    # Elements are read before any target is bound.
    "swap": 's = os.getenv("PB_SWAP")\nc = "x"\ns, c = c, s\nprint(c)',
    "swap_kill": 'k = os.getenv("PB_SWAPK")\nc = "x"\nk, c = c, k\nprint(k)',
    # Controls that already worked.
    "plain": 'v = os.getenv("PB_PLAIN")\nprint(v)',
    "chained_last": 'a = b = os.getenv("PB_CHAIN_LAST")\nprint(b)',
    # Unpacking also kills: a name rebound to a clean element is clean.
    "kill_by_unpack": 't = os.getenv("PB_KILL")\nt, u = "safe", 1\nprint(t)',
    "kill_by_walrus": 't = os.getenv("PB_WKILL")\nif (t := "safe"):\n    print(t)',
    # The other element's source never reaches the sink.
    "pairwise": 'x, y = os.getenv("PB_X"), os.getenv("PB_Y")\nprint(y)',
    "clean_unpack": "a, b = 1, 2\nprint(a)",
    # Bot review on PR #2764: Python evaluates the value once, left to right
    # with its calls and walrus bindings, and only then binds the targets; a
    # walrus Python may skip binds only on the path that runs it.
    "walrus_short_circuit": (
        'token = os.getenv("PB_SHORT")\nflag = len("")\n'
        'if flag and (token := "safe"):\n    pass\nprint(token)'
    ),
    "walrus_arm": (
        'token = os.getenv("PB_ARM")\nv = (token := "safe") if len("") else 0\n'
        "print(token)"
    ),
    "chain_swap": (
        'a = os.getenv("PB_CHSWAP")\nb = "safe"\na, b = b, a = b, a\nprint(a)'
    ),
    "value_splat": '*rest, tail = [*os.getenv("PB_SPLAT"), "safe"]\nprint(rest)',
    "walrus_then_read": 'x, y = (t := os.getenv("PB_WREAD")), t\nprint(y)',
    "call_before_unpack": 'x = os.getenv("PB_CALLU")\nx, unused = print(x), 1',
    "call_in_walrus": 'x = os.getenv("PB_CALLW")\n(x := print(x))',
    "call_before_assign": 'x = os.getenv("PB_CALLA")\nx = print(x)',
    # A walrus Python always runs binds strongly, and a target bound after a
    # walrus in the value replaces what the walrus bound.
    "walrus_left_operand": (
        'token = os.getenv("PB_LEFTOP")\n'
        'if (token := "safe") and len(""):\n    pass\nprint(token)'
    ),
    "walrus_then_target": 't, y = "safe", (t := os.getenv("PB_WAFTER"))\nprint(t)',
}


def _source(functions: dict[str, str]) -> str:
    out = ["import os", ""]
    for name, body in functions.items():
        indented = "\n".join(f"    {line}" for line in body.splitlines())
        out += ["", f"def {name}():", indented, ""]
    return "\n".join(out)


@pytest.fixture(scope="module")
def flows(tmp_path_factory: pytest.TempPathFactory) -> set[tuple[str, str]]:
    root = tmp_path_factory.mktemp("pybind")
    (root / "binds.py").write_text(_source(FUNCTIONS), encoding="utf-8")
    parsers, queries = load_parsers()
    mock = MagicMock()
    GraphUpdater(
        ingestor=mock,
        repo_path=root,
        parsers=parsers,
        queries=queries,
        capture=_CAPTURE_IO,
    ).run()
    return {
        (c.args[0][2], c.args[2][2])
        for c in mock.ensure_relationship_batch.call_args_list
        if str(c.args[1]) == FLOWS_TO
    }


def _leaks(flows: set[tuple[str, str]], var: str) -> bool:
    return (f"resource::ENV::{var}", STDOUT) in flows


@pytest.mark.parametrize(
    "var",
    [
        "PB_PASS",
        "PB_PAREN",
        "PB_LIST",
        "PB_STAR",
        "PB_REST",
        "PB_LAST",
        "PB_NEST",
        "PB_PAIR",
        "PB_FOR",
        "PB_WALRUS",
        "PB_WHILE",
        "PB_WARG",
        "PB_WLATER",
        "PB_CHAIN",
        "PB_SWAP",
        "PB_SHORT",
        "PB_ARM",
        "PB_CHSWAP",
        "PB_SPLAT",
        "PB_WREAD",
        "PB_CALLU",
        "PB_CALLW",
        "PB_CALLA",
    ],
)
def test_a_name_bound_by_unpacking_or_walrus_carries_its_value(
    flows: set[tuple[str, str]], var: str
) -> None:
    assert _leaks(flows, var)


@pytest.mark.parametrize(
    "var", ["PB_KILL", "PB_WKILL", "PB_SWAPK", "PB_LEFTOP", "PB_WAFTER"]
)
def test_a_name_rebound_to_a_clean_value_is_clean(
    flows: set[tuple[str, str]], var: str
) -> None:
    assert not _leaks(flows, var)


# Negative: what must not change.


@pytest.mark.parametrize("var", ["PB_PLAIN", "PB_CHAIN_LAST"])
def test_plain_and_chained_assignments_still_flow(
    flows: set[tuple[str, str]], var: str
) -> None:
    assert _leaks(flows, var)


@pytest.mark.parametrize("var", ["PB_USER", "PB_X"])
def test_the_element_bound_to_another_name_does_not_flow(
    flows: set[tuple[str, str]], var: str
) -> None:
    assert not _leaks(flows, var)
