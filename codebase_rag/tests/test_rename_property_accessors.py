"""Issue #2667: renaming either accessor of a Python property renames the property.

A property with a setter is two methods with one name, `Account.balance` and
`Account.balance@10`. `cgr rename` only followed OVERRIDES edges, so the plan
for the getter missed the setter's `def` and its `@balance.setter` decorator:
the rename was refused, or with `--allow-heuristic` rewritten half way and
rolled back. Starting from the setter renamed the setter alone, which made a
second property and left `balance` read-only.
"""

from __future__ import annotations

import importlib.util
import sys
import uuid
from pathlib import Path
from types import ModuleType

import pytest

from codebase_rag import constants as cs
from codebase_rag.editing.rename import (
    QueryFn,
    RenameRefused,
    rename,
    renamed_qualified_name,
)
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.types_defs import PropertyParams, ResultRow
from evals.cgr_graph import _StatefulIngestor

PROJECT = "propproj"
GETTER = f"{PROJECT}.bank.Account.balance"
SETTER = f"{PROJECT}.bank.Account.balance@10"

BANK = """class Account:
    def __init__(self, amount):
        self._amount = amount

    @property
    def balance(self):
        return self._amount

    @balance.setter
    def balance(self, v):
        if v < 0:
            raise ValueError("negative")
        self._amount = v


def audit(acct: Account):
    return acct.balance
"""

RENAMED = """class Account:
    def __init__(self, amount):
        self._amount = amount

    @property
    def funds(self):
        return self._amount

    @funds.setter
    def funds(self, v):
        if v < 0:
            raise ValueError("negative")
        self._amount = v


def audit(acct: Account):
    return acct.funds
"""


def _query(store: _StatefulIngestor) -> QueryFn:
    def fetch_all(query: str, params: PropertyParams | None) -> list[ResultRow]:
        return store.fetch_all(query, None if params is None else dict(params))

    return fetch_all


def _indexed(
    root: Path, files: dict[str, str]
) -> tuple[_StatefulIngestor, GraphUpdater]:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    parsers, queries = load_parsers()
    store = _StatefulIngestor()
    updater = GraphUpdater(
        ingestor=store,
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name=PROJECT,
    )
    updater.run(force=True)
    return store, updater


def _methods(store: _StatefulIngestor) -> set[str]:
    return {str(qn) for label, qn in store.nodes if label == cs.NodeLabel.METHOD.value}


def _load(path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"_bank_{uuid.uuid4().hex}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        # The module object stays usable; the registry keeps nothing.
        sys.modules.pop(spec.name, None)
    return module


@pytest.fixture
def bank(tmp_path: Path) -> tuple[Path, _StatefulIngestor, GraphUpdater]:
    root = tmp_path / PROJECT
    root.mkdir()
    store, updater = _indexed(root, {"bank.py": BANK})
    assert {GETTER, SETTER} <= _methods(store)
    return root, store, updater


@pytest.mark.parametrize("start", [GETTER, SETTER], ids=["from-getter", "from-setter"])
def test_renaming_an_accessor_renames_the_whole_property(
    bank: tuple[Path, _StatefulIngestor, GraphUpdater], start: str
) -> None:
    root, store, updater = bank

    report = rename(
        root,
        _query(store),
        PROJECT,
        start,
        "funds",
        allow_heuristic=True,
        reingest=updater.reingest,
    )

    assert report.applied, report.message
    assert report.verdict is not None and report.verdict.ok, report.verdict
    assert (root / "bank.py").read_text(encoding="utf-8") == RENAMED
    methods = _methods(store)
    assert {
        f"{PROJECT}.bank.Account.funds",
        f"{PROJECT}.bank.Account.funds@10",
    } <= methods
    assert not {GETTER, SETTER} & methods


def test_the_renamed_property_still_reads_and_writes(
    bank: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    root, store, updater = bank
    rename(
        root,
        _query(store),
        PROJECT,
        GETTER,
        "funds",
        allow_heuristic=True,
        reingest=updater.reingest,
    )

    module = _load(root / "bank.py")
    account = module.Account(3)
    account.funds = 7
    assert module.audit(account) == 7
    with pytest.raises(ValueError, match="negative"):
        account.funds = -1


def test_the_plan_names_both_accessors_and_the_decorator(
    bank: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    root, store, _updater = bank

    report = rename(
        root,
        _query(store),
        PROJECT,
        SETTER,
        "funds",
        allow_heuristic=True,
        dry_run=True,
    )

    assert set(report.hierarchy) == {GETTER, SETTER}
    assert ("decorator", "bank.py", 9, 5) in {
        (s.kind, s.path, s.line, s.col) for s in report.sites
    }
    assert (root / "bank.py").read_text(encoding="utf-8") == BANK


@pytest.mark.parametrize(
    ("member", "expected"),
    [
        ("p.bank.Account.balance@10", "p.bank.Account.funds@10"),
        ("p.bank.Account.balance@10_4", "p.bank.Account.funds@10_4"),
    ],
)
def test_the_expected_name_keeps_the_duplicate_marker(
    member: str, expected: str
) -> None:
    assert renamed_qualified_name(member, "funds") == expected


WITH_DELETER = """class Account:
    @property
    def balance(self):
        return self._amount

    @balance.setter
    def balance(self, v):
        self._amount = v

    @balance.deleter
    def balance(self):
        del self._amount
"""


# Bot review on PR #2725: a plain `def` of the property's name is not one of
# its accessors, before or after it, and the renamed accessors take the
# names the re-index gives them.
PLAIN_AFTER = """class Account:
    @property
    def balance(self):
        return 1

    @balance.setter
    def balance(self, v):
        pass

    def balance(self):
        return 2
"""

PLAIN_BEFORE = """class Account:
    def balance(self):
        return 2

    @property
    def balance(self):
        return 1

    @balance.setter
    def balance(self, v):
        pass
"""


def test_a_plain_def_before_the_accessors_is_not_one_of_them(tmp_path: Path) -> None:
    root = tmp_path / PROJECT
    root.mkdir()
    store, updater = _indexed(root, {"bank.py": PLAIN_BEFORE})

    report = rename(
        root,
        _query(store),
        PROJECT,
        f"{PROJECT}.bank.Account.balance@6",
        "funds",
        allow_heuristic=True,
        reingest=updater.reingest,
    )

    assert set(report.hierarchy) == {
        f"{PROJECT}.bank.Account.balance@6",
        f"{PROJECT}.bank.Account.balance@10",
    }
    assert report.applied, report.message
    assert report.verdict is not None and report.verdict.ok, report.verdict
    text = (root / "bank.py").read_text(encoding="utf-8")
    assert text == PLAIN_BEFORE.replace("balance", "funds").replace(
        "def funds(self):\n        return 2", "def balance(self):\n        return 2"
    )
    assert {
        f"{PROJECT}.bank.Account.balance",
        f"{PROJECT}.bank.Account.funds",
        f"{PROJECT}.bank.Account.funds@10",
    } <= _methods(store)


def test_a_plain_def_after_the_accessors_refuses_the_rename(tmp_path: Path) -> None:
    # That `def` replaces the property, and would take its plain name.
    root = tmp_path / PROJECT
    root.mkdir()
    store, _updater = _indexed(root, {"bank.py": PLAIN_AFTER})

    with pytest.raises(RenameRefused, match="redefined by a plain def on line 10"):
        rename(
            root,
            _query(store),
            PROJECT,
            f"{PROJECT}.bank.Account.balance@7",
            "funds",
            allow_heuristic=True,
        )

    assert (root / "bank.py").read_text(encoding="utf-8") == PLAIN_AFTER


def test_a_deleter_is_renamed_with_the_property(tmp_path: Path) -> None:
    root = tmp_path / PROJECT
    root.mkdir()
    store, updater = _indexed(root, {"bank.py": WITH_DELETER})
    deleter = f"{PROJECT}.bank.Account.balance@11"
    assert deleter in _methods(store)

    report = rename(
        root,
        _query(store),
        PROJECT,
        deleter,
        "funds",
        allow_heuristic=True,
        reingest=updater.reingest,
    )

    assert report.applied, report.message
    assert (root / "bank.py").read_text(encoding="utf-8") == WITH_DELETER.replace(
        "balance", "funds"
    )


# Negative: what must not change.


GETTER_ONLY = """class Account:
    def __init__(self, amount):
        self._amount = amount

    @property
    def balance(self):
        return self._amount


def audit(acct: Account):
    return acct.balance
"""

REDEFINED = """class Account:
    def balance(self):
        return 1

    def balance(self):
        return 2
"""


def test_a_read_only_property_is_renamed_as_before(tmp_path: Path) -> None:
    root = tmp_path / PROJECT
    root.mkdir()
    store, updater = _indexed(root, {"bank.py": GETTER_ONLY})

    report = rename(
        root,
        _query(store),
        PROJECT,
        GETTER,
        "funds",
        allow_heuristic=True,
        reingest=updater.reingest,
    )

    assert report.applied, report.message
    assert report.hierarchy == (GETTER,)
    assert "def funds(self):" in (root / "bank.py").read_text(encoding="utf-8")


def test_a_redefinition_without_accessor_decorators_is_not_pulled_in(
    tmp_path: Path,
) -> None:
    root = tmp_path / PROJECT
    root.mkdir()
    store, _updater = _indexed(root, {"bank.py": REDEFINED})
    twins = sorted(q for q in _methods(store) if ".balance" in q)
    assert len(twins) == 2

    report = rename(
        root,
        _query(store),
        PROJECT,
        twins[0],
        "total",
        allow_heuristic=True,
        dry_run=True,
    )

    assert report.hierarchy == (twins[0],)


def test_without_allow_heuristic_the_overload_access_still_refuses(
    bank: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    root, store, _updater = bank

    with pytest.raises(RenameRefused):
        rename(root, _query(store), PROJECT, GETTER, "funds", dry_run=True)

    assert (root / "bank.py").read_text(encoding="utf-8") == BANK


@pytest.mark.parametrize(
    ("member", "expected"),
    [
        ("p.pkg.util.helper", "p.pkg.util.assist"),
        ("p.bank.Account.balance", "p.bank.Account.assist"),
    ],
)
def test_a_name_without_a_marker_is_renamed_as_before(
    member: str, expected: str
) -> None:
    assert renamed_qualified_name(member, "assist") == expected
