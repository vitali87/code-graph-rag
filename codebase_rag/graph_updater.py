import hashlib
import json
import os
import sys
from collections import OrderedDict, defaultdict
from collections.abc import Callable, ItemsView, KeysView
from pathlib import Path

from loguru import logger
from rich.progress import Progress, SpinnerColumn, TextColumn
from tree_sitter import Node, Parser, QueryCursor

from . import constants as cs
from . import logs as ls
from .config import settings
from .language_spec import LANGUAGE_FQN_SPECS, get_language_spec
from .parser_loader import COMBINED_FUNC_CLASS_IMPORT_QUERIES
from .parsers.cpp_frontend import (
    cpp_frontend_available,
    find_compile_commands,
    run_cpp_frontend,
)
from .parsers.factory import ProcessorFactory
from .parsers.utils import sorted_captures
from .services import IngestorProtocol, QueryProtocol
from .types_defs import (
    EmbeddingQueryResult,
    FunctionRegistry,
    LanguageQueries,
    NodeType,
    QualifiedName,
    ResultRow,
    SimpleNameLookup,
    TrieNode,
)
from .utils.dependencies import has_semantic_dependencies
from .utils.fqn_resolver import find_function_source_by_fqn
from .utils.path_utils import (
    cached_relative_path,
    matches_ignore_patterns,
    should_skip_path,
    should_skip_rel_file,
    unignore_could_match_within,
)
from .utils.source_extraction import extract_source_with_fallback

type FileHashCache = dict[str, str]
type DirMtimesCache = dict[str, float]


class FunctionRegistryTrie:
    __slots__ = (
        "root",
        "_entries",
        "_simple_name_lookup",
        "_ending_with_cache",
        "_duplicates",
        "_properties",
        "_property_names",
        "_abstracts",
        "_callable_params",
    )

    def __init__(self, simple_name_lookup: SimpleNameLookup | None = None) -> None:
        self.root: TrieNode = {}
        self._entries: FunctionRegistry = {}
        self._simple_name_lookup = simple_name_lookup
        self._ending_with_cache: dict[str, list[QualifiedName]] = {}
        self._duplicates: dict[QualifiedName, list[QualifiedName]] = {}
        self._properties: set[QualifiedName] = set()
        self._property_names: set[str] = set()
        self._abstracts: set[QualifiedName] = set()
        self._callable_params: dict[QualifiedName, dict[str, int]] = {}

    def mark_callable_params(
        self, qualified_name: QualifiedName, params: dict[str, int]
    ) -> None:
        if params:
            self._callable_params[qualified_name] = params

    def callable_params(self, qualified_name: QualifiedName) -> dict[str, int] | None:
        return self._callable_params.get(qualified_name)

    def mark_property(self, qualified_name: QualifiedName) -> None:
        self._properties.add(qualified_name)
        self._property_names.add(qualified_name.rsplit(cs.SEPARATOR_DOT, 1)[-1])

    def is_property(self, qualified_name: QualifiedName) -> bool:
        return qualified_name in self._properties

    def property_names(self) -> set[str]:
        return self._property_names

    def mark_abstract(self, qualified_name: QualifiedName) -> None:
        self._abstracts.add(qualified_name)

    def is_abstract(self, qualified_name: QualifiedName) -> bool:
        return qualified_name in self._abstracts

    def register_unique_qn(
        self, natural_qn: QualifiedName, start_line: int
    ) -> QualifiedName:
        if natural_qn not in self._entries:
            return natural_qn
        variant = f"{natural_qn}{cs.DUP_QN_MARKER}{start_line}"
        bucket = self._duplicates.setdefault(natural_qn, [natural_qn])
        if variant not in bucket:
            bucket.append(variant)
        return variant

    def variants(self, qualified_name: QualifiedName) -> list[QualifiedName]:
        return self._duplicates.get(qualified_name, [qualified_name])

    def insert(self, qualified_name: QualifiedName, func_type: NodeType) -> None:
        qualified_name = sys.intern(qualified_name)
        self._entries[qualified_name] = func_type

        simple_name = qualified_name.rsplit(cs.SEPARATOR_DOT, 1)[-1]
        if self._simple_name_lookup is not None:
            self._simple_name_lookup[simple_name].add(qualified_name)
        self._invalidate_ending_with_cache(qualified_name, simple_name)

        parts = qualified_name.split(cs.SEPARATOR_DOT)
        current: TrieNode = self.root

        for part in parts:
            if part not in current:
                current[part] = {}
            child = current[part]
            assert isinstance(child, dict)
            current = child

        current[cs.TRIE_TYPE_KEY] = func_type
        current[cs.TRIE_QN_KEY] = qualified_name

    def get(
        self, qualified_name: QualifiedName, default: NodeType | None = None
    ) -> NodeType | None:
        return self._entries.get(qualified_name, default)

    def __contains__(self, qualified_name: QualifiedName) -> bool:
        return qualified_name in self._entries

    def __getitem__(self, qualified_name: QualifiedName) -> NodeType:
        return self._entries[qualified_name]

    def __setitem__(self, qualified_name: QualifiedName, func_type: NodeType) -> None:
        self.insert(qualified_name, func_type)

    def __delitem__(self, qualified_name: QualifiedName) -> None:
        if qualified_name not in self._entries:
            return

        del self._entries[qualified_name]
        self._duplicates.pop(qualified_name, None)
        for natural, bucket in list(self._duplicates.items()):
            if qualified_name in bucket:
                bucket.remove(qualified_name)
                if len(bucket) <= 1:
                    self._duplicates.pop(natural, None)
        simple_name = qualified_name.rsplit(cs.SEPARATOR_DOT, 1)[-1]

        if qualified_name in self._properties:
            self._properties.discard(qualified_name)
            if not any(
                p.rsplit(cs.SEPARATOR_DOT, 1)[-1] == simple_name
                for p in self._properties
            ):
                self._property_names.discard(simple_name)
        self._abstracts.discard(qualified_name)
        self._callable_params.pop(qualified_name, None)

        self._invalidate_ending_with_cache(qualified_name, simple_name)

        if self._simple_name_lookup is not None:
            if simple_name in self._simple_name_lookup:
                self._simple_name_lookup[simple_name].discard(qualified_name)

        parts = qualified_name.split(cs.SEPARATOR_DOT)
        self._cleanup_trie_path(parts, self.root)

    def _cleanup_trie_path(self, parts: list[str], node: TrieNode) -> bool:
        if not parts:
            node.pop(cs.TRIE_QN_KEY, None)
            node.pop(cs.TRIE_TYPE_KEY, None)
            return not node

        part = parts[0]
        if part not in node:
            return False

        child = node[part]
        assert isinstance(child, dict)
        if self._cleanup_trie_path(parts[1:], child):
            del node[part]

        is_endpoint = cs.TRIE_QN_KEY in node
        has_children = any(not key.startswith(cs.TRIE_INTERNAL_PREFIX) for key in node)
        return not has_children and not is_endpoint

    def _navigate_to_prefix(self, prefix: str) -> TrieNode | None:
        parts = prefix.split(cs.SEPARATOR_DOT) if prefix else []
        current: TrieNode = self.root
        for part in parts:
            if part not in current:
                return None
            child = current[part]
            assert isinstance(child, dict)
            current = child
        return current

    def _collect_from_subtree(
        self,
        node: TrieNode,
        filter_fn: Callable[[QualifiedName], bool] | None = None,
    ) -> list[tuple[QualifiedName, NodeType]]:
        results: list[tuple[QualifiedName, NodeType]] = []

        def dfs(n: TrieNode) -> None:
            if cs.TRIE_QN_KEY in n:
                qn = n[cs.TRIE_QN_KEY]
                func_type = n[cs.TRIE_TYPE_KEY]
                assert isinstance(qn, str) and isinstance(func_type, NodeType)
                if filter_fn is None or filter_fn(qn):
                    results.append((qn, func_type))

            for key, child in n.items():
                if not key.startswith(cs.TRIE_INTERNAL_PREFIX):
                    assert isinstance(child, dict)
                    dfs(child)

        dfs(node)
        return results

    def keys(self) -> KeysView[QualifiedName]:
        return self._entries.keys()

    def items(self) -> ItemsView[QualifiedName, NodeType]:
        return self._entries.items()

    def __len__(self) -> int:
        return len(self._entries)

    def find_with_prefix_and_suffix(
        self, prefix: str, suffix: str
    ) -> list[QualifiedName]:
        node = self._navigate_to_prefix(prefix)
        if node is None:
            return []
        suffix_pattern = f".{suffix}"
        matches = self._collect_from_subtree(
            node, lambda qn: qn.endswith(suffix_pattern)
        )
        return [qn for qn, _ in matches]

    def _invalidate_ending_with_cache(
        self, qualified_name: QualifiedName, simple_name: str
    ) -> None:
        if not self._ending_with_cache:
            return
        self._ending_with_cache.pop(simple_name, None)
        # (H) dotted suffixes are cached too (#513); drop any the qn ends with.
        for key in [
            k
            for k in self._ending_with_cache
            if cs.SEPARATOR_DOT in k and qualified_name.endswith(f".{k}")
        ]:
            del self._ending_with_cache[key]

    def find_ending_with(self, suffix: str) -> list[QualifiedName]:
        cached = self._ending_with_cache.get(suffix)
        if cached is not None:
            return cached
        if self._simple_name_lookup is not None:
            if suffix in self._simple_name_lookup:
                result = sorted(self._simple_name_lookup[suffix])
            elif cs.SEPARATOR_DOT in suffix:
                # (H) #513: the index only holds last segments, so a dotted
                # (H) suffix ("Class.method") always misses it; fall back to
                # (H) the linear scan instead of dropping the match.
                result = sorted(
                    qn for qn in self._entries.keys() if qn.endswith(f".{suffix}")
                )
            else:
                # (H) dot-free miss is authoritative: insert() indexes every
                # (H) entry's last segment, so nothing can end with ".suffix".
                result = []
        else:
            result = sorted(
                qn for qn in self._entries.keys() if qn.endswith(f".{suffix}")
            )
        self._ending_with_cache[suffix] = result
        return result

    def find_with_prefix(self, prefix: str) -> list[tuple[QualifiedName, NodeType]]:
        node = self._navigate_to_prefix(prefix)
        return [] if node is None else self._collect_from_subtree(node)


class BoundedASTCache:
    __slots__ = ("cache", "max_entries", "max_memory_bytes")

    def __init__(
        self,
        max_entries: int | None = None,
        max_memory_mb: int | None = None,
    ):
        self.cache: OrderedDict[Path, tuple[Node, cs.SupportedLanguage]] = OrderedDict()
        self.max_entries = (
            max_entries if max_entries is not None else settings.CACHE_MAX_ENTRIES
        )
        max_mem = (
            max_memory_mb if max_memory_mb is not None else settings.CACHE_MAX_MEMORY_MB
        )
        self.max_memory_bytes = max_mem * cs.BYTES_PER_MB

    def __setitem__(self, key: Path, value: tuple[Node, cs.SupportedLanguage]) -> None:
        if key in self.cache:
            del self.cache[key]

        self.cache[key] = value

        self._enforce_limits()

    def __getitem__(self, key: Path) -> tuple[Node, cs.SupportedLanguage]:
        value = self.cache[key]
        self.cache.move_to_end(key)
        return value

    def __delitem__(self, key: Path) -> None:
        if key in self.cache:
            del self.cache[key]

    def __contains__(self, key: Path) -> bool:
        return key in self.cache

    def items(self) -> ItemsView[Path, tuple[Node, cs.SupportedLanguage]]:
        return self.cache.items()

    def _enforce_limits(self) -> None:
        while len(self.cache) > self.max_entries:
            self.cache.popitem(last=False)  # (H) Remove least recently used

        if self._should_evict_for_memory():
            entries_to_remove = max(
                1, len(self.cache) // settings.CACHE_EVICTION_DIVISOR
            )
            for _ in range(entries_to_remove):
                if self.cache:
                    self.cache.popitem(last=False)

    def _should_evict_for_memory(self) -> bool:
        try:
            cache_size = sum(sys.getsizeof(v) for v in self.cache.values())
            return cache_size > self.max_memory_bytes
        except Exception:
            return (
                len(self.cache)
                > self.max_entries * settings.CACHE_MEMORY_THRESHOLD_RATIO
            )


def _hash_file(filepath: Path) -> str:
    data = filepath.read_bytes()
    return hashlib.md5(data, usedforsecurity=False).hexdigest()


def _hash_file_with_bytes(filepath: Path) -> tuple[str, bytes] | None:
    try:
        with open(filepath, "rb") as f:
            data = f.read()
    except OSError as e:
        logger.warning(ls.FILE_UNREADABLE, path=filepath, error=e)
        return None
    return hashlib.md5(data, usedforsecurity=False).hexdigest(), data


def _load_hash_cache(cache_path: Path) -> FileHashCache:
    if not cache_path.is_file():
        return {}
    try:
        with cache_path.open(encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            logger.info(ls.HASH_CACHE_LOADED, count=len(data), path=cache_path)
            return data
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(ls.HASH_CACHE_LOAD_FAILED, path=cache_path, error=e)
    return {}


def _save_hash_cache(cache_path: Path, hashes: FileHashCache) -> None:
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        with cache_path.open("w", encoding="utf-8") as f:
            json.dump(hashes, f, indent=2)
        logger.info(ls.HASH_CACHE_SAVED, count=len(hashes), path=cache_path)
    except OSError as e:
        logger.warning(ls.HASH_CACHE_SAVE_FAILED, path=cache_path, error=e)


def _load_dir_mtimes(cache_path: Path) -> DirMtimesCache:
    if not cache_path.is_file():
        return {}
    try:
        with cache_path.open(encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return {k: float(v) for k, v in data.items() if isinstance(v, int | float)}
    except (json.JSONDecodeError, OSError, ValueError):
        pass
    return {}


def _save_dir_mtimes(cache_path: Path, mtimes: DirMtimesCache) -> None:
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        with cache_path.open("w", encoding="utf-8") as f:
            json.dump(mtimes, f)
    except OSError:
        pass


def _touch_empty_json(cache_path: Path) -> None:
    if cache_path.exists():
        return
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        with cache_path.open("w", encoding="utf-8") as f:
            f.write(cs.JSON_EMPTY_OBJECT)
    except OSError:
        pass


class GraphUpdater:
    def __init__(
        self,
        ingestor: IngestorProtocol,
        repo_path: Path,
        parsers: dict[cs.SupportedLanguage, Parser],
        queries: dict[cs.SupportedLanguage, LanguageQueries],
        unignore_paths: frozenset[str] | None = None,
        exclude_paths: frozenset[str] | None = None,
        project_name: str | None = None,
    ):
        self.ingestor = ingestor
        self._single_file: Path | None = None
        if repo_path.is_file():
            resolved = repo_path.resolve()
            self._single_file = resolved
            repo_path = resolved.parent
        self.repo_path = repo_path
        self.parsers = parsers
        self.queries = queries
        self.project_name = (
            project_name and project_name.strip()
        ) or repo_path.resolve().name
        self.simple_name_lookup: SimpleNameLookup = defaultdict(set)
        self.function_registry = FunctionRegistryTrie(
            simple_name_lookup=self.simple_name_lookup
        )
        self.ast_cache = BoundedASTCache()
        # (H) Every file parsed this run, in parse order. The AST cache is bounded
        # (H) and evicts on large repos, so Pass 3 must iterate this full list (not
        # (H) the cache) and re-parse evicted files, or their calls are dropped.
        self._parsed_files: list[tuple[Path, cs.SupportedLanguage]] = []
        self.unignore_paths = unignore_paths
        self.exclude_paths = exclude_paths
        self.skipped_because_in_sync = False
        self._collected_dir_mtimes: DirMtimesCache = {}
        self._cpp_frontend_covered: frozenset[str] = frozenset()

        self.factory = ProcessorFactory(
            ingestor=self.ingestor,
            repo_path=self.repo_path,
            project_name=self.project_name,
            queries=self.queries,
            function_registry=self.function_registry,
            simple_name_lookup=self.simple_name_lookup,
            ast_cache=self.ast_cache,
            unignore_paths=self.unignore_paths,
            exclude_paths=self.exclude_paths,
        )

    def _run_cpp_frontend(self) -> None:
        # (H) Optional libclang C++ pre-pass: when CPP_FRONTEND=libclang and a
        # (H) compile_commands.json is discoverable, emit macro-accurate C/C++
        # (H) nodes/edges directly (tree-sitter cannot expand macros). Covered
        # (H) files are then skipped by the tree-sitter definition pass. Missing
        # (H) either condition falls back to tree-sitter with no change.
        self._cpp_frontend_covered = frozenset()
        if settings.CPP_FRONTEND != cs.CppFrontend.LIBCLANG:
            return
        if not cpp_frontend_available():
            logger.warning(ls.CPP_FRONTEND_UNAVAILABLE)
            return
        compdb_dir = find_compile_commands(self.repo_path)
        if compdb_dir is None:
            logger.warning(ls.CPP_FRONTEND_NO_COMPDB)
            return
        logger.info(ls.CPP_FRONTEND_RUNNING.format(path=compdb_dir))
        self._cpp_frontend_covered = run_cpp_frontend(
            self.ingestor,
            self.repo_path,
            self.project_name,
            compdb_dir,
            function_registry=self.function_registry,
            simple_name_lookup=self.simple_name_lookup,
            structural_elements=self.factory.structure_processor.structural_elements,
        )
        logger.info(
            ls.CPP_FRONTEND_COVERED.format(count=len(self._cpp_frontend_covered))
        )

    def _is_dependency_file(self, file_name: str, filepath: Path) -> bool:
        return (
            file_name.lower() in cs.DEPENDENCY_FILES
            or filepath.suffix.lower() == cs.CSPROJ_SUFFIX
        )

    def run(self, force: bool = False) -> None:
        py_engine = self.factory.type_inference._python_type_inference
        if py_engine is not None:
            py_engine._available_classes_cache.clear()
            py_engine._return_stmt_cache.clear()
            py_engine._method_return_type_cache.clear()
            py_engine._self_assignment_cache.clear()
        # (H) Reset per-run parse tracking so a reused updater does not reprocess
        # (H) a previous run's files in Pass 3.
        self._parsed_files.clear()
        self.ingestor.ensure_node_batch(
            cs.NODE_PROJECT, {cs.KEY_NAME: self.project_name}
        )
        logger.info(ls.ENSURING_PROJECT, name=self.project_name)

        if not force and self._is_already_in_sync():
            logger.info(ls.GRAPH_ALREADY_IN_SYNC)
            self.skipped_because_in_sync = True
            self.ingestor.flush_all()
            return

        logger.info(ls.PASS_1_STRUCTURE)
        self.factory.structure_processor.identify_structure()

        self._run_cpp_frontend()

        logger.info(ls.PASS_2_FILES)
        self._process_files(force=force)

        corrected = self.factory.definition_processor.resolve_deferred_cpp_methods()
        if corrected:
            logger.info("Resolved {} deferred C++ out-of-class methods", corrected)

        contained = self.factory.definition_processor.resolve_deferred_cpp_containment()
        if contained:
            logger.info("Resolved {} deferred C++ nested containments", contained)

        go_methods = self.factory.definition_processor.resolve_deferred_go_methods()
        if go_methods:
            logger.info("Resolved {} Go receiver methods", go_methods)

        if not force:
            self._rehydrate_registry_from_graph()

        # (H) After rehydration so the "does a real definition exist?" check sees
        # (H) definitions in files an incremental run did not re-parse; otherwise a
        # (H) forward declaration whose definition lives in an unchanged file would be
        # (H) kept as a phantom and re-fragment the class.
        kept_forwards = (
            self.factory.definition_processor.resolve_deferred_forward_declarations()
        )
        if kept_forwards:
            logger.info(
                "Registered {} forward-declared C/C++ types with no definition",
                kept_forwards,
            )

        # (H) After forward declarations so a base whose only representation is
        # (H) a kept forward declaration still resolves to a real node.
        inherits = self.factory.definition_processor.resolve_deferred_cpp_inherits()
        if inherits:
            logger.info("Resolved {} deferred C++ inheritance bases", inherits)

        # (H) Last containment step: every node-registering pass above (deferred
        # (H) C++ methods, Go receivers, kept forward declarations) must finish
        # (H) before parent qns are verified against the registry.
        linked = self.factory.definition_processor.resolve_deferred_parent_links()
        if linked:
            logger.info("Resolved {} deferred containment parents", linked)

        logger.info(ls.FOUND_FUNCTIONS, count=len(self.function_registry))
        logger.info(ls.PASS_3_CALLS)
        self._process_function_calls()

        self.factory.definition_processor.process_all_method_overrides()

        logger.info(ls.ANALYSIS_COMPLETE)
        self.ingestor.flush_all()

        self._prune_orphan_nodes()

        self._generate_semantic_embeddings()

    def _rehydrate_registry_from_graph(self) -> None:
        # (H) Incremental runs populate the function registry only from re-parsed
        # (H) files. Read every definition's qualified name back from the graph and
        # (H) re-register the ones missing locally, so calls and instantiations
        # (H) into files that were not re-parsed still resolve and their edges are
        # (H) re-emitted. Without this, editing one file drops cross-file CALLS /
        # (H) INSTANTIATES into any unchanged file (issue #532, outbound half).
        if not isinstance(self.ingestor, QueryProtocol):
            return
        added = 0
        for row in self.ingestor.fetch_all(cs.CYPHER_ALL_DEFINITION_QNS):
            qn = row.get(cs.KEY_QUALIFIED_NAME)
            label = row.get(cs.KEY_LABEL)
            if not isinstance(qn, str) or not isinstance(label, str):
                continue
            if qn in self.function_registry:
                continue
            try:
                node_type = NodeType(label)
            except ValueError:
                continue
            self.function_registry[qn] = node_type
            # (H) Restore the property-name set for unchanged files: property-dispatch
            # (H) resolution (`obj.prop`) consults it, so a re-parsed file's call to a
            # (H) @property defined elsewhere would otherwise drop vs a clean index.
            if row.get(cs.KEY_IS_PROPERTY):
                self.function_registry.mark_property(qn)
            added += 1
        if added:
            logger.info(ls.REGISTRY_REHYDRATED, count=added)
        self._rehydrate_class_inheritance_from_graph()

    def _rehydrate_class_inheritance_from_graph(self) -> None:
        # (H) Incremental runs rebuild class_inheritance only from re-parsed files.
        # (H) Restore the child->bases map for classes defined in files that were
        # (H) not re-parsed, so protocol dispatch and inherited-method resolution
        # (H) work in Pass 3 (issue #532 residual). Only fill entries missing
        # (H) locally: a re-parsed class already has its fresh, correctly ordered
        # (H) bases, so we must not overwrite or duplicate them. CYPHER_ALL_INHERITS
        # (H) is ordered by base_index, so a rehydrated class's bases keep their
        # (H) original source order (multiple inheritance resolves the same base a
        # (H) clean index would).
        if not isinstance(self.ingestor, QueryProtocol):
            return
        class_inheritance = self.factory.definition_processor.class_inheritance
        rows = self.ingestor.fetch_all(cs.CYPHER_ALL_INHERITS)
        for child, bases in self._rehydrated_bases_by_child(
            rows, class_inheritance
        ).items():
            class_inheritance[child] = bases

    @staticmethod
    def _rehydrated_bases_by_child(
        rows: list[ResultRow], existing: dict[str, list[str]]
    ) -> dict[str, list[str]]:
        # (H) Group persisted INHERITS rows into child -> ordered bases, restoring
        # (H) the original source order from base_index. Skip children already
        # (H) present locally (freshly re-parsed). A class with more than one base
        # (H) needs a reliable order (method resolution / override attribution are
        # (H) first-match-wins over the base list); if any of its edges lacks a
        # (H) base_index -- e.g. an INHERITS relationship written by an older index
        # (H) before base_index existed -- the order cannot be trusted, so that
        # (H) class is NOT rehydrated and falls back to name-based resolution rather
        # (H) than risk binding to the wrong base. Single-base classes are
        # (H) order-independent and always safe.
        collected: dict[str, list[tuple[int | None, str]]] = {}
        for row in rows:
            child = row.get(cs.KEY_CHILD_QN)
            base = row.get(cs.KEY_BASE_QN)
            if not isinstance(child, str) or not isinstance(base, str):
                continue
            if child in existing:
                continue
            raw_index = row.get(cs.KEY_BASE_INDEX)
            index = raw_index if isinstance(raw_index, int) else None
            collected.setdefault(child, []).append((index, base))
        result: dict[str, list[str]] = {}
        for child, pairs in collected.items():
            if len(pairs) > 1 and any(index is None for index, _ in pairs):
                continue
            pairs.sort(key=lambda pair: (pair[0] is None, pair[0] or 0))
            result[child] = [base for _index, base in pairs]
        return result

    def _capture_inbound_edges(self, reindexed_keys: list[str]) -> list[ResultRow]:
        # (H) Record the reference edges that unchanged files point at the
        # (H) re-indexed files, BEFORE those files' subtrees (and thus the inbound
        # (H) edges) are deleted. Capturing and restoring the exact edges avoids
        # (H) re-resolving the callers, whose resolution would diverge from a clean
        # (H) index (cgr resolution is context-sensitive).
        if not reindexed_keys or not isinstance(self.ingestor, QueryProtocol):
            return []
        return self.ingestor.fetch_all(
            cs.CYPHER_INBOUND_EDGES, {cs.CYPHER_PARAM_PATHS: reindexed_keys}
        )

    def _restore_inbound_edges(self, captured: list[ResultRow]) -> None:
        # (H) Re-emit each captured inbound edge whose target still exists after the
        # (H) re-index. A target that was renamed or removed is correctly left
        # (H) without its stale inbound edge, matching a clean re-index.
        if not captured:
            return
        module_label = cs.NodeLabel.MODULE.value
        restored = 0
        for row in captured:
            caller_label = row.get(cs.KEY_CALLER_LABEL)
            caller_qn = row.get(cs.KEY_CALLER_QN)
            rel = row.get(cs.KEY_REL)
            target_label = row.get(cs.KEY_TARGET_LABEL)
            target_qn = row.get(cs.KEY_TARGET_QN)
            if not (
                isinstance(caller_label, str)
                and isinstance(caller_qn, str)
                and isinstance(rel, str)
                and isinstance(target_label, str)
                and isinstance(target_qn, str)
            ):
                continue
            if target_label != module_label and target_qn not in self.function_registry:
                continue
            caller_key = cs.NODE_UNIQUE_CONSTRAINTS.get(caller_label)
            target_key = cs.NODE_UNIQUE_CONSTRAINTS.get(target_label)
            if caller_key is None or target_key is None:
                continue
            self.ingestor.ensure_relationship_batch(
                (caller_label, caller_key, caller_qn),
                rel,
                (target_label, target_key, target_qn),
            )
            restored += 1
        if restored:
            logger.info(ls.INCREMENTAL_REBUILD_INBOUND, count=restored)

    def remove_file_from_state(self, file_path: Path) -> None:
        logger.debug(ls.REMOVING_STATE, path=file_path)

        if file_path in self.ast_cache:
            del self.ast_cache[file_path]
            logger.debug(ls.REMOVED_FROM_CACHE)

        relative_path = cached_relative_path(file_path, self.repo_path)
        path_parts = (
            relative_path.parent.parts
            if file_path.name == cs.INIT_PY
            else relative_path.with_suffix("").parts
        )
        module_qn_prefix = cs.SEPARATOR_DOT.join([self.project_name, *path_parts])

        qns_to_remove = set()

        for qn in list(self.function_registry.keys()):
            if qn.startswith(f"{module_qn_prefix}.") or qn == module_qn_prefix:
                qns_to_remove.add(qn)
                del self.function_registry[qn]

        if qns_to_remove:
            logger.debug(ls.REMOVING_QNS, count=len(qns_to_remove))

        for simple_name, qn_set in self.simple_name_lookup.items():
            original_count = len(qn_set)
            new_qn_set = qn_set - qns_to_remove
            if len(new_qn_set) < original_count:
                self.simple_name_lookup[simple_name] = new_qn_set
                logger.debug(ls.CLEANED_SIMPLE_NAME, name=simple_name)

    def _delete_module_entities(self, file_key: str) -> None:
        """Remove a changed/deleted file's Module subtree from the graph.

        The incremental path re-parses a changed file and re-adds its
        entities, but the entities the previous parse contributed (the
        Module and everything it DEFINES, plus their IMPORTS/CALLS edges via
        DETACH) must be removed first; otherwise renamed-away Function/Class/
        Method nodes and their edges linger alongside the new ones.
        """
        if isinstance(self.ingestor, QueryProtocol):
            self.ingestor.execute_write(
                cs.CYPHER_DELETE_MODULE, {cs.KEY_PATH: file_key}
            )

    def _diff_dir_against_cache(
        self,
        dir_path_str: str,
        dir_key: str,
        old_hashes: FileHashCache,
        old_dir_mtimes: DirMtimesCache,
    ) -> tuple[str | None, str | None]:
        prefix = "" if dir_key == cs.ROOT_DIR_KEY else f"{dir_key}/"
        expected_files: set[str] = set()
        expected_dirs: set[str] = set()
        for fk in old_hashes:
            if fk.startswith(prefix):
                rest = fk[len(prefix) :]
                if "/" not in rest:
                    expected_files.add(rest)
        for dk in old_dir_mtimes:
            if dk == cs.ROOT_DIR_KEY or not dk.startswith(prefix):
                continue
            rest = dk[len(prefix) :]
            if "/" not in rest:
                expected_dirs.add(rest)

        actual_files: set[str] = set()
        actual_dirs: set[str] = set()
        try:
            with os.scandir(dir_path_str) as it:
                for entry in it:
                    name = entry.name
                    if name in (cs.HASH_CACHE_FILENAME, cs.DIR_MTIMES_FILENAME):
                        continue
                    try:
                        is_symlink = entry.is_symlink()
                    except OSError:
                        is_symlink = False
                    try:
                        is_dir_following = entry.is_dir()
                    except OSError:
                        is_dir_following = False
                    if is_symlink and is_dir_following:
                        continue
                    if is_dir_following:
                        actual_dirs.add(name)
                    else:
                        actual_files.add(name)
        except OSError:
            return None, dir_key

        dir_parts: tuple[str, ...] = (
            () if dir_key == cs.ROOT_DIR_KEY else tuple(dir_key.split("/"))
        )
        dir_prefix_for_keep = "" if dir_key == cs.ROOT_DIR_KEY else f"{dir_key}/"

        for name in actual_dirs - expected_dirs:
            if not self._should_keep_dir(name, dir_prefix_for_keep):
                continue
            return f"{prefix}{name}", None
        for name in actual_files - expected_files:
            dot = name.rfind(".")
            suffix = name[dot:] if dot != -1 else ""
            if should_skip_rel_file(
                f"{prefix}{name}",
                dir_parts,
                suffix,
                exclude_paths=self.exclude_paths,
                unignore_paths=self.unignore_paths,
            ):
                continue
            return f"{prefix}{name}", None

        for name in expected_files - actual_files:
            return None, f"{prefix}{name}"
        for name in expected_dirs - actual_dirs:
            return None, f"{prefix}{name}"

        return None, None

    def _should_keep_dir(self, dirname: str, dir_prefix: str) -> bool:
        rel_dir = f"{dir_prefix}{dirname}"
        # (H) an explicit exclude can never be rescued by unignore (excludes win
        # (H) at the file level too), so prune the subtree outright.
        if self.exclude_paths and matches_ignore_patterns(
            f"{rel_dir}/", self.exclude_paths
        ):
            return False
        if dirname not in cs.IGNORE_PATTERNS:
            return True
        return bool(
            self.unignore_paths
            and any(
                unignore_could_match_within(u, rel_dir) for u in self.unignore_paths
            )
        )

    def _is_already_in_sync(self) -> bool:
        if self._single_file is not None:
            return False
        cache_path = self.repo_path / cs.HASH_CACHE_FILENAME
        if not cache_path.is_file():
            return False
        cache_mtime = cache_path.stat().st_mtime
        dir_mtimes_path = self.repo_path / cs.DIR_MTIMES_FILENAME
        old_hashes = _load_hash_cache(cache_path)
        old_dir_mtimes = _load_dir_mtimes(dir_mtimes_path)
        if not old_hashes or not old_dir_mtimes:
            return False

        repo_str = str(self.repo_path)
        for dir_key, cached_mtime in old_dir_mtimes.items():
            dir_path_str = (
                repo_str if dir_key == cs.ROOT_DIR_KEY else f"{repo_str}/{dir_key}"
            )
            try:
                current_mtime = os.stat(dir_path_str).st_mtime
            except OSError:
                return False
            if current_mtime != cached_mtime:
                addition, removal = self._diff_dir_against_cache(
                    dir_path_str, dir_key, old_hashes, old_dir_mtimes
                )
                if addition is not None or removal is not None:
                    return False

        for file_key, old_hash in old_hashes.items():
            file_path_str = f"{repo_str}/{file_key}"
            try:
                stat = os.stat(file_path_str)
            except OSError:
                return False
            if stat.st_mtime <= cache_mtime:
                continue
            if _hash_file(Path(file_path_str)) != old_hash:
                return False
        return True

    def _collect_eligible_files(self) -> list[tuple[Path, str]]:
        if self._single_file is not None:
            if not should_skip_path(
                self._single_file,
                self.repo_path,
                exclude_paths=self.exclude_paths,
                unignore_paths=self.unignore_paths,
            ):
                file_key = cached_relative_path(
                    self._single_file, self.repo_path
                ).as_posix()
                return [(self._single_file, file_key)]
            return []

        eligible: list[tuple[Path, str]] = []
        hash_name = cs.HASH_CACHE_FILENAME
        dir_mtimes_name = cs.DIR_MTIMES_FILENAME
        repo_str = str(self.repo_path)
        repo_prefix_len = len(repo_str) + 1
        exclude_paths = self.exclude_paths
        unignore_paths = self.unignore_paths
        self._collected_dir_mtimes = {}
        for dirpath, dirnames, filenames in os.walk(repo_str):
            if len(dirpath) < repo_prefix_len:
                rel_dir = ""
                dir_parts: tuple[str, ...] = ()
                dir_key = cs.ROOT_DIR_KEY
            else:
                rel_dir = dirpath[repo_prefix_len:].replace(os.sep, "/")
                dir_parts = tuple(rel_dir.split("/")) if rel_dir else ()
                dir_key = rel_dir or cs.ROOT_DIR_KEY
            dir_prefix = f"{rel_dir}/" if rel_dir else ""
            try:
                self._collected_dir_mtimes[dir_key] = os.stat(dirpath).st_mtime
            except OSError:
                pass
            dirnames[:] = sorted(
                d for d in dirnames if self._should_keep_dir(d, dir_prefix)
            )
            for fname in sorted(filenames):
                if fname in (hash_name, dir_mtimes_name):
                    continue
                dot = fname.rfind(".")
                suffix = fname[dot:] if dot != -1 else ""
                rel_path_str = f"{dir_prefix}{fname}"
                if not should_skip_rel_file(
                    rel_path_str,
                    dir_parts,
                    suffix,
                    exclude_paths=exclude_paths,
                    unignore_paths=unignore_paths,
                ):
                    eligible.append((Path(f"{dirpath}/{fname}"), rel_path_str))
        return eligible

    def _process_files(self, force: bool = False) -> None:
        cache_path = self.repo_path / cs.HASH_CACHE_FILENAME
        dir_mtimes_path = self.repo_path / cs.DIR_MTIMES_FILENAME
        old_hashes = _load_hash_cache(cache_path) if not force else {}
        cache_mtime = cache_path.stat().st_mtime if cache_path.is_file() else 0.0
        if force:
            logger.info(ls.INCREMENTAL_FORCE)

        _touch_empty_json(cache_path)
        _touch_empty_json(dir_mtimes_path)

        eligible_files = self._collect_eligible_files()
        new_hashes: FileHashCache = {}
        skipped_count = 0
        changed_count = 0
        unreadable_count = 0

        current_file_keys: set[str] = set()

        processed_since_flush = 0

        changed_entries: list[tuple[Path, str, bool, bytes]] = []
        for filepath, file_key in eligible_files:
            if not force and file_key in old_hashes:
                try:
                    file_mtime = filepath.stat().st_mtime
                except OSError:
                    unreadable_count += 1
                    continue
                if file_mtime <= cache_mtime:
                    new_hashes[file_key] = old_hashes[file_key]
                    current_file_keys.add(file_key)
                    skipped_count += 1
                    continue

            hashed = _hash_file_with_bytes(filepath)
            if hashed is None:
                unreadable_count += 1
                continue
            current_hash, file_bytes = hashed

            current_file_keys.add(file_key)
            new_hashes[file_key] = current_hash

            if (
                not force
                and file_key in old_hashes
                and old_hashes[file_key] == current_hash
            ):
                logger.debug(ls.FILE_HASH_UNCHANGED, path=file_key)
                skipped_count += 1
                continue

            is_new = file_key not in old_hashes
            if not is_new:
                logger.debug(ls.FILE_HASH_CHANGED, path=file_key)
            else:
                logger.debug(ls.FILE_HASH_NEW, path=file_key)
            changed_entries.append((filepath, file_key, is_new, file_bytes))

        # (H) Before deleting any changed file's subtree (which removes the inbound
        # (H) CALLS/IMPORTS/INSTANTIATES edges incident on it), capture those edges
        # (H) so they can be restored verbatim afterwards (issue #532, inbound
        # (H) half). New files have no prior inbound edges, so only re-indexed
        # (H) (changed, non-new) files matter.
        reindexed_keys = sorted(
            file_key for _fp, file_key, is_new, _b in changed_entries if not is_new
        )
        captured_inbound = self._capture_inbound_edges(reindexed_keys)

        pre_parsed = self._pre_parse_changed_files(changed_entries)

        with Progress(
            SpinnerColumn(),
            TextColumn(ls.PROGRESS_INDEXING_LABEL),
            TextColumn("[progress.description]{task.description}"),
            transient=True,
            disable=not sys.stderr.isatty(),
        ) as progress:
            task = progress.add_task("", total=len(eligible_files))
            if skipped_count or unreadable_count:
                progress.advance(task, skipped_count + unreadable_count)

            for filepath, file_key, is_new, file_bytes in changed_entries:
                if not is_new:
                    self.remove_file_from_state(filepath)
                    self._delete_module_entities(file_key)

                changed_count += 1
                self._process_single_file(
                    filepath,
                    file_bytes=file_bytes,
                    pre_parsed=pre_parsed.get(filepath),
                )

                processed_since_flush += 1
                if processed_since_flush >= settings.FILE_FLUSH_INTERVAL:
                    logger.info(ls.PERIODIC_FLUSH.format(count=processed_since_flush))
                    self.ingestor.flush_all()
                    processed_since_flush = 0

                progress.update(
                    task,
                    advance=1,
                    description=ls.PROGRESS_FILES_PROCESSED.format(count=changed_count),
                )

        deleted_keys = set(old_hashes.keys()) - current_file_keys
        if deleted_keys:
            logger.info(ls.INCREMENTAL_DELETED, count=len(deleted_keys))
            for deleted_key in deleted_keys:
                deleted_path = self.repo_path / deleted_key
                self.remove_file_from_state(deleted_path)
                self._delete_module_entities(deleted_key)
                if isinstance(self.ingestor, QueryProtocol):
                    self.ingestor.execute_write(
                        cs.CYPHER_DELETE_FILE, {cs.KEY_PATH: deleted_key}
                    )

        self._restore_inbound_edges(captured_inbound)

        if skipped_count > 0:
            logger.info(ls.INCREMENTAL_SKIPPED, count=skipped_count)
        if changed_count > 0:
            logger.info(ls.INCREMENTAL_CHANGED, count=changed_count)
        if unreadable_count > 0:
            logger.info(ls.INCREMENTAL_UNREADABLE, count=unreadable_count)

        _save_hash_cache(cache_path, new_hashes)
        _save_dir_mtimes(dir_mtimes_path, self._collected_dir_mtimes)

    def _pre_parse_changed_files(
        self,
        changed_entries: list[tuple[Path, str, bool, bytes]],
    ) -> dict[Path, tuple[Node, dict[str, list] | None]]:
        result: dict[Path, tuple[Node, dict[str, list] | None]] = {}
        for filepath, _file_key, _is_new, file_bytes in changed_entries:
            lang_config = get_language_spec(filepath.suffix)
            if not (
                lang_config
                and isinstance(lang_config.language, cs.SupportedLanguage)
                and lang_config.language in self.parsers
            ):
                continue
            language = lang_config.language
            parser = self.queries[language].get(cs.KEY_PARSER)
            if not parser:
                continue
            tree = parser.parse(file_bytes)
            root_node = tree.root_node
            combined_query = COMBINED_FUNC_CLASS_IMPORT_QUERIES.get(language)
            combined_captures: dict[str, list] | None = None
            if combined_query:
                cursor = QueryCursor(combined_query)
                combined_captures = sorted_captures(cursor, root_node)
            result[filepath] = (root_node, combined_captures)
        return result

    def _process_single_file(
        self,
        filepath: Path,
        file_bytes: bytes | None = None,
        pre_parsed: tuple[Node, dict[str, list] | None] | None = None,
    ) -> None:
        if self._cpp_frontend_covered:
            rel = cached_relative_path(filepath, self.repo_path).as_posix()
            if rel in self._cpp_frontend_covered:
                # (H) The libclang frontend already emitted this file's
                # (H) definitions; keep only the generic File node.
                self.factory.structure_processor.process_generic_file(
                    filepath, filepath.name
                )
                return

        lang_config = get_language_spec(filepath.suffix)
        if (
            lang_config
            and isinstance(lang_config.language, cs.SupportedLanguage)
            and lang_config.language in self.parsers
        ):
            result = self.factory.definition_processor.process_file(
                filepath,
                lang_config.language,
                self.queries,
                self.factory.structure_processor.structural_elements,
                source_bytes=file_bytes,
                pre_parsed=pre_parsed,
            )
            if result:
                root_node, language = result
                self.ast_cache[filepath] = (root_node, language)
                self._parsed_files.append((filepath, language))
        elif self._is_dependency_file(filepath.name, filepath):
            self.factory.definition_processor.process_dependencies(filepath)

        self.factory.structure_processor.process_generic_file(filepath, filepath.name)

    def _ast_for(self, file_path: Path, language: cs.SupportedLanguage) -> Node | None:
        # (H) Return the file's AST from the bounded cache, or re-parse from disk
        # (H) when it was evicted. Evicted files carry stale captures (nodes from
        # (H) the discarded tree), so drop them: downstream recomputes captures
        # (H) from this fresh tree. Re-caching keeps the cache bounded across the
        # (H) two Pass-3 loops.
        if file_path in self.ast_cache:
            return self.ast_cache[file_path][0]
        parser = self.queries[language].get(cs.KEY_PARSER)
        if parser is None:
            return None
        try:
            file_bytes = file_path.read_bytes()
        except OSError as e:
            logger.error(ls.CALL_PROCESSING_FAILED, path=file_path, error=e)
            return None
        root_node = parser.parse(file_bytes).root_node
        self.ast_cache[file_path] = (root_node, language)
        self.factory._func_class_captures_cache.pop(file_path, None)
        return root_node

    def _process_function_calls(self) -> None:
        captures_cache = self.factory._func_class_captures_cache
        # (H) Iterate every file parsed this run, not the bounded AST cache: on a
        # (H) large repo the cache evicts most files, and iterating it drops their
        # (H) calls (a whole module ends up with zero CALLS edges).
        for file_path, language in self._parsed_files:
            root_node = self._ast_for(file_path, language)
            if root_node is None:
                continue
            self.factory.call_processor.collect_callable_field_bindings(
                file_path,
                root_node,
                language,
                self.queries,
                func_class_captures_cache=captures_cache,
            )
        # (H) Bindings are pending until every file's ctor metadata (param order,
        # (H) param->attribute renames) is in: a construction site may be scanned
        # (H) before the file defining its class.
        self.factory.call_processor.finalize_callable_field_bindings()
        for file_path, language in self._parsed_files:
            root_node = self._ast_for(file_path, language)
            if root_node is None:
                continue
            if captures_cache is not None and file_path in captures_cache:
                cached = captures_cache[file_path]
                if not cached.get(cs.CAPTURE_CALL) and not cached.get(
                    cs.CAPTURE_FUNCTION
                ):
                    continue
            self.factory.call_processor.process_calls_in_file(
                file_path,
                root_node,
                language,
                self.queries,
                func_class_captures_cache=captures_cache,
            )
        self.factory.call_processor.finalize_callable_param_flow()

    def _prune_orphan_nodes(self) -> None:
        """Remove graph nodes whose files/folders no longer exist on disk."""
        if not isinstance(self.ingestor, QueryProtocol):
            return

        logger.info(ls.PRUNE_START)
        total_pruned = 0

        project_prefix = self.project_name + "."
        repo_abs = self.repo_path.resolve().as_posix()
        prune_specs: list[tuple[str, str, str]] = [
            (cs.CYPHER_ALL_FILE_PATHS, cs.CYPHER_DELETE_FILE, "File"),
            (
                cs.CYPHER_ALL_MODULE_PATHS_INTERNAL,
                cs.CYPHER_DELETE_MODULE,
                "Module",
            ),
            (cs.CYPHER_ALL_FOLDER_PATHS, cs.CYPHER_DELETE_FOLDER, "Folder"),
        ]

        for query_all, delete_query, label in prune_specs:
            rows = self.ingestor.fetch_all(query_all)
            orphans = []
            for r in rows:
                path = r.get("path")
                if not isinstance(path, str) or not path:
                    continue
                if path.startswith(cs.INLINE_MODULE_PATH_PREFIX):
                    continue
                abs_path = r.get("absolute_path")
                qn = r.get("qualified_name", "")
                if isinstance(abs_path, str) and not abs_path.startswith(repo_abs):
                    continue
                if isinstance(qn, str) and qn and not qn.startswith(project_prefix):
                    continue
                if not (self.repo_path / path).exists():
                    orphans.append(path)

            if orphans:
                logger.info(ls.PRUNE_FOUND, count=len(orphans), label=label)
                for orphan_path in orphans:
                    logger.debug(ls.PRUNE_DELETING, label=label, path=orphan_path)
                    self.ingestor.execute_write(
                        delete_query, {cs.KEY_PATH: orphan_path}
                    )
                total_pruned += len(orphans)

        # (H) Drop external import-target modules that no module imports anymore,
        # (H) e.g. an imported name renamed/removed on an incremental rebuild.
        self.ingestor.execute_write(cs.CYPHER_DELETE_ORPHAN_EXTERNAL_MODULES)

        if total_pruned:
            logger.info(ls.PRUNE_COMPLETE, count=total_pruned)
        else:
            logger.info(ls.PRUNE_SKIP)

    def _generate_semantic_embeddings(self) -> None:
        if not has_semantic_dependencies():
            logger.info(ls.SEMANTIC_NOT_AVAILABLE)
            return

        if not isinstance(self.ingestor, QueryProtocol):
            logger.info(ls.INGESTOR_NO_QUERY)
            return

        try:
            from .embedder import embed_code_batch, get_embedding_cache
            from .vector_store import (
                close_qdrant_client,
                store_embedding_batch,
                verify_stored_ids,
            )

            logger.info(ls.PASS_4_EMBEDDINGS)

            results = self.ingestor.fetch_all(
                cs.CYPHER_QUERY_EMBEDDINGS, {"project_name": self.project_name}
            )

            if not results:
                logger.info(ls.NO_FUNCTIONS_FOR_EMBEDDING)
                return

            logger.info(ls.GENERATING_EMBEDDINGS, count=len(results))

            embedded_count = 0
            expected_ids: set[int] = set()
            pending: list[tuple[int, str, str]] = []
            flush_at = settings.QDRANT_BATCH_SIZE

            def flush() -> int:
                nonlocal pending
                if not pending:
                    return 0
                snippets = [item[2] for item in pending]
                try:
                    embeddings = embed_code_batch(snippets)
                except Exception as e:
                    logger.warning(
                        ls.EMBEDDING_BATCH_COMPUTE_FAILED,
                        count=len(pending),
                        error=e,
                    )
                    pending = []
                    return 0
                points: list[tuple[int, list[float], str]] = [
                    (node_id, emb, qname)
                    for (node_id, qname, _), emb in zip(pending, embeddings)
                ]
                for node_id, _qname, _src in pending:
                    expected_ids.add(node_id)
                stored = store_embedding_batch(points)
                pending = []
                return stored

            for row in results:
                parsed = self._parse_embedding_result(row)
                if parsed is None:
                    continue

                node_id = parsed[cs.KEY_NODE_ID]
                qualified_name = parsed[cs.KEY_QUALIFIED_NAME]
                start_line = parsed.get(cs.KEY_START_LINE)
                end_line = parsed.get(cs.KEY_END_LINE)
                file_path = parsed.get(cs.KEY_PATH)

                if start_line is None or end_line is None or file_path is None:
                    logger.debug(ls.NO_SOURCE_FOR, name=qualified_name)
                    continue

                if source_code := self._extract_source_code(
                    qualified_name, file_path, start_line, end_line
                ):
                    pending.append((node_id, qualified_name, source_code))
                    if len(pending) >= flush_at:
                        embedded_count += flush()
                        if (
                            embedded_count % settings.EMBEDDING_PROGRESS_INTERVAL == 0
                            and embedded_count > 0
                        ):
                            logger.debug(
                                ls.EMBEDDING_PROGRESS,
                                done=embedded_count,
                                total=len(results),
                            )
                else:
                    logger.debug(ls.NO_SOURCE_FOR, name=qualified_name)

            embedded_count += flush()

            logger.info(ls.EMBEDDINGS_COMPLETE, count=embedded_count)

            self._reconcile_embeddings(expected_ids, verify_stored_ids)

            get_embedding_cache().save()
            close_qdrant_client()

        except Exception as e:
            logger.warning(ls.EMBEDDING_GENERATION_FAILED, error=e)

    def _reconcile_embeddings(
        self,
        expected_ids: set[int],
        verify_fn: Callable[[set[int]], set[int]],
    ) -> None:
        if not expected_ids:
            return
        try:
            stored_ids = verify_fn(expected_ids)
            missing = expected_ids - stored_ids
            if missing:
                sample = sorted(missing)[:10]
                logger.warning(
                    ls.EMBEDDING_RECONCILE_MISSING.format(
                        missing=len(missing),
                        expected=len(expected_ids),
                        sample_ids=sample,
                    )
                )
            else:
                logger.info(ls.EMBEDDING_RECONCILE_OK.format(count=len(expected_ids)))
        except Exception as e:
            logger.warning(ls.EMBEDDING_RECONCILE_FAILED.format(error=e))

    def _extract_source_code(
        self, qualified_name: str, file_path: str, start_line: int, end_line: int
    ) -> str | None:
        if not file_path or not start_line or not end_line:
            return None

        file_path_obj = self.repo_path / file_path

        ast_extractor = None
        if file_path_obj in self.ast_cache:
            root_node, language = self.ast_cache[file_path_obj]
            fqn_config = LANGUAGE_FQN_SPECS.get(language)

            if fqn_config:

                def ast_extractor_func(qname: str, path: Path) -> str | None:
                    return find_function_source_by_fqn(
                        root_node,
                        qname,
                        path,
                        self.repo_path,
                        self.project_name,
                        fqn_config,
                    )

                ast_extractor = ast_extractor_func

        return extract_source_with_fallback(
            file_path_obj, start_line, end_line, qualified_name, ast_extractor
        )

    def _parse_embedding_result(self, row: ResultRow) -> EmbeddingQueryResult | None:
        node_id = row.get(cs.KEY_NODE_ID)
        qualified_name = row.get(cs.KEY_QUALIFIED_NAME)

        if not isinstance(node_id, int) or not isinstance(qualified_name, str):
            return None

        start_line = row.get(cs.KEY_START_LINE)
        end_line = row.get(cs.KEY_END_LINE)
        file_path = row.get(cs.KEY_PATH)

        return EmbeddingQueryResult(
            node_id=node_id,
            qualified_name=qualified_name,
            start_line=start_line if isinstance(start_line, int) else None,
            end_line=end_line if isinstance(end_line, int) else None,
            path=file_path if isinstance(file_path, str) else None,
        )
