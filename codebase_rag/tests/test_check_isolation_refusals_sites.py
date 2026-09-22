from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from check_isolation_helpers import (
    PROJECT,
    _edit,
)

from codebase_rag import constants as cs
from codebase_rag import cypher_queries as cq
from codebase_rag.capture import CaptureSelection, resolve_capture
from codebase_rag.check_isolation import IsolationGuard
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.structural_check import CheckError, _FileSnapshot, run_check
from evals.cgr_graph import _StatefulIngestor

pytest_plugins = ("check_isolation_helpers",)

# --- refusals -----------------------------------------------------------------


def test_an_isolated_check_refuses_a_capture_holding_io_links(
    indexed: tuple[Path, _StatefulIngestor],
) -> None:
    """Resource links are rewritten graph-wide by the endpoint pass and sit
    off the capture's walk, so they cannot be restored. Refuse rather than
    lose them silently."""
    root, store = indexed
    parsers, queries = load_parsers()
    # Built outside the raises block: only the call under test may be the
    # thing that throws, or a failure in the setup reads as a pass
    # (python:S5778).
    io_capture = resolve_capture(["io"])

    with pytest.raises(CheckError, match="isolated"):
        run_check(
            root,
            "HEAD",
            PROJECT,
            store,
            parsers,
            queries,
            isolated=True,
            capture=io_capture,
        )


def test_an_isolated_check_runs_without_io_links(
    indexed: tuple[Path, _StatefulIngestor],
) -> None:
    """The control: the refusal is about IO links, not about --isolated."""
    root, store = indexed
    _edit(root)
    parsers, queries = load_parsers()

    delta = run_check(
        root,
        "HEAD",
        PROJECT,
        store,
        parsers,
        queries,
        isolated=True,
        capture=resolve_capture(["none", "structure", "calls"]),
    )

    assert delta["dangling_callers"]


# --- per-site edges -----------------------------------------------------------


def test_two_sites_on_one_pair_are_captured_and_restored_separately() -> None:
    """Parallel edges differ only in their site properties.

    `MERGE_KEY_PROPS_BY_REL` makes `(line, col)` part of a CALLS edge's
    identity, so one caller/callee pair carries one edge per site (#1522).
    The guard must key its capture the same way or the second site's row
    replaces the first and the restore re-emits one edge where there were
    two.

    Driven against a hand-built store rather than a parsed fixture: the
    eval double collapses parallel edges into one (issue #1921), so a
    fixture with two call sites cannot express the case it is meant to
    test. The rows here are the shape the production query returns.
    """
    captured: list[tuple[object, str, object, dict | None]] = []

    class _Store:
        def fetch_all(self, query: str, params: dict | None = None) -> list[dict]:
            if query == cq.CYPHER_CHECK_SCOPE_NODES:
                return []
            return [
                {
                    cs.KEY_LABEL: cs.NodeLabel.FUNCTION.value,
                    cs.KEY_QUALIFIED_NAME: "p.app.run",
                    cs.KEY_REL: cs.RelationshipType.CALLS.value,
                    cs.KEY_OUTGOING: True,
                    cs.KEY_PROPS: {cs.KEY_LINE: 5, cs.KEY_COL: col},
                    cs.KEY_FAR_LABEL: cs.NodeLabel.FUNCTION.value,
                    cs.FAR_END_PREFIX + cs.KEY_QUALIFIED_NAME: "p.util.helper",
                }
                for col in (11, 23)
            ]

        def execute_write(self, query: str, params: dict | None = None) -> None:
            return None

        def ensure_node_batch(self, label: str, properties: dict) -> None:
            return None

        def ensure_relationship_batch(
            self,
            from_spec: tuple,
            rel_type: str,
            to_spec: tuple,
            properties: dict | None = None,
        ) -> None:
            captured.append((from_spec, rel_type, to_spec, properties))

        def flush_all(self) -> None:
            return None

    guard = IsolationGuard(_Store(), "p", Path("/nonexistent"))
    guard.capture(["app.py"])
    guard.restore()

    assert [props[cs.KEY_COL] for _s, _r, _t, props in captured if props] == [11, 23]


def test_the_validated_capture_is_the_one_the_run_uses(
    indexed: tuple[Path, _StatefulIngestor], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Validating a selection and then running under a different one is no
    validation at all: the updater would fall back to the configured
    default, which can enable the IO links isolated mode just refused
    (Greptile, #1718)."""
    root, store = indexed
    _edit(root)
    parsers, queries = load_parsers()
    selection = resolve_capture(["none", "structure", "calls"])
    seen: list[CaptureSelection | None] = []
    original = GraphUpdater.__init__

    def record(self: GraphUpdater, *args: Any, **kwargs: Any) -> None:
        seen.append(kwargs.get("capture"))
        original(self, *args, **kwargs)

    # monkeypatch, not a manual save/restore: it undoes the patch even when
    # the body raises, where a `finally` only runs if control reaches it
    # (python:S8067).
    monkeypatch.setattr(GraphUpdater, "__init__", record)

    run_check(
        root,
        "HEAD",
        PROJECT,
        store,
        parsers,
        queries,
        isolated=True,
        capture=selection,
    )

    assert seen == [selection]


def test_an_unreadable_hash_cache_is_left_alone(
    indexed: tuple[Path, _StatefulIngestor], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Absent and unreadable are different states. Collapsing them made the
    restore DELETE a cache it merely could not read (Greptile, #1718)."""
    root, _store = indexed
    cache = root / cs.HASH_CACHE_FILENAME
    assert cache.is_file()
    content = cache.read_bytes()
    real_read_bytes = Path.read_bytes

    def refuse(self: Path, *args: Any, **kwargs: Any) -> bytes:
        if self == cache:
            raise PermissionError(13, "Permission denied")
        return real_read_bytes(self, *args, **kwargs)

    # Undone by the fixture, including on a failure inside the block, and
    # restored before the assertions below read the file (python:S8067).
    with monkeypatch.context() as patched:
        patched.setattr(Path, "read_bytes", refuse)
        snapshot = _FileSnapshot(cache)

    snapshot.put_back()

    assert cache.is_file(), "an unreadable cache was deleted by the restore"
    assert cache.read_bytes() == content


def test_a_missing_hash_cache_is_removed_again(
    indexed: tuple[Path, _StatefulIngestor],
) -> None:
    """The control for the test above: a cache that genuinely did not exist
    when the snapshot was taken must still be removed if the check created
    one, or the distinction would be achieved by never unlinking at all."""
    root, _store = indexed
    cache = root / cs.HASH_CACHE_FILENAME
    cache.unlink()

    snapshot = _FileSnapshot(cache)
    cache.write_bytes(b"{}")
    snapshot.put_back()

    assert not cache.exists()


def test_an_isolated_check_refuses_a_graph_already_holding_io_links(
    indexed: tuple[Path, _StatefulIngestor],
) -> None:
    """The capture check covers what THIS run writes; a graph built earlier
    with IO on can hold a resource chain the re-ingest's repo-wide prune
    would delete once a changed file's subtree stops anchoring it (bot
    review, #1718). `test_an_isolated_check_runs_without_io_links` is the
    control: the same fixture without the edge runs."""
    root, store = indexed
    resource = cs.NodeLabel.RESOURCE.value
    for name in ("env:HOME", "file:/tmp/out"):
        store.ensure_node_batch(resource, {cs.KEY_QUALIFIED_NAME: name})
    store.ensure_relationship_batch(
        (resource, cs.KEY_QUALIFIED_NAME, "env:HOME"),
        cs.RelationshipType.FLOWS_TO.value,
        (resource, cs.KEY_QUALIFIED_NAME, "file:/tmp/out"),
    )
    store.flush_all()
    _edit(root)
    parsers, queries = load_parsers()

    with pytest.raises(CheckError, match="already holds"):
        run_check(root, "HEAD", PROJECT, store, parsers, queries, isolated=True)


def test_an_isolated_check_refuses_an_unreadable_hash_cache(
    indexed: tuple[Path, _StatefulIngestor], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cache that exists but cannot be read may still be writable (mode
    0o200), so the re-ingest could rewrite it and `put_back` could not undo
    that. Refused before anything is written (bot review, #1718)."""
    root, store = indexed
    cache = root / cs.HASH_CACHE_FILENAME
    content = cache.read_bytes()
    nodes_before = dict(store.nodes)
    _edit(root)
    parsers, queries = load_parsers()
    real_read_bytes = Path.read_bytes

    def refuse(self: Path, *args: Any, **kwargs: Any) -> bytes:
        if self == cache:
            raise PermissionError(13, "Permission denied")
        return real_read_bytes(self, *args, **kwargs)

    with monkeypatch.context() as patched:
        patched.setattr(Path, "read_bytes", refuse)
        with pytest.raises(CheckError, match="cannot be read"):
            run_check(root, "HEAD", PROJECT, store, parsers, queries, isolated=True)

    assert cache.read_bytes() == content
    assert store.nodes == nodes_before


class _RecordingStore:
    """The stateful store, with every write query recorded in order."""

    def __init__(self, inner: _StatefulIngestor) -> None:
        self._inner = inner
        self.writes: list[str] = []

    def execute_write(self, query: str, params: Any = None) -> None:
        self.writes.append(query)
        self._inner.execute_write(query, params)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def _marker_writes(store: _RecordingStore) -> list[str]:
    marks = {
        cq.CYPHER_MARK_PROJECT_INCOMPLETE: "mark",
        cq.CYPHER_CLEAR_PROJECT_INCOMPLETE: "clear",
    }
    return [marks[query] for query in store.writes if query in marks]


def test_an_isolated_check_clears_its_marker_once_restored(
    indexed: tuple[Path, _StatefulIngestor],
) -> None:
    """The marker is set before the first write and cleared only after the
    restore; this is the success half of the test below."""
    root, inner = indexed
    store = _RecordingStore(inner)
    _edit(root)
    parsers, queries = load_parsers()

    run_check(root, "HEAD", PROJECT, store, parsers, queries, isolated=True)

    assert _marker_writes(store) == ["mark", "clear"]


def test_a_failed_restore_leaves_the_incomplete_marker_set(
    indexed: tuple[Path, _StatefulIngestor], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The restore deletes before it re-creates, with no transaction around
    the two, so a failure between them leaves the graph partial. The
    persistent marker (#1679) must then stay set, so readers refuse and the
    next full update repairs (bot review, #1718)."""
    root, inner = indexed
    store = _RecordingStore(inner)
    _edit(root)
    parsers, queries = load_parsers()

    def fail(_self: IsolationGuard) -> None:
        raise RuntimeError("restore failed after its deletes")

    monkeypatch.setattr(IsolationGuard, "_replace_survivor_properties", fail)
    with pytest.raises(RuntimeError, match="restore failed"):
        run_check(root, "HEAD", PROJECT, store, parsers, queries, isolated=True)

    assert _marker_writes(store) == ["mark"]


@pytest.mark.parametrize("flag", [True, False])
def test_cgr_check_forwards_isolated_to_run_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, flag: bool
) -> None:
    """The CLI boundary itself: `cgr check --isolated` must reach
    `run_check` as `isolated=True`, and its absence as False (the control).
    Driving `run_check` directly could not catch a typo in the option or a
    dropped keyword (bot review, #1718)."""
    import contextlib

    from typer.testing import CliRunner

    from codebase_rag import cli, structural_check
    from codebase_rag import cli_help as ch

    seen: dict[str, Any] = {}

    class _Graph:
        def list_projects(self) -> list[str]:
            return [PROJECT]

    @contextlib.contextmanager
    def connect(**_kwargs: Any):  # noqa: ANN202
        yield _Graph()

    def run_check(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        seen.update(kwargs)
        return {}

    monkeypatch.setattr(cli, "connect_memgraph", connect)
    monkeypatch.setattr(cli, "load_parsers", lambda: ({}, {}))
    monkeypatch.setattr(structural_check, "indexed_scope", lambda *a, **k: (None, None))
    monkeypatch.setattr(structural_check, "run_check", run_check)
    args = [ch.CLICommandName.CHECK.value, "--repo-path", str(tmp_path)]
    args += ["--project", PROJECT] + (["--isolated"] if flag else [])

    result = CliRunner().invoke(cli.app, args)

    assert result.exit_code == 0, result.output
    assert seen["isolated"] is flag
