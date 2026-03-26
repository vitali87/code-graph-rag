# Language Recommendations for Performance Hotspots

## Executive Summary

**CPU profiling reveals that 48.3% of total runtime is spent in a single Python function** (`FunctionRegistryTrie.find_ending_with()`) performing a linear scan fallback with 123.7M `str.endswith()` calls. This is a pure algorithmic bottleneck, not a language limitation, and fixing the simple name lookup index (80.7% miss rate) would nearly halve total runtime with zero language rewrite.

After addressing algorithmic issues (Phase 0: ~3.7x total improvement from pure Python fixes), **Rust via PyO3** is the recommended target language for the remaining CPU-bound hotspots (AST wrapper overhead, trie operations, call resolution). For serialization, **orjson** (Rust-backed) is a drop-in replacement for stdlib json. ~~neo4j-rust-ext~~ was retracted (codebase uses Memgraph/pymgclient, not Neo4j).

**Critical distinction:** This report contains both theoretical per-instruction overhead multipliers (20x-50x from structural analysis) and empirical runtime impact (from CPU profiling). The structural multipliers explain WHY Python is slow at specific operations, but the IMPACT must be measured against the actual profiled runtime distribution via Amdahl's law. After Phase 0 Python fixes reduce the baseline from 31.2s to ~8-10s, the Rust extension (Phase 2) addresses ~20% of the reduced baseline, yielding diminishing but still meaningful returns.

**Profiling baseline:** 31.2 seconds (cProfile), 14.0s (wall-clock), 179M function calls for indexing 352 Python files.

---

## Hotspot Categories and Recommendations

### HOTSPOT 1: Tree-sitter AST Parsing and Traversal

**Files:** `parsers/call_processor.py`, `parsers/call_resolver.py`, `parsers/definition_processor.py`, `parsers/function_ingest.py`, `parsers/structure_processor.py`, all `parsers/handlers/*.py`

**Workload:** Per-file tree-sitter parsing, QueryCursor iteration, recursive Node traversal, text extraction/decoding from AST nodes. Every file in a repository triggers full AST parsing and multi-pass traversal for functions, classes, calls, and imports.

**Recommended Language:** Rust (via PyO3/maturin)

**Projected Speedup:** 20x to 50x (revised upward based on structural analysis)

**CPU PROFILING DATA:**
- `TypeInferenceEngine.build_local_variable_type_map()`: **2.59s cumulative (8.3%)** across 5,228 calls. Traverses ASTs that have already been parsed, with no caching of results across calls within the same file.
- `QueryCursor.captures()`: **0.78s self time (2.5%)** across 11,028 calls. Already a C extension, largely irreducible.
- `Parser.parse()`: **0.19s self time (0.6%)** across 352 calls. Already C, already fast.
- **Key insight from profiling:** Tree-sitter C operations (parse + captures) total only ~1.0s (3.1% of runtime). The overwhelming majority of AST-related CPU time is in the Python wrapper code doing traversal, type inference, and call resolution around these fast C operations. This validates the Rust rewrite approach: keep tree-sitter's C parsing (fast), move the Python traversal/processing into Rust.
- Loguru debug logging: **1.84s cumulative (5.9%)** across 91,119 calls, including 85,099 debug-level calls processed even when not displayed. This is a Python-level fix (reduce log level or guard debug calls).

**Evidence:**
- Gauge.sh case study: Moving AST-dependent operations into a Rust extension yielded a 16x speedup (8.7s to 530ms) on a 500k-line codebase. The original Python implementation made ~60M malloc calls and spent 35% of cycles on GC; the Rust version made ~7M malloc calls with no significant GC activity. [Source: gauge.sh/blog/python-extensions-should-be-lazy]
- Tree-sitter is already written in C/Rust. The Python bindings add per-node FFI overhead on every `.child_by_field_name()`, `.text`, and `.children` access. Moving traversal logic into Rust eliminates this boundary-crossing cost entirely.
- ast-grep (Rust-based tree-sitter tool) demonstrates that keeping AST processing in Rust-land and only returning final results to Python is the optimal architecture. [Source: github.com/ast-grep/ast-grep]
- **Structural analysis (CRITICAL severity):** Static analysis confirmed 20x to 50x overhead multiplier per node visit. Every `.parent`, `.children`, `.type` access on tree-sitter nodes goes through Python's descriptor protocol (~50 instructions vs ~1 instruction for a direct struct field read in Rust/C). Specific hot patterns identified:
  - `_build_nested_qualified_name()` in `function_ingest.py:344-389`: walks parent chain upward
  - `_resolve_inherited_method()` in `call_resolver.py:624-649`: BFS through class_inheritance dict
  - `is_method_node()` in `parsers/utils.py:159-173`: walks parent chain for every function node
  - `_collect_ancestor_path_parts()` in `function_ingest.py:369-389`: ancestor walk with repeated type checks
  - `_is_nested_inside_function()` in `class_ingest/mixin.py:34-45`: another parent chain walk
- **Additional structural overhead:** `bytes.decode("utf-8")` on every `node.text` access (MEDIUM severity, 3x to 5x overhead). The LRU cache at `parsers/utils.py:48-50` mitigates this partially, but `call_processor.py:49` bypasses the cache entirely. In Rust, zero-copy `&[u8]` slices eliminate this entirely.

**Architecture:** Build a Rust extension that accepts file bytes and a language enum, performs tree-sitter parsing and all traversal passes (function extraction, class extraction, call extraction, import extraction) in Rust, and returns structured results (lists of function definitions, call relationships, class hierarchies) as Python objects.

**GIL consideration (from concurrency analysis):** Tree-sitter's C extension already releases the GIL during parsing, which enables ThreadPoolExecutor parallelism for the current Python implementation. Any Rust rewrite MUST preserve this property by using `Python::allow_threads` in PyO3 during parsing and traversal, enabling concurrent file processing across threads without process-level parallelism overhead.

**Why not Cython:** Cython cannot eliminate the Python-to-C FFI overhead of tree-sitter node access, since the bottleneck is the per-node boundary crossing, not Python loop overhead. Rust allows direct tree-sitter C API access without Python object creation.

**Why not Go:** Go's FFI to C (cgo) has higher overhead than Rust's native C interop. Go's garbage collector would reintroduce the GC pauses that are a key problem in the Python implementation. PyO3 is a more mature Python interop story than Go's limited options (gopy, cgo+ctypes).

---

### HOTSPOT 2: FunctionRegistryTrie Operations

**Files:** `graph_updater.py` (FunctionRegistryTrie class), `parsers/call_resolver.py`

**Workload:** Trie insertion and lookup for qualified function names. Every function/method/class definition triggers a trie insert (string splitting on `.`, nested dict traversal). Every call resolution triggers trie lookups, often with multiple fallback strategies (direct lookup, inheritance chain walking, simple name fallback).

**Recommended Language:** Rust (via PyO3/maturin)

**Projected Speedup:** 10x to 50x on the post-fix baseline (NOT on the current 15s runtime)

**IMPORTANT CONTEXT (from integration-architect):** The 10x-50x speedup applies to trie operations AFTER the algorithmic index fix (Priority 0a). After fixing the `_simple_name_lookup` 80.7% miss rate, trie operations drop from 15s to under 1s in pure Python. The Rust trie's 10x-50x improvement then applies to an operation taking <1s, yielding <1s additional savings. The algorithmic fix alone yields ~2x on total runtime. The Rust rewrite is justified by (a) GIL release enabling thread parallelism and (b) cumulative savings across all trie/string operations, but the root cause is an algorithmic bug, not a language limitation.

**CPU PROFILING DATA (the #1 finding):**
- `find_ending_with()` at `graph_updater.py:156`: **7.91s self time (25.3%), 15.07s cumulative (48.3%)** across 27,376 calls
- Root cause: The `_simple_name_lookup` index has an **80.7% miss rate** (22,096 of 27,376 calls miss). On each miss, the code falls back to a linear scan: `[qn for qn in self._entries.keys() if qn.endswith(f".{suffix}")]`, triggering **123.7M `str.endswith()` calls** (7.21s self time)
- Called 26,950 times from `CallResolver._try_resolve_via_trie()`, the last-resort call resolution strategy
- **This single function accounts for nearly half of all CPU time. The trie data structure exists but is bypassed in favor of the linear fallback in most cases.**
- **CRITICAL: Fix the simple name lookup index first (Python algorithmic fix).** A proper reverse index mapping simple names to qualified names would eliminate the linear scan entirely, reducing this from 15.07s to sub-second. This is the highest-ROI optimization in the entire codebase. Note: even after the algorithmic fix, Python's per-call `str.endswith()` overhead is 5x to 10x what Rust byte-slice comparisons would cost (structural analysis cross-reference), so the Rust trie rewrite remains valuable for the remaining lookup operations.

**Evidence for language rewrite (after algorithmic fix):**
- **Concurrency analysis confirms this is GIL-bound:** Pure Python trie/dict operations in `FunctionRegistryTrie` and `CallResolver` hold the GIL throughout, preventing any thread-level parallelism. The concurrency analyst estimates 10x to 50x speedup from moving this to native code. This is the strongest case for a Rust rewrite since it eliminates both per-operation overhead AND the GIL bottleneck.
- The current implementation uses nested Python dicts as trie nodes, which means every level of trie traversal creates Python string objects and performs dict hash lookups with full Python object overhead.
- **Structural analysis (HIGH severity):** Python dicts carry 50 to 80 bytes overhead per entry plus hash computation. Each `in` or `[]` lookup involves: hash the key string (O(n) for string length), probe the hash table, compare keys. In Rust, a `HashMap` has similar algorithmic complexity but with inline storage, no reference counting, and cache-friendly memory layout. Specialized data structures (arena-allocated tries, interned string IDs) are practical in systems languages but impractical in Python due to the object model.
- **String overhead (HIGH severity, 5x to 15x):** Qualified names are constructed, split, compared, and looked up thousands of times per file. Each `.split(".")` allocates a new list of new string objects. Each f-string creates a new heap allocation. `_calculate_import_distance()` at `call_resolver.py:651-671` splits both strings and compares elementwise. In Rust, these would be zero-copy string views or stack-allocated slices.
- Rust trie implementations (radix_trie crate) store data contiguously in memory with no per-node heap allocation, eliminating GC pressure. For high-miss-rate lookups (common in call resolution with fallback chains), optimized Rust tries outperform Python dicts. [Source: dev.to/timclicks/two-trie-implementations-in-rust]
- The Gauge.sh case study showed that moving data structures out of Python and into compact Rust structs reduced malloc calls by 8.5x, directly relevant to this trie-heavy workload.
- PyO3 achieves 92% of pure Rust performance for data structure operations while maintaining full Python interoperability. [Source: pyo3.rs/main/performance]

**Architecture:** First, fix the `_simple_name_lookup` index to cover the 80.7% miss cases (Python fix). Then, implement `FunctionRegistryTrie` as a Rust struct exposed via PyO3. The `insert()`, `get()`, and `find_ending_with()` methods accept Python strings, perform all trie operations in Rust, and return results. The `__contains__` check (used heavily in call resolution) stays in Rust. Use Rust's `lasso` or `string-interner` crate for interned string IDs to eliminate the qualified name duplication across trie, `_entries`, `simple_name_lookup`, and `import_mapping` (memory profiling shows 3.5 MiB for 10k entries in Python vs ~400 KiB estimated in Rust with interning, a 9x reduction).

**Convergence point (CPU + memory):** This is the strongest single rewrite target in the codebase. FunctionRegistryTrie is simultaneously the #1 CPU hotspot (48.3%) AND carries 9x memory overhead. A Rust replacement addresses both dimensions in one component.

**Why not Cython:** Cython would help with loop overhead but cannot change the fundamental data layout. The bottleneck is Python dict overhead per trie node, which requires a different data structure (Rust's contiguous memory layout).

---

### HOTSPOT 3: JSON Serialization/Deserialization for Graph Data

**Files:** `graph_loader.py`, `graph_updater.py`, `services/graph_service.py`

**Workload:** Loading and saving large graph JSON files (nodes, relationships, properties). The `GraphLoader.load()` method reads potentially multi-megabyte JSON files. The `GraphUpdater` serializes graph data for Neo4j ingestion.

**Recommended Language:** Drop-in replacement with orjson (Rust-backed)

**Projected Speedup:** 5x to 15x

**Evidence:**
- orjson (written in Rust) is 2x to 15.8x faster than Python's stdlib json, depending on payload size. For large payloads (>1MB), gains are 10x or more. [Source: medium.com/codeelevation/want-500-faster-json-in-python-try-orjson]
- orjson uses SIMD (AVX2) for parallel UTF-8 validation and string escaping, scanning 32 bytes at once vs byte-by-byte. [Source: github.com/ijl/orjson]
- Memory usage is 75% lower peak RSS, which matters for large graph files.
- For a 10K-record benchmark, orjson achieved 820 MB/s serialization vs json's 52 MB/s (15.8x).

**Architecture:** Replace `import json` with `import orjson` throughout the codebase. This is the lowest-effort, highest-ROI optimization. orjson is a drop-in replacement for most use cases. The only API difference is that `orjson.dumps()` returns bytes instead of str.

**Why this over a full rewrite:** The JSON parsing itself is the bottleneck, not the surrounding Python code. orjson already provides native Rust performance for this specific operation. Writing a custom Rust extension for JSON handling would duplicate orjson's work.

---

### ~~HOTSPOT 4: Neo4j Driver Communication~~ RETRACTED

**CORRECTION (from integration-architect):** This codebase uses **Memgraph via `pymgclient`** (a C extension), NOT the Neo4j Python driver. There is no `neo4j` dependency in `pyproject.toml`. The `neo4j-rust-ext` package patches the Neo4j driver's PackStream implementation and has **zero effect** on `pymgclient`. This recommendation is retracted.

`pymgclient` is already a C extension with low overhead. CPU profiling confirms database serialization (protobuf) is negligible at 0.17s total. No language rewrite is needed for the database communication layer.

---

### HOTSPOT 5: Embedding Cache Hashing

**Files:** `embedder.py` (EmbeddingCache class)

**Workload:** SHA256 hashing of code snippets for cache key generation. Each snippet is hashed via `hashlib.sha256(content.encode()).hexdigest()`. For large codebases, thousands of snippets are hashed.

**Recommended Language:** Conditional: BLAKE3 (Rust-backed) if profiling confirms hashing as bottleneck

**Projected Speedup:** 4x to 10x (for hashing only)

**Evidence:**
- Python's hashlib SHA256 is already implemented in C (OpenSSL), so it's reasonably fast. Rust SHA256 achieves roughly 1.5x over Python's hashlib. [Source: users.rust-lang.org/t/hash-digest-performance-rust-vs-python/89686]
- If hashing is confirmed as a bottleneck, switching to BLAKE3 (via the `blake3` Python package, which is Rust-backed) provides 4x to 10x speedup over SHA256 because BLAKE3 is inherently faster and uses SIMD parallelism. [Source: devtoolspro.org/articles/sha256-alternatives-faster-hash-functions-2025/]
- The `blake3` Python package is a drop-in hash function replacement. API change is minimal: `blake3.blake3(content.encode()).hexdigest()`.

**Architecture:** Replace `hashlib.sha256` with `blake3.blake3` in the `EmbeddingCache._content_hash()` method. This is a one-line change. Note: existing caches would need to be regenerated since hash values will differ.

**CPU PROFILING RESULT: Hashing is NOT a bottleneck.** `_hash_file()` costs only 0.04s total (0.1%) across 453 calls. SHA-256 hashing is fast and not worth optimizing. BLAKE3 swap is deprioritized.

**Additional structural insight (MEDIUM severity):** The embedding pipeline at `embedder.py:109-126` and `unixcoder.py:97-107` crosses the Python/C boundary 3+ times per embedding: Python `list[list[int]]` to `torch.tensor` (copy), through PyTorch C++ backend (efficient), `.cpu().numpy()` (copy), `.tolist()` (N allocations for N-dim vector). Each crossing involves full memory copies and new container allocations. In Rust with `tch-rs`, tensor references can be held throughout without conversion overhead, providing 2x to 3x improvement on the embedding data path itself (separate from model inference time).

---

### HOTSPOT 6: File Traversal and Processing Pipeline

**Files:** `parsers/structure_processor.py`, `graph_updater.py` (file walking, `should_skip_path`)

**Workload:** Walking repository directories, reading files, determining language, applying gitignore/skip rules, and feeding files into the parser pipeline.

**Recommended Language:** Python (with concurrency improvements)

**Projected Speedup:** 3x to 5x (via pathlib fix + deduplication, not language rewrite)

**CPU PROFILING DATA:**
- `should_skip_path()`: **4.29s cumulative (13.7%)** across 59,270 calls. Dominated by `pathlib.relative_to()` at 3.18s across 54,519 calls, which creates intermediate `PurePosixPath` objects internally.
- `_collect_eligible_files()`: **4.71s cumulative (15.1%)** from a single call. The `rglob` itself costs only ~0.4s, but `should_skip_path` per file dominates.
- `identify_structure()`: **1.57s cumulative (5.0%)** from a single call. Performs a **duplicate** `rglob("*")` pass with separate `should_skip_path()` calls.
- **Key insight from profiling:** File traversal is NOT I/O-bound as originally assumed. The bottleneck is Python pathlib object overhead (creating intermediate Path objects for every `relative_to()` call), not filesystem I/O (`posix.scandir` costs only 0.42s). Using string-based path operations instead of pathlib would eliminate most of this overhead. Additionally, merging the duplicate traversal passes would cut FS stat calls in half.

**I/O PROFILING DATA (confirms NOT I/O-bound):**
- Actual disk I/O for the entire workload totals only **0.85s (6.1% of 14.0s)**. File reads: 0.02s, hashing: 0.02s, protobuf serialization: 0.01s, JSON cache: 0.001s.
- `pathlib.relative_to()` performs **zero disk I/O**. It constructs intermediate `PurePosixPath` objects via `__init__`, `is_relative_to`, `with_segments`, `_from_parsed_parts`. Measured at **10.6 us/call**.
- **String slice equivalent: 0.065 us/call (163x faster).** This is the measured speedup from the I/O profiler for replacing `pathlib.relative_to()` with string slicing.
- Duplicate `rglob("*")` traversals cost ~0.80s combined (two passes of ~0.40s each scanning 59,283 entries).

**Evidence:**
- The `rglob` filesystem traversal itself is fast (0.42s). The 4.29s in `should_skip_path` is pure Python object creation overhead from pathlib.
- The real opportunity is (a) replacing `pathlib.relative_to()` with string slicing (163x faster per call), and (b) merging the two separate `rglob` passes into one.

**Architecture:** Keep file traversal in Python. Fix pathlib overhead first (Priority 0b). Thread-based parallelism for file processing is less impactful than originally estimated: CPU profiling shows tree-sitter parsing is only 0.6% of total CPU, so parallelizing parsing yields minimal gains. The dominant bottleneck (48.3%) is in the post-parsing call resolution phase, which is sequential and GIL-bound.

**Why not Rust for traversal:** The per-file processing calls into tree-sitter (C library) and constructs Python objects. The overhead is in path manipulation (pathlib), not traversal I/O. A string-based path fix in Python is sufficient.

**Revised concurrency estimate (from concurrency analysis):** Original 3x-6x estimate for parallel file parsing revised downward since tree-sitter parsing is only 0.6% of CPU. Parallelism gains are secondary to algorithmic and native extension improvements.

**Note (from concurrency analysis):** The Memgraph/Neo4j flush layer already uses ThreadPoolExecutor with separate connections, so the I/O layer is well structured and does not need a language rewrite.

---

### HOTSPOT 7: String Processing in Call Resolution

**Files:** `parsers/call_resolver.py`, `parsers/import_processor.py`

**Workload:** Regex matching (`_SEPARATOR_PATTERN`, `_CHAINED_METHOD_PATTERN`), string splitting, qualified name construction (f-string concatenation), dict lookups in import maps.

**Recommended Language:** Rust (bundled with Hotspot 1 and 2 rewrites)

**Projected Speedup:** 5x to 20x (as part of the combined AST processing extension)

**Evidence:**
- Rust string processing is 10x to 80x faster than Python for CPU-intensive operations. [Source: blog.jetbrains.com/rust/2025/11/10/rust-vs-python-finding-the-right-balance]
- The call resolution logic is tightly coupled to AST traversal (it runs during the call processing pass). Moving it into the same Rust extension as Hotspot 1 eliminates all Python object creation overhead for intermediate strings.
- The regex patterns used are simple (separator splitting, method chaining detection) and would be even faster using Rust's `regex` crate, which uses finite automata rather than Python's backtracking regex engine.
- **Structural analysis: Interpreter loop overhead (HIGH severity, 5x to 20x).** The innermost loops at `call_processor.py:285-328`, `import_processor.py:164-172`, and `graph_updater.py:405-434` execute ~20 to 30 Python bytecode instructions per iteration just for control flow (dynamic dispatch, isinstance checks with MRO traversal, reference count updates), before the actual work in called methods. A compiled language would inline these calls and eliminate dispatch overhead entirely.

**Architecture:** Include call resolution logic in the Hotspot 1 Rust extension. The Rust code performs AST traversal, call name extraction, and call resolution in a single pass, returning only the final resolved call relationships to Python.

---

## CPU Profiling Summary (from cProfile)

**Workload:** `GraphUpdater.run(force=True)` indexing 352 Python files, 31.2s total, 179M function calls.

| Rank | Function | Self Time | Cum. Time | % Total | Calls | Root Cause |
|---|---|---|---|---|---|---|
| 1 | `find_ending_with` | 7.91s | 15.07s | 48.3% | 27,376 | Linear scan fallback, 123.7M `endswith` calls |
| 2 | `should_skip_path` | 0.07s | 4.29s | 13.7% | 59,270 | Pathlib `relative_to` overhead (3.18s) |
| 3 | `build_local_variable_type_map` | 0.004s | 2.59s | 8.3% | 5,228 | Repeated AST traversal, no caching |
| 4 | Loguru logging | 0.41s | 1.84s | 5.9% | 91,119 | Debug-level overhead at high call volume |
| 5 | `identify_structure` | 0.02s | 1.57s | 5.0% | 1 | Duplicate FS traversal + should_skip_path |
| 6 | `QueryCursor.captures` | 0.78s | 0.78s | 2.5% | 11,028 | C extension, largely irreducible |
| 7 | `Parser.parse` | 0.19s | 0.19s | 0.6% | 352 | C extension, already fast |
| 8 | `_hash_file` | 0.001s | 0.04s | 0.1% | 453 | Negligible |

**Key observations:**
1. 48.3% of CPU is in a single function with an algorithmic fix available (index miss rate)
2. Tree-sitter C operations (parse + captures) total only 1.0s (3.1%), confirming the bottleneck is Python wrapper code
3. Protobuf serialization is negligible (0.17s total)
4. File hashing is negligible (0.04s total)

---

## Structural Performance Ceilings (from Static Analysis)

The static-pattern-analyst identified 9 categories of Python runtime overhead that create inherent performance ceilings. These are organized by severity:

| Severity | Pattern | Overhead Multiplier | Rewrite Benefit |
|---|---|---|---|
| CRITICAL | AST tree traversal (pointer chasing + dynamic dispatch) | 20x-50x per node visit | Highest |
| CRITICAL | GIL preventing parallel parsing/resolution | Linear with core count | Highest |
| HIGH | String operations on qualified names | 5x-15x | High |
| HIGH | Dictionary lookups in hot loops | 3x-10x | High |
| HIGH | Interpreter loop overhead in tight iteration | 5x-20x | High |
| MEDIUM | `bytes.decode("utf-8")` on every node text access | 3x-5x | Moderate |
| MEDIUM | Object headers + reference counting on all intermediates | 2x-5x memory reduction | Moderate |
| MEDIUM | Embedding data format conversions (Python/Tensor/NumPy) | 2x-3x per embedding | Low (model dominates) |
| MEDIUM-HIGH | File I/O with Path objects (revised upward: CPU profiling shows 13.7% of CPU) | 3x-5x | Significant (pathlib overhead, not I/O) |

**Key insight:** The CRITICAL and HIGH severity patterns are all concentrated in the same code: the parser/ingestion pipeline (Hotspots 1, 2, 7). A single Rust extension covering AST traversal, trie operations, and call resolution would address 5 of the 9 overhead categories simultaneously.

**Diffuse overhead note:** Object header overhead (16 bytes per object minimum) and reference counting affect all Python code. Every intermediate `tuple`, `list[str]` from `.split()`, and NamedTuple is heap-allocated with refcounting. A `tuple[str, str]` is ~100 bytes in Python vs ~16 bytes in Rust (stack-allocated). This is not directly addressable per hotspot but is eliminated automatically when hot paths move to Rust.

## Memory Profiling Data (from tracemalloc)

Memory profiling confirms that Python's object model creates significant memory overhead in the same hotspot areas identified by CPU profiling and structural analysis:

| Structure | Python (measured) | Estimated Rust | Memory Ratio |
|---|---|---|---|
| Tree-sitter AST node wrappers | 87.3 MiB (343 files, 1.67M wrapper objects) | ~5-10 MiB (direct C struct access) | 9-17x |
| EmbeddingCache `list[float]` | 48.6 MiB (2k embeddings) | ~6 MiB (packed f32 arrays) | 8x |
| import_mapping | 5.6 MiB (2k modules) | ~1.5 MiB | 3.7x |
| rel_groups | 3.6 MiB | ~800 KiB | 4.5x |
| FunctionRegistryTrie | 3.5 MiB (10k entries, 13.2k intermediate dicts) | ~400 KiB (arena-allocated trie) | 9x |

**Key memory findings:**
1. **AST node wrappers (87.3 MiB)** are the largest memory consumer. Each `node.children` access creates new Python Node wrapper objects around C pointers. A Rust extension performing extraction natively would avoid all wrapper allocation, reinforcing the Hotspot 1 recommendation.
2. **EmbeddingCache (48.6 MiB)** uses Python `float` objects (28 bytes each). A 768-dim embedding as `list[float]` uses ~21.5 KiB vs ~6 KiB as packed f32. Switching to numpy arrays (Python-level fix) would provide 4x reduction; Rust packed f32 arrays would be optimal.
3. **FunctionRegistryTrie (3.5 MiB)** has 13.2k intermediate Python dict objects (64+ bytes each) for 10k entries. A Rust compact trie with byte slices or arena allocation would use ~400 KiB.
4. **String duplication:** Qualified names are stored in multiple structures (trie, `_entries`, `simple_name_lookup`, `import_mapping`). Python's string interning does not cover long qualified names. Rust string interning via a global interner would deduplicate these.

---

## Non-Language Optimizations (Algorithmic / Python-Level)

CPU profiling and concurrency analysis identified multiple high-impact optimizations that do NOT require a language rewrite. **These should be implemented first** as they collectively address over 70% of CPU time.

### ALGORITHMIC 0: Fix `find_ending_with()` Simple Name Index (THE #1 PRIORITY)

**Issue:** `FunctionRegistryTrie.find_ending_with()` at `graph_updater.py:156` accounts for **48.3% of total CPU time** (15.07s of 31.2s). The `_simple_name_lookup` index has an 80.7% miss rate, causing a linear scan fallback with 123.7M `str.endswith()` calls.

**Projected Speedup:** ~2x on total runtime (eliminating 15s from a 31s run)

**Action:** Build a proper reverse index mapping simple (unqualified) names to their list of qualified names. Populate it during trie insertion. This converts the O(N) linear scan into an O(1) dict lookup per call. This is a pure Python data structure fix requiring minimal code changes.

### ALGORITHMIC 0b: Replace pathlib `relative_to()` with String Operations

**Issue:** `should_skip_path()` consumes **4.29s (13.7%)** due to pathlib's `relative_to()` creating intermediate `PurePosixPath` objects 54,519 times. The actual filesystem I/O is only 0.42s.

**Projected Speedup:** ~3x on the file collection phase (reducing 4.29s to ~0.5s)

**Action:** Replace `path.relative_to(base)` with `str(path)[len(str(base))+1:]` or equivalent string slicing. Merge the duplicate `rglob("*")` passes from `_collect_eligible_files()` and `identify_structure()` into a single traversal. Additionally, pre-filter at directory level: walk the tree manually and skip ignored directories (.git, __pycache__, node_modules, site) immediately rather than enumerating all 59K descendants and filtering after. This would reduce traversal from 59K to ~600 paths.

### ALGORITHMIC 0c: Cache Type Inference Results Per File

**Issue:** `build_local_variable_type_map()` consumes **2.59s (8.3%)** across 5,228 calls, re-traversing ASTs that have already been parsed with no caching across calls within the same file.

**Projected Speedup:** ~2x to 5x on the type inference phase

**Action:** Memoize type inference results per function AST node. Since the AST is immutable after parsing, results are safe to cache.

### ALGORITHMIC 0d: Reduce Debug Logging Overhead

**Issue:** Loguru logging consumes **1.84s (5.9%)** across 91,119 calls, including 85,099 debug-level calls processed even when not displayed.

**Projected Speedup:** Eliminates ~1.8s (5.9% of total runtime)

**Action:** Guard debug log calls with `if logger.isEnabledFor(DEBUG):` or use lazy formatting, or set the minimum log level to INFO in production.

### ALGORITHMIC 0e: Use Compact JSON for Graph Export

**Issue:** `_write_graph_json()` in `main.py:744` uses `json.dump(graph_data, f, indent=2)` which is **8x slower** than compact JSON (86ms vs 11ms for 10K nodes) and produces 1.5x larger output.

**Projected Speedup:** 8x on graph JSON export

**Action:** Use compact JSON (no indent) for machine consumption. Add a separate `--pretty` flag for human-readable output.

### ALGORITHMIC 0f: Binary Format for Embedding Cache

**Issue:** 500 embeddings (768-dim float vectors) stored as JSON = 6.3MB, save = 149ms, load = 38ms. Each embedding is serialized as a JSON array of 768 float values with full decimal precision.

**Projected Speedup:** 10x+ on embedding cache I/O (both size and speed)

**Action:** Use numpy `.npy` or `.npz` format for embedding vectors. A 768-dim float32 vector is 3 KiB in binary vs ~15 KiB in JSON text.

### ALGORITHMIC 1: Batch Embedding API Usage

**Issue:** The `embed_code_batch` function exists but is unused in the main pipeline. The embedding phase calls `embed_code` per-item instead.

**Projected Speedup:** Potentially 5x to 12x on the embedding phase (based on batching reducing HTTP round-trip overhead and enabling server-side batching). The Baseten case study showed 12x throughput improvement from proper batching with GIL release. [Source: baseten.co/blog/your-client-code-matters-10x-higher-embedding-throughput-with-python-and-rust/]

**Action:** Fix the Python pipeline to use `embed_code_batch`. This is a Python-level fix with zero language rewrite cost.

### ALGORITHMIC 2: Incremental Call Re-Resolution

**Issue:** The realtime updater (`realtime_updater.py`) performs full call re-resolution on every file change, reprocessing the entire function registry and call graph.

**Projected Speedup:** 10x to 100x for incremental updates (per the concurrency analysis), since only the changed file's calls and its direct dependents need re-resolution.

**Action:** Implement incremental call resolution that tracks which qualified names changed and only re-resolves calls that reference those names. This is an algorithmic improvement, not a language choice.

**These two Python-level fixes should be implemented BEFORE the Rust extension work**, as they may reduce the urgency of the more expensive rewrites.

---

## Language Comparison Matrix

| Criterion | Rust (PyO3) | Cython | Go | Mojo | Zig |
|---|---|---|---|---|---|
| **Raw performance** | Excellent (C-level) | Good (C-level for numeric) | Good (2x slower than Rust) | Excellent (claims C-level) | Excellent (C-level) |
| **Python FFI quality** | Excellent (PyO3 is mature, zero-copy numpy, vectorcall) | Native (compiles to C extension) | Poor (cgo+ctypes, limited) | Poor (early stage, no stable FFI) | Poor (C ABI only, no Python tooling) |
| **Ecosystem for this workload** | Excellent (tree-sitter crate, regex, serde_json, radix_trie) | Limited (no tree-sitter, string ops need C) | Moderate (tree-sitter-go exists) | None (no tree-sitter, no graph libs) | Limited (tree-sitter C API via @cImport) |
| **Memory safety** | Excellent (borrow checker) | Poor (manual, C-level) | Good (GC, but adds pauses) | Unknown (early stage) | Moderate (manual, but safer than C) |
| **Build complexity** | Moderate (maturin makes it easy) | Low (cythonize) | High (separate binary, IPC needed) | High (Modular toolchain only) | High (no Python tooling) |
| **Developer availability** | Growing (22% increase in Python+Rust developers in 2025) | Declining | Low for Python extensions | Very low | Very low |
| **Real-world precedent** | ruff, uv, polars, pydantic-core, orjson | numpy, scipy (legacy) | None for similar tools | None for similar tools | None for similar tools |

### Why Rust is the clear winner for this codebase:

1. **PyO3 maturity:** PyO3 is the most mature Python FFI framework, with zero-copy mechanisms, vectorcall support, and 92% of pure Rust performance. [Source: pyo3.rs/main/performance]

2. **Tree-sitter native support:** Tree-sitter's runtime is written in C/Rust. Rust can call the tree-sitter C API directly without any Python intermediary, eliminating the per-node FFI overhead that is the primary bottleneck.

3. **Industry precedent:** The most successful Python performance tools of 2024-2025 are all Rust-backed: ruff (linter, 10-100x faster), uv (package manager), polars (DataFrame, 5-10x faster), pydantic-core (validation, 17x faster), orjson (JSON, 15x faster). [Source: thenewstack.io/rust-pythons-new-performance-engine/]

4. **maturin build system:** maturin (also by the PyO3 team) simplifies building and distributing Rust Python extensions as standard wheels. No complex build system integration needed.

---

## Prioritized Implementation Order

### Phase 0: Python Algorithmic Fixes (addresses ~72% of CPU time)

| Priority | Fix | Effort | CPU Time Saved | % of Total |
|---|---|---|---|---|
| 0a | Fix `find_ending_with` simple name index | Very low | ~15s | 48.3% |
| 0b | Replace pathlib `relative_to` with string ops + merge duplicate rglob | Low | ~4s | 13.7% |
| 0c | Cache type inference results per file | Low | ~2s | 8.3% |
| 0d | Reduce debug logging overhead | Very low | ~1.8s | 5.9% |
| 0e | Batch embedding API usage | Very low | TBD (embedding phase) | TBD |
| 0f | Incremental call re-resolution | Medium | 10x-100x on realtime | N/A (realtime only) |

**Phase 0 collectively addresses ~72% of measured CPU time (22.8s of 31.2s) with pure Python changes.** After Phase 0, the expected baseline would be ~8-10s for the same 352-file workload.

### Phase 1: Drop-in Rust-backed Libraries (zero code changes)

| Priority | Library | Effort | Expected Speedup |
|---|---|---|---|
| 1a | JSON serialization (orjson) | Very low (dependency swap) | 5x-15x on JSON ops |
| ~~1b~~ | ~~Neo4j driver (neo4j-rust-ext)~~ | ~~RETRACTED~~ | ~~Inapplicable: codebase uses Memgraph/pymgclient, not Neo4j~~ |
| 1b | Embedding hash (BLAKE3) | Very low (one-line change) | 4x-10x on hashing (confirmed negligible: 0.04s) |

**Note from profiling:** File hashing (`_hash_file`) is only 0.04s total (0.1%), and protobuf serialization is 0.17s total. These are negligible. BLAKE3 (Priority 1b) can be deprioritized. orjson remains worthwhile for larger codebases. The neo4j-rust-ext recommendation was retracted because this codebase uses Memgraph via `pymgclient` (C extension), not the Neo4j Python driver.

### Phase 2: Rust Extension (addresses remaining CPU-bound overhead)

| Priority | Component | Effort | Expected Speedup |
|---|---|---|---|
| 2a | AST traversal + type inference (Rust) | High (new extension) | 20x-50x on AST processing |
| 2b | Trie + call resolution (Rust) | Medium (extend 2a) | 10x-50x on lookups (GIL-bound) |

**Phase 2 should be implemented as a single `codebase-rag-core` Rust crate**, since AST traversal, trie operations, and call resolution are tightly coupled. The Rust extension MUST release the GIL via `Python::allow_threads` during parsing and traversal to preserve thread-level parallelism.

**Amdahl's law caveat (from integration-architect):** Tree-sitter C operations (parse + captures) are only 3.1% of CPU time. A 16x speedup on 3.1% yields only 1.03x total improvement. The value of the Rust AST extension is NOT in speeding up tree-sitter itself (already fast C code), but in eliminating the Python wrapper overhead around it: type inference re-traversal (8.3%), call resolution string operations, and interpreter loop overhead in the tight iteration loops. These Python-side AST costs total ~20% of CPU, making the combined Phase 2 extension worthwhile after Phase 0 algorithmic fixes are applied.

### Phase 3: Architecture Improvements

| Priority | Change | Effort | Expected Speedup |
|---|---|---|---|
| 3a | File processing parallelism (ThreadPoolExecutor) | Medium | Downgraded: marginal gains |

**Phase 3 is downgraded based on revised analysis.** CPU profiling shows tree-sitter parsing is only 0.6% of CPU, and the file processing bottleneck (`pathlib.relative_to` at 13.7%) is GIL-bound pure Python that ThreadPoolExecutor cannot parallelize. The pathlib fix (Phase 0b, string slicing, 163x faster) is the correct solution, not parallelism. ProcessPoolExecutor for call resolution is also impractical: memory profiling shows 170 MiB peak memory, making serialization cost too high. The Rust PyO3 native extension (Phase 2) is the only viable path for parallelizing call resolution, as it can release the GIL via `Python::allow_threads`.

---

## Sources

- [Gauge.sh: Python extensions should be lazy](https://www.gauge.sh/blog/python-extensions-should-be-lazy) - 16x speedup moving AST processing to Rust
- [Neo4j Python Driver 10x Faster With Rust](https://neo4j.com/blog/developer/python-driver-10x-faster-with-rust/) - neo4j-rust-ext benchmarks
- [Baseten: 12x higher embedding throughput with Python and Rust](https://www.baseten.co/blog/your-client-code-matters-10x-higher-embedding-throughput-with-python-and-rust/) - PyO3 GIL release pattern
- [orjson: 500% Faster JSON in Python](https://medium.com/codeelevation/want-500-faster-json-in-python-try-orjson-powered-by-rust-22995c25c312) - JSON serialization benchmarks
- [PyO3 Performance Guide](https://pyo3.rs/main/performance) - FFI overhead characteristics
- [Rust: Python's New Performance Engine](https://thenewstack.io/rust-pythons-new-performance-engine/) - Industry adoption trends
- [Comparing Cython to Rust for Python Extensions](https://willayd.com/comparing-cython-to-rust-evaluating-python-extensions.html) - Graph algorithm benchmarks
- [SHA-256 Alternatives: BLAKE3 vs SHA-3 Speed Comparison](https://devtoolspro.org/articles/sha256-alternatives-faster-hash-functions-2025/) - Hash function benchmarks
- [Neo4j Performance Recommendations](https://neo4j.com/docs/python-manual/current/performance/) - Batch loading best practices
- [JetBrains Rust vs Python 2025](https://blog.jetbrains.com/rust/2025/11/10/rust-vs-python-finding-the-right-balance-between-speed-and-simplicity/) - String processing benchmarks
- [Databooth: Benchmarking Python with Cython, C, C++, and Rust](https://www.databooth.com.au/posts/py-num-bench/) - Extension comparison
- [Cython, Rust, and more: choosing a language for Python extensions](https://pythonspeed.com/articles/rust-cython-python-extensions/) - When to use each approach
- [ast-grep](https://github.com/ast-grep/ast-grep) - Rust tree-sitter code analysis tool
- [Rust trie implementations](https://dev.to/timclicks/two-trie-implementations-in-rust-ones-super-fast) - Trie performance
- [Corrode: Migrating from Python to Rust](https://corrode.dev/learn/migration-guides/python-to-rust/) - Migration guide
- [Datadog: Migrating static analyzer from Java to Rust](https://www.datadoghq.com/blog/engineering/how-we-migrated-our-static-analyzer-from-java-to-rust/) - Code analysis tool migration
