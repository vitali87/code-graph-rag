"""`write_bytes_atomic` / `write_text_atomic`: a file replaced in one step."""

import os
import stat
import sys
from pathlib import Path

import pytest

from codebase_rag.utils import atomic_write
from codebase_rag.utils.atomic_write import (
    open_temp_sibling,
    write_bytes_atomic,
    write_text_atomic,
)

_POSIX_ONLY = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX permission bits and symlinks"
)


def _siblings(path: Path) -> list[str]:
    return sorted(entry.name for entry in path.parent.iterdir())


def test_writes_a_new_file(tmp_path: Path) -> None:
    target = tmp_path / "state.json"

    write_bytes_atomic(target, b"{}")

    assert target.read_bytes() == b"{}"
    assert _siblings(target) == ["state.json"]


def test_replaces_an_existing_file_whole(tmp_path: Path) -> None:
    target = tmp_path / "state.json"
    target.write_bytes(b"a much longer previous content")

    write_bytes_atomic(target, b"short")

    assert target.read_bytes() == b"short"
    assert _siblings(target) == ["state.json"]


def test_a_failed_replace_leaves_the_previous_file_and_no_temporary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "state.json"
    target.write_bytes(b"previous")

    def _refuse(_src: str, _dst: str) -> None:
        raise OSError("read-only file system")

    monkeypatch.setattr(atomic_write.os, "replace", _refuse)
    with pytest.raises(OSError, match="read-only"):
        write_bytes_atomic(target, b"new")

    assert target.read_bytes() == b"previous"
    assert _siblings(target) == ["state.json"]


@_POSIX_ONLY
def test_keeps_an_existing_files_mode(tmp_path: Path) -> None:
    target = tmp_path / "run.sh"
    target.write_bytes(b"#!/bin/sh\n")
    target.chmod(0o750)

    write_bytes_atomic(target, b"#!/bin/sh\necho hi\n")

    assert stat.S_IMODE(target.stat().st_mode) == 0o750


@_POSIX_ONLY
def test_a_new_file_gets_the_mode_a_plain_write_gives(tmp_path: Path) -> None:
    plain = tmp_path / "plain.txt"
    plain.write_text("x")
    atomic = tmp_path / "atomic.txt"

    write_text_atomic(atomic, "x")

    assert stat.S_IMODE(atomic.stat().st_mode) == stat.S_IMODE(plain.stat().st_mode)


@_POSIX_ONLY
def test_a_symlink_at_the_path_is_replaced_not_written_through(
    tmp_path: Path,
) -> None:
    elsewhere = tmp_path / "elsewhere.txt"
    elsewhere.write_text("untouched")
    target = tmp_path / "state.json"
    target.symlink_to(elsewhere)

    write_text_atomic(target, "new")

    assert not target.is_symlink()
    assert target.read_text() == "new"
    assert elsewhere.read_text() == "untouched"


def test_text_is_written_as_utf8_with_the_platforms_newlines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "notes.txt"
    monkeypatch.setattr(atomic_write.os, "linesep", "\r\n")

    write_text_atomic(target, "é\nz\n")

    assert target.read_bytes() == "é\r\nz\r\n".encode()


def test_the_temporary_is_a_fresh_named_sibling(tmp_path: Path) -> None:
    target = tmp_path / "cache.json"

    descriptor, temp = open_temp_sibling(target, 0o600)
    os.close(descriptor)

    assert temp.parent == tmp_path
    assert temp.name.startswith("cache.json.")
    assert temp.name.endswith(".tmp")
    assert not target.exists()
