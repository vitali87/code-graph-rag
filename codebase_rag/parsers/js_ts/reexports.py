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
    if qn in function_registry:
        return qn
    return _ReexportWalk(
        import_mapping, exports, module_paths, function_registry
    ).follow(qn)[0]


def js_ts_module_qn(module_qn: str, module_paths: Mapping[str, Path]) -> str | None:
    """The JS/TS module a specifier's qn names, else None.

    A specifier names `lib.ts` when it exists, else `lib/index.ts`. Only a
    JS/TS module's maps hold JS export semantics.
    """
    for candidate in (
        module_qn,
        f"{module_qn}{cs.SEPARATOR_DOT}{cs.JS_INDEX_STEM}",
    ):
        path = module_paths.get(candidate)
        if path is not None and path.suffix in cs.JS_TS_ALL_EXTENSIONS:
            return candidate
    return None


class _Step(NamedTuple):
    """A qn the walk reached: a module's export, or (`local`) one of its own
    bindings."""

    qn: str
    local: bool


class _ReexportWalk:
    """One `follow_js_reexports` lookup.

    A module two `export *` paths reach is walked on each: the first path can
    end ambiguous where the second does not, so marking the module visited
    for the whole lookup lost the export the second path provides. Only a
    step on the current path stops a walk, which still ends every cycle; a
    step settled without meeting one is remembered, so a barrel shared by
    many paths is still walked once.
    """

    __slots__ = (
        "_imports",
        "_exports",
        "_modules",
        "_registry",
        "_path",
        "_settled",
    )

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
        self._path: set[_Step] = set()
        self._settled: dict[_Step, str] = {}

    def follow(self, qn: str) -> tuple[str, bool]:
        # `qn` names a module's export, then possibly one of its own bindings
        # (`local`). Also says whether a cycle cut the walk short: a cut walk
        # depends on the path that reached it, so it is never settled.
        step = _Step(qn, local=False)
        entered: list[_Step] = []
        cut = False
        while step.qn not in self._registry:
            if step in self._settled:
                step = _Step(self._settled[step], local=False)
                break
            if step in self._path:
                cut = True
                break
            self._path.add(step)
            entered.append(step)
            following, star_cut = self._next(step)
            cut = cut or star_cut
            if following is None:
                break
            step = following
        self._path.difference_update(entered)
        if not cut:
            self._settled.update(dict.fromkeys(entered, step.qn))
        return step.qn, cut

    def _next(self, step: _Step) -> tuple[_Step | None, bool]:
        if step.local:
            return self._binding(step.qn), False
        return self._export(step.qn)

    def _export(self, qn: str) -> tuple[_Step | None, bool]:
        module_qn, separator, name = qn.rpartition(cs.SEPARATOR_DOT)
        barrel = js_ts_module_qn(module_qn, self._modules) if separator else None
        if barrel is None:
            return None, False
        if (export := self._exports.get(barrel, {}).get(name)) is not None:
            return _Step(export.target, export.local), False
        # `export *` passes on every name a source exports except `default`.
        if name == cs.TS_EXPORT_DEFAULT:
            return None, False
        star, cut = self._star_source(barrel, name)
        return (_Step(star, local=False) if star is not None else None), cut

    def _binding(self, qn: str) -> _Step | None:
        # A binding the module exports but does not define is an import of
        # it: `import { pad } from "./pad"; export { pad as leftPad }`.
        module_qn, _separator, name = qn.rpartition(cs.SEPARATOR_DOT)
        imported = self._imports.get(module_qn, {}).get(name)
        return _Step(imported, local=False) if imported is not None else None

    def _star_source(self, barrel: str, name: str) -> tuple[str | None, bool]:
        # Each `export * from` source may export the name, itself possibly
        # through further barrels. Two that reach different definitions make
        # the name ambiguous, and TypeScript exports neither, so nothing is
        # guessed; one reached along two paths is one binding. A declaration
        # its module does not export is never passed on.
        reached: set[str] = set()
        cut = False
        for key, source in self._imports.get(barrel, {}).items():
            if not key.startswith(cs.IMPORTED_NAME_WILDCARD):
                continue
            target, branch_cut = self.follow(f"{source}{cs.SEPARATOR_DOT}{name}")
            cut = cut or branch_cut
            if target in self._registry and not self._registry.is_module_private(
                target
            ):
                reached.add(target)
        return (reached.pop() if len(reached) == 1 else None), cut
