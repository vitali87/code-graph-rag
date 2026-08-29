import hashlib
import random
import string
import sys
import time
from pathlib import Path

from codebase_rag.embedder import EmbeddingCache

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness  # noqa: E402

WARMUP_RUNS = 3
BENCH_RUNS = 50
EMBEDDING_DIM = 768


def generate_snippets(count: int, avg_length: int = 200) -> list[str]:
    snippets = []
    for i in range(count):
        length = avg_length + random.randint(-50, 50)
        snippet = "".join(random.choices(string.ascii_letters + string.digits + " \n\t", k=length))
        snippets.append(snippet)
    return snippets


def generate_embedding() -> list[float]:
    return [random.random() for _ in range(EMBEDDING_DIM)]


def bench_sha256_hashing(snippets: list[str]) -> float:
    start = time.perf_counter()
    for s in snippets:
        _ = hashlib.sha256(s.encode()).hexdigest()
    return time.perf_counter() - start


def bench_cache_put(cache: EmbeddingCache, snippets: list[str], embeddings: list[list[float]]) -> float:
    start = time.perf_counter()
    for s, e in zip(snippets, embeddings):
        cache.put(s, e)
    return time.perf_counter() - start


def bench_cache_get_hit(cache: EmbeddingCache, snippets: list[str]) -> float:
    start = time.perf_counter()
    for s in snippets:
        _ = cache.get(s)
    return time.perf_counter() - start


def bench_cache_get_miss(cache: EmbeddingCache, miss_snippets: list[str]) -> float:
    start = time.perf_counter()
    for s in miss_snippets:
        _ = cache.get(s)
    return time.perf_counter() - start


def bench_cache_get_many(cache: EmbeddingCache, snippets: list[str]) -> float:
    start = time.perf_counter()
    _ = cache.get_many(snippets)
    return time.perf_counter() - start


def run_benchmark(name: str, func, *args) -> dict[str, float]:
    return harness.run_benchmark(
        name, func, *args, warmup_runs=WARMUP_RUNS, bench_runs=BENCH_RUNS
    )


def print_results(results: list[dict[str, float]]) -> None:
    harness.print_results(results, 40)


def main() -> None:
    random.seed(42)

    sizes = [500, 2000, 10000]

    for size in sizes:
        print(f"\n{'='*110}")
        print(f"EmbeddingCache Benchmark (n={size})")
        print(f"{'='*110}")

        snippets = generate_snippets(size)
        embeddings = [generate_embedding() for _ in range(size)]
        miss_snippets = generate_snippets(size, avg_length=300)

        results = []

        r = run_benchmark(f"sha256 hashing ({size})", bench_sha256_hashing, snippets)
        results.append(r)

        cache = EmbeddingCache()
        r = run_benchmark(f"cache.put ({size})", bench_cache_put, cache, snippets, embeddings)
        results.append(r)

        cache = EmbeddingCache()
        cache.put_many(snippets, embeddings)

        r = run_benchmark(f"cache.get hit ({size})", bench_cache_get_hit, cache, snippets)
        results.append(r)

        r = run_benchmark(f"cache.get miss ({size})", bench_cache_get_miss, cache, miss_snippets)
        results.append(r)

        r = run_benchmark(f"cache.get_many ({size})", bench_cache_get_many, cache, snippets)
        results.append(r)

        print_results(results)


if __name__ == "__main__":
    main()
