"""Memory allocation profiler for code-graph-rag.

Profiles the main data structures and parsing pipeline using tracemalloc.
Does NOT require external services (Memgraph, PGVector).
"""

import gc
import json
import sys
import tracemalloc
from collections import OrderedDict, defaultdict
from pathlib import Path
from textwrap import dedent

PROJECT_ROOT = Path(__file__).resolve().parent.parent

sys.path.insert(0, str(PROJECT_ROOT))


def format_bytes(size: int) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if abs(size) < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024  # type: ignore[assignment]
    return f"{size:.1f} TiB"


def snapshot_diff(label: str, snap1: tracemalloc.Snapshot, snap2: tracemalloc.Snapshot, top_n: int = 15) -> dict:
    stats = snap2.compare_to(snap1, "lineno")
    total_diff = sum(s.size_diff for s in stats if s.size_diff > 0)
    result = {
        "label": label,
        "total_new_alloc": total_diff,
        "total_new_alloc_human": format_bytes(total_diff),
        "top_allocators": [],
    }
    for stat in stats[:top_n]:
        if stat.size_diff > 0:
            result["top_allocators"].append({
                "file": str(stat.traceback),
                "size_diff": stat.size_diff,
                "size_diff_human": format_bytes(stat.size_diff),
                "count_diff": stat.count_diff,
            })
    return result


def measure_object_sizes() -> dict:
    """Measure sizes of core Python data structures used in the codebase."""
    results = {}

    # 1. FunctionRegistryTrie: dict + trie node overhead
    from codebase_rag.graph_updater import FunctionRegistryTrie

    trie = FunctionRegistryTrie()
    gc.collect()
    tracemalloc.clear_traces()
    snap_before = tracemalloc.take_snapshot()

    for i in range(10_000):
        qn = f"project.module_{i // 100}.class_{i // 10}.func_{i}"
        trie.insert(qn, "Function")

    gc.collect()
    snap_after = tracemalloc.take_snapshot()
    results["FunctionRegistryTrie_10k_insert"] = snapshot_diff(
        "FunctionRegistryTrie: insert 10k qualified names", snap_before, snap_after
    )
    results["FunctionRegistryTrie_10k_insert"]["entries_size"] = sys.getsizeof(trie._entries)
    results["FunctionRegistryTrie_10k_insert"]["entry_count"] = len(trie._entries)

    # Measure trie overhead vs flat dict
    flat_dict = {}
    gc.collect()
    tracemalloc.clear_traces()
    snap_before = tracemalloc.take_snapshot()
    for i in range(10_000):
        qn = f"project.module_{i // 100}.class_{i // 10}.func_{i}"
        flat_dict[qn] = "Function"
    gc.collect()
    snap_after = tracemalloc.take_snapshot()
    results["flat_dict_10k_baseline"] = snapshot_diff(
        "Flat dict: 10k entries baseline", snap_before, snap_after
    )

    # 2. SimpleNameLookup: defaultdict[str, set[str]]
    simple_lookup: defaultdict[str, set[str]] = defaultdict(set)
    gc.collect()
    tracemalloc.clear_traces()
    snap_before = tracemalloc.take_snapshot()
    for i in range(10_000):
        simple_name = f"func_{i % 500}"
        qn = f"project.module_{i // 100}.class_{i // 10}.{simple_name}"
        simple_lookup[simple_name].add(qn)
    gc.collect()
    snap_after = tracemalloc.take_snapshot()
    results["SimpleNameLookup_10k"] = snapshot_diff(
        "SimpleNameLookup: 10k entries, 500 unique names", snap_before, snap_after
    )

    # 3. BoundedASTCache with OrderedDict
    from codebase_rag.graph_updater import BoundedASTCache

    cache = BoundedASTCache(max_entries=5000, max_memory_mb=512)
    gc.collect()
    tracemalloc.clear_traces()
    snap_before = tracemalloc.take_snapshot()

    # Simulate storing mock entries (can't use real AST nodes without tree-sitter parsing)
    for i in range(1000):
        key = Path(f"/fake/path/module_{i}.py")
        # Use a placeholder tuple since we can't create real AST nodes without parsing
        cache.cache[key] = (None, "python")  # type: ignore
    gc.collect()
    snap_after = tracemalloc.take_snapshot()
    results["BoundedASTCache_1k_entries"] = snapshot_diff(
        "BoundedASTCache (OrderedDict): 1k entries", snap_before, snap_after
    )

    # 4. node_buffer in MemgraphIngestor pattern
    node_buffer: list[tuple[str, dict[str, str | int | float | bool | list[str] | None]]] = []
    gc.collect()
    tracemalloc.clear_traces()
    snap_before = tracemalloc.take_snapshot()
    for i in range(5000):
        node_buffer.append((
            "Function",
            {
                "qualified_name": f"project.mod_{i // 50}.cls_{i // 10}.fn_{i}",
                "name": f"fn_{i}",
                "start_line": i * 10,
                "end_line": i * 10 + 15,
                "path": f"src/mod_{i // 50}/cls_{i // 10}.py",
            },
        ))
    gc.collect()
    snap_after = tracemalloc.take_snapshot()
    results["node_buffer_5k"] = snapshot_diff(
        "node_buffer: 5k buffered nodes", snap_before, snap_after
    )

    # 5. _rel_groups in MemgraphIngestor pattern
    rel_groups: defaultdict[tuple, list[dict]] = defaultdict(list)
    gc.collect()
    tracemalloc.clear_traces()
    snap_before = tracemalloc.take_snapshot()
    for i in range(10_000):
        pattern = ("Function", "qualified_name", "CALLS", "Function", "qualified_name")
        rel_groups[pattern].append({
            "from_val": f"project.mod.fn_{i}",
            "to_val": f"project.mod.fn_{i + 1}",
            "props": {},
        })
    gc.collect()
    snap_after = tracemalloc.take_snapshot()
    results["rel_groups_10k"] = snapshot_diff(
        "rel_groups: 10k buffered relationships", snap_before, snap_after
    )

    # 6. import_mapping pattern
    import_mapping: dict[str, dict[str, str]] = {}
    gc.collect()
    tracemalloc.clear_traces()
    snap_before = tracemalloc.take_snapshot()
    for i in range(2000):
        module_qn = f"project.module_{i}"
        imports = {}
        for j in range(20):
            imports[f"import_{j}"] = f"external.package_{j}.symbol_{j}"
        import_mapping[module_qn] = imports
    gc.collect()
    snap_after = tracemalloc.take_snapshot()
    results["import_mapping_2k_modules"] = snapshot_diff(
        "import_mapping: 2k modules x 20 imports each", snap_before, snap_after
    )

    # 7. class_inheritance pattern
    class_inheritance: dict[str, list[str]] = {}
    gc.collect()
    tracemalloc.clear_traces()
    snap_before = tracemalloc.take_snapshot()
    for i in range(3000):
        class_qn = f"project.module_{i // 30}.Class_{i}"
        parents = [f"project.module_{i // 30}.BaseClass_{j}" for j in range(3)]
        class_inheritance[class_qn] = parents
    gc.collect()
    snap_after = tracemalloc.take_snapshot()
    results["class_inheritance_3k"] = snapshot_diff(
        "class_inheritance: 3k classes x 3 parents", snap_before, snap_after
    )

    return results


def measure_tree_sitter_parsing() -> dict:
    """Profile memory during tree-sitter parsing of actual Python files."""
    results = {}

    try:
        from tree_sitter import Language, Parser
        import tree_sitter_python

        py_language = Language(tree_sitter_python.language())
        parser = Parser(py_language)
    except Exception as e:
        return {"error": f"tree-sitter setup failed: {e}"}

    # Find Python files in the project itself
    py_files = sorted(PROJECT_ROOT.glob("codebase_rag/**/*.py"))
    if not py_files:
        return {"error": "No Python files found"}

    # Profile parsing all project files
    gc.collect()
    tracemalloc.clear_traces()
    snap_before = tracemalloc.take_snapshot()

    trees = []
    total_bytes_parsed = 0
    for f in py_files:
        try:
            source = f.read_bytes()
            total_bytes_parsed += len(source)
            tree = parser.parse(source)
            trees.append((f, tree))
        except Exception:
            pass

    gc.collect()
    snap_after = tracemalloc.take_snapshot()
    results["parse_all_project_files"] = snapshot_diff(
        f"Parse {len(trees)} Python files ({format_bytes(total_bytes_parsed)} source)",
        snap_before, snap_after
    )
    results["parse_all_project_files"]["file_count"] = len(trees)
    results["parse_all_project_files"]["source_bytes"] = total_bytes_parsed

    # Profile AST node retention
    gc.collect()
    tracemalloc.clear_traces()
    snap_before = tracemalloc.take_snapshot()

    root_nodes = [tree.root_node for _, tree in trees]

    gc.collect()
    snap_after = tracemalloc.take_snapshot()
    results["ast_node_retention"] = snapshot_diff(
        f"Retaining {len(root_nodes)} AST root nodes", snap_before, snap_after
    )

    # Profile what happens when we walk AST nodes (simulating function extraction)
    gc.collect()
    tracemalloc.clear_traces()
    snap_before = tracemalloc.take_snapshot()

    all_function_nodes = []
    for root in root_nodes:
        stack = [root]
        while stack:
            node = stack.pop()
            if node.type in ("function_definition", "class_definition"):
                all_function_nodes.append(node)
            stack.extend(node.children)

    gc.collect()
    snap_after = tracemalloc.take_snapshot()
    results["ast_walk_function_extraction"] = snapshot_diff(
        f"Walking ASTs, collected {len(all_function_nodes)} function/class nodes",
        snap_before, snap_after,
    )
    results["ast_walk_function_extraction"]["function_class_count"] = len(all_function_nodes)

    # Cleanup
    del trees, root_nodes, all_function_nodes

    return results


def measure_graph_loader_json() -> dict:
    """Profile GraphLoader JSON loading and indexing with synthetic data."""
    results = {}

    # Create synthetic graph JSON
    nodes = []
    relationships = []
    for i in range(5000):
        nodes.append({
            "node_id": i,
            "labels": ["Function"],
            "properties": {
                "qualified_name": f"project.module_{i // 50}.class_{i // 10}.func_{i}",
                "name": f"func_{i}",
                "start_line": i * 10,
                "end_line": i * 10 + 15,
                "path": f"src/module_{i // 50}/class_{i // 10}.py",
            },
        })
    for i in range(8000):
        relationships.append({
            "from_id": i % 5000,
            "to_id": (i + 1) % 5000,
            "type": "CALLS",
            "properties": {},
        })

    graph_data = {
        "nodes": nodes,
        "relationships": relationships,
        "metadata": {
            "total_nodes": len(nodes),
            "total_relationships": len(relationships),
            "exported_at": "2024-01-01T00:00:00Z",
        },
    }

    # Write temp file
    tmp_path = PROJECT_ROOT / "optimize" / "_tmp_graph.json"
    with open(tmp_path, "w") as f:
        json.dump(graph_data, f)

    try:
        from codebase_rag.graph_loader import GraphLoader

        gc.collect()
        tracemalloc.clear_traces()
        snap_before = tracemalloc.take_snapshot()

        loader = GraphLoader(str(tmp_path))
        loader.load()

        gc.collect()
        snap_after = tracemalloc.take_snapshot()
        results["graph_loader_5k_nodes_8k_rels"] = snapshot_diff(
            "GraphLoader: load 5k nodes + 8k relationships from JSON",
            snap_before, snap_after,
        )

        # Measure index building
        gc.collect()
        tracemalloc.clear_traces()
        snap_before = tracemalloc.take_snapshot()

        loader._build_property_index("qualified_name")

        gc.collect()
        snap_after = tracemalloc.take_snapshot()
        results["graph_loader_property_index"] = snapshot_diff(
            "GraphLoader: build property index on qualified_name",
            snap_before, snap_after,
        )

    except Exception as e:
        results["error"] = str(e)
    finally:
        tmp_path.unlink(missing_ok=True)

    return results


def measure_embedding_cache() -> dict:
    """Profile EmbeddingCache with simulated embeddings."""
    results = {}

    try:
        from codebase_rag.embedder import EmbeddingCache

        cache = EmbeddingCache()
        gc.collect()
        tracemalloc.clear_traces()
        snap_before = tracemalloc.take_snapshot()

        # Simulate 2k embeddings, each 768-dim float vector
        for i in range(2000):
            content = f"def function_{i}(x, y): return x + y + {i}"
            embedding = [float(j) / 768.0 for j in range(768)]
            cache.put(content, embedding)

        gc.collect()
        snap_after = tracemalloc.take_snapshot()
        results["embedding_cache_2k_768dim"] = snapshot_diff(
            "EmbeddingCache: 2k entries x 768-dim embeddings",
            snap_before, snap_after,
        )
        results["embedding_cache_2k_768dim"]["cache_dict_size"] = sys.getsizeof(cache._cache)
        results["embedding_cache_2k_768dim"]["entry_count"] = len(cache)

    except Exception as e:
        results["error"] = str(e)

    return results


def measure_gc_pressure() -> dict:
    """Measure GC pressure by tracking collections during workload simulation."""
    results = {}

    gc.collect()
    gc_stats_before = gc.get_stats()
    gc.disable()

    # Simulate a typical file processing workload creating many temporary objects
    temp_objects_created = 0
    for i in range(1000):
        # Simulate tree-sitter query results (lists of tuples, dicts)
        captures = {"function": [f"node_{j}" for j in range(20)]}
        for func_name in captures["function"]:
            # Simulate qualified name construction (many string concatenations)
            parts = ["project", f"module_{i}", f"class_{i // 10}", func_name]
            qn = ".".join(parts)
            # Simulate property dict construction
            props = {
                "qualified_name": qn,
                "name": func_name,
                "start_line": i * 10,
                "end_line": i * 10 + 15,
            }
            temp_objects_created += 1
            del props

    gc.enable()
    gc.collect()
    gc_stats_after = gc.get_stats()

    results["gc_pressure_simulation"] = {
        "label": "GC pressure during simulated file processing (1k files x 20 funcs)",
        "temp_objects_created": temp_objects_created,
        "gc_gen0_before": gc_stats_before[0],
        "gc_gen0_after": gc_stats_after[0],
        "gc_gen1_before": gc_stats_before[1],
        "gc_gen1_after": gc_stats_after[1],
        "gc_gen2_before": gc_stats_before[2],
        "gc_gen2_after": gc_stats_after[2],
    }

    return results


def measure_string_duplication() -> dict:
    """Estimate memory wasted on duplicated strings in typical data structures."""
    results = {}

    gc.collect()
    tracemalloc.clear_traces()
    snap_before = tracemalloc.take_snapshot()

    # Simulate how property dicts repeat the same key strings thousands of times
    all_dicts: list[dict] = []
    for i in range(5000):
        d = {
            "qualified_name": f"project.mod_{i // 50}.cls_{i // 10}.fn_{i}",
            "name": f"fn_{i}",
            "start_line": i * 10,
            "end_line": i * 10 + 15,
            "path": f"src/mod_{i // 50}/cls_{i // 10}.py",
        }
        all_dicts.append(d)

    gc.collect()
    snap_after = tracemalloc.take_snapshot()
    results["property_dict_duplication_5k"] = snapshot_diff(
        "5k property dicts with repeated key strings", snap_before, snap_after
    )

    # Compare: same data using tuples (no key duplication)
    gc.collect()
    tracemalloc.clear_traces()
    snap_before = tracemalloc.take_snapshot()

    all_tuples: list[tuple] = []
    for i in range(5000):
        t = (
            f"project.mod_{i // 50}.cls_{i // 10}.fn_{i}",
            f"fn_{i}",
            i * 10,
            i * 10 + 15,
            f"src/mod_{i // 50}/cls_{i // 10}.py",
        )
        all_tuples.append(t)

    gc.collect()
    snap_after = tracemalloc.take_snapshot()
    results["property_tuple_alternative_5k"] = snapshot_diff(
        "5k tuples (no key duplication) as alternative", snap_before, snap_after
    )

    return results


def measure_peak_usage_full_pipeline() -> dict:
    """Simulate the full pipeline memory envelope.

    This exercises the complete data structure lifecycle:
    1. Build FunctionRegistryTrie
    2. Build import mappings
    3. Build class inheritance
    4. Buffer nodes and relationships
    5. Measure peak
    """
    results = {}

    gc.collect()
    tracemalloc.clear_traces()
    snap_baseline = tracemalloc.take_snapshot()

    # Phase 1: Build FunctionRegistryTrie
    from codebase_rag.graph_updater import FunctionRegistryTrie

    simple_name_lookup: defaultdict[str, set[str]] = defaultdict(set)
    trie = FunctionRegistryTrie(simple_name_lookup=simple_name_lookup)

    for i in range(15_000):
        simple_name = f"func_{i % 1000}"
        qn = f"project.module_{i // 150}.class_{i // 15}.{simple_name}"
        trie.insert(qn, "Function")
        simple_name_lookup[simple_name].add(qn)

    gc.collect()
    snap_phase1 = tracemalloc.take_snapshot()
    results["phase1_trie_15k"] = snapshot_diff(
        "Phase 1: FunctionRegistryTrie + SimpleNameLookup (15k entries)",
        snap_baseline, snap_phase1,
    )

    # Phase 2: Import mappings
    import_mapping: dict[str, dict[str, str]] = {}
    for i in range(1500):
        module_qn = f"project.module_{i}"
        imports = {f"sym_{j}": f"ext.pkg_{j}.sym_{j}" for j in range(25)}
        import_mapping[module_qn] = imports

    gc.collect()
    snap_phase2 = tracemalloc.take_snapshot()
    results["phase2_imports_1500_modules"] = snapshot_diff(
        "Phase 2: import_mapping (1500 modules x 25 imports)",
        snap_phase1, snap_phase2,
    )

    # Phase 3: Class inheritance
    class_inheritance: dict[str, list[str]] = {}
    for i in range(5000):
        class_qn = f"project.module_{i // 50}.Class_{i}"
        parents = [f"project.module_{i // 50}.Base_{j}" for j in range(2)]
        class_inheritance[class_qn] = parents

    gc.collect()
    snap_phase3 = tracemalloc.take_snapshot()
    results["phase3_inheritance_5k"] = snapshot_diff(
        "Phase 3: class_inheritance (5k classes x 2 parents)",
        snap_phase2, snap_phase3,
    )

    # Phase 4: Node + relationship buffers
    node_buffer: list[tuple[str, dict]] = []
    for i in range(10_000):
        node_buffer.append((
            "Function",
            {
                "qualified_name": f"project.mod_{i // 100}.cls_{i // 10}.fn_{i}",
                "name": f"fn_{i}",
                "start_line": i * 5,
                "end_line": i * 5 + 10,
            },
        ))

    rel_groups: defaultdict[tuple, list[dict]] = defaultdict(list)
    for i in range(20_000):
        pattern = ("Function", "qualified_name", "CALLS", "Function", "qualified_name")
        rel_groups[pattern].append({
            "from_val": f"project.mod.fn_{i}",
            "to_val": f"project.mod.fn_{i + 1}",
            "props": {},
        })

    gc.collect()
    snap_phase4 = tracemalloc.take_snapshot()
    results["phase4_buffers_10k_nodes_20k_rels"] = snapshot_diff(
        "Phase 4: node_buffer (10k) + rel_groups (20k)",
        snap_phase3, snap_phase4,
    )

    # Total from baseline
    results["total_pipeline_memory"] = snapshot_diff(
        "TOTAL: Full pipeline memory (all phases combined)",
        snap_baseline, snap_phase4,
    )

    # Peak usage
    current, peak = tracemalloc.get_traced_memory()
    results["peak_traced_memory"] = {
        "current": current,
        "current_human": format_bytes(current),
        "peak": peak,
        "peak_human": format_bytes(peak),
    }

    return results


def main() -> None:
    tracemalloc.start(25)  # 25 frames for stack traces

    all_results: dict[str, dict] = {}

    print("=" * 70)
    print("MEMORY ALLOCATION PROFILING REPORT")
    print("=" * 70)

    print("\n[1/7] Measuring core data structure sizes...")
    all_results["data_structures"] = measure_object_sizes()

    print("[2/7] Profiling tree-sitter parsing...")
    all_results["tree_sitter"] = measure_tree_sitter_parsing()

    print("[3/7] Profiling GraphLoader JSON loading...")
    all_results["graph_loader"] = measure_graph_loader_json()

    print("[4/7] Profiling EmbeddingCache...")
    all_results["embedding_cache"] = measure_embedding_cache()

    print("[5/7] Measuring GC pressure...")
    all_results["gc_pressure"] = measure_gc_pressure()

    print("[6/7] Measuring string duplication overhead...")
    all_results["string_duplication"] = measure_string_duplication()

    print("[7/7] Measuring peak usage in full pipeline simulation...")
    all_results["full_pipeline"] = measure_peak_usage_full_pipeline()

    tracemalloc.stop()

    # Print summary report
    print("\n" + "=" * 70)
    print("RESULTS SUMMARY")
    print("=" * 70)

    for section_name, section_data in all_results.items():
        print(f"\n--- {section_name.upper()} ---")
        for key, value in section_data.items():
            if isinstance(value, dict) and "label" in value:
                total = value.get("total_new_alloc_human", value.get("peak_human", "N/A"))
                print(f"  {value['label']}")
                print(f"    Total new allocation: {total}")
                if "top_allocators" in value:
                    for i, alloc in enumerate(value["top_allocators"][:5]):
                        print(f"    [{i+1}] {alloc['size_diff_human']} ({alloc['count_diff']} objects) - {alloc['file'][:80]}")
            elif isinstance(value, dict) and "current_human" in value:
                print(f"  Current traced: {value['current_human']}")
                print(f"  Peak traced: {value['peak_human']}")
            elif isinstance(value, dict) and "temp_objects_created" in value:
                print(f"  {value['label']}")
                print(f"    Temp objects created: {value['temp_objects_created']}")
                for gen in range(3):
                    before = value[f"gc_gen{gen}_before"]
                    after = value[f"gc_gen{gen}_after"]
                    print(f"    Gen{gen}: collections {before['collections']} -> {after['collections']}, collected {before['collected']} -> {after['collected']}")

    # Save detailed JSON
    output_path = PROJECT_ROOT / "optimize" / "memory_profile_results.json"
    with open(output_path, "w") as f:
        json.dump(all_results, f, indent=2, default=str)
    print(f"\nDetailed results saved to: {output_path}")


if __name__ == "__main__":
    main()
