"""Dart's import pass: `import`, `export` and `part` directives into the
shared import map, their `as` prefixes and the libraries a barrel hands on,
and the repo's own pubspec packages a `package:` URI can name.

`ImportProcessor` owns what every language shares (the import map, the
import sites, the deferred IMPORTS edges and their flush) and hands a Dart
file's imports to the `DartImporter` it builds."""

from pathlib import Path
from typing import Protocol

from tree_sitter import Node

from ... import constants as cs
from ...types_defs import PropertyDict
from . import utils as dart_utils
from .package_paths import discover_dart_packages


class DartImportHost(Protocol):
    """What the Dart importer reads and calls on the import processor that
    builds it: the indexer's walk policy, and the import-site record every
    language's bindings go through.

    The policy is read here rather than copied in: the factory re-resolves
    `unignore_paths` on processors it has already built
    (`propagate_unignore_paths`), and a stale copy would miss a pubspec in a
    tree the indexer walks, or find one in a tree it skips (issue #1088)."""

    exclude_paths: frozenset[str] | None
    unignore_paths: frozenset[str] | None

    def _record_import_site(
        self,
        scope_qn: str,
        local_name: str,
        node: Node | None,
        imported_name: str | None = None,
        owner_qn: str | None = None,
    ) -> PropertyDict | None: ...


class DartImporter:
    __slots__ = (
        "_host",
        "repo_path",
        "project_name",
        "import_mapping",
        "dart_prefix_shadows",
        "dart_import_aliases",
        "dart_exposed_libraries",
        "_dart_packages",
    )

    def __init__(
        self,
        host: DartImportHost,
        repo_path: Path,
        project_name: str,
        import_mapping: dict[str, dict[str, str]],
    ) -> None:
        self._host = host
        self.repo_path = repo_path
        self.project_name = project_name
        # The processor's own map, shared rather than copied: a directive
        # binds its name in the map every later pass reads.
        self.import_mapping = import_mapping
        # Dart: {module qn: {import prefix: [byte spans where a local or
        # parameter of that name shadows it]}} (issue #2033).
        self.dart_prefix_shadows: dict[str, dict[str, list[tuple[int, int]]]] = {}
        # `import 'other.dart' as helper;` binds `helper` to other.dart even
        # when an unprefixed `import 'helper.dart';` already owns that key in
        # import_mapping. Kept separately so BOTH imports keep their IMPORTS
        # edge (those come from import_mapping's values) while the name the
        # source writes resolves to the aliased library (#2033).
        self.dart_import_aliases: dict[str, dict[str, list[str]]] = {}
        # {library module qn: [module qns it hands to ITS importers]}: the
        # targets of its `export` and `part` directives. import_mapping holds
        # those beside plain imports, which expose nothing, so an extension
        # reached through a barrel file could not be told from one the barrel
        # merely uses (issue #2482).
        self.dart_exposed_libraries: dict[str, list[str]] = {}
        # The repo's own Dart packages, pubspec `name:` -> qn of its `lib/`,
        # found on the first `package:` import so a project with no Dart
        # pays no extra walk (issue #3278).
        self._dart_packages: dict[str, str] | None = None

    def parse_imports(
        self,
        captures: dict[str, list[Node]],
        module_qn: str,
        root_node: Node | None = None,
    ) -> None:
        # Dart import/export/part directives carry a URI string. `dart:` and
        # `package:` targets are external (kept verbatim); relative paths and part
        # files resolve to a project-internal module qn. A `part of my.library;`
        # directive names a dotted library, not a file, so it has no URI and is
        # skipped.
        prefixes: set[str] = set()
        for import_node in captures.get(cs.CAPTURE_IMPORT, []):
            uri = dart_utils.extract_uri(import_node)
            if not uri:
                continue
            if full_name := self.dart_import_target(uri, module_qn):
                local_name = dart_utils.local_name(uri)
                self.import_mapping[module_qn][local_name] = full_name
                self._host._record_import_site(module_qn, local_name, import_node, uri)
                if dart_utils.exposes_library(import_node):
                    self.dart_exposed_libraries.setdefault(module_qn, []).append(
                        full_name
                    )
                # `import 'lib.dart' as p;` binds the library's names under
                # `p`, and the file-derived key never appears in the source,
                # so a prefixed reference (`p.Box`) resolves only once the
                # PREFIX is a key too (issue #2033). Both keys are kept: the
                # file-derived one still serves an unprefixed import of the
                # same file elsewhere in the module.
                prefix = dart_utils.import_prefix(import_node)
                if prefix:
                    self._bind_dart_import_prefix(
                        module_qn, prefix, full_name, import_node, uri
                    )
                    prefixes.add(prefix)
        # A local or parameter of the same name SHADOWS the prefix inside its
        # scope, and an UNTYPED one (`var p = 1`) never reaches the resolver's
        # local_var_types, so the type map cannot answer this. The binding is
        # syntactic, so record its span here and let the fold check the call
        # site against it (issue #2033).
        if root_node is not None and prefixes:
            self.dart_prefix_shadows[module_qn] = dart_utils.binding_spans(
                root_node, frozenset(prefixes)
            )

    def dart_import_target(self, uri: str, module_qn: str) -> str:
        # What `uri` resolves to under the package map as it is on disk now;
        # a pubspec change is judged by whether this moved (#3278).
        packages = (
            self._dart_package_libs()
            if uri.startswith(cs.DART_SCHEME_PACKAGE)
            else None
        )
        return dart_utils.resolve_import(uri, module_qn, packages)

    def _dart_package_libs(self) -> dict[str, str]:
        # `package:<name>/<path>` with `<name>` a pubspec in this repository
        # names a module of the project, not a dependency (issue #3278).
        if self._dart_packages is None:
            self._dart_packages = {
                name: f"{self.project_name}{cs.SEPARATOR_DOT}{lib}"
                for name, lib in discover_dart_packages(
                    self.repo_path, self._host.exclude_paths, self._host.unignore_paths
                ).items()
            }
        return self._dart_packages

    def _bind_dart_import_prefix(
        self,
        module_qn: str,
        prefix: str,
        full_name: str,
        import_node: Node,
        uri: str,
    ) -> None:
        # An explicit `as` prefix is the name the SOURCE uses for
        # this import, so it owns that name outright. Recorded in
        # its own map rather than overwriting import_mapping,
        # whose values carry the IMPORTS edges: writing it there
        # dropped the colliding unprefixed import entirely.
        # Several imports may SHARE a prefix (`import 'a.dart' as
        # p; import 'b.dart' as p;`), so every library is kept and
        # the fold picks the one defining the name (CodeRabbit,
        # PR #2040).
        self.dart_import_aliases.setdefault(module_qn, {}).setdefault(
            prefix, []
        ).append(full_name)
        # An alias must not clobber a key another import already
        # owns: `import 'helper.dart'; import 'other.dart' as helper;`
        # would otherwise drop the first import entirely, since the
        # file-derived key and the alias collide. The prefix is the
        # name the source uses for THIS import, so it is only added
        # where it is free.
        if prefix not in self.import_mapping[module_qn]:
            self.import_mapping[module_qn][prefix] = full_name
            # Its IMPORTS edge carries a span and alias like every
            # other binding (PR #2040).
            self._host._record_import_site(module_qn, prefix, import_node, uri)

    def reset_dart_path_caches(self) -> None:
        # Same contract as reset_rust_path_caches: a pubspec added, removed
        # or renamed since the package map was built must be re-read.
        self._dart_packages = None
