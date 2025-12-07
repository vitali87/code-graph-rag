import sys
from collections import OrderedDict, defaultdict
from collections.abc import ItemsView, KeysView
from pathlib import Path
from typing import Any

from loguru import logger
from tree_sitter import Node, Parser

from .config import IGNORE_PATTERNS
from .language_config import LANGUAGE_FQN_CONFIGS, get_language_config
from .parsers.factory import ProcessorFactory
from .services import IngestorProtocol, QueryProtocol
from .utils.dependencies import has_semantic_dependencies
from .utils.fqn_resolver import find_function_source_by_fqn
from .utils.source_extraction import extract_source_with_fallback


class FunctionRegistryTrie:
    """Trie data structure optimized for function qualified name lookups."""

    def __init__(self) -> None:
        self.root: dict[str, Any] = {}
        self._entries: dict[str, str] = {}

    def insert(self, qualified_name: str, func_type: str) -> None:
        """Insert a function into the trie."""
        self._entries[qualified_name] = func_type

        parts = qualified_name.split(".")
        current = self.root

        for part in parts:
            if part not in current:
                current[part] = {}
            current = current[part]

        current["__type__"] = func_type
        current["__qn__"] = qualified_name

    def get(self, qualified_name: str, default: str | None = None) -> str | None:
        """Get function type by exact qualified name."""
        return self._entries.get(qualified_name, default)

    def __contains__(self, qualified_name: str) -> bool:
        """Check if qualified name exists in registry."""
        return qualified_name in self._entries

    def __getitem__(self, qualified_name: str) -> str:
        """Get function type by qualified name."""
        return self._entries[qualified_name]

    def __setitem__(self, qualified_name: str, func_type: str) -> None:
        """Set function type for qualified name."""
        self.insert(qualified_name, func_type)

    def __delitem__(self, qualified_name: str) -> None:
        """Remove qualified name from registry and clean up trie structure.

        Performs proper cleanup of the trie to prevent memory leaks during
        long-running sessions with file deletions/updates.
        """
        if qualified_name not in self._entries:
            return

        del self._entries[qualified_name]

        parts = qualified_name.split(".")
        self._cleanup_trie_path(parts, self.root)

    def _cleanup_trie_path(self, parts: list[str], node: dict[str, Any]) -> bool:
        """Recursively clean up empty trie nodes.

        Args:
            parts: Remaining parts of the qualified name path
            node: Current trie node

        Returns:
            True if current node is empty and can be deleted
        """
        if not parts:
            node.pop("__qn__", None)
            node.pop("__type__", None)
            return len(node) == 0

        part = parts[0]
        if part not in node:
            return False

        child_empty = self._cleanup_trie_path(parts[1:], node[part])

        if child_empty:
            del node[part]

        is_endpoint = "__qn__" in node
        has_children = any(not key.startswith("__") for key in node)
        return not has_children and not is_endpoint

    def keys(self) -> KeysView[str]:
        """Return all qualified names."""
        return self._entries.keys()

    def items(self) -> ItemsView[str, str]:
        """Return all (qualified_name, type) pairs."""
        return self._entries.items()

    def __len__(self) -> int:
        """Return number of entries."""
        return len(self._entries)

    def find_with_prefix_and_suffix(self, prefix: str, suffix: str) -> list[str]:
        """Find all qualified names that start with prefix and end with suffix."""
        results = []
        prefix_parts = prefix.split(".") if prefix else []

        current = self.root
        for part in prefix_parts:
            if part not in current:
                return []
            current = current[part]

        def dfs(node: dict[str, Any]) -> None:
            if "__qn__" in node:
                qn = node["__qn__"]
                if qn.endswith(f".{suffix}"):
                    results.append(qn)

            for key, child in node.items():
                if not key.startswith("__"):
                    dfs(child)

        dfs(current)
        return results

    def find_ending_with(self, suffix: str) -> list[str]:
        """Find all qualified names ending with the given suffix."""
        return [qn for qn in self._entries.keys() if qn.endswith(f".{suffix}")]

    def find_with_prefix(self, prefix: str) -> list[tuple[str, str]]:
        """Find all qualified names that start with the given prefix.

        Args:
            prefix: The prefix to search for (e.g., "module.Class.method")

        Returns:
            List of (qualified_name, type) tuples matching the prefix
        """
        results = []
        prefix_parts = prefix.split(".")

        current = self.root
        for part in prefix_parts:
            if part not in current:
                return []
            current = current[part]

        def dfs(node: dict[str, Any]) -> None:
            if "__qn__" in node:
                qn = node["__qn__"]
                func_type = node["__type__"]
                results.append((qn, func_type))

            for key, child in node.items():
                if not key.startswith("__"):
                    dfs(child)

        dfs(current)
        return results


class BoundedASTCache:
    """Memory-aware AST cache with automatic cleanup to prevent memory leaks.

    Uses LRU eviction strategy and monitors memory usage to maintain
    reasonable memory consumption during long-running analysis sessions.
    """

    def __init__(self, max_entries: int = 1000, max_memory_mb: int = 500):
        """Initialize the bounded AST cache.

        Args:
            max_entries: Maximum number of AST entries to cache
            max_memory_mb: Soft memory limit in MB for cache eviction
        """
        self.cache: OrderedDict[Path, tuple[Node, str]] = OrderedDict()
        self.max_entries = max_entries
        self.max_memory_bytes = max_memory_mb * 1024 * 1024

    def __setitem__(self, key: Path, value: tuple[Node, str]) -> None:
        """Add or update an AST cache entry with automatic cleanup."""
        if key in self.cache:
            del self.cache[key]

        self.cache[key] = value

        self._enforce_limits()

    def __getitem__(self, key: Path) -> tuple[Node, str]:
        """Get AST cache entry and mark as recently used."""
        value = self.cache[key]
        self.cache.move_to_end(key)
        return value

    def __delitem__(self, key: Path) -> None:
        """Remove entry from cache."""
        if key in self.cache:
            del self.cache[key]

    def __contains__(self, key: Path) -> bool:
        """Check if key exists in cache."""
        return key in self.cache

    def items(self) -> Any:
        """Return all cache items."""
        return self.cache.items()

    def _enforce_limits(self) -> None:
        """Enforce cache size and memory limits by evicting old entries."""
        while len(self.cache) > self.max_entries:
            self.cache.popitem(last=False)  # (H) Remove least recently used

        if self._should_evict_for_memory():
            entries_to_remove = max(1, len(self.cache) // 10)
            for _ in range(entries_to_remove):
                if self.cache:
                    self.cache.popitem(last=False)

    def _should_evict_for_memory(self) -> bool:
        """Check if we should evict entries due to memory pressure."""
        try:
            cache_size = sum(sys.getsizeof(v) for v in self.cache.values())
            return cache_size > self.max_memory_bytes
        except Exception:
            return len(self.cache) > self.max_entries * 0.8


class GraphUpdater:
    """Parses code using Tree-sitter and updates the graph."""

    def __init__(
        self,
        ingestor: IngestorProtocol,
        repo_path: Path,
        parsers: dict[str, Parser],
        queries: dict[str, Any],
    ):
        self.ingestor = ingestor
        self.repo_path = repo_path
        self.parsers = parsers
        self.queries = self._prepare_queries_with_parsers(queries, parsers)
        self.project_name = repo_path.name
        self.function_registry = FunctionRegistryTrie()
        self.simple_name_lookup: dict[str, set[str]] = defaultdict(set)
        self.ast_cache = BoundedASTCache(max_entries=1000, max_memory_mb=500)
        self.ignore_dirs = IGNORE_PATTERNS

        self.factory = ProcessorFactory(
            ingestor=self.ingestor,
            repo_path_getter=lambda: self.repo_path,
            project_name_getter=lambda: self.project_name,
            queries=self.queries,
            function_registry=self.function_registry,
            simple_name_lookup=self.simple_name_lookup,
            ast_cache=self.ast_cache,
        )

    def _is_dependency_file(self, file_name: str, filepath: Path) -> bool:
        """Check if a file is a dependency file that should be processed for external dependencies."""
        dependency_files = {
            "pyproject.toml",
            "requirements.txt",
            "package.json",
            "cargo.toml",
            "go.mod",
            "gemfile",
            "composer.json",
        }

        if file_name.lower() in dependency_files:
            return True

        if filepath.suffix.lower() == ".csproj":
            return True

        return False

    def _prepare_queries_with_parsers(
        self, queries: dict[str, Any], parsers: dict[str, Parser]
    ) -> dict[str, Any]:
        """Add parser references to query objects for processors."""
        updated_queries = {}
        for lang, query_data in queries.items():
            if lang in parsers:
                updated_queries[lang] = {**query_data, "parser": parsers[lang]}
            else:
                updated_queries[lang] = query_data
        return updated_queries

    def run(self) -> None:
        """Orchestrates the parsing and ingestion process."""
        self.ingestor.ensure_node_batch("Project", {"name": self.project_name})
        logger.info(f"Ensuring Project: {self.project_name}")

        logger.info("--- Pass 1: Identifying Packages and Folders ---")
        self.factory.structure_processor.identify_structure()

        logger.info(
            "\n--- Pass 2: Processing Files, Caching ASTs, and Collecting Definitions ---"
        )
        self._process_files()

        logger.info(
            f"\n--- Found {len(self.function_registry)} functions/methods in codebase ---"
        )
        logger.info("--- Pass 3: Processing Function Calls from AST Cache ---")
        self._process_function_calls()

        self.factory.definition_processor.process_all_method_overrides()

        logger.info("\n--- Analysis complete. Flushing all data to database... ---")
        self.ingestor.flush_all()

        self._generate_semantic_embeddings()

    def remove_file_from_state(self, file_path: Path) -> None:
        """Removes all state associated with a file from the updater's memory."""
        logger.debug(f"Removing in-memory state for: {file_path}")

        if file_path in self.ast_cache:
            del self.ast_cache[file_path]
            logger.debug("  - Removed from ast_cache")

        relative_path = file_path.relative_to(self.repo_path)
        if file_path.name == "__init__.py":
            module_qn_prefix = ".".join(
                [self.project_name] + list(relative_path.parent.parts)
            )
        else:
            module_qn_prefix = ".".join(
                [self.project_name] + list(relative_path.with_suffix("").parts)
            )

        qns_to_remove = set()

        for qn in list(self.function_registry.keys()):
            if qn.startswith(module_qn_prefix + ".") or qn == module_qn_prefix:
                qns_to_remove.add(qn)
                del self.function_registry[qn]

        if qns_to_remove:
            logger.debug(
                f"  - Removing {len(qns_to_remove)} QNs from function_registry"
            )

        for simple_name, qn_set in self.simple_name_lookup.items():
            original_count = len(qn_set)
            new_qn_set = qn_set - qns_to_remove
            if len(new_qn_set) < original_count:
                self.simple_name_lookup[simple_name] = new_qn_set
                logger.debug(f"  - Cleaned simple_name '{simple_name}'")

    def _process_files(self) -> None:
        """Second pass: Efficiently processes all files, parses them, and caches their ASTs."""

        def should_skip_path(path: Path) -> bool:
            """Check if file path should be skipped based on ignore patterns."""
            return any(
                part in self.ignore_dirs
                for part in path.relative_to(self.repo_path).parts
            )

        for filepath in self.repo_path.rglob("*"):
            if filepath.is_file() and not should_skip_path(filepath):
                lang_config = get_language_config(filepath.suffix)
                if lang_config and lang_config.name in self.parsers:
                    result = self.factory.definition_processor.process_file(
                        filepath,
                        lang_config.name,
                        self.queries,
                        self.factory.structure_processor.structural_elements,
                    )
                    if result:
                        root_node, language = result
                        self.ast_cache[filepath] = (root_node, language)

                    self.factory.structure_processor.process_generic_file(
                        filepath, filepath.name
                    )

                elif self._is_dependency_file(filepath.name, filepath):
                    self.factory.definition_processor.process_dependencies(filepath)
                    self.factory.structure_processor.process_generic_file(
                        filepath, filepath.name
                    )
                else:
                    self.factory.structure_processor.process_generic_file(
                        filepath, filepath.name
                    )

    def _process_function_calls(self) -> None:
        """Third pass: Process function calls using the cached ASTs."""
        ast_cache_items = list(self.ast_cache.items())
        for file_path, (root_node, language) in ast_cache_items:
            self.factory.call_processor.process_calls_in_file(
                file_path, root_node, language, self.queries
            )

    def _generate_semantic_embeddings(self) -> None:
        """Generate and store semantic embeddings for functions and methods."""
        if not has_semantic_dependencies():
            logger.info(
                "Semantic search dependencies not available, skipping embedding generation"
            )
            return

        if not isinstance(self.ingestor, QueryProtocol):
            logger.info(
                "Ingestor does not support querying, skipping embedding generation"
            )
            return

        try:
            from .embedder import embed_code
            from .vector_store import store_embedding

            logger.info("--- Pass 4: Generating semantic embeddings ---")

            query = """
            MATCH (m:Module)-[:DEFINES]->(n)
            WHERE n:Function OR n:Method
            RETURN id(n) AS node_id, n.qualified_name AS qualified_name,
                   n.start_line AS start_line, n.end_line AS end_line,
                   m.path AS path
            ORDER BY n.qualified_name
            """

            results = self.ingestor.fetch_all(query)

            if not results:
                logger.info("No functions or methods found for embedding generation")
                return

            logger.info(f"Generating embeddings for {len(results)} functions/methods")

            embedded_count = 0
            for result in results:
                node_id = result["node_id"]
                qualified_name = result["qualified_name"]
                start_line = result.get("start_line")
                end_line = result.get("end_line")
                file_path = result.get("path")

                source_code = self._extract_source_code(
                    qualified_name, file_path, start_line, end_line
                )

                if source_code:
                    try:
                        embedding = embed_code(source_code)
                        store_embedding(node_id, embedding, qualified_name)
                        embedded_count += 1

                        if embedded_count % 10 == 0:
                            logger.debug(
                                f"Generated {embedded_count}/{len(results)} embeddings"
                            )

                    except Exception as e:
                        logger.warning(f"Failed to embed {qualified_name}: {e}")
                else:
                    logger.debug(f"No source code found for {qualified_name}")

            logger.info(f"Successfully generated {embedded_count} semantic embeddings")

        except Exception as e:
            logger.warning(f"Failed to generate semantic embeddings: {e}")

    def _extract_source_code(
        self, qualified_name: str, file_path: str, start_line: int, end_line: int
    ) -> str | None:
        """Extract source code for a function/method from cached AST or file."""
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
