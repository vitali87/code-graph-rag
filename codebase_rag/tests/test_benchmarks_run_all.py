"""`benchmarks/run_all.py` runs each listed suite as a module of the
`benchmarks` package and files its output under `results/`. A listed suite
that is missing is skipped with one line, and one that is not runnable as a
module prints nothing and still reads OK, so both failures are silent; the
list and the dispatch are pinned here without running the real suites."""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from benchmarks import run_all

BENCH_DIR = Path(run_all.__file__).resolve().parent


def _main_needs_no_arguments(tree: ast.Module) -> bool:
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "main":
            args = node.args
            positional = [*args.posonlyargs, *args.args]
            # `kw_defaults` holds None for a keyword-only argument without one.
            return (
                len(positional) == len(args.defaults) and None not in args.kw_defaults
            )
    return False


def _runs_main_as_a_script(tree: ast.Module) -> bool:
    for node in tree.body:
        if (
            isinstance(node, ast.If)
            and isinstance(node.test, ast.Compare)
            and isinstance(node.test.left, ast.Name)
            and node.test.left.id == "__name__"
        ):
            return any(
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Name)
                and call.func.id == "main"
                for call in ast.walk(node)
            )
    return False


@pytest.mark.parametrize("bench_file", run_all.BENCHMARKS)
def test_every_listed_suite_runs_as_a_module_with_no_arguments(
    bench_file: str,
) -> None:
    path = BENCH_DIR / bench_file
    assert path.is_file(), f"{bench_file} is listed but run_all would skip it"
    # Parsed rather than imported: importing every suite would pull in torch
    # through the embedding cache suite, and only the shape matters here.
    tree = ast.parse(path.read_text(encoding="utf-8"))
    assert _main_needs_no_arguments(tree), f"{bench_file}'s main needs arguments"
    assert _runs_main_as_a_script(tree), f"`-m` would import {bench_file} and stop"


def test_suites_run_in_order_and_each_result_is_filed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    bench_dir = tmp_path / "benchmarks"
    bench_dir.mkdir()
    for name in ("bench_good.py", "bench_bad.py"):
        (bench_dir / name).write_text("", encoding="utf-8")
    calls: list[tuple[list[str], Path]] = []

    def run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert kwargs["capture_output"] is True
        assert kwargs["text"] is True
        calls.append((args, Path(str(kwargs["cwd"]))))
        failed = args[-1].endswith("bad")
        return subprocess.CompletedProcess(
            args, 2 if failed else 0, stdout=f"table of {args[-1]}\n", stderr="boom"
        )

    # main locates the suites and the results directory from its own path.
    monkeypatch.setattr(run_all, "__file__", str(bench_dir / "run_all.py"))
    monkeypatch.setattr(
        run_all, "BENCHMARKS", ["bench_good.py", "bench_gone.py", "bench_bad.py"]
    )
    monkeypatch.setattr(run_all, "subprocess", SimpleNamespace(run=run))

    run_all.main()

    assert calls == [
        ([sys.executable, "-m", "benchmarks.bench_good"], tmp_path),
        ([sys.executable, "-m", "benchmarks.bench_bad"], tmp_path),
    ]
    out = capsys.readouterr().out
    assert "SKIP: bench_gone.py (not found)" in out
    assert "  FAILED (exit code 2, " in out
    assert out.count("  OK (") == 1

    results = sorted((bench_dir / "results").iterdir())
    assert [r.name.rsplit("_", 2)[0] for r in results] == ["bench_bad", "bench_good"]
    bad, good = (r.read_text(encoding="utf-8") for r in results)
    assert "Exit code: 0\n" in good
    assert good.endswith("table of benchmarks.bench_good\n")
    assert "STDERR" not in good
    assert "Exit code: 2\n" in bad
    assert bad.endswith("table of benchmarks.bench_bad\n\nSTDERR:\nboom")
