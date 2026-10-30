"""Parse one file into the graph through the first tier that claims it, and
re-parse a file the bounded AST cache evicted."""

from collections.abc import Collection, Iterable, Mapping
from pathlib import Path, PurePosixPath

from loguru import logger
from tree_sitter import Node, Parser, QueryCursor

from .. import constants as cs
from .. import logs as ls
from ..ast_cache import BoundedASTCache
from ..language_spec import get_language_for_extension, get_language_spec
from ..node_text import sorted_captures
from ..parsers.ast_grep_tier import AstGrepTier
from ..parsers.cpp.preproc_recovery import parse_with_preproc_recovery
from ..parsers.document_tier import DocumentTier
from ..parsers.factory import ProcessorFactory
from ..parsers.notebook import NotebookSkipped, is_notebook_path, read_notebook
from ..services import QueryProtocol
from ..types_defs import LanguageQueries
from ..utils.path_utils import (
    cached_relative_path,
    python_stub_has_implementation,
    walk_eligible_files,
)
from ..utils.source_encoding import grammar_bytes
from .frontends import FrontendRunner
from .generated_sources import GeneratedSources
from .types_defs import WalkScope


def read_or_empty(path: Path) -> bytes:
    """A file's bytes, or none when it cannot be read."""
    try:
        return path.read_bytes()
    except OSError:
        return b""


def is_dependency_file(file_name: str, filepath: Path) -> bool:
    return (
        file_name.lower() in cs.DEPENDENCY_FILES
        or filepath.suffix.lower() == cs.CSPROJ_SUFFIX
    )


class FileParser:
    """Every file of a pass parsed into the graph, one at a time.

    A file goes to Tree-sitter when a loaded grammar defines its module,
    then to the dependency-manifest parser, the notebook reader, and the
    secondary tiers (ast-grep, documents); every file gets its File node
    regardless. A parsed file joins `parsed_files`, the updater's record of
    what the call pass walks, and its tree the bounded `ast_cache`, which
    re-reads an evicted file through `load_ast_from_disk`. Files LIBCLANG
    emitted itself (`frontend_runner.cpp_covered`) keep only their File
    node, and a Lombok-annotated file is parsed through its delomboked
    bytes (`generated_sources.overlay`).
    """

    def __init__(
        self,
        graph: QueryProtocol | None,
        factory: ProcessorFactory,
        ast_cache: BoundedASTCache,
        parsers: Mapping[cs.SupportedLanguage, Parser],
        queries: Mapping[cs.SupportedLanguage, LanguageQueries],
        repo_path: Path,
        project_name: str,
        scope: WalkScope,
        parsed_files: list[tuple[Path, cs.SupportedLanguage]],
        frontend_runner: FrontendRunner,
        generated_sources: GeneratedSources,
        ast_grep_tier: AstGrepTier,
        document_tier: DocumentTier,
    ) -> None:
        self._graph = graph
        self._factory = factory
        self._ast_cache = ast_cache
        self._parsers = parsers
        self._queries = queries
        self._repo_path = repo_path
        self._project_name = project_name
        self._scope = scope
        self._parsed_files = parsed_files
        self._frontend_runner = frontend_runner
        self._generated_sources = generated_sources
        self._ast_grep_tier = ast_grep_tier
        self._document_tier = document_tier

    def resync_dependencies(
        self,
        touched_keys: Iterable[str],
        reparsed_keys: Collection[str],
        manifests: Iterable[tuple[Path, str]] | None = None,
    ) -> None:
        """Rebuild the project's DEPENDS_ON_EXTERNAL edges when a manifest moved.

        Re-parsing a manifest only MERGEs the dependencies it still names, so
        a dependency removed from it, or a whole manifest deleted, kept its
        edge through every later sync while a fresh index dropped it (issue
        #2396). An edge does not record which manifest declared it (two
        manifests can name one package), so the project's edges go and every
        manifest is parsed again; manifests are few and cheap to parse. The
        ones in `reparsed_keys` are skipped because the caller's re-parse
        re-adds them. `manifests` is the caller's own walk when it has one;
        a caller holding a partial file list walks the tree here instead.

        Runs before anything this sync buffers, so the orphan sweep cannot
        race a re-add: a package still declared is MERGEd back afterwards.
        """
        graph = self._graph
        if graph is None:
            return
        if not any(
            is_dependency_file(PurePosixPath(key).name, Path(key))
            for key in touched_keys
        ):
            return
        logger.info(ls.DEPENDENCIES_RESYNC, project=self._project_name)
        graph.execute_write(
            cs.CYPHER_DELETE_PROJECT_DEPENDENCIES,
            {cs.KEY_PROJECT_NAME: self._project_name},
        )
        graph.execute_write(cs.CYPHER_DELETE_ORPHAN_EXTERNAL_PACKAGES)
        if manifests is None:
            manifests = (
                (Path(f"{dirpath}/{fname}"), rel_path)
                for dirpath, fname, rel_path in walk_eligible_files(
                    self._repo_path,
                    self._scope.exclude_paths,
                    self._scope.unignore_paths,
                )
            )
        for path, key in manifests:
            if key not in reparsed_keys and is_dependency_file(path.name, path):
                self._factory.definition_processor.process_dependencies(path)

    def log_unparsable_manifests(self) -> None:
        """Name the manifests this run could not parse in one WARNING (#2568).

        Each is a DEBUG line already: fixture and mock manifests are broken on
        purpose often enough that a line apiece buried the run's own output,
        so they are counted like the unreadable files above. The shallowest is
        the one named, so a broken root manifest, which is the project's own
        dependency list, is never hidden behind a fixture that sorts first.
        """
        unparsable = self._factory.definition_processor.unparsable_manifests
        if not unparsable:
            return
        first = min(unparsable, key=lambda path: (len(path.parts), path))
        logger.warning(
            ls.DEP_MANIFESTS_UNPARSABLE,
            count=len(unparsable),
            path=cached_relative_path(first, self._repo_path).as_posix(),
            error=unparsable[first],
        )
        unparsable.clear()

    def pre_parse_changed_files(
        self,
        changed_entries: list[tuple[Path, str, bool, bytes]],
    ) -> dict[Path, tuple[Node, dict[str, list] | None]]:
        result: dict[Path, tuple[Node, dict[str, list] | None]] = {}
        for filepath, _file_key, _is_new, file_bytes in changed_entries:
            language = self.tree_sitter_language(filepath)
            if language is None:
                continue
            # `parsers` and `queries` arrive separately; a language one lacks
            # is left to the per-file path rather than aborting the run.
            language_queries = self._queries.get(language)
            if language_queries is None:
                continue
            parser = language_queries.get(cs.KEY_PARSER)
            if parser is None:
                continue
            tree = parse_with_preproc_recovery(
                parser, grammar_bytes(file_bytes, language, filepath), language
            )
            root_node = tree.root_node
            combined_query = language_queries[cs.QUERY_FUNC_CLASS_IMPORT]
            combined_captures: dict[str, list] | None = None
            if combined_query:
                cursor = QueryCursor(combined_query)
                combined_captures = sorted_captures(cursor, root_node)
            result[filepath] = (root_node, combined_captures)
        return result

    def process_single_file(
        self,
        filepath: Path,
        file_bytes: bytes | None = None,
        pre_parsed: tuple[Node, dict[str, list] | None] | None = None,
    ) -> None:
        """Ingest one file through the first tier that claims it.

        Tries tree-sitter, then dependency manifests, then the secondary
        tiers; every file gets a File node regardless, so an unclaimed
        extension is still represented in the graph.
        """
        covered = self._frontend_runner.cpp_covered
        if covered:
            rel = cached_relative_path(filepath, self._repo_path).as_posix()
            if rel in covered:
                # The libclang frontend already emitted this file's
                # definitions; keep only the generic File node.
                self._factory.structure_processor.process_generic_file(
                    filepath, filepath.name
                )
                return

        if (language := self.tree_sitter_language(filepath)) is not None:
            result = self._factory.definition_processor.process_file(
                filepath,
                language,
                self._queries,
                self._factory.structure_processor.structural_elements,
                source_bytes=file_bytes,
                pre_parsed=pre_parsed,
            )
            if result:
                root_node, language = result
                self._ast_cache[filepath] = (root_node, language)
                self._parsed_files.append((filepath, language))
        elif is_dependency_file(filepath.name, filepath):
            self._factory.definition_processor.process_dependencies(filepath)
        elif is_notebook_path(filepath):
            self._process_notebook(filepath, file_bytes)
        else:
            self.process_with_secondary_tier(filepath)

        self._factory.structure_processor.process_generic_file(filepath, filepath.name)

    def tree_sitter_language(self, filepath: Path) -> cs.SupportedLanguage | None:
        """The loaded grammar that defines `filepath`'s module, if any.

        None for a `.pyi` stub whose implementation is indexed: it shares that
        module's qn, and parsing it would claim or twin the Module. It still
        falls through to the generic File node (issue #2445).
        """
        lang_config = get_language_spec(filepath.suffix)
        if not (
            lang_config
            and isinstance(lang_config.language, cs.SupportedLanguage)
            and lang_config.language in self._parsers
        ):
            return None
        if python_stub_has_implementation(
            filepath,
            self._repo_path,
            exclude_paths=self._scope.exclude_paths,
            unignore_paths=self._scope.unignore_paths,
        ):
            return None
        return lang_config.language

    def process_with_secondary_tier(self, filepath: Path) -> bool:
        """Parse a file with whichever non-tree-sitter tier claims it.

        Shared with the file watcher, which re-parses one changed file and
        would otherwise handle only tree-sitter languages: it deletes a
        file's Module (and everything hanging off it) before re-parsing, so
        a tier it does not know about loses its symbols entirely rather than
        merely going stale (issue #1427).

        Returns True when a tier took the file, so the batch path can keep
        its if/elif chain.
        """
        structural_elements = self._factory.structure_processor.structural_elements
        if self._ast_grep_tier.handles(filepath.suffix):
            self._ast_grep_tier.process_file(filepath, structural_elements)
            return True
        if self._document_tier.handles(filepath.suffix):
            self._document_tier.process_file(filepath, structural_elements)
            return True
        # A file no tier took still gets its File node; the ast-grep tier
        # counts the ones it only skipped for lack of its extra.
        self._ast_grep_tier.note_unparsed(filepath.suffix)
        return False

    def _process_notebook(self, filepath: Path, file_bytes: bytes | None) -> None:
        """Parse a Python notebook's code cells as the module they make.

        Registered with the call pass like any parsed file, so its imports and
        calls become edges; a notebook in another language, or one that does
        not read as nbformat 4, keeps only its `File` node (issue #2480).
        """
        if cs.SupportedLanguage.PYTHON not in self._parsers:
            return
        notebook = read_notebook(
            read_or_empty(filepath) if file_bytes is None else file_bytes
        )
        rel = cached_relative_path(filepath, self._repo_path).as_posix()
        if isinstance(notebook, NotebookSkipped):
            if notebook.reason == cs.NotebookSkip.NOT_PYTHON:
                logger.debug(
                    ls.NOTEBOOK_NOT_PYTHON, path=rel, language=notebook.language
                )
            else:
                logger.warning(ls.NOTEBOOK_UNREADABLE, path=rel, reason=notebook.reason)
            return
        result = self._factory.definition_processor.process_file(
            filepath,
            cs.SupportedLanguage.PYTHON,
            self._queries,
            self._factory.structure_processor.structural_elements,
            source_bytes=notebook.source,
        )
        if result:
            self._ast_cache[filepath] = result
            self._parsed_files.append((filepath, cs.SupportedLanguage.PYTHON))
            logger.debug(ls.NOTEBOOK_INDEXED, path=rel, cells=len(notebook.cells))

    def ast_for(self, file_path: Path) -> Node | None:
        """The cached AST root for a file, or None when it is not cached."""
        entry = self._ast_cache.load(file_path)
        return entry[0] if entry else None

    def load_ast_from_disk(
        self, file_path: Path
    ) -> tuple[Node, cs.SupportedLanguage] | None:
        """Re-parse a file the AST cache evicted, for BoundedASTCache.

        Evicted files carry stale captures (nodes from the discarded tree),
        so those are dropped: downstream recomputes them from the fresh tree.
        """
        if is_notebook_path(file_path):
            return self._load_notebook_ast(file_path)
        language = get_language_for_extension(file_path.suffix)
        if language is None or language not in self._parsers:
            return None
        parser = self._queries[language].get(cs.KEY_PARSER)
        if parser is None:
            return None
        try:
            file_bytes = file_path.read_bytes()
            overlay_key = cached_relative_path(file_path, self._repo_path).as_posix()
            file_bytes = self._generated_sources.overlay.get(overlay_key, file_bytes)
        except OSError as e:
            logger.error(ls.AST_RELOAD_FAILED, path=file_path, error=e)
            return None
        # Transcoded exactly as the first parse was, or a declared-encoding
        # file's call pass would see names its definitions do not carry.
        parsed = grammar_bytes(file_bytes, language, file_path)
        root_node = parse_with_preproc_recovery(parser, parsed, language).root_node
        if language == cs.SupportedLanguage.SQL:
            # The reload is the tree the call pass now walks, so its DDL must
            # come from these bytes, not the first parse's (issue #3181).
            self._factory.definition_processor.keep_sql_source(
                file_path, root_node, parsed
            )
        self._factory.func_class_captures_cache.pop(file_path, None)
        return (root_node, language)

    def _load_notebook_ast(
        self, file_path: Path
    ) -> tuple[Node, cs.SupportedLanguage] | None:
        """Re-parse an evicted notebook from the Python source it was parsed as."""
        language_queries = self._queries.get(cs.SupportedLanguage.PYTHON)
        parser = language_queries.get(cs.KEY_PARSER) if language_queries else None
        if parser is None:
            return None
        try:
            notebook = read_notebook(file_path.read_bytes())
        except OSError as e:
            logger.error(ls.AST_RELOAD_FAILED, path=file_path, error=e)
            return None
        if isinstance(notebook, NotebookSkipped):
            return None
        root_node = parse_with_preproc_recovery(
            parser, notebook.source, cs.SupportedLanguage.PYTHON
        ).root_node
        self._factory.func_class_captures_cache.pop(file_path, None)
        return (root_node, cs.SupportedLanguage.PYTHON)
