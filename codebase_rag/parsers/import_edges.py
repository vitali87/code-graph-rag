"""The IMPORTS edges held back until every file is parsed, and where each
one lands.

An internal import target is only real if some file yields that module qn,
known once every file is parsed (issue #652), so `ImportProcessor` queues
each binding's edge and hands the queue to the `ImportEdges` it owns. Each
edge is routed by its language: a Python name that may be a submodule, a
Rust `crate::` path trimmed to the longest prefix that is a module, a Julia
target kept inside its own nested package, a C# namespace landed on the
modules that declare it and that this file uses, a Scala path into the
project's own packages. Every other target resolves to a module path: an
internal one is verified against the real module qns (or dropped, with the
name noted for the file that may define it later), an external one gets
the ExternalModule node no file pass creates. `ImportEdges` calls back
through `ImportEdgeHost` for the graph, the project and the record of what
a module could not resolve."""

from collections.abc import Mapping
from pathlib import Path
from typing import NamedTuple, Protocol

from loguru import logger

from .. import constants as cs
from .. import logs as ls
from ..services import IngestorProtocol
from ..types_defs import DeferredImportEdge, PropertyDict, StemSiblingModules
from .csharp.imports import CSharpImporter
from .import_host import UnresolvedNameHost
from .import_targets import (
    module_alias_map,
    stem_sibling_modules,
    verify_internal_import_target,
)
from .java.imports import JavaImporter
from .js_ts.imports import JsTsImporter
from .julia.imports import JuliaImporter
from .scala.imports import ScalaImporter
from .stdlib_extractor import StdlibExtractor
from .utils import is_project_qn


def _names_js_module(site: PropertyDict | None) -> bool:
    # `export * from` and `import * as ns` record `*` as their imported name,
    # and `const m = require("./m")` records none: each names the module
    # itself, never a name inside it (issues #2464, #2934).
    if site is None:
        return False
    imported = site.get(cs.KEY_IMPORTED_NAME)
    return imported is None or imported == cs.IMPORTED_NAME_WILDCARD


def _external_module_name(module_path: str) -> str:
    """The display name for an ExternalModule qn.

    The last DOTTED segment names a package path (`os.path` -> `path`,
    `std.fmt` -> `fmt`), but a C/C++ header carries its EXTENSION in that
    position, so every `.h` include minted an ExternalModule called `h`:
    `std.stdio.h`, `std.signal.h` and `std.sys.types.h` alike (issue #1758).
    A header is named by its own stem instead, the same name
    `_cpp_include_local_name` binds it under, so the node and the local name
    agree.

    The header rule is deliberately confined to the `std.` prefix that
    `_cpp_include_full_name` puts on an include, AND to qns of three or more
    segments. This function serves EVERY language's external imports, and a
    package whose last segment is literally `h` is a real thing (the Python
    HTTP/2 library), so an unconditional rule would rename `mypkg.h` to
    `mypkg` for languages that have no header extensions at all.

    The three-segment floor leaves one known inconsistency: `<std.h>` has the
    two-segment qn `std.h`, indistinguishable here from a package `h` under
    `std`, so its node is named `h` while `_cpp_include_local_name` binds it
    as `std`. Lowering the floor to two would rename every `<pkg.h>`-shaped
    external of every language; `<std.h>` is not a real header, so the
    inconsistency is the cheaper of the two errors.

    A second, unfixable-here case: this sees only the qn, and slash
    segmentation has already erased the difference between a PATH and dots
    inside one basename. `<foo/bar.h>` and `<foo.bar.h>` both become
    `std.foo.bar.h`, yet `_cpp_include_local_name` binds the first as `bar`
    (basename stem) and the second as `foo` (first stem segment). One name
    must therefore disagree with its binding, and no rule at this layer can
    tell which. It is `<foo/bar.h>` -- a real path, the common shape -- that
    is served correctly; `<foo.bar.h>` gets `bar` where its binding says
    `foo`. Fixing that means passing the ORIGINAL include path down to node
    creation rather than deriving from the qn, which is a wider change than
    the naming defect warrants (#1758).
    """
    segments = module_path.split(cs.SEPARATOR_DOT)
    # `stdio.h` occupies the last TWO dotted segments, stem and extension.
    if (
        len(segments) >= 3
        and module_path.startswith(cs.IMPORT_STD_PREFIX)
        and f"{cs.SEPARATOR_DOT}{segments[-1]}" in (cs.EXT_H, cs.EXT_HPP)
    ):
        return segments[-2]
    return segments[-1]


class _KnownModules(NamedTuple):
    # What one flush verifies its internal targets against: every module qn
    # with its file path (for the language tie-break), the container
    # aliases, each family's stem siblings, and the modules declaring each
    # C# namespace.
    paths: dict[str, str]
    qns: set[str]
    aliases: dict[str, str]
    siblings: StemSiblingModules
    csharp_namespace_modules: dict[str, dict[str, set[str]]]


class ImportEdgeHost(UnresolvedNameHost, Protocol):
    """What the import edges read and call on the import processor that
    owns them: the graph they are written to (None: parse only), the
    repository and project the module qns derive from, the stdlib layout an
    external target's module path is read from, and the record of a name a
    module could not resolve."""

    ingestor: IngestorProtocol | None
    repo_path: Path
    project_name: str
    stdlib_extractor: StdlibExtractor


class ImportEdges:
    """Lands a flushed batch of deferred IMPORTS edges on their targets.

    Built and owned by `ImportProcessor`, with the importers whose own
    layouts decide where a language's targets are."""

    __slots__ = ("_host", "_java", "_js_ts", "_julia", "_csharp", "_scala")

    def __init__(
        self,
        host: ImportEdgeHost,
        *,
        java: JavaImporter,
        js_ts: JsTsImporter,
        julia: JuliaImporter,
        csharp: CSharpImporter,
        scala: ScalaImporter,
    ) -> None:
        self._host = host
        self._java = java
        self._js_ts = js_ts
        self._julia = julia
        self._csharp = csharp
        self._scala = scala

    def flush(
        self, deferred: list[DeferredImportEdge], known_module_paths: dict[str, str]
    ) -> int:
        """Emit each deferred edge whose target verifies; the number
        emitted."""
        known_module_qns = set(known_module_paths)
        module_aliases = module_alias_map(known_module_qns)
        siblings = stem_sibling_modules(
            known_module_paths, self._host.repo_path, self._host.project_name
        )
        # namespace -> the indexed modules DECLARING it, from the per-file
        # scan done at parse time: C# namespaces need not mirror directory
        # names, so path-based guessing cannot answer "is this internal". An
        # incremental run only re-parses CHANGED files, so unchanged C#
        # modules recover their namespaces here from a targeted parse of
        # their source, or the internal detection would silently fail in
        # watch mode and resurrect the dead-end ExternalModule.
        self._csharp.recover_unparsed_csharp_state(known_module_paths)
        known = _KnownModules(
            known_module_paths,
            known_module_qns,
            module_aliases,
            siblings,
            self._csharp.csharp_namespace_modules(known_module_qns),
        )
        emitted = 0
        for entry in deferred:
            emitted += self._flush_entry(entry, known)
        return emitted

    def _flush_entry(self, entry: DeferredImportEdge, known: _KnownModules) -> int:
        # `from pkg.transport import TTransport` is ambiguous in
        # Python: TTransport may be an item OR a submodule. The stdlib
        # extractor strips it as an item, anchoring the edge at the
        # package; when the FULL dotted name verifies as a real module,
        # the submodule is the true target.
        if entry.language == cs.SupportedLanguage.PYTHON and (
            full_target := self._verify(
                entry.full_name, known, entry.language, known.siblings
            )
        ):
            self._emit_import_edge(entry, cs.NodeLabel.MODULE, full_target)
            return 1
        if self._is_rust_project_import(entry):
            return self._flush_rust_project_import(entry, known)
        project_name = self._host.project_name
        if entry.language == cs.SupportedLanguage.JULIA and (
            entry.full_name == project_name
            or entry.full_name.startswith(f"{project_name}{cs.SEPARATOR_DOT}")
        ):
            return self._flush_julia_project_import(entry, known)
        if entry.language == cs.SupportedLanguage.CSHARP and (
            declaring := known.csharp_namespace_modules.get(entry.full_name)
        ):
            return self._emit_csharp_internal_imports(entry, declaring, known.qns)
        if self._scala.is_scala_project_import(entry):
            return self._flush_scala_project_import(entry, known.qns)
        return self._flush_resolved_import(entry, known)

    def _verify(
        self,
        module_path: str,
        known: _KnownModules,
        language: cs.SupportedLanguage,
        siblings: StemSiblingModules | None,
    ) -> str | None:
        return verify_internal_import_target(
            module_path,
            known.paths,
            known.aliases,
            language,
            self._host.project_name,
            siblings,
        )

    def _emit_csharp_internal_imports(
        self,
        entry: DeferredImportEdge,
        declaring: dict[str, set[str]],
        known_module_qns: set[str],
    ) -> int:
        """IMPORTS edges for a `using` of a namespace declared in the repo.

        A `using` names a NAMESPACE. When indexed modules declare it, the edge
        lands on the specific modules this file actually uses: either a
        resolved call/reference/inherit crossed into the module, or the file
        mentions a type name the module declares under that namespace. A
        namespace-keyed ExternalModule would be a dead end no edge ever
        connects to the defining files (issue #1347).
        """
        if self._host.ingestor is None:
            return 0
        emitted = 0
        for target_module_qn in self._csharp.using_targets(entry.module_qn, declaring):
            if target_module_qn not in known_module_qns:
                continue
            self._emit_import_edge(entry, cs.NodeLabel.MODULE, target_module_qn)
            emitted += 1
        return emitted

    def _emit_import_edge(
        self, entry: DeferredImportEdge, target_label: str, target_qn: str
    ) -> None:
        ingestor = self._host.ingestor
        if ingestor is None:
            return
        from_spec = (cs.NodeLabel.MODULE, cs.KEY_QUALIFIED_NAME, entry.module_qn)
        to_spec = (target_label, cs.KEY_QUALIFIED_NAME, target_qn)
        if entry.site:
            ingestor.ensure_relationship_batch(
                from_spec, cs.RelationshipType.IMPORTS, to_spec, properties=entry.site
            )
        else:
            ingestor.ensure_relationship_batch(
                from_spec, cs.RelationshipType.IMPORTS, to_spec
            )

    def _flush_julia_project_import(
        self, entry: DeferredImportEdge, known: _KnownModules
    ) -> int:
        # A Julia `using`/`import` target resolved to a file-based
        # project qn at parse time: verify it against the real module
        # qns, not the generic resolver (which would externalise the
        # bare project name).
        candidate = entry.full_name
        importer_pkg = self._julia.julia_nested_pkg_root(
            known.paths.get(entry.module_qn, "")
        )
        target = None
        while True:
            target = self._verify(candidate, known, entry.language, None)
            target_pkg = (
                self._julia.julia_nested_pkg_root(known.paths.get(target, ""))
                if target is not None
                else None
            )
            if target_pkg is not None and target_pkg != importer_pkg:
                # A target in ANOTHER nested package is unreachable
                # from here; one in the SAME nested package is a
                # normal sibling import (issue #1882).
                target = None
            if target is not None:
                break
            trimmed = candidate.rsplit(cs.SEPARATOR_DOT, 1)[0]
            if trimmed in (candidate, self._host.project_name):
                break
            candidate = trimmed
        if target == entry.module_qn:
            # `using .SelfModule` inside the file's own module: a
            # self-import edge is meaningless.
            return 0
        if target is None:
            logger.debug(
                ls.IMP_DROPPED_PHANTOM_TARGET,
                from_module=entry.module_qn,
                to_module=entry.full_name,
            )
            return 0
        self._emit_import_edge(entry, cs.NodeLabel.MODULE, target)
        return 1

    def _flush_scala_project_import(
        self, entry: DeferredImportEdge, known_module_qns: set[str]
    ) -> int:
        # A path into the project's own packages lands on the module
        # declaring its member; never on an ExternalModule.
        if entry.full_name not in known_module_qns:
            return 0
        self._emit_import_edge(entry, cs.NodeLabel.MODULE, entry.full_name)
        return 1

    def _is_rust_project_import(self, entry: DeferredImportEdge) -> bool:
        return entry.language == cs.SupportedLanguage.RUST and is_project_qn(
            entry.full_name, self._host.project_name
        )

    def _flush_rust_project_import(
        self, entry: DeferredImportEdge, known: _KnownModules
    ) -> int:
        # A crate::/super::/self:: use path was rewritten to a project
        # qn at parse time. The module target is the longest prefix
        # that verifies: the full path for a module import, minus the
        # item for item imports, further for enum variants and
        # associated items (use crate::color::Color::Red). Never
        # externalise a local path (the phantom ExternalModule would
        # orphan; issue #1007).
        candidate = entry.full_name
        target = None
        while True:
            target = self._verify(candidate, known, entry.language, known.siblings)
            if target is not None:
                break
            trimmed = candidate.rsplit(cs.SEPARATOR_DOT, 1)[0]
            if trimmed in (candidate, self._host.project_name):
                break
            candidate = trimmed
        if target == entry.module_qn:
            # `use super::*` in an inline mod resolves to the file's
            # own module; a self-import edge is meaningless.
            return 0
        if target is None:
            logger.debug(
                ls.IMP_DROPPED_PHANTOM_TARGET,
                from_module=entry.module_qn,
                to_module=entry.full_name,
            )
            self._host.note_unresolved(entry.module_qn, entry.full_name)
            return 0
        self._emit_import_edge(entry, cs.NodeLabel.MODULE, target)
        return 1

    def _flush_resolved_import(
        self, entry: DeferredImportEdge, known: _KnownModules
    ) -> int:
        module_path = self.resolve_module_path(
            entry.full_name, entry.language, known.paths, entry.site
        )
        target_label = self._module_label(module_path)
        if target_label == cs.NodeLabel.EXTERNAL_MODULE:
            # An external import target has no file pass to create its
            # node; without one here the IMPORTS edge MERGEs against
            # nothing and is silently dropped (issue #652).
            self.ensure_external_module_node(module_path, entry.full_name)
        else:
            verified = self._verify(module_path, known, entry.language, known.siblings)
            if verified is None and entry.language == cs.SupportedLanguage.PYTHON:
                # A package-anchored guess that names no sibling module is an
                # ABSOLUTE import in Python semantics (`import sys` inside a
                # package); re-resolve it as one.
                if absolute := self._python_absolute_fallback(
                    module_path, entry.module_qn
                ):
                    self.ensure_external_module_node(absolute, entry.full_name)
                    target_label = cs.NodeLabel.EXTERNAL_MODULE
                    verified = absolute
            if verified is None:
                logger.debug(
                    ls.IMP_DROPPED_PHANTOM_TARGET,
                    from_module=entry.module_qn,
                    to_module=module_path,
                )
                self._host.note_unresolved(entry.module_qn, module_path)
                self._host.note_unresolved(entry.module_qn, entry.full_name)
                return 0
            module_path = verified
        if entry.language in cs.JS_TS_LANGUAGES and _names_js_module(entry.site):
            # `export * from "./add"` in `math/index.ts` stores the module
            # `math.add`, which the resolution above reads as the name `add`
            # of the `math` barrel: the barrel itself (issue #2464). A
            # namespace import or whole-module `require` beside a directory's
            # `index.ts` or a same-named file read the same way, so
            # `import * as m from "./utils/math"` imported `utils.index`
            # (issue #2934).
            module_path = (
                self._verify(entry.full_name, known, entry.language, known.siblings)
                or module_path
            )
        if module_path == entry.module_qn and entry.language in cs.JS_TS_LANGUAGES:
            # A module importing itself is no edge.
            return 0
        self._emit_import_edge(entry, target_label, module_path)
        logger.debug(
            ls.IMP_CREATED_RELATIONSHIP,
            from_module=entry.module_qn,
            to_module=module_path,
            full_name=entry.full_name,
        )
        return 1

    def _python_absolute_fallback(self, module_path: str, module_qn: str) -> str | None:
        package_qn = module_qn.rsplit(cs.SEPARATOR_DOT, 1)[0]
        prefix = f"{package_qn}{cs.SEPARATOR_DOT}"
        if not module_path.startswith(prefix):
            return None
        written = module_path[len(prefix) :]
        if not written:
            return None
        absolute = self._host.stdlib_extractor.extract_module_path(
            written, cs.SupportedLanguage.PYTHON
        )
        project_prefix = f"{self._host.project_name}{cs.SEPARATOR_DOT}"
        if not absolute or absolute.startswith(project_prefix):
            return None
        return absolute

    def _is_local_js_import(self, full_name: str) -> bool:
        return full_name.startswith(self._host.project_name + cs.SEPARATOR_DOT)

    def _module_label(self, module_path: str) -> cs.NodeLabel:
        # #498: import targets outside the project prefix live under the
        # dedicated ExternalModule label (mirroring Package/ExternalPackage).
        project_name = self._host.project_name
        if module_path == project_name or module_path.startswith(
            project_name + cs.SEPARATOR_DOT
        ):
            return cs.NodeLabel.MODULE
        return cs.NodeLabel.EXTERNAL_MODULE

    def ensure_external_module_node(self, module_path: str, full_name: str) -> None:
        """Write the ExternalModule node an external target's edge lands on."""
        ingestor = self._host.ingestor
        if not ingestor or not module_path:
            return
        if cs.SEPARATOR_DOUBLE_COLON in module_path:
            name = module_path.rsplit(cs.SEPARATOR_DOUBLE_COLON, 1)[-1]
        else:
            name = _external_module_name(module_path)
        ingestor.ensure_node_batch(
            cs.NodeLabel.EXTERNAL_MODULE,
            {
                cs.KEY_NAME: name,
                cs.KEY_QUALIFIED_NAME: module_path,
                cs.KEY_PATH: full_name,
            },
        )

    def resolve_rust_import_path(self, import_path: str) -> str:
        """The module an external Rust `use` path names, its node written."""
        # Local (crate::/super::/self::) paths never reach this point: they
        # are rewritten to project qns at parse time and short-circuited in
        # the flush. Only external paths (std::fmt) remain.
        parts = import_path.split(cs.SEPARATOR_DOUBLE_COLON)
        module_path = (
            cs.SEPARATOR_DOUBLE_COLON.join(parts[:-1]) if len(parts) > 1 else parts[0]
        )

        self.ensure_external_module_node(module_path, import_path)
        return module_path

    def resolve_module_path(
        self,
        full_name: str,
        language: cs.SupportedLanguage,
        known_module_paths: Mapping[str, str] | None = None,
        site: PropertyDict | None = None,
    ) -> str:
        """The module an import target names, by its language's rule; an
        external one's node written."""
        project_prefix = self._host.project_name + cs.SEPARATOR_DOT
        match language:
            # Java MODULE semantics: internal imports point to file-level MODULE
            # nodes (project.utils.StringUtils) because Java files are named after
            # their primary class. External imports point to package-level
            # (java.util) because we lack source code for file-level nodes. This
            # asymmetry is intentional.
            case cs.SupportedLanguage.JAVA:
                if full_name.startswith(project_prefix):
                    return self._java.java_owning_module_qn(full_name)
            case (
                cs.SupportedLanguage.JS
                | cs.SupportedLanguage.TS
                | cs.SupportedLanguage.TSX
            ):
                if self._is_local_js_import(full_name):
                    return self._js_ts.resolve_js_internal_module(
                        full_name, known_module_paths, site
                    )
            case cs.SupportedLanguage.RUST:
                return self.resolve_rust_import_path(full_name)

        module_path = self._host.stdlib_extractor.extract_module_path(
            full_name, language
        )
        # The bare project name is first-party too: a Go import of the module
        # path itself binds the module's root package, whose qn is exactly it.
        if self._module_label(module_path) == cs.NodeLabel.EXTERNAL_MODULE:
            self.ensure_external_module_node(module_path, full_name)
        return module_path
