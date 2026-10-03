"""Follow a JS/TS import through the barrels that re-export it (issue #2464).

A library exposes its API through barrels (`lib/index.ts`): `export * from`,
`export { add as plus } from`, `export { default as times } from`, or an
import the barrel exports again. An importer's map names the barrel
(`lib.plus`), where nothing is registered; what the barrel binds that name to
is in its own import map (the re-exports with a `from`) and its export
bindings (its own bindings exported under another name).
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from ... import constants as cs
from ...types_defs import FunctionRegistryTrieProtocol


def follow_js_reexports(
    qn: str,
    import_mapping: Mapping[str, Mapping[str, str]],
    export_bindings: Mapping[str, Mapping[str, str]],
    module_paths: Mapping[str, Path],
    function_registry: FunctionRegistryTrieProtocol,
) -> str:
    """Where the import of `qn` (`<module>.<name>`) leads through barrels.

    The registered definition the chain of barrels reaches, or the last name
    it reached when it ends anywhere else: a name no barrel binds, two star
    sources exporting it, a cycle, or `<module>.default` for a default export
    known by no name.
    """
    return _ReexportWalk(
        import_mapping, export_bindings, module_paths, function_registry
    ).follow(qn, set())


class _ReexportWalk:
    __slots__ = ("_imports", "_exports", "_modules", "_registry")

    def __init__(
        self,
        import_mapping: Mapping[str, Mapping[str, str]],
        export_bindings: Mapping[str, Mapping[str, str]],
        module_paths: Mapping[str, Path],
        function_registry: FunctionRegistryTrieProtocol,
    ) -> None:
        self._imports = import_mapping
        self._exports = export_bindings
        self._modules = module_paths
        self._registry = function_registry

    def follow(self, qn: str, seen: set[str]) -> str:
        # `seen` is shared with the star branches, so a cycle of barrels ends.
        current = qn
        while current not in self._registry and current not in seen:
            seen.add(current)
            following = self._hop(current, seen)
            if following is None:
                break
            current = following
        return current

    def _hop(self, qn: str, seen: set[str]) -> str | None:
        module_qn, separator, name = qn.rpartition(cs.SEPARATOR_DOT)
        barrel = self._barrel(module_qn) if separator else None
        if barrel is None:
            return None
        # The barrel's own `export { local as name }` comes first: a module
        # may import a name and export another binding under it.
        if (local := self._exports.get(barrel, {}).get(name)) is not None:
            return local
        bindings = self._imports.get(barrel, {})
        if (bound := bindings.get(name)) is not None:
            return bound
        # `export *` passes on every name a source exports except `default`.
        if name == cs.TS_EXPORT_DEFAULT:
            return None
        return self._star_source(bindings, name, seen)

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
        self, bindings: Mapping[str, str], name: str, seen: set[str]
    ) -> str | None:
        # Each `export * from` source may export the name, itself possibly
        # through further barrels. Two that reach different definitions make
        # the name ambiguous, and TypeScript exports neither, so nothing is
        # guessed.
        reached = {
            target
            for key, source in bindings.items()
            if key.startswith(cs.IMPORTED_NAME_WILDCARD)
            and (target := self.follow(f"{source}{cs.SEPARATOR_DOT}{name}", seen))
            in self._registry
        }
        return reached.pop() if len(reached) == 1 else None
