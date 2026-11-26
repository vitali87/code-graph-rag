"""The Rust engine's frame against the Python code it replaces.

Phase 2 of the Rust rewrite (docs/reports/PHASE2_FRAME.md) builds the frame
in steps behind `engine_frame.ENABLED`. Each part must give exactly what the
Python engine gives, on any number of threads. The first part is the hash
the sync records for each file.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import codebase_rag
from codebase_rag import engine_frame
from codebase_rag.graph_updater import _hash_file


@pytest.fixture
def engine() -> engine_frame.Engine:
    made = engine_frame.new_engine(1)
    # The checkout builds its own crate, so the frame is always there.
    assert made is not None, "the extension lacks the engine; rebuild the crate"
    return made


def _files(root: Path) -> list[Path]:
    contents: dict[str, bytes] = {
        "empty.py": b"",
        "crlf.py": b"x = 1\r\ny = 2\r\n",
        "bom.py": b"\xef\xbb\xbfx = 1\n",
        "binary.bin": bytes(range(256)) * 3,
        # Larger than several of the engine's reads.
        "large.txt": b"0123456789abcdef" * 20_000,
        "näme.py": "café = 1\n".encode(),
        "deep/a/b/c.go": b"package c\n",
    }
    paths = []
    for rel, data in contents.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        paths.append(path)
    return paths


def test_it_is_off_until_the_last_step() -> None:
    # The last step (section 13's step 10) has landed: on, with no check in
    # place, as users run it. The `both` cells turn it off for their whole
    # session, and they and the `rust` cells put a check in place
    # (engine_modes.py), so the module's own assignments are what hold the
    # defaults.
    tree = ast.parse(Path(engine_frame.__file__).read_text(encoding="utf-8"))
    defaults = {
        node.target.id: node.value.value
        for node in tree.body
        if isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and isinstance(node.value, ast.Constant)
    }
    assert defaults == {"ENABLED": True, "CHECK": None}


def test_the_hashes_are_the_ones_the_sync_records(
    tmp_path: Path, engine: engine_frame.Engine
) -> None:
    paths = _files(tmp_path)

    assert engine.md5_files(paths) == [_hash_file(path) for path in paths]


def test_a_file_that_cannot_be_read_has_no_hash(
    tmp_path: Path, engine: engine_frame.Engine
) -> None:
    present = tmp_path / "present.py"
    present.write_bytes(b"x = 1\n")

    hashes = engine.md5_files([tmp_path / "absent.py", tmp_path, present])

    assert hashes == [None, None, _hash_file(present)]


@pytest.mark.parametrize("threads", [0, 1, 2, 3])
def test_any_number_of_threads_gives_the_same_hashes_in_order(
    threads: int, tmp_path: Path
) -> None:
    # cgr's own sources: hundreds of files of every size, plus the fixture's.
    # 0 is the default pool, one thread per core the process may use.
    package = Path(codebase_rag.__file__).parent
    paths = sorted(package.rglob("*.py")) + _files(tmp_path)
    made = engine_frame.new_engine(threads)
    assert made is not None

    assert made.threads == threads or (threads == 0 and made.threads >= 1)
    assert made.md5_files(paths) == [_hash_file(path) for path in paths]


def test_zero_threads_gives_the_default_pool() -> None:
    # Its exact size is the Rust side's `available_parallelism`, which counts
    # the CPUs this process may use (affinity, cgroup quota); `os.cpu_count()`
    # counts the machine's, so only Rust's own test checks the number.
    made = engine_frame.new_engine(0)
    assert made is not None

    assert made.threads >= 1


def test_the_shared_engine_is_made_once() -> None:
    first = engine_frame.engine()

    assert first is not None
    assert engine_frame.engine() is first
    default = engine_frame.new_engine(0)
    assert default is not None
    assert first.threads == default.threads


def test_without_the_extensions_engine_there_is_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A build of cgr-graph-rs from before phase 2 has no `Engine`.
    monkeypatch.setattr(engine_frame, "rust_extension", object)

    assert engine_frame.new_engine() is None
