"""PHP's import pass: `use` declarations (classes, functions, constants) and
include/require expressions into the shared import map, and the `namespace`
each module declares, which an import path is matched against since PHP
namespaces need not mirror directories (issue #1185).

`ImportProcessor` owns what every language shares (the import map, the
import sites, the deferred IMPORTS edges and their flush) and hands a PHP
file's imports to the `PhpImporter` it builds."""

from tree_sitter import Node

from ... import constants as cs
from ...node_text import safe_decode_with_fallback
from ..import_host import ImporterContext, ImportSiteHost
from .namespaces import declared_namespace


def _php_use_clause_binding(clause: Node) -> tuple[str, str] | None:
    """Return (imported dotted path, local name) for one `use` clause."""
    qn_node = next(
        (c for c in clause.named_children if c.type == cs.TS_PHP_QUALIFIED_NAME),
        None,
    )
    if not qn_node:
        return None
    imported_path = safe_decode_with_fallback(qn_node)
    if not imported_path:
        return None
    imported_path = imported_path.replace("\\", cs.SEPARATOR_DOT)
    alias_node = clause.child_by_field_name(cs.FIELD_ALIAS)
    if alias_node and alias_node.text:
        return imported_path, safe_decode_with_fallback(alias_node)
    parts = imported_path.split(cs.SEPARATOR_DOT)
    return imported_path, parts[-1] if parts else imported_path


class PhpImporter:
    __slots__ = (
        "_host",
        "repo_path",
        "project_name",
        "import_mapping",
        "php_function_imports",
        "php_module_namespaces",
    )

    def __init__(
        self,
        host: ImportSiteHost,
        context: ImporterContext,
    ) -> None:
        self._host = host
        self.repo_path = context.repo_path
        self.project_name = context.project_name
        self.import_mapping = context.import_mapping
        # Local names brought in by a PHP `use function A\B\c` import, keyed by
        # module. A PHP namespace path never matches cgr's file-path qn (a global
        # helper declares `namespace Illuminate\Support` from
        # Collections/functions.php), so these must resolve by simple name via the
        # trie rather than being judged external-import and suppressed.
        self.php_function_imports: dict[str, set[str]] = {}
        # The `namespace Vendor\Pkg` a PHP module declares, keyed by module qn,
        # stored dotted (`Vendor.Pkg`) to match how import targets are recorded.
        #
        # PHP namespaces and directory layout are INDEPENDENT: PSR-4 maps a
        # namespace prefix to a directory root, so `Vendor\Pkg\helper` may live
        # at any depth beneath it. Without this map a `use function
        # App\Text\format` import cannot be matched to the file that declares
        # `namespace App\Text`, so the call falls through to the simple-name
        # trie and binds to whichever same-named function it reaches first --
        # a WRONG edge rather than a missing one (issue #1185).
        self.php_module_namespaces: dict[str, str] = {}

    _PHP_INCLUDE_REQUIRE_TYPES = frozenset(
        {
            cs.TS_PHP_INCLUDE_EXPRESSION,
            cs.TS_PHP_INCLUDE_ONCE_EXPRESSION,
            cs.TS_PHP_REQUIRE_EXPRESSION,
            cs.TS_PHP_REQUIRE_ONCE_EXPRESSION,
        }
    )

    def _record_php_namespace(self, root_node: Node, module_qn: str) -> None:
        """Bind this module to the `namespace` it declares, if any.

        Walked from the root rather than read from the imports query, because
        that query captures `namespace_use_declaration` only -- the `use`
        statements -- and never `namespace_definition`. Widening the query
        would change its capture contract for every consumer; this reads the
        one node it needs.

        A file with SEVERAL top-level namespace blocks records NOTHING. PHP
        permits `namespace A { } namespace B { }` in one file, and this map
        answers "which namespace is this module", which such a file has no
        single answer to. An earlier version recorded the first block and
        called that deliberate; review reproduced the consequence (#1484): with
        `App\\First` recorded and both blocks' functions living in the same
        module qn, an import of `App\\First\\helper` resolved to the `helper`
        defined in `App\\Second` -- a confident wrong binding, which is the
        exact failure this whole change exists to remove.

        Recording nothing sends those files to the trie fallback, which is
        where they were before this feature existed. Strictly no worse than
        the status quo, and unlike a first-block guess it never asserts an
        answer it does not have.
        """
        # `namespace { ... }` -- the explicit GLOBAL namespace -- has no name,
        # and binding it to "" would make every unqualified lookup match it.
        if (declared := declared_namespace(root_node)) is not None:
            self.php_module_namespaces[module_qn] = declared

    def parse_imports(
        self,
        captures: dict[str, list[Node]],
        module_qn: str,
        root_node: Node | None = None,
    ) -> None:
        if root_node is not None:
            self._record_php_namespace(root_node, module_qn)
        all_imports = captures.get(cs.CAPTURE_IMPORT, []) + captures.get(
            cs.CAPTURE_IMPORT_FROM, []
        )
        for import_node in all_imports:
            if import_node.type == cs.TS_PHP_NAMESPACE_USE_DECLARATION:
                self._handle_php_use_declaration(import_node, module_qn)
            elif import_node.type in self._PHP_INCLUDE_REQUIRE_TYPES:
                self._handle_php_include_require(import_node, module_qn)

    def _handle_php_use_declaration(self, use_node: Node, module_qn: str) -> None:
        # `use function A\B\c` / `use const A\B\C` carry the modifier on the
        # declaration (older grammar) or inside each clause (current grammar).
        decl_is_function = any(c.type == cs.TS_PHP_FUNCTION for c in use_node.children)
        for child in use_node.named_children:
            if child.type != cs.TS_PHP_NAMESPACE_USE_CLAUSE:
                continue
            binding = _php_use_clause_binding(child)
            if binding is None:
                continue
            imported_path, local_name = binding
            self.import_mapping[module_qn][local_name] = imported_path
            self._host.record_import_site(
                module_qn,
                local_name,
                use_node,
                imported_path.split(cs.SEPARATOR_DOT)[-1],
            )
            if decl_is_function or any(
                c.type == cs.TS_PHP_FUNCTION for c in child.children
            ):
                self.php_function_imports.setdefault(module_qn, set()).add(local_name)

    def _handle_php_include_require(self, node: Node, module_qn: str) -> None:
        for child in node.children:
            if child.type in {"string", "encapsed_string"}:
                raw = safe_decode_with_fallback(child)
                if not raw:
                    continue
                path_str = raw.strip("'\"")
                path_str = path_str.replace("/", cs.SEPARATOR_DOT).replace(
                    "\\", cs.SEPARATOR_DOT
                )
                if path_str.endswith(".php"):
                    path_str = path_str[:-4]
                parts = path_str.split(cs.SEPARATOR_DOT)
                local_name = parts[-1] if parts else path_str
                self.import_mapping[module_qn][local_name] = path_str
                self._host.record_import_site(module_qn, local_name, node, path_str)
                return
