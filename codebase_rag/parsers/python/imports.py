"""Python's import pass: `import` and `from ... import` statements into the
shared import map, the names a conditional import binds and every target a
re-bound name took, and the package layout (the repo's own package, nested
source roots) a written module name resolves against.

`ImportProcessor` owns what every language shares (the import map, the
import sites, the deferred IMPORTS edges and their flush) and hands a Python
file's imports to the `PythonImporter` it builds."""

from functools import lru_cache
from pathlib import Path
from typing import Protocol

from loguru import logger
from tree_sitter import Node

from ... import constants as cs
from ... import logs as ls
from ...node_text import safe_decode_text
from ...types_defs import PropertyDict
from .source_roots import discover_python_source_roots, resolve_via_source_roots


def _is_conditional_import_node(import_node: Node) -> bool:
    # An import nested under an if/try (click's platform-conditional
    # `if WIN: from ._winconsole import X ... else: def X(...)`) binds its
    # name only on some runtime paths; a same-named local def is then a
    # mutually-exclusive fallback variant, not shadowed dead code.
    current = import_node.parent
    while current is not None:
        if current.type in (cs.TS_PY_IF_STATEMENT, cs.TS_PY_TRY_STATEMENT):
            return True
        current = current.parent
    return False


class PythonImportHost(Protocol):
    """What the Python importer calls on the import processor that builds it:
    the import-site record every language's bindings go through, whose sites
    ride on the IMPORTS edges the processor's flush emits."""

    def _record_import_site(
        self,
        scope_qn: str,
        local_name: str,
        node: Node | None,
        imported_name: str | None = None,
        owner_qn: str | None = None,
    ) -> PropertyDict | None: ...


class PythonImporter:
    __slots__ = (
        "_host",
        "repo_path",
        "project_name",
        "import_mapping",
        "conditional_imports",
        "python_import_rebinds",
        "_project_named_package",
        "_map_py_source_root",
        "_is_local_module_cached",
    )

    def __init__(
        self,
        host: PythonImportHost,
        repo_path: Path,
        project_name: str,
        import_mapping: dict[str, dict[str, str]],
    ) -> None:
        self._host = host
        self.repo_path = repo_path
        self.project_name = project_name
        # The processor's own map, shared rather than copied: an import binds
        # its names in the map every later pass reads.
        self.import_mapping = import_mapping
        # Names bound by a CONDITIONAL Python import (nested under if/try --
        # click's `if WIN: from ._winconsole import X`): the dead-code fan-out
        # treats a same-named local def as the mutually-exclusive fallback
        # variant ONLY for these; an unconditional import is plain shadowing.
        self.conditional_imports: dict[str, set[str]] = {}
        # Every target a Python name was bound to, for names its module's
        # imports bind more than once (`try: import tomllib` / `except
        # ImportError: import tomli as tomllib`). `import_mapping` keeps one
        # binding, and plain imports are handled before from-imports whatever
        # their source order, so the resolver needs all of them to tell a
        # module that is external on every path from one a branch binds to
        # first-party code (issue #2360).
        self.python_import_rebinds: dict[str, dict[str, set[str]]] = {}

        repo_is_package = (repo_path / cs.INIT_PY).is_file()

        # A repo whose top-level package shares the repo's name (a django
        # clone is django/django, celery is celery/celery) makes written
        # absolute imports collide with the project prefix: `django.http`
        # names the package dir and needs the project prefix on top
        # (django.django.http), while in a flat layout (repo root doubles
        # as the installed package) the written path already IS the qn.
        self._project_named_package = (
            not repo_is_package and (repo_path / project_name).is_dir()
        )

        # Python packages under nested source roots (src-layout, monorepo packages,
        # pyproject package-dir remaps) are importable by a name that differs from
        # their repo-relative path, so absolute imports of them cannot resolve by the
        # import-name == path assumption. Discover the name -> dotted-path map once
        # so those imports resolve first-party.
        py_source_roots = discover_python_source_roots(repo_path)

        @lru_cache(maxsize=4096)
        def _map_py_source_root_cached(module_name: str) -> str | None:
            return resolve_via_source_roots(repo_path, py_source_roots, module_name)

        self._map_py_source_root = _map_py_source_root_cached

        @lru_cache(maxsize=4096)
        def _is_local_module_cached(module_name: str) -> bool:
            # When the repo root is itself a package, its children are importable
            # only under the package name (project_name.child), never as bare
            # top-level names, so a bare top-level import resolves externally.
            if repo_is_package:
                return module_name == project_name
            # A top-level stub with no `.py` is a local module too: it is what
            # a compiled extension's definitions are indexed from (#2445).
            return (repo_path / module_name).is_dir() or any(
                (repo_path / f"{module_name}{ext}").is_file()
                for ext in cs.PY_EXTENSIONS
            )

        self._is_local_module_cached = _is_local_module_cached

    def parse_imports(self, captures: dict[str, list[Node]], module_qn: str) -> None:
        all_imports = captures.get(cs.CAPTURE_IMPORT, []) + captures.get(
            cs.CAPTURE_IMPORT_FROM, []
        )
        for import_node in all_imports:
            before = dict(self.import_mapping[module_qn])
            if import_node.type == cs.TS_PY_IMPORT_STATEMENT:
                self._handle_python_import_statement(import_node, module_qn)
            elif import_node.type == cs.TS_PY_IMPORT_FROM_STATEMENT:
                self._handle_python_import_from_statement(import_node, module_qn)
            if _is_conditional_import_node(import_node):
                new_names = self.import_mapping[module_qn].keys() - before.keys()
                if new_names:
                    self.conditional_imports.setdefault(module_qn, set()).update(
                        new_names
                    )
            self._record_python_rebinds(module_qn, before)

    def _record_python_rebinds(self, module_qn: str, before: dict[str, str]) -> None:
        mapping = self.import_mapping[module_qn]
        for name, previous in before.items():
            current = mapping.get(name)
            if current is not None and current != previous:
                self.python_import_rebinds.setdefault(module_qn, {}).setdefault(
                    name, {previous}
                ).add(current)

    def _handle_python_import_statement(
        self, import_node: Node, module_qn: str
    ) -> None:
        for child in import_node.named_children:
            match child.type:
                case cs.TS_DOTTED_NAME:
                    self._handle_dotted_name_import(child, module_qn)
                case cs.TS_ALIASED_IMPORT:
                    self._handle_aliased_import(child, module_qn)

    def _handle_dotted_name_import(self, child: Node, module_qn: str) -> None:
        module_name = safe_decode_text(child) or ""
        local_name = module_name.split(cs.SEPARATOR_DOT)[0]
        full_name = self._resolve_import_full_name(module_name, local_name)
        self.import_mapping[module_qn][local_name] = full_name
        self._host._record_import_site(module_qn, local_name, child.parent)
        logger.debug(ls.IMP_IMPORT, local=local_name, full=full_name)

    def _handle_aliased_import(self, child: Node, module_qn: str) -> None:
        module_name_node = child.child_by_field_name(cs.FIELD_NAME)
        alias_node = child.child_by_field_name(cs.FIELD_ALIAS)
        if not module_name_node or not alias_node:
            return

        module_name = safe_decode_text(module_name_node)
        alias = safe_decode_text(alias_node)
        if not module_name or not alias:
            return

        top_level = module_name.split(cs.SEPARATOR_DOT)[0]
        full_name = self._resolve_import_full_name(module_name, top_level)
        self.import_mapping[module_qn][alias] = full_name
        self._host._record_import_site(module_qn, alias, child.parent)
        logger.debug(ls.IMP_ALIASED_IMPORT, alias=alias, full=full_name)

    def _resolve_import_full_name(self, module_name: str, top_level: str) -> str:
        if module_name == self.project_name or module_name.startswith(
            self.project_name + cs.SEPARATOR_DOT
        ):
            if self._project_named_package:
                return f"{self.project_name}{cs.SEPARATOR_DOT}{module_name}"
            return module_name
        if self._is_local_module(top_level):
            return f"{self.project_name}{cs.SEPARATOR_DOT}{module_name}"
        if mapped := self._map_py_source_root(module_name):
            return f"{self.project_name}{cs.SEPARATOR_DOT}{mapped}"
        return module_name

    def _is_local_module(self, module_name: str) -> bool:
        return self._is_local_module_cached(module_name)

    def _handle_python_import_from_statement(
        self, import_node: Node, module_qn: str
    ) -> None:
        module_name = self._extract_python_from_module_name(import_node, module_qn)
        if not module_name:
            return

        imported_items = self._extract_python_imported_items(import_node)
        is_wildcard = any(
            child.type == cs.TS_WILDCARD_IMPORT for child in import_node.children
        )

        if not imported_items and not is_wildcard:
            return

        self._register_python_from_imports(
            module_qn, module_name, imported_items, is_wildcard, import_node
        )

    def _extract_python_from_module_name(
        self, import_node: Node, module_qn: str
    ) -> str | None:
        module_name_node = import_node.child_by_field_name(cs.FIELD_MODULE_NAME)
        if not module_name_node:
            return None

        if module_name_node.type == cs.TS_DOTTED_NAME:
            # A written absolute path resolves through the same collision-aware
            # mapping as plain imports; a relative import is already project-prefixed
            # by construction and must NOT re-enter it (a second prefixing would
            # double the project segment).
            if written := safe_decode_text(module_name_node):
                return self._resolve_python_base_module(written)
            return None
        if module_name_node.type == cs.TS_RELATIVE_IMPORT:
            return self._resolve_relative_import(module_name_node, module_qn)
        return None

    def _extract_python_imported_items(
        self, import_node: Node
    ) -> list[tuple[str, str]]:
        imported_items: list[tuple[str, str]] = []

        for name_node in import_node.children_by_field_name(cs.FIELD_NAME):
            if item := self._extract_single_python_import(name_node):
                imported_items.append(item)

        return imported_items

    def _extract_single_python_import(self, name_node: Node) -> tuple[str, str] | None:
        if name_node.type == cs.TS_DOTTED_NAME:
            if name := safe_decode_text(name_node):
                return (name, name)
        elif name_node.type == cs.TS_ALIASED_IMPORT:
            original_node = name_node.child_by_field_name(cs.FIELD_NAME)
            alias_node = name_node.child_by_field_name(cs.FIELD_ALIAS)
            if original_node and alias_node:
                original = safe_decode_text(original_node)
                alias = safe_decode_text(alias_node)
                if original and alias:
                    return (alias, original)
        return None

    def _resolve_python_base_module(self, module_name: str) -> str:
        # The old `startswith(project_name)` as-is shortcut is subsumed by
        # _resolve_import_full_name's first branch, which also handles the
        # project-named-package collision.
        top_level = module_name.split(cs.SEPARATOR_DOT, maxsplit=1)[0]
        return self._resolve_import_full_name(module_name, top_level)

    def _register_python_from_imports(
        self,
        module_qn: str,
        base_module: str,
        imported_items: list[tuple[str, str]],
        is_wildcard: bool,
        import_node: Node | None = None,
    ) -> None:
        if is_wildcard:
            wildcard_key = f"*{base_module}"
            self.import_mapping[module_qn][wildcard_key] = base_module
            self._host._record_import_site(
                module_qn, wildcard_key, import_node, cs.IMPORTED_NAME_WILDCARD
            )
            logger.debug(ls.IMP_WILDCARD_IMPORT, module=base_module)
            return

        for local_name, original_name in imported_items:
            full_name = f"{base_module}{cs.SEPARATOR_DOT}{original_name}"
            self.import_mapping[module_qn][local_name] = full_name
            self._host._record_import_site(
                module_qn, local_name, import_node, original_name
            )
            logger.debug(ls.IMP_FROM_IMPORT, local=local_name, full=full_name)

    def _is_package_qn(self, module_qn: str) -> bool:
        prefix = self.project_name + cs.SEPARATOR_DOT
        if not module_qn.startswith(prefix):
            return False
        rel = module_qn[len(prefix) :].replace(cs.SEPARATOR_DOT, cs.SEPARATOR_SLASH)
        # A stub-only package's `__init__.pyi` names the package too (#2445).
        return any(
            (self.repo_path / rel / init).is_file() for init in cs.PY_PACKAGE_INIT_FILES
        )

    def _resolve_relative_import(self, relative_node: Node, module_qn: str) -> str:
        # Relative imports are always internal; resolve to the full project-prefixed
        # qn so resolution does not depend on bare-name locality checks (which treat
        # package children as external).
        module_parts = module_qn.split(cs.SEPARATOR_DOT)

        dots = 0
        module_name = ""

        for child in relative_node.children:
            if child.type == cs.TS_IMPORT_PREFIX:
                if decoded_text := safe_decode_text(child):
                    dots = len(decoded_text)
            elif child.type == cs.TS_DOTTED_NAME:
                if decoded_name := safe_decode_text(child):
                    module_name = decoded_name

        # A package's qn already IS the package, so `from .` inside an __init__.py
        # drops one fewer level than inside a regular module.
        drop = dots - 1 if self._is_package_qn(module_qn) else dots
        keep = max(len(module_parts) - drop, 0)
        target_parts = module_parts[:keep]

        if module_name:
            target_parts.extend(module_name.split(cs.SEPARATOR_DOT))

        # A relative climb that lands at the project root (`from . import x` in a
        # top-level module) leaves no parts; resolve it to the project root so the
        # import is not silently dropped.
        if not target_parts:
            return self.project_name

        return cs.SEPARATOR_DOT.join(target_parts)
