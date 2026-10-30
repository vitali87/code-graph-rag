"""Restore the import maps of the unchanged modules an incremental run's
re-parsed files reach, so their re-exports resolve as a clean index resolves
them (issue #2559)."""

from collections.abc import Mapping
from pathlib import Path

from loguru import logger
from tree_sitter import Parser

from .. import constants as cs
from .. import logs as ls
from ..ast_cache import BoundedASTCache
from ..language_spec import get_language_spec
from ..parsers.factory import ProcessorFactory
from ..types_defs import LanguageQueries
from .frontends import FrontendRunner


def _module_holding(name: str, module_paths: Mapping[str, Path]) -> str | None:
    """The file module `name` lives in: its longest dotted prefix that is one.

    The name itself counts, since an import can name a whole module.
    """
    parts = name.split(cs.SEPARATOR_DOT)
    for end in range(len(parts), 0, -1):
        prefix = cs.SEPARATOR_DOT.join(parts[:end])
        if prefix in module_paths:
            return prefix
    return None


def _file_module_holding(name: str, module_paths: Mapping[str, Path]) -> str | None:
    """`_module_holding`, widened from an inline `mod` to its file's module.

    An incremental run seeds an inline `mod`'s node, which carries its file's
    path, as a module of its own. Its imports are its file's to restore, and
    restored under the `mod`'s qn they keyed one scope too deep (#2805
    review). The file's module is the shortest dotted prefix on that path.
    """
    holder = _module_holding(name, module_paths)
    if holder is None:
        return None
    path = module_paths[holder]
    parts = holder.split(cs.SEPARATOR_DOT)
    for end in range(1, len(parts)):
        prefix = cs.SEPARATOR_DOT.join(parts[:end])
        if module_paths.get(prefix) == path:
            return prefix
    return holder


class ImportClosure:
    """The import closure of the files a run re-parsed, read from disk.

    `ast_cache` re-parses a module the run did not, and `frontend_runner`
    names the files the libclang frontend emitted, whose imports Pass 2
    parses none of.
    """

    def __init__(
        self,
        factory: ProcessorFactory,
        ast_cache: BoundedASTCache,
        parsers: Mapping[cs.SupportedLanguage, Parser],
        queries: Mapping[cs.SupportedLanguage, LanguageQueries],
        repo_path: Path,
        frontend_runner: FrontendRunner,
    ) -> None:
        self._factory = factory
        self._ast_cache = ast_cache
        self._parsers = parsers
        self._queries = queries
        self._repo_path = repo_path
        self._frontend_runner = frontend_runner

    def restore_reached(self) -> None:
        """Parse the imports of the unchanged modules re-parsed files reach.

        An incremental run parses only the changed files, yet resolving them
        follows re-exports through the import maps of the modules they import
        from: `from pkg import Client` names `pkg.Client`, which only
        `pkg/__init__.py`'s map turns into `pkg._client.Client`. A clean index
        holds every map; without them the receiver lost its type and the call
        bound by name to whatever else carries it (issue #2559).

        What is read is the import closure of the re-parsed files: each module
        an import names is restored and then walked in turn. A reached
        definition carries the walk on too, since resolving its own
        annotations (`def make() -> Client` in an unchanged module) reads its
        module's map, and that can lead to yet another re-export. A module no
        re-parsed file reaches is never opened, and a module whose imports
        this updater already holds is not parsed again.
        """
        mapping = self._factory.import_processor.import_mapping
        module_paths = self._factory.definition_processor.module_qn_to_file_path
        if all(module_qn in mapping for module_qn in module_paths):
            # A full build or a reused updater: every map is already held.
            return
        restored = self._restore_closure(module_paths)
        if restored:
            logger.info(ls.IMPORT_STATE_RESTORED, count=restored)

    def _restore_closure(self, module_paths: Mapping[str, Path]) -> int:
        """Walk out from every held map, restoring each unheld module reached.

        Returns how many modules had their imports restored.
        """
        import_processor = self._factory.import_processor
        mapping = import_processor.import_mapping
        # Read once: each restored map is re-pointed before the walk reads it.
        siblings = import_processor.stem_sibling_modules(module_paths)
        # The walk is a closure, so the order scopes are taken in is moot.
        pending = list(mapping)
        walked = set(pending)
        restored = 0
        while pending:
            scope = pending.pop()
            # A copy, not a live view: restoring a Rust module commits its
            # inline-`mod` uses under their scope keys until its parse is
            # retracted, and `mod b {}` in `a.rs` keys on the same qn as a
            # cfg-twin `a/b.rs`, so `scope`'s own dict can change mid-loop.
            # Its inline `mod`s' uses join it: they stay held back until the
            # arbitration after this walk, and an import through one
            # (`use crate::api::inner::Widget`) leads on from there.
            targets = [
                *mapping.get(scope, {}).values(),
                *import_processor.rust.rust_mod_scope_targets(scope),
            ]
            for target in targets:
                holder = _file_module_holding(target, module_paths)
                if holder is None or holder in walked:
                    continue
                walked.add(holder)
                if holder not in mapping:
                    if not self._restore_module_imports(holder, module_paths[holder]):
                        continue
                    restored += 1
                    # A restored map names a shared stem as written
                    # (`proj.shim.Widget`), while the module carries its
                    # suffix (`proj.shim.py`, issue #2586). The re-parsed
                    # maps were re-pointed before this walk; a restored one
                    # must be too before its targets are looked up, or the
                    # suffixed module is never found and never restored.
                    import_processor.point_module_imports_at_own_language_siblings(
                        holder, module_paths, siblings
                    )
                pending.append(holder)
        return restored

    def _restore_module_imports(self, module_qn: str, path: Path) -> bool:
        """Parse one unchanged module's imports; False when Pass 2 would not."""
        spec = get_language_spec(path.suffix)
        if spec is None or not isinstance(spec.language, cs.SupportedLanguage):
            return False
        language = spec.language
        language_queries = self._queries.get(language)
        if (
            language not in self._parsers
            or language_queries is None
            or not language_queries.get(cs.QUERY_IMPORTS)
        ):
            return False
        try:
            rel = path.relative_to(self._repo_path).as_posix()
        except ValueError:
            return False
        if rel in self._frontend_runner.cpp_covered:
            # The libclang frontend emitted this file, so Pass 2 parses none
            # of its imports either.
            return False
        entry = self._ast_cache.load(path)
        if entry is None:
            return False
        self._factory.import_processor.restore_unparsed_imports(
            entry[0],
            module_qn,
            language,
            self._queries,
            self._factory.facts.function_locations,
        )
        return True
