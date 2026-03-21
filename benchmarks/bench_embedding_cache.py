import hashlib
import random
import statistics
import string
import time

from codebase_rag.embedder import EmbeddingCache

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
    print(f"\n{'Benchmark':<40} {'Median':>10} {'Mean':>10} {'StdDev':>10} {'Min':>10} {'Max':>10} {'P95':>10}")
    print("-" * 110)
    for r in results:
        print(
            f"{r['name']:<40} {r['median_ms']:>9.3f}ms {r['mean_ms']:>9.3f}ms "
            f"{r['stddev_ms']:>9.3f}ms {r['min_ms']:>9.3f}ms {r['max_ms']:>9.3f}ms "
            f"{r['p95_ms']:>9.3f}ms"
        )


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
