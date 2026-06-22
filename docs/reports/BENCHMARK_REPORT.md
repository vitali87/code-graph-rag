# Benchmark Report: Measured vs Projected Performance

## Methodology

All benchmarks ran on macOS (Darwin 25.3.0), Python 3.12, using `uv run`. Each benchmark used:
- 3 warmup runs (discarded)
- 20 to 100 measured iterations (depending on benchmark)
- Statistical measures: median, mean, stddev, min, max, p95
- Realistic data sizes matching the profiled workload (352 files, ~4,500 registry entries)

Benchmark scripts are in `benchmarks/`. Run all with `uv run python benchmarks/run_all.py`.

---

## FINDING 1: `find_ending_with` Linear Scan (48.3% of CPU)

**The single biggest performance win available, requiring zero dependencies.**

The `FunctionRegistryTrie.find_ending_with()` method falls back to a linear scan of all entries when the `_simple_name_lookup` index misses (80.7% miss rate per profiling data).

### Measured Results

| Scenario | Registry Size | Queries | Linear Scan (ms) | Full Suffix Index (ms) | Speedup |
|---|---|---|---|---|---|
| Batch lookup | 1,000 | 38 | 1.77 | 0.007 | **261x** |
| Batch lookup | 4,500 | 38 | 8.04 | 0.023 | **356x** |
| Batch lookup | 10,000 | 38 | 17.78 | 0.046 | **382x** |
| Single lookup | 4,500 | 1 | 0.22 | 0.001 | **178x** |

### Projected vs Measured

The integration feasibility report projected ~1.9x total speedup (saving 13.5s of 31.2s). Our benchmarks show that building a complete suffix index provides **178x to 382x speedup** on the specific operation, validating the projection and suggesting the total improvement could be even larger than estimated.

### Fix

Build a complete suffix index in `FunctionRegistryTrie` by populating `_simple_name_lookup` for every insert, and ensure all insertion code paths (including `__setitem__`) update the index. This eliminates the linear scan fallback entirely.

---

## FINDING 2: pathlib vs String Operations (13.7% of CPU)

**The `should_skip_path` function uses `pathlib.Path.relative_to()` which creates intermediate objects on every call.**

### Measured Results

| Operation | pathlib (ms) | String ops (ms) | Speedup |
|---|---|---|---|
| `relative_to` vs `removeprefix` (5,000 paths) | 61.3 | 0.097 | **634x** |
| `relative_to` vs `removeprefix` (20,000 paths) | 253.0 | 0.394 | **643x** |
| Full `should_skip_path` (5,000 paths) | 69.3 | 1.55 | **45x** |
| Full `should_skip_path` (20,000 paths) | 285.9 | 6.21 | **46x** |
| `Path.suffix` vs `str.rfind` (5,000 paths) | 6.97 | 0.278 | **25x** |
| `Path.name` vs `str.rfind+slice` (5,000 paths) | 6.37 | 0.360 | **18x** |

### Projected vs Measured

The integration report projected 4.0s savings (13.7% of 31.2s total). Our benchmarks show `pathlib.relative_to` is 634x slower than `str.removeprefix`, and the full `should_skip_path` function is 45x slower with pathlib. These numbers validate the projection: for 59,012 calls at ~57us/call (pathlib), the total is ~3.4s, matching the profiled 3.39s.

### Fix

Convert paths to strings at the boundary of `should_skip_path` and use `str.removeprefix()`, `str.split("/")`, and `set` membership testing instead of `Path.relative_to()` and `Path.parts`.

---

## FINDING 3: orjson vs stdlib json (JSON Serialization)

**orjson provides massive speedups on serialization with zero integration overhead.**

### Measured Results

| Operation | Data Size | json (ms) | orjson (ms) | Speedup |
|---|---|---|---|---|
| dumps compact | 372 KB | 1.16 | 0.21 | **5.5x** |
| dumps compact | 1.9 MB | 5.73 | 1.01 | **5.7x** |
| dumps compact | 8.5 MB | 26.6 | 4.91 | **5.4x** |
| dumps indented | 372 KB | 9.70 | 0.39 | **24.7x** |
| dumps indented | 1.9 MB | 48.5 | 2.02 | **24.0x** |
| dumps indented | 8.5 MB | 216.9 | 8.58 | **25.3x** |
| loads | 372 KB | 1.26 | 0.62 | **2.0x** |
| loads | 1.9 MB | 6.23 | 3.24 | **1.9x** |
| loads | 8.5 MB | 30.1 | 16.6 | **1.8x** |

### Projected vs Measured

The language recommendations projected 5x to 15x. Our measured results show:
- **Compact serialization: 5.4x to 5.7x** (within projected range)
- **Indented serialization: 24x to 25x** (exceeds projected range significantly)
- **Deserialization: 1.8x to 2.0x** (below projected range)

The indented serialization speedup is particularly relevant because `_write_graph_json` uses `json.dump(data, f, indent=2)` (the slowest path). For a 20K node graph, this drops from 217ms to 8.6ms.

---

## FINDING 4: BLAKE3 vs SHA256 Hashing (NEGATIVE RESULT)

**BLAKE3 is slower than hashlib.sha256 for this workload. The recommendation is invalidated.**

### Measured Results

| Operation | SHA256 (ms) | BLAKE3 (ms) | Speedup |
|---|---|---|---|
| 500 snippet hashes | 0.155 | 0.325 | **0.5x (slower)** |
| 2,000 snippet hashes | 0.594 | 1.177 | **0.5x (slower)** |
| 10,000 snippet hashes | 2.988 | 6.131 | **0.5x (slower)** |
| 50 file hashes (5KB avg) | 0.968 | 1.031 | **0.9x (slower)** |
| 200 file hashes (10KB avg) | 4.419 | 4.964 | **0.9x (slower)** |
| 500 file hashes (20KB avg) | 14.164 | 15.883 | **0.9x (slower)** |

### Analysis

The language recommendations projected 4x to 10x speedup. Our benchmarks show BLAKE3 is actually **0.5x to 0.9x** (slower) for this workload. This is because:

1. **hashlib.sha256 is already C-backed** (OpenSSL). The baseline is not pure Python.
2. **BLAKE3's SIMD advantages require large contiguous buffers.** Code snippets average 200 bytes; file chunks are 5-20KB. BLAKE3's parallelism does not engage at these sizes.
3. **FFI overhead dominates.** The `blake3` Python package adds per-call FFI overhead that exceeds the algorithmic savings for small inputs.

**Verdict: Do not adopt BLAKE3.** The recommendation was based on algorithmic benchmarks, not Python binding benchmarks.

---

## FINDING 5: FunctionRegistryTrie Baseline Performance

### Measured Results (Existing Python Implementation)

| Operation | 1K entries | 5K entries | 10K entries | 50K entries |
|---|---|---|---|---|
| insert (ms) | 0.33 | 1.76 | 3.74 | 18.1 |
| lookup (ms) | 0.04 | 0.19 | 0.41 | 2.06 |
| find_ending_with (ms) | 0.004 | 0.018 | 0.046 | 0.47 |
| find_with_prefix (ms) | 0.39 | 2.18 | 4.18 | 39.9 |
| delete 25% (ms) | 0.42 | 2.10 | 4.20 | 22.2 |

### Analysis

The trie operations are already fast when the index is hit (O(1) via `_simple_name_lookup`). The Rust trie rewrite (projected 3x to 8x) would save microseconds per operation. The integration feasibility report correctly identified that a standalone Rust trie provides only 1.5x to 3x net gain after FFI overhead. The **pure Python fix (Finding 1) provides 178x to 382x speedup** on the actual bottleneck, making the Rust rewrite unnecessary.

---

## FINDING 6: GraphLoader JSON Parse + Index Build

### Measured Results

| Graph Size | JSON Parse Only (ms) | GraphLoader.load (ms) | Index Build Overhead |
|---|---|---|---|
| 1K nodes, 2K rels | 1.03 | 2.10 | 2.0x |
| 5K nodes, 10K rels | 5.15 | 10.6 | 2.1x |
| 20K nodes, 50K rels | 24.2 | 64.2 | 2.7x |

### Analysis

GraphLoader.load() is 2x to 2.7x slower than raw JSON parsing due to index construction (node-by-id, node-by-label, outgoing/incoming relationship indexes). With orjson, the JSON parse portion would drop from 24.2ms to ~13.4ms (1.8x), but index construction would remain unchanged. Net improvement for 20K nodes: 64.2ms to ~53ms (1.2x). The index construction is pure Python dict/list operations.

---

## FINDING 7: File Hashing Comparison

### Measured Results

| Algorithm | 50 files (5KB) | 200 files (10KB) | 500 files (20KB) |
|---|---|---|---|
| SHA256 (8KB buffer) | 0.98ms | 4.43ms | 14.3ms |
| SHA256 (64KB buffer) | 1.05ms | 4.61ms | 14.9ms |
| SHA256 (mmap) | 1.30ms | 5.76ms | 17.4ms |
| MD5 | 1.22ms | 6.44ms | 24.7ms |
| BLAKE2b | 1.04ms | 5.17ms | 17.5ms |

### Analysis

SHA256 with 8KB buffer is already the fastest option. Larger buffers and mmap add overhead for these file sizes. MD5 is slower (no hardware acceleration on this platform). File hashing consumes <0.5% of total runtime. No optimization needed.

---

## Summary: Validated vs Invalidated Recommendations

| Recommendation | Language Report Projection | Measured Result | Verdict |
|---|---|---|---|
| Fix `find_ending_with` index | ~1.9x total speedup | **261x to 382x** on the operation | **VALIDATED (exceeds projection)** |
| Replace pathlib with strings | ~1.15x total speedup | **45x to 643x** on path ops | **VALIDATED (exceeds projection)** |
| orjson for JSON | 5x to 15x on JSON ops | **1.8x to 25x** depending on operation | **VALIDATED** |
| BLAKE3 for hashing | 4x to 10x speedup | **0.5x (slower)** | **INVALIDATED** |
| neo4j-rust-ext | 3x to 10x on DB ops | N/A (wrong driver) | **INVALIDATED** (uses Memgraph/pymgclient) |
| Rust AST extension | 10x to 16x on parsing | Not benchmarked (3.1% of CPU) | **DEPRIORITIZED** (targets 3.1% of runtime) |
| Rust trie | 3x to 8x on lookups | 1.5x to 3x net (per feasibility) | **SUPERSEDED** by Python index fix |

## Revised Priority Order (Measured)

| Priority | Fix | Type | Measured Speedup | Effort |
|---|---|---|---|---|
| **1** | Fix `find_ending_with` suffix index | Python bugfix | 261x to 382x on operation (~1.9x total) | Low |
| **2** | Replace pathlib with string ops | Python refactor | 45x to 643x on path ops (~1.15x total) | Low |
| **3** | Cache type inference results | Python memoization | Not benchmarked (projected ~1.07x total) | Low |
| **4** | Suppress debug logging | Config change | Not benchmarked (projected ~1.06x total) | Trivial |
| **5** | Deduplicate FS traversal | Python refactor | Not benchmarked (projected ~1.05x total) | Low |
| **6** | orjson for JSON | Dependency swap | 5.4x to 25x on JSON ops | Trivial |
| **7** | Rust AST extension | Rust crate | Targets 3.1% of CPU; ~1.03x total after Python fixes | High |

**Combined estimated speedup from priorities 1 through 6: ~3.7x, with zero language rewrites.**

The Rust AST extension (previously the headline recommendation at "10x to 16x") targets only 3.1% of actual CPU time and provides ~1.03x total improvement after the pure Python fixes are applied. It should only be considered for repositories significantly larger than the current benchmark workload.
