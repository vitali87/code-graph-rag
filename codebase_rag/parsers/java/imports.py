"""Java's import pass: `import` and `import static` declarations into the
shared import map and each module's static-import list, and the source-root
layout (the build tool's roots and discovered annotation-processor output)
whose prefix a local import's qn carries.

`ImportProcessor` owns what every language shares (the import map, the
import sites, the deferred IMPORTS edges and their flush) and hands a Java
file's imports to the `JavaImporter` it builds."""

from functools import lru_cache
from pathlib import Path
from typing import Protocol

from loguru import logger
from tree_sitter import Node

from ... import constants as cs
from ... import logs as ls
from ...node_text import safe_decode_with_fallback
from ...types_defs import JavaStaticImport, PropertyDict


def _java_import_shape(import_node: Node) -> tuple[str | None, bool, bool]:
    # (imported path, is static, is wildcard) of a Java import declaration.
    imported_path = None
    is_static = False
    is_wildcard = False
    for child in import_node.children:
        if child.type == cs.TS_STATIC:
            is_static = True
        elif child.type == cs.TS_SCOPED_IDENTIFIER:
            imported_path = safe_decode_with_fallback(child)
        elif child.type == cs.TS_ASTERISK:
            is_wildcard = True
    return imported_path, is_static, is_wildcard


class JavaImportHost(Protocol):
    """What the Java importer calls on the import processor that builds it:
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


class JavaImporter:
    __slots__ = (
        "_host",
        "repo_path",
        "project_name",
        "import_mapping",
        "java_static_imports",
        "java_generated_roots",
        "_is_local_java_import_cached",
        "_java_source_root_prefix_cached",
    )

    def __init__(
        self,
        host: JavaImportHost,
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
        # Java `import static` brings a class's static MEMBERS into bare-call
        # scope. import_mapping keys `import static C.m;` like a type import,
        # and files `import static C.*;` (members of CLASS C) under the same
        # `*path` key as `import p.*;` (types of PACKAGE p), so a bare `m()`
        # cannot tell from it whether any static import names it. Module qn ->
        # its static imports in source order (issue #2544).
        self.java_static_imports: dict[str, list[JavaStaticImport]] = {}
        # Discovered annotation-processor roots (issue #1140): mutated via
        # set_java_generated_roots, which clears the probe cache below.
        self.java_generated_roots: tuple[tuple[str, ...], ...] = ()

        @lru_cache(maxsize=4096)
        def _java_source_root_prefix_cached(import_path: str) -> str | None:
            # The registered Module qns carry the build-tool source root
            # (src.main.java.), so a local import's qn must too; a flat
            # layout keeps the empty prefix and is unchanged (issue #1121).
            # Resolution probes the COMPLETE import target under each root:
            # an external import sharing a local top-level segment
            # (com.fasterxml under a repo with src/main/java/com) stays
            # external, and a test-only class binds to src/test/java even
            # when src/main/java also contains the top-level package. The
            # target may be a package dir or any class-file ancestor: a
            # static member (Utility.run) sits one segment past its file and
            # a nested-class member (Outer.Inner.CONSTANT) two or more, so
            # every ancestor is probed.
            parts = import_path.split(cs.SEPARATOR_DOT)
            for root in (
                (),
                *cs.JAVA_MAVEN_SOURCE_ROOTS,
                *self.java_generated_roots,
            ):
                base = repo_path.joinpath(*root)
                if base.joinpath(*parts).is_dir() or any(
                    base.joinpath(
                        *parts[: end - 1], f"{parts[end - 1]}{cs.EXT_JAVA}"
                    ).is_file()
                    for end in range(len(parts), 0, -1)
                ):
                    return (
                        cs.SEPARATOR_DOT.join(root) + cs.SEPARATOR_DOT if root else ""
                    )
            return None

        def _is_local_java_import_cached(import_path: str) -> bool:
            return _java_source_root_prefix_cached(import_path) is not None

        self._is_local_java_import_cached = _is_local_java_import_cached
        self._java_source_root_prefix_cached = _java_source_root_prefix_cached

    def parse_imports(
        self,
        captures: dict[str, list[Node]],
        module_qn: str,
        root_node: Node | None = None,
    ) -> None:
        for import_node in captures.get(cs.CAPTURE_IMPORT, []):
            if import_node.type != cs.TS_IMPORT_DECLARATION:
                continue
            imported_path, is_static, is_wildcard = _java_import_shape(import_node)
            if imported_path:
                self._record_java_import(
                    import_node,
                    module_qn,
                    self._resolve_java_import_path(imported_path),
                    is_static,
                    is_wildcard,
                )

    def _record_java_import(
        self,
        import_node: Node,
        module_qn: str,
        resolved_path: str,
        is_static: bool,
        is_wildcard: bool,
    ) -> None:
        if is_static:
            self._record_java_static_import(module_qn, resolved_path, is_wildcard)
        if is_wildcard:
            logger.debug(ls.IMP_JAVA_WILDCARD, path=resolved_path)
            self.import_mapping[module_qn][f"*{resolved_path}"] = resolved_path
            self._host._record_import_site(
                module_qn,
                f"*{resolved_path}",
                import_node,
                cs.IMPORTED_NAME_WILDCARD,
            )
            return
        imported_name = resolved_path.rsplit(cs.SEPARATOR_DOT, maxsplit=1)[-1]
        self.import_mapping[module_qn][imported_name] = resolved_path
        self._host._record_import_site(
            module_qn, imported_name, import_node, imported_name
        )
        logger.debug(
            ls.IMP_JAVA_STATIC if is_static else ls.IMP_JAVA_IMPORT,
            name=imported_name,
            path=resolved_path,
        )

    def _record_java_static_import(
        self, module_qn: str, resolved_path: str, is_wildcard: bool
    ) -> None:
        # The on-demand form names the class itself; the single-member form
        # names one member past it.
        if is_wildcard:
            entry = JavaStaticImport(class_path=resolved_path, member=None)
        else:
            class_path, _, member = resolved_path.rpartition(cs.SEPARATOR_DOT)
            if not class_path:
                return
            entry = JavaStaticImport(class_path=class_path, member=member)
        self.java_static_imports.setdefault(module_qn, []).append(entry)

    def set_java_generated_roots(self, roots: list[tuple[str, ...]]) -> None:
        # The probe closure reads the attribute late-bound, but its lru cache
        # may hold pre-discovery misses; clear so generated imports resolve.
        self.java_generated_roots = tuple(roots)
        self._java_source_root_prefix_cached.cache_clear()

    def reset_java_path_caches(self) -> None:
        # Same contract as reset_rust_path_caches: the filesystem may have
        # gained or lost files since the layout decisions were cached.
        self._java_source_root_prefix_cached.cache_clear()

    def _is_local_java_import(self, import_path: str) -> bool:
        return self._is_local_java_import_cached(import_path)

    def _resolve_java_import_path(self, import_path: str) -> str:
        prefix = self._java_source_root_prefix_cached(import_path)
        if prefix is not None:
            return f"{self.project_name}{cs.SEPARATOR_DOT}{prefix}{import_path}"
        return import_path

    def java_owning_module_qn(self, full_name: str) -> str:
        # A static or nested-class import carries symbol segments past the
        # class file (Utility.run, Outer.Inner.CONSTANT). The import map keeps
        # the full path for call resolution, but the IMPORTS edge must land on
        # the owning file-level Module, so truncate to the deepest ancestor
        # whose .java file exists. Package (wildcard) and file-level targets
        # come back unchanged.
        project_prefix = self.project_name + cs.SEPARATOR_DOT
        segments = full_name[len(project_prefix) :].split(cs.SEPARATOR_DOT)
        for end in range(len(segments), 0, -1):
            candidate = segments[:end]
            if self.repo_path.joinpath(*candidate).is_dir():
                break
            if self.repo_path.joinpath(
                *candidate[:-1], f"{candidate[-1]}{cs.EXT_JAVA}"
            ).is_file():
                return project_prefix + cs.SEPARATOR_DOT.join(candidate)
        return full_name
