import subprocess
import sys
import time
from pathlib import Path

BENCHMARKS = [
    "bench_string_ops.py",
    "bench_trie.py",
    "bench_find_ending_with_fix.py",
    "bench_dropin_replacements.py",
    "bench_graph_loader.py",
    "bench_file_hashing.py",
    "bench_embedding_cache.py",
    "bench_json_serialization.py",
    "bench_ast_cache.py",
    "bench_pathlib_vs_string.py",
]


def main() -> None:
    bench_dir = Path(__file__).parent
    results_dir = bench_dir / "results"
    results_dir.mkdir(exist_ok=True)

    timestamp = time.strftime("%Y%m%d_%H%M%S")
    overall_start = time.perf_counter()

    print(f"Running {len(BENCHMARKS)} benchmark suites")
    print(f"Results will be saved to: {results_dir}")
    print(f"Timestamp: {timestamp}")
    print("=" * 80)

    for bench_file in BENCHMARKS:
        bench_path = bench_dir / bench_file
        if not bench_path.exists():
            print(f"SKIP: {bench_file} (not found)")
            continue

        result_file = results_dir / f"{bench_path.stem}_{timestamp}.txt"
        print(f"\nRunning: {bench_file}")

        start = time.perf_counter()
        result = subprocess.run(
            [sys.executable, str(bench_path)],
            capture_output=True,
            text=True,
            timeout=600,
        )
        elapsed = time.perf_counter() - start

        output = result.stdout
        if result.returncode != 0:
            output += f"\nSTDERR:\n{result.stderr}"
            print(f"  FAILED (exit code {result.returncode}, {elapsed:.1f}s)")
        else:
            print(f"  OK ({elapsed:.1f}s)")

        with result_file.open("w") as f:
            f.write(f"Benchmark: {bench_file}\n")
            f.write(f"Timestamp: {timestamp}\n")
            f.write(f"Exit code: {result.returncode}\n")
            f.write(f"Duration: {elapsed:.1f}s\n")
            f.write(f"Python: {sys.version}\n")
            f.write("=" * 80 + "\n")
            f.write(output)

    total = time.perf_counter() - overall_start
    print(f"\n{'='*80}")
    print(f"All benchmarks completed in {total:.1f}s")
    print(f"Results saved in: {results_dir}")


if __name__ == "__main__":
    main()
