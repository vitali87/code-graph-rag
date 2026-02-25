import sys
from collections import OrderedDict, defaultdict
from collections.abc import Callable, ItemsView, KeysView
from pathlib import Path

from loguru import logger
from tree_sitter import Node, Parser

from . import constants as cs
from . import logs as ls
from .config import settings
from .language_spec import LANGUAGE_FQN_SPECS, get_language_spec
from .parsers.factory import ProcessorFactory
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
from .utils.path_utils import should_skip_path
from .utils.source_extraction import extract_source_with_fallback


class FunctionRegistryTrie:
    def __init__(self, simple_name_lookup: SimpleNameLookup | None = None) -> None:
        self.root: TrieNode = {}
        self._entries: FunctionRegistry = {}
        self._simple_name_lookup = simple_name_lookup

    def insert(self, qualified_name: QualifiedName, func_type: NodeType) -> None:
        self._entries[qualified_name] = func_type

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


class GraphUpdater:
    def __init__(
        self,
        ingestor: IngestorProtocol,
        repo_path: Path,
        parsers: dict[cs.SupportedLanguage, Parser],
        queries: dict[cs.SupportedLanguage, LanguageQueries],
        unignore_paths: frozenset[str] | None = None,
        exclude_paths: frozenset[str] | None = None,
    ):
        self.ingestor = ingestor
        self.repo_path = repo_path
        self.parsers = parsers
        self.queries = queries
        self.project_name = repo_path.resolve().name
        self.simple_name_lookup: SimpleNameLookup = defaultdict(set)
        self.function_registry = FunctionRegistryTrie(
            simple_name_lookup=self.simple_name_lookup
        )
        self.ast_cache = BoundedASTCache()
        self.unignore_paths = unignore_paths
        self.exclude_paths = exclude_paths

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

    def _is_dependency_file(self, file_name: str, filepath: Path) -> bool:
        return (
            file_name.lower() in cs.DEPENDENCY_FILES
            or filepath.suffix.lower() == cs.CSPROJ_SUFFIX
        )

    def run(self) -> None:
        self.ingestor.ensure_node_batch(
            cs.NODE_PROJECT, {cs.KEY_NAME: self.project_name}
        )
        logger.info(ls.ENSURING_PROJECT.format(name=self.project_name))

        logger.info(ls.PASS_1_STRUCTURE)
        self.factory.structure_processor.identify_structure()

        logger.info(ls.PASS_2_FILES)
        self._process_files()

        logger.info(ls.FOUND_FUNCTIONS.format(count=len(self.function_registry)))
        logger.info(ls.PASS_3_CALLS)
        self._process_function_calls()

        self.factory.definition_processor.process_all_method_overrides()

        logger.info(ls.ANALYSIS_COMPLETE)
        self.ingestor.flush_all()

        self._generate_semantic_embeddings()

    def remove_file_from_state(self, file_path: Path) -> None:
        logger.debug(ls.REMOVING_STATE.format(path=file_path))

        if file_path in self.ast_cache:
            del self.ast_cache[file_path]
            logger.debug(ls.REMOVED_FROM_CACHE)

        relative_path = file_path.relative_to(self.repo_path)
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
            logger.debug(ls.REMOVING_QNS.format(count=len(qns_to_remove)))

        for simple_name, qn_set in self.simple_name_lookup.items():
            original_count = len(qn_set)
            new_qn_set = qn_set - qns_to_remove
            if len(new_qn_set) < original_count:
                self.simple_name_lookup[simple_name] = new_qn_set
                logger.debug(ls.CLEANED_SIMPLE_NAME.format(name=simple_name))

    def _process_files(self) -> None:
        for filepath in self.repo_path.rglob("*"):
            if filepath.is_file() and not should_skip_path(
                filepath,
                self.repo_path,
                exclude_paths=self.exclude_paths,
                unignore_paths=self.unignore_paths,
            ):
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
            logger.info(ls.SEMANTIC_NOT_AVAILABLE)
            return

        if not isinstance(self.ingestor, QueryProtocol):
            logger.info(ls.INGESTOR_NO_QUERY)
            return

        try:
            from .embedder import embed_code
            from .vector_store import store_embedding

            logger.info(ls.PASS_4_EMBEDDINGS)

            project_name = str(self.project_name).rstrip(".")
            results = self.ingestor.fetch_all(
                cs.CYPHER_QUERY_EMBEDDINGS, {"project_name": project_name}
            )

            if not results:
                logger.info(ls.NO_FUNCTIONS_FOR_EMBEDDING)
                return

            logger.info(ls.GENERATING_EMBEDDINGS.format(count=len(results)))

            embedded_count = 0
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
                    logger.debug(ls.NO_SOURCE_FOR.format(name=qualified_name))

                elif source_code := self._extract_source_code(
                    qualified_name, file_path, start_line, end_line
                ):
                    try:
                        embedding = embed_code(source_code)
                        store_embedding(node_id, embedding, qualified_name)
                        embedded_count += 1

                        if embedded_count % settings.EMBEDDING_PROGRESS_INTERVAL == 0:
                            logger.debug(
                                ls.EMBEDDING_PROGRESS.format(
                                    done=embedded_count, total=len(results)
                                )
                            )

                    except Exception as e:
                        logger.warning(
                            ls.EMBEDDING_FAILED.format(name=qualified_name, error=e)
                        )
                else:
                    logger.debug(ls.NO_SOURCE_FOR.format(name=qualified_name))
            logger.info(ls.EMBEDDINGS_COMPLETE.format(count=embedded_count))

        except Exception as e:
            logger.warning(ls.EMBEDDING_GENERATION_FAILED.format(error=e))

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
