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
    # Second bot review on PR #2764: a later read in the same expression sees
    # what a walrus bound; a walrus in a comprehension reads its loop
    # variables; one in a lambda's body binds the lambda's own name, while a
    # lambda's default runs here.
    "walrus_read_later": 'x = str(len(t := os.getenv("PB_NREAD"))) + t\nprint(x)',
    "walrus_in_comprehension": (
        'secret = os.getenv("PB_COMP")\nout = [(last := c) for c in secret]\n'
        "print(last)"
    ),
    "walrus_in_lambda": (
        't = os.getenv("PB_LAMBDA")\nf = lambda: (t := "safe")\nprint(t)'
    ),
    "walrus_in_lambda_default": (
        'f = lambda a=(t := os.getenv("PB_LDEFAULT")): a\nprint(t)'
    ),
    "comprehension_clean_walrus": (
        's = os.getenv("PB_COMPCLEAN")\nout = [(last := 1) for c in s]\nprint(last)'
    ),
    # Third bot review on PR #2764: every place Python may skip a walrus
    # keeps the old value too. A chained comparison stops at its first false
    # link, an assert's message runs only when the test fails, and a failed
    # guard leaves what it bound to the arms after it. The first two
    # comparison operands and a def's default always run.
    "walrus_late_comparison": (
        'token = os.getenv("PB_CMP")\n0 > 1 > (token := "safe")\nprint(token)'
    ),
    "walrus_after_not_in": (
        'token = os.getenv("PB_CMPIN")\n0 in [] not in (token := "safe")\nprint(token)'
    ),
    "walrus_first_comparison_link": (
        'token = os.getenv("PB_CMPKILL")\n0 < (token := "safe") < 1\nprint(token)'
    ),
    "walrus_in_comprehension_filter": (
        'last = os.getenv("PB_COMPIF")\nout = [c for c in "" if (last := "safe")]\n'
        "print(last)"
    ),
    "walrus_in_assert": (
        'token = os.getenv("PB_ASSERT")\nassert len("") == 0, (token := "safe")\n'
        "print(token)"
    ),
    "walrus_in_failed_guard": (
        'match len(""):\n    case 0 if (t := os.getenv("PB_GUARD")) == "":\n'
        "        pass\n    case _:\n        print(t)"
    ),
    "walrus_in_skipped_guard": (
        'token = os.getenv("PB_GUARDKEEP")\nmatch len(""):\n'
        '    case 1 if (token := "safe"):\n        pass\n    case _:\n        pass\n'
        "print(token)"
    ),
    "walrus_in_def_default": (
        'def g(a=(t := os.getenv("PB_DEF"))):\n    pass\nprint(t)'
    ),
    "walrus_kill_in_def_default": (
        'token = os.getenv("PB_DEFKILL")\ndef g(a=(token := "safe")):\n    pass\n'
        "print(token)"
    ),
    "walrus_in_skipped_lambda_default": (
        'token = os.getenv("PB_LDSHORT")\n'
        'f = len("") and (lambda a=(token := "safe"): a)\nprint(token)'
    ),
    # A local variable's annotation never runs, but an annotated subscript
    # target's parts do.
    "local_annotation": 'x: print(os.getenv("PB_LOCALANN")) = 1',
    "local_annotated_subscript": 'd = {}\nd[print(os.getenv("PB_LOCALTARGET"))]: int',
    # Fourth bot review on PR #2764: each operand is read where Python
    # evaluates it, after the walrus bindings before it and not after the
    # ones that follow, in a call's arguments, a returned value, `+=` and an
    # assignment alike.
    "walrus_read_later_in_call": 'print(len(t := os.getenv("PB_ARGREAD")), t)',
    "call_arg_before_walrus": 't = os.getenv("PB_ARGBEFORE")\nprint(t, (t := "safe"))',
    "walrus_rebinds_before_call_arg": (
        't = os.getenv("PB_ARGSTALE")\nprint((t := "safe"), t)'
    ),
    "returns_walrus_read_later": 'return str(len(t := os.getenv("PB_RETREAD"))) + t',
    "prints_walrus_read_later": "print(returns_walrus_read_later())",
    "returns_rebound_name": 't = os.getenv("PB_RETSTALE")\nreturn (t := "safe") + t',
    "prints_rebound_name": "print(returns_rebound_name())",
    "walrus_read_later_in_augmented": (
        'x = ""\nx += str(len(t := os.getenv("PB_AUGREAD"))) + t\nprint(x)'
    ),
    "augmented_target_read_first": (
        'x = os.getenv("PB_AUGLHS")\nx += (x := "safe")\nprint(x)'
    ),
    "walrus_rebinds_before_operand": (
        't = os.getenv("PB_STALE")\nx = (t := "safe") + t\nprint(x)'
    ),
    # A starred comprehension target binds its names, and a filter sees only
    # the `for` clauses before it.
    "starred_comprehension_target": (
        'pairs = os.getenv("PB_STARFOR")\n'
        "out = [(last := rest) for *rest, tail in pairs]\nprint(last)"
    ),
    "walrus_between_for_clauses": (
        's = os.getenv("PB_COMPORDER")\n'
        "out = [0 for c in [s] if (last := c) for c in [1]]\nprint(last)"
    ),
    # Fifth bot review on PR #2764: `a if c else b` runs `c` first, and a
    # comprehension runs where it stands, its clauses before its body, so a
    # walrus later in the statement does not change what it read.
    "walrus_in_condition_read_by_arm": (
        'x = t if (t := os.getenv("PB_COND")) else ""\nprint(x)'
    ),
    "walrus_in_condition_read_in_call": (
        'print(t if (t := os.getenv("PB_CONDCALL")) else "")'
    ),
    "walrus_in_condition_kills_arm": (
        't = os.getenv("PB_CONDKILL")\nx = t if (t := "safe") else ""\nprint(x)'
    ),
    "comprehension_iterable_before_walrus": (
        's = os.getenv("PB_SAVED")\nout, unused = [c for c in s], (s := "")\nprint(out)'
    ),
    "comprehension_body_before_walrus": (
        't = os.getenv("PB_COMPOUTER")\nout, unused = [t for c in "ab"], (t := "")\n'
        "print(out)"
    ),
    "comprehension_filter_before_body": (
        's = os.getenv("PB_FILTERBODY")\nout = [t for c in s if (t := c)]\nprint(out)'
    ),
}

# Module-level annotations run, after the target is bound, and so do a
# def's parameter annotations; a string annotation, one under `if
# TYPE_CHECKING:` and a `type` alias's value never do.
MODULE_LEVEL = (
    'bare: print(os.getenv("PB_MODANN"))\n'
    'valued: print(os.getenv("PB_MODANNV")) = 1\n'
    'late: print(late) = os.getenv("PB_MODLATE")\n'
    "table = {}\n"
    'table[print(os.getenv("PB_MODTARGET"))]: int\n'
    'def annotated(a: print(os.getenv("PB_PARAMANN"))):\n    pass\n'
    "quoted: \"print(os.getenv('PB_STRANN'))\" = 1\n"
    "from typing import TYPE_CHECKING\n"
    'if TYPE_CHECKING:\n    checked: print(os.getenv("PB_TCANN"))\n'
    'type Alias = print(os.getenv("PB_ALIAS"))\n'
)

# Under `from __future__ import annotations` (PEP 563) no annotation runs;
# a default value still does.
DEFERRED_MODULE = (
    '"""Annotations stay strings here."""\n'
    "from __future__ import annotations\n\n"
    "import os\n\n"
    'lazy: print(os.getenv("PB_FUTANN")) = 1\n\n\n'
    "def typed(\n"
    '    a: print(os.getenv("PB_FUTPARAM")) = print(os.getenv("PB_FUTDEFAULT")),\n'
    ') -> print(os.getenv("PB_FUTRET")):\n'
    "    pass\n"
)


def _source(functions: dict[str, str], module_level: str) -> str:
    out = ["import os", ""]
    for name, body in functions.items():
        indented = "\n".join(f"    {line}" for line in body.splitlines())
        out += ["", f"def {name}():", indented, ""]
    out += ["", module_level]
    return "\n".join(out)


@pytest.fixture(scope="module")
def flows(tmp_path_factory: pytest.TempPathFactory) -> set[tuple[str, str]]:
    root = tmp_path_factory.mktemp("pybind")
    (root / "binds.py").write_text(_source(FUNCTIONS, MODULE_LEVEL), encoding="utf-8")
    (root / "deferred.py").write_text(DEFERRED_MODULE, encoding="utf-8")
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
        "PB_NREAD",
        "PB_COMP",
        "PB_LAMBDA",
        "PB_LDEFAULT",
        "PB_CMP",
        "PB_CMPIN",
        "PB_COMPIF",
        "PB_ASSERT",
        "PB_GUARD",
        "PB_GUARDKEEP",
        "PB_DEF",
        "PB_LDSHORT",
        "PB_ARGREAD",
        "PB_ARGBEFORE",
        "PB_RETREAD",
        "PB_AUGREAD",
        "PB_AUGLHS",
        "PB_STARFOR",
        "PB_COMPORDER",
        "PB_COND",
        "PB_CONDCALL",
        "PB_SAVED",
        "PB_COMPOUTER",
        "PB_FILTERBODY",
    ],
)
def test_a_name_bound_by_unpacking_or_walrus_carries_its_value(
    flows: set[tuple[str, str]], var: str
) -> None:
    assert _leaks(flows, var)


@pytest.mark.parametrize(
    "var",
    [
        "PB_KILL",
        "PB_WKILL",
        "PB_SWAPK",
        "PB_LEFTOP",
        "PB_WAFTER",
        "PB_COMPCLEAN",
        "PB_CMPKILL",
        "PB_DEFKILL",
        "PB_ARGSTALE",
        "PB_RETSTALE",
        "PB_STALE",
        "PB_CONDKILL",
    ],
)
def test_a_name_rebound_to_a_clean_value_is_clean(
    flows: set[tuple[str, str]], var: str
) -> None:
    assert not _leaks(flows, var)


@pytest.mark.parametrize(
    "var",
    [
        "PB_MODANN",
        "PB_MODANNV",
        "PB_MODLATE",
        "PB_MODTARGET",
        "PB_LOCALTARGET",
        "PB_PARAMANN",
        "PB_FUTDEFAULT",
    ],
)
def test_an_annotated_assignment_runs_its_annotation_and_target(
    flows: set[tuple[str, str]], var: str
) -> None:
    assert _leaks(flows, var)


@pytest.mark.parametrize(
    "var",
    [
        "PB_LOCALANN",
        "PB_STRANN",
        "PB_TCANN",
        "PB_ALIAS",
        "PB_FUTANN",
        "PB_FUTPARAM",
        "PB_FUTRET",
    ],
)
def test_an_annotation_python_never_runs_does_not_flow(
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
