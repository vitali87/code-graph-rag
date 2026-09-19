# A file the run could not read was left out of the hash cache, and the
# run still published the directory stamps, so the next run's in-sync fast
# path reported nothing to do and the file was never retried until its
# directory changed (issue #1983). The cache now carries an unreadable mark
# for it: the in-sync check refuses on the mark, the pass hashes the file
# whatever its mtime, and a KNOWN file is re-parsed with its old subtree
# deleted first rather than as a new file.
from __future__ import annotations

import os
from pathlib import Path

import pytest

from codebase_rag import constants as cs
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


def test_a_symbol_renamed_while_unreadable_leaves_no_stale_entity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A previously cached file that becomes readable again must not be
    treated as NEW: it would skip the delete-before-reparse and keep the
    old symbol beside the renamed one (bot review on PR #1993). The cache
    carries the unreadable mark, so the retry is a known-file re-parse."""
    import json

    root = tmp_path / "proj"
    root.mkdir()
    (root / "a.py").write_text("def a():\n    return 1\n")
    (root / "pkg").mkdir()
    (root / "pkg" / "__init__.py").write_text("")
    (root / "pkg" / "b.py").write_text("def old_name():\n    return 2\n")
    store = _StatefulIngestor()
    _updater(root, store).run(force=True)
    project = next(uid for label, uid in store.nodes if label == "Project")
    assert ("Function", f"{project}.pkg.b.old_name") in store.nodes

    cache_mtime = (root / cs.HASH_CACHE_FILENAME).stat().st_mtime
    edited = root / "pkg" / "b.py"
    edited.write_text("def new_name():\n    return 2\n")
    os.utime(edited, (cache_mtime + 1, cache_mtime + 1))
    real = gu._hash_file_with_bytes
    monkeypatch.setattr(
        gu,
        "_hash_file_with_bytes",
        lambda path: None if path.name == "b.py" else real(path),
    )
    _updater(root, store).run()
    cache = json.loads((root / cs.HASH_CACHE_FILENAME).read_text())
    assert cache["pkg/b.py"] == cs.HASH_CACHE_UNREADABLE
    monkeypatch.setattr(gu, "_hash_file_with_bytes", real)

    retry = _updater(root, store)
    assert retry._is_already_in_sync() is False
    retry.run()
    assert retry._reparsed_file_keys == {"pkg/b.py"}
    assert ("Function", f"{project}.pkg.b.new_name") in store.nodes
    assert ("Function", f"{project}.pkg.b.old_name") not in store.nodes
    assert _updater(root, store)._is_already_in_sync() is True


def test_only_a_gone_path_escapes_the_mark(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A vanished path and a broken symlink are gone; a file that exists but
    cannot be reached is not, even when `exists()` says so (bot review on
    PR #1993): the mark must survive a permission failure on the file or
    its directory."""
    present = tmp_path / "present.py"
    present.write_text("x = 1\n")
    assert gu._vanished(tmp_path / "missing.py") is True
    link = tmp_path / "dangling.py"
    try:
        link.symlink_to(tmp_path / "nowhere.py")
    except OSError:  # Windows without symlink privileges: the rest still holds.
        pytest.skip("symlinks need privileges on this host")
    assert gu._vanished(link) is True
    assert gu._vanished(present) is False
    # `exists()` reading False for a reachable path must not count as gone.
    monkeypatch.setattr(Path, "exists", lambda self: False)
    assert gu._vanished(present) is False
    assert gu._vanished(link) is True


def test_a_link_whose_target_cannot_be_reached_is_not_gone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A symlink to a target whose metadata lookup fails with a permission
    error is unreadable, not vanished: the mark must be recorded and the
    run must not raise (bot review on PR #1993)."""
    target = tmp_path / "target.py"
    target.write_text("x = 1\n")
    link = tmp_path / "link.py"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlinks need privileges on this host")
    real_stat = os.stat

    def denied(path, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN202
        if Path(str(path)) == link and not kwargs.get("follow_symlinks", True) is False:
            raise PermissionError(13, "Permission denied", str(path))
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(gu.os, "stat", denied)
    assert gu._vanished(link) is False
