"""The file-hashing benchmark compares buffer sizes, mmap and algorithms. The
sha256 rows are only comparable if the three variants compute the same digest,
and every row only counts if it hashed every file main wrote."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest

from benchmarks import bench_file_hashing as bench

_ROW = re.compile(r"^(?P<name>.+?) +(?:\d+\.\d{3}ms *){6}$", re.MULTILINE)
_OPERATIONS = {"sha256 8KB buf", "sha256 64KB buf", "sha256 mmap", "md5", "blake2b"}


def test_test_files_have_the_requested_count_and_size(tmp_path: Path) -> None:
    files = bench.create_test_files(str(tmp_path), 3, 2)
    assert [f.name for f in files] == ["file_0.py", "file_1.py", "file_2.py"]
    assert all(f.stat().st_size == 2048 for f in files)
    # Random bytes, so no two files hash alike and nothing is deduplicated.
    assert len({f.read_bytes() for f in files}) == 3


@pytest.mark.parametrize("size_kb", [1, 70], ids=["one-chunk", "many-chunks"])
def test_every_variant_agrees_with_hashlib(tmp_path: Path, size_kb: int) -> None:
    (path,) = bench.create_test_files(str(tmp_path), 1, size_kb)
    content = path.read_bytes()
    expected = hashlib.sha256(content).hexdigest()
    assert bench.hash_file_sha256(path) == expected
    assert bench.hash_file_sha256_large_buffer(path) == expected
    assert bench.hash_file_sha256_mmap(path) == expected
    assert (
        bench.hash_file_md5(path)
        == hashlib.md5(content, usedforsecurity=False).hexdigest()
    )
    assert bench.hash_file_blake2b(path) == hashlib.blake2b(content).hexdigest()


def test_a_timed_pass_hashes_every_file(tmp_path: Path) -> None:
    files = bench.create_test_files(str(tmp_path), 4, 1)
    seen: list[Path] = []

    def record(path: Path) -> str:
        seen.append(path)
        return ""

    assert bench.bench_hash_files(files, record) >= 0
    assert seen == files


def test_main_times_every_algorithm_at_every_size(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(bench, "WARMUP_RUNS", 0)
    monkeypatch.setattr(bench, "BENCH_RUNS", 1)
    create = bench.create_test_files
    # main's sizes are local to it (up to 500 files of 20KB), so the file
    # writer is capped instead.
    monkeypatch.setattr(
        bench,
        "create_test_files",
        lambda directory, count, size_kb: create(directory, min(count, 3), 1),
    )

    bench.main()

    out = capsys.readouterr().out
    names = [match["name"] for match in _ROW.finditer(out)]
    assert len(names) == 3 * len(_OPERATIONS)
    assert {name.rsplit(" (", 1)[0] for name in names} == _OPERATIONS
    assert out.count("Total data: 0.0 MB") == 3
