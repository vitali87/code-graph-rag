"""The pathlib-vs-string benchmark races pathlib against string slicing on
generated paths. The string shortcuts only answer what pathlib answers when
every path lies under the root and carries its extension in its last segment,
so those preconditions of the generated paths are what is pinned here."""

from __future__ import annotations

import re
from pathlib import PurePosixPath

import pytest

from benchmarks import bench_pathlib_vs_string as bench

_ROW = re.compile(r"^(?P<name>.+?) +(?:\d+\.\d{3}ms *){6}$", re.MULTILINE)
_RATIO = re.compile(r"^  -> .+: \d+(?:\.\d)?x slower$", re.MULTILINE)
_ROOT = "/repo/root"
_OPERATIONS = {
    "pathlib.relative_to",
    "str.removeprefix",
    "os.path.relpath",
    "pathlib should_skip",
    "string should_skip",
    "Path.suffix",
    "str.rfind",
    "os.path.splitext",
    "Path.name",
    "str.rfind+slice",
}


def test_paths_lie_under_the_root_with_their_extension_last() -> None:
    paths = bench.generate_file_paths(_ROOT, 400)
    assert len(set(paths)) == 400
    assert paths[0] == f"{_ROOT}/src/base/module_0.py"
    for path in paths:
        relative = path.removeprefix(f"{_ROOT}/")
        assert relative == PurePosixPath(path).relative_to(_ROOT).as_posix()
        # The string variant takes everything after the last dot of the whole
        # path, which is the suffix only while that dot is in the file name.
        assert path.rfind(".") > path.rfind("/")
    # 8 directories x 6 subdirectories cycle before the extension moves on,
    # so 400 paths reach all seven languages.
    assert {PurePosixPath(p).suffix for p in paths} == {
        ".py",
        ".js",
        ".ts",
        ".rs",
        ".go",
        ".java",
        ".cpp",
    }


def test_no_generated_path_is_skipped() -> None:
    # A path the skip check rejects early would time less than the full scan
    # of its parts both variants are meant to race.
    skip = set(bench.generate_skip_patterns())
    for path in bench.generate_file_paths(_ROOT, 400):
        assert not skip.intersection(PurePosixPath(path).parts)


def test_the_pathlib_skip_check_tolerates_a_path_outside_the_root() -> None:
    elapsed = bench.bench_pathlib_should_skip(
        ["/elsewhere/a.py"], _ROOT, bench.generate_skip_patterns()
    )
    assert elapsed >= 0


def test_every_timed_operation_reports_elapsed_seconds() -> None:
    paths = bench.generate_file_paths(_ROOT, 10)
    skip = bench.generate_skip_patterns()
    for elapsed in (
        bench.bench_pathlib_relative_to(paths, _ROOT),
        bench.bench_string_removeprefix(paths, _ROOT),
        bench.bench_os_path_relpath(paths, _ROOT),
        bench.bench_pathlib_should_skip(paths, _ROOT, skip),
        bench.bench_string_should_skip(paths, _ROOT, skip),
        bench.bench_pathlib_suffix_check(paths),
        bench.bench_string_suffix_check(paths),
        bench.bench_os_path_splitext(paths),
        bench.bench_pathlib_name(paths),
        bench.bench_string_name(paths),
    ):
        assert 0 <= elapsed < 1


def test_main_times_every_operation_at_every_size(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(bench, "WARMUP_RUNS", 0)
    monkeypatch.setattr(bench, "BENCH_RUNS", 1)
    generate = bench.generate_file_paths
    # main's sizes are local to it (up to 59012 paths), so the generator is
    # capped instead.
    monkeypatch.setattr(
        bench,
        "generate_file_paths",
        lambda repo_root, count: generate(repo_root, min(count, 200)),
    )

    bench.main()

    out = capsys.readouterr().out
    names = [match["name"] for match in _ROW.finditer(out)]
    assert len(names) == 4 * len(_OPERATIONS)
    assert {name.rsplit(" (", 1)[0] for name in names} == _OPERATIONS
    assert len(_RATIO.findall(out)) == 4 * 5
