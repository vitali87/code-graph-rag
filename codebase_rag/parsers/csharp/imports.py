"""C#'s import pass: `using` directives into the shared import map and each
module's `using static` scope, and the per-file namespace and identifier scans
that pin a namespace `using` to the modules it actually uses. C# namespaces
need not mirror directories, so only those scans can say a `using` is the
project's own (issue #1347).

`ImportProcessor` owns what every language shares (the import map, the
import sites, the deferred IMPORTS edges and their flush) and hands a C#
file's imports to the `CSharpImporter` it builds."""

from pathlib import Path
from typing import Protocol

from loguru import logger
from tree_sitter import Node

from ... import constants as cs
from ... import logs as ls
from ...node_text import safe_decode_text, safe_decode_with_fallback
from ...types_defs import PropertyDict
from ...utils.source_encoding import grammar_bytes


class CSharpImportHost(Protocol):
    """What the C# importer calls on the import processor that builds it: the
    import-site record and deferred-edge queue every language's imports go
    through."""

    def defer_import_edge(
        self,
        module_qn: str,
        full_name: str,
        language: cs.SupportedLanguage,
        site: PropertyDict | None = None,
    ) -> None: ...

    def _record_import_site(
        self,
        scope_qn: str,
        local_name: str,
        node: Node | None,
        imported_name: str | None = None,
        owner_qn: str | None = None,
    ) -> PropertyDict | None: ...


class CSharpImporter:
    __slots__ = (
        "_host",
        "repo_path",
        "project_name",
        "import_mapping",
        "csharp_static_imports",
        "csharp_global_static_imports",
        "_inferred_module_imports",
        "csharp_module_namespaces",
        "_csharp_module_identifiers",
    )

    def __init__(
        self,
        host: CSharpImportHost,
        repo_path: Path,
        project_name: str,
        import_mapping: dict[str, dict[str, str]],
    ) -> None:
        self._host = host
        self.repo_path = repo_path
        self.project_name = project_name
        # The processor's own map, shared rather than copied: a `using` binds
        # its name in the map every later pass reads.
        self.import_mapping = import_mapping
        # `using static N.T;` brings T's MEMBERS into bare-call scope, which
        # import_mapping cannot express: it maps the TYPE name (T -> N.T), so a
        # bare `Member()` has no entry to resolve through. Module qn -> the set
        # of (enclosing namespace, statically imported type path), probed as an
        # extra bare-call scope (issue #2005). The namespace is "" for a
        # file-level directive; one inside `namespace N { ... }` scopes only N.
        self.csharp_static_imports: dict[str, set[tuple[str, str]]] = {}
        # `global using static N.T;` is in scope in EVERY file of the
        # compilation. Keyed by the DECLARING module, so a re-parse or a
        # removal of that file drops exactly its own directives.
        self.csharp_global_static_imports: dict[str, set[str]] = {}
        self._inferred_module_imports: dict[str, set[str]] = {}
        self.csharp_module_namespaces: dict[str, dict[str, set[str]]] = {}
        self._csharp_module_identifiers: dict[str, frozenset[str]] = {}

    @staticmethod
    def _csharp_using_target(import_node: Node) -> tuple[str, Node | None] | None:
        # `using Alias = Target;` marks the alias with a `name` field; the
        # imported path is then the OTHER name node. Plain/static/global
        # forms have no `name` field, so the sole name node is the path.
        name_types = (cs.TS_CSHARP_QUALIFIED_NAME, cs.TS_CSHARP_IDENTIFIER)
        alias_node = import_node.child_by_field_name(cs.TS_CSHARP_FIELD_NAME)
        target = None
        for child in import_node.children:
            if child.type in name_types and child != alias_node:
                target = child
        if target is None:
            return None
        imported_path = safe_decode_with_fallback(target)
        if not imported_path:
            return None
        return imported_path, alias_node

    def _record_csharp_static_import(
        self,
        import_node: Node,
        module_qn: str,
        imported_path: str,
        alias_node: Node | None,
        global_only: bool = False,
    ) -> None:
        # `using static` is marked by a literal `static` child; an alias
        # form (`using static X = N.T;`) is not a member import.
        modifiers = {child.type for child in import_node.children}
        if alias_node is not None or cs.TS_CSHARP_STATIC not in modifiers:
            return
        is_global = cs.TS_CSHARP_GLOBAL in modifiers
        if global_only and not is_global:
            return
        if is_global:
            self.csharp_global_static_imports.setdefault(module_qn, set()).add(
                imported_path
            )
        else:
            self.csharp_static_imports.setdefault(module_qn, set()).add(
                (self._csharp_enclosing_namespace(import_node), imported_path)
            )

    @staticmethod
    def _csharp_enclosing_namespace(node: Node) -> str:
        # The dotted name of the `namespace N { ... }` blocks around `node`,
        # outermost first. A file-scoped `namespace N;` covers the whole file,
        # so a directive under it is file-level and is not counted.
        parts: list[str] = []
        parent = node.parent
        while parent is not None:
            if parent.type == cs.TS_CSHARP_NAMESPACE_DECLARATION:
                name = safe_decode_with_fallback(
                    parent.child_by_field_name(cs.TS_CSHARP_FIELD_NAME)
                )
                if name:
                    parts.append(name)
            parent = parent.parent
        return cs.SEPARATOR_DOT.join(reversed(parts))

    def _collect_csharp_global_static_imports(self, root: Node, module_qn: str) -> None:
        # A `global using` must precede every other member of the file, so
        # the directives are direct children of the compilation unit.
        for import_node in root.children:
            if import_node.type != cs.TS_CSHARP_USING_DIRECTIVE:
                continue
            if parsed := self._csharp_using_target(import_node):
                imported_path, alias_node = parsed
                self._record_csharp_static_import(
                    import_node, module_qn, imported_path, alias_node, global_only=True
                )

    def parse_imports(
        self,
        captures: dict[str, list[Node]],
        module_qn: str,
        root_node: Node | None = None,
    ) -> None:
        for import_node in captures.get(cs.CAPTURE_IMPORT, []):
            if import_node.type != cs.TS_CSHARP_USING_DIRECTIVE:
                continue
            parsed = self._csharp_using_target(import_node)
            if parsed is None:
                continue
            imported_path, alias_node = parsed
            if alias_node is not None and alias_node.text:
                local_name = safe_decode_with_fallback(alias_node)
            else:
                local_name = imported_path.split(cs.SEPARATOR_DOT)[-1]
            self.import_mapping[module_qn][local_name] = imported_path
            self._record_csharp_static_import(
                import_node, module_qn, imported_path, alias_node
            )
            self._host._record_import_site(
                module_qn,
                local_name,
                import_node,
                imported_path.split(cs.SEPARATOR_DOT)[-1],
            )
            logger.debug(ls.IMP_CSHARP, name=local_name, path=imported_path)

    def scan_module(self, module_qn: str, root_node: Node) -> None:
        self.csharp_module_namespaces[module_qn] = self._collect_csharp_namespaces(
            root_node
        )
        self._csharp_module_identifiers[module_qn] = self._collect_csharp_identifiers(
            root_node
        )

    _CSHARP_TYPE_DECLARATIONS = (
        cs.TS_CSHARP_CLASS_DECLARATION,
        cs.TS_CSHARP_STRUCT_DECLARATION,
        cs.TS_CSHARP_RECORD_DECLARATION,
        cs.TS_CSHARP_INTERFACE_DECLARATION,
        cs.TS_CSHARP_ENUM_DECLARATION,
        cs.TS_CSHARP_DELEGATE_DECLARATION,
    )

    def _csharp_declared_type_names(self, container: Node) -> set[str]:
        names: set[str] = set()
        for child in container.named_children:
            if child.type not in self._CSHARP_TYPE_DECLARATIONS:
                continue
            name_node = child.child_by_field_name(cs.TS_CSHARP_FIELD_NAME)
            if name_node is not None and (name := safe_decode_with_fallback(name_node)):
                names.add(name)
        return names

    def _walk_csharp_namespaces(
        self, container: Node, prefix: str, found: dict[str, set[str]]
    ) -> None:
        for child in container.named_children:
            if child.type not in (
                cs.TS_CSHARP_NAMESPACE_DECLARATION,
                cs.TS_CSHARP_FILE_SCOPED_NAMESPACE_DECLARATION,
            ):
                continue
            name_node = child.child_by_field_name(cs.TS_CSHARP_FIELD_NAME)
            if name_node is None or not (name := safe_decode_with_fallback(name_node)):
                continue
            full = f"{prefix}{cs.SEPARATOR_DOT}{name}" if prefix else name
            if child.type == cs.TS_CSHARP_FILE_SCOPED_NAMESPACE_DECLARATION:
                found.setdefault(full, set()).update(
                    self._csharp_declared_type_names(container)
                )
                continue
            body = child.child_by_field_name(cs.FIELD_BODY)
            if body is None:
                found.setdefault(full, set())
                continue
            found.setdefault(full, set()).update(self._csharp_declared_type_names(body))
            self._walk_csharp_namespaces(body, full, found)

    def _collect_csharp_namespaces(self, root_node: Node) -> dict[str, set[str]]:
        # Namespaces appear only at the top level and inside namespace bodies,
        # so the walk prunes into nothing else; nesting composes dotted names.
        # Each namespace carries the TYPE NAMES it declares in this file: the
        # name-level evidence that pins a type-only `using` to the module.
        found: dict[str, set[str]] = {}
        self._walk_csharp_namespaces(root_node, "", found)
        return found

    @staticmethod
    def _csharp_field_of(node: Node) -> str | None:
        parent = node.parent
        if parent is None:
            return None
        for index, child in enumerate(parent.children):
            if child.id == node.id:
                return parent.field_name_for_child(index)
        return None

    def _collect_csharp_identifiers(self, root_node: Node) -> frozenset[str]:
        # Three tiers of identifier occurrences: TYPE-POSITION mentions are
        # definitive references (a namesake property, `Widget Widget`, is
        # idiomatic C# and must keep its evidence); NAME-POSITION identifiers
        # declare rather than reference; everything else is a plain mention
        # whose spelling is withdrawn when the file also declares it, because
        # such a mention binds to the local declaration.
        type_positions: set[str] = set()
        mentions: set[str] = set()
        declared: set[str] = set()
        stack = [root_node]
        while stack:
            node = stack.pop()
            stack.extend(node.children)
            if node.type != cs.TS_CSHARP_IDENTIFIER:
                continue
            text = safe_decode_text(node)
            if not text:
                continue
            field = self._csharp_field_of(node)
            parent_type = node.parent.type if node.parent is not None else None
            if field == cs.TS_CSHARP_FIELD_TYPE or parent_type in (
                cs.TS_CSHARP_BASE_LIST,
                cs.TS_CSHARP_TYPE_ARGUMENT_LIST,
            ):
                type_positions.add(text)
            elif field == cs.TS_CSHARP_FIELD_NAME:
                declared.add(text)
            else:
                mentions.add(text)
        return frozenset(type_positions | (mentions - declared))

    def drop_csharp_module_import_state(self, module_qn: str) -> None:
        # A deleted C# file must stop declaring its namespaces, contributing
        # identifier evidence, and queueing its using entries, or the requeue
        # would rebuild edges a clean rebuild of the same tree never emits.
        self.csharp_module_namespaces.pop(module_qn, None)
        self._csharp_module_identifiers.pop(module_qn, None)
        self._inferred_module_imports.pop(module_qn, None)
        self.import_mapping.pop(module_qn, None)
        self.csharp_static_imports.pop(module_qn, None)
        self.csharp_global_static_imports.pop(module_qn, None)

    def requeue_csharp_import_edges(self) -> None:
        """Re-queue every parsed C# module's using entries for a fresh flush.

        A watched edit to a PROVIDER file deletes and recreates its Module
        node, severing edges from unchanged importers, whose deferred entries
        were consumed by an earlier flush. The persistent import mapping still
        holds their using directives, and re-emission is idempotent MERGEs
        (issue #1347).
        """
        for module_qn in self.csharp_module_namespaces:
            for imported_path in self.import_mapping.get(module_qn, {}).values():
                self._host.defer_import_edge(
                    module_qn, imported_path, cs.SupportedLanguage.CSHARP
                )

    def csharp_namespace_modules(
        self, known_module_qns: set[str]
    ) -> dict[str, dict[str, set[str]]]:
        # namespace -> {declaring module qn -> type names it declares there}.
        # C# namespaces need not mirror directory names, so path-based
        # guessing cannot answer "is this internal"; the per-file scans can.
        namespace_modules: dict[str, dict[str, set[str]]] = {}
        for module_qn, namespaces in self.csharp_module_namespaces.items():
            if module_qn not in known_module_qns:
                continue
            for namespace, declared_types in namespaces.items():
                namespace_modules.setdefault(namespace, {})[module_qn] = declared_types
        return namespace_modules

    def recover_unparsed_csharp_state(self, known_module_paths: dict[str, str]) -> None:
        """Rebuild the per-file C# scans for modules this run did not parse.

        An incremental run re-parses only CHANGED files, so an unchanged
        file's declared namespaces, identifiers and `global using static`
        directives are recovered here from a targeted parse of its source.
        Run before Pass 3: a global static import in an unchanged file scopes
        the bare calls of every re-parsed one (PR #2036).
        """
        self._recover_unparsed_csharp_namespaces(known_module_paths)

    def _recover_unparsed_csharp_namespaces(
        self, known_module_paths: dict[str, str]
    ) -> None:
        pending = [
            (module_qn, path)
            for module_qn, path in known_module_paths.items()
            if module_qn not in self.csharp_module_namespaces
            and str(path).endswith(cs.EXT_CS)
        ]
        if not pending:
            return
        from ...parser_loader import load_parsers

        parsers, _queries = load_parsers()
        if cs.SupportedLanguage.CSHARP not in parsers:
            return
        parser = parsers[cs.SupportedLanguage.CSHARP]
        for module_qn, path in pending:
            try:
                source = Path(path).read_bytes()
            except OSError:
                self.csharp_module_namespaces[module_qn] = {}
                self._csharp_module_identifiers[module_qn] = frozenset()
                continue
            # Parsed as the indexer parses it: a UTF-16 file's raw bytes read
            # as no declarations at all (#3153).
            tree = parser.parse(
                grammar_bytes(source, cs.SupportedLanguage.CSHARP, Path(path))
            )
            self.csharp_module_namespaces[module_qn] = self._collect_csharp_namespaces(
                tree.root_node
            )
            self._csharp_module_identifiers[module_qn] = (
                self._collect_csharp_identifiers(tree.root_node)
            )
            self._collect_csharp_global_static_imports(tree.root_node, module_qn)

    def record_resolved_cross_module_use(
        self, importing_module_qn: str, target_module_qn: str
    ) -> None:
        """Remember that a resolved call/reference/inherit crossed modules.

        A C# `using` names a NAMESPACE, not a file, so the import edge can
        only be pinned to real Module nodes by observing which modules the
        importing file actually resolved entities from (issue #1347).
        """
        if importing_module_qn == target_module_qn:
            return
        self._inferred_module_imports.setdefault(importing_module_qn, set()).add(
            target_module_qn
        )

    def drop_resolved_cross_module_uses(self, module_qn: str) -> None:
        # A watch-mode re-parse must not carry references the edited file no
        # longer makes (issue #1347).
        self._inferred_module_imports.pop(module_qn, None)

    def using_targets(
        self, module_qn: str, declaring: dict[str, set[str]]
    ) -> list[str]:
        """The modules declaring a `using`'s namespace that `module_qn` uses,
        in the order their IMPORTS edges are emitted: a resolved
        call/reference/inherit crossed into the module, or the file mentions
        a type name the module declares under that namespace (issue #1347)."""
        inferred = self._inferred_module_imports.get(module_qn, set())
        # Names the importer DECLARES itself are not evidence: an unqualified
        # mention binds to the local type, not the imported namespace's.
        own_types: set[str] = set()
        for own_declared in self.csharp_module_namespaces.get(module_qn, {}).values():
            own_types.update(own_declared)
        used = self._csharp_module_identifiers.get(module_qn, frozenset()) - own_types
        targets = {
            target
            for target, declared_types in declaring.items()
            if target != module_qn
            and (target in inferred or not used.isdisjoint(declared_types))
        }
        return sorted(targets)
