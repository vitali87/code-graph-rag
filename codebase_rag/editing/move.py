"""`move(qn, target_module)`: edit-algebra operation 3 (issue #1534).

Moving a symbol out of a shared dumping ground into the one package that
uses it is the refactor that shrinks affected sets, and by hand it is
tedious: the definition, its own imports, every importer, re-exports. The
graph knows the definition's span, the imports its module binds, and every
importer's statement (issue #1522), so the whole move is one transaction:

- cut the definition (decorators, docstring, adjacent comments included)
  and paste it into the target with the imports it needs;
- rewrite every importer through the import rewriter;
- give the old module an import of the moved name when it still uses it,
  and optionally a deprecation re-export (`keep_alias=True`);
- refuse before touching a file when the move would create an import
  cycle, naming the cycle.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple

from tree_sitter import Node

from .. import constants as cs
from .. import cypher_queries as cq
from .. import graph_query
from ..graph_query import QueryFn
from ..language_spec import get_language_for_extension
from ..parser_loader import load_parsers
from ..structural_delta import import_cycles, snapshot
from ..utils.path_utils import base_module_qn
from .contract import Reingest, Verdict, measure, move_expectation, verify
from .imports import (
    _JS_NAMED,
    _JS_SPEC,
    _PY_IMPORT,
    ImportRewriter,
    ImportSite,
    SymbolMove,
    _local_name,
    _match_py_from,
    _relative_specifier,
    _split_names,
)
from .patcher import Patcher, PatcherError
from .rename import _name_token
from .transaction import EditTransaction, StagedTree, VerificationResult, undo_last

_IDENTIFIER = r"(?<![\w.])%s(?!\w)"
_JS_LANGUAGES = frozenset({cs.SupportedLanguage.JS, cs.SupportedLanguage.TS})
_WRAPPERS = frozenset({cs.TS_PY_DECORATED_DEFINITION, cs.TS_EXPORT_STATEMENT})
_IMPORT_TYPES = frozenset(
    {cs.TS_PY_IMPORT_STATEMENT, cs.TS_PY_IMPORT_FROM_STATEMENT, cs.TS_IMPORT_STATEMENT}
)


class MoveRefused(ValueError):
    """The move cannot be planned as asked; nothing was written."""

    def __init__(self, message: str, cycle: tuple[str, ...] = ()) -> None:
        super().__init__(message)
        self.cycle = cycle


class MoveReport(NamedTuple):
    qualified_name: str
    new_qualified_name: str
    old_path: str
    new_path: str
    applied: bool
    transaction_id: str
    files: tuple[str, ...]
    importers: tuple[str, ...]
    unchanged_importers: tuple[str, ...]
    copied_imports: tuple[str, ...]
    diff: str
    message: str
    verdict: Verdict | None = None


class _Cut(NamedTuple):
    start: int
    end: int
    text: str


class _NeededImport(NamedTuple):
    statement: str
    target_qn: str


def _text(node: Node | None) -> str:
    if node is None or node.text is None:
        return ""
    return node.text.decode(cs.ENCODING_UTF8, errors="replace")


def _uses(text: str, name: str) -> bool:
    return re.search(_IDENTIFIER % re.escape(name), text) is not None


def _definition_at(root: Node, line: int, col: int) -> Node | None:
    """The outermost definition whose own name token starts at (line, col)."""
    stack = [root]
    while stack:
        node = stack.pop()
        named = node.child_by_field_name(cs.FIELD_NAME)
        if (
            named is not None
            and named.start_point == (line - 1, col)
            and (node.type not in (cs.TS_IDENTIFIER, cs.TS_PY_IDENTIFIER))
        ):
            return node
        if node.start_point[0] <= line - 1 <= node.end_point[0]:
            stack.extend(reversed(node.children))
    return None


def _cut_span(source: bytes, node: Node) -> _Cut:
    """Whole lines of the definition plus decorators, export and comments."""
    target = node
    if target.parent is not None and target.parent.type in _WRAPPERS:
        target = target.parent
    first = target
    sibling = target.prev_named_sibling
    while (
        sibling is not None
        and sibling.type == cs.TS_COMMENT
        and sibling.end_point[0] + 1 == first.start_point[0]
    ):
        first = sibling
        sibling = sibling.prev_named_sibling
    start = source.rfind(b"\n", 0, first.start_byte) + 1
    end = source.find(b"\n", target.end_byte)
    end = len(source) if end < 0 else end + 1
    text = source[start:end].decode(cs.ENCODING_UTF8, errors="replace")
    # Swallow the blank lines that separated it from what follows. A blank line
    # is b"\r\n" in a CRLF file, so testing only for b"\n" stopped on the b"\r"
    # and left the separator behind on every Windows checkout.
    while True:
        if source[end : end + 2] == b"\r\n":
            end += 2
        elif source[end : end + 1] in (b"\n", b"\r"):
            end += 1
        else:
            break
    return _Cut(start, end, text)


def _import_block_end(source: bytes, root: Node) -> int:
    """Byte offset just after the last top-level import (0 when none)."""
    end = 0
    for child in root.children:
        if child.type in _IMPORT_TYPES:
            end = source.find(b"\n", child.end_byte)
            end = len(source) if end < 0 else end + 1
    return end


def _strip_project(qn: str, project: str) -> str:
    prefix = f"{project}{cs.SEPARATOR_DOT}"
    return qn[len(prefix) :] if qn.startswith(prefix) else qn


class Mover:
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

    def _refuse_cycles(
        self,
        old_module: str,
        new_module: str,
        copied_targets: list[str],
        needs_old: bool,
        old_needs_new: bool,
        old_path: str,
        new_path: str,
    ) -> None:
        graph = {
            qn: set(targets)
            for qn, targets in snapshot(
                self.fetch_all, self.project, [old_path, new_path]
            ).imports.items()
        }
        before = import_cycles({qn: frozenset(t) for qn, t in graph.items()})
        graph.setdefault(new_module, set()).update(t for t in copied_targets if t)
        if needs_old:
            graph.setdefault(new_module, set()).add(old_module)
        if old_needs_new:
            graph.setdefault(old_module, set()).add(new_module)
        for importer, targets in graph.items():
            if old_module in targets and importer not in (old_module, new_module):
                targets.add(new_module)
        after = import_cycles({qn: frozenset(t) for qn, t in graph.items()})
        fresh = [c for c in after - before if new_module in c or old_module in c]
        if fresh:
            cycle = tuple(sorted(fresh[0]))
            raise MoveRefused(cs.MOVE_CYCLE.format(cycle=" -> ".join(cycle)), cycle)

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
                if not _uses(text, f"import {new_spelled}"):
                    at_import = _import_block_end(source, root)
                    patcher.replace_span(
                        path, (at_import, at_import), f"import {new_spelled}\n"
                    )

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


# --- statement helpers -----------------------------------------------------------------


def _statement_text(source: bytes, site: ImportSite) -> str:
    from .patcher import line_col_to_byte

    start = line_col_to_byte(source, site.line, site.col)
    end = line_col_to_byte(source, site.end_line, site.end_col)
    return source[start:end].decode(cs.ENCODING_UTF8, errors="replace")


def _narrow_statement(
    statement: str,
    alias: str,
    language: cs.SupportedLanguage | None,
    old_path: str,
    new_path: str,
) -> str | None:
    """The statement reduced to the entry binding `alias`, respelled for
    the new file where the specifier is relative."""
    if language in _JS_LANGUAGES:
        spec = _JS_SPEC.search(statement)
        if spec is None:
            return None
        text = statement
        if spec.group("spec").startswith("."):
            target = (Path(old_path).parent / spec.group("spec")).as_posix()
            text = (
                statement[: spec.start("spec")]
                + _relative_specifier(new_path, target)
                + statement[spec.end("spec") :]
            )
        named = _JS_NAMED.search(text)
        if named is None:
            return text.strip()
        entries = [e.strip() for e in named.group("names").split(",") if e.strip()]
        kept = [e for e in entries if _local_name(e) == alias]
        if not kept:
            return None
        return (
            text[: named.start()] + "{ " + ", ".join(kept) + " }" + text[named.end() :]
        ).strip()
    if parsed := _match_py_from(statement):
        # main replaced the _PY_FROM regex with a token parser returning
        # (lead, module, mid, names); only those two fields are needed here.
        _lead, module, _mid, raw_names = parsed
        entries, _open, _close = _split_names(raw_names)
        kept = [e for e in entries if _local_name(e) == alias]
        if not kept:
            return None
        return f"from {module} import {kept[0]}"
    if _PY_IMPORT.match(statement):
        return statement.strip()
    return statement.strip()


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
