# Integration Feasibility Report

## Build System and Deployment Context

**Package manager:** `uv` (Astral), defined in `pyproject.toml` with `uv.lock`
**Build backend:** setuptools (via `[tool.setuptools]`), three packages: `codebase_rag`, `codec`, `cgr`
**Distribution:** PyPI wheel, Docker image (`python:3.12-slim`), PyInstaller binary
**CI/CD:** Pre-commit hooks (ruff, ty, bandit), Makefile targets
**Python version:** 3.12+ required
**Key native dependency:** `pymgclient` (compiled from source with `--no-binary-package`)

---

## Candidate 1: orjson (Drop-in JSON Replacement)

### Integration Strategy
Drop-in dependency swap. Replace `import json` with `import orjson` in graph_loader.py, graph_updater.py, services/graph_service.py, embedder.py, stdlib_extractor.py.

### Integration Overhead
- **Serialization boundary:** Zero. orjson is a direct Python C extension. No FFI marshalling.
- **API difference:** `orjson.dumps()` returns `bytes` not `str`. Every `json.dumps()` call site that feeds the result to something expecting `str` needs `.decode()`. In this codebase, the `_write_graph_json` function in `main.py` uses `json.dump(graph_data, f, indent=2, ensure_ascii=False)` which would need adjustment since orjson's `OPT_INDENT_2` flag replaces the `indent` parameter.
- **Protobuf service:** `services/protobuf_service.py` does not use JSON. No impact.
- **Hash cache I/O:** `_save_hash_cache` and `_load_hash_cache` use `json.dump/load` with file objects. orjson does not support file-object streaming; need to call `orjson.dumps()` then `f.write()`.
- **Embedding cache:** Same pattern. `EmbeddingCache.save()` uses `json.dump(self._cache, f)`. Requires manual write of bytes.
- **Build system change:** Add `orjson>=3.10.0` to `[project.dependencies]`. orjson publishes pre-built wheels for all platforms. No toolchain change.
- **Docker impact:** Zero. orjson wheels are self-contained.
- **PyInstaller impact:** Add `--hidden-import orjson`. orjson is a single .so/.pyd file, minimal size increase.

### Net Projected Gain
- **Raw gain:** 5x to 15x on JSON operations
- **Integration overhead:** Near zero. ~10 call sites need minor API adjustments (bytes vs str, file.write vs json.dump).
- **Net gain:** 5x to 15x on JSON operations. No overhead erosion.
- **Risk:** Very low. Widely adopted library (polars, FastAPI, etc.)

---

## Candidate 2: neo4j-rust-ext (NOT APPLICABLE)

### Integration Strategy
NOT APPLICABLE. This codebase uses **Memgraph** via `pymgclient` (mgclient C library), NOT the Neo4j Python driver. The `neo4j-rust-ext` package patches the `neo4j` Python driver's PackStream implementation. It has zero effect on `pymgclient`.

### Assessment
- `services/graph_service.py` imports `mgclient`, connects to Memgraph, and uses the mgclient C API directly.
- There is no `neo4j` dependency in `pyproject.toml`.
- The language researcher's recommendation was based on an incorrect assumption about the database driver.

### Alternative for Memgraph Driver
- pymgclient is already a C extension wrapping Memgraph's C client library. It is already compiled code.
- The actual overhead is in Python-side batch construction (building `list[RelBatchRow]` and `list[NodeBatchRow]` dicts), Cypher query string formatting, and result deserialization in `_cursor_to_results`.
- The `_cursor_to_results` method iterates cursor results and builds `list[ResultRow]` via `dict(zip(column_names, row))`. This is pure Python overhead.
- Potential optimization: Use cursor iteration in C rather than Python, but this requires pymgclient changes, not neo4j-rust-ext.

### Net Projected Gain
- **Net gain:** 0x. This recommendation is inapplicable.

---

## Candidate 3: BLAKE3 (Embedding Cache Hashing)

### Integration Strategy
Drop-in hash function replacement in `EmbeddingCache._content_hash()` and `_hash_file()` in `graph_updater.py`.

### Integration Overhead
- **Serialization boundary:** Zero. blake3 Python package is a C extension.
- **API change:** `hashlib.sha256(content.encode()).hexdigest()` becomes `blake3.blake3(content.encode()).hexdigest()`. One-line change per call site.
- **Cache invalidation:** Existing embedding caches (`.qdrant_code_embeddings/embedding_cache.json`) and file hash caches (`.file_hashes.json`) will be invalidated because hash values change. This forces a full re-index on first run after the change.
- **Build system change:** Add `blake3>=1.0.0` to dependencies. blake3 publishes pre-built wheels.
- **Docker/PyInstaller:** Minimal impact. blake3 is a small native extension.

### Net Projected Gain
- **Raw gain:** 4x to 10x on hashing operations
- **Practical impact:** Hashing is NOT the bottleneck. `_hash_file` reads 8KB chunks and hashes them. For a typical codebase (1000 files, avg 5KB), total hashing takes ~5ms (already fast because hashlib SHA256 is C-backed). The real I/O cost is the filesystem reads, not the hash computation.
- **Embedding cache hashing:** Similarly marginal. `_content_hash` hashes short code snippets. Each call takes microseconds.
- **Cache invalidation cost:** Forces a full re-indexing pass (potentially minutes for large repos), creating a one-time negative impact that dwarfs the per-operation savings.
- **Net gain:** Negligible in practice. The 4x to 10x improvement applies to an operation that takes microseconds per call.
- **Recommendation:** Skip unless profiling proves hashing is >5% of total wall clock time.

---

## Candidate 4: Rust AST Processing Extension (via PyO3/maturin)

### Integration Strategy
Build a Rust extension crate (e.g., `codebase-rag-core`) that accepts file bytes + language enum and returns structured extraction results. Use PyO3 for Python bindings and maturin for building.

### Integration Overhead Assessment

**Data crossing the FFI boundary:**
- **Input:** File bytes (`bytes`) and language enum (`str`). Minimal copy cost. PyO3 provides zero-copy access to Python bytes via `&[u8]`.
- **Output:** The Rust extension must return complex structured data to Python:
  - Function definitions: list of (qualified_name, name, start_line, end_line, decorators, docstring)
  - Class definitions: list of (qualified_name, name, parent_classes, methods)
  - Call relationships: list of (caller_qn, callee_qn, caller_type, callee_type)
  - Import mappings: dict of (module_qn -> dict of (local_name -> imported_qn))

  Each of these requires constructing Python objects from Rust data. For a file with 50 functions and 200 call sites, this means ~250 Python dict/tuple creations on the return path.

**Boundary crossing cost estimate:**
- PyO3 object creation: ~100ns per Python object (dict, str, list element)
- For a typical large file (50 functions, 100 calls, 20 imports): ~170 result objects * 5 fields each = ~850 Python object creations = ~85 microseconds
- Per-file processing time in Python currently: ~5-50ms (depends on file size)
- **FFI boundary cost as fraction of saved time: <1%**. This is excellent.

**Coupling analysis:**

The Rust extension needs to replicate or subsume:
1. `definition_processor.py` (7.5KB): Function/class/method extraction from AST
2. `call_processor.py` (13.7KB): Call relationship extraction
3. `call_resolver.py` (24.4KB): Call resolution with trie lookups, inheritance chains, import maps
4. `import_processor.py` (40KB): Language-specific import parsing (Python, JS/TS, Java, Rust, Go, C++, Lua)
5. `function_ingest.py` (16.4KB): Function registration and qualified name resolution
6. `type_inference.py` (5.8KB) + language-specific engines: Type inference for call resolution
7. `FunctionRegistryTrie` in `graph_updater.py`: Trie data structure

Total: ~110KB of Python code with complex multi-language logic spanning 8+ languages.

**Build system changes:**
- Add `maturin` as build dependency
- Add a `Cargo.toml` at project root or in a subdirectory (e.g., `rust/`)
- Add `tree-sitter` and language grammar crates as Rust dependencies
- Modify `pyproject.toml` to include maturin build configuration or create a separate wheel
- CI needs Rust toolchain (rustup) installed
- Docker builder stage needs Rust toolchain (~300MB image layer increase)
- PyInstaller needs to collect the compiled .so/.pyd from the Rust extension

**Compatibility concerns:**
- Tree-sitter versions must match between Rust and Python. The codebase uses `tree-sitter==0.25.2`. The Rust `tree-sitter` crate version must be compatible.
- The Rust extension must handle all 9 supported languages with language-specific AST patterns.
- The `IngestorProtocol` interface (ensure_node_batch, ensure_relationship_batch) is called from within the processing loop. Either the Rust extension calls back into Python (expensive, defeats the purpose) OR the Rust extension accumulates all results and returns them in bulk (preferred).

**Critical: tree-sitter Node FFI constraint (from adversarial review):**
- Tree-sitter `Node` objects are C-level pointers that cannot be marshalled across FFI boundaries. The call resolution pipeline operates on `Node` objects thousands of times per file.
- This rules out an incremental approach (e.g., rewriting just CallResolver in Rust while keeping Python tree-sitter nodes). The Rust extension must parse files from scratch using the `tree-sitter` Rust crate directly, producing Rust-native `Node` references.
- Consequence: the Rust extension is an all-or-nothing replacement of the entire parse-extract-resolve pipeline. Incremental migration is not feasible. This increases both effort and risk.

**Deployment complexity:**
- Requires publishing platform-specific wheels (linux-x86_64, linux-aarch64, macos-x86_64, macos-arm64, windows-x64)
- maturin handles this via GitHub Actions + `maturin[zig]` for cross-compilation
- Users without pre-built wheels need a Rust toolchain to install from source
- The Docker image build becomes significantly more complex (multi-stage with Rust)

### Net Projected Gain
- **Raw gain:** 10x to 16x on AST processing (the primary CPU hotspot)
- **FFI boundary overhead:** <1% (excellent input/output ratio: bytes in, structured results out)
- **Build system overhead:** Significant one-time cost. Ongoing CI cost of ~2-3 min for Rust compilation per release.
- **Development effort:** High. ~110KB of Python code to rewrite in Rust, with complex multi-language pattern matching.
- **Net gain:** 9x to 15x on AST processing operations, assuming bulk return pattern.
- **Risk:** Medium-high. Large surface area, 8+ language parsers, tight coupling with existing Python data structures.
- **Recommendation:** High value but should be incremental. Start with a single language (Python parser) as proof of concept, measure actual gains, then expand.

---

## Candidate 5: Rust FunctionRegistryTrie (via PyO3)

### Integration Strategy
Expose a Rust-backed trie as a Python class via PyO3, bundled in the same crate as Candidate 4.

### Integration Overhead Assessment

**Data crossing the FFI boundary:**
- **Insert:** Python str -> Rust &str (zero-copy via PyO3), Rust stores owned copy. Cost: one string allocation per insert.
- **Lookup (`__contains__`, `get`):** Python str -> Rust &str (zero-copy), returns bool or Python str. Cost: near zero per lookup.
- **Batch operations (`find_ending_with`, `find_with_prefix`):** Returns list of Python strings. For a query returning 50 matches, this means 50 Python string allocations.

**Boundary crossing cost estimate:**
- Single lookup: ~50ns (vs ~200ns in Python dict)
- `find_ending_with` returning 10 results: ~1us (vs ~50us scanning Python dict)
- The trie has hot-path usage in `call_resolver.py` where every call expression triggers 2-5 trie lookups.

**Coupling with Candidate 4:**
- If AST processing moves to Rust (Candidate 4), the trie must also be in Rust to avoid crossing back to Python for every lookup during call resolution.
- If Candidate 4 is NOT done, the Rust trie is still useful standalone, but the benefit is reduced because the Python call resolution code still creates Python strings for every lookup key.

**Build system changes:**
- Bundled with Candidate 4. No additional build complexity.

### Net Projected Gain
- **Raw gain:** 3x to 8x on trie operations
- **Standalone net gain (without Candidate 4):** 1.5x to 3x. Python call resolution code still creates string objects for lookup keys. FFI crossing happens per-lookup.
- **Combined net gain (with Candidate 4):** 3x to 8x. All trie operations happen in Rust with no FFI boundary during resolution.
- **Recommendation:** Only implement together with Candidate 4. Standalone, the integration overhead cuts the gains roughly in half.

---

## Candidate 6: File Processing Parallelism (Python)

### Integration Strategy
Use `concurrent.futures.ProcessPoolExecutor` to parallelize per-file processing in `GraphUpdater._process_files()`.

### Integration Overhead Assessment

**Serialization at boundary:**
- Each worker process needs: file path (Path, serializable), language queries (NOT serializable: contains tree-sitter Parser, Query, Language objects which are C pointers).
- **Critical problem:** `LanguageQueries` contains `Parser`, `Query`, and `Language` objects from tree-sitter, which are C-level objects that cannot be serialized across process boundaries.
- Each worker would need to call `load_parsers()` independently, loading all language grammars (~50ms startup cost per worker).
- Results (function definitions, call relationships) are Python dicts/tuples that serialize easily.

**State synchronization:**
- `FunctionRegistryTrie` is shared mutable state. Workers write to it during function registration, and readers need it during call resolution.
- With multiprocessing, each worker would have its own trie. Merging tries after parallel processing adds complexity.
- `import_mapping` in `ImportProcessor` is similarly shared mutable state.
- The three-pass architecture (structure -> definitions -> calls) has inherent sequential dependencies: pass 3 needs results from pass 2.

**GIL considerations:**
- `threading.Thread` would not help because call resolution is CPU-bound Python code held by the GIL.
- `ProcessPoolExecutor` bypasses GIL but introduces serialization overhead.
- Estimated per-file serialization overhead for results: ~0.1ms per file.
- For 1000 files on 4 cores: ~25ms total serialization overhead vs ~5000ms saved.

### Net Projected Gain
- **Raw gain:** 2x to 4x (limited by sequential passes and Amdahl's law)
- **Serialization overhead:** ~5ms for 1000 files (minimal)
- **Worker initialization overhead:** ~50ms per worker (grammar loading), amortized across files
- **Architecture complexity:** High. Requires restructuring the three-pass processing pipeline, managing shared state (trie, import maps), and handling errors across processes.
- **Net gain:** 1.5x to 3x after accounting for sequential bottlenecks (pass dependencies)
- **Recommendation:** Medium priority. Worth doing after Candidate 4 (Rust extension) is evaluated. If Candidate 4 makes per-file processing fast enough, parallelism becomes less critical.

---

## Candidate 7: String Processing in Call Resolution (Rust)

### Integration Strategy
Bundled with Candidate 4. Call resolution logic moves into the Rust AST processing extension.

### Integration Overhead
- **Standalone:** NOT recommended. Call resolution is deeply interleaved with trie lookups, import map lookups, and AST node access. Extracting just the string processing would require marshalling all context (import maps, trie state, class inheritance) across FFI on every call.
- **Bundled with Candidate 4:** Zero additional FFI overhead. The Rust extension performs call resolution as part of the same processing pass.

### Net Projected Gain
- **Standalone net gain:** Negative. The overhead of passing import maps and trie state across FFI for each call resolution would exceed the savings from faster string processing.
- **Bundled net gain:** 5x to 10x (absorbed into Candidate 4's gains)
- **Recommendation:** Only implement as part of Candidate 4.

---

## Summary: Feasibility Verdicts

| Candidate | Strategy | FFI Overhead | Build Impact | Net Gain | Verdict |
|---|---|---|---|---|---|
| 1. orjson | Dependency swap | None | Trivial | 5x-15x on JSON | **PROCEED** |
| 2. neo4j-rust-ext | N/A | N/A | N/A | 0x (wrong driver) | **REJECT** |
| 3. BLAKE3 hashing | Dependency swap | None | Trivial | Negligible | **SKIP** (not a bottleneck) |
| 4. Rust AST extension | PyO3/maturin crate | <1% | Significant | 9x-15x on AST | **PROCEED** (incremental) |
| 5. Rust trie | PyO3 (bundled #4) | ~50% standalone | Bundled with #4 | 1.5x-3x standalone, 3x-8x bundled | **BUNDLE with #4** |
| 6. File parallelism | ProcessPoolExecutor | ~5ms/1000 files | Moderate refactor | 1.5x-3x | **DEFER** (after #4) |
| 7. String processing | Rust (bundled #4) | Negative standalone | Bundled with #4 | Negative standalone, 5x-10x bundled | **BUNDLE with #4** |

## Key Finding: Integration Overhead Negation Analysis

The critical insight is that **Candidates 5 and 7 have negative net gains if implemented standalone** because the FFI boundary crossing cost exceeds the per-operation savings. They are only viable when bundled with Candidate 4, which keeps all related operations on the Rust side of the boundary.

This validates the principle: **a function 10x faster but with 8x overhead at the boundary is only 1.25x improvement.** For Candidates 5 and 7, the standalone case is even worse because the boundary must be crossed per-lookup (thousands of times per file) rather than per-file.

**Candidate 2 is completely inapplicable** due to incorrect driver assumption.

**Candidate 3 optimizes a non-bottleneck** (microsecond-level operations).

The only candidates with clear positive ROI accounting for integration overhead are:
1. **orjson** (zero overhead, significant JSON gains)
2. **Rust AST extension** (minimal overhead due to bytes-in/results-out architecture, massive CPU gains)

---

## ADDENDUM: Revised Analysis Based on CPU Profiling Data

The CPU profiling report (cProfile, 31.2s total, 179M function calls on 352 Python files) **dramatically changes the priority landscape.** The actual hotspots are fundamentally different from those assumed in the language recommendations.

### Profiling Reality vs. Language Researcher Assumptions

| Rank | Actual Hotspot | % CPU | Language Researcher Assumption |
|------|---------------|-------|-------------------------------|
| 1 | `find_ending_with` linear scan | 48.3% | Assumed trie was working; recommended Rust trie for data layout improvement |
| 2 | `should_skip_path` pathlib overhead | 13.7% | Not identified as a hotspot |
| 3 | `build_local_variable_type_map` (uncached AST retraversal) | 8.3% | Assumed this was part of general AST processing |
| 4 | Loguru debug logging overhead | 5.9% | Not identified |
| 5 | `identify_structure` (duplicate FS traversal) | 5.0% | Not identified |
| 6 | tree-sitter `QueryCursor.captures` | 2.5% | Assumed this was the primary bottleneck (10x-16x claim) |
| 7 | tree-sitter `Parser.parse` | 0.6% | Assumed this was the primary bottleneck |

**Tree-sitter operations total 3.1% of CPU time.** The language researcher's Hotspot 1 ("AST Parsing and Traversal, 10x-16x via Rust") targeted an operation that consumes only 3.1% of runtime. A 16x speedup on 3.1% of runtime yields 1.03x total speedup (Amdahl's law). The projected 10x-16x headline number is misleading.

### Revised Candidate Assessments

#### NEW CANDIDATE A: Fix `find_ending_with` Linear Scan (Pure Python Fix)

**Integration strategy:** Pure Python algorithmic fix. No FFI, no new dependencies.

**Root cause:** `_simple_name_lookup` index has an 80.7% miss rate (22,096 of 27,376 calls). On miss, the code falls back to `[qn for qn in self._entries.keys() if qn.endswith(f".{suffix}")]`, scanning all ~4,500 entries per call. This generates 123.7M `str.endswith()` invocations.

**Fix options:**
1. **Populate `_simple_name_lookup` more aggressively:** The index only contains entries added via `FunctionRegistryTrie.insert()` which populates `self._simple_name_lookup` via the passed-in reference. The 80.7% miss rate suggests many qualified names are inserted through code paths that bypass the simple name index population. Audit all insertion paths.
2. **Build a suffix index:** Create a `dict[str, set[QualifiedName]]` mapping the last dot-separated segment of every qualified name to its full name. This converts O(n) scans to O(1) lookups.
3. **Cache negative results:** If a suffix has been scanned and yielded no results, cache that fact to avoid re-scanning.

**Integration overhead:** Zero. This is a bugfix/optimization within existing Python code.
**Projected gain:** Eliminating 15.07s (48.3% of total) would reduce total runtime from 31.2s to ~16.1s. Even a 90% reduction (fixing most misses) saves ~13.5s.
**Net gain:** ~1.9x total speedup from a pure Python fix.
**Risk:** Very low.

#### NEW CANDIDATE B: Replace pathlib with String Operations in `should_skip_path`

**Integration strategy:** Pure Python refactor. Replace `Path.relative_to()` (3.39s across 59,012 calls) with `str.removeprefix()` or `os.path.relpath()`.

**Root cause:** `pathlib.PurePosixPath.relative_to()` creates intermediate path objects on every call. For 59,012 calls, this creates ~118,000 intermediate objects.

**Fix:** Convert paths to strings at the boundary and use `str.startswith()` / `str.removeprefix()` for prefix checks. The `should_skip_path` function only needs string comparison operations.

**Integration overhead:** Zero. Internal refactor.
**Projected gain:** 4.29s (13.7%) reduced to ~0.2s (estimated 20x faster for string ops vs pathlib). Saves ~4s.
**Net gain:** ~1.15x total speedup.
**Risk:** Very low.

#### NEW CANDIDATE C: Cache `build_local_variable_type_map` Results

**Integration strategy:** Memoize results keyed by (file_path, function_start_line, function_end_line).

**Root cause:** Called 5,228 times, re-traversing AST nodes that have already been parsed. Multiple functions in the same file trigger independent traversals.

**Integration overhead:** Memory cost of caching ~5,000 dict results. Estimated ~2MB.
**Projected gain:** 2.59s (8.3%) reduced to ~0.5s (first traversal per function cached, subsequent hits free). Saves ~2s.
**Net gain:** ~1.07x total speedup.
**Risk:** Low. Need to ensure cache is invalidated when files change (already handled by the incremental update system).

#### NEW CANDIDATE D: Suppress Debug Logging in Production

**Integration strategy:** Set loguru level to INFO or WARNING during graph building, or use lazy evaluation for debug messages.

**Root cause:** 85,099 `debug()` calls processed (1.75s) even when debug output is not displayed.

**Fix options:**
1. Wrap debug calls in `if logger.level <= DEBUG` guards.
2. Use `logger.opt(lazy=True).debug(lambda: ...)` for expensive format strings.
3. Set log level to INFO at the start of `GraphUpdater.run()`.

**Integration overhead:** Zero.
**Projected gain:** 1.84s (5.9%) reduced to ~0.1s. Saves ~1.7s.
**Net gain:** ~1.06x total speedup.
**Risk:** Very low. Debug output is not needed during normal operation.

#### NEW CANDIDATE E: Deduplicate Filesystem Traversal

**Integration strategy:** `identify_structure()` and `_collect_eligible_files()` both call `rglob("*")` + `should_skip_path()`. Merge into a single traversal pass.

**Integration overhead:** Moderate refactor of the two-pass architecture.
**Projected gain:** 1.57s (5.0%) eliminated for the duplicate pass. If combined with Candidate B (string paths), the single remaining pass also runs ~20x faster.
**Net gain:** ~1.05x total speedup.
**Risk:** Low.

### Combined Impact of Pure Python Fixes (Candidates A through E)

| Fix | Time Saved | % of Total |
|-----|-----------|------------|
| A: Fix find_ending_with | ~13.5s | 43.3% |
| B: String paths | ~4.0s | 12.8% |
| C: Cache type inference | ~2.0s | 6.4% |
| D: Suppress debug logging | ~1.7s | 5.5% |
| E: Deduplicate FS traversal | ~1.5s | 4.8% |
| **Total saved** | **~22.7s** | **72.8%** |
| **Remaining runtime** | **~8.5s** | **27.2%** |

**Combined speedup: ~3.7x from pure Python fixes alone, with zero integration overhead, zero build system changes, and zero deployment complexity.**

After these fixes, the remaining 8.5s would be:
- tree-sitter operations: ~1.0s (now 11.8% of reduced total)
- Remaining call resolution: ~2.5s
- File I/O + hashing: ~0.5s
- Graph construction: ~2.5s
- Miscellaneous: ~2.0s

### Revised Candidate 4 (Rust AST Extension) Assessment

After pure Python fixes, tree-sitter operations are 1.0s out of 8.5s (11.8%). A 16x Rust speedup on tree-sitter would save 0.94s, reducing total runtime from 8.5s to 7.6s (1.12x improvement). **This is far below the break-even threshold** given the high development cost (~110KB of Python code to port) and build system complexity.

The Rust AST extension only becomes worthwhile AFTER all pure Python fixes are applied AND the workload scales to much larger codebases (10,000+ files) where tree-sitter operations become a larger fraction of the reduced total.

### Revised Priority Order

| Priority | Candidate | Type | Net Gain (on 31.2s total) | Effort | Integration Overhead |
|----------|-----------|------|---------------------------|--------|---------------------|
| **1** | **A: Fix find_ending_with** | **Python bugfix** | **~1.9x (13.5s saved)** | **Low** | **Zero** |
| **2** | **B: String path ops** | **Python refactor** | **~1.15x (4.0s saved)** | **Low** | **Zero** |
| **3** | **C: Cache type inference** | **Python memoization** | **~1.07x (2.0s saved)** | **Low** | **Zero** |
| **4** | **D: Suppress debug logging** | **Config change** | **~1.06x (1.7s saved)** | **Trivial** | **Zero** |
| **5** | **E: Deduplicate FS traversal** | **Python refactor** | **~1.05x (1.5s saved)** | **Low** | **Zero** |
| 6 | 1: orjson | Dependency swap | Marginal on indexing | Trivial | Zero |
| 7 | 4+5+7: Rust AST extension | Rust crate | 1.12x after Python fixes | High | Significant |
| 8 | 6: File parallelism | Architecture change | 1.5x-3x after Python fixes | Moderate | Moderate |

### Conclusion

**The top 5 optimizations require zero language rewrites and zero integration overhead.** They fix algorithmic inefficiencies (linear scan), unnecessary object creation (pathlib), redundant computation (uncached type inference, duplicate traversal), and avoidable overhead (debug logging). Together they provide ~3.7x speedup.

The Rust AST extension (previously the headline recommendation) addresses only 3.1% of actual CPU time and is demoted to priority 7. It should only be reconsidered after Python-level fixes are applied and the workload scales to repositories an order of magnitude larger than the current test case.
