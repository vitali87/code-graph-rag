import sys
from collections import OrderedDict, defaultdict
from collections.abc import Callable, ItemsView, KeysView
from pathlib import Path

from loguru import logger
from tree_sitter import Node, Parser

from .constants import (
    BYTES_PER_MB,
    CACHE_EVICTION_DIVISOR,
    CACHE_MEMORY_THRESHOLD_RATIO,
    CSPROJ_SUFFIX,
    CYPHER_QUERY_EMBEDDINGS,
    DEFAULT_CACHE_ENTRIES,
    DEFAULT_CACHE_MEMORY_MB,
    DEPENDENCY_FILES,
    EMBEDDING_PROGRESS_INTERVAL,
    IGNORE_PATTERNS,
    INIT_PY,
    KEY_END_LINE,
    KEY_NAME,
    KEY_NODE_ID,
    KEY_PATH,
    KEY_QUALIFIED_NAME,
    KEY_START_LINE,
    LOG_ANALYSIS_COMPLETE,
    LOG_CLEANED_SIMPLE_NAME,
    LOG_EMBEDDING_FAILED,
    LOG_EMBEDDING_GENERATION_FAILED,
    LOG_EMBEDDING_PROGRESS,
    LOG_EMBEDDINGS_COMPLETE,
    LOG_ENSURING_PROJECT,
    LOG_FOUND_FUNCTIONS,
    LOG_GENERATING_EMBEDDINGS,
    LOG_INGESTOR_NO_QUERY,
    LOG_NO_FUNCTIONS_FOR_EMBEDDING,
    LOG_NO_SOURCE_FOR,
    LOG_PASS_1_STRUCTURE,
    LOG_PASS_2_FILES,
    LOG_PASS_3_CALLS,
    LOG_PASS_4_EMBEDDINGS,
    LOG_REMOVED_FROM_CACHE,
    LOG_REMOVING_QNS,
    LOG_REMOVING_STATE,
    LOG_SEMANTIC_NOT_AVAILABLE,
    NODE_PROJECT,
    SEPARATOR_DOT,
    TRIE_INTERNAL_PREFIX,
    TRIE_QN_KEY,
    TRIE_TYPE_KEY,
    SupportedLanguage,
)
from .language_config import LANGUAGE_FQN_CONFIGS, get_language_config
from .parsers.factory import ProcessorFactory
from .services import IngestorProtocol, QueryProtocol
from .types_defs import (
    EmbeddingQueryResult,
    FunctionRegistry,
    LanguageQueries,
    NodeType,
    QualifiedName,
    SimpleNameLookup,
    TrieNode,
)
from .utils.dependencies import has_semantic_dependencies
from .utils.fqn_resolver import find_function_source_by_fqn
from .utils.source_extraction import extract_source_with_fallback


class FunctionRegistryTrie:
    def __init__(self, simple_name_lookup: SimpleNameLookup | None = None) -> None:
        self.root: TrieNode = {}
        self._entries: FunctionRegistry = {}
        self._simple_name_lookup = simple_name_lookup

    def insert(self, qualified_name: QualifiedName, func_type: NodeType) -> None:
        self._entries[qualified_name] = func_type

        parts = qualified_name.split(SEPARATOR_DOT)
        current: TrieNode = self.root

        for part in parts:
            if part not in current:
                current[part] = {}
            child = current[part]
            assert isinstance(child, dict)
            current = child

        current[TRIE_TYPE_KEY] = func_type
        current[TRIE_QN_KEY] = qualified_name

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

        parts = qualified_name.split(SEPARATOR_DOT)
        self._cleanup_trie_path(parts, self.root)

    def _cleanup_trie_path(self, parts: list[str], node: TrieNode) -> bool:
        if not parts:
            node.pop(TRIE_QN_KEY, None)
            node.pop(TRIE_TYPE_KEY, None)
            return not node

        part = parts[0]
        if part not in node:
            return False

        child = node[part]
        assert isinstance(child, dict)
        if self._cleanup_trie_path(parts[1:], child):
            del node[part]

        is_endpoint = TRIE_QN_KEY in node
        has_children = any(not key.startswith(TRIE_INTERNAL_PREFIX) for key in node)
        return not has_children and not is_endpoint

    def _navigate_to_prefix(self, prefix: str) -> TrieNode | None:
        parts = prefix.split(SEPARATOR_DOT) if prefix else []
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
            if TRIE_QN_KEY in n:
                qn = n[TRIE_QN_KEY]
                func_type = n[TRIE_TYPE_KEY]
                assert isinstance(qn, str) and isinstance(func_type, NodeType)
                if filter_fn is None or filter_fn(qn):
                    results.append((qn, func_type))

            for key, child in n.items():
                if not key.startswith(TRIE_INTERNAL_PREFIX):
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

    def find_ending_with(self, suffix: str) -> list[QualifiedName]:
        if self._simple_name_lookup is not None and suffix in self._simple_name_lookup:
            # (H) O(1) lookup using the simple_name_lookup index
            return list(self._simple_name_lookup[suffix])
        # (H) Fallback to linear scan if no index available
        return [qn for qn in self._entries.keys() if qn.endswith(f".{suffix}")]

    def find_with_prefix(self, prefix: str) -> list[tuple[QualifiedName, NodeType]]:
        node = self._navigate_to_prefix(prefix)
        return [] if node is None else self._collect_from_subtree(node)


class BoundedASTCache:
    def __init__(
        self,
        max_entries: int = DEFAULT_CACHE_ENTRIES,
        max_memory_mb: int = DEFAULT_CACHE_MEMORY_MB,
    ):
        self.cache: OrderedDict[Path, tuple[Node, SupportedLanguage]] = OrderedDict()
        self.max_entries = max_entries
        self.max_memory_bytes = max_memory_mb * BYTES_PER_MB

    def __setitem__(self, key: Path, value: tuple[Node, SupportedLanguage]) -> None:
        if key in self.cache:
            del self.cache[key]

        self.cache[key] = value

        self._enforce_limits()

    def __getitem__(self, key: Path) -> tuple[Node, SupportedLanguage]:
        value = self.cache[key]
        self.cache.move_to_end(key)
        return value

    def __delitem__(self, key: Path) -> None:
        if key in self.cache:
            del self.cache[key]

    def __contains__(self, key: Path) -> bool:
        return key in self.cache

    def items(self) -> ItemsView[Path, tuple[Node, SupportedLanguage]]:
        return self.cache.items()

    def _enforce_limits(self) -> None:
        while len(self.cache) > self.max_entries:
            self.cache.popitem(last=False)  # (H) Remove least recently used

        if self._should_evict_for_memory():
            entries_to_remove = max(1, len(self.cache) // CACHE_EVICTION_DIVISOR)
            for _ in range(entries_to_remove):
                if self.cache:
                    self.cache.popitem(last=False)

    def _should_evict_for_memory(self) -> bool:
        try:
            cache_size = sum(sys.getsizeof(v) for v in self.cache.values())
            return cache_size > self.max_memory_bytes
        except Exception:
            return len(self.cache) > self.max_entries * CACHE_MEMORY_THRESHOLD_RATIO


class GraphUpdater:
    def __init__(
        self,
        ingestor: IngestorProtocol,
        repo_path: Path,
        parsers: dict[SupportedLanguage, Parser],
        queries: dict[SupportedLanguage, LanguageQueries],
    ):
        self.ingestor = ingestor
        self.repo_path = repo_path
        self.parsers = parsers
        self.queries = queries
        self.project_name = repo_path.name
        self.simple_name_lookup: SimpleNameLookup = defaultdict(set)
        self.function_registry = FunctionRegistryTrie(
            simple_name_lookup=self.simple_name_lookup
        )
        self.ast_cache = BoundedASTCache()
        self.ignore_dirs = IGNORE_PATTERNS

        self.factory = ProcessorFactory(
            ingestor=self.ingestor,
            repo_path=self.repo_path,
            project_name=self.project_name,
            queries=self.queries,
            function_registry=self.function_registry,
            simple_name_lookup=self.simple_name_lookup,
            ast_cache=self.ast_cache,
        )

    def _is_dependency_file(self, file_name: str, filepath: Path) -> bool:
        return (
            file_name.lower() in DEPENDENCY_FILES
            or filepath.suffix.lower() == CSPROJ_SUFFIX
        )

    def run(self) -> None:
        self.ingestor.ensure_node_batch(NODE_PROJECT, {KEY_NAME: self.project_name})
        logger.info(LOG_ENSURING_PROJECT.format(name=self.project_name))

        logger.info(LOG_PASS_1_STRUCTURE)
        self.factory.structure_processor.identify_structure()

        logger.info(LOG_PASS_2_FILES)
        self._process_files()

        logger.info(LOG_FOUND_FUNCTIONS.format(count=len(self.function_registry)))
        logger.info(LOG_PASS_3_CALLS)
        self._process_function_calls()

        self.factory.definition_processor.process_all_method_overrides()

        logger.info(LOG_ANALYSIS_COMPLETE)
        self.ingestor.flush_all()

        self._generate_semantic_embeddings()

    def remove_file_from_state(self, file_path: Path) -> None:
        logger.debug(LOG_REMOVING_STATE.format(path=file_path))

        if file_path in self.ast_cache:
            del self.ast_cache[file_path]
            logger.debug(LOG_REMOVED_FROM_CACHE)

        relative_path = file_path.relative_to(self.repo_path)
        path_parts = (
            relative_path.parent.parts
            if file_path.name == INIT_PY
            else relative_path.with_suffix("").parts
        )
        module_qn_prefix = SEPARATOR_DOT.join([self.project_name, *path_parts])

        qns_to_remove = set()

        for qn in list(self.function_registry.keys()):
            if qn.startswith(f"{module_qn_prefix}.") or qn == module_qn_prefix:
                qns_to_remove.add(qn)
                del self.function_registry[qn]

        if qns_to_remove:
            logger.debug(LOG_REMOVING_QNS.format(count=len(qns_to_remove)))

        for simple_name, qn_set in self.simple_name_lookup.items():
            original_count = len(qn_set)
            new_qn_set = qn_set - qns_to_remove
            if len(new_qn_set) < original_count:
                self.simple_name_lookup[simple_name] = new_qn_set
                logger.debug(LOG_CLEANED_SIMPLE_NAME.format(name=simple_name))

    def _process_files(self) -> None:
        def should_skip_path(path: Path) -> bool:
            return any(
                part in self.ignore_dirs
                for part in path.relative_to(self.repo_path).parts
            )

        for filepath in self.repo_path.rglob("*"):
            if filepath.is_file() and not should_skip_path(filepath):
                lang_config = get_language_config(filepath.suffix)
                if (
                    lang_config
                    and isinstance(lang_config.language, SupportedLanguage)
                    and lang_config.language in self.parsers
                ):
                    result = self.factory.definition_processor.process_file(
                        filepath,
                        lang_config.language,
                        self.queries,
                        self.factory.structure_processor.structural_elements,
                    )
                    if result:
                        root_node, language = result
                        self.ast_cache[filepath] = (root_node, language)
                elif self._is_dependency_file(filepath.name, filepath):
                    self.factory.definition_processor.process_dependencies(filepath)

                self.factory.structure_processor.process_generic_file(
                    filepath, filepath.name
                )

    def _process_function_calls(self) -> None:
        ast_cache_items = list(self.ast_cache.items())
        for file_path, (root_node, language) in ast_cache_items:
            self.factory.call_processor.process_calls_in_file(
                file_path, root_node, language, self.queries
            )

    def _generate_semantic_embeddings(self) -> None:
        if not has_semantic_dependencies():
            logger.info(LOG_SEMANTIC_NOT_AVAILABLE)
            return

        if not isinstance(self.ingestor, QueryProtocol):
            logger.info(LOG_INGESTOR_NO_QUERY)
            return

        try:
            from .embedder import embed_code
            from .vector_store import store_embedding

            logger.info(LOG_PASS_4_EMBEDDINGS)

            results = self.ingestor.fetch_all(CYPHER_QUERY_EMBEDDINGS)

            if not results:
                logger.info(LOG_NO_FUNCTIONS_FOR_EMBEDDING)
                return

            logger.info(LOG_GENERATING_EMBEDDINGS.format(count=len(results)))

            embedded_count = 0
            for result in results:
                result: EmbeddingQueryResult
                node_id = result[KEY_NODE_ID]
                qualified_name = result[KEY_QUALIFIED_NAME]
                start_line = result.get(KEY_START_LINE)
                end_line = result.get(KEY_END_LINE)
                file_path = result.get(KEY_PATH)

                if source_code := self._extract_source_code(
                    qualified_name, file_path, start_line, end_line
                ):
                    try:
                        embedding = embed_code(source_code)
                        store_embedding(node_id, embedding, qualified_name)
                        embedded_count += 1

                        if embedded_count % EMBEDDING_PROGRESS_INTERVAL == 0:
                            logger.debug(
                                LOG_EMBEDDING_PROGRESS.format(
                                    done=embedded_count, total=len(results)
                                )
                            )

                    except Exception as e:
                        logger.warning(
                            LOG_EMBEDDING_FAILED.format(name=qualified_name, error=e)
                        )
                else:
                    logger.debug(LOG_NO_SOURCE_FOR.format(name=qualified_name))

            logger.info(LOG_EMBEDDINGS_COMPLETE.format(count=embedded_count))

        except Exception as e:
            logger.warning(LOG_EMBEDDING_GENERATION_FAILED.format(error=e))

    def _extract_source_code(
        self, qualified_name: str, file_path: str, start_line: int, end_line: int
    ) -> str | None:
        if not file_path or not start_line or not end_line:
            return None

        file_path_obj = self.repo_path / file_path

        ast_extractor = None
        if file_path_obj in self.ast_cache:
            root_node, language = self.ast_cache[file_path_obj]
            fqn_config = LANGUAGE_FQN_CONFIGS.get(language)

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
