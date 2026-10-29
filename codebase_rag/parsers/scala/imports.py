"""Scala's import pass: import declarations into the shared import map, and
the package index they resolve through once every module is known. Scala
packages need not mirror directories, so an import's written path becomes a
project module only when the `package` clauses say so (issue #2450).

`ImportProcessor` owns what every language shares (the import map, the
import sites, the deferred IMPORTS edges and their flush) and hands a Scala
file's imports to the `ScalaImporter` it builds."""

from pathlib import Path
from typing import Protocol

from tree_sitter import Node

from ... import constants as cs
from ...language_spec import own_extension_module_qn
from ...node_text import safe_decode_with_fallback
from ...services import IngestorProtocol
from ...types_defs import (
    DeferredImportEdge,
    FunctionRegistryTrieProtocol,
    PropertyDict,
    ScalaBlockImports,
    ScalaImportBinding,
    ScalaImportTarget,
    ScalaPackageBlock,
    ScalaPackageIndex,
    ScalaPackageScan,
)
from ...utils.path_utils import base_module_qn, walk_eligible_files
from . import utils as scala_utils

# A module known to declare no Scala package: not a Scala file, or unreadable.
_EMPTY_SCALA_SCAN = ScalaPackageScan(
    packages={}, enclosing=(), mentions=frozenset(), blocks=(), class_bases={}
)


class ScalaImportHost(Protocol):
    """What the Scala importer reads and calls on the import processor that
    builds it: the indexer's walk policy, the ingestor and registry, and the
    import-site record and deferred-edge queue every language's imports go
    through.

    The policy is read here rather than copied in: the factory re-resolves
    `unignore_paths` on processors it has already built
    (`propagate_unignore_paths`), and a stale copy would look for an unscanned
    module's file where the indexer does not (issue #1088)."""

    ingestor: IngestorProtocol | None
    function_registry: FunctionRegistryTrieProtocol | None
    exclude_paths: frozenset[str] | None
    unignore_paths: frozenset[str] | None

    def defer_import_edge(
        self,
        module_qn: str,
        full_name: str,
        language: cs.SupportedLanguage,
        site: PropertyDict | None = None,
    ) -> None: ...

    def drop_deferred_import_edges(
        self, module_qns: set[str], language: cs.SupportedLanguage
    ) -> None: ...

    def _record_import_site(
        self,
        scope_qn: str,
        local_name: str,
        node: Node | None,
        imported_name: str | None = None,
        owner_qn: str | None = None,
    ) -> PropertyDict | None: ...


class ScalaImporter:
    __slots__ = (
        "_host",
        "repo_path",
        "project_name",
        "import_mapping",
        "_import_sites",
        "_scala_scans",
        "_scala_imports",
        "_scala_block_imports",
        "_scala_resolved_edges",
        "_scala_requeue",
        "_scala_import_members",
        "_scala_package_index",
    )

    def __init__(
        self,
        host: ScalaImportHost,
        repo_path: Path,
        project_name: str,
        import_mapping: dict[str, dict[str, str]],
        import_sites: dict[str, dict[str, PropertyDict]],
    ) -> None:
        self._host = host
        self.repo_path = repo_path
        self.project_name = project_name
        # The processor's own maps, shared rather than copied: an import binds
        # its names in the map every later pass reads, and its site rides on
        # the IMPORTS edge the processor's flush emits.
        self.import_mapping = import_mapping
        self._import_sites = import_sites
        # Scala packages need not mirror directories (issue #2450): each
        # file's package clauses, scanned when it is parsed, say which
        # modules declare a package an import names.
        self._scala_scans: dict[str, ScalaPackageScan] = {}
        # Each Scala file's imports as written, in order. The import maps are
        # rebuilt from these once every file is parsed, so a provider edited
        # in watch mode re-points importers that were not re-parsed.
        self._scala_imports: dict[str, list[ScalaImportBinding]] = {}
        # Importer -> the resolved import map of each `package p { ... }`
        # block. Sibling blocks may bind one name to different classes, so a
        # block's imports stay out of the file-level map and its own.
        self._scala_block_imports: dict[str, list[ScalaBlockImports]] = {}
        # (importer, module qn) pairs of the deferred IMPORTS edges that land
        # on the project's own modules; any other Scala edge is external.
        self._scala_resolved_edges: set[tuple[str, str]] = set()
        # Scala files parsed since the last resolution: their IMPORTS edges
        # are queued per binding once the project's packages are known.
        self._scala_requeue: set[str] = set()
        # Importer -> written import path -> the project member it names, for
        # consumers that captured the written path before it was resolved.
        self._scala_import_members: dict[str, dict[str, str]] = {}
        # The packages every known Scala module declares, for name lookups
        # that follow the same visibility rules as the imports.
        self._scala_package_index = ScalaPackageIndex(modules={}, owners={})

    def record_package_scan(self, module_qn: str, root_node: Node) -> None:
        self._scala_scans[module_qn] = scala_utils.scan_packages(root_node)

    def drop_module_imports(self, module_qn: str) -> None:
        # Re-recorded once the Scala parse completes; a parse that fails
        # midway must not leave the previous imports to be restored.
        self._scala_imports.pop(module_qn, None)
        self._scala_block_imports.pop(module_qn, None)

    def parse_imports(
        self,
        captures: dict[str, list[Node]],
        module_qn: str,
        root_node: Node | None = None,
    ) -> None:
        """Scala imports: plain, selector group, rename and wildcard.

        Scala writes the package path as loose `identifier` and `.` children
        of the import_declaration rather than as one scoped node, so the
        prefix is assembled from those children up to the point where a
        selector group or wildcard begins. A parser that grabs the first
        identifier gets `scala` for every `scala.*` import.

        The four forms #1186 lists, all on the same node type:
            import a.b.C            -> C     -> a.b.C
            import a.b.{C, D}       -> C, D  -> a.b.C, a.b.D
            import a.b.{C => Alias} -> Alias -> a.b.C   (C stays unbound)
            import a.b._            -> *a.b  -> a.b
        """
        mapping = self.import_mapping[module_qn]
        bindings: list[ScalaImportBinding] = []
        for import_node in captures.get(cs.CAPTURE_IMPORT, []):
            if import_node.type != cs.TS_SCALA_IMPORT_DECLARATION:
                continue
            # Bound into a map of its own first, so exactly the names THIS
            # declaration binds are known: each resolves from the package the
            # import sits in, once the project's packages are (issue #2450).
            bound: dict[str, str] = {}
            self.import_mapping[module_qn] = bound
            try:
                self._parse_scala_import_declaration(import_node, module_qn)
            finally:
                self.import_mapping[module_qn] = mapping
            block = scala_utils.import_block(import_node)
            if block is None:
                # A package block's imports are visible in that block only.
                mapping.update(bound)
            enclosing = scala_utils.enclosing_packages_at(import_node)
            sites = self._import_sites.get(module_qn, {})
            bindings.extend(
                ScalaImportBinding(name, path, enclosing, block, sites.get(name))
                for name, path in bound.items()
            )
        self._scala_imports[module_qn] = bindings
        self._scala_requeue.add(module_qn)

    def _parse_scala_import_declaration(
        self, import_node: Node, module_qn: str
    ) -> None:
        """One declaration, which may hold SEVERAL comma-separated imports.

        `import a.b.C, d.e.F` is a single import_declaration whose children
        run straight through the comma. Collecting identifiers across the
        whole node merges the two into one prefix, yielding the nonsense path
        `a.b.C.d.e.F` for `F` and losing `C` entirely -- so the children are
        split on `,` and each group parsed independently.
        """
        group: list[Node] = []
        for child in import_node.children:
            if child.type == cs.CHAR_COMMA:
                self._parse_scala_import_group(group, module_qn, import_node)
                group = []
            elif child.type != cs.TS_SCALA_IMPORT_KEYWORD:
                group.append(child)
        self._parse_scala_import_group(group, module_qn, import_node)

    def _parse_scala_import_group(
        self, children: list[Node], module_qn: str, import_node: Node
    ) -> None:
        prefix_parts: list[str] = []
        selectors: Node | None = None
        wildcard: Node | None = None
        renamed: Node | None = None

        for child in children:
            if child.type == cs.TS_SCALA_NAMESPACE_SELECTORS:
                selectors = child
            elif child.type == cs.TS_SCALA_NAMESPACE_WILDCARD:
                wildcard = child
            elif child.type == cs.TS_SCALA_AS_RENAMED_IDENTIFIER:
                # Scala 3 `import a.b as Alias`: the renamed node sits directly
                # under the declaration rather than inside a selector group, so
                # this path is distinct from the braced `{C as Alias}` form.
                renamed = child
            elif child.type == cs.TS_IDENTIFIER:
                if name := safe_decode_with_fallback(child):
                    prefix_parts.append(name)

        if renamed is not None:
            self._bind_scala_rename(
                renamed, cs.SEPARATOR_DOT.join(prefix_parts), module_qn, import_node
            )
            return

        if not prefix_parts:
            return

        if wildcard is not None:
            # `import a.b._` (Scala 2) and `_root_.a.b.*` (Scala 3) both reach
            # here; the package itself is what comes into scope.
            package = cs.SEPARATOR_DOT.join(prefix_parts)
            key = f"{cs.SCALA_WILDCARD_PREFIX}{package}"
            self.import_mapping[module_qn][key] = package
            self._host._record_import_site(module_qn, key, import_node)
            return

        if selectors is not None:
            package = cs.SEPARATOR_DOT.join(prefix_parts)
            self._parse_scala_selectors(selectors, package, module_qn, import_node)
            return

        # Plain `import a.b.C`: the last part is the imported name, and
        # everything is also the full path.
        full_path = cs.SEPARATOR_DOT.join(prefix_parts)
        self.import_mapping[module_qn][prefix_parts[-1]] = full_path
        self._host._record_import_site(
            module_qn, prefix_parts[-1], import_node, prefix_parts[-1]
        )

    def _bind_scala_rename(
        self, node: Node, package: str, module_qn: str, import_node: Node
    ) -> None:
        """Bind `original as/=> alias` to the ALIAS only.

        Binding the original as well would make it resolvable under a name
        this file never introduced, which is precisely what a rename avoids --
        and a presence-only test would not notice.

        `package` is the prefix the rename hangs off: the selector group's
        package for `a.b.{C as X}`, and the leading segments for the top-level
        `a.b as X`, where the renamed node itself carries the last segment.
        """
        names = [
            decoded
            for child in node.children
            if child.type == cs.TS_IDENTIFIER
            and (decoded := safe_decode_with_fallback(child))
        ]
        if len(names) != 2:
            return
        original, alias = names
        full = f"{package}{cs.SEPARATOR_DOT}{original}" if package else original
        self.import_mapping[module_qn][alias] = full
        self._host._record_import_site(module_qn, alias, import_node, original)

    def _parse_scala_selectors(
        self, selectors: Node, package: str, module_qn: str, import_node: Node
    ) -> None:
        for child in selectors.children:
            if child.type in (
                cs.TS_SCALA_ARROW_RENAMED_IDENTIFIER,
                cs.TS_SCALA_AS_RENAMED_IDENTIFIER,
            ):
                # `{Map => MMap}` (Scala 2) and `{Map as MMap}` (Scala 3) are
                # different node types spelling the same thing.
                self._bind_scala_rename(child, package, module_qn, import_node)
            elif child.type == cs.TS_SCALA_NAMESPACE_WILDCARD:
                # `import a.{b, _}` -- a wildcard alongside explicit names.
                key = f"{cs.SCALA_WILDCARD_PREFIX}{package}"
                self.import_mapping[module_qn][key] = package
                self._host._record_import_site(module_qn, key, import_node)
            elif child.type == cs.TS_IDENTIFIER:
                if name := safe_decode_with_fallback(child):
                    self.import_mapping[module_qn][name] = (
                        f"{package}{cs.SEPARATOR_DOT}{name}"
                    )
                    self._host._record_import_site(module_qn, name, import_node, name)

    def resolve_scala_project_imports(self, known_module_paths: dict[str, str]) -> None:
        """Point Scala imports of the project's own packages at its modules.

        A Scala import names a package path (`shop.Cart`), and the package
        lives wherever a `package shop` clause says, not in a directory: read
        raw, every first-party import became an ExternalModule and the names
        it bound resolved nowhere (issue #2450). Rebuilt from the written
        imports on every run, after every module is known and before the
        inheritance pass reads bases through the map.
        """
        importers = [qn for qn in self._scala_imports if qn in known_module_paths]
        # The edges queued by this run's parse were keyed by name, which two
        # package blocks can share, and left a block's imports out; they are
        # queued again per binding.
        requeue = self._scala_requeue if self._host.ingestor is not None else set()
        self._scala_requeue = set()
        self._scala_import_members = {}
        if not importers:
            return
        self._recover_unscanned_scala_modules(known_module_paths)
        index = scala_utils.package_index(self._scala_scans, known_module_paths)
        self._scala_package_index = index
        self._host.drop_deferred_import_edges(requeue, cs.SupportedLanguage.SCALA)
        self._scala_resolved_edges = {
            pair for pair in self._scala_resolved_edges if pair[0] not in requeue
        }
        for importer in importers:
            self._rewrite_scala_imports(importer, index, importer in requeue)

    def _rewrite_scala_imports(
        self, importer: str, index: ScalaPackageIndex, requeue: bool
    ) -> None:
        scan = self._scala_scans.get(importer)
        file_mapping: dict[str, str] = {}
        block_mappings: dict[ScalaPackageBlock, dict[str, str]] = {}
        members: dict[str, str] = {}
        for binding in self._scala_imports[importer]:
            target = scala_utils.resolve_import(binding.path, binding.enclosing, index)
            if binding.block is None:
                mapping = file_mapping
                if target is not None and target.member_qn is not None:
                    members[binding.path] = target.member_qn
            else:
                mapping = block_mappings.setdefault(binding.block, {})
            scala_utils.bind_import(mapping, binding.local_name, binding.path, target)
            if requeue:
                self._requeue_scala_import_edges(importer, binding, target, scan)
        current = self.import_mapping.setdefault(importer, {})
        current.clear()
        current.update(file_mapping)
        self._scala_block_imports[importer] = [
            ScalaBlockImports(block, mapping)
            for block, mapping in block_mappings.items()
        ]
        if members:
            self._scala_import_members[importer] = members

    def _requeue_scala_import_edges(
        self,
        importer: str,
        binding: ScalaImportBinding,
        target: ScalaImportTarget | None,
        scan: ScalaPackageScan | None,
    ) -> None:
        if target is None:
            # Not the project's: the written path takes the external route.
            self._host.defer_import_edge(
                importer, binding.path, cs.SupportedLanguage.SCALA, binding.site
            )
            return
        for module_qn in self._scala_edge_modules(importer, target, scan):
            self._scala_resolved_edges.add((importer, module_qn))
            self._host.defer_import_edge(
                importer, module_qn, cs.SupportedLanguage.SCALA, binding.site
            )

    def scala_block_import(
        self, module_qn: str, name: str, position: int
    ) -> str | None:
        """What `name` is imported as by the package blocks around `position`.

        The innermost block binding it wins, an explicit import over a
        wildcard; None when no block around the position imports it, so the
        file-level imports answer. A class the importing block, or a block
        inside it, declares in this file binds tighter than the import, so
        that declaration answers instead.
        """
        scan = self._scala_scans.get(module_qn)
        blocks = {
            imports.block: imports.mapping
            for imports in self._scala_block_imports.get(module_qn, ())
        }
        if scan is None or not blocks:
            return None
        declared: str | None = None
        for block in scala_utils.blocks_at(scan.blocks, position):
            declared = declared or self._scala_block_declared(
                module_qn, scan, block, name
            )
            if imported := self._scala_block_mapped(blocks.get(block, {}), name):
                return declared or imported
        return None

    def _scala_block_mapped(self, mapping: dict[str, str], name: str) -> str | None:
        if name in mapping:
            return mapping[name]
        return self._scala_wildcard_member(mapping, name)

    def _scala_block_declared(
        self,
        module_qn: str,
        scan: ScalaPackageScan,
        block: ScalaPackageBlock,
        name: str,
    ) -> str | None:
        # A member the block's own package declares in this file, which the
        # definition pass names under the module: package blocks add nothing.
        own = f"{module_qn}{cs.SEPARATOR_DOT}{name}"
        if (
            block.enclosing
            and name in scan.packages.get(block.enclosing[0], ())
            and self._host.function_registry is not None
            and own in self._host.function_registry
        ):
            return own
        return None

    def _scala_wildcard_member(self, mapping: dict[str, str], name: str) -> str | None:
        # The first wildcard import whose package or object registers `name`.
        registry = self._host.function_registry
        if registry is None:
            return None
        for key, imported in mapping.items():
            candidate = f"{imported}{cs.SEPARATOR_DOT}{name}"
            if key.startswith(cs.SCALA_WILDCARD_PREFIX) and candidate in registry:
                return candidate
        return None

    def scala_type_qn(
        self, module_qn: str, written: str, position: int | None = None
    ) -> str | None:
        """The project qn a type name written in a Scala file names, if any.

        Looked up the way the compiler would, and no further: the imports of
        the package blocks around `position`, the file's imports, its own
        definitions, its packages' other files, then wildcard imports. A name
        none of them reaches is not the project's, however many classes
        elsewhere share it.
        """
        head, _, rest = written.partition(cs.SEPARATOR_DOT)
        if position is not None and (
            imported := self.scala_block_import(module_qn, head, position)
        ):
            return f"{imported}{cs.SEPARATOR_DOT}{rest}" if rest else imported
        enclosing = self._scala_enclosing_packages(module_qn, position)
        if rest:
            target = scala_utils.resolve_import(
                written, enclosing, self._scala_package_index
            )
            return target.member_qn if target is not None else None
        return self._scala_simple_type_qn(module_qn, written, enclosing)

    def _scala_enclosing_packages(
        self, module_qn: str, position: int | None
    ) -> tuple[str, ...]:
        # The packages of the innermost package block around `position`, or
        # the file's own when no block holds it.
        scan = self._scala_scans.get(module_qn)
        if scan is None:
            return ()
        if position is not None and (
            blocks := scala_utils.blocks_at(scan.blocks, position)
        ):
            return blocks[0].enclosing
        return scan.enclosing

    def _scala_simple_type_qn(
        self, module_qn: str, written: str, enclosing: tuple[str, ...]
    ) -> str | None:
        # An undotted name, past the package blocks: the file's imports, its
        # own definitions, its packages' other files, then wildcard imports.
        mapping = self.import_mapping.get(module_qn, {})
        if written in mapping:
            return mapping[written]
        registry = self._host.function_registry
        own = f"{module_qn}{cs.SEPARATOR_DOT}{written}"
        if registry is not None and own in registry:
            return own
        for package in enclosing:
            if owner := scala_utils.package_owner(
                package, written, self._scala_package_index
            ):
                return f"{owner}{cs.SEPARATOR_DOT}{written}"
        return self._scala_wildcard_member(mapping, written)

    def scala_base_qn(
        self, module_qn: str, child_qn: str, parent_qn: str, base_index: int
    ) -> str | None:
        """The project qn a Scala class's base names through its imports.

        The base was bound while the file was parsed, before any package was
        known to be the project's own: to its file-level import's written
        path, or by name alone when only a package block imports it. A base
        a block around the class imports resolves through that block, or to
        the class of that name the block declares in this file.
        """
        prefix = f"{module_qn}{cs.SEPARATOR_DOT}"
        scan = self._scala_scans.get(module_qn)
        site = (
            scan.class_bases.get(child_qn[len(prefix) :])
            if scan is not None and child_qn.startswith(prefix)
            else None
        )
        if site is not None and base_index < len(site.bases):
            written = site.bases[base_index]
            head, _, rest = written.partition(cs.SEPARATOR_DOT)
            last = written.rsplit(cs.SEPARATOR_DOT, 1)[-1]
            if parent_qn.rsplit(cs.SEPARATOR_DOT, 1)[-1] == last and (
                imported := self.scala_block_import(module_qn, head, site.point)
            ):
                return f"{imported}{cs.SEPARATOR_DOT}{rest}" if rest else imported
        return self._scala_import_members.get(module_qn, {}).get(parent_qn)

    def _scala_edge_modules(
        self,
        importer: str,
        target: ScalaImportTarget,
        scan: ScalaPackageScan | None,
    ) -> tuple[str, ...]:
        if target.member_qn is not None:
            return tuple(qn for qn in target.declaring if qn != importer)
        # A package import pins only the modules whose names the file uses,
        # as a C# namespace `using` does (issue #1347); a name the file
        # declares itself binds to its own definition, so it is no evidence.
        used: frozenset[str] = frozenset()
        if scan is not None:
            own = {name for names in scan.packages.values() for name in names}
            used = scan.mentions - own
        return tuple(
            sorted(
                qn
                for qn, names in target.declaring.items()
                if qn != importer and not used.isdisjoint(names)
            )
        )

    def _recover_unscanned_scala_modules(
        self, known_module_paths: dict[str, str]
    ) -> None:
        # An incremental run parses only CHANGED files, and a module it did
        # not parse is known by qn alone: without its package clauses an
        # unchanged provider would turn its importers back into external
        # imports. Its file is found by the walk the indexer itself takes.
        pending = {
            qn: Path(path)
            for qn, path in known_module_paths.items()
            if qn not in self._scala_scans and path.endswith(cs.SCALA_EXTENSIONS)
        }
        pathless = {
            qn
            for qn, path in known_module_paths.items()
            if not path and qn not in self._scala_scans
        }
        if pathless:
            pending.update(self._scala_files_for(pathless))
            for qn in pathless - pending.keys():
                # Not a Scala file: remembered so later runs skip the walk.
                self._scala_scans[qn] = _EMPTY_SCALA_SCAN
        if not pending:
            return
        from ...parser_loader import load_parsers

        parsers, _queries = load_parsers()
        parser = parsers.get(cs.SupportedLanguage.SCALA)
        for qn, path in pending.items():
            try:
                source = path.read_bytes()
            except OSError:
                self._scala_scans[qn] = _EMPTY_SCALA_SCAN
                continue
            self._scala_scans[qn] = (
                scala_utils.scan_packages(parser.parse(source).root_node)
                if parser is not None
                else _EMPTY_SCALA_SCAN
            )

    def _scala_files_for(self, module_qns: set[str]) -> dict[str, Path]:
        found: dict[str, Path] = {}
        for _dirpath, fname, rel in walk_eligible_files(
            self.repo_path, self._host.exclude_paths, self._host.unignore_paths
        ):
            if not fname.endswith(cs.SCALA_EXTENSIONS):
                continue
            base = base_module_qn(Path(rel), self.project_name)
            # A stem shared with another file carries its extension.
            for qn in (base, own_extension_module_qn(base, fname)):
                if qn in module_qns:
                    found[qn] = self.repo_path / rel
        return found

    def is_scala_project_import(self, entry: DeferredImportEdge) -> bool:
        return (
            entry.language == cs.SupportedLanguage.SCALA
            and (entry.module_qn, entry.full_name) in self._scala_resolved_edges
        )
