"""Follow a JS/TS import through the barrels that re-export it (issue #2464).

A library exposes its API through barrels (`lib/index.ts`): `export * from`,
`export { add as plus } from`, `export { default as times } from`, or an
import the barrel exports again. An importer's map names the barrel
(`lib.plus`), where nothing is registered; what the barrel exports under that
name is in its export table and its `export *` sources. Only exports are
followed: a barrel may import `foo` for its own use and export another `foo`.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import NamedTuple

from ... import constants as cs
from ...types_defs import FunctionRegistryTrieProtocol


class JsExport(NamedTuple):
    """What one name a JS/TS module exports stands for."""

    # `<module>.<binding>` for the module's own binding (`local`), or
    # `<source>.<name>` for another module's export it re-exports.
    target: str
    local: bool


def follow_js_reexports(
    qn: str,
    import_mapping: Mapping[str, Mapping[str, str]],
    exports: Mapping[str, Mapping[str, JsExport]],
    module_paths: Mapping[str, Path],
    function_registry: FunctionRegistryTrieProtocol,
) -> str:
    """Where the import of `qn` (`<module>.<name>`) leads through barrels.

    The registered definition the chain of barrels reaches, or the last name
    it reached when it ends anywhere else: a name no barrel exports, two star
    sources exporting it, a cycle, or `<module>.default` for a default export
    known by no name.
    """
    return _ReexportWalk(
        import_mapping, exports, module_paths, function_registry
    ).follow(qn, set())


class _ReexportWalk:
    __slots__ = ("_imports", "_exports", "_modules", "_registry")

    def __init__(
        self,
        import_mapping: Mapping[str, Mapping[str, str]],
        exports: Mapping[str, Mapping[str, JsExport]],
        module_paths: Mapping[str, Path],
        function_registry: FunctionRegistryTrieProtocol,
    ) -> None:
        self._imports = import_mapping
        self._exports = exports
        self._modules = module_paths
        self._registry = function_registry

    def follow(self, qn: str, seen: set[tuple[str, bool]]) -> str:
        # `qn` names a module's export, then possibly one of its own bindings
        # (`local`). `seen` is shared with the star branches, so a cycle of
        # barrels ends.
        current, local = qn, False
        while current not in self._registry and (current, local) not in seen:
            seen.add((current, local))
            step = self._binding(current) if local else self._export(current, seen)
            if step is None:
                break
            current, local = step
        return current

    def _export(self, qn: str, seen: set[tuple[str, bool]]) -> tuple[str, bool] | None:
        module_qn, separator, name = qn.rpartition(cs.SEPARATOR_DOT)
        barrel = self._barrel(module_qn) if separator else None
        if barrel is None:
            return None
        if (export := self._exports.get(barrel, {}).get(name)) is not None:
            return export.target, export.local
        # `export *` passes on every name a source exports except `default`.
        if name == cs.TS_EXPORT_DEFAULT:
            return None
        if (star := self._star_source(barrel, name, seen)) is None:
            return None
        return star, False

    def _binding(self, qn: str) -> tuple[str, bool] | None:
        # A binding the module exports but does not define is an import of
        # it: `import { pad } from "./pad"; export { pad as leftPad }`.
        module_qn, _separator, name = qn.rpartition(cs.SEPARATOR_DOT)
        imported = self._imports.get(module_qn, {}).get(name)
        return (imported, False) if imported is not None else None

    def _barrel(self, module_qn: str) -> str | None:
        # A specifier names `lib.ts` when it exists, else `lib/index.ts`.
        # Only a JS/TS module's maps hold JS export semantics.
        for candidate in (
            module_qn,
            f"{module_qn}{cs.SEPARATOR_DOT}{cs.JS_INDEX_STEM}",
        ):
            path = self._modules.get(candidate)
            if path is not None and path.suffix in cs.JS_TS_ALL_EXTENSIONS:
                return candidate
        return None

    def _star_source(
        self, barrel: str, name: str, seen: set[tuple[str, bool]]
    ) -> str | None:
        # Each `export * from` source may export the name, itself possibly
        # through further barrels. Two that reach different definitions make
        # the name ambiguous, and TypeScript exports neither, so nothing is
        # guessed.
        reached = {
            target
            for key, source in self._imports.get(barrel, {}).items()
            if key.startswith(cs.IMPORTED_NAME_WILDCARD)
            and not self._private_to(source, name)
            and (target := self.follow(f"{source}{cs.SEPARATOR_DOT}{name}", seen))
            in self._registry
        }
        return reached.pop() if len(reached) == 1 else None

    def _private_to(self, source: str, name: str) -> bool:
        # A definition of `name` in the source module that it does not
        # export: `export *` does not pass it on, so it neither binds nor
        # makes an exported twin in another source ambiguous.
        if f"{source}{cs.SEPARATOR_DOT}{name}" not in self._registry:
            return False
        module = self._barrel(source)
        return module is not None and name not in self._exports.get(module, {})
