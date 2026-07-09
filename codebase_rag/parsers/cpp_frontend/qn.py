from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

from ... import constants as cs
from ...utils.path_utils import should_skip_rel_file
from ..cpp.utils import convert_operator_symbol_to_name
from . import constants as fc

if TYPE_CHECKING:
    from clang.cindex import Cursor


def _eligible_rel_files(repo_path: Path) -> list[str]:
    # (H) Reproduce GraphUpdater._collect_eligible_files' ordering exactly: an
    # (H) os.walk with dirnames AND filenames sorted, top-down. The module-qn
    # (H) disambiguation below depends on this order (the file processed LATER in
    # (H) a basename collision is the one that gets its extension appended), so it
    # (H) must match cgr's tree-sitter pass to produce identical qualified names.
    repo_str = str(repo_path)
    repo_prefix_len = len(repo_str) + 1
    rels: list[str] = []
    for dirpath, dirnames, filenames in os.walk(repo_str):
        rel_dir = "" if len(dirpath) < repo_prefix_len else dirpath[repo_prefix_len:]
        rel_dir = rel_dir.replace(os.sep, "/")
        dir_parts = tuple(rel_dir.split("/")) if rel_dir else ()
        dir_prefix = f"{rel_dir}/" if rel_dir else ""
        dirnames[:] = sorted(dirnames)
        for fname in sorted(filenames):
            dot = fname.rfind(".")
            suffix = fname[dot:] if dot != -1 else ""
            rel_path_str = f"{dir_prefix}{fname}"
            if not should_skip_rel_file(rel_path_str, dir_parts, suffix):
                rels.append(rel_path_str)
    return rels


def _base_module_qn(rel: str, project_name: str) -> str:
    rel_path = Path(rel)
    if rel_path.name in (cs.INIT_PY, cs.MOD_RS):
        parts = rel_path.parent.parts
    else:
        parts = rel_path.with_suffix("").parts
    return cs.SEPARATOR_DOT.join([project_name, *parts])


def build_module_qn_map(repo_path: Path, project_name: str) -> dict[str, str]:
    # (H) Mirror DefinitionProcessor._disambiguate_module_qn: a base qn is claimed
    # (H) by the first file (in walk order); a later file colliding on that base qn
    # (H) gets its extension appended (foo.cpp -> proj.foo, foo.h -> proj.foo.h).
    claimed: dict[str, str] = {}
    result: dict[str, str] = {}
    for rel in _eligible_rel_files(repo_path):
        base = _base_module_qn(rel, project_name)
        existing = claimed.get(base)
        if existing is None or existing == rel:
            final = base
        else:
            suffix = Path(rel).suffix.lstrip(cs.SEPARATOR_DOT)
            final = f"{base}{cs.SEPARATOR_DOT}{suffix}"
        claimed.setdefault(final, rel)
        result[rel] = final
    return result


class CppQnResolver:
    """Synthesizes cgr-correct qualified names for libclang cursors.

    The qns must be byte-identical to what the tree-sitter C++ path produces
    (parsers/cpp/utils.build_qualified_name + the deferred out-of-class method
    resolver), because the whole graph keys on them.
    """

    def __init__(self, repo_path: Path, project_name: str) -> None:
        self.repo_path = repo_path.resolve()
        self.project_name = project_name
        self._module_qn = build_module_qn_map(self.repo_path, project_name)

    def rel_path(self, absolute_file: str) -> str | None:
        try:
            return Path(absolute_file).resolve().relative_to(self.repo_path).as_posix()
        except ValueError:
            return None

    def module_qn(self, absolute_file: str) -> str | None:
        rel = self.rel_path(absolute_file)
        if rel is None:
            return None
        return self._module_qn.get(rel)

    def module_qn_for_rel(self, rel: str) -> str | None:
        # (H) Map lookup only -- for callers that already paid rel_path's
        # (H) filesystem resolution and must not pay it twice.
        return self._module_qn.get(rel)

    def _namespace_chain(self, cursor: Cursor) -> list[str]:
        parts: list[str] = []
        parent = cursor.semantic_parent
        while parent is not None and parent.kind.name == fc.KIND_NAMESPACE:
            if parent.spelling:  # (H) skip anonymous namespaces (no name segment)
                parts.append(parent.spelling)
            parent = parent.semantic_parent
        parts.reverse()
        return parts

    def member_name(self, cursor: Cursor) -> str:
        # (H) Mirror cpp.utils.extract_operator_name / extract_destructor_name:
        # (H) destructors keep their `~Name` spelling, operators map their symbol
        # (H) through CPP_OPERATOR_SYMBOL_MAP; everything else is its plain name.
        spelling = cursor.spelling
        if cursor.kind.name == fc.KIND_DESTRUCTOR:
            return spelling
        if self._is_operator_spelling(spelling):
            symbol = spelling[len(cs.CPP_OPERATOR_TEXT_PREFIX) :].strip()
            return convert_operator_symbol_to_name(symbol)
        return spelling

    @staticmethod
    def _is_operator_spelling(spelling: str) -> bool:
        prefix = cs.CPP_OPERATOR_TEXT_PREFIX
        if not spelling.startswith(prefix):
            return False
        rest = spelling[len(prefix) :]
        # (H) `operator+`, `operator[]`, `operator int` are operators/conversions;
        # (H) an identifier like `operatorState` is not (next char is alnum/_).
        return not rest or not (rest[0].isalnum() or rest[0] == cs.CHAR_UNDERSCORE)

    def class_qn(self, cursor: Cursor) -> str | None:
        if cursor.location.file is None:
            return None
        module_qn = self.module_qn(cursor.location.file.name)
        if module_qn is None:
            return None
        parts = [module_qn, *self._namespace_chain(cursor), cursor.spelling]
        return cs.SEPARATOR_DOT.join(parts)

    def function_qn(self, cursor: Cursor) -> str | None:
        if cursor.location.file is None:
            return None
        module_qn = self.module_qn(cursor.location.file.name)
        if module_qn is None:
            return None
        parts = [module_qn, *self._namespace_chain(cursor), self.member_name(cursor)]
        return cs.SEPARATOR_DOT.join(parts)

    def type_qn(self, cursor: Cursor) -> str | None:
        # (H) A class-scoped `using`/`typedef` is anchored to its enclosing class
        # (H) (e.g. proj.Box.Handle); a namespace/file-scoped one mirrors a free
        # (H) function's qn (module + namespace chain + name).
        parent = cursor.semantic_parent
        if parent is not None and parent.kind.name in fc.CLASS_KIND_NAMES:
            class_qn = self.class_qn(parent)
            if class_qn is None:
                return None
            return cs.SEPARATOR_DOT.join([class_qn, cursor.spelling])
        if cursor.location.file is None:
            return None
        module_qn = self.module_qn(cursor.location.file.name)
        if module_qn is None:
            return None
        parts = [module_qn, *self._namespace_chain(cursor), cursor.spelling]
        return cs.SEPARATOR_DOT.join(parts)

    def method_qn(self, cursor: Cursor) -> str | None:
        # (H) A method's qn is anchored to its CLASS's declaring file (the header),
        # (H) via semantic_parent, NOT the out-of-line definition file. This mirrors
        # (H) cgr's deferred out-of-class method resolver.
        parent = cursor.semantic_parent
        if parent is None:
            return None
        class_qn = self.class_qn(parent)
        if class_qn is None:
            return None
        return cs.SEPARATOR_DOT.join([class_qn, self.member_name(cursor)])
