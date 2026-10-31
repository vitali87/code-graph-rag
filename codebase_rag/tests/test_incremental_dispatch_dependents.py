# A fresh index fans a call out to every dispatch target it can reach: a
# `self.do_match()` in a base class reaches each subclass override, and a
# call on a Protocol-typed receiver reaches every structural conformer. An
# incremental sync that ADDS the subclass or the conformer in a new file
# never re-parsed the unchanged callers, so their edges stayed as they were
# until a full re-index: the new file holds no inbound edge for the
# dependents lookup to follow, and the callers record no unresolved
# reference (issue #3338).
from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.tests.test_incremental_added_dependents import (
    _add_after_cache,
    _index,
    _materialise,
    _snapshot,
)
from codebase_rag.tests.test_incremental_deleted_dependents import Snapshot
from evals.cgr_graph import _StatefulIngestor

BASE = """class Transformer:
    def do_match(self, line):
        raise NotImplementedError

    def run(self, line):
        return self.do_match(line)


class Merger(Transformer):
    def do_match(self, line):
        return line
"""

LIB = """from typing import Protocol


class Client(Protocol):
    def get(self, key: str) -> bytes: ...


class Cache:
    def __init__(self, client: Client):
        self.client = client

    def load(self, k):
        return self.client.get(k)
"""

MOCK = 'class Mock:\n    def get(self, key):\n        return b"m"\n'

SPLITTER = """from base import Transformer


class Splitter(Transformer):
    def do_match(self, line):
        return line.split()
"""

REAL = 'class Real:\n    def get(self, k):\n        return b"r"\n'

BEFORE: dict[str, str] = {
    "base.py": BASE,
    "lib.py": LIB,
    "tests/test_lib.py": MOCK,
}

AFTER: dict[str, str] = {**BEFORE, "split.py": SPLITTER, "real.py": REAL}


def _callees(snapshot: Snapshot, caller_suffix: str) -> set[str]:
    return {
        str(edge[4])
        for edge in snapshot[1]
        if edge[2] == cs.RelationshipType.CALLS.value
        and str(edge[1]).endswith(caller_suffix)
    }


def _clean_snapshot(
    temp_repo: Path, fixture: dict[str, str], label: str = "clean"
) -> tuple[Snapshot, _StatefulIngestor]:
    clean_root = temp_repo / label / "proj"
    clean_root.parent.mkdir(parents=True, exist_ok=True)
    _materialise(clean_root, fixture)
    clean_store = _StatefulIngestor()
    _index(clean_store, clean_root, cs.SupportedLanguage.PYTHON, force=True)
    return _snapshot(clean_store), clean_store


def _reparsed_keys_spy(
    monkeypatch: pytest.MonkeyPatch,
) -> list[set[str]]:
    """Capture each run's re-parsed file keys, as the waiter tests do."""
    from codebase_rag.graph_updater import GraphUpdater

    keys: list[set[str]] = []
    original = GraphUpdater._process_files

    def spy(self: GraphUpdater, force: bool = False) -> None:
        original(self, force)
        keys.append(set(self._reparsed_file_keys))

    monkeypatch.setattr(GraphUpdater, "_process_files", spy)
    return keys


def test_incremental_new_subclass_and_conformer_match_fresh_index(
    temp_repo: Path,
) -> None:
    root = temp_repo / "proj"
    _materialise(root, BEFORE)
    store = _StatefulIngestor()
    _index(store, root, cs.SupportedLanguage.PYTHON, force=True)
    _add_after_cache(root, "split.py", SPLITTER)
    _add_after_cache(root, "real.py", REAL)
    _index(store, root, cs.SupportedLanguage.PYTHON, force=False)
    after = _snapshot(store)

    clean, _clean_store = _clean_snapshot(temp_repo, AFTER)

    run_targets = _callees(clean, "Transformer.run")
    assert any(t.endswith("Merger.do_match") for t in run_targets)
    assert any(t.endswith("Splitter.do_match") for t in run_targets)
    load_targets = _callees(clean, "Cache.load")
    assert any(t.endswith("Mock.get") for t in load_targets)
    assert any(t.endswith("Real.get") for t in load_targets)
    assert _callees(after, "Transformer.run") == run_targets
    assert _callees(after, "Cache.load") == load_targets
    assert after == clean


def test_incremental_override_added_to_existing_class_matches_fresh_index(
    temp_repo: Path,
) -> None:
    """An override added to an existing subclass: `Splitter` exists without
    `do_match`, then gains it. The base file is unchanged, so only the
    dispatch-dependent lookup can bring it back for a re-parse."""
    before = {
        **BEFORE,
        "split.py": "from base import Transformer\n\n\nclass Splitter(Transformer):\n    pass\n",
    }
    gained = "from base import Transformer\n\n\nclass Splitter(Transformer):\n    def do_match(self, line):\n        return line.split()\n"
    root = temp_repo / "proj"
    _materialise(root, before)
    store = _StatefulIngestor()
    _index(store, root, cs.SupportedLanguage.PYTHON, force=True)
    _add_after_cache(root, "split.py", gained)
    _index(store, root, cs.SupportedLanguage.PYTHON, force=False)
    after = _snapshot(store)

    clean, _clean_store = _clean_snapshot(temp_repo, {**before, "split.py": gained})

    run_targets = _callees(clean, "Transformer.run")
    assert any(t.endswith("Splitter.do_match") for t in run_targets)
    assert _callees(after, "Transformer.run") == run_targets
    assert after == clean


def test_incremental_conformer_method_added_to_existing_class_matches_fresh_index(
    temp_repo: Path,
) -> None:
    """A conformer method added to an existing class: `Real` exists without
    `get`, then gains it. The Protocol caller's file is unchanged."""
    before = {**BEFORE, "real.py": "class Real:\n    pass\n"}
    gained = 'class Real:\n    def get(self, k):\n        return b"r"\n'
    root = temp_repo / "proj"
    _materialise(root, before)
    store = _StatefulIngestor()
    _index(store, root, cs.SupportedLanguage.PYTHON, force=True)
    _add_after_cache(root, "real.py", gained)
    _index(store, root, cs.SupportedLanguage.PYTHON, force=False)
    after = _snapshot(store)

    clean, _clean_store = _clean_snapshot(temp_repo, {**before, "real.py": gained})

    load_targets = _callees(clean, "Cache.load")
    assert any(t.endswith("Real.get") for t in load_targets)
    assert _callees(after, "Cache.load") == load_targets
    assert after == clean


GRAND_BASE = """class Transformer:
    def do_match(self, line):
        raise NotImplementedError

    def run(self, line):
        return self.do_match(line)


class Merger(Transformer):
    def do_match(self, line):
        return line


class Left(Transformer):
    pass


class Right(Transformer):
    pass
"""

GRANDCHILD = """from base import Merger


class Grandchild(Merger):
    def do_match(self, line):
        return line.upper()
"""

DIAMOND = """from base import Left, Right


class Diamond(Left, Right):
    def do_match(self, line):
        return line[::-1]
"""


def test_incremental_grandchild_and_diamond_overrides_match_fresh_index(
    temp_repo: Path,
) -> None:
    """Dispatch through more than one level of inheritance, and through
    a diamond: the override sits two levels down, and on a class with
    two bases, and the base file is unchanged in both cases."""
    before = {"base.py": GRAND_BASE, "lib.py": LIB, "tests/test_lib.py": MOCK}
    after = {**before, "grand.py": GRANDCHILD, "diamond.py": DIAMOND}
    root = temp_repo / "proj"
    _materialise(root, before)
    store = _StatefulIngestor()
    _index(store, root, cs.SupportedLanguage.PYTHON, force=True)
    _add_after_cache(root, "grand.py", GRANDCHILD)
    _add_after_cache(root, "diamond.py", DIAMOND)
    _index(store, root, cs.SupportedLanguage.PYTHON, force=False)
    snapshot = _snapshot(store)

    clean, _clean_store = _clean_snapshot(temp_repo, after)

    run_targets = _callees(clean, "Transformer.run")
    assert any(t.endswith("Grandchild.do_match") for t in run_targets)
    assert any(t.endswith("Diamond.do_match") for t in run_targets)
    # The diamond contributes exactly one edge to its override: the
    # same call site must not fan out once per base.
    assert sum(1 for t in run_targets if t.endswith("Diamond.do_match")) == 1
    assert _callees(snapshot, "Transformer.run") == run_targets
    assert snapshot == clean


LIB_TWO_METHODS = """from typing import Protocol


class Client(Protocol):
    def get(self, key: str) -> bytes: ...

    def set(self, key: str, value: bytes) -> None: ...


class Cache:
    def __init__(self, client: Client):
        self.client = client

    def load(self, k):
        return self.client.get(k)
"""

MOCK_TWO_METHODS = """class Mock:
    def get(self, key):
        return b"m"

    def set(self, key, value):
        return None
"""

PARTIAL_REAL = 'class Real:\n    def get(self, k):\n        return b"r"\n'
FULL_REAL = """class Real:
    def get(self, k):
        return b"r"

    def set(self, k, value):
        return None
"""


def test_partial_conformer_does_not_queue_caller_until_complete(
    temp_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A class defining only one of a Protocol's methods is not a
    conformer: adding it must not re-parse the caller, and the call
    gains no edge to it. Completing the conformance in a later sync
    queues the caller and the edge appears."""
    before = {
        "base.py": BASE,
        "lib.py": LIB_TWO_METHODS,
        "tests/test_lib.py": MOCK_TWO_METHODS,
    }
    root = temp_repo / "proj"
    _materialise(root, before)
    store = _StatefulIngestor()
    keys = _reparsed_keys_spy(monkeypatch)
    _index(store, root, cs.SupportedLanguage.PYTHON, force=True)
    _add_after_cache(root, "real.py", PARTIAL_REAL)
    _index(store, root, cs.SupportedLanguage.PYTHON, force=False)
    assert "lib.py" not in keys[-1]
    assert "real.py" in keys[-1]
    partial_clean, _store = _clean_snapshot(
        temp_repo, {**before, "real.py": PARTIAL_REAL}, label="clean-partial"
    )
    assert _snapshot(store) == partial_clean
    assert not any(
        t.endswith("Real.get") for t in _callees(_snapshot(store), "Cache.load")
    )

    _add_after_cache(root, "real.py", FULL_REAL)
    _index(store, root, cs.SupportedLanguage.PYTHON, force=False)
    assert "lib.py" in keys[-1]
    full_clean, _store2 = _clean_snapshot(
        temp_repo, {**before, "real.py": FULL_REAL}, label="clean-full"
    )
    assert _snapshot(store) == full_clean
    assert any(t.endswith("Real.get") for t in _callees(_snapshot(store), "Cache.load"))


def test_unrelated_same_named_method_and_third_party_base_queue_nothing(
    temp_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A same-named method on a class that extends nothing in the
    project, and a method on a subclass of a third-party base, change
    no dispatch set: only the added files themselves re-parse."""
    root = temp_repo / "proj"
    _materialise(root, BEFORE)
    store = _StatefulIngestor()
    keys = _reparsed_keys_spy(monkeypatch)
    _index(store, root, cs.SupportedLanguage.PYTHON, force=True)
    unrelated = "class Unrelated:\n    def do_match(self, line):\n        return line\n"
    third_party = "class MyDict(dict):\n    def keys(self):\n        return []\n"
    _add_after_cache(root, "unrelated.py", unrelated)
    _add_after_cache(root, "ext.py", third_party)
    _index(store, root, cs.SupportedLanguage.PYTHON, force=False)
    assert keys[-1] == {"unrelated.py", "ext.py"}
    clean, _clean_store = _clean_snapshot(
        temp_repo, {**BEFORE, "unrelated.py": unrelated, "ext.py": third_party}
    )
    assert _snapshot(store) == clean


def test_comment_only_edit_after_dispatch_fix_keeps_callers_edges(
    temp_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The #3363 parity case: once the dispatch edges exist, a
    comment-only edit to the subclass file leaves its interface
    fingerprint unchanged, so the dispatch lookup finds no change
    and queues nothing. `lib.py` -- which shares no edge with the
    edited file at all -- is not re-parsed and keeps its edges.
    `base.py` IS re-parsed, as before: the edited file imports it,
    and an edge from a re-parsed file into a dependent is one the
    fingerprint skip never restores (issue #2531)."""
    from codebase_rag.graph_updater import GraphUpdater

    root = temp_repo / "proj"
    _materialise(root, BEFORE)
    store = _StatefulIngestor()
    keys = _reparsed_keys_spy(monkeypatch)
    _index(store, root, cs.SupportedLanguage.PYTHON, force=True)
    _add_after_cache(root, "split.py", SPLITTER)
    _add_after_cache(root, "real.py", REAL)
    _index(store, root, cs.SupportedLanguage.PYTHON, force=False)
    assert "base.py" in keys[-1] and "lib.py" in keys[-1]
    commented = SPLITTER + "\n# trailing comment\n"
    _add_after_cache(root, "split.py", commented)
    _index(store, root, cs.SupportedLanguage.PYTHON, force=False)
    assert "lib.py" not in keys[-1]
    assert keys[-1] == {"split.py", "base.py"}
    clean, _clean_store = _clean_snapshot(temp_repo, {**AFTER, "split.py": commented})
    assert _snapshot(store) == clean
    # The dispatch lookup itself is a no-op for the comment edit: the
    # scan matches the graph's definitions exactly.
    probe = GraphUpdater(
        ingestor=store,
        repo_path=root,
        parsers={},
        queries={},
        project_name="proj",
    )
    assert (
        probe._dispatch_dependent_keys([], [("split.py", commented.encode())], []) == []
    )


def test_incremental_dispatch_removal_prunes_old_edges(
    temp_repo: Path,
) -> None:
    """Removing the subclass file and the conformer method prunes the
    dispatch edges the sync had added: no orphaned edge to a removed
    definition survives, and the graph matches a fresh index."""
    root = temp_repo / "proj"
    _materialise(root, AFTER)
    store = _StatefulIngestor()
    _index(store, root, cs.SupportedLanguage.PYTHON, force=True)
    (root / "split.py").unlink()
    _add_after_cache(root, "real.py", "class Real:\n    pass\n")
    _index(store, root, cs.SupportedLanguage.PYTHON, force=False)
    snapshot = _snapshot(store)

    remaining = {**BEFORE, "real.py": "class Real:\n    pass\n"}
    clean, _clean_store = _clean_snapshot(temp_repo, remaining)
    assert not any("Splitter.do_match" in str(edge[4]) for edge in snapshot[1])
    assert not any("Real.get" in str(edge[4]) for edge in snapshot[1])
    assert snapshot == clean


def test_incremental_dispatch_rename_prunes_old_target(
    temp_repo: Path,
) -> None:
    """Renaming the override removes the old dispatch target and adds
    no edge to the new name, which overrides nothing the base declares."""
    root = temp_repo / "proj"
    _materialise(root, AFTER)
    store = _StatefulIngestor()
    _index(store, root, cs.SupportedLanguage.PYTHON, force=True)
    renamed = SPLITTER.replace("def do_match", "def do_other")
    _add_after_cache(root, "split.py", renamed)
    _index(store, root, cs.SupportedLanguage.PYTHON, force=False)
    snapshot = _snapshot(store)

    clean, _clean_store = _clean_snapshot(temp_repo, {**AFTER, "split.py": renamed})
    run_targets = _callees(snapshot, "Transformer.run")
    assert not any(t.endswith("Splitter.do_match") for t in run_targets)
    assert not any(t.endswith("Splitter.do_other") for t in run_targets)
    assert snapshot == clean


def test_scoped_reingest_adds_dispatch_edges(temp_repo: Path) -> None:
    """The watcher/MCP path: creating the subclass and conformer files
    and re-ingesting just them gives the same dispatch edges as a
    fresh index, without a full sync."""
    from codebase_rag.graph_updater import GraphUpdater
    from codebase_rag.parser_loader import load_parsers

    root = temp_repo / "proj"
    _materialise(root, BEFORE)
    store = _StatefulIngestor()
    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=store,
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name="proj",
    )
    updater.run(force=True)
    (root / "split.py").write_text(SPLITTER, encoding="utf-8")
    (root / "real.py").write_text(REAL, encoding="utf-8")
    updater.reingest(["split.py", "real.py"])
    snapshot = _snapshot(store)

    clean, _clean_store = _clean_snapshot(temp_repo, AFTER)
    assert any(
        t.endswith("Splitter.do_match") for t in _callees(snapshot, "Transformer.run")
    )
    assert any(t.endswith("Real.get") for t in _callees(snapshot, "Cache.load"))
    assert snapshot == clean


def test_scoped_reingest_removal_prunes_dispatch_edges(temp_repo: Path) -> None:
    """The watcher/MCP path on a removal: deleting the subclass file
    and re-ingesting it prunes the dispatch edges, matching a fresh
    index of the project without it."""
    from codebase_rag.graph_updater import GraphUpdater
    from codebase_rag.parser_loader import load_parsers

    root = temp_repo / "proj"
    _materialise(root, AFTER)
    store = _StatefulIngestor()
    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=store,
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name="proj",
    )
    updater.run(force=True)
    (root / "split.py").unlink()
    updater.reingest(["split.py"])
    snapshot = _snapshot(store)

    clean, _clean_store = _clean_snapshot(
        temp_repo, {k: v for k, v in AFTER.items() if k != "split.py"}
    )
    assert not any(
        t.endswith("Splitter.do_match") for t in _callees(snapshot, "Transformer.run")
    )
    assert snapshot == clean
