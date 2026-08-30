"""Transactional multi-file edits (issue #1528).

An edit-algebra operation that touches twelve files must land completely or
not at all. `EditTransaction` collects the new content of every file it will
touch in an in-memory overlay keyed by repo-relative posix path (the delombok
overlay idiom), lets a verifier inspect the staged tree, and only then writes
to the real tree: each file through a temp sibling and `os.replace`, with the
originals held so a failure part-way restores what was already written.

Every committed transaction is appended to `.cgr-edit-history.json` at the
repo root (the patch set plus the verification outcome), so `cgr edits show`
can list the last N and `cgr edits undo` can reverse them, each undo being a
transaction itself that refuses if the tree has moved on since.
"""

from __future__ import annotations

import base64
import difflib
import json
import os
import shutil
import tempfile
import threading
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import NamedTuple

from loguru import logger

from .. import constants as cs
from .. import logs as ls
from ..config import load_ignore_patterns
from ..utils.path_utils import should_skip_path


class VerificationResult(NamedTuple):
    """What a verifier concluded about the staged tree."""

    ok: bool
    message: str = ""


Verifier = Callable[["StagedTree"], "VerificationResult | bool | None"]


class StagedFile(NamedTuple):
    """One file's before/after bytes; `None` means absent (create / delete)."""

    path: str
    before: bytes | None
    after: bytes | None


class TransactionOutcome(NamedTuple):
    """The result of `EditTransaction.commit` (or of an undo)."""

    transaction_id: str
    applied: bool
    files: tuple[str, ...]
    diff: str
    verification: VerificationResult
    message: str


class TransactionConflict(ValueError):
    """The working tree no longer matches what the transaction was staged on."""


class TransactionError(ValueError):
    """The transaction could not be prepared (bad path, unreadable file)."""


# One lock per repo root: two transactions committing into the same tree must
# serialise their read-verify-write windows, the same invariant FileEditor keeps
# for single-file writes.
_REPO_LOCKS: dict[str, threading.Lock] = {}
_REPO_LOCKS_GUARD = threading.Lock()


def _repo_lock(root: Path) -> threading.Lock:
    key = str(root)
    with _REPO_LOCKS_GUARD:
        return _REPO_LOCKS.setdefault(key, threading.Lock())


def _decode(data: bytes | None) -> list[str]:
    if data is None:
        return []
    return data.decode(cs.ENCODING_UTF8, errors="replace").splitlines(keepends=True)


def unified_diff(staged: StagedFile) -> str:
    """A unified diff for one staged file, `/dev/null` for create and delete."""
    from_name = cs.DIFF_DEV_NULL if staged.before is None else f"a/{staged.path}"
    to_name = cs.DIFF_DEV_NULL if staged.after is None else f"b/{staged.path}"
    lines = difflib.unified_diff(
        _decode(staged.before),
        _decode(staged.after),
        fromfile=from_name,
        tofile=to_name,
    )
    return "".join(lines)


class StagedTree:
    """What a verifier sees: the working tree with the overlay applied.

    `read` and `exists` answer from the overlay first and the disk second,
    which is all a parse-level check needs. `root` materialises a copy of
    the tree (ignored directories excluded, the overlay written on top) for
    verifiers that must run a real tool against real files; the copy is
    made once, on first use, and removed when the transaction finishes.
    """

    def __init__(self, repo_root: Path, overlay: dict[str, StagedFile]) -> None:
        self.repo_root = repo_root
        self._overlay = overlay
        self._materialised: Path | None = None

    def exists(self, rel_path: str) -> bool:
        staged = self._overlay.get(rel_path)
        if staged is not None:
            return staged.after is not None
        return (self.repo_root / rel_path).is_file()

    def read(self, rel_path: str) -> bytes | None:
        staged = self._overlay.get(rel_path)
        if staged is not None:
            return staged.after
        path = self.repo_root / rel_path
        return path.read_bytes() if path.is_file() else None

    def staged_paths(self) -> tuple[str, ...]:
        return tuple(sorted(self._overlay))

    @property
    def root(self) -> Path:
        if self._materialised is None:
            self._materialised = self._materialise()
        return self._materialised

    def _materialise(self) -> Path:
        target = Path(tempfile.mkdtemp(prefix=cs.EDIT_STAGING_PREFIX))
        patterns = load_ignore_patterns(self.repo_root)
        exclude = patterns.exclude
        unignore = patterns.unignore
        repo_root = self.repo_root

        def ignore(directory: str, names: list[str]) -> set[str]:
            here = Path(directory)
            skipped: set[str] = set()
            for name in names:
                candidate = here / name
                if name in cs.CGR_STATE_FILENAMES:
                    skipped.add(name)
                elif candidate.is_dir() and should_skip_path(
                    candidate, repo_root, exclude, unignore, is_file=False
                ):
                    skipped.add(name)
            return skipped

        shutil.copytree(
            repo_root, target, ignore=ignore, symlinks=True, dirs_exist_ok=True
        )
        for rel_path, staged in self._overlay.items():
            path = target / rel_path
            if staged.after is None:
                path.unlink(missing_ok=True)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(staged.after)
        return target

    def cleanup(self) -> None:
        if self._materialised is not None:
            shutil.rmtree(self._materialised, ignore_errors=True)
            self._materialised = None


class EditTransaction:
    """Stage, verify, then apply a set of file edits atomically."""

    def __init__(self, repo_root: Path, record: bool = True) -> None:
        self.repo_root = repo_root.resolve()
        self.transaction_id = uuid.uuid4().hex[: cs.EDIT_TRANSACTION_ID_LENGTH]
        self._overlay: dict[str, StagedFile] = {}
        self._record = record
        self._finished = False

    # --- staging ----------------------------------------------------------------

    def _relative(self, rel_path: str | Path) -> str:
        path = Path(rel_path)
        if not path.is_absolute():
            path = self.repo_root / path
        try:
            resolved = path.resolve()
            inside = resolved.relative_to(self.repo_root)
        except (OSError, ValueError) as error:
            raise TransactionError(
                ls.FILE_OUTSIDE_ROOT.format(action=cs.FileAction.EDIT)
            ) from error
        return inside.as_posix()

    def _current(self, rel_path: str) -> bytes | None:
        path = self.repo_root / rel_path
        if not path.exists():
            return None
        if not path.is_file():
            raise TransactionError(cs.EDIT_NOT_A_FILE.format(path=rel_path))
        return path.read_bytes()

    def read(self, rel_path: str | Path) -> bytes | None:
        """The file as the transaction currently sees it (overlay first)."""
        key = self._relative(rel_path)
        staged = self._overlay.get(key)
        return staged.after if staged is not None else self._current(key)

    def stage(self, rel_path: str | Path, content: str | bytes | None) -> StagedFile:
        """Set a file's full content; `None` deletes it. Later stages win."""
        self._check_open()
        key = self._relative(rel_path)
        after = (
            content.encode(cs.ENCODING_UTF8) if isinstance(content, str) else content
        )
        existing = self._overlay.get(key)
        before = existing.before if existing is not None else self._current(key)
        staged = StagedFile(key, before, after)
        if before == after:
            # Staging what is already there is not an edit; forgetting a
            # no-op also lets a later real stage of the same path start
            # from the disk state again.
            self._overlay.pop(key, None)
        else:
            self._overlay[key] = staged
        return staged

    def unstage(self, rel_path: str | Path) -> None:
        self._overlay.pop(self._relative(rel_path), None)

    @property
    def staged(self) -> tuple[StagedFile, ...]:
        return tuple(self._overlay[k] for k in sorted(self._overlay))

    def diff(self) -> str:
        """The combined unified diff of everything staged."""
        return "".join(unified_diff(s) for s in self.staged)

    def tree(self) -> StagedTree:
        return StagedTree(self.repo_root, dict(self._overlay))

    # --- commit / rollback ------------------------------------------------------

    def rollback(self) -> None:
        """Discard the staging; the tree was never touched."""
        self._overlay.clear()
        self._finished = True

    def _check_open(self) -> None:
        if self._finished:
            raise TransactionError(cs.EDIT_TRANSACTION_FINISHED)

    def _check_unchanged(self) -> None:
        # Verification and application read the tree as it is NOW; a file
        # that moved since it was staged would make the diff a lie and the
        # undo record wrong, so the whole transaction refuses.
        for staged in self._overlay.values():
            if self._current(staged.path) != staged.before:
                raise TransactionConflict(cs.EDIT_CONFLICT.format(path=staged.path))

    def commit(self, verify: Verifier | None = None) -> TransactionOutcome:
        """Verify the staged tree, then apply every file or none of them."""
        self._check_open()
        diff = self.diff()
        files = tuple(sorted(self._overlay))
        if not files:
            self._finished = True
            return TransactionOutcome(
                self.transaction_id,
                False,
                (),
                "",
                VerificationResult(True),
                cs.EDIT_NOTHING_STAGED,
            )
        with _repo_lock(self.repo_root):
            self._check_unchanged()
            tree = self.tree()
            try:
                verification = _run_verifier(verify, tree)
            finally:
                tree.cleanup()
            if not verification.ok:
                self.rollback()
                logger.info(
                    ls.EDIT_TX_REJECTED,
                    tx=self.transaction_id,
                    why=verification.message,
                )
                return TransactionOutcome(
                    self.transaction_id,
                    False,
                    files,
                    diff,
                    verification,
                    cs.EDIT_VERIFICATION_FAILED.format(reason=verification.message),
                )
            self._apply_all()
            if self._record:
                record_transaction(
                    self.repo_root, self.transaction_id, self.staged, verification
                )
        self._finished = True
        logger.info(ls.EDIT_TX_APPLIED, tx=self.transaction_id, count=len(files))
        return TransactionOutcome(
            self.transaction_id,
            True,
            files,
            diff,
            verification,
            cs.EDIT_APPLIED.format(count=len(files)),
        )

    def _apply_all(self) -> None:
        done: list[StagedFile] = []
        try:
            for staged in self.staged:
                _write_file(self.repo_root / staged.path, staged.after)
                done.append(staged)
        except Exception:
            # Best-effort restore of what already landed, so the caller sees
            # either every file or the tree it started from.
            for staged in reversed(done):
                try:
                    _write_file(self.repo_root / staged.path, staged.before)
                except OSError as error:  # pragma: no cover - disk failure
                    logger.error(
                        ls.EDIT_TX_RESTORE_FAILED, path=staged.path, error=error
                    )
            raise


def _run_verifier(verify: Verifier | None, tree: StagedTree) -> VerificationResult:
    if verify is None:
        return VerificationResult(True)
    try:
        result = verify(tree)
    except Exception as error:
        return VerificationResult(False, cs.EDIT_VERIFIER_RAISED.format(error=error))
    if result is None or result is True:
        return VerificationResult(True)
    if result is False:
        return VerificationResult(False, cs.EDIT_VERIFIER_FALSE)
    return result


def _write_file(path: Path, content: bytes | None) -> None:
    if content is None:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f"{path.name}{cs.TMP_EXTENSION}")
    try:
        temp_path.write_bytes(content)
        os.replace(temp_path, path)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise


# --- history ------------------------------------------------------------------


def history_path(repo_root: Path) -> Path:
    return repo_root / cs.EDIT_HISTORY_FILENAME


def _b64(data: bytes | None) -> str | None:
    return None if data is None else base64.b64encode(data).decode("ascii")


def _unb64(data: str | None) -> bytes | None:
    return None if data is None else base64.b64decode(data)


def load_history(repo_root: Path) -> list[dict]:
    path = history_path(repo_root)
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding=cs.ENCODING_UTF8))
    except (OSError, ValueError):
        return []
    return data if isinstance(data, list) else []


def _save_history(repo_root: Path, entries: list[dict]) -> None:
    _write_file(
        history_path(repo_root),
        json.dumps(entries[-cs.EDIT_HISTORY_LIMIT :], indent=2).encode(
            cs.ENCODING_UTF8
        ),
    )


def record_transaction(
    repo_root: Path,
    transaction_id: str,
    staged: tuple[StagedFile, ...],
    verification: VerificationResult,
) -> None:
    entries = load_history(repo_root)
    entries.append(
        {
            cs.EDIT_KEY_ID: transaction_id,
            cs.EDIT_KEY_AT: datetime.now(UTC).isoformat(timespec="seconds"),
            cs.EDIT_KEY_FILES: [
                {
                    cs.KEY_PATH: s.path,
                    cs.EDIT_KEY_BEFORE: _b64(s.before),
                    cs.EDIT_KEY_AFTER: _b64(s.after),
                }
                for s in staged
            ],
            cs.EDIT_KEY_VERIFICATION: {
                cs.EDIT_KEY_OK: verification.ok,
                cs.EDIT_KEY_MESSAGE: verification.message,
            },
        }
    )
    _save_history(repo_root, entries)


def entry_files(entry: dict) -> list[StagedFile]:
    return [
        StagedFile(
            f[cs.KEY_PATH],
            _unb64(f.get(cs.EDIT_KEY_BEFORE)),
            _unb64(f.get(cs.EDIT_KEY_AFTER)),
        )
        for f in entry.get(cs.EDIT_KEY_FILES, [])
    ]


def entry_diff(entry: dict) -> str:
    return "".join(unified_diff(s) for s in entry_files(entry))


def undo_last(repo_root: Path, count: int = 1) -> list[TransactionOutcome]:
    """Reverse the last `count` recorded transactions, newest first.

    Each undo is itself a transaction staging `after -> before`; it refuses
    (and stops the run) if a file no longer holds what the transaction
    wrote, so an undo never clobbers later hand edits.
    """
    root = repo_root.resolve()
    outcomes: list[TransactionOutcome] = []
    for _ in range(count):
        entries = load_history(root)
        if not entries:
            break
        entry = entries[-1]
        reverse = EditTransaction(root, record=False)
        for staged in entry_files(entry):
            reverse._overlay[staged.path] = StagedFile(
                staged.path, staged.after, staged.before
            )
        outcome = reverse.commit()
        outcomes.append(
            outcome._replace(transaction_id=str(entry.get(cs.EDIT_KEY_ID, "")))
        )
        if not outcome.applied and outcome.message != cs.EDIT_NOTHING_STAGED:
            break
        _save_history(root, entries[:-1])
    return outcomes


@contextmanager
def transaction(repo_root: Path) -> Iterator[EditTransaction]:
    """`with transaction(root) as tx: tx.stage(...)`; rolls back on exception."""
    tx = EditTransaction(repo_root)
    try:
        yield tx
    except BaseException:
        if not tx._finished:
            tx.rollback()
        raise
