# Rewrite Recommendations: code-graph-rag Performance Optimization

## Executive Summary

A comprehensive performance analysis of the code-graph-rag codebase (31.2s total, 179M function calls indexing 352 Python files) reveals that **no language rewrite is currently justified**. The top performance bottlenecks are algorithmic inefficiencies and unnecessary object creation in pure Python code, addressable with zero new dependencies and zero build system changes.

### Top 3 Recommendations

1. **Fix `find_ending_with` suffix index** (Python bugfix): Eliminates 48.3% of total CPU time. The `_simple_name_lookup` index has an 80.7% miss rate, causing 123.7M `str.endswith()` calls via linear scan fallback. Benchmarked fix: **261x to 382x speedup** on the operation. Projected total speedup: ~1.9x.

2. **Replace pathlib with string operations in `should_skip_path`** (Python refactor): Eliminates 13.7% of total CPU time. `pathlib.relative_to()` creates intermediate objects on every call (59,012 calls, 3.39s total). Benchmarked fix: **45x to 634x speedup** on path operations. Projected total speedup: ~1.15x.

3. **Cache `build_local_variable_type_map` results** (Python memoization): Eliminates 8.3% of total CPU time. 5,228 uncached AST traversals. Projected total speedup: ~1.07x.

**Combined Tier 1 impact:** ~3.7x total speedup (31.2s to ~8.5s) from pure Python fixes with zero integration overhead.

### Key Finding: Rust Rewrite Not Justified

The language researcher's headline recommendation (Rust AST extension for "10x to 16x speedup") targets tree-sitter operations that consume only **3.1% of actual CPU time**. After Tier 1 Python fixes, a 16x Rust speedup on tree-sitter would yield only **1.03x total improvement** (Amdahl's law). The high development cost (~110KB of Python to port, multi-language parser support, Rust toolchain in CI/Docker) and maintenance burden make this poor ROI until repository sizes exceed 5,000+ files.

### Adversarial Review Outcome

The adversarial reviewer confirmed that **no language rewrite candidate survives challenge**. All top hotspots are fixable in Python. The Rust AST extension was the only candidate with theoretical merit, but the measured 3.1% CPU share makes it unjustifiable at current scale.

### Security Audit Outcome

The security auditor approved all recommended candidates with zero disputes. The only new dependency (orjson) is a widely adopted, well-maintained package with pre-built wheels.

---

## Profiling Baseline

| Metric | Value |
|--------|-------|
| Profiling tool | cProfile |
| Total runtime | 31.2 seconds |
| Total function calls | 179M |
| Workload | `GraphUpdater.run(force=True)` indexing 352 Python files |
| Platform | macOS Darwin 25.3.0, ARM64 |
| Python version | 3.12.2 (CPython) |
| Key dependencies | tree-sitter 0.25.2, pymgclient, loguru, torch 2.10 |

---

## Detailed Analysis: Accepted Candidates

### Candidate 1: Fix `find_ending_with` Linear Scan

**Priority:** 1 (Highest)
**Type:** Python bugfix
**Effort:** Low
**Files:** `codebase_rag/graph_updater.py:156-161`

**Profiling Data:**
- Self time: 7.91s (25.3%)
- Cumulative time: 15.07s (48.3%)
- Call count: 27,376 calls
- Root cause: `_simple_name_lookup` index miss rate of 80.7% (22,096 of 27,376 calls)
- Fallback: `[qn for qn in self._entries.keys() if qn.endswith(f".{suffix}")]` generating 123.7M `str.endswith()` invocations

**Benchmark Results:**

| Registry Size | Queries | Linear Scan (ms) | Suffix Index (ms) | Speedup |
|---|---|---|---|---|
| 1,000 | 38 | 1.77 | 0.007 | 261x |
| 4,500 | 38 | 8.04 | 0.023 | 356x |
| 10,000 | 38 | 17.78 | 0.046 | 382x |

**Fix:** Populate `_simple_name_lookup` for every insert path, including `__setitem__`. Build a complete suffix index mapping the last dot-separated segment to the full qualified name set. This converts O(n) scans to O(1) lookups.

**Projected Net Gain:** ~1.9x total speedup (13.5s saved)
**Integration Overhead:** Zero
**Risk:** Very low

---

### Candidate 2: Replace pathlib with String Operations

**Priority:** 2
**Type:** Python refactor
**Effort:** Low
**Files:** `codebase_rag/utils/path_utils.py`, `codebase_rag/graph_updater.py:364-388`

**Profiling Data:**
- Cumulative time: 4.29s (13.7%)
- Call count: 59,270 calls
- Root cause: `pathlib.relative_to()` creates intermediate `PurePosixPath` objects (3.39s across 54,519 calls)

**Benchmark Results:**

| Operation | pathlib (ms) | String ops (ms) | Speedup |
|---|---|---|---|
| `relative_to` vs `removeprefix` (5K paths) | 61.3 | 0.097 | 634x |
| Full `should_skip_path` (5K paths) | 69.3 | 1.55 | 45x |
| Full `should_skip_path` (20K paths) | 285.9 | 6.21 | 46x |

**Fix:** Convert paths to strings at the function boundary. Use `str.removeprefix()` and `str.split("/")` instead of `Path.relative_to()` and `Path.parts`.

**Projected Net Gain:** ~1.15x total speedup (4.0s saved)
**Integration Overhead:** Zero
**Risk:** Very low

---

### Candidate 3: Cache Type Inference Results

**Priority:** 3
**Type:** Python memoization
**Effort:** Low
**Files:** `codebase_rag/parsers/type_inference.py:119`

**Profiling Data:**
- Cumulative time: 2.59s (8.3%)
- Call count: 5,228 calls
- Root cause: Re-traverses AST nodes per function for type inference without caching

**Fix:** Memoize results keyed by `(file_path, function_start_line, function_end_line)`. Cache invalidation handled by existing incremental update system.

**Projected Net Gain:** ~1.07x total speedup (2.0s saved)
**Integration Overhead:** ~2MB memory for cache
**Risk:** Low

---

### Candidate 4: Suppress Debug Logging in Production

**Priority:** 4
**Type:** Configuration change
**Effort:** Trivial
**Files:** `codebase_rag/graph_updater.py` (run method)

**Profiling Data:**
- Cumulative time: 1.84s (5.9%)
- Call count: 91,119 calls (85,099 debug-level)
- Root cause: Debug log calls processed even when output is suppressed

**Fix:** Set loguru level to INFO at the start of `GraphUpdater.run()`, or use `logger.opt(lazy=True).debug()` for expensive format strings.

**Projected Net Gain:** ~1.06x total speedup (1.7s saved)
**Integration Overhead:** Zero
**Risk:** Very low

---

### Candidate 5: Deduplicate Filesystem Traversal

**Priority:** 5
**Type:** Python refactor
**Effort:** Low
**Files:** `codebase_rag/graph_updater.py:364`, `codebase_rag/parsers/structure_processor.py:49`

**Profiling Data:**
- `identify_structure()`: 1.57s (5.0%)
- `_collect_eligible_files()`: 4.71s (15.1%, overlapping with Candidate 2)
- Root cause: Both call `rglob("*")` + `should_skip_path()` independently

**Fix:** Merge into a single traversal pass that collects both structural elements and eligible files.

**Projected Net Gain:** ~1.05x total speedup (1.5s saved)
**Integration Overhead:** Moderate refactor of two-pass architecture
**Risk:** Low

---

### Candidate 6: orjson for JSON Serialization

**Priority:** 6
**Type:** Dependency swap
**Effort:** Trivial
**Files:** All files using `import json` (graph_loader.py, graph_updater.py, embedder.py, services/graph_service.py)

**Benchmark Results:**

| Operation | json (ms) | orjson (ms) | Speedup |
|---|---|---|---|
| Compact dumps (1.9 MB) | 5.73 | 1.01 | 5.7x |
| Indented dumps (1.9 MB) | 48.5 | 2.02 | 24.0x |
| Loads (1.9 MB) | 6.23 | 3.24 | 1.9x |

**Fix:** Add `orjson>=3.10.0` to dependencies. Replace `json.dumps()` with `orjson.dumps()` (~10 call sites, minor API adjustment for bytes vs str return type).

**Projected Net Gain:** 5.4x to 25x on JSON operations. Marginal impact on indexing (JSON is not a dominant hotspot), significant impact on graph export/import.
**Integration Overhead:** Near zero
**Security:** Widely adopted (polars, FastAPI). Pre-built wheels. Approved by security audit.
**Risk:** Very low

---

## Combined Impact Projection

| Phase | Fixes | Time Saved | Cumulative Speedup | Overhead |
|-------|-------|-----------|-------------------|----------|
| Tier 1 | Candidates 1 through 6 | ~22.7s | ~3.7x (31.2s to ~8.5s) | Zero (except orjson dep) |

**Post Tier 1 runtime breakdown (projected ~8.5s):**

| Component | Time | % of Reduced Total |
|-----------|------|--------------------|
| Call resolution | ~2.5s | 29.4% |
| Graph construction | ~2.5s | 29.4% |
| Miscellaneous | ~2.0s | 23.5% |
| Tree-sitter operations | ~1.0s | 11.8% |
| File I/O + hashing | ~0.5s | 5.9% |

---

## Deferred Candidates

### Rust AST Processing Extension (PyO3/maturin)

**Status:** DEFERRED (reconsider at 5,000+ file scale)

**Rationale:** Tree-sitter operations consume 3.1% of CPU (0.97s). After Tier 1 fixes, this becomes 11.8% of the reduced 8.5s runtime. A 16x Rust speedup saves 0.94s, yielding 1.12x total improvement.

**Why deferred, not rejected:**
- At 5,000+ file scale, tree-sitter time scales linearly while Python fix savings are largely constant
- The structural overhead per node visit (20x to 50x) is real but only matters when visit count is high enough
- Rust extension would also unlock GIL-free thread parallelism for file processing

**Cost if pursued:** ~110KB of Python code to port, 8+ language parsers, maturin build system, Rust toolchain in CI/Docker, platform-specific wheels, ongoing Rust maintenance

### File Processing Parallelism

**Status:** DEFERRED (pursue after Tier 1 fixes)

**Rationale:** Tree-sitter releases the GIL during parsing, enabling ThreadPoolExecutor parallelism. However, shared mutable state (`FunctionRegistryTrie`, `import_mapping`) requires architectural restructuring. The three-pass architecture (structure, definitions, calls) has inherent sequential dependencies.

**Projected gain:** 1.5x to 3x after Tier 1 fixes
**Prerequisite:** Tier 1 fixes must be applied first to establish the new performance baseline

---

## Rejected Candidates

### neo4j-rust-ext

**Verdict:** REJECTED (inapplicable)
**Reason:** This codebase uses Memgraph via `pymgclient` (C extension), not the Neo4j Python driver. `neo4j-rust-ext` patches the `neo4j` driver which is not a dependency. The language researcher's recommendation was based on an incorrect assumption about the database driver.

### BLAKE3 Hashing

**Verdict:** REJECTED (invalidated by benchmarks)

**Benchmark Results:**

| Operation | SHA256 (ms) | BLAKE3 (ms) | Speedup |
|---|---|---|---|
| 500 snippet hashes | 0.155 | 0.325 | 0.5x (slower) |
| 2,000 snippet hashes | 0.594 | 1.177 | 0.5x (slower) |
| 50 file hashes (5KB avg) | 0.968 | 1.031 | 0.9x (slower) |

**Reason:** The language recommendations projected 4x to 10x speedup based on algorithmic benchmarks, not Python binding benchmarks. hashlib SHA256 is already C-backed (OpenSSL). BLAKE3's SIMD advantages require large contiguous buffers; code snippets average 200 bytes. FFI overhead per call exceeds algorithmic savings for small inputs. Additionally, hashing is <0.1% of total runtime.

### Rust FunctionRegistryTrie (Standalone)

**Verdict:** REJECTED
**Reason:** Standalone Rust trie provides only 1.5x to 3x net gain after FFI overhead. The FFI boundary is crossed per-lookup (thousands of times per file), cutting gains roughly in half. More critically, the Python suffix index fix (Candidate 1) provides 261x to 382x speedup on the actual bottleneck, making the Rust trie unnecessary. Only viable if bundled with a full Rust AST extension.

### Rust String Processing in Call Resolution (Standalone)

**Verdict:** REJECTED
**Reason:** Negative net gains when implemented standalone. Call resolution is deeply interleaved with trie lookups, import map lookups, and AST node access. Extracting just the string processing would require marshalling all context (import maps, trie state, class inheritance) across FFI on every call, which exceeds the per-operation savings.

---

## Optimize-First Recommendations (Non-Rewrite)

These Python-level improvements should be implemented before any language rewrite consideration:

1. **Use `embed_code_batch`** in `graph_updater.py:_generate_semantic_embeddings`: The batch function exists but the pipeline calls `embed_code` per item. Projected 5x to 20x speedup on the embedding phase.

2. **Incremental call re-resolution** in `realtime_updater.py`: Currently performs full call re-resolution on every file change. Implementing incremental resolution (re-resolve only affected qualified names) would provide 10x to 100x speedup for realtime updates.

3. **Fix BoundedASTCache memory limit**: `sys.getsizeof()` misses C-level tree-sitter memory, so the cache size limit is effectively broken. Use `tracemalloc` or a conservative estimate based on entry count instead.

4. **EmbeddingCache data format**: Replace `list[float]` with numpy arrays for 4x memory reduction on embedding storage.

5. **FunctionRegistryTrie dual storage**: Consolidate `_entries` dict and trie nodes to eliminate 2.5 MiB waste per 10K entries (addressable as part of Candidate 1).

---

## Benchmark Methodology

**Infrastructure:** Established by test-sentinel (task #1). All benchmarks in `benchmarks/` directory.

| Parameter | Value |
|-----------|-------|
| Warmup runs | 3 (discarded) |
| Measured iterations | 20 to 100 per benchmark |
| Statistics | Median, mean, stddev, min, max, p95 |
| GC | Disabled during timing |
| Isolation | Fresh function scope per run |

**Benchmark suite:**

| File | Target |
|------|--------|
| `bench_find_ending_with_fix.py` | Suffix index vs linear scan |
| `bench_pathlib_vs_string.py` | pathlib vs string path operations |
| `bench_json_serialization.py` | stdlib json vs orjson |
| `bench_file_hashing.py` | SHA256 vs BLAKE3 vs BLAKE2b |
| `bench_trie.py` | FunctionRegistryTrie operations |
| `bench_string_ops.py` | String operation microbenchmarks |
| `bench_embedding_cache.py` | EmbeddingCache operations |
| `bench_ast_cache.py` | BoundedASTCache operations |
| `bench_graph_loader.py` | GraphLoader JSON parse + index build |
| `bench_dropin_replacements.py` | Drop-in library comparisons |

Run all benchmarks: `uv run python benchmarks/run_all.py`

---

## Profiling Data Sources

| Phase | Task | Owner | Output |
|-------|------|-------|--------|
| Baseline | #1 | test-sentinel | Green test suite, benchmark methodology |
| CPU profiling | #2 | cpu-profiler | Hotspot report (cProfile, 31.2s, 179M calls) |
| Memory profiling | #3 | memory-profiler | Allocation report (tracemalloc, 25-frame traces) |
| I/O profiling | #4 | cpu-profiler | I/O report |
| Concurrency analysis | #5 | concurrency-analyst | GIL analysis, parallelism opportunities, scaling factors |
| Structural analysis | #6 | static-pattern-analyst | 9 language-inherent ceilings with severity rankings |
| Language research | #7 | language-researcher | Target language recommendations (Rust via PyO3) |
| Integration feasibility | #8 | integration-architect | FFI overhead analysis, build system impact, net gain calculations |
| Benchmarks | #9 | benchmark-designer | Measured performance for all candidates |
| Scorecard | #10 | evaluator | Prioritized ranking with scores |
| Adversarial review | #11 | adversarial-reviewer | No rewrite justified at current scale |
| Security audit | #12 | security-auditor | All candidates approved, zero disputes |

---

## Conclusion

The performance analysis produced a clear, data-driven result: **optimize Python first, rewrite later (if ever).**

The top 5 bottlenecks consuming 72.8% of runtime are all pure Python algorithmic issues (linear scan fallback, pathlib object overhead, uncached traversals, debug logging, duplicate traversals). Fixing them provides ~3.7x total speedup with zero integration overhead, zero build system changes, and zero maintenance burden.

The Rust AST extension, while technically sound as a future optimization for large-scale workloads, targets only 3.1% of current CPU time and provides ~1.03x total improvement after Python fixes. It should be reconsidered only when the codebase routinely processes 5,000+ file repositories and the Python fixes have been applied.

No language rewrite recommendation survived the adversarial review at current scale.
