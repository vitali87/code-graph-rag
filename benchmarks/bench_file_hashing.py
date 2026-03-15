import hashlib
import os
import statistics
import tempfile
import time
from pathlib import Path

WARMUP_RUNS = 3
BENCH_RUNS = 30


def create_test_files(directory: str, count: int, avg_size_kb: int) -> list[Path]:
    paths = []
    for i in range(count):
        path = Path(directory) / f"file_{i}.py"
        content = os.urandom(avg_size_kb * 1024)
        path.write_bytes(content)
        paths.append(path)
    return paths


def hash_file_sha256(filepath: Path) -> str:
    hasher = hashlib.sha256()
    with filepath.open("rb") as f:
        while chunk := f.read(8192):
            hasher.update(chunk)
    return hasher.hexdigest()


def hash_file_sha256_large_buffer(filepath: Path) -> str:
    hasher = hashlib.sha256()
    with filepath.open("rb") as f:
        while chunk := f.read(65536):
            hasher.update(chunk)
    return hasher.hexdigest()


def hash_file_sha256_mmap(filepath: Path) -> str:
    import mmap
    hasher = hashlib.sha256()
    with filepath.open("rb") as f:
        with mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mm:
            hasher.update(mm)
    return hasher.hexdigest()


def hash_file_md5(filepath: Path) -> str:
    hasher = hashlib.md5()
    with filepath.open("rb") as f:
        while chunk := f.read(8192):
            hasher.update(chunk)
    return hasher.hexdigest()


def hash_file_blake2b(filepath: Path) -> str:
    hasher = hashlib.blake2b()
    with filepath.open("rb") as f:
        while chunk := f.read(8192):
            hasher.update(chunk)
    return hasher.hexdigest()


def bench_hash_files(files: list[Path], hash_func) -> float:
    start = time.perf_counter()
    for f in files:
        _ = hash_func(f)
    return time.perf_counter() - start


def run_benchmark(name: str, func, *args) -> dict[str, float]:
    for _ in range(WARMUP_RUNS):
        func(*args)

    times = []
    for _ in range(BENCH_RUNS):
        times.append(func(*args))

    return {
        "name": name,
        "median_ms": statistics.median(times) * 1000,
        "mean_ms": statistics.mean(times) * 1000,
        "stddev_ms": statistics.stdev(times) * 1000 if len(times) > 1 else 0,
        "min_ms": min(times) * 1000,
        "max_ms": max(times) * 1000,
        "p95_ms": sorted(times)[int(len(times) * 0.95)] * 1000,
    }


def print_results(results: list[dict[str, float]]) -> None:
    print(f"\n{'Benchmark':<45} {'Median':>10} {'Mean':>10} {'StdDev':>10} {'Min':>10} {'Max':>10} {'P95':>10}")
    print("-" * 115)
    for r in results:
        print(
            f"{r['name']:<45} {r['median_ms']:>9.3f}ms {r['mean_ms']:>9.3f}ms "
            f"{r['stddev_ms']:>9.3f}ms {r['min_ms']:>9.3f}ms {r['max_ms']:>9.3f}ms "
            f"{r['p95_ms']:>9.3f}ms"
        )


def main() -> None:
    configs = [
        (50, 5),
        (200, 10),
        (500, 20),
    ]

    for file_count, avg_size_kb in configs:
        print(f"\n{'='*115}")
        print(f"File Hashing Benchmark (files={file_count}, avg_size={avg_size_kb}KB)")
        print(f"{'='*115}")

        with tempfile.TemporaryDirectory() as tmpdir:
            files = create_test_files(tmpdir, file_count, avg_size_kb)
            total_mb = sum(f.stat().st_size for f in files) / (1024 * 1024)
            print(f"Total data: {total_mb:.1f} MB")

            results = []

            r = run_benchmark(f"sha256 8KB buf ({file_count}f)", bench_hash_files, files, hash_file_sha256)
            results.append(r)

            r = run_benchmark(f"sha256 64KB buf ({file_count}f)", bench_hash_files, files, hash_file_sha256_large_buffer)
            results.append(r)

            r = run_benchmark(f"sha256 mmap ({file_count}f)", bench_hash_files, files, hash_file_sha256_mmap)
            results.append(r)

            r = run_benchmark(f"md5 ({file_count}f)", bench_hash_files, files, hash_file_md5)
            results.append(r)

            r = run_benchmark(f"blake2b ({file_count}f)", bench_hash_files, files, hash_file_blake2b)
            results.append(r)

            print_results(results)


if __name__ == "__main__":
    main()
