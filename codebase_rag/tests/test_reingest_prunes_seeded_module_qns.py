"""`reingest` must prune the seeded module-qn map, like `run()` does (#1712).

`_prune_stale_seeded_module_qns` was reachable only from `run()`. An MCP
session that indexes once and then only makes scoped re-ingest calls holds a
retained updater that never re-enters `run()`, so a qn whose Module another
writer deleted stayed in `module_qn_to_file_path` with a real path -- still
offered by `known_module_paths()`, and still winning the Rust sub-scope
ownership arbitration against the file that legitimately owns the scope.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers


class BufferingIngestor:
    """A sink whose reads see only FLUSHED writes, as production does.

    The test needs this rather than the suite's write-through fake. The
    prune exempts a file this operation re-parsed precisely because its
    Module node may not be flushed yet, so a sink that makes a just-written
    node instantly readable means the exemption never has to do anything --
    the very case it exists for is invisible, and a prune that ignored the
    exemption entirely would pass.
    """

    def __init__(self) -> None:
        self.flushed: dict[str, str] = {}  # qn -> path
        self.pending: dict[str, str] = {}
        self.writes: list[tuple[str, dict[str, Any]]] = []

    # -- the QueryProtocol surface the prune uses -------------------------
    def fetch_all(self, query: str, params: dict | None = None) -> list[dict]:
        if "MATCH (m:Module)" in query:
            return [
                {cs.KEY_QUALIFIED_NAME: qn, cs.KEY_PATH: p}
                for qn, p in self.flushed.items()
            ]
        return []

    def execute_write(self, query: str, params: dict | None = None) -> None:
        # Declared explicitly, not via __getattr__: `QueryProtocol` is a
        # runtime-checkable Protocol and `isinstance` inspects the CLASS, so a
        # method supplied by __getattr__ does not satisfy it. Without this the
        # prune's `isinstance(self.ingestor, QueryProtocol)` guard returns
        # early and the whole fix appears not to work.
        return None

    def ensure_node_batch(self, label: Any, props: dict[str, Any]) -> None:
        self.writes.append((str(label), props))
        if str(label).endswith("MODULE") or str(label) == "Module":
            qn = props.get("qualified_name")
            if isinstance(qn, str):
                self.pending[qn] = str(props.get("path", ""))

    def flush_all(self) -> None:
        self.flushed.update(self.pending)
        self.pending.clear()

    def __getattr__(self, name: str) -> Any:  # every other sink call is a no-op
        def _noop(*_a: Any, **_k: Any) -> None:
            return None

        return _noop


def test_the_fake_satisfies_the_protocol_the_prune_gates_on() -> None:
    """`_prune_stale_seeded_module_qns` returns early unless this holds.

    `QueryProtocol` is runtime-checkable, so `isinstance` inspects the CLASS:
    a method supplied by `__getattr__` does not satisfy it. A fake missing
    `execute_write` makes the prune a silent no-op and every assertion below
    fails for a reason that has nothing to do with the fix -- measured, and
    it cost a full mutation matrix before it was found.
    """
    from codebase_rag.services import QueryProtocol

    assert isinstance(BufferingIngestor(), QueryProtocol), (
        "the fake does not satisfy QueryProtocol, so the prune's isinstance "
        "guard returns early and nothing under test ever runs"
    )


def test_the_buffering_fake_actually_buffers() -> None:
    """Validate the instrument before trusting a result that depends on it.

    If `fetch_all` saw pending writes, this fake would be write-through and
    every assertion below about the exemption would pass for free.
    """
    ing = BufferingIngestor()
    ing.ensure_node_batch(
        cs.NodeLabel.MODULE, {"qualified_name": "p.m", "path": "m.py"}
    )
    assert ing.fetch_all("MATCH (m:Module) RETURN m.path AS path") == [], (
        "the fake is write-through: a pending module is visible before flush, "
        "so the unflushed case this suite tests cannot occur"
    )
    ing.flush_all()
    assert [r[cs.KEY_QUALIFIED_NAME] for r in ing.fetch_all("MATCH (m:Module) x")] == [
        "p.m"
    ], "flush_all did not move pending writes into the readable set"


def _updater(repo: Path, ingestor: BufferingIngestor) -> GraphUpdater:
    parsers, queries = load_parsers()
    return GraphUpdater(
        ingestor=ingestor,  # type: ignore[arg-type]
        repo_path=repo,
        parsers=parsers,
        queries=queries,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    root.mkdir()
    (root / "a.py").write_text("def a():\n    pass\n", encoding="utf-8")
    (root / "b.py").write_text("def b():\n    pass\n", encoding="utf-8")
    return root


def test_reingest_drops_a_qn_whose_module_another_writer_deleted(repo: Path) -> None:
    """The defect: a retained updater never re-enters `run()`, so never prunes.

    A second writer (another updater, a delete_project then re-index, a run in
    another clone) removes a Module from the graph. The qn stays in this
    updater's seeded map with a REAL path, so `known_module_paths()` keeps
    offering it.
    """
    ing = BufferingIngestor()
    updater = _updater(repo, ing)
    updater.run()
    ing.flush_all()

    dp = updater.factory.definition_processor
    victim = next(qn for qn in dp.module_qn_to_file_path if qn.endswith(".b"))
    assert victim in dp.module_qn_to_file_path, "fixture guard: b must be mapped"

    # Another writer deletes b's Module from the graph. The map still holds it.
    del ing.flushed[victim]

    updater.reingest(["a.py"])

    assert victim not in dp.module_qn_to_file_path, (
        "a scoped reingest left a qn in the seeded map whose Module the graph "
        "no longer holds, so known_module_paths() keeps offering it with a "
        "real path"
    )


def test_a_file_this_reingest_reparsed_survives_its_own_unflushed_write(
    repo: Path,
) -> None:
    """The exemption, and why the fake must buffer.

    A re-parsed file writes its own map entry, and its Module node may still
    be unflushed when the prune reads. Dropping it would delete the entry the
    operation just created. With a write-through sink the module is instantly
    readable and this case cannot arise -- which is why the fake buffers.
    """
    ing = BufferingIngestor()
    updater = _updater(repo, ing)
    updater.run()
    ing.flush_all()

    dp = updater.factory.definition_processor
    target = next(qn for qn in dp.module_qn_to_file_path if qn.endswith(".a"))

    # a.py's Module is absent from the readable set for the whole reingest:
    # exactly the unflushed shape, since the re-parse rewrites it as pending.
    del ing.flushed[target]

    updater.reingest(["a.py"])

    assert target in dp.module_qn_to_file_path, (
        "the prune dropped the entry for the file this reingest re-parsed, "
        "whose Module node is still unflushed"
    )


def test_the_prune_keeps_working_across_repeated_reingest_calls(repo: Path) -> None:
    """The accumulation case, which a single-call test cannot see.

    `_parsed_files` accumulates across reingest calls (deliberately --
    `_hydrate_for_reingest` reads it as a liveness proxy). So a fix that
    reused it as the exemption exempts everything the process ever parsed.

    Measured, this does NOT catch anything the first test misses: the naive
    mutation reddens both, because that test's `run()` parses b.py into
    `_parsed_files` and the stale exemption covers it there too. Both fail
    for one reason -- an exemption left by an EARLIER operation -- so this is
    a second instance of that reason, not an independent axis.

    Kept anyway, for a reason worth stating rather than pretending it
    discriminates: it pins the multi-call shape the issue is actually about
    (an MCP `_live_updater` making repeated scoped calls), which the first
    test reaches only incidentally through `run()`. If the first test were
    ever rewritten to seed the map without a full run, it would stop covering
    accumulation and this one would be the only thing left holding it.
    """
    ing = BufferingIngestor()
    updater = _updater(repo, ing)
    updater.run()
    ing.flush_all()
    dp = updater.factory.definition_processor

    # First call: parses a.py, so a.py joins _parsed_files for good.
    updater.reingest(["a.py"])
    ing.flush_all()

    victim = next(qn for qn in dp.module_qn_to_file_path if qn.endswith(".a"))
    assert victim in dp.module_qn_to_file_path, "fixture guard: a must be mapped"
    assert any(p.name == "a.py" for p, _ in updater._parsed_files), (
        "fixture guard: a.py must be in the accumulated _parsed_files, or the "
        "naive exemption would not wrongly protect it and this proves nothing"
    )

    # Now another writer deletes a's Module, and we reingest a DIFFERENT file.
    del ing.flushed[victim]
    updater.reingest(["b.py"])

    assert victim not in dp.module_qn_to_file_path, (
        "the second reingest did not prune a stale qn, because the exemption "
        "still covered a file parsed by an EARLIER call: the map never shrinks "
        "on the retained updater this fix exists for"
    )
