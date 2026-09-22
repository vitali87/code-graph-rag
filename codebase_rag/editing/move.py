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

import ast
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
from .transaction import (
    EditTransaction,
    StagedFile,
    StagedTree,
    TransactionConflict,
    VerificationResult,
    load_history,
    undo_transaction,
    unified_diff,
)

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


def _module_bound(text: str, module: str) -> bool:
    """Whether `text` binds `module`'s root name via a plain `import`.

    The move rewrites call sites to `pkg.new.helper(...)`, which needs
    `pkg` bound. Only a plain `import pkg.new` does that -- or a deeper
    `import pkg.new.sub`, which binds `pkg` just the same (verified
    against a real interpreter; it must keep answering True).

    Deciding this by searching the raw text was wrong in the direction
    that breaks code. `import pkg.new as n` binds only `n`; a comment or
    a string containing the words binds nothing. Each made the caller
    skip adding the real import AFTER the call sites had been rewritten,
    and both versions parse, so the postcondition could not catch it
    either -- the program died at runtime with
    `NameError: name 'pkg' is not defined` on the line the move wrote.

    Unparseable input answers False: the caller then adds an import it
    may not need, which is recoverable, rather than omitting one it does.
    """
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return False
    wanted = module.split(".")
    for node in ast.walk(tree):
        if not isinstance(node, ast.Import):
            continue
        for alias in node.names:
            # `import x as y` binds only `y`, never x's root.
            if alias.asname is None and alias.name.split(".")[: len(wanted)] == wanted:
                return True
    return False


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
    # Swallow the blank lines that separated it from what follows.
    while source[end : end + 1] == b"\n":
        end += 1
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
    # What the import and paste generators below can write. TSX is left out
    # on purpose: `_narrow_statement` spells JavaScript only for
    # `_JS_LANGUAGES`, which does not name TSX, so a TSX move would copy
    # its imports in Python syntax. Anything else has a grammar but no
    # generator here, and every non-JS branch writes Python.
    _MOVABLE = frozenset(
        {cs.SupportedLanguage.PYTHON, cs.SupportedLanguage.JS, cs.SupportedLanguage.TS}
    )
    _FUTURE = "__future__"
    _FUTURE_IMPORT = "from __future__ import {features}\n"
    # Refusals that have no home in constants/cli.py yet.
    _UNSUPPORTED = "move does not support {language} ({path})"
    _CROSS_LANGUAGE = (
        "{new_path} is not in the language of {old_path}; a move cannot "
        "change a definition's language"
    )
    _NESTED = "{qn} is nested inside another definition; move that one instead"
    _COLLISION = "{path} already binds {name}; the moved definition would shadow it"
    _NOT_EXPORTED = (
        "{names} is not exported from {path}, so the moved definition could "
        "not import it; export it first"
    )
    _ROLLBACK_REFUSED = (
        "Move failed its postcondition ({reasons}) and was not rolled back "
        "({error}); the moved files may remain modified; check the working tree"
    )

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
        if language not in self._MOVABLE:
            raise MoveRefused(
                self._UNSUPPORTED.format(language=language, path=old_path)
            )
        # An explicit `pkg/core.ts` for a Python definition would get Python
        # text under a TypeScript name, and no parser of the right language
        # would ever look at it.
        if get_language_for_extension(Path(new_path).suffix) != language:
            raise MoveRefused(
                self._CROSS_LANGUAGE.format(new_path=new_path, old_path=old_path)
            )
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
        # A nested function or class keeps its indentation when cut, and
        # pasted at module level it no longer parses (or, one level down,
        # silently changes what it closes over).
        top = (
            node.parent
            if node.parent is not None and node.parent.type in _WRAPPERS
            else node
        )
        if top.parent is None or top.parent.type != root.type:
            raise MoveRefused(self._NESTED.format(qn=qn))
        existing = self._existing_target(patcher, new_path)
        target_root: Node | None = None
        if existing is not None:
            _language, target_root = self._parse(new_path, existing)
            if name in self._bindings(target_root):
                raise MoveRefused(self._COLLISION.format(path=new_path, name=name))
        cut = _cut_span(source, node)
        remainder = (source[: cut.start] + source[cut.end :]).decode(
            cs.ENCODING_UTF8, errors="replace"
        )

        needed = self._needed_imports(
            old_module, old_path, new_path, cut.text, language
        )
        old_bindings = self._bindings(root)
        from_old = self._needed_from_old(old_path, cut.text, name, old_bindings)
        if language in _JS_LANGUAGES:
            # `import { X }` of a name the module never exported is a load
            # error in ESM and a compile error in TypeScript.
            hidden = [n for n in from_old if not old_bindings.get(n, False)]
            if hidden:
                raise MoveRefused(
                    self._NOT_EXPORTED.format(names=", ".join(hidden), path=old_path)
                )
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
            qn,
            name,
            language,
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
        # `from __future__ import annotations` is never referenced by name,
        # so the use filter above drops it, and without it the destination
        # evaluates the moved annotations eagerly: a forward reference that
        # loaded fine before the move is a NameError after it.
        futures = (
            self._future_features(source)
            if language == cs.SupportedLanguage.PYTHON
            else []
        )
        new_content: str | None = None
        if existing is None:
            head = (
                self._FUTURE_IMPORT.format(features=", ".join(futures)) + "\n"
                if futures
                else ""
            )
            new_content = head + paste.lstrip("\n")
        else:
            sep = (
                ""
                if not existing.strip()
                else ("\n" if existing.endswith(b"\n\n") else "\n\n")
            )
            if existing and not existing.endswith(b"\n"):
                sep = "\n" + sep
            have = set(self._future_features(existing))
            missing = [f for f in futures if f not in have]
            future_line = (
                self._FUTURE_IMPORT.format(features=", ".join(missing))
                if missing
                else ""
            )
            # A future statement must precede every other statement, so it
            # goes at the head of the destination (after its docstring),
            # never with the pasted imports at the end.
            assert target_root is not None
            at = self._head_end(existing, target_root)
            if future_line and at < len(existing):
                patcher.replace_span(new_path, (at, at), future_line)
                future_line = ""
            patcher.replace_span(
                new_path,
                (len(existing), len(existing)),
                sep + future_line + paste.lstrip("\n"),
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
            diff=self._preview_diff(patcher, new_path, new_content),
            message=cs.MOVE_PLANNED.format(
                importers=len(importers), unchanged=len(unchanged)
            ),
        )
        return report, patcher, new_content

    @staticmethod
    def _preview_diff(patcher: Patcher, new_path: str, new_content: str | None) -> str:
        """The diff `apply` would write, so a dry run can be inspected.

        Built from the patched contents in memory: nothing is staged or
        written, and `apply` replaces it with the committed diff.
        """
        staged = [
            StagedFile(key, patcher.source(key), result.content)
            for key, result in patcher.apply().items()
        ]
        if new_content is not None:
            staged.append(
                StagedFile(new_path, None, new_content.encode(cs.ENCODING_UTF8))
            )
        return "".join(unified_diff(s) for s in sorted(staged))

    def _existing_target(self, patcher: Patcher, new_path: str) -> bytes | None:
        """The destination's bytes, or None when it is a new file.

        Only an absent path inside the repository is a new file. Reading
        every `PatcherError` as "absent" planned a target outside the repo
        as a file to create, and the refusal came later, from the
        transaction, as a crash instead of a refused move.
        """
        try:
            return patcher.source(new_path)
        except PatcherError as error:
            candidate = self.repo_root / new_path
            try:
                inside = candidate.resolve().is_relative_to(self.repo_root)
            except OSError:
                inside = False
            if inside and not candidate.exists() and not candidate.is_symlink():
                return None
            raise MoveRefused(str(error)) from error

    @staticmethod
    def _bindings(root: Node) -> dict[str, bool]:
        """Top-level names a module binds, each with whether it is exported.

        Definitions, and plain assignments or `const`/`let`/`var`
        declarations, which the graph has no node for. Imports are not
        included: they are copied by `_needed_imports`. "Exported" only
        means something for JavaScript and TypeScript.
        """
        bound: dict[str, bool] = {}
        exported_as: set[str] = set()
        for child in root.children:
            exported = child.type == cs.TS_EXPORT_STATEMENT
            nodes = list(child.named_children) if child.type in _WRAPPERS else [child]
            for node in nodes:
                if node.type == cs.TS_EXPORT_CLAUSE:
                    for spec in node.named_children:
                        if spec.type == cs.TS_EXPORT_SPECIFIER:
                            as_name = spec.child_by_field_name(
                                cs.FIELD_ALIAS
                            ) or spec.child_by_field_name(cs.FIELD_NAME)
                            exported_as.add(_text(as_name))
                    continue
                for named in Mover._bound_names(node):
                    bound[named] = bound.get(named, False) or exported
        for named in exported_as:
            if named in bound:
                bound[named] = True
        return bound

    @staticmethod
    def _bound_names(node: Node) -> list[str]:
        # An import statement has a `name` field too (the imported module).
        if node.type in _IMPORT_TYPES:
            return []
        named = node.child_by_field_name(cs.FIELD_NAME)
        if named is not None:
            return [_text(named)]
        if node.type in (cs.TS_LEXICAL_DECLARATION, cs.TS_VARIABLE_DECLARATION):
            declared = (
                d.child_by_field_name(cs.FIELD_NAME)
                for d in node.named_children
                if d.type == cs.TS_VARIABLE_DECLARATOR
            )
            return [
                _text(n)
                for n in declared
                if n is not None and n.type == cs.TS_IDENTIFIER
            ]
        if node.type != cs.TS_PY_EXPRESSION_STATEMENT:
            return []
        names: list[str] = []
        for assignment in node.named_children:
            # `a = b = 1` nests the second target in the right-hand side.
            while assignment is not None and assignment.type == cs.TS_PY_ASSIGNMENT:
                left = assignment.child_by_field_name(cs.FIELD_LEFT)
                if left is not None and left.type == cs.TS_PY_IDENTIFIER:
                    names.append(_text(left))
                elif left is not None and left.type in cs.PY_UNPACKING_TARGET_TYPES:
                    names.extend(
                        _text(n)
                        for n in left.named_children
                        if n.type == cs.TS_PY_IDENTIFIER
                    )
                assignment = assignment.child_by_field_name(cs.FIELD_RIGHT)
        return names

    @classmethod
    def _future_features(cls, source: bytes) -> list[str]:
        """The `from __future__ import ...` features a Python module enables."""
        try:
            tree = ast.parse(source)
        except (SyntaxError, ValueError):
            return []
        return [
            alias.name
            for node in tree.body
            if isinstance(node, ast.ImportFrom) and node.module == cls._FUTURE
            for alias in node.names
        ]

    @staticmethod
    def _head_end(source: bytes, root: Node) -> int:
        """Byte offset after a Python module's leading comments and docstring.

        Where a statement goes that must not displace them: a shebang or an
        encoding line only works on the first lines, and a string that is no
        longer the first statement is no longer `__doc__`.
        """
        end = 0
        for child in root.children:
            is_docstring = (
                child.type == cs.TS_PY_EXPRESSION_STATEMENT
                and child.named_child_count == 1
                and child.named_children[0].type == cs.TS_PY_STRING
            )
            if child.type != cs.TS_COMMENT and not is_docstring:
                break
            end = source.find(b"\n", child.end_byte)
            end = len(source) if end < 0 else end + 1
            if is_docstring:
                break
        return end

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
            # Future directives are placed by `plan`, at the destination's
            # head; one pasted among the ordinary imports is a SyntaxError.
            if (
                narrowed is None
                or imported_raw == self._FUTURE
                or (row.get(cs.KEY_TO_QN) == self._FUTURE)
            ):
                continue
            target = str(row.get(cs.KEY_TO_QN) or "")
            out.setdefault(narrowed, _NeededImport(narrowed, target))
        return list(out.values())

    def _needed_from_old(
        self,
        old_path: str,
        moved_text: str,
        name: str,
        bindings: dict[str, bool],
    ) -> list[str]:
        """Old-module names the moved text still refers to.

        The graph has definitions only, so a module constant the moved code
        reads (`DEFAULT_TIMEOUT = 5`) came from the parsed `bindings`
        instead; without it the move parsed, passed its contract, and died
        with a NameError the first time the moved code ran.
        """
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
        names.update(
            other for other in bindings if other != name and _uses(moved_text, other)
        )
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
        qn: str,
        name: str,
        language: cs.SupportedLanguage | None,
    ) -> None:
        # `snapshot`'s module-import query is project-wide (it filters by
        # project, not by `paths`), so an importer outside the two modules
        # is in `graph` and a cycle through it is still seen.
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
        # Only the importers the move rewires gain an edge to the new module:
        # an importer of some other name from the old module is untouched, and
        # modelling it as rewired refused safe moves over cycles that would
        # never exist. One whose only import from the old module is the moved
        # name loses that edge.
        rewired, keeps_old = self._rewired_importers(old_module, qn, name, language)
        for importer in rewired - {old_module, new_module}:
            targets = graph.setdefault(importer, set())
            targets.add(new_module)
            if importer not in keeps_old:
                targets.discard(old_module)
        after = import_cycles({m: frozenset(t) for m, t in graph.items()})
        # Sorted, so the cycle named is the same on every run when there are
        # several: set order follows string hashing, which is per-process.
        fresh = sorted(
            tuple(sorted(c))
            for c in after - before
            if new_module in c or old_module in c
        )
        if fresh:
            cycle = fresh[0]
            raise MoveRefused(cs.MOVE_CYCLE.format(cycle=" -> ".join(cycle)), cycle)

    def _rewired_importers(
        self,
        old_module: str,
        qn: str,
        name: str,
        language: cs.SupportedLanguage | None,
    ) -> tuple[set[str], set[str]]:
        """Modules the move points at the new module, and those of them (or
        others) that still import something else from the old one."""
        rows = graph_query.importers(self.fetch_all, self.project, old_module)
        rewired = {r["module"] for r in rows if r["imported_name"] == name}
        keeps_old = {r["module"] for r in rows if r["imported_name"] != name}
        if language == cs.SupportedLanguage.PYTHON:
            # `pkg.util.helper(...)` through `import pkg.util` gains an
            # `import pkg.new` (`_retarget_attribute_uses`) and keeps its
            # module import.
            for row in graph_query.callers(self.fetch_all, self.project, qn):
                if isinstance(row["path"], str):
                    module = base_module_qn(Path(row["path"]), self.project)
                    if module in keeps_old:
                        rewired.add(module)
        return rewired, keeps_old

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
        if not at and language == cs.SupportedLanguage.PYTHON:
            # No imports: offset 0 would push a shebang, an encoding line or
            # the module docstring down, and the docstring stops being one.
            at = self._head_end(source, root)
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
                if not _module_bound(text, new_spelled):
                    at_import = _import_block_end(source, root)
                    patcher.replace_span(
                        path, (at_import, at_import), f"import {new_spelled}\n"
                    )

    # --- applying ---------------------------------------------------------------------

    def apply(self, qn: str, target: str, keep_alias: bool = False) -> MoveReport:
        report, patcher, new_content = self.plan(qn, target, keep_alias)
        tx = EditTransaction(self.repo_root)
        results = patcher.stage_into(tx)
        broken = [key for key, result in results.items() if result.parses is False]
        if new_content is not None:
            # `stage_into` parses every patched file; a new destination is
            # not patched, so it gets the same gate here.
            if self._parses(report.new_path, new_content):
                tx.stage(report.new_path, new_content)
            else:
                broken.append(report.new_path)
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

    def _parses(self, path: str, content: str) -> bool:
        _language, root = self._parse(path, content.encode(cs.ENCODING_UTF8))
        return not root.has_error

    def _enforce_contract(self, report: MoveReport) -> MoveReport:
        assert self.reingest is not None
        renamed = (report.qualified_name, report.new_qualified_name)
        delta = measure(
            self.fetch_all,
            self.project,
            self.repo_root,
            report.files,
            self.reingest,
            # An empty class has no fingerprint and no members to match the
            # two sides by, so the delta cannot infer this rename; without
            # it the move of one reads as a removal plus an addition.
            declared_renames=(renamed,),
        )
        verdict = verify(move_expectation(*renamed), delta)
        if verdict.ok:
            return report._replace(verdict=verdict)
        reasons = "; ".join(verdict.failures)
        report = report._replace(verdict=verdict)
        try:
            # This move's own transaction, not whatever is newest: the lock
            # is released before the re-ingest, so another edit can land in
            # between, and `undo_last` reversed THAT edit and kept the move.
            outcome = undo_transaction(self.repo_root, report.transaction_id)
        except TransactionConflict as conflict:
            return self._not_rolled_back(report, reasons, str(conflict))
        if not outcome.applied:
            return self._not_rolled_back(report, reasons, outcome.message)
        self.reingest(list(report.files))
        return report._replace(
            applied=False, message=cs.MOVE_CONTRACT_FAILED.format(reasons=reasons)
        )

    def _not_rolled_back(
        self, report: MoveReport, reasons: str, error: str
    ) -> MoveReport:
        """The report for a failed move this op could not reverse.

        `applied` stays True while the history still records the move: a
        later edit stacked on it, or a file changed since, keeps it on disk.
        An entry already gone may mean another actor reversed it, which
        this op cannot tell from the history, so it claims nothing then.
        """
        recorded = any(
            entry.get(cs.EDIT_KEY_ID) == report.transaction_id
            for entry in load_history(self.repo_root)
        )
        return report._replace(
            applied=recorded,
            message=self._ROLLBACK_REFUSED.format(reasons=reasons, error=error),
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
