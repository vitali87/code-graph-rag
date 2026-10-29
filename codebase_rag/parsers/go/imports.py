"""Go's import pass: import specs into the shared import map, and the go.mod
module directives that rewrite a local import path to its package's qn.

`ImportProcessor` owns what every language shares (the import map, the
import sites, the deferred IMPORTS edges and their flush) and hands a Go
file's imports to the `GoImporter` it builds."""

from functools import lru_cache
from pathlib import Path
from typing import Protocol

from loguru import logger
from tree_sitter import Node

from ... import constants as cs
from ... import logs as ls
from ...node_text import safe_decode_with_fallback
from ...types_defs import PropertyDict
from .module_paths import discover_go_module_paths, resolve_go_import_path


class GoImportHost(Protocol):
    """What the Go importer calls on the import processor that builds it: the
    import-site record every language's bindings go through, whose sites ride
    on the IMPORTS edges the processor's flush emits."""

    def _record_import_site(
        self,
        scope_qn: str,
        local_name: str,
        node: Node | None,
        imported_name: str | None = None,
        owner_qn: str | None = None,
    ) -> PropertyDict | None: ...


class GoImporter:
    __slots__ = (
        "_host",
        "repo_path",
        "project_name",
        "import_mapping",
        "_map_go_import_path",
    )

    def __init__(
        self,
        host: GoImportHost,
        repo_path: Path,
        project_name: str,
        import_mapping: dict[str, dict[str, str]],
    ) -> None:
        self._host = host
        self.repo_path = repo_path
        self.project_name = project_name
        # The processor's own map, shared rather than copied: an import binds
        # its package name in the map every later pass reads.
        self.import_mapping = import_mapping

        # Go import paths are module-path-prefixed (github.com/acme/tool/pkg), never
        # repo-relative, so no local Go import resolves by the name == path
        # assumption. Map each go.mod module directive to its directory once so local
        # imports rewrite to project-prefixed qns and unmapped (external) paths stay
        # recognisably slash-separated.
        go_module_paths = discover_go_module_paths(repo_path)

        @lru_cache(maxsize=4096)
        def _map_go_import_path_cached(import_path: str) -> str | None:
            return resolve_go_import_path(go_module_paths, import_path)

        self._map_go_import_path = _map_go_import_path_cached

    def parse_imports(
        self,
        captures: dict[str, list[Node]],
        module_qn: str,
        root_node: Node | None = None,
    ) -> None:
        for import_node in captures.get(cs.CAPTURE_IMPORT, []):
            if import_node.type == cs.TS_GO_IMPORT_DECLARATION:
                self._parse_go_import_declaration(import_node, module_qn)

    def _parse_go_import_declaration(self, import_node: Node, module_qn: str) -> None:
        for child in import_node.children:
            if child.type == cs.TS_IMPORT_SPEC:
                self._parse_go_import_spec(child, module_qn)
            elif child.type == cs.TS_IMPORT_SPEC_LIST:
                for grandchild in child.children:
                    if grandchild.type == cs.TS_IMPORT_SPEC:
                        self._parse_go_import_spec(grandchild, module_qn)

    def _parse_go_import_spec(self, spec_node: Node, module_qn: str) -> None:
        alias_name = None
        import_path = None
        is_dot_import = False

        for child in spec_node.children:
            if child.type == cs.TS_PACKAGE_IDENTIFIER:
                alias_name = safe_decode_with_fallback(child)
            elif child.type == cs.TS_GO_DOT:
                is_dot_import = True
            elif child.type == cs.TS_INTERPRETED_STRING_LITERAL:
                import_path = safe_decode_with_fallback(child).strip('"')

        if import_path:
            package_name = alias_name or import_path.split(cs.SEPARATOR_SLASH)[-1]
            # A path under a local go.mod module rewrites to the package dir's
            # project qn ('' remainder = the module root package itself), so both the
            # IMPORTS edge and call resolution bind first-party. External paths stay
            # raw.
            if (mapped := self._map_go_import_path(import_path)) is not None:
                import_path = (
                    f"{self.project_name}{cs.SEPARATOR_DOT}{mapped}"
                    if mapped
                    else self.project_name
                )
            if is_dot_import:
                # `import . "fmt"` binds the package's exported names, not the
                # package identifier; a `.`-prefixed sentinel key (no identifier
                # can contain a dot) lets bare-callee lookups re-qualify.
                dot_key = f"{cs.SEPARATOR_DOT}{package_name}"
                self.import_mapping[module_qn][dot_key] = import_path
                self._host._record_import_site(module_qn, dot_key, spec_node)
            else:
                self.import_mapping[module_qn][package_name] = import_path
                self._host._record_import_site(module_qn, package_name, spec_node)
            logger.debug(ls.IMP_GO, package=package_name, path=import_path)
