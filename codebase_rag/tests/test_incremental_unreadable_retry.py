# A file the run could not read was left out of the hash cache, and the
# run still published the directory stamps, so the next run's in-sync fast
# path reported nothing to do and the file was never retried until its
# directory changed (issue #1983).
from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import graph_updater as gu
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from evals.cgr_graph import _StatefulIngestor


def _updater(root: Path, store: _StatefulIngestor) -> GraphUpdater:
    # The derived project name: a NAMED project never took the fast path
    # on this base, which is issue #1981, not this one.
    parsers, queries = load_parsers()
    return GraphUpdater(
        ingestor=store,
        repo_path=root,
        parsers=parsers,
        queries=queries,
    )


def test_an_unreadable_file_is_retried_by_the_next_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    (root / "a.py").write_text("def a():\n    return 1\n")
    # In a SUBDIRECTORY: the repository root's mtime moves whenever the
    # cache stamps are written into it, so a root-level file absent from the
    # cache is re-diffed regardless; a subdirectory's mtime does not move.
    (root / "pkg").mkdir()
    (root / "pkg" / "__init__.py").write_text("")
    (root / "pkg" / "b.py").write_text("def b():\n    return 2\n")
    store = _StatefulIngestor()
    real = gu._hash_file_with_bytes

    # Portable unreadability: the read of `b.py` fails on this run only.
    def failing(path: Path):  # noqa: ANN202
        return None if path.name == "b.py" else real(path)

    monkeypatch.setattr(gu, "_hash_file_with_bytes", failing)
    first = _updater(root, store)
    first.run(force=True)
    assert first._reparsed_file_keys == {"a.py", "pkg/__init__.py"}
    monkeypatch.setattr(gu, "_hash_file_with_bytes", real)

    second = _updater(root, store)
    assert second._is_already_in_sync() is False
    second.run()
    assert second._reparsed_file_keys == {"pkg/b.py"}
    assert any(
        label == "Function" and str(uid).endswith(".b.b") for label, uid in store.nodes
    )

    third = _updater(root, store)
    assert third._is_already_in_sync() is True


def test_a_run_that_reads_every_file_keeps_the_fast_path(tmp_path: Path) -> None:
    """The control: with nothing unreadable the directory stamp is written
    and the next run is in sync."""
    root = tmp_path / "proj"
    root.mkdir()
    (root / "a.py").write_text("def a():\n    return 1\n")
    store = _StatefulIngestor()
    _updater(root, store).run(force=True)
    assert _updater(root, store)._is_already_in_sync() is True


def test_a_file_that_becomes_unreadable_after_a_healthy_run_is_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The common shape: a healthy run wrote the directory stamp, then a
    file is edited and its read fails on the next sync. Merely withholding
    the stamp would leave the healthy run's stamp on disk and the fast path
    would fire on it (local review); the stamp is cleared instead, so the
    run after walks the tree and parses the file."""
    root = tmp_path / "proj"
    root.mkdir()
    (root / "a.py").write_text("def a():\n    return 1\n")
    (root / "pkg").mkdir()
    (root / "pkg" / "__init__.py").write_text("")
    (root / "pkg" / "b.py").write_text("def b():\n    return 2\n")
    store = _StatefulIngestor()
    _updater(root, store).run(force=True)
    assert _updater(root, store)._is_already_in_sync() is True

    cache_mtime = (root / ".cgr-hash-cache.json").stat().st_mtime
    edited = root / "pkg" / "b.py"
    edited.write_text("def b():\n    return 3\n")
    import os

    os.utime(edited, (cache_mtime + 1, cache_mtime + 1))
    real = gu._hash_file_with_bytes
    monkeypatch.setattr(
        gu,
        "_hash_file_with_bytes",
        lambda path: None if path.name == "b.py" else real(path),
    )
    unreadable_run = _updater(root, store)
    unreadable_run.run()
    assert "pkg/b.py" not in unreadable_run._reparsed_file_keys
    monkeypatch.setattr(gu, "_hash_file_with_bytes", real)

    retry = _updater(root, store)
    assert retry._is_already_in_sync() is False
    retry.run()
    assert retry._reparsed_file_keys == {"pkg/b.py"}
    assert _updater(root, store)._is_already_in_sync() is True
