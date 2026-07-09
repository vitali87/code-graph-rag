from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from ... import constants as cs
from ...services import IngestorProtocol
from ...types_defs import (
    FunctionRegistryTrieProtocol,
    NodeType,
    PropertyDict,
    SimpleNameLookup,
)
from . import constants as fc
from .qn import CppQnResolver

if TYPE_CHECKING:
    from clang.cindex import Cursor

_NodeKey = tuple[str, str]
_EdgeKey = tuple[str, str, str, str, str]
_Scope = tuple[str, str] | None

_COMPILE_COMMANDS = "compile_commands.json"
_BUILD_DIR = "build"


def cpp_frontend_available() -> bool:
    try:
        import clang.cindex as ci

        ci.Index.create()
    except Exception:
        return False
    return True


def find_compile_commands(start: Path) -> Path | None:
    # (H) Discover the directory holding a compile_commands.json: the indexed
    # (H) target, a conventional build/ subdir, then walking up to the repo root.
    start = start.resolve()
    seen: set[Path] = set()
    for candidate in (start, start / _BUILD_DIR, *start.parents):
        if candidate in seen:
            continue
        seen.add(candidate)
        if (candidate / _COMPILE_COMMANDS).is_file():
            return candidate
    return None


def _base_simple_name(spelling: str) -> str:
    flat = spelling.replace(cs.SEPARATOR_DOUBLE_COLON, cs.SEPARATOR_DOT)
    return flat.rsplit(cs.SEPARATOR_DOT, 1)[-1]


def _classify(cursor: Cursor) -> str | None:
    kind = cursor.kind.name
    if kind in fc.CLASS_KIND_NAMES:
        return fc.LABEL_CLASS
    if kind in fc.TYPE_KIND_NAMES:
        return fc.LABEL_TYPE
    if kind in fc.METHOD_KIND_NAMES:
        return fc.LABEL_METHOD
    if kind in fc.FUNCTION_KIND_NAMES:
        parent = cursor.semantic_parent
        if parent is not None and parent.kind.name in fc.CLASS_KIND_NAMES:
            return fc.LABEL_METHOD
        return fc.LABEL_FUNCTION
    return None


class _Collector:
    def __init__(
        self,
        resolver: CppQnResolver,
        function_registry: FunctionRegistryTrieProtocol | None = None,
        simple_name_lookup: SimpleNameLookup | None = None,
        structural_elements: dict[Path, str | None] | None = None,
    ) -> None:
        self.resolver = resolver
        self.function_registry = function_registry
        self.simple_name_lookup = simple_name_lookup
        self.structural_elements = structural_elements
        self.nodes: dict[_NodeKey, tuple[str, PropertyDict, bool]] = {}
        self.modules: dict[str, PropertyDict] = {}
        self.edges: set[_EdgeKey] = set()
        self.covered: set[str] = set()
        # (H) absolute file name -> (rel, module_qn), or None when outside the
        # (H) repo. rel_path resolves symlinks (filesystem-touching); headers
        # (H) recur across every TU that includes them, so resolve each once.
        self._include_file_info: dict[str, tuple[str, str] | None] = {}
        # (H) (use-site rel, use-site line, macro Function qn, use-site absolute
        # (H) file): macro cursors are TU-level preprocessing entities, so the
        # (H) enclosing caller is only recoverable by span once ALL definitions
        # (H) are collected -- resolved at flush.
        self._pending_macro_calls: set[tuple[str, int, str, str]] = set()

    def _node_props(self, cursor: Cursor, qn: str, name: str, rel: str) -> PropertyDict:
        return {
            cs.KEY_QUALIFIED_NAME: qn,
            cs.KEY_NAME: name,
            cs.KEY_DECORATORS: [],
            cs.KEY_START_LINE: cursor.location.line,
            cs.KEY_END_LINE: cursor.extent.end.line,
            cs.KEY_DOCSTRING: None,
            cs.KEY_IS_EXPORTED: False,
            cs.KEY_PATH: rel,
            cs.KEY_ABSOLUTE_PATH: Path(cursor.location.file.name).resolve().as_posix(),
        }

    def _add_node(self, label: str, qn: str, props: PropertyDict, is_def: bool) -> None:
        key: _NodeKey = (label, qn)
        existing = self.nodes.get(key)
        # (H) Prefer the definition cursor's properties (its span is the accurate
        # (H) one) over a mere declaration's, matching cgr where the deferred
        # (H) out-of-line definition is ingested last and wins the MERGE.
        if existing is None or (is_def and not existing[2]):
            self.nodes[key] = (label, props, is_def)

    def _add_module(self, module_qn: str, rel: str, absolute_file: str) -> None:
        if module_qn in self.modules:
            return
        self.modules[module_qn] = {
            cs.KEY_QUALIFIED_NAME: module_qn,
            cs.KEY_NAME: Path(rel).name,
            cs.KEY_PATH: rel,
            cs.KEY_ABSOLUTE_PATH: Path(absolute_file).resolve().as_posix(),
        }

    def _add_edge(
        self, rel_type: str, from_label: str, from_qn: str, to_label: str, to_qn: str
    ) -> None:
        self.edges.add((rel_type, from_label, from_qn, to_label, to_qn))

    def process(self, cursor: Cursor, enclosing: _Scope) -> _Scope:
        # (H) Returns the scope its subtree should attribute calls to: the node's
        # (H) own (label, qn) when it is a function/method, else the unchanged
        # (H) enclosing scope.
        if cursor.kind.name == fc.KIND_CALL_EXPR:
            self._process_call(cursor, enclosing)
            return None
        if cursor.kind.name == fc.KIND_MACRO_DEFINITION:
            self._process_macro_definition(cursor)
            return None
        if cursor.kind.name == fc.KIND_MACRO_INSTANTIATION:
            self._queue_macro_call(cursor)
            return None
        label = _classify(cursor)
        if label is None or cursor.location.file is None:
            return None
        if label == fc.LABEL_CLASS and not cursor.is_definition():
            return None  # (H) forward declarations are not nodes
        rel = self.resolver.rel_path(cursor.location.file.name)
        module_qn = self.resolver.module_qn(cursor.location.file.name)
        if rel is None or module_qn is None:
            return None  # (H) outside the indexed repo (system headers, etc.)

        if label == fc.LABEL_METHOD:
            return self._process_method(cursor, rel)
        if label == fc.LABEL_TYPE:
            self._process_type(cursor, rel, module_qn)
            return None

        qn = (
            self.resolver.class_qn(cursor)
            if label == fc.LABEL_CLASS
            else self.resolver.function_qn(cursor)
        )
        if qn is None:
            return None
        self.covered.add(rel)
        self._add_module(module_qn, rel, cursor.location.file.name)
        self._add_node(
            label,
            qn,
            self._node_props(cursor, qn, cursor.spelling, rel),
            cursor.is_definition(),
        )
        self._add_edge(
            cs.RelationshipType.DEFINES, fc.LABEL_MODULE, module_qn, label, qn
        )
        if label == fc.LABEL_CLASS:
            self._emit_inheritance(cursor, qn)
            return None
        return (label, qn)

    def _process_method(self, cursor: Cursor, rel: str) -> _Scope:
        qn = self.resolver.method_qn(cursor)
        parent = cursor.semantic_parent
        if qn is None or parent is None:
            return None
        class_qn = self.resolver.class_qn(parent)
        if class_qn is None:
            return None
        self.covered.add(rel)
        name = self.resolver.member_name(cursor)
        self._add_node(
            fc.LABEL_METHOD,
            qn,
            self._node_props(cursor, qn, name, rel),
            cursor.is_definition(),
        )
        self._add_edge(
            cs.RelationshipType.DEFINES_METHOD,
            fc.LABEL_CLASS,
            class_qn,
            fc.LABEL_METHOD,
            qn,
        )
        return (fc.LABEL_METHOD, qn)

    def _process_type(self, cursor: Cursor, rel: str, module_qn: str) -> None:
        # (H) A `using`/`typedef` alias becomes a Type node, DEFINED by its
        # (H) enclosing Class (member alias) or its Module (namespace/file scope),
        # (H) matching the tree-sitter alias path and Go/Rust type decls.
        qn = self.resolver.type_qn(cursor)
        if qn is None:
            return
        self.covered.add(rel)
        self._add_module(module_qn, rel, cursor.location.file.name)
        self._add_node(
            fc.LABEL_TYPE,
            qn,
            self._node_props(cursor, qn, cursor.spelling, rel),
            cursor.is_definition(),
        )
        parent = cursor.semantic_parent
        if parent is not None and parent.kind.name in fc.CLASS_KIND_NAMES:
            class_qn = self.resolver.class_qn(parent)
            if class_qn is not None:
                self._add_edge(
                    cs.RelationshipType.DEFINES,
                    fc.LABEL_CLASS,
                    class_qn,
                    fc.LABEL_TYPE,
                    qn,
                )
                return
        self._add_edge(
            cs.RelationshipType.DEFINES, fc.LABEL_MODULE, module_qn, fc.LABEL_TYPE, qn
        )

    def _process_call(self, cursor: Cursor, enclosing: _Scope) -> None:
        # (H) Resolve the callee semantically via cursor.referenced (libclang did
        # (H) the overload/name resolution already), preferring its definition so
        # (H) the edge targets the node the frontend emitted for the body.
        referenced = cursor.referenced
        if referenced is None:
            return
        callee = referenced.get_definition() or referenced
        callee_label = _classify(callee)
        if callee_label is None or callee_label == fc.LABEL_CLASS:
            return
        callee_qn = (
            self.resolver.method_qn(callee)
            if callee_label == fc.LABEL_METHOD
            else self.resolver.function_qn(callee)
        )
        if callee_qn is None:
            return  # (H) callee outside the indexed repo (stdlib, etc.)
        caller = enclosing or self._module_caller(cursor)
        if caller is None:
            return
        caller_label, caller_qn = caller
        self._add_edge(
            cs.RelationshipType.CALLS, caller_label, caller_qn, callee_label, callee_qn
        )

    def _module_caller(self, cursor: Cursor) -> _Scope:
        # (H) A call with no enclosing function/method runs at module load time
        # (H) (a default member initializer or a file/namespace-scope global
        # (H) initializer); the tree-sitter path attributes these to the Module,
        # (H) so mirror that instead of dropping the edge. The call site must be
        # (H) inside the indexed repo (module_qn is None for system headers).
        if cursor.location.file is None:
            return None
        file_name = cursor.location.file.name
        module_qn = self.resolver.module_qn(file_name)
        rel = self.resolver.rel_path(file_name)
        if module_qn is None or rel is None:
            return None
        self._add_module(module_qn, rel, file_name)
        return (fc.LABEL_MODULE, module_qn)

    def _macro_function_qn(self, cursor: Cursor) -> str | None:
        # (H) Macros register as Function nodes (the cross-language decision:
        # (H) C/C++/Rust macros all map onto Function). Builtins and
        # (H) command-line macros have no file; system-header macros resolve
        # (H) outside the repo; an empty-bodied object-like macro (include
        # (H) guard, feature flag -- extent covers only the name) is a flag,
        # (H) not a callable.
        if cursor.location.file is None:
            return None
        extent = cursor.extent
        if (
            extent.start.line == extent.end.line
            and extent.end.column - extent.start.column == len(cursor.spelling)
        ):
            return None
        return self.resolver.function_qn(cursor)

    def _process_macro_definition(self, cursor: Cursor) -> None:
        # (H) Guard order matters for reachability: builtins/command-line
        # (H) macros (no file) first, system-header macros (outside the repo)
        # (H) second, THEN the shared eligibility check (whose remaining filter
        # (H) here is the empty-body one -- include guards, feature flags).
        if cursor.location.file is None:
            return
        file_name = cursor.location.file.name
        rel = self.resolver.rel_path(file_name)
        module_qn = self.resolver.module_qn(file_name)
        if rel is None or module_qn is None:
            return
        qn = self._macro_function_qn(cursor)
        if qn is None:
            return
        self.covered.add(rel)
        self._add_module(module_qn, rel, file_name)
        props = self._node_props(cursor, qn, cursor.spelling, rel)
        props[cs.KEY_IS_MACRO] = True
        self._add_node(fc.LABEL_FUNCTION, qn, props, True)
        self._add_edge(
            cs.RelationshipType.DEFINES,
            fc.LABEL_MODULE,
            module_qn,
            fc.LABEL_FUNCTION,
            qn,
        )

    def _queue_macro_call(self, cursor: Cursor) -> None:
        # (H) MACRO_INSTANTIATION.referenced is the exact MACRO_DEFINITION
        # (H) (libclang resolved it); the caller needs span containment over the
        # (H) full node set, so defer to flush.
        if cursor.location.file is None:
            return
        referenced = cursor.referenced
        if referenced is None:
            return
        callee_qn = self._macro_function_qn(referenced)
        if callee_qn is None:
            return
        file_name = cursor.location.file.name
        rel = self.resolver.rel_path(file_name)
        if rel is None:
            return
        self._pending_macro_calls.add((rel, cursor.location.line, callee_qn, file_name))

    def _resolve_macro_calls(self) -> None:
        # (H) Attribute each macro use to the tightest enclosing
        # (H) function/method span in its file (macro cursors are TU children,
        # (H) never AST-nested); a use outside any span is a module-load-time
        # (H) expansion -> the Module, mirroring _module_caller.
        spans: dict[str, list[tuple[int, int, str, str]]] = {}
        for label, props, _ in self.nodes.values():
            if label not in (fc.LABEL_FUNCTION, fc.LABEL_METHOD):
                continue
            path = props.get(cs.KEY_PATH)
            qn = props.get(cs.KEY_QUALIFIED_NAME)
            start = props.get(cs.KEY_START_LINE)
            end = props.get(cs.KEY_END_LINE)
            if (
                isinstance(path, str)
                and isinstance(qn, str)
                and isinstance(start, int)
                and isinstance(end, int)
            ):
                spans.setdefault(path, []).append((start, end, label, qn))
        for rel, line, callee_qn, file_name in sorted(self._pending_macro_calls):
            containing = [
                s
                for s in spans.get(rel, ())
                if s[0] <= line <= s[1] and s[3] != callee_qn
            ]
            if containing:
                _, _, caller_label, caller_qn = min(
                    containing, key=lambda s: s[1] - s[0]
                )
            else:
                module_qn = self.resolver.module_qn_for_rel(rel)
                if module_qn is None:
                    continue
                self._add_module(module_qn, rel, file_name)
                caller_label, caller_qn = fc.LABEL_MODULE, module_qn
            self._add_edge(
                cs.RelationshipType.CALLS,
                caller_label,
                caller_qn,
                fc.LABEL_FUNCTION,
                callee_qn,
            )

    def process_includes(self, tu) -> None:
        # (H) `#include` is the C++ import: emit IMPORTS Module -> Module for every
        # (H) within-repo inclusion, at any depth (calc.h including util.h counts,
        # (H) attributed to calc.h -- FileInclusion.source is the INCLUDING file).
        # (H) System headers resolve outside the repo (module_qn None) and emit
        # (H) nothing; the source != include guard keeps the tree-sitter path's
        # (H) self-import bug out of the frontend.
        for inclusion in tu.get_includes():
            source = inclusion.source
            included = inclusion.include
            if source is None or included is None:
                continue
            src = self._include_info(source.name)
            inc = self._include_info(included.name)
            if src is None or inc is None or src[1] == inc[1]:
                continue
            src_rel, src_qn = src
            inc_rel, inc_qn = inc
            self._add_module(src_qn, src_rel, source.name)
            self._add_module(inc_qn, inc_rel, included.name)
            self._add_edge(
                cs.RelationshipType.IMPORTS,
                fc.LABEL_MODULE,
                src_qn,
                fc.LABEL_MODULE,
                inc_qn,
            )

    def _include_info(self, file_name: str) -> tuple[str, str] | None:
        # (H) Resolve rel + module_qn together, once per file: rel_path is the
        # (H) filesystem-touching step and module_qn is a map lookup keyed by it.
        if file_name in self._include_file_info:
            return self._include_file_info[file_name]
        rel = self.resolver.rel_path(file_name)
        qn = self.resolver.module_qn_for_rel(rel) if rel is not None else None
        info = (rel, qn) if rel is not None and qn is not None else None
        self._include_file_info[file_name] = info
        return info

    def _emit_inheritance(self, cursor: Cursor, derived_qn: str) -> None:
        for child in cursor.get_children():
            if child.kind.name != fc.KIND_BASE_SPECIFIER:
                continue
            base_decl = child.type.get_declaration()
            base_qn = self.resolver.class_qn(base_decl) if base_decl else None
            if base_qn is None:
                base_qn = _base_simple_name(child.type.spelling)
            self._add_edge(
                cs.RelationshipType.INHERITS,
                fc.LABEL_CLASS,
                derived_qn,
                fc.LABEL_CLASS,
                base_qn,
            )

    def _contains_module_parent(self, rel: str) -> tuple[str, str, str]:
        # (H) Mirror DefinitionProcessor's module-parent choice: a Package if the
        # (H) directory is one, else a Folder, else the Project at the root.
        parent_rel = Path(rel).parent
        package_qn = (
            self.structural_elements.get(parent_rel)
            if self.structural_elements is not None
            else None
        )
        if package_qn:
            return (cs.NodeLabel.PACKAGE, cs.KEY_QUALIFIED_NAME, package_qn)
        if parent_rel != Path(cs.SEPARATOR_DOT):
            return (cs.NodeLabel.FOLDER, cs.KEY_PATH, parent_rel.as_posix())
        return (cs.NodeLabel.PROJECT, cs.KEY_NAME, self.resolver.project_name)

    def _register(self, label: str, props: PropertyDict) -> None:
        if self.function_registry is None:
            return
        qn = props[cs.KEY_QUALIFIED_NAME]
        if not isinstance(qn, str):
            return
        self.function_registry[qn] = NodeType(label)
        name = props[cs.KEY_NAME]
        if self.simple_name_lookup is not None and isinstance(name, str):
            self.simple_name_lookup[name].add(qn)

    def flush(self, ingestor: IngestorProtocol) -> None:
        self._resolve_macro_calls()
        for module_qn, props in self.modules.items():
            ingestor.ensure_node_batch(fc.LABEL_MODULE, props)
            path = props[cs.KEY_PATH]
            if self.structural_elements is not None and isinstance(path, str):
                ingestor.ensure_relationship_batch(
                    self._contains_module_parent(path),
                    cs.RelationshipType.CONTAINS_MODULE,
                    (fc.LABEL_MODULE, cs.KEY_QUALIFIED_NAME, module_qn),
                )
        for label, props, _ in self.nodes.values():
            ingestor.ensure_node_batch(label, props)
            self._register(label, props)
        for rel_type, from_label, from_qn, to_label, to_qn in self.edges:
            ingestor.ensure_relationship_batch(
                (from_label, cs.KEY_QUALIFIED_NAME, from_qn),
                rel_type,
                (to_label, cs.KEY_QUALIFIED_NAME, to_qn),
            )


def _walk(cursor: Cursor, collector: _Collector, enclosing: _Scope = None) -> None:
    for child in cursor.get_children():
        produced = collector.process(child, enclosing)
        _walk(child, collector, produced or enclosing)


def run_cpp_frontend(
    ingestor: IngestorProtocol,
    repo_path: Path,
    project_name: str,
    compdb_dir: Path,
    function_registry: FunctionRegistryTrieProtocol | None = None,
    simple_name_lookup: SimpleNameLookup | None = None,
    structural_elements: dict[Path, str | None] | None = None,
) -> frozenset[str]:
    """Index C/C++ via libclang + a compile_commands.json (macro-accurate).

    Parses every translation unit in the compilation database, walks the cursor
    tree, and emits Module/Class/Function/Method nodes plus DEFINES /
    DEFINES_METHOD / INHERITS edges and exact spans straight to the ingestor,
    synthesizing the same qualified names the tree-sitter path would. Repo macro
    definitions register as Function nodes and macro uses emit CALLS from their
    enclosing function (see the graph-schema docs). Returns the set of
    repo-relative files it covered (so callers can skip them in the tree-sitter
    pass).

    When ``function_registry`` / ``simple_name_lookup`` are supplied, emitted
    definitions are registered for cross-file resolution; when
    ``structural_elements`` is supplied, each Module is linked to its parent via
    CONTAINS_MODULE (the full-replace path used by GraphUpdater).
    """
    import clang.cindex as ci

    resolver = CppQnResolver(repo_path, project_name)
    collector = _Collector(
        resolver, function_registry, simple_name_lookup, structural_elements
    )

    db = ci.CompilationDatabase.fromDirectory(str(Path(compdb_dir).resolve()))
    index = ci.Index.create()
    for command in db.getAllCompileCommands():
        args = list(command.arguments)[1:]
        try:
            # (H) the detailed record exposes MACRO_DEFINITION /
            # (H) MACRO_INSTANTIATION cursors (preprocessing entities are
            # (H) otherwise absent from the cursor tree)
            tu = index.parse(
                None,
                args=args,
                options=ci.TranslationUnit.PARSE_DETAILED_PROCESSING_RECORD,
            )
        except ci.TranslationUnitLoadError:
            continue
        _walk(tu.cursor, collector)
        collector.process_includes(tu)

    collector.flush(ingestor)
    return frozenset(collector.covered)
