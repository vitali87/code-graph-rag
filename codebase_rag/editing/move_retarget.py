"""Importer retargeting for the move refactoring."""

from __future__ import annotations

from pathlib import Path

from tree_sitter import Node

from .. import constants as cs
from .. import graph_query
from ..graph_query import QueryFn
from .imports import _JS_SPEC, ImportRewriter, ImportSite, SymbolMove
from .move_scope import _JS_LANGUAGES, _import_block_end, _module_bound, _statement_text
from .patcher import Patcher, PatcherError


class _MoveRetargetMixin:
    repo_root: Path
    fetch_all: QueryFn
    project: str

    def _parse(
        self, path: str, source: bytes
    ) -> tuple[cs.SupportedLanguage | None, Node]: ...

    def _retarget_importers(
        self,
        patcher: Patcher,
        old_module: str,
        old_spelled: str,
        new_spelled: str,
        new_path: str,
        name: str,
        language: cs.SupportedLanguage | None,
    ) -> tuple[list[str], list[str]]:
        rewriter = ImportRewriter(self.repo_root, patcher)
        importers: set[str] = set()
        unchanged: set[str] = set()
        for row in graph_query.importers(self.fetch_all, self.project, old_module):
            if (
                row["imported_name"] != name
                or row["path"] is None
                or row["line"] is None
            ):
                continue
            site = ImportSite(
                row["path"],
                row["line"],
                row["col"] or 0,
                row["end_line"] or row["line"],
                row["end_col"] or 0,
                row["alias"],
                row["imported_name"],
            )
            spelled = old_spelled
            if language in _JS_LANGUAGES:
                statement = _statement_text(patcher.source(site.path), site)
                spec = _JS_SPEC.search(statement)
                spelled = spec.group("spec") if spec else old_spelled
            move = SymbolMove(name, spelled, new_spelled, new_module_path=new_path)
            if rewriter.retarget([site], move):
                importers.add(site.path)
            else:
                unchanged.add(f"{site.path}:{site.line}")
        return sorted(importers), sorted(unchanged)

    def _retarget_attribute_uses(
        self,
        patcher: Patcher,
        qn: str,
        old_spelled: str,
        new_spelled: str,
        name: str,
        language: cs.SupportedLanguage | None,
    ) -> None:
        """`pkg.util.helper(...)` through `import pkg.util` follows the move."""
        if language != cs.SupportedLanguage.PYTHON:
            return
        from .patcher import line_col_to_byte

        old_attr = f"{old_spelled}{cs.SEPARATOR_DOT}{name}".encode(cs.ENCODING_UTF8)
        new_attr = f"{new_spelled}{cs.SEPARATOR_DOT}{name}"
        touched: set[str] = set()
        for row in graph_query.callers(self.fetch_all, self.project, qn):
            path, line, col = row["path"], row["line"], row["col"]
            if not isinstance(path, str) or line is None or col is None:
                continue
            try:
                source = patcher.source(path)
            except PatcherError:
                continue
            start = line_col_to_byte(source, line, col)
            end = line_col_to_byte(
                source, row["end_line"] or line, row["end_col"] or col
            )
            at = source.find(old_attr, start, end)
            if at < 0:
                continue
            patcher.replace_span(path, (at, at + len(old_attr)), new_attr)
            if path not in touched:
                touched.add(path)
                _language, root = self._parse(path, source)
                text = source.decode(cs.ENCODING_UTF8, errors="replace")
                if not _module_bound(text, new_spelled):
                    at_import = _import_block_end(source, root)
                    patcher.replace_span(
                        path, (at_import, at_import), f"import {new_spelled}\n"
                    )
