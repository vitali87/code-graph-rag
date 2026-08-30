# Transactional multi-file edits (issue #1528): stage, verify, then apply all
# files or none; every applied transaction is recorded so it can be shown and
# undone. The acceptance criteria are byte-identity of the tree after a failed
# verification and atomic application (plus the combined diff) on success.
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from codebase_rag import constants as cs
from codebase_rag.editing import (
    EditTransaction,
    StagedTree,
    TransactionConflict,
    TransactionError,
    VerificationResult,
    transaction,
    undo_last,
)
from codebase_rag.editing.cli import cli as edits_cli
from codebase_rag.editing.transaction import load_history


def _tree_digest(root: Path) -> dict[str, str]:
    """path -> sha256 of every regular file, history file excluded."""
    digest: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.name not in cs.CGR_STATE_FILENAMES:
            digest[path.relative_to(root).as_posix()] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
    return digest


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "a.py").write_bytes(b"def a():\n    return 1\n")
    (root / "pkg" / "b.py").write_bytes(b"def b():\n    return 2\n")
    (root / "README.md").write_bytes(b"# repo\n")
    (root / "node_modules").mkdir()
    (root / "node_modules" / "junk.js").write_bytes(b"x")
    return root


# --- acceptance ----------------------------------------------------------------


def test_failed_verification_leaves_the_tree_byte_identical(repo: Path) -> None:
    before = _tree_digest(repo)
    tx = EditTransaction(repo)
    tx.stage("pkg/a.py", "def a():\n    return 10\n")
    tx.stage("pkg/new.py", "def n():\n    return 0\n")
    tx.stage("pkg/b.py", None)

    outcome = tx.commit(lambda tree: VerificationResult(False, "tests failed"))

    assert outcome.applied is False
    assert "tests failed" in outcome.message
    assert outcome.files == ("pkg/a.py", "pkg/b.py", "pkg/new.py")
    assert _tree_digest(repo) == before
    assert not (repo / cs.EDIT_HISTORY_FILENAME).exists()


def test_successful_commit_applies_every_file_and_returns_the_diff(
    repo: Path,
) -> None:
    tx = EditTransaction(repo)
    tx.stage("pkg/a.py", "def a():\n    return 10\n")
    tx.stage("pkg/new.py", "def n():\n    return 0\n")
    tx.stage("pkg/b.py", None)

    outcome = tx.commit(lambda tree: True)

    assert outcome.applied is True
    assert outcome.files == ("pkg/a.py", "pkg/b.py", "pkg/new.py")
    assert (repo / "pkg" / "a.py").read_text() == "def a():\n    return 10\n"
    assert (repo / "pkg" / "new.py").read_text() == "def n():\n    return 0\n"
    assert not (repo / "pkg" / "b.py").exists()
    assert "--- a/pkg/a.py" in outcome.diff
    assert "+++ b/pkg/a.py" in outcome.diff
    assert "-    return 1\n+    return 10\n" in outcome.diff
    assert f"--- {cs.DIFF_DEV_NULL}\n+++ b/pkg/new.py" in outcome.diff
    assert f"--- a/pkg/b.py\n+++ {cs.DIFF_DEV_NULL}" in outcome.diff
    # No temp siblings left behind by the atomic writes.
    assert not list(repo.rglob(f"*{cs.TMP_EXTENSION}"))


# --- staging semantics --------------------------------------------------------


def test_staged_tree_reads_overlay_first_and_disk_second(repo: Path) -> None:
    tx = EditTransaction(repo)
    tx.stage("pkg/a.py", "changed\n")
    tx.stage("pkg/b.py", None)
    tree = tx.tree()
    assert tree.read("pkg/a.py") == b"changed\n"
    assert tree.read("pkg/b.py") is None
    assert tree.exists("pkg/b.py") is False
    assert tree.read("README.md") == b"# repo\n"
    assert tree.staged_paths() == ("pkg/a.py", "pkg/b.py")
    # The working tree is untouched until commit.
    assert (repo / "pkg" / "a.py").read_text() == "def a():\n    return 1\n"


def test_materialised_tree_has_the_overlay_and_skips_ignored_dirs(
    repo: Path,
) -> None:
    tx = EditTransaction(repo)
    tx.stage("pkg/a.py", "changed\n")
    tx.stage("pkg/b.py", None)
    tx.stage("deep/new.py", "new\n")
    tree = tx.tree()
    root = tree.root
    try:
        assert root != repo
        assert (root / "pkg" / "a.py").read_text() == "changed\n"
        assert not (root / "pkg" / "b.py").exists()
        assert (root / "deep" / "new.py").read_text() == "new\n"
        assert (root / "README.md").read_text() == "# repo\n"
        assert not (root / "node_modules").exists()
        assert tree.root == root, "materialised once"
    finally:
        tree.cleanup()
    assert not root.exists()


def test_verifier_sees_the_staged_tree_and_can_run_a_tool(repo: Path) -> None:
    seen: dict[str, object] = {}

    def verify(tree: StagedTree) -> VerificationResult:
        seen["overlay"] = tree.read("pkg/a.py")
        seen["disk"] = (tree.root / "pkg" / "a.py").read_bytes()
        seen["root"] = tree.root
        return VerificationResult(True, "ok")

    tx = EditTransaction(repo)
    tx.stage("pkg/a.py", "verified\n")
    outcome = tx.commit(verify)
    assert outcome.applied
    assert seen["overlay"] == b"verified\n" == seen["disk"]
    assert not Path(str(seen["root"])).exists(), "staging copy removed after commit"


def test_staging_the_current_content_is_not_an_edit(repo: Path) -> None:
    tx = EditTransaction(repo)
    tx.stage("pkg/a.py", "def a():\n    return 1\n")
    assert tx.staged == ()
    outcome = tx.commit()
    assert outcome.applied is False
    assert outcome.message == cs.EDIT_NOTHING_STAGED
    assert not (repo / cs.EDIT_HISTORY_FILENAME).exists()


def test_later_stage_of_the_same_file_wins_but_keeps_the_disk_baseline(
    repo: Path,
) -> None:
    tx = EditTransaction(repo)
    tx.stage("pkg/a.py", "first\n")
    tx.stage("pkg/a.py", "second\n")
    (staged,) = tx.staged
    assert staged.before == b"def a():\n    return 1\n"
    assert staged.after == b"second\n"
    tx.unstage("pkg/a.py")
    assert tx.staged == ()


def test_paths_outside_the_repo_are_refused(repo: Path) -> None:
    tx = EditTransaction(repo)
    with pytest.raises(TransactionError):
        tx.stage("../escape.py", "x")
    with pytest.raises(TransactionError):
        tx.stage(repo.parent / "other.py", "x")
    with pytest.raises(TransactionError):
        tx.stage("pkg", "x")  # a directory


def test_a_file_changed_after_staging_makes_commit_refuse(repo: Path) -> None:
    tx = EditTransaction(repo)
    tx.stage("pkg/a.py", "mine\n")
    (repo / "pkg" / "a.py").write_bytes(b"someone else\n")
    before = _tree_digest(repo)
    with pytest.raises(TransactionConflict):
        tx.commit()
    assert _tree_digest(repo) == before


def test_verifier_exceptions_and_false_count_as_failure(repo: Path) -> None:
    tx = EditTransaction(repo)
    tx.stage("pkg/a.py", "x\n")

    def boom(tree: StagedTree) -> bool:
        raise RuntimeError("kaboom")

    outcome = tx.commit(boom)
    assert outcome.applied is False
    assert "kaboom" in outcome.verification.message

    tx2 = EditTransaction(repo)
    tx2.stage("pkg/a.py", "x\n")
    outcome = tx2.commit(lambda tree: False)
    assert outcome.applied is False
    assert outcome.verification.message == cs.EDIT_VERIFIER_FALSE
    assert (repo / "pkg" / "a.py").read_text() == "def a():\n    return 1\n"


def test_finished_transactions_cannot_be_reused(repo: Path) -> None:
    tx = EditTransaction(repo)
    tx.stage("pkg/a.py", "x\n")
    tx.rollback()
    assert tx.staged == ()
    with pytest.raises(TransactionError):
        tx.stage("pkg/a.py", "y\n")
    with pytest.raises(TransactionError):
        tx.commit()


def test_context_manager_rolls_back_on_exception(repo: Path) -> None:
    holder: dict[str, EditTransaction] = {}

    def abort() -> None:
        with transaction(repo) as tx:
            holder["tx"] = tx
            tx.stage("pkg/a.py", "x\n")
            raise RuntimeError("abort")

    with pytest.raises(RuntimeError):
        abort()
    assert holder["tx"].staged == ()
    assert (repo / "pkg" / "a.py").read_text() == "def a():\n    return 1\n"


def test_a_write_failure_midway_restores_what_already_landed(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import importlib

    module = importlib.import_module("codebase_rag.editing.transaction")

    original = module._write_file
    calls: list[str] = []

    def flaky(path: Path, content: bytes | None, mode: int | None = None) -> None:
        calls.append(path.name)
        if path.name == "b.py":
            raise OSError("disk full")
        original(path, content, mode)

    monkeypatch.setattr(module, "_write_file", flaky)
    before = _tree_digest(repo)
    tx = EditTransaction(repo)
    tx.stage("pkg/a.py", "changed\n")
    tx.stage("pkg/b.py", "changed\n")
    with pytest.raises(OSError):
        tx.commit()
    monkeypatch.setattr(module, "_write_file", original)
    assert calls[:2] == ["a.py", "b.py"]
    assert _tree_digest(repo) == before


# --- history and undo ---------------------------------------------------------


def test_commit_records_the_patch_set_and_undo_reverses_it(repo: Path) -> None:
    original = _tree_digest(repo)
    tx = EditTransaction(repo)
    tx.stage("pkg/a.py", "v2\n")
    tx.stage("pkg/new.py", "new\n")
    tx.stage("pkg/b.py", None)
    outcome = tx.commit(lambda tree: VerificationResult(True, "12 tests passed"))

    entries = load_history(repo)
    assert [e[cs.EDIT_KEY_ID] for e in entries] == [outcome.transaction_id]
    entry = entries[0]
    assert [f[cs.KEY_PATH] for f in entry[cs.EDIT_KEY_FILES]] == [
        "pkg/a.py",
        "pkg/b.py",
        "pkg/new.py",
    ]
    assert entry[cs.EDIT_KEY_VERIFICATION] == {
        cs.EDIT_KEY_OK: True,
        cs.EDIT_KEY_MESSAGE: "12 tests passed",
    }

    undone = undo_last(repo)
    assert [u.applied for u in undone] == [True]
    assert undone[0].transaction_id == outcome.transaction_id
    assert _tree_digest(repo) == original
    assert load_history(repo) == []


def test_undo_stops_at_a_file_that_changed_since(repo: Path) -> None:
    tx1 = EditTransaction(repo)
    tx1.stage("pkg/a.py", "one\n")
    tx1.commit()
    tx2 = EditTransaction(repo)
    tx2.stage("pkg/b.py", "two\n")
    tx2.commit()
    (repo / "pkg" / "b.py").write_bytes(b"hand edit\n")

    with pytest.raises(TransactionConflict):
        undo_last(repo, count=2)

    # Nothing was undone: the newest transaction conflicted first.
    assert (repo / "pkg" / "a.py").read_text() == "one\n"
    assert (repo / "pkg" / "b.py").read_text() == "hand edit\n"
    assert len(load_history(repo)) == 2


def test_undo_count_walks_newest_first(repo: Path) -> None:
    for i in range(3):
        tx = EditTransaction(repo)
        tx.stage("pkg/a.py", f"v{i}\n")
        tx.commit()
    outcomes = undo_last(repo, count=2)
    assert [o.applied for o in outcomes] == [True, True]
    assert (repo / "pkg" / "a.py").read_text() == "v0\n"
    assert len(load_history(repo)) == 1


def test_history_is_capped(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cs, "EDIT_HISTORY_LIMIT", 2)
    for i in range(4):
        tx = EditTransaction(repo)
        tx.stage("pkg/a.py", f"v{i}\n")
        tx.commit()
    entries = load_history(repo)
    assert len(entries) == 2
    raw = json.loads((repo / cs.EDIT_HISTORY_FILENAME).read_text())
    assert len(raw) == 2


def test_history_file_is_not_indexed() -> None:
    assert cs.EDIT_HISTORY_FILENAME in cs.CGR_STATE_FILENAMES


# --- cgr edits ----------------------------------------------------------------


def test_cli_show_lists_transactions_newest_first(repo: Path) -> None:
    ids = []
    for i in range(2):
        tx = EditTransaction(repo)
        tx.stage("pkg/a.py", f"v{i}\n")
        ids.append(tx.commit().transaction_id)
    result = CliRunner().invoke(edits_cli, ["show", "--repo-path", str(repo), "--diff"])
    assert result.exit_code == 0, result.output
    assert result.output.index(ids[1]) < result.output.index(ids[0])
    assert "pkg/a.py" in result.output
    assert "+v1" in result.output


def test_cli_show_with_no_history(repo: Path) -> None:
    result = CliRunner().invoke(edits_cli, ["show", "--repo-path", str(repo)])
    assert result.exit_code == 0
    assert cs.EDIT_SHOW_NONE in result.output


def test_cli_undo_reverses_and_reports(repo: Path) -> None:
    tx = EditTransaction(repo)
    tx.stage("pkg/a.py", "v1\n")
    tx_id = tx.commit().transaction_id
    result = CliRunner().invoke(edits_cli, ["undo", "--repo-path", str(repo)])
    assert result.exit_code == 0, result.output
    assert tx_id in result.output
    assert (repo / "pkg" / "a.py").read_text() == "def a():\n    return 1\n"
    again = CliRunner().invoke(edits_cli, ["undo", "--repo-path", str(repo)])
    assert cs.EDIT_UNDO_NONE in again.output


def test_cli_undo_conflict_exits_nonzero(repo: Path) -> None:
    tx = EditTransaction(repo)
    tx.stage("pkg/a.py", "v1\n")
    tx.commit()
    (repo / "pkg" / "a.py").write_bytes(b"hand edit\n")
    result = CliRunner().invoke(edits_cli, ["undo", "--repo-path", str(repo)])
    assert result.exit_code == 1
    assert (repo / "pkg" / "a.py").read_text() == "hand edit\n"


# --- Review findings on PR #1540 ----------------------------------------------


def test_escaping_symlink_cannot_reach_the_live_tree_from_the_staging_copy(
    repo: Path, tmp_path: Path
) -> None:
    """A verifier writing through a symlink in `tree.root` must never touch
    the working tree (or anything outside it) when it then fails."""
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"keep\n")
    (repo / "escape").symlink_to(outside)
    (repo / "inner").symlink_to(repo / "README.md")
    before = _tree_digest(repo)

    def verify(tree: StagedTree) -> VerificationResult:
        root = tree.root
        assert not (root / "escape").exists(), "escaping link is not copied"
        # An in-repo link is a plain copy: writing to it changes the staging
        # tree only.
        assert not (root / "inner").is_symlink()
        (root / "inner").write_bytes(b"scribble\n")
        (root / "README.md").write_bytes(b"scribble\n")
        return VerificationResult(False, "no")

    tx = EditTransaction(repo)
    tx.stage("pkg/a.py", "x\n")
    outcome = tx.commit(verify)
    assert outcome.applied is False
    assert outside.read_text() == "keep\n"
    assert (repo / "README.md").read_text() == "# repo\n"
    assert _tree_digest(repo) == before


def test_undo_rejects_history_paths_outside_the_repo(
    repo: Path, tmp_path: Path
) -> None:
    victim = tmp_path / "victim.txt"
    victim.write_bytes(b"keep\n")
    history = repo / cs.EDIT_HISTORY_FILENAME
    history.write_text(
        json.dumps(
            [
                {
                    cs.EDIT_KEY_ID: "evil",
                    cs.EDIT_KEY_AT: "now",
                    cs.EDIT_KEY_FILES: [
                        {
                            cs.KEY_PATH: str(victim),
                            cs.EDIT_KEY_BEFORE: None,
                            cs.EDIT_KEY_AFTER: "a2VlcAo=",
                        }
                    ],
                    cs.EDIT_KEY_VERIFICATION: {
                        cs.EDIT_KEY_OK: True,
                        cs.EDIT_KEY_MESSAGE: "",
                    },
                }
            ]
        ),
        encoding="utf-8",
    )
    with pytest.raises(TransactionError):
        undo_last(repo)
    assert victim.read_text() == "keep\n"
    assert len(load_history(repo)) == 1


def test_commit_holds_an_os_level_lock_file(repo: Path) -> None:
    tx = EditTransaction(repo)
    tx.stage("pkg/a.py", "x\n")
    seen: dict[str, bool] = {}

    def verify(tree: StagedTree) -> bool:
        seen["lock_exists"] = (repo / cs.EDIT_LOCK_FILENAME).exists()
        return True

    tx.commit(verify)
    assert seen["lock_exists"] is True
    assert cs.EDIT_LOCK_FILENAME in cs.CGR_STATE_FILENAMES


def test_history_record_failure_restores_the_files(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import importlib

    module = importlib.import_module("codebase_rag.editing.transaction")

    def broken(*args: object, **kwargs: object) -> None:
        raise OSError("history disk full")

    monkeypatch.setattr(module, "record_transaction", broken)
    before = _tree_digest(repo)
    tx = EditTransaction(repo)
    tx.stage("pkg/a.py", "changed\n")
    tx.stage("pkg/new.py", "new\n")
    with pytest.raises(OSError):
        tx.commit()
    assert _tree_digest(repo) == before
    assert not (repo / "pkg" / "new.py").exists()


def test_undo_history_truncation_failure_restores_the_reversal(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import importlib

    module = importlib.import_module("codebase_rag.editing.transaction")
    tx = EditTransaction(repo)
    tx.stage("pkg/a.py", "v1\n")
    tx.commit()
    original_save = module._save_history

    def broken(root: Path, entries: list[dict]) -> None:
        if entries == []:
            raise OSError("history disk full")
        original_save(root, entries)

    monkeypatch.setattr(module, "_save_history", broken)
    with pytest.raises(OSError):
        undo_last(repo)
    # The reversal was put back: tree and history still agree.
    assert (repo / "pkg" / "a.py").read_text() == "v1\n"
    assert len(load_history(repo)) == 1


def test_cli_counts_must_be_positive(repo: Path) -> None:
    for args in (["show", "-n", "0"], ["undo", "-n", "0"], ["undo", "-n", "-2"]):
        result = CliRunner().invoke(edits_cli, [*args, "--repo-path", str(repo)])
        assert result.exit_code == 2, args


def test_replacing_an_executable_keeps_its_mode(repo: Path) -> None:
    import os
    import stat

    script = repo / "run.sh"
    script.write_bytes(b"#!/bin/sh\necho one\n")
    script.chmod(0o755)
    tx = EditTransaction(repo)
    tx.stage("run.sh", "#!/bin/sh\necho two\n")
    assert tx.commit().applied
    assert stat.S_IMODE(os.stat(script).st_mode) == 0o755
    (undone,) = undo_last(repo)
    assert undone.applied
    assert script.read_text() == "#!/bin/sh\necho one\n"
    assert stat.S_IMODE(os.stat(script).st_mode) == 0o755


def test_dangling_symlink_does_not_break_staging(repo: Path) -> None:
    (repo / "broken").symlink_to(repo / "does-not-exist")
    tx = EditTransaction(repo)
    tx.stage("pkg/a.py", "x\n")
    seen: dict[str, bool] = {}

    def verify(tree: StagedTree) -> bool:
        seen["broken"] = (tree.root / "broken").exists()
        seen["a"] = (tree.root / "pkg" / "a.py").read_text() == "x\n"
        return True

    assert tx.commit(verify).applied
    assert seen == {"broken": False, "a": True}


def test_materialise_failure_removes_the_staging_copy(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import importlib

    module = importlib.import_module("codebase_rag.editing.transaction")
    made: list[Path] = []
    original = module.tempfile.mkdtemp

    def tracking_mkdtemp(*args: object, **kwargs: object) -> str:
        path = original(*args, **kwargs)
        made.append(Path(path))
        return path

    monkeypatch.setattr(module.tempfile, "mkdtemp", tracking_mkdtemp)
    monkeypatch.setattr(
        module.shutil, "copytree", lambda *a, **k: (_ for _ in ()).throw(OSError("no"))
    )
    tree = StagedTree(repo, {})
    with pytest.raises(OSError):
        _ = tree.root
    assert made
    assert not made[0].exists()


def test_reserved_state_files_cannot_be_staged(repo: Path) -> None:
    tx = EditTransaction(repo)
    with pytest.raises(TransactionError):
        tx.stage(cs.EDIT_LOCK_FILENAME, "x")
    with pytest.raises(TransactionError):
        tx.stage(cs.EDIT_HISTORY_FILENAME, "[]")


def test_deleted_executable_is_restored_executable(repo: Path) -> None:
    import os
    import stat

    script = repo / "run.sh"
    script.write_bytes(b"#!/bin/sh\necho one\n")
    script.chmod(0o755)
    tx = EditTransaction(repo)
    tx.stage("run.sh", None)
    assert tx.commit().applied
    assert not script.exists()
    (undone,) = undo_last(repo)
    assert undone.applied
    assert stat.S_IMODE(os.stat(script).st_mode) == 0o755


def test_undo_reads_the_history_under_the_lock(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import importlib

    module = importlib.import_module("codebase_rag.editing.transaction")
    tx = EditTransaction(repo)
    tx.stage("pkg/a.py", "v1\n")
    tx.commit()
    state = {"locked": False, "loaded_locked": None}
    original_lock = module._repo_lock
    original_load = module.load_history

    @module.contextmanager
    def spy_lock(root: Path):  # type: ignore[no-untyped-def]
        state["locked"] = True
        with original_lock(root):
            yield
        state["locked"] = False

    def spy_load(root: Path) -> list[dict]:
        state["loaded_locked"] = state["locked"]
        return original_load(root)

    monkeypatch.setattr(module, "_repo_lock", spy_lock)
    monkeypatch.setattr(module, "load_history", spy_load)
    undo_last(repo)
    assert state["loaded_locked"] is True
