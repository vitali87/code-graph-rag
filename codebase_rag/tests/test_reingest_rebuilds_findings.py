"""A scoped re-ingest must rebuild the passes a full run does (issue #1670).

Two repository-wide passes never ran on the `reingest` path:

* the finding analysis (`HAS_SMELL`, `HAS_VULNERABILITY`,
  `IMPLEMENTS_PATTERN`). `CYPHER_DELETE_MODULE` detaches a re-parsed Module
  from its finding nodes and nothing recreated the edges until the next full
  `update_repository`, so a re-ingested file lost its findings and stayed
  lost -- which reads as "this file is clean";
* the URL-to-endpoint link pass, which rebuilds `RESOLVES_TO` from all live
  resources, so a changed route or client URL kept its old links.

The issue offers a scoped variant or "a measured statement that the full pass
is cheap enough to run every time". Measured over this repo's own parser
tree: the finding pass takes 1.001s for 125 modules against 0.001s for one.
The full pass grows with the repository rather than with the change, so the
scoped variant is the answer.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from codebase_rag import constants as cs
from codebase_rag.capture import resolve_capture
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers

_FINDING_RELS = {
    cs.RelationshipType.HAS_SMELL.value,
    cs.RelationshipType.HAS_VULNERABILITY.value,
    cs.RelationshipType.IMPLEMENTS_PATTERN.value,
}

# `eval` on a parameter is flagged by the shipped rules. If that ever stops
# being true the fixture guard below fails loudly rather than passing empty.
_RISKY = "def run(data):\n    return eval(data)\n"
_PLAIN = "def helper():\n    return 1\n"


def _finding_edges(mock: MagicMock) -> set[tuple[str, str]]:
    return {
        (str(call.args[0][2]), str(call.args[2][2]))
        for call in mock.ensure_relationship_batch.call_args_list
        if str(call.args[1]) in _FINDING_RELS
    }


def _build(tmp_path: Path, files: dict[str, str]) -> tuple[GraphUpdater, MagicMock]:
    for rel, content in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    parsers, queries = load_parsers()
    mock = MagicMock()
    updater = GraphUpdater(
        ingestor=mock,
        repo_path=tmp_path,
        parsers=parsers,
        queries=queries,
        # FINDINGS is opt-in. Without it the analyzer no-ops and every
        # assertion here passes on an empty set -- the whole file would be
        # green while measuring nothing.
        capture=resolve_capture([cs.CaptureGroup.FINDINGS.value]),
    )
    updater.run()
    return updater, mock


def test_a_reingested_file_keeps_its_findings(tmp_path: Path) -> None:
    updater, mock = _build(tmp_path, {"risky.py": _RISKY, "other.py": _PLAIN})
    assert _finding_edges(mock), (
        "fixture guard: the full index produced no findings, so this test "
        "cannot tell a rebuilt edge from an absent one"
    )

    mock.reset_mock()
    target = tmp_path / "risky.py"
    target.write_text(_RISKY.replace("eval(data)", "eval(data)  # edited"), "utf-8")
    updater.reingest((target,))

    assert _finding_edges(mock), (
        "the re-ingested file emitted no findings; its Module was detached "
        "from them and nothing rebuilt the edges, so the file reads as clean"
    )


def test_the_rebuild_is_scoped_to_the_reparsed_files(tmp_path: Path) -> None:
    """The measured reason for scoping, asserted rather than assumed.

    Re-running the whole repository's analysis on every re-ingest is the
    other option the issue offers, and it costs time proportional to the
    repository rather than to the change. This pins that the analyser is
    handed only the re-parsed modules, so a future "simplification" to the
    full map fails here instead of quietly slowing every re-ingest.
    """
    updater, _ = _build(
        tmp_path,
        {"risky.py": _RISKY, "other.py": _PLAIN, "third.py": _PLAIN},
    )

    # The analyzer uses __slots__, so the spy goes on the CLASS and is
    # removed again before the assertions run.
    seen: list[int] = []
    analyzer = updater.finding_analyzer
    real = type(analyzer).analyze

    def _spy(self: object, module_map: dict[str, Path]) -> None:
        if self is analyzer:
            seen.append(len(module_map))
        return real(self, module_map)

    target = tmp_path / "risky.py"
    target.write_text(_RISKY.replace("eval(data)", "eval(data)  # edited"), "utf-8")
    with patch.object(type(analyzer), "analyze", _spy):
        updater.reingest((target,))

    assert seen, "the re-ingest never ran the finding analysis at all"
    assert max(seen) < 3, (
        f"the re-ingest analysed {max(seen)} modules for a one-file change; "
        "the pass is scoped to the re-parsed files, not the repository"
    )


def test_a_reingest_relinks_endpoint_resources(tmp_path: Path) -> None:
    """The second pass: the URL-to-endpoint link rebuild.

    It deletes every network link and rebuilds it from all live resources, so
    a changed route or client URL kept its old links until the next full
    update. Asserted through the call rather than the edges, because the link
    pass needs a queryable store and the fixture ingestor is a mock -- the
    defect was that it never ran on this path at all.
    """
    updater, _ = _build(tmp_path, {"risky.py": _RISKY})

    calls: list[int] = []
    real_link = type(updater)._link_endpoint_resources

    def _spy(self: object) -> None:
        if self is updater:
            calls.append(1)
        return real_link(self)

    target = tmp_path / "risky.py"
    target.write_text(_RISKY.replace("eval(data)", "eval(data)  # edited"), "utf-8")
    with patch.object(type(updater), "_link_endpoint_resources", _spy):
        updater.reingest((target,))

    assert calls, (
        "the re-ingest never ran the endpoint link pass, so a changed route "
        "or client URL keeps its old links until the next full update"
    )


def test_the_rebuild_runs_before_the_hash_commit(tmp_path: Path) -> None:
    """Ordering, not just presence (raised in review of #1852).

    `_reingest_update_hashes` saves the hash cache to disk, which records
    these files as indexed. Queueing the rebuilt finding and link writes
    AFTER it meant an interruption in between left the caches claiming the
    files were done while those writes never landed -- and the next run would
    skip them as unchanged, so the loss became permanent rather than being
    repaired on the following pass.

    `run()` does both passes before its own cache write; this asserts the
    re-ingest path matches.
    """
    updater, _ = _build(tmp_path, {"risky.py": _RISKY})
    order: list[str] = []

    real_findings = type(updater)._reingest_rebuild_findings
    real_link = type(updater)._link_endpoint_resources
    real_hashes = type(updater)._reingest_update_hashes
    real_flush = updater.ingestor.flush_all

    def _findings(self: object, *a: object, **k: object) -> object:
        order.append("findings")
        return real_findings(self, *a, **k)

    def _link(self: object, *a: object, **k: object) -> object:
        order.append("link")
        return real_link(self, *a, **k)

    def _hashes(self: object, *a: object, **k: object) -> object:
        order.append("hashes")
        return real_hashes(self, *a, **k)

    def _flush(*a: object, **k: object) -> object:
        order.append("flush")
        return real_flush(*a, **k)

    updater.ingestor.flush_all = _flush  # type: ignore[method-assign]

    target = tmp_path / "risky.py"
    target.write_text(_RISKY.replace("eval(data)", "eval(data)  # edited"), "utf-8")
    with (
        patch.object(type(updater), "_reingest_rebuild_findings", _findings),
        patch.object(type(updater), "_link_endpoint_resources", _link),
        patch.object(type(updater), "_reingest_update_hashes", _hashes),
    ):
        updater.reingest((target,))

    assert "hashes" in order, "fixture guard: the hash commit never ran"
    assert "findings" in order, "fixture guard: the finding rebuild never ran"
    assert order.index("findings") < order.index("hashes"), (
        f"the finding rebuild ran AFTER the hash commit: {order}"
    )
    assert order.index("link") < order.index("hashes"), (
        f"the endpoint link pass ran AFTER the hash commit: {order}"
    )
    # And the queued writes must actually reach the store before the cache
    # records these files as current. `_reingest_rebuild_findings` only
    # QUEUES, and `_link_endpoint_resources` returns early when RESOLVES_TO
    # is disabled, so neither post-pass guarantees a flush of its own.
    # A flush must fall BETWEEN the rebuild and the hash commit. `.index`
    # alone is not enough: an earlier flush already runs inside the re-ingest
    # (before the rebuild), so "a flush happened before the hash commit" is
    # true even with the new one removed -- the first version of this
    # assertion passed against exactly that mutation.
    first = order.index("findings")
    last = order.index("hashes")
    assert "flush" in order[first:last], (
        f"the writes the rebuild QUEUED were never flushed before the hash "
        f"commit, so an interruption loses them for good: {order}"
    )


def test_a_full_run_still_analyses_everything(tmp_path: Path) -> None:
    """The control: scoping the re-ingest must not scope the full run.

    A fix that narrowed both would satisfy the tests above while quietly
    stopping `update_repository` from analysing the repository.

    Asserts what the analyser RECEIVED, not what the module map contains
    (raised by CodeRabbit on #1852). The earlier version checked the map was
    populated, which is true even if analysis is skipped entirely or handed a
    single module -- it could not see the regression it exists to catch.
    """
    for rel, content in {"risky.py": _RISKY, "other.py": _PLAIN}.items():
        (tmp_path / rel).write_text(content, encoding="utf-8")
    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=MagicMock(),
        repo_path=tmp_path,
        parsers=parsers,
        queries=queries,
        capture=resolve_capture([cs.CaptureGroup.FINDINGS.value]),
    )

    received: list[set[Path]] = []
    real = type(updater.finding_analyzer).analyze

    def _spy(self: object, module_map: dict[str, Path]) -> None:
        received.append(set(module_map.values()))
        return real(self, module_map)

    with patch.object(type(updater.finding_analyzer), "analyze", _spy):
        updater.run()

    assert received, "the full run never analysed anything"
    analysed = set().union(*received)
    for rel in ("risky.py", "other.py"):
        assert tmp_path / rel in analysed, (
            f"the full run did not hand {rel} to the finding analyser; "
            f"it saw {sorted(p.name for p in analysed)}"
        )
