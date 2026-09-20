"""Implementation of the move refactoring operation."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from tree_sitter import Node

from .. import constants as cs
from .. import cypher_queries as cq
from .. import graph_query
from ..graph_query import QueryFn
from ..language_spec import get_language_for_extension
from ..parser_loader import load_parsers
from ..utils.path_utils import base_module_qn
from .contract import Reingest, measure, move_expectation, verify
from .imports import ImportSite, _relative_specifier
from .move_cycles import _MoveCycleMixin
from .move_retarget import _MoveRetargetMixin
from .move_scope import (
    _JS_LANGUAGES,
    _cut_span,
    _definition_at,
    _import_block_end,
    _narrow_statement,
    _statement_text,
    _strip_project,
    _uses,
)
from .move_types import MoveRefused, MoveReport, _NeededImport
from .patcher import Patcher, PatcherError
from .rename import _name_token
from .transaction import EditTransaction, StagedTree, VerificationResult, undo_last


class Mover(_MoveRetargetMixin, _MoveCycleMixin):
    def __init__(
        self,
        repo_root: Path,
        fetch_all: QueryFn,
        project_name: str,
        verify: Callable[[StagedTree], VerificationResult | bool | None] | None = None,
        reingest: Reingest | None = None,
    ) -> None:
        self.repo_root = repo_root.resolve()
        self.fetch_all = fetch_all
        self.project = project_name
        self.verify = verify
        self.reingest = reingest
        self._parsers = load_parsers()[0]

    def _parse(
        self, path: str, source: bytes
    ) -> tuple[cs.SupportedLanguage | None, Node]:
        language = get_language_for_extension(Path(path).suffix)
        parser = self._parsers.get(language) if language is not None else None
        if parser is None:
            raise MoveRefused(cs.MOVE_NO_GRAMMAR.format(path=path))
        return language, parser.parse(source).root_node

    def _target_path(self, target: str, old_path: str) -> str:
        suffix = Path(old_path).suffix
        if target.endswith(suffix) or "/" in target:
            return Path(target).as_posix()
        dotted = _strip_project(target, self.project)
        return Path(*dotted.split(cs.SEPARATOR_DOT)).with_suffix(suffix).as_posix()

    # --- planning ---------------------------------------------------------------------

    def plan(
        self, qn: str, target: str, keep_alias: bool = False
    ) -> tuple[MoveReport, Patcher, str | None]:
        definition = graph_query.definition(
            self.fetch_all, self.project, qn, self.repo_root
        )
        if not definition["found"] or not definition["path"]:
            raise MoveRefused(cs.MOVE_UNKNOWN.format(qn=qn))
        if definition["label"] == cs.NodeLabel.METHOD.value:
            raise MoveRefused(cs.MOVE_METHOD.format(qn=qn))
        old_path = definition["path"]
        name = definition["name"] or qn.rsplit(cs.SEPARATOR_DOT, 1)[-1]
        new_path = self._target_path(target, old_path)
        if new_path == old_path:
            raise MoveRefused(cs.MOVE_SAME_MODULE.format(path=old_path))
        old_module = base_module_qn(Path(old_path), self.project)
        new_module = base_module_qn(Path(new_path), self.project)
        patcher = Patcher(self.repo_root)
        source = patcher.source(old_path)
        language, root = self._parse(old_path, source)
        token = _name_token(
            source,
            language,
            definition["start_line"] or 1,
            definition["end_line"] or 1,
            name,
        )
        node = _definition_at(root, *token) if token else None
        if node is None:
            raise MoveRefused(cs.MOVE_NO_DEFINITION_TOKEN.format(qn=qn, path=old_path))
        cut = _cut_span(source, node)
        remainder = (source[: cut.start] + source[cut.end :]).decode(
            cs.ENCODING_UTF8, errors="replace"
        )

        needed = self._needed_imports(
            old_module, old_path, new_path, cut.text, language
        )
        from_old = self._needed_from_old(old_path, cut.text, name)
        old_uses = _uses(remainder, name)
        # The cycle check runs on the graph BEFORE any file is touched.
        self._refuse_cycles(
            old_module,
            new_module,
            [n.target_qn for n in needed],
            bool(from_old),
            old_uses or keep_alias,
            old_path,
            new_path,
        )

        old_spelled = _strip_project(old_module, self.project)
        new_spelled = _strip_project(new_module, self.project)
        # 1. Cut from the old module, import the name back when still used.
        replacement = ""
        patcher.replace_span(old_path, (cut.start, cut.end), replacement)
        if old_uses or keep_alias:
            self._add_import(
                patcher,
                old_path,
                source,
                root,
                language,
                new_spelled,
                new_path,
                name,
                export=keep_alias and not old_uses,
            )
        # 2. Paste into the target with what it needs.
        paste = self._paste_text(
            cut.text, needed, from_old, old_spelled, old_path, new_path, language
        )
        new_content: str | None = None
        try:
            existing = patcher.source(new_path)
        except PatcherError:
            new_content = paste.lstrip("\n")
        else:
            sep = (
                ""
                if not existing.strip()
                else ("\n" if existing.endswith(b"\n\n") else "\n\n")
            )
            if existing and not existing.endswith(b"\n"):
                sep = "\n" + sep
            patcher.replace_span(
                new_path, (len(existing), len(existing)), sep + paste.lstrip("\n")
            )
        # 3. Importers, and uses through a module import (`pkg.util.helper`).
        self._retarget_attribute_uses(
            patcher, qn, old_spelled, new_spelled, name, language
        )
        importers, unchanged = self._retarget_importers(
            patcher, old_module, old_spelled, new_spelled, new_path, name, language
        )
        files = sorted(set(patcher.pending) | {new_path})
        report = MoveReport(
            qualified_name=qn,
            new_qualified_name=f"{new_module}{cs.SEPARATOR_DOT}{name}",
            old_path=old_path,
            new_path=new_path,
            applied=False,
            transaction_id="",
            files=tuple(files),
            importers=tuple(sorted(importers)),
            unchanged_importers=tuple(sorted(unchanged)),
            copied_imports=tuple(n.statement for n in needed),
            diff="",
            message=cs.MOVE_PLANNED.format(
                importers=len(importers), unchanged=len(unchanged)
            ),
        )
        return report, patcher, new_content

    def _needed_imports(
        self,
        old_module: str,
        old_path: str,
        new_path: str,
        moved_text: str,
        language: cs.SupportedLanguage | None,
    ) -> list[_NeededImport]:
        """The old module's import statements the moved text relies on."""
        rows = self.fetch_all(
            cq.CYPHER_GRAPH_IMPORTS_OF,
            {
                cs.KEY_PROJECT_PREFIX: f"{self.project}{cs.SEPARATOR_DOT}",
                cs.KEY_QN: old_module,
            },
        )
        source = Patcher(self.repo_root).source(old_path)
        out: dict[str, _NeededImport] = {}
        for row in rows:
            alias = row.get(cs.KEY_ALIAS)
            line, col = row.get(cs.KEY_LINE), row.get(cs.KEY_COL)
            end_line, end_col = row.get(cs.KEY_END_LINE), row.get(cs.KEY_END_COL)
            if (
                not isinstance(alias, str)
                or not isinstance(line, int)
                or not isinstance(col, int)
            ):
                continue
            bound = alias.split(cs.SEPARATOR_DOT)[0]
            imported_raw = row.get(cs.KEY_IMPORTED_NAME)
            imported = imported_raw if isinstance(imported_raw, str) else None
            if not _uses(moved_text, bound):
                continue
            site = ImportSite(
                old_path,
                line,
                col,
                end_line if isinstance(end_line, int) else line,
                end_col if isinstance(end_col, int) else col,
                alias,
                imported,
            )
            statement = _statement_text(source, site)
            narrowed = _narrow_statement(statement, alias, language, old_path, new_path)
            if narrowed is None:
                continue
            target = str(row.get(cs.KEY_TO_QN) or "")
            out.setdefault(narrowed, _NeededImport(narrowed, target))
        return list(out.values())

    def _needed_from_old(self, old_path: str, moved_text: str, name: str) -> list[str]:
        """Old-module definitions the moved text still refers to."""
        rows = self.fetch_all(
            cq.CYPHER_DELTA_DEFINITIONS,
            {
                cs.KEY_PROJECT_PREFIX: f"{self.project}{cs.SEPARATOR_DOT}",
                cs.CYPHER_PARAM_PATHS: [old_path],
            },
        )
        names: set[str] = set()
        for row in rows:
            other = row.get(cs.KEY_NAME)
            qn = str(row.get(cs.KEY_QUALIFIED_NAME) or "")
            if (
                not isinstance(other, str)
                or other == name
                or cs.SEPARATOR_DOT
                in qn[len(base_module_qn(Path(old_path), self.project)) + 1 :]
            ):
                continue
            if _uses(moved_text, other):
                names.add(other)
        return sorted(names)

    def _add_import(
        self,
        patcher: Patcher,
        path: str,
        source: bytes,
        root: Node,
        language: cs.SupportedLanguage | None,
        new_spelled: str,
        new_path: str,
        name: str,
        export: bool,
    ) -> None:
        at = _import_block_end(source, root)
        if language in _JS_LANGUAGES:
            spec = _relative_specifier(path, new_path)
            line = (
                f"export {{ {name} }} from '{spec}';\n"
                if export
                else f"import {{ {name} }} from '{spec}';\n"
            )
        else:
            line = f"from {new_spelled} import {name}\n"
            if export:
                line = f"from {new_spelled} import {name}  # noqa: F401  (moved; re-exported for compatibility)\n"
        patcher.replace_span(path, (at, at), line if at else line + "\n")

    def _paste_text(
        self,
        moved: str,
        needed: list[_NeededImport],
        from_old: list[str],
        old_spelled: str,
        old_path: str,
        new_path: str,
        language: cs.SupportedLanguage | None,
    ) -> str:
        lines = [n.statement.rstrip("\n") for n in needed]
        if from_old:
            if language in _JS_LANGUAGES:
                spec = _relative_specifier(new_path, old_path)
                lines.append(f"import {{ {', '.join(from_old)} }} from '{spec}';")
            else:
                lines.append(f"from {old_spelled} import {', '.join(from_old)}")
        header = "\n".join(lines)
        return (header + "\n\n\n" if header else "") + moved.rstrip("\n") + "\n"

    # --- applying ---------------------------------------------------------------------

    def apply(self, qn: str, target: str, keep_alias: bool = False) -> MoveReport:
        report, patcher, new_content = self.plan(qn, target, keep_alias)
        tx = EditTransaction(self.repo_root)
        results = patcher.stage_into(tx)
        if new_content is not None:
            tx.stage(report.new_path, new_content)
        broken = [key for key, result in results.items() if result.parses is False]
        if broken:
            tx.rollback()
            return report._replace(
                message=cs.MOVE_PARSE_FAILED.format(files=", ".join(broken))
            )

        def verifier(tree: StagedTree) -> VerificationResult | bool | None:
            return self.verify(tree) if self.verify is not None else True

        outcome = tx.commit(verifier)
        report = report._replace(
            applied=outcome.applied,
            transaction_id=outcome.transaction_id,
            files=outcome.files,
            diff=outcome.diff,
            message=outcome.message,
        )
        if outcome.applied and self.reingest is not None:
            report = self._enforce_contract(report)
        return report

    def _enforce_contract(self, report: MoveReport) -> MoveReport:
        assert self.reingest is not None
        delta = measure(
            self.fetch_all, self.project, self.repo_root, report.files, self.reingest
        )
        verdict = verify(
            move_expectation(report.qualified_name, report.new_qualified_name), delta
        )
        if verdict.ok:
            return report._replace(verdict=verdict)
        undo_last(self.repo_root)
        self.reingest(list(report.files))
        return report._replace(
            applied=False,
            verdict=verdict,
            message=cs.MOVE_CONTRACT_FAILED.format(reasons="; ".join(verdict.failures)),
        )


def move(
    repo_root: Path,
    fetch_all: QueryFn,
    project_name: str,
    qualified_name: str,
    target_module: str,
    keep_alias: bool = False,
    dry_run: bool = False,
    verify: Callable[[StagedTree], VerificationResult | bool | None] | None = None,
    reingest: Reingest | None = None,
) -> MoveReport:
    """The op: plan (refusing on a cycle) or plan and apply."""
    mover = Mover(repo_root, fetch_all, project_name, verify=verify, reingest=reingest)
    if dry_run:
        report, _patcher, _content = mover.plan(
            qualified_name, target_module, keep_alias
        )
        return report
    return mover.apply(qualified_name, target_module, keep_alias)
