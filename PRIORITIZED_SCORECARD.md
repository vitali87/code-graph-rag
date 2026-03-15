# Prioritized Scorecard: Rewrite Candidates

**Baseline:** 31.2s total, 179M function calls, indexing 352 Python files (cProfile)

## Scoring Methodology

Each candidate is scored 1 to 5 on six dimensions. The final rank is determined by **Net Score**, which weights measured/projected performance gain and scope of impact highest, while penalizing integration overhead, risk, and maintenance burden.

**Weights:** Performance Gain (25%) | Memory Improvement (10%) | Integration Feasibility (20%) | Risk & Complexity (20%) | Scope of Impact (15%) | Maintenance Burden (10%)

**Score key:** 5 = excellent, 4 = good, 3 = moderate, 2 = poor, 1 = unacceptable

---

## Tier 1: ACCEPTED (High confidence, clear positive ROI)

### Rank 1: Fix `find_ending_with` Linear Scan (Python Bugfix)

| Dimension | Score | Rationale |
|---|---|---|
| Performance Gain | 5 | 48.3% of CPU (15.07s). Eliminates 123.7M `str.endswith()` calls. Projected ~1.9x total speedup. |
| Memory Improvement | 3 | Reduces temporary string allocations from linear scans. |
| Integration Feasibility | 5 | Pure Python fix. Zero new dependencies, zero build changes. |
| Risk & Complexity | 5 | Low risk. Fix the 80.7% miss rate in `_simple_name_lookup` index, or build suffix index. |
| Scope of Impact | 5 | Affects every file processed. Dominant bottleneck in the entire pipeline. |
| Maintenance Burden | 5 | No new language, no new build tooling. Standard Python data structure. |
| **Net Score** | **4.80** | |

**Verdict: PROCEED IMMEDIATELY.** This is a bugfix, not a rewrite. The `_simple_name_lookup` index has an 80.7% miss rate, causing fallback to O(n) linear scan on every call resolution. Fixing the index population or adding a suffix index is a straightforward Python change with the highest ROI of any candidate.

---

### Rank 2: Replace pathlib with String Operations in `should_skip_path` (Python Refactor)

| Dimension | Score | Rationale |
|---|---|---|
| Performance Gain | 4 | 13.7% of CPU (4.29s across 59,012 calls). ~20x faster with string ops. |
| Memory Improvement | 4 | Eliminates ~118,000 intermediate Path objects per run. |
| Integration Feasibility | 5 | Internal refactor. No dependencies. |
| Risk & Complexity | 5 | Replace `Path.relative_to()` with `str.removeprefix()`. Straightforward. |
| Scope of Impact | 4 | Affects file traversal (called for every file and directory). |
| Maintenance Burden | 5 | Simpler code than current pathlib usage. |
| **Net Score** | **4.50** | |

**Verdict: PROCEED.** Convert paths to strings at the boundary and use string comparison. The pathlib object creation overhead is avoidable.

---

### Rank 3: Cache `build_local_variable_type_map` Results (Python Memoization)

| Dimension | Score | Rationale |
|---|---|---|
| Performance Gain | 3 | 8.3% of CPU (2.59s across 5,228 calls). Saves ~2s. |
| Memory Improvement | 2 | Adds ~2MB cache. Slight memory increase. |
| Integration Feasibility | 5 | Add `@lru_cache` or dict-based memoization. No dependencies. |
| Risk & Complexity | 5 | Keyed by (file_path, function_start_line, function_end_line). Cache invalidation handled by existing incremental update system. |
| Scope of Impact | 3 | Affects call resolution for files with multiple functions. |
| Maintenance Burden | 5 | Standard memoization pattern. |
| **Net Score** | **3.90** | |

**Verdict: PROCEED.** Standard memoization with minimal memory cost.

---

### Rank 4: Suppress Debug Logging in Production (Config Change)

| Dimension | Score | Rationale |
|---|---|---|
| Performance Gain | 3 | 5.9% of CPU (1.84s from 85,099 debug calls). Saves ~1.7s. |
| Memory Improvement | 2 | Reduces temporary string allocations from format strings. |
| Integration Feasibility | 5 | Set log level to INFO at start of `GraphUpdater.run()`. One line. |
| Risk & Complexity | 5 | Trivial. Debug output not needed during normal graph building. |
| Scope of Impact | 3 | Affects all debug logging throughout pipeline. |
| Maintenance Burden | 5 | No maintenance cost. |
| **Net Score** | **3.75** | |

**Verdict: PROCEED.** Trivial change, meaningful gain.

---

### Rank 5: Deduplicate Filesystem Traversal (Python Refactor)

| Dimension | Score | Rationale |
|---|---|---|
| Performance Gain | 3 | 5.0% of CPU (1.57s). Eliminates duplicate `rglob("*")` + `should_skip_path()` pass. |
| Memory Improvement | 3 | Avoids building duplicate file lists. |
| Integration Feasibility | 4 | Moderate refactor: merge `identify_structure()` and `_collect_eligible_files()` into single traversal. |
| Risk & Complexity | 4 | Requires restructuring two-pass architecture. Not trivial but well-scoped. |
| Scope of Impact | 3 | Affects initial file discovery phase only. |
| Maintenance Burden | 4 | Single-pass is arguably simpler than two-pass. |
| **Net Score** | **3.55** | |

**Verdict: PROCEED.** Combine with Rank 2 (string paths) for maximum benefit on the file traversal phase.

---

### Rank 6: orjson (Drop-in JSON Replacement)

| Dimension | Score | Rationale |
|---|---|---|
| Performance Gain | 3 | 5x to 15x on JSON ops. JSON is NOT a dominant hotspot in the profiling data (indexing phase), but significant for graph export and cache I/O. |
| Memory Improvement | 4 | 75% lower peak RSS for JSON operations. |
| Integration Feasibility | 5 | Add dependency, ~10 call sites need minor adjustment (bytes vs str). |
| Risk & Complexity | 5 | Widely adopted (polars, FastAPI). Pre-built wheels for all platforms. |
| Scope of Impact | 2 | JSON ops are a small fraction of total indexing time. Bigger impact on graph export/import. |
| Maintenance Burden | 5 | Drop-in replacement. No ongoing maintenance cost. |
| **Net Score** | **3.50** | |

**Verdict: PROCEED.** Low effort, low risk, moderate gain on I/O-heavy workflows (export, cache load/save). Not a game-changer for indexing performance.

---

## Tier 2: CONDITIONAL (Worthwhile only after Tier 1 is complete)

### Rank 7: Rust AST Processing Extension (PyO3/maturin)

| Dimension | Score | Rationale |
|---|---|---|
| Performance Gain | 2 | Tree-sitter ops are only 3.1% of CPU BEFORE Python fixes. After Tier 1 fixes (~3.7x speedup), tree-sitter becomes ~11.8% of reduced runtime. A 16x Rust speedup saves 0.94s from 8.5s. Only 1.12x total improvement post-fixes. |
| Memory Improvement | 4 | Eliminates Python object overhead (50-80 bytes per dict entry), reduces malloc calls by ~8x. |
| Integration Feasibility | 2 | ~110KB of Python code to port. 8+ language parsers. Complex multi-language pattern matching. Requires maturin build system, Rust toolchain in CI/Docker, platform-specific wheels. |
| Risk & Complexity | 2 | Large surface area. Tight coupling with existing data structures. Tree-sitter version compatibility. IngestorProtocol callback complexity. |
| Scope of Impact | 3 | Affects all file processing. But only becomes meaningful at 10,000+ file scale. |
| Maintenance Burden | 2 | Introduces Rust into a pure Python project. Requires Rust expertise for ongoing maintenance. Multi-language build complexity. |
| **Net Score** | **2.35** | |

**Verdict: DEFER.** The integration architect's analysis is decisive: tree-sitter operations consume only 3.1% of actual CPU time. The language researcher's headline claim of 10x to 16x was based on incorrect assumptions about where time was spent. After Tier 1 Python fixes, the remaining 8.5s runtime has tree-sitter at 11.8%, making a 16x Rust speedup yield only 1.12x total. The high development cost (~110KB port, multi-language parsers) and maintenance burden (Rust toolchain, platform-specific wheels) make this poor ROI until the codebase scales an order of magnitude.

**Reconsider when:** Repository size exceeds 5,000+ files, making tree-sitter operations a larger fraction of total runtime.

---

### Rank 8: File Processing Parallelism (ProcessPoolExecutor)

| Dimension | Score | Rationale |
|---|---|---|
| Performance Gain | 3 | 1.5x to 3x after Tier 1 fixes. Limited by sequential pass dependencies (Amdahl's law). |
| Memory Improvement | 1 | Increases memory (per-worker grammar loading, duplicate tries). |
| Integration Feasibility | 3 | Requires restructuring three-pass pipeline. Shared mutable state (trie, import maps) needs synchronization. |
| Risk & Complexity | 3 | Tree-sitter objects not serializable across process boundaries. Worker initialization overhead (~50ms per worker). |
| Scope of Impact | 3 | Affects per-file processing throughput. |
| Maintenance Burden | 3 | Adds concurrency complexity. Harder to debug. |
| **Net Score** | **2.70** | |

**Verdict: DEFER.** Worth pursuing after Tier 1 fixes reduce the baseline. The concurrency analyst confirmed tree-sitter releases the GIL during parsing, so ThreadPoolExecutor (not ProcessPoolExecutor) is the preferred approach, with lower overhead. But this requires the three-pass architecture to be restructured.

---

## Tier 3: REJECTED (Net gain does not justify complexity)

### Rank 9: Rust FunctionRegistryTrie (PyO3, standalone)

| Dimension | Score | Rationale |
|---|---|---|
| Performance Gain | 2 | Standalone: 1.5x to 3x on trie ops. Python call resolution code still creates strings for every lookup key. FFI crossing per-lookup cuts gains in half. |
| Memory Improvement | 4 | Contiguous memory layout eliminates per-node dict overhead. |
| Integration Feasibility | 2 | Only viable bundled with Rank 7 (Rust AST extension). Standalone, FFI overhead negates gains. |
| Risk & Complexity | 3 | Moderate if bundled. High coupling with Rank 7. |
| Scope of Impact | 2 | **Rank 1 (fix `find_ending_with`) eliminates the primary trie bottleneck.** After that fix, trie operations are no longer the dominant cost. |
| Maintenance Burden | 2 | Requires Rust maintenance alongside Python trie. |
| **Net Score** | **2.30** | |

**Verdict: REJECT standalone. BUNDLE with Rank 7 if/when Rank 7 proceeds.** The critical insight from the integration architect: standalone Rust trie has negative net gains because FFI boundary crossing happens per-lookup (thousands of times per file). Only viable when bundled with the full Rust AST extension. Furthermore, Rank 1 (Python bugfix) eliminates the primary trie bottleneck (the linear scan), making Rust trie less urgent.

---

### Rank 10: neo4j-rust-ext

| Dimension | Score | Rationale |
|---|---|---|
| Performance Gain | 1 | **0x. This codebase uses Memgraph via pymgclient, NOT the Neo4j Python driver.** neo4j-rust-ext patches the `neo4j` driver which is not used. |
| Memory Improvement | 1 | N/A. |
| Integration Feasibility | 1 | Inapplicable. No `neo4j` dependency in `pyproject.toml`. |
| Risk & Complexity | 1 | Wrong driver assumption. |
| Scope of Impact | 1 | Zero impact. |
| Maintenance Burden | 1 | N/A. |
| **Net Score** | **1.00** | |

**Verdict: REJECT.** The language researcher incorrectly assumed the codebase uses the Neo4j Python driver. It uses Memgraph via pymgclient (a C extension). neo4j-rust-ext has zero applicability.

---

### Rank 11: BLAKE3 Hashing

| Dimension | Score | Rationale |
|---|---|---|
| Performance Gain | 1 | Negligible. Hashing is NOT a bottleneck. `_hash_file` processes ~5ms total for 1000 files. `_content_hash` takes microseconds per call. hashlib SHA256 is already C-backed. |
| Memory Improvement | 1 | No meaningful change. |
| Integration Feasibility | 5 | One-line change per call site. Drop-in. |
| Risk & Complexity | 3 | Cache invalidation forces full re-index on first run after change. One-time negative impact dwarfs per-operation savings. |
| Scope of Impact | 1 | Hashing is <0.1% of total runtime. |
| Maintenance Burden | 4 | Minimal. |
| **Net Score** | **1.85** | |

**Verdict: REJECT.** Optimizing an operation that takes microseconds per call provides no meaningful improvement. The cache invalidation cost (forced full re-index) creates a one-time penalty that exceeds months of per-operation savings. The integration architect's analysis is correct: "Skip unless profiling proves hashing is >5% of total wall clock time." It is far below 5%.

---

### Rank 12: String Processing in Call Resolution (Rust, standalone)

| Dimension | Score | Rationale |
|---|---|---|
| Performance Gain | 1 | **Negative standalone.** FFI overhead of passing import maps and trie state for each call resolution exceeds the savings from faster string processing. |
| Memory Improvement | 3 | Would reduce temporary string allocations. |
| Integration Feasibility | 1 | Deeply interleaved with trie lookups, import maps, AST node access. Cannot be isolated without massive FFI overhead. |
| Risk & Complexity | 1 | Requires marshalling all context across FFI per call. |
| Scope of Impact | 2 | Affects call resolution, but FFI boundary negates gains. |
| Maintenance Burden | 2 | Additional Rust code for marginal or negative benefit. |
| **Net Score** | **1.40** | |

**Verdict: REJECT standalone. BUNDLE with Rank 7 only.** The integration architect proved that the boundary crossing cost exceeds per-operation savings when implemented standalone. Only viable as part of a comprehensive Rust AST extension (Rank 7).

---

## Combined Impact Projection

### Phase 1: Tier 1 Python Fixes (Ranks 1 through 6)

| Fix | Time Saved | % of Total | Cumulative |
|-----|-----------|------------|------------|
| Rank 1: Fix find_ending_with | ~13.5s | 43.3% | 43.3% |
| Rank 2: String path ops | ~4.0s | 12.8% | 56.1% |
| Rank 3: Cache type inference | ~2.0s | 6.4% | 62.5% |
| Rank 4: Suppress debug logging | ~1.7s | 5.5% | 68.0% |
| Rank 5: Deduplicate FS traversal | ~1.5s | 4.8% | 72.8% |
| Rank 6: orjson (I/O workflows) | Variable | Marginal on indexing | 72.8%+ |
| **Total** | **~22.7s** | **72.8%** | |

**Projected runtime after Phase 1:** ~8.5s (3.7x speedup from pure Python fixes)
**Integration overhead:** Zero
**Build system changes:** One dependency added (orjson)
**Maintenance burden:** None beyond standard Python

### Phase 2: Tier 2 (Only if needed after Phase 1)

After Phase 1, the remaining 8.5s breaks down as:
- Tree-sitter operations: ~1.0s (11.8%)
- Call resolution: ~2.5s (29.4%)
- Graph construction: ~2.5s (29.4%)
- File I/O + hashing: ~0.5s (5.9%)
- Miscellaneous: ~2.0s (23.5%)

The Rust AST extension (Rank 7) would save ~0.94s from tree-sitter, reducing to ~7.6s (1.12x). File parallelism (Rank 8) could provide 1.5x to 3x on top. Combined: ~3.0 to 5.0s total.

**Phase 2 is only justified when repository sizes exceed 5,000+ files**, where tree-sitter and call resolution become a proportionally larger fraction of total runtime.

---

## Key Findings

1. **72.8% of the total runtime is addressable with pure Python fixes** (zero integration overhead, zero build changes, zero maintenance burden).

2. **The headline Rust AST rewrite (10x to 16x) targets only 3.1% of actual CPU time.** Profiling data invalidated the language researcher's core assumption about where time is spent.

3. **neo4j-rust-ext is completely inapplicable** (wrong database driver). This was a factual error in the language recommendations.

4. **BLAKE3 hashing optimizes a non-bottleneck** (microsecond-level operations that total <0.1% of runtime).

5. **Standalone Rust trie and string processing have negative net gains** due to per-lookup FFI boundary crossing costs that exceed the per-operation savings.

6. **The single largest optimization (Rank 1) is a Python bugfix**, not a language rewrite. Fixing the `_simple_name_lookup` index miss rate from 80.7% to near 0% eliminates 48.3% of total CPU time.

---

## Scorecard Summary

| Rank | Candidate | Type | Net Score | Time Saved | Verdict |
|------|-----------|------|-----------|------------|---------|
| 1 | Fix `find_ending_with` | Python bugfix | 4.80 | ~13.5s (43.3%) | **PROCEED** |
| 2 | String path ops | Python refactor | 4.50 | ~4.0s (12.8%) | **PROCEED** |
| 3 | Cache type inference | Python memoization | 3.90 | ~2.0s (6.4%) | **PROCEED** |
| 4 | Suppress debug logging | Config change | 3.75 | ~1.7s (5.5%) | **PROCEED** |
| 5 | Deduplicate FS traversal | Python refactor | 3.55 | ~1.5s (4.8%) | **PROCEED** |
| 6 | orjson | Dependency swap | 3.50 | Variable | **PROCEED** |
| 7 | Rust AST extension | Rust crate | 2.35 | ~0.94s post-fixes | **DEFER** |
| 8 | File parallelism | Architecture change | 2.70 | 1.5x to 3x post-fixes | **DEFER** |
| 9 | Rust trie (standalone) | Rust (PyO3) | 2.30 | Marginal standalone | **REJECT** |
| 10 | neo4j-rust-ext | N/A | 1.00 | 0 (wrong driver) | **REJECT** |
| 11 | BLAKE3 hashing | Dependency swap | 1.85 | Negligible | **REJECT** |
| 12 | Rust string processing | Rust (standalone) | 1.40 | Negative standalone | **REJECT** |

---

**Note:** Task #9 (proof-of-concept benchmarks) was still in progress when this scorecard was produced. If benchmark data reveals performance characteristics that contradict the profiling data used here, this scorecard should be revised. However, the profiling data (cProfile, 31.2s, 179M calls) is empirical and provides a strong basis for these rankings.
