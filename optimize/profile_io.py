import hashlib
import json
import statistics
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from codebase_rag import constants as cs
from codebase_rag.graph_updater import _hash_file, _load_hash_cache, _save_hash_cache
from codebase_rag.parser_loader import load_parsers
from codebase_rag.parsers.utils import safe_decode_with_fallback
from codebase_rag.services.protobuf_service import ProtobufFileIngestor
from codebase_rag.utils.path_utils import should_skip_path


REPO_PATH = Path(__file__).resolve().parent.parent
RUNS = 5


def benchmark(func, *args, runs=RUNS, label=""):
    times = []
    result = None
    for _ in range(runs):
        start = time.perf_counter()
        result = func(*args)
        elapsed = time.perf_counter() - start
        times.append(elapsed)
    avg = statistics.mean(times)
    std = statistics.stdev(times) if len(times) > 1 else 0.0
    med = statistics.median(times)
    return {
        "label": label,
        "avg_ms": avg * 1000,
        "median_ms": med * 1000,
        "std_ms": std * 1000,
        "min_ms": min(times) * 1000,
        "max_ms": max(times) * 1000,
        "runs": runs,
        "result": result,
    }


def collect_py_files():
    files = []
    for f in REPO_PATH.rglob("*.py"):
        if not should_skip_path(f, REPO_PATH):
            files.append(f)
    return files


def profile_file_hashing(files):
    print("\n=== FILE HASHING (SHA-256) ===")
    results = []
    total_bytes = 0
    for f in files:
        total_bytes += f.stat().st_size

    def hash_all():
        for f in files:
            _hash_file(f)

    r = benchmark(hash_all, label=f"hash {len(files)} files ({total_bytes/1024:.0f} KB)")
    results.append(r)
    print(f"  {r['label']}: avg={r['avg_ms']:.2f}ms, median={r['median_ms']:.2f}ms, std={r['std_ms']:.2f}ms")

    per_file_ms = r['avg_ms'] / len(files) if files else 0
    print(f"  Per file average: {per_file_ms:.3f}ms")
    print(f"  Throughput: {total_bytes / (r['avg_ms']/1000) / 1024 / 1024:.1f} MB/s")

    single_sizes = [(f, f.stat().st_size) for f in files]
    single_sizes.sort(key=lambda x: x[1], reverse=True)
    for f, sz in single_sizes[:5]:
        r2 = benchmark(_hash_file, f, runs=10, label=f"hash {f.relative_to(REPO_PATH)} ({sz}B)")
        results.append(r2)
        print(f"  {r2['label']}: avg={r2['avg_ms']:.3f}ms")

    return results


def profile_file_reading(files):
    print("\n=== FILE READING (read_bytes + parse) ===")
    results = []

    def read_all_bytes():
        for f in files:
            f.read_bytes()

    total_bytes = sum(f.stat().st_size for f in files)
    r = benchmark(read_all_bytes, label=f"read_bytes {len(files)} files ({total_bytes/1024:.0f} KB)")
    results.append(r)
    print(f"  {r['label']}: avg={r['avg_ms']:.2f}ms, median={r['median_ms']:.2f}ms")
    print(f"  Throughput: {total_bytes / (r['avg_ms']/1000) / 1024 / 1024:.1f} MB/s")

    def read_all_text():
        for f in files:
            f.read_text(encoding="utf-8")

    r2 = benchmark(read_all_text, label=f"read_text {len(files)} files")
    results.append(r2)
    print(f"  {r2['label']}: avg={r2['avg_ms']:.2f}ms, median={r2['median_ms']:.2f}ms")

    return results


def profile_tree_sitter_parsing(files):
    print("\n=== TREE-SITTER PARSING ===")
    results = []
    parsers, queries = load_parsers()
    py_parser = parsers.get(cs.SupportedLanguage.PYTHON)
    if not py_parser:
        print("  Python parser not available, skipping")
        return results

    py_files = [f for f in files if f.suffix == ".py"]
    file_bytes = [(f, f.read_bytes()) for f in py_files]

    def parse_all():
        for f, src in file_bytes:
            py_parser.parse(src)

    r = benchmark(parse_all, label=f"parse {len(py_files)} Python files")
    results.append(r)
    print(f"  {r['label']}: avg={r['avg_ms']:.2f}ms, median={r['median_ms']:.2f}ms")
    per_file_ms = r['avg_ms'] / len(py_files) if py_files else 0
    print(f"  Per file average: {per_file_ms:.3f}ms")

    file_bytes_sorted = sorted(file_bytes, key=lambda x: len(x[1]), reverse=True)
    for f, src in file_bytes_sorted[:5]:
        r2 = benchmark(py_parser.parse, src, runs=10,
                        label=f"parse {f.relative_to(REPO_PATH)} ({len(src)}B)")
        results.append(r2)
        print(f"  {r2['label']}: avg={r2['avg_ms']:.3f}ms")

    return results


def profile_json_serialization():
    print("\n=== JSON SERIALIZATION ===")
    results = []

    small = {"key": "value", "num": 42, "arr": [1, 2, 3]}
    r = benchmark(json.dumps, small, runs=1000, label="json.dumps small dict")
    results.append(r)
    print(f"  {r['label']}: avg={r['avg_ms']:.4f}ms")

    medium_nodes = [
        {"node_id": i, "labels": ["Function"], "properties": {"name": f"func_{i}", "path": f"src/mod_{i//10}.py", "start_line": i*10, "end_line": i*10+5}}
        for i in range(1000)
    ]
    medium_rels = [
        {"from_id": i, "to_id": (i+1) % 1000, "type": "CALLS", "properties": {}}
        for i in range(2000)
    ]
    medium = {"nodes": medium_nodes, "relationships": medium_rels, "metadata": {"total_nodes": 1000, "total_relationships": 2000}}

    r2 = benchmark(json.dumps, medium, runs=5, label=f"json.dumps graph (1K nodes, 2K rels, {len(json.dumps(medium))/1024:.0f}KB)")
    results.append(r2)
    print(f"  {r2['label']}: avg={r2['avg_ms']:.2f}ms")

    json_str = json.dumps(medium)
    r3 = benchmark(json.loads, json_str, runs=5, label=f"json.loads graph ({len(json_str)/1024:.0f}KB)")
    results.append(r3)
    print(f"  {r3['label']}: avg={r3['avg_ms']:.2f}ms")

    large_nodes = medium_nodes * 10
    large_rels = medium_rels * 10
    large = {"nodes": large_nodes, "relationships": large_rels, "metadata": {"total_nodes": 10000, "total_relationships": 20000}}
    large_json = json.dumps(large)
    r4 = benchmark(json.dumps, large, runs=3, label=f"json.dumps large graph (10K nodes, 20K rels, {len(large_json)/1024:.0f}KB)")
    results.append(r4)
    print(f"  {r4['label']}: avg={r4['avg_ms']:.2f}ms")

    r5 = benchmark(json.loads, large_json, runs=3, label=f"json.loads large graph ({len(large_json)/1024:.0f}KB)")
    results.append(r5)
    print(f"  {r5['label']}: avg={r5['avg_ms']:.2f}ms")

    with_indent = lambda d: json.dumps(d, indent=2, ensure_ascii=False)
    r6 = benchmark(with_indent, large, runs=3, label=f"json.dumps large graph (indent=2)")
    results.append(r6)
    print(f"  {r6['label']}: avg={r6['avg_ms']:.2f}ms")

    return results


def profile_protobuf_serialization():
    print("\n=== PROTOBUF SERIALIZATION ===")
    results = []
    try:
        import codec.schema_pb2 as pb
    except ImportError:
        print("  protobuf schema not available, skipping")
        return results

    import tempfile, shutil
    tmp_dir = Path(tempfile.mkdtemp())
    try:
        ingestor = ProtobufFileIngestor(output_path=str(tmp_dir))

        for i in range(100):
            ingestor.ensure_node_batch("Function", {
                "qualified_name": f"project.mod.func_{i}",
                "name": f"func_{i}",
                "path": f"src/mod.py",
                "start_line": i * 10,
                "end_line": i * 10 + 5,
            })
        for i in range(200):
            ingestor.ensure_relationship_batch(
                ("Function", "qualified_name", f"project.mod.func_{i % 100}"),
                "CALLS",
                ("Function", "qualified_name", f"project.mod.func_{(i+1) % 100}"),
            )

        def flush_protobuf():
            ingestor.flush_all()

        r = benchmark(flush_protobuf, runs=5, label="protobuf flush (100 nodes, 200 rels)")
        results.append(r)
        print(f"  {r['label']}: avg={r['avg_ms']:.2f}ms")

        index_file = tmp_dir / "graph_code_index.pb"
        if index_file.exists():
            size = index_file.stat().st_size
            print(f"  Output size: {size} bytes")

            def read_protobuf():
                idx = pb.GraphCodeIndex()
                idx.ParseFromString(index_file.read_bytes())
                return idx

            r2 = benchmark(read_protobuf, runs=10, label=f"protobuf parse ({size}B)")
            results.append(r2)
            print(f"  {r2['label']}: avg={r2['avg_ms']:.3f}ms")

        for node_path in tmp_dir.iterdir():
            if node_path.suffix == ".pb":
                sz = node_path.stat().st_size
                print(f"  Protobuf file: {node_path.name} ({sz} bytes)")

    finally:
        shutil.rmtree(tmp_dir)

    return results


def profile_hash_cache_io():
    print("\n=== HASH CACHE I/O ===")
    results = []

    import tempfile
    tmp = Path(tempfile.mkdtemp())
    try:
        cache_data = {f"path/to/file_{i}.py": hashlib.sha256(f"content_{i}".encode()).hexdigest() for i in range(1000)}
        cache_path = tmp / ".file_hashes.json"

        r = benchmark(_save_hash_cache, cache_path, cache_data, runs=5, label=f"save hash cache ({len(cache_data)} entries)")
        results.append(r)
        print(f"  {r['label']}: avg={r['avg_ms']:.2f}ms, size={cache_path.stat().st_size/1024:.1f}KB")

        r2 = benchmark(_load_hash_cache, cache_path, runs=5, label=f"load hash cache ({len(cache_data)} entries)")
        results.append(r2)
        print(f"  {r2['label']}: avg={r2['avg_ms']:.2f}ms")
    finally:
        import shutil
        shutil.rmtree(tmp)

    return results


def profile_file_traversal():
    print("\n=== FILESYSTEM TRAVERSAL ===")
    results = []

    def rglob_all():
        return list(REPO_PATH.rglob("*"))

    r = benchmark(rglob_all, runs=5, label="rglob('*') entire repo")
    results.append(r)
    all_paths = r['result']
    print(f"  {r['label']}: avg={r['avg_ms']:.2f}ms, found {len(all_paths)} paths")

    def rglob_with_filter():
        eligible = []
        for f in REPO_PATH.rglob("*"):
            if f.is_file() and not should_skip_path(f, REPO_PATH):
                eligible.append(f)
        return eligible

    r2 = benchmark(rglob_with_filter, runs=5, label="rglob + should_skip_path filter")
    results.append(r2)
    eligible = r2['result']
    print(f"  {r2['label']}: avg={r2['avg_ms']:.2f}ms, eligible {len(eligible)} files")

    overhead_ms = r2['avg_ms'] - r['avg_ms']
    print(f"  Filter overhead: {overhead_ms:.2f}ms")

    return results


def profile_source_extraction():
    print("\n=== SOURCE EXTRACTION ===")
    results = []
    from codebase_rag.utils.source_extraction import extract_source_lines

    py_files = [f for f in REPO_PATH.rglob("*.py")
                if not should_skip_path(f, REPO_PATH) and f.stat().st_size > 100]
    if not py_files:
        print("  No Python files found")
        return results

    target = py_files[0]
    line_count = len(target.read_text().splitlines())

    def extract_50_lines():
        return extract_source_lines(target, 1, min(50, line_count))

    r = benchmark(extract_50_lines, runs=20, label=f"extract 50 lines from {target.relative_to(REPO_PATH)}")
    results.append(r)
    print(f"  {r['label']}: avg={r['avg_ms']:.3f}ms")

    def extract_all_files_10_lines():
        for f in py_files[:50]:
            extract_source_lines(f, 1, 10)

    r2 = benchmark(extract_all_files_10_lines, runs=5, label=f"extract 10 lines from {min(50, len(py_files))} files")
    results.append(r2)
    print(f"  {r2['label']}: avg={r2['avg_ms']:.2f}ms")

    return results


def profile_embedding_cache_io():
    print("\n=== EMBEDDING CACHE I/O ===")
    results = []
    import tempfile

    from codebase_rag.embedder import EmbeddingCache

    tmp = Path(tempfile.mkdtemp())
    try:
        cache = EmbeddingCache(path=tmp / "embedding_cache.json")
        for i in range(500):
            cache.put(f"def func_{i}(): pass", [float(j) / 768 for j in range(768)])

        def save_cache():
            cache.save()

        r = benchmark(save_cache, runs=5, label=f"save embedding cache ({len(cache)} entries, 768-dim)")
        results.append(r)
        size = (tmp / "embedding_cache.json").stat().st_size
        print(f"  {r['label']}: avg={r['avg_ms']:.2f}ms, size={size/1024/1024:.2f}MB")

        def load_cache():
            new_cache = EmbeddingCache(path=tmp / "embedding_cache.json")
            new_cache.load()
            return new_cache

        r2 = benchmark(load_cache, runs=5, label=f"load embedding cache ({size/1024/1024:.2f}MB)")
        results.append(r2)
        print(f"  {r2['label']}: avg={r2['avg_ms']:.2f}ms")
        print(f"  Throughput: {size / (r2['avg_ms']/1000) / 1024 / 1024:.1f} MB/s")
    finally:
        import shutil
        shutil.rmtree(tmp)

    return results


def profile_directory_structure():
    print("\n=== DIRECTORY STRUCTURE IDENTIFICATION ===")
    results = []
    from codebase_rag.language_spec import LANGUAGE_SPECS

    package_indicators = set()
    for spec in LANGUAGE_SPECS.values():
        package_indicators.update(spec.package_indicators)

    def identify_packages():
        dirs = set()
        for p in REPO_PATH.rglob("*"):
            if p.is_dir() and not should_skip_path(p, REPO_PATH):
                dirs.add(p)
        packages = 0
        for d in dirs:
            for indicator in package_indicators:
                if (d / indicator).exists():
                    packages += 1
                    break
        return packages

    r = benchmark(identify_packages, runs=5, label="identify package structure")
    results.append(r)
    print(f"  {r['label']}: avg={r['avg_ms']:.2f}ms, packages={r['result']}")

    return results


def main():
    print("=" * 70)
    print("I/O AND SERIALIZATION LATENCY PROFILE")
    print(f"Repo: {REPO_PATH}")
    print("=" * 70)

    all_results = []
    files = collect_py_files()
    print(f"\nPython files for profiling: {len(files)}")

    all_results.extend(profile_file_traversal())
    all_results.extend(profile_file_reading(files))
    all_results.extend(profile_file_hashing(files))
    all_results.extend(profile_tree_sitter_parsing(files))
    all_results.extend(profile_source_extraction())
    all_results.extend(profile_json_serialization())
    all_results.extend(profile_protobuf_serialization())
    all_results.extend(profile_hash_cache_io())
    all_results.extend(profile_embedding_cache_io())
    all_results.extend(profile_directory_structure())

    print("\n" + "=" * 70)
    print("RANKED SUMMARY (by avg wall-clock time)")
    print("=" * 70)
    ranked = sorted(all_results, key=lambda x: x['avg_ms'], reverse=True)
    for i, r in enumerate(ranked, 1):
        print(f"  {i:2d}. [{r['avg_ms']:10.2f}ms] {r['label']}")


if __name__ == "__main__":
    main()
