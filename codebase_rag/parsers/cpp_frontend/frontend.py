from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from ... import constants as cs
from ...services import IngestorProtocol
from ...types_defs import (
    FunctionRegistryTrieProtocol,
    NodeType,
    PendingExpansionCall,
    PendingMacroCall,
    PropertyDict,
    SimpleNameLookup,
)
from ...utils.path_utils import cached_resolve_posix
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
    # Discover the directory holding a compile_commands.json: the indexed
    # target, then each ancestor, checking the conventional build/ subdir
    # beside every level, so indexing a subdirectory (nlohmann's
    # include/nlohmann) still finds the repo root's build/.
    start = start.resolve()
    seen: set[Path] = set()
    for level in (start, *start.parents):
        for candidate in (level, level / _BUILD_DIR):
            if candidate in seen:
                continue
            seen.add(candidate)
            if (candidate / _COMPILE_COMMANDS).is_file():
                return candidate
    return None


def _base_simple_name(spelling: str) -> str:
    flat = spelling.replace(cs.SEPARATOR_DOUBLE_COLON, cs.SEPARATOR_DOT)
    return flat.rsplit(cs.SEPARATOR_DOT, 1)[-1]


def _has_internal_linkage(cursor: Cursor) -> bool:
    # Internal linkage (`static`, anonymous namespace): libclang's linkage
    # kind covers both uniformly. Some cursor kinds raise on the property in
    # older libclangs, so fail open (treated as external).
    try:
        return cursor.linkage.name == "INTERNAL"
    except Exception:
        return False


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
        hybrid: bool = False,
    ) -> None:
        self.resolver = resolver
        # Hybrid mode: tree-sitter remains the backbone (its definitions and
        # CALLS stand), so collect ONLY the facts libclang is uniquely right
        # about: macro definitions/uses and includes. Definition qns diverge
        # from tree-sitter's wherever macros hide namespaces, so no definition
        # node may be emitted; macro and Module qns are scheme-identical and
        # safe.
        self.hybrid = hybrid
        self.function_registry = function_registry
        self.simple_name_lookup = simple_name_lookup
        self.structural_elements = structural_elements
        self.nodes: dict[_NodeKey, tuple[str, PropertyDict, bool]] = {}
        self.modules: dict[str, PropertyDict] = {}
        self.edges: set[_EdgeKey] = set()
        self.covered: set[str] = set()
        # absolute file name -> (rel, module_qn), or None when outside the
        # repo. rel_path resolves symlinks (filesystem-touching); headers
        # recur across every TU that includes them, so resolve each once.
        self._include_file_info: dict[str, tuple[str, str] | None] = {}
        # (use-site rel, use-site line, macro Function qn, use-site absolute
        # file): macro cursors are TU-level preprocessing entities, so the
        # enclosing caller is only recoverable by span once ALL definitions are
        # collected; resolved at flush.
        self._pending_macro_calls: set[tuple[str, int, str, str]] = set()
        # {macro qn: identifier tokens in its definition body}: a macro
        # expanded only inside another macro's body is a NESTED expansion the
        # preprocessing record never reports, so the body reference is the only
        # evidence of use; resolved to macro -> macro CALLS at flush, once every
        # macro node is known.
        self._macro_body_refs: dict[str, set[str]] = {}
        # {absolute file: [(sl, sc, el, ec)]} macro instantiation extents: a
        # CALL_EXPR whose own start lies inside one was produced by the
        # expansion (its text lives in the macro body), so tree-sitter never
        # sees it, the one call class hybrid must emit itself.
        # Preprocessing-record cursors precede the AST among TU children, so
        # within a TU every instantiation is recorded before any CALL_EXPR at
        # its site is visited.
        self._instantiation_extents: dict[str, list[tuple[int, int, int, int]]] = {}
        # (caller rel, caller line, callee USR, callee rel, callee line):
        # both ends join to tree-sitter definition spans after Pass 2.
        self._pending_expansion_calls: set[tuple[str, int, str, str, int]] = set()
        # {USR: (rel, line)} of every in-repo function/method DEFINITION seen
        # across all TUs: a cross-TU callee's get_definition() is None in the
        # calling TU (only the header declaration is visible), but another TU
        # parses the definition; prefer its location so the span join targets
        # the definition node, not the prototype.
        self._usr_definitions: dict[str, tuple[str, int]] = {}
        # Function node keys with INTERNAL linkage (`static`, anonymous
        # namespace): each TU owns a separate function, so the prototype
        # dedupe never drops them.
        self._internal_linkage_keys: set[_NodeKey] = set()

    def _node_props(self, cursor: Cursor, qn: str, name: str, rel: str) -> PropertyDict:
        return {
            cs.KEY_QUALIFIED_NAME: qn,
            cs.KEY_NAME: name,
            cs.KEY_MODIFIERS: [],
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
        # Prefer the definition cursor's properties (its span is the accurate
        # one) over a mere declaration's, matching cgr where the deferred
        # out-of-line definition is ingested last and wins the MERGE.
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
        # Returns the scope its subtree should attribute calls to: the node's
        # own (label, qn) when it is a function/method, else the unchanged
        # enclosing scope.
        if cursor.kind.name == fc.KIND_MACRO_DEFINITION:
            self._process_macro_definition(cursor)
            return None
        if cursor.kind.name == fc.KIND_MACRO_INSTANTIATION:
            self._queue_macro_call(cursor)
            if self.hybrid:
                self._record_instantiation_extent(cursor)
            return None
        if self.hybrid:
            self._process_hybrid(cursor)
            return None
        if cursor.kind.name == fc.KIND_CALL_EXPR:
            self._process_call(cursor, enclosing)
            return None
        return self._process_definition(cursor)

    def _process_definition(self, cursor: Cursor) -> _Scope:
        label = _classify(cursor)
        if label is None or cursor.location.file is None:
            return None
        if label == fc.LABEL_CLASS and not cursor.is_definition():
            return None  # forward declarations are not nodes
        rel = self.resolver.rel_path(cursor.location.file.name)
        module_qn = self.resolver.module_qn(cursor.location.file.name)
        if rel is None or module_qn is None:
            return None  # outside the indexed repo (system headers, etc.)

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
        if label == fc.LABEL_FUNCTION and _has_internal_linkage(cursor):
            self._internal_linkage_keys.add((label, qn))
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
        # A `using`/`typedef` alias becomes a Type node, DEFINED by its
        # enclosing Class (member alias) or its Module (namespace/file scope),
        # matching the tree-sitter alias path and Go/Rust type decls.
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
        # Resolve the callee semantically via cursor.referenced (libclang did
        # the overload/name resolution already), preferring its definition so
        # the edge targets the node the frontend emitted for the body.
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
            return  # callee outside the indexed repo (stdlib, etc.)
        caller = enclosing or self._module_caller(cursor)
        if caller is None:
            return
        caller_label, caller_qn = caller
        self._add_edge(
            cs.RelationshipType.CALLS, caller_label, caller_qn, callee_label, callee_qn
        )

    def _module_caller(self, cursor: Cursor) -> _Scope:
        # A call with no enclosing function/method runs at module load time
        # (a default member initializer or a file/namespace-scope global
        # initializer); the tree-sitter path attributes these to the Module,
        # so mirror that. The call site must be inside the indexed repo
        # (module_qn is None for system headers).
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
        # Macros register as Function nodes (the cross-language decision:
        # C/C++/Rust macros all map onto Function). Builtins and command-line
        # macros have no file; system-header macros resolve outside the repo;
        # an empty-bodied object-like macro (include guard or feature flag,
        # extent covers only the name) is a flag, not a callable.
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
        # Guard order matters for reachability: builtins/command-line macros
        # (no file) first, system-header macros (outside the repo) second,
        # THEN the shared eligibility check (whose remaining filter here is the
        # empty-body one: include guards, feature flags).
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
        # Body tokens after the macro's own name. A function-like macro ('('
        # abutting the name, the standard's distinction from an object-like
        # body that starts with a parenthesis) has its parameter list skipped
        # and those names excluded from the body: a parameter is substituted by
        # the caller's argument, so one named like a real macro is not a
        # reference to it.
        tokens = list(cursor.get_tokens())
        body_start = 1
        params: set[str] = set()
        name_end = tokens[0].extent.end
        if (
            len(tokens) > 1
            and tokens[1].spelling == fc.TOKEN_LPAREN
            and tokens[1].extent.start.line == name_end.line
            and tokens[1].extent.start.column == name_end.column
        ):
            body_start = len(tokens)
            for i, tok in enumerate(tokens[2:], start=2):
                if tok.spelling == fc.TOKEN_RPAREN:
                    body_start = i + 1
                    break
                if tok.spelling.isidentifier():
                    params.add(tok.spelling)
        refs = {
            t.spelling
            for t in tokens[body_start:]
            if t.spelling.isidentifier()
            and t.spelling != cursor.spelling
            and t.spelling not in params
        }
        if refs:
            self._macro_body_refs.setdefault(qn, set()).update(refs)

    def _record_instantiation_extent(self, cursor: Cursor) -> None:
        if cursor.location.file is None:
            return
        extent = cursor.extent
        self._instantiation_extents.setdefault(cursor.location.file.name, []).append(
            (
                extent.start.line,
                extent.start.column,
                extent.end.line,
                extent.end.column,
            )
        )

    def _inside_instantiation(self, cursor: Cursor) -> bool:
        # Containment of the cursor's OWN start position: a call whose
        # callee is written in the source but takes a macro argument
        # (`foo(MY_CONST)`) starts BEFORE the instantiation extent and is
        # tree-sitter's; one starting inside it was produced by expansion.
        loc = cursor.location
        if loc.file is None:
            return False
        pos = (loc.line, loc.column)
        return any(
            (sl, sc) <= pos <= (el, ec)
            for sl, sc, el, ec in self._instantiation_extents.get(loc.file.name, ())
        )

    def _process_hybrid(self, cursor: Cursor) -> None:
        # The hybrid subset: expansion-produced calls, scope-safe type aliases,
        # and definition LOCATIONS (for cross-TU callee joins). Everything else
        # emits definition nodes or CALLS with libclang-scheme qns,
        # tree-sitter's territory in hybrid.
        if cursor.kind.name == fc.KIND_CALL_EXPR:
            self._queue_expansion_call(cursor)
            return
        match _classify(cursor):
            case fc.LABEL_TYPE:
                self._process_hybrid_type_alias(cursor)
            case fc.LABEL_FUNCTION | fc.LABEL_METHOD:
                self._record_usr_definition(cursor)

    def _record_usr_definition(self, cursor: Cursor) -> None:
        # No node is emitted: only the definition's location is kept so a
        # cross-TU expansion callee can join to the definition's span.
        if not cursor.is_definition() or cursor.location.file is None:
            return
        rel = self.resolver.rel_path(cursor.location.file.name)
        if rel is None:
            return
        usr = cursor.get_usr()
        if usr:
            self._usr_definitions[usr] = (rel, cursor.location.line)

    def _queue_expansion_call(self, cursor: Cursor) -> None:
        # Only expansion-produced calls: everything else is tree-sitter's.
        # Both ends are kept as locations; qns are joined to tree-sitter spans
        # after Pass 2 (libclang's own qns are wrong-scheme wherever macros
        # hide namespaces, which is exactly where macros live).
        if not self._inside_instantiation(cursor):
            return
        referenced = cursor.referenced
        if referenced is None:
            return
        callee = referenced.get_definition() or referenced
        if _classify(callee) not in (fc.LABEL_FUNCTION, fc.LABEL_METHOD):
            return
        if callee.location.file is None:
            return
        callee_rel = self.resolver.rel_path(callee.location.file.name)
        caller_rel = self.resolver.rel_path(cursor.location.file.name)
        if callee_rel is None or caller_rel is None:
            return
        self._pending_expansion_calls.add(
            (
                caller_rel,
                cursor.location.line,
                callee.get_usr() or "",
                callee_rel,
                callee.location.line,
            )
        )

    def _process_hybrid_type_alias(self, cursor: Cursor) -> None:
        # tree-sitter emits no Type nodes for C++ using/typedef at all, so
        # namespace/file-scope aliases are a pure addition with a
        # Module-anchored DEFINES (Module qns are scheme-identical). A MEMBER
        # alias would anchor to a libclang-scheme Class qn, a phantom node in
        # hybrid, so it is skipped.
        if cursor.location.file is None:
            return
        parent = cursor.semantic_parent
        if parent is not None and parent.kind.name in fc.CLASS_KIND_NAMES:
            return
        rel = self.resolver.rel_path(cursor.location.file.name)
        module_qn = self.resolver.module_qn(cursor.location.file.name)
        if rel is None or module_qn is None:
            return
        self._process_type(cursor, rel, module_qn)

    def _queue_macro_call(self, cursor: Cursor) -> None:
        # MACRO_INSTANTIATION.referenced is the exact MACRO_DEFINITION
        # (libclang resolved it); the caller needs span containment over the
        # full node set, so defer to flush.
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
        # Attribute each macro use to the tightest enclosing
        # function/method span in its file (macro cursors are TU children,
        # never AST-nested); a use outside any span is a module-load-time
        # expansion -> the Module, mirroring _module_caller.
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
        # `#include` is the C++ import: emit IMPORTS Module -> Module for every
        # within-repo inclusion, at any depth (calc.h including util.h counts,
        # attributed to calc.h, since FileInclusion.source is the INCLUDING
        # file). System headers resolve outside the repo (module_qn None) and
        # emit nothing; the source != include guard keeps the tree-sitter
        # path's self-import bug out of the frontend.
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
        # Resolve rel + module_qn together, once per file: rel_path is the
        # filesystem-touching step and module_qn is a map lookup keyed by it.
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
        # Mirror DefinitionProcessor's module-parent choice: a Package if the
        # directory is one, else a Folder, else the Project at the root.
        parent_rel = Path(rel).parent
        package_qn = (
            self.structural_elements.get(parent_rel)
            if self.structural_elements is not None
            else None
        )
        if package_qn:
            return (cs.NodeLabel.PACKAGE, cs.KEY_QUALIFIED_NAME, package_qn)
        if parent_rel != Path(cs.SEPARATOR_DOT):
            # Folder identity is the absolute path (issue #897).
            return (
                cs.NodeLabel.FOLDER,
                cs.KEY_ABSOLUTE_PATH,
                cached_resolve_posix(self.resolver.repo_path / parent_rel),
            )
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

    def _resolve_macro_body_refs(self) -> None:
        # Macros share one global, unscoped namespace, so match body
        # identifiers by simple name; a name defined in several files gets
        # an edge to each candidate (the duplicate-qn CALLS-to-both rule).
        # No self-loop is possible: a macro's collected refs exclude its
        # own spelling, and qn equality would imply name equality.
        macros_by_name: dict[str, list[str]] = {}
        for label, props, _ in self.nodes.values():
            name = props.get(cs.KEY_NAME)
            qn = props.get(cs.KEY_QUALIFIED_NAME)
            if (
                props.get(cs.KEY_IS_MACRO)
                and isinstance(name, str)
                and isinstance(qn, str)
            ):
                macros_by_name.setdefault(name, []).append(qn)
        for macro_qn, refs in self._macro_body_refs.items():
            for name in refs:
                for target_qn in macros_by_name.get(name, ()):
                    self._add_edge(
                        cs.RelationshipType.CALLS,
                        fc.LABEL_FUNCTION,
                        macro_qn,
                        fc.LABEL_FUNCTION,
                        target_qn,
                    )

    def pending_expansion_calls(self) -> list[PendingExpansionCall]:
        # Same deferral as pending_macro_calls: the Module fallback is
        # pre-resolved for the caller end; a caller file with no module qn
        # can never carry an edge.
        pending: list[PendingExpansionCall] = []
        for caller_rel, caller_line, usr, callee_rel, callee_line in sorted(
            self._pending_expansion_calls
        ):
            module_qn = self.resolver.module_qn_for_rel(caller_rel)
            if module_qn is None:
                continue
            # Prefer the DEFINITION's location (recorded from whichever TU
            # parsed it) over the declaration the calling TU could see.
            callee_rel, callee_line = self._usr_definitions.get(
                usr, (callee_rel, callee_line)
            )
            pending.append(
                PendingExpansionCall(
                    caller_rel, caller_line, callee_rel, callee_line, module_qn
                )
            )
        return pending

    def pending_macro_calls(self) -> list[PendingMacroCall]:
        # Hybrid: callers are unknowable until the tree-sitter pass has
        # recorded its definition spans, so hand the uses back with the
        # Module fallback pre-resolved. A use site whose file has no
        # module qn (an ignored dir, e.g. build/) can never carry an edge.
        pending: list[PendingMacroCall] = []
        for rel, line, callee_qn, _file_name in sorted(self._pending_macro_calls):
            module_qn = self.resolver.module_qn_for_rel(rel)
            if module_qn is None:
                continue
            pending.append(PendingMacroCall(rel, line, callee_qn, module_qn))
        return pending

    def flush(self, ingestor: IngestorProtocol) -> None:
        self._resolve_macro_body_refs()
        if not self.hybrid:
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
        dropped = self._duplicate_prototype_keys()
        for key, (label, props, _) in self.nodes.items():
            if key in dropped:
                continue
            ingestor.ensure_node_batch(label, props)
            self._register(label, props)
        for rel_type, from_label, from_qn, to_label, to_qn in self.edges:
            if (from_label, from_qn) in dropped or (to_label, to_qn) in dropped:
                continue
            ingestor.ensure_relationship_batch(
                (from_label, cs.KEY_QUALIFIED_NAME, from_qn),
                rel_type,
                (to_label, cs.KEY_QUALIFIED_NAME, to_qn),
            )

    def _duplicate_prototype_keys(self) -> set[_NodeKey]:
        # A free-function PROTOTYPE node duplicating a bodied definition in
        # another file (utils.h.FreeHelper beside utils.FreeHelper) has zero
        # incoming edges forever, so it is dropped along with its edges,
        # mirroring the tree-sitter pass (issue #893). Namespace-qualified
        # comparison: the longest matching module prefix is stripped so the
        # header's extension segment never defeats the match.
        module_qns = sorted(self.modules.keys(), key=len, reverse=True)

        def ns_of(qn: str) -> str:
            for module_qn in module_qns:
                if qn.startswith(f"{module_qn}{cs.SEPARATOR_DOT}"):
                    return qn[len(module_qn) + 1 :]
            return qn

        defined = {
            ns_of(qn)
            for (label, qn), (_, _, is_def) in self.nodes.items()
            if label == fc.LABEL_FUNCTION and is_def
        }
        return {
            (label, qn)
            for (label, qn), (_, _, is_def) in self.nodes.items()
            if label == fc.LABEL_FUNCTION
            and not is_def
            and (label, qn) not in self._internal_linkage_keys
            and ns_of(qn) in defined
        }


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
    collector = _Collector(
        CppQnResolver(repo_path, project_name),
        function_registry,
        simple_name_lookup,
        structural_elements,
    )
    _parse_and_collect(collector, compdb_dir)
    collector.flush(ingestor)
    return frozenset(collector.covered)


def run_cpp_frontend_hybrid(
    ingestor: IngestorProtocol,
    repo_path: Path,
    project_name: str,
    compdb_dir: Path,
    function_registry: FunctionRegistryTrieProtocol | None = None,
    simple_name_lookup: SimpleNameLookup | None = None,
    structural_elements: dict[Path, str | None] | None = None,
) -> tuple[list[PendingMacroCall], list[PendingExpansionCall]]:
    """Layer libclang's macro, alias, and include facts onto a tree-sitter index.

    Parses every translation unit like :func:`run_cpp_frontend` but emits ONLY
    macro Function nodes (with their Module DEFINES), namespace/file-scope Type
    aliases, and ``#include`` IMPORTS edges -- the facts whose qns are
    scheme-identical between libclang and tree-sitter or that tree-sitter does
    not model at all. No definition nodes and no direct CALLS are emitted:
    tree-sitter remains the backbone and covers every file, so nothing is
    skipped. Returns the macro uses and the expansion-produced calls it saw;
    the caller joins each to tree-sitter definition spans after Pass 2.
    """
    collector = _Collector(
        CppQnResolver(repo_path, project_name),
        function_registry,
        simple_name_lookup,
        structural_elements,
        hybrid=True,
    )
    _parse_and_collect(collector, compdb_dir)
    collector.flush(ingestor)
    return collector.pending_macro_calls(), collector.pending_expansion_calls()


def _parse_and_collect(collector: _Collector, compdb_dir: Path) -> None:
    import clang.cindex as ci

    db = ci.CompilationDatabase.fromDirectory(str(Path(compdb_dir).resolve()))
    index = ci.Index.create()
    for command in db.getAllCompileCommands():
        args = list(command.arguments)[1:]
        try:
            # the detailed record exposes MACRO_DEFINITION /
            # MACRO_INSTANTIATION cursors (preprocessing entities are
            # otherwise absent from the cursor tree)
            tu = index.parse(
                None,
                args=args,
                options=ci.TranslationUnit.PARSE_DETAILED_PROCESSING_RECORD,
            )
        except ci.TranslationUnitLoadError:
            continue
        _walk(tu.cursor, collector)
        collector.process_includes(tu)
