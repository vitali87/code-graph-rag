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
import symtable
from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple

from loguru import logger
from tree_sitter import Node

from .. import constants as cs
from .. import cypher_queries as cq
from .. import graph_query
from ..graph_query import QueryFn
from ..graph_updater import ReingestAborted
from ..language_spec import get_language_for_extension
from ..parser_loader import load_parsers
from ..structural_delta import _longer_project_prefixes, import_cycles, snapshot
from ..utils.path_utils import base_module_qn
from .contract import Reingest, Verdict, measure, move_expectation, verify
from .imports import (
    _JS_KEYWORD,
    _JS_NAMED,
    _JS_SPEC,
    ImportRewriter,
    ImportSite,
    SymbolMove,
    _local_name,
    _match_py_from,
    _relative_specifier,
    _split_names,
)
from .patcher import (
    Patcher,
    PatcherError,
    SpanEdit,
    apply_span_edits,
    line_col_to_byte,
)
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
# The shared set, so `.tsx` takes the JS path too: a hand-kept {JS, TS}
# sent TSX through the Python branch, which wrote `from a import b` into
# a .tsx file and made every TSX move fail the parse gate.
_JS_LANGUAGES = cs.JS_TS_LANGUAGES
_WRAPPERS = frozenset({cs.TS_PY_DECORATED_DEFINITION, cs.TS_EXPORT_STATEMENT})
_IMPORT_TYPES = frozenset(
    {cs.TS_PY_IMPORT_STATEMENT, cs.TS_PY_IMPORT_FROM_STATEMENT, cs.TS_IMPORT_STATEMENT}
)
# What must stay above any import added to a file that has none: a
# shebang or coding line, `from __future__` (anything
# before it is a SyntaxError), and a leading string statement -- the
# module docstring, or a JS "use strict" directive, both of which stop
# being one the moment a statement precedes them.
_TS_FUTURE_IMPORT = "future_import_statement"
_TS_HASH_BANG = "hash_bang_line"
_PROLOGUE_TYPES = frozenset({_TS_HASH_BANG, _TS_FUTURE_IMPORT})
_PINNED_COMMENT_LINES = 2
# What a cut that ends mid-line swallows after the definition.
_INLINE_SPACE = (b" ", b"\t")
# `import D, * as ns, { a } from ...`: the clause between the keyword and
# `from`, and the one-binding clauses it is split into.
_JS_FROM = re.compile(r"\s+from\s+(?=['\"])")
_JS_NAMESPACE = re.compile(r"\*\s*as\s+(?P<name>[\w$]+)")
# A TS inline type-only entry (`{ type Foo }`) binds `Foo`; the modifier
# must be set aside before comparing, but `{ type }` alone is a real name.
_TS_INLINE_TYPE = re.compile(r"^type\s+(?=\S)")
# Module-level statements whose bodies may run once, many times or never,
# and the nodes opening a scope of their own: a name bound inside one is
# not a module binding (a definition's own name is).
_PY_FLOW = (
    ast.If,
    ast.Try,
    ast.TryStar,
    ast.With,
    ast.AsyncWith,
    ast.For,
    ast.AsyncFor,
    ast.While,
    ast.Match,
)
_PY_SCOPES = (
    ast.Lambda,
    ast.ListComp,
    ast.SetComp,
    ast.DictComp,
    ast.GeneratorExp,
)
_PY_STAR = "*"
_PY_ALL = "__all__"
_PY_PRIVATE = "_"
_MODULE_SCOPE = "module"
_SYMTABLE_FILENAME = "<moved>"
_SYMTABLE_MODE = "exec"
# `if TYPE_CHECKING:` binds its imports for the type checker only. They are
# carried under the same guard rather than refused as maybe-unbound, since
# annotations are the one use that never needs them at runtime.
_TYPE_CHECKING = "TYPE_CHECKING"
_TYPE_CHECKING_GUARDS = frozenset({_TYPE_CHECKING, f"typing.{_TYPE_CHECKING}"})
_TYPE_CHECKING_IMPORT = f"from typing import {_TYPE_CHECKING}"
_TYPE_CHECKING_HEAD = f"if {_TYPE_CHECKING}:\n"
_PY_INDENT = "    "


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
    # The graph may be partial: a re-ingest failed after it began writing.
    graph_incomplete: bool = False


class _Cut(NamedTuple):
    start: int
    end: int
    text: str
    # Where `text` begins in the source; `start` may sit before it.
    origin: int


class _NeededImport(NamedTuple):
    statement: str
    target_qn: str
    # The one name the statement binds at the destination.
    local: str


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

    Only the module's own statements count. An import inside a function
    or class binds that scope's name, not the module's, so `ast.walk`
    let a nested `import pkg.new` suppress the top-level one the
    rewritten module-level call sites need. An import under a top-level
    `if`/`try` is skipped too: it may not run, and the cost of that
    choice is at worst a redundant import.
    """
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return False
    wanted = module.split(".")
    for node in tree.body:
        if not isinstance(node, ast.Import):
            continue
        for alias in node.names:
            # `import x as y` binds only `y`, never x's root.
            if alias.asname is None and alias.name.split(".")[: len(wanted)] == wanted:
                return True
    return False


def _uses(text: str, name: str) -> bool:
    return re.search(_IDENTIFIER % re.escape(name), text) is not None


def _py_bound(node: ast.AST) -> set[str]:
    """Every module-scope name `node` can bind, on whichever path it runs."""
    names: set[str] = set()
    stack = [node]
    while stack:
        current = stack.pop()
        if isinstance(current, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            names.add(current.name)
            continue
        if isinstance(current, _PY_SCOPES) or (
            # `x: int` declares `x` without binding it.
            isinstance(current, ast.AnnAssign) and current.value is None
        ):
            continue
        if isinstance(current, ast.Name) and isinstance(current.ctx, ast.Store):
            names.add(current.id)
        elif isinstance(current, ast.Import | ast.ImportFrom):
            names.update(
                alias.asname or alias.name.split(cs.SEPARATOR_DOT)[0]
                for alias in current.names
                if alias.name != _PY_STAR
            )
        elif (
            isinstance(current, ast.ExceptHandler | ast.MatchAs | ast.MatchStar)
            and current.name
        ):
            names.add(current.name)
        elif isinstance(current, ast.MatchMapping) and current.rest:
            names.add(current.rest)
        stack.extend(ast.iter_child_nodes(current))
    return names


def _meet(left: set[str] | None, right: set[str] | None) -> set[str] | None:
    # None is a path that never completes, which binds everything vacuously.
    if left is None:
        return right
    if right is None:
        return left
    return left & right


def _py_definite(body: list[ast.stmt]) -> set[str] | None:
    """Names certainly bound once `body` completes; None when it never does.

    Conservative where it cannot tell: a loop may run zero times and a
    `match` may match nothing, so neither binds anything for certain.
    """
    names: set[str] = set()
    for statement in body:
        if isinstance(statement, ast.Raise):
            return None
        if isinstance(statement, ast.If):
            bound = _meet(_py_definite(statement.body), _py_definite(statement.orelse))
        elif isinstance(statement, ast.Try | ast.TryStar):
            bound = _py_definite(statement.body + statement.orelse)
            for handler in statement.handlers:
                bound = _meet(bound, _py_definite(handler.body))
            final = _py_definite(statement.finalbody)
            bound = None if bound is None or final is None else bound | final
        elif isinstance(statement, ast.With | ast.AsyncWith):
            bound = _py_definite(statement.body)
            if bound is not None:
                for item in statement.items:
                    if item.optional_vars is not None:
                        bound |= _py_bound(item.optional_vars)
        elif isinstance(statement, _PY_FLOW):
            bound = set()
        else:
            bound = _py_bound(statement)
        if bound is None:
            return None
        names |= bound
    return names


def _flow_bindings(source: bytes) -> tuple[frozenset[str], frozenset[str]]:
    """(carried, unsafe): module names bound under top-level control flow.

    `carried` are bound on every path that loads the module, and only
    there, so the destination can import them back like any constant;
    `unsafe` may be left unbound, and importing one by name would fail
    on load wherever the old module skipped it, where before only a call
    reaching it failed. Names a top-level statement binds are neither:
    `_bindings` and `_needed_imports` already account for them.
    """
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return frozenset(), frozenset()
    simple: set[str] = set()
    anywhere: set[str] = set()
    for statement in tree.body:
        bound = _py_bound(statement)
        anywhere |= bound
        if not isinstance(statement, _PY_FLOW):
            simple |= bound
    definite = _py_definite(tree.body) or set()
    return frozenset(definite - simple), frozenset(anywhere - definite)


def _global_references(text: str) -> set[str] | None:
    """Module-scope names the Python `text` reads; None when it won't parse.

    Scoped, unlike `_uses`: a refusal on a name the moved code only binds
    locally (a loop variable sharing a module-level loop's name) would
    refuse a move that is safe.
    """
    try:
        table = symtable.symtable(text, _SYMTABLE_FILENAME, _SYMTABLE_MODE)
    except SyntaxError:
        return None
    names: set[str] = set()
    tables = [table]
    while tables:
        current = tables.pop()
        tables.extend(current.get_children())
        at_module = current.get_type() == _MODULE_SCOPE
        names.update(
            symbol.get_name()
            for symbol in current.get_symbols()
            if symbol.is_referenced() and (at_module or symbol.is_global())
        )
    return names


def _global_writes(text: str) -> set[str]:
    """Module names the Python `text` rebinds from inside a function or a
    class through a `global` statement."""
    try:
        table = symtable.symtable(text, _SYMTABLE_FILENAME, _SYMTABLE_MODE)
    except SyntaxError:
        return set()
    names: set[str] = set()
    tables = list(table.get_children())
    while tables:
        current = tables.pop()
        tables.extend(current.get_children())
        names.update(
            symbol.get_name()
            for symbol in current.get_symbols()
            if symbol.is_declared_global() and symbol.is_assigned()
        )
    return names


def _module_scope_names(text: str) -> set[str]:
    """Every module-scope name the Python `text` binds, imports or reads,
    at the top level or from inside a function."""
    try:
        table = symtable.symtable(text, _SYMTABLE_FILENAME, _SYMTABLE_MODE)
    except SyntaxError:
        return set()
    # A name another scope declares `global` is listed at module scope
    # with no flags of its own; only real uses count.
    names = {
        symbol.get_name()
        for symbol in table.get_symbols()
        if symbol.is_assigned() or symbol.is_imported() or symbol.is_referenced()
    }
    tables = list(table.get_children())
    while tables:
        current = tables.pop()
        tables.extend(current.get_children())
        names.update(
            symbol.get_name()
            for symbol in current.get_symbols()
            if symbol.is_global() and (symbol.is_referenced() or symbol.is_assigned())
        )
    return names


def _split_globals(moved: str, remainder: str, name: str) -> list[str]:
    """Module variables the move would split in two.

    The destination imports an old-module name by value, so a `global`
    rebinding on one side of the move updates that side's copy only: the
    moved code counting in its own module while the old one, and every
    reader of it, kept the old value -- or the other way round.
    """
    reads = _global_references(moved) or set()
    staying = _module_scope_names(remainder)
    split = {n for n in _global_writes(moved) if n in staying}
    split |= {n for n in _global_writes(remainder) if n in reads}
    return sorted(split - {name})


def _star_exports(source: bytes, name: str) -> bool:
    """Whether `from module import *` of the Python `source` binds `name`.

    Without `__all__` it binds every public name; with one, the names it
    lists. An `__all__` built any other way than by one literal assignment
    is read as listing everything, since the move cannot tell what it holds.
    """
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return True
    text = source.decode(cs.ENCODING_UTF8, errors="replace")
    if _PY_ALL not in _module_scope_names(text):
        return not name.startswith(_PY_PRIVATE)
    assigned = [
        node.value
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == _PY_ALL for t in node.targets)
    ]
    if len(assigned) != 1:
        return True
    try:
        listed = ast.literal_eval(assigned[0])
    except ValueError:
        return True
    return not isinstance(listed, list | tuple) or name in listed


def _identifiers(node: Node) -> set[str]:
    found: set[str] = set()
    stack = [node]
    while stack:
        current = stack.pop()
        if current.type == cs.TS_IDENTIFIER:
            found.add(_text(current))
        stack.extend(current.named_children)
    return found


def _js_parameter_names(parameters: Node) -> set[str]:
    """The names a JS/TS parameter list binds, leaving out its defaults."""
    names: set[str] = set()
    for parameter in parameters.named_children:
        bound: Node | None = parameter
        if parameter.type == cs.TS_ASSIGNMENT_PATTERN:
            bound = parameter.child_by_field_name(cs.FIELD_LEFT)
        elif parameter.type in (cs.TS_REQUIRED_PARAMETER, cs.TS_OPTIONAL_PARAMETER):
            bound = parameter.child_by_field_name(cs.TS_FIELD_PATTERN)
        if bound is not None:
            names |= _identifiers(bound)
    return names


def _js_assigned(top: Node) -> set[str]:
    """Names the JS/TS definition `top` assigns without declaring them.

    Scope is read coarsely: a name declared anywhere inside counts as
    local throughout, so a write is reported only when no declaration of
    the definition could be what it assigns.
    """
    declared: set[str] = set()
    written: set[str] = set()
    stack = [top]
    while stack:
        current = stack.pop()
        stack.extend(current.named_children)
        target: Node | None = None
        if current.type in (
            cs.TS_JS_ASSIGNMENT_EXPRESSION,
            cs.TS_JS_AUGMENTED_ASSIGNMENT_EXPRESSION,
        ):
            target = current.child_by_field_name(cs.FIELD_LEFT)
        elif current.type == cs.TS_JS_UPDATE_EXPRESSION:
            target = current.child_by_field_name(cs.TS_JS_FIELD_ARGUMENT)
        elif current.type == cs.TS_VARIABLE_DECLARATOR:
            named = current.child_by_field_name(cs.FIELD_NAME)
            if named is not None:
                declared |= _identifiers(named)
        elif current.type == cs.TS_JS_FORMAL_PARAMETERS:
            declared |= _js_parameter_names(current)
        if target is not None and target.type == cs.TS_IDENTIFIER:
            written.add(_text(target))
    return written - declared


def _type_checking_imports(
    source: bytes, old_path: str, new_path: str
) -> dict[str, str]:
    """Names an import under a top-level `if TYPE_CHECKING:` binds, each
    with that import narrowed to it and spelled for `new_path`.

    Only a guard without an `else` counts: one with an `else` binds at
    runtime too, and is control flow like any other `if`.
    """
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return {}
    out: dict[str, str] = {}
    for node in tree.body:
        if (
            not isinstance(node, ast.If)
            or node.orelse
            or ast.unparse(node.test) not in _TYPE_CHECKING_GUARDS
        ):
            continue
        for statement in node.body:
            if isinstance(statement, ast.ImportFrom):
                module = _rebase_py_relative(
                    cs.SEPARATOR_DOT * statement.level + (statement.module or ""),
                    old_path,
                    new_path,
                )
                for alias in statement.names:
                    if alias.name != _PY_STAR:
                        out.setdefault(
                            alias.asname or alias.name,
                            f"from {module} import {ast.unparse(alias)}",
                        )
            elif isinstance(statement, ast.Import):
                for alias in statement.names:
                    out.setdefault(
                        alias.asname or alias.name.split(cs.SEPARATOR_DOT)[0],
                        f"import {ast.unparse(alias)}",
                    )
    return out


def _type_checking_block(root: Node) -> Node | None:
    """The body of a module's top-level `if TYPE_CHECKING:`, if it has one."""
    for child in root.children:
        if (
            child.type == cs.TS_PY_IF_STATEMENT
            and child.child_by_field_name(cs.FIELD_ALTERNATIVE) is None
            and _text(child.child_by_field_name(cs.TS_FIELD_CONDITION))
            in _TYPE_CHECKING_GUARDS
        ):
            return child.child_by_field_name(cs.TS_FIELD_CONSEQUENCE)
    return None


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


def _own_span(source: bytes, first: Node, last: Node) -> tuple[int, int] | None:
    """The bytes of `first` through `last` alone, and the spaces setting
    them apart on the shared line, when another statement shares their
    first or last line; None when they have their lines to themselves. A
    trailing comment is not a statement: it goes with the lines.
    """
    before, after = first.prev_named_sibling, last.next_named_sibling
    shares_last = (
        after is not None
        and after.type != cs.TS_COMMENT
        and after.start_point[0] == last.end_point[0]
    )
    if not shares_last and (
        before is None or before.end_point[0] != first.start_point[0]
    ):
        return None
    start, end = first.start_byte, last.end_byte
    if shares_last:
        while source[end : end + 1] in _INLINE_SPACE:
            end += 1
    else:
        while start > 0 and source[start - 1 : start] in _INLINE_SPACE:
            start -= 1
    return start, end


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
    # Whole lines, unless other code shares the first or the last one
    # (`function f() {} console.log('ready');` in JS): the whole-line cut
    # deleted that statement from the old module and pasted it at the
    # destination. Then only the definition's own bytes are cut.
    own = _own_span(source, first, target)
    if own is not None:
        text = source[first.start_byte : target.end_byte].decode(
            cs.ENCODING_UTF8, errors="replace"
        )
        return _Cut(*own, text, first.start_byte)
    start = source.rfind(b"\n", 0, first.start_byte) + 1
    end = source.find(b"\n", target.end_byte)
    end = len(source) if end < 0 else end + 1
    text = source[start:end].decode(cs.ENCODING_UTF8, errors="replace")
    # Swallow the blank lines that separated it from what follows.
    while source[end : end + 1] == b"\n":
        end += 1
    return _Cut(start, end, text, start)


def _line_end(source: bytes, node: Node) -> int:
    end = source.find(b"\n", node.end_byte)
    return len(source) if end < 0 else end + 1


def _is_directive(node: Node) -> bool:
    # A statement that is only a string literal: docstring or "use strict".
    named = node.named_children
    return (
        node.type == cs.TS_EXPRESSION_STATEMENT
        and len(named) == 1
        and named[0].type == cs.TS_STRING
    )


def _prologue_end(source: bytes, root: Node) -> int:
    """Byte offset just after the file's prologue (0 when it has none)."""
    end = 0
    seen_directive = False
    for child in root.children:
        if child.type == cs.TS_COMMENT:
            # Comments are stepped over, but only a shebang or PEP 263
            # coding line (first two lines) is pinned above the import:
            # pinning every comment would split a `# note` from the
            # definition it annotates.
            if child.start_point[0] < _PINNED_COMMENT_LINES:
                end = _line_end(source, child)
        elif child.type in _PROLOGUE_TYPES:
            end = _line_end(source, child)
        elif not seen_directive and _is_directive(child):
            # Only the FIRST statement can be the docstring or directive.
            seen_directive = True
            end = _line_end(source, child)
        else:
            break
    return end


def _import_block_end(source: bytes, root: Node) -> int:
    """Byte offset just after the last top-level import.

    With no import, just after the prologue rather than byte 0: an import
    written above a module docstring turns `__doc__` into None, above a
    shebang breaks the script, above `from __future__` is a SyntaxError,
    and above "use strict" silently drops strict mode. All four still
    parse, so the transaction's parse gate cannot catch them.
    """
    end = 0
    for child in root.children:
        if child.type in _IMPORT_TYPES:
            end = _line_end(source, child)
    return end or _prologue_end(source, root)


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
        # Whether the last plan respelled imports inside the moved text,
        # which changes its shape: the contract must then take the move's
        # word for the pairing.
        self._reshaped = False

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
        cut = _cut_span(source, node)
        moved_text = (
            self._rebase_local_imports(source, top, cut, qn, old_path, new_path)
            if language == cs.SupportedLanguage.PYTHON
            else cut.text
        )
        self._reshaped = moved_text != cut.text
        # A JS definition exported by its own `export { helper }` lost the
        # export with the cut: the destination did not export it, and every
        # rewritten importer (and the old module's import of it back)
        # failed to link. The entry moves with it unless `keep_alias`.
        listed = (
            self._listed_exports(source, root, name)
            if language in _JS_LANGUAGES
            else []
        )
        old_edits = [SpanEdit(cut.start, cut.end, b"")]
        if not keep_alias:
            old_edits.extend(listed)
        remainder = apply_span_edits(source, old_edits).decode(
            cs.ENCODING_UTF8, errors="replace"
        )

        old_spelled = _strip_project(old_module, self.project)
        new_spelled = _strip_project(new_module, self.project)
        needed = self._needed_imports(
            old_module, old_path, new_path, source, root, cut.text, language
        )
        old_bindings = self._bindings(root)
        guarded: dict[str, str] = {}
        if language == cs.SupportedLanguage.PYTHON:
            # `_bindings` reads top-level statements only, so a constant set
            # in both branches of an `if` was left behind (a NameError once
            # moved); a name some path leaves unbound cannot be carried.
            carried, unsafe = _flow_bindings(source)
            reads = _global_references(cut.text)
            unbound = sorted(
                n
                for n in unsafe
                if n != name
                and (n in reads if reads is not None else _uses(cut.text, n))
            )
            split = _split_globals(cut.text, remainder, name)
            if split:
                raise MoveRefused(
                    cs.MOVE_SPLIT_GLOBAL.format(
                        names=cs.SEPARATOR_COMMA_SPACE.join(split), path=old_path
                    )
                )
            typed = _type_checking_imports(source, old_path, new_path)
            guarded = {n: typed[n] for n in unbound if n in typed}
            unbound = [n for n in unbound if n not in guarded]
            if unbound:
                raise MoveRefused(
                    cs.MOVE_MAYBE_UNBOUND.format(
                        names=cs.SEPARATOR_COMMA_SPACE.join(unbound), path=old_path
                    )
                )
            old_bindings = {**dict.fromkeys(carried, False), **old_bindings}
        from_old = self._needed_from_old(old_path, cut.text, name, old_bindings)
        if language in _JS_LANGUAGES:
            # An imported binding is read-only: the moved `count += 1` that
            # updated the old module's `let` would throw at the destination.
            assigned = sorted(_js_assigned(top) & set(from_old))
            if assigned:
                raise MoveRefused(
                    cs.MOVE_ASSIGNS_IMPORT.format(
                        names=cs.SEPARATOR_COMMA_SPACE.join(assigned), path=old_path
                    )
                )
            # `import { X }` of a name the module never exported is a load
            # error in ESM and a compile error in TypeScript.
            hidden = [n for n in from_old if not old_bindings.get(n, False)]
            if hidden:
                raise MoveRefused(
                    self._NOT_EXPORTED.format(names=", ".join(hidden), path=old_path)
                )
        if existing is not None:
            assert target_root is not None
            self._refuse_collisions(
                existing,
                target_root,
                old_module,
                new_module,
                new_path,
                name,
                needed,
                self._from_old_statement(
                    from_old, old_spelled, old_path, new_path, language
                ),
                from_old,
                guarded,
                language,
            )
        old_uses = _uses(remainder, name)
        if not keep_alias:
            self._refuse_wildcard_loss(
                old_module, old_path, source, root, name, language, old_uses
            )
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

        # 1. Cut from the old module, import the name back when still used.
        for edit in old_edits:
            patcher.replace_span(old_path, (edit.start, edit.end), edit.text)
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
                use=old_uses,
                # An export list `keep_alias` left in place already exports it.
                export=keep_alias and not listed,
            )
        # 2. Paste into the target with what it needs.
        guard_lines, guard_head = self._place_guarded(
            patcher, existing, target_root, guarded, new_path
        )
        paste = self._paste_text(
            moved_text,
            needed,
            from_old,
            old_spelled,
            old_path,
            new_path,
            language,
            guard_lines,
            guard_head,
        )
        if (
            language in _JS_LANGUAGES
            and top.type != cs.TS_EXPORT_STATEMENT
            and (listed or old_uses or keep_alias)
        ):
            # Whatever imports it from the destination needs it exported
            # there: the rewritten importers, and the old module.
            paste += f"\nexport {{ {name} }};\n"
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
            copied_imports=tuple(n.statement for n in needed) + tuple(guarded.values()),
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
    def _rebase_local_imports(
        source: bytes,
        top: Node,
        cut: _Cut,
        qn: str,
        old_path: str,
        new_path: str,
    ) -> str:
        """The cut text with each relative import inside it respelled for
        `new_path`, where it stays, in the scope it was written in.

        Copied verbatim, `from .deps import X` in a function moved from
        pkg/sub/util.py to pkg/core.py counted its dots from `pkg` and
        imported `pkg.deps` (or nothing) the first time the function ran.
        """
        edits: list[SpanEdit] = []
        stack = [top]
        while stack:
            current = stack.pop()
            stack.extend(current.named_children)
            if current.type != cs.TS_PY_IMPORT_FROM_STATEMENT:
                continue
            module = current.child_by_field_name(cs.FIELD_MODULE_NAME)
            if module is None or module.type != cs.TS_RELATIVE_IMPORT:
                continue
            spelled = _text(module)
            level = len(spelled) - len(spelled.lstrip(cs.SEPARATOR_DOT))
            # Climbing to or past the tree's top names no package any
            # destination could spell.
            if level - 1 >= len(Path(old_path).parent.parts):
                raise MoveRefused(
                    cs.MOVE_RELATIVE_IMPORT_ESCAPES.format(
                        statement=_text(current), qn=qn, path=old_path
                    )
                )
            rebased = _rebase_py_relative(spelled, old_path, new_path)
            if rebased != spelled:
                edits.append(
                    SpanEdit(
                        module.start_byte - cut.origin,
                        module.end_byte - cut.origin,
                        rebased.encode(cs.ENCODING_UTF8),
                    )
                )
        if not edits:
            return cut.text
        moved = apply_span_edits(cut.text.encode(cs.ENCODING_UTF8), edits)
        return moved.decode(cs.ENCODING_UTF8, errors="replace")

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
    def _listed_exports(source: bytes, root: Node, name: str) -> list[SpanEdit]:
        """The edits that take `name` out of the module's `export { ... }`
        lists: the entry alone, or the whole statement when it was the only
        one. Only an unaliased entry is taken, since importers of `name`
        are rewritten to the destination; an alias (`helper as h`) stays,
        and the old module imports the name back for it.
        """
        edits: list[SpanEdit] = []
        for child in root.children:
            if (
                child.type != cs.TS_EXPORT_STATEMENT
                or child.child_by_field_name(cs.FIELD_SOURCE) is not None
            ):
                continue
            clause = next(
                (c for c in child.named_children if c.type == cs.TS_EXPORT_CLAUSE),
                None,
            )
            if clause is None:
                continue
            specs = [
                spec
                for spec in clause.named_children
                if spec.type == cs.TS_EXPORT_SPECIFIER
            ]
            kept = [
                _text(spec)
                for spec in specs
                if _text(spec.child_by_field_name(cs.FIELD_NAME)) != name
                or spec.child_by_field_name(cs.FIELD_ALIAS) is not None
            ]
            if len(kept) == len(specs):
                continue
            if kept:
                edits.append(
                    SpanEdit(
                        clause.start_byte,
                        clause.end_byte,
                        f"{{ {', '.join(kept)} }}".encode(cs.ENCODING_UTF8),
                    )
                )
                continue
            # Its own bytes when other code shares its line: the whole-line
            # removal took `registerPlugin();` in `export { helper };
            # registerPlugin();` with it.
            own = _own_span(source, child, child)
            if own is not None:
                edits.append(SpanEdit(*own, b""))
                continue
            start = source.rfind(b"\n", 0, child.start_byte) + 1
            end = _line_end(source, child)
            # Swallow the blank lines that separated it from what follows.
            while source[end : end + 1] == b"\n":
                end += 1
            edits.append(SpanEdit(start, end, b""))
        return edits

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
        source: bytes,
        root: Node,
        moved_text: str,
        language: cs.SupportedLanguage | None,
    ) -> list[_NeededImport]:
        """The old module's import statements the moved text relies on.

        Only the module's own top-level imports are copied. The graph holds
        an import written inside a function as well, and pasting that one at
        the destination's top level makes an import the function ran only
        on demand (an optional backend) run, and fail, on load. One inside
        the moved definition travels with its text; one under a top-level
        `if` or `try` is a module binding, left to `_flow_bindings`.
        """
        top_level = [
            (child.start_byte, child.end_byte)
            for child in root.children
            if child.type in _IMPORT_TYPES
        ]
        rows = self.fetch_all(
            cq.CYPHER_GRAPH_IMPORTS_OF,
            {
                cs.KEY_PROJECT_PREFIX: f"{self.project}{cs.SEPARATOR_DOT}",
                cs.KEY_QN: old_module,
            },
        )
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
            try:
                at = line_col_to_byte(source, line, col)
            except PatcherError:
                continue
            if not any(start <= at < end for start, end in top_level):
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
            out.setdefault(narrowed, _NeededImport(narrowed, target, bound))
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
                cs.KEY_LONGER_PROJECT_PREFIXES: list(
                    _longer_project_prefixes(self.fetch_all, self.project)
                ),
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

    def _refuse_wildcard_loss(
        self,
        old_module: str,
        old_path: str,
        source: bytes,
        root: Node,
        name: str,
        language: cs.SupportedLanguage | None,
        old_uses: bool,
    ) -> None:
        """Refuse when a wildcard importer of the old module would lose `name`.

        The graph records `from pkg.util import *` (and JS `export *` or
        `import * as ns`) under the name `*`, which no importer rewrite
        matches: the move left it in place, and `from pkg.api import
        helper` through the re-export raised ImportError. The old module
        importing the name back keeps a Python star reaching it; in JS only
        an export does, which is `keep_alias`.
        """
        if language == cs.SupportedLanguage.PYTHON:
            if old_uses or not _star_exports(source, name):
                return
        elif not self._bindings(root).get(name, False):
            return
        importers = sorted(
            {
                row["path"]
                for row in graph_query.importers(
                    self.fetch_all, self.project, old_module
                )
                if row["imported_name"] == cs.IMPORTED_NAME_WILDCARD and row["path"]
            }
        )
        if importers:
            raise MoveRefused(
                cs.MOVE_WILDCARD_IMPORTER.format(
                    importers=cs.SEPARATOR_COMMA_SPACE.join(importers),
                    name=name,
                    path=old_path,
                )
            )

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
        use: bool,
        export: bool,
    ) -> None:
        """Import `name` back into the old module (`use`), re-export it
        from there (`export`), or both.

        Both is JavaScript's case only: a Python import of the name is
        already a re-export, but a JS import is not, and writing just the
        import when the old module still used the name took the old path
        away from every external importer `keep_alias` was meant to keep.
        """
        at = _import_block_end(source, root)
        if not at and language == cs.SupportedLanguage.PYTHON:
            # No imports: offset 0 would push a shebang, an encoding line or
            # the module docstring down, and the docstring stops being one.
            at = self._head_end(source, root)
        if language in _JS_LANGUAGES:
            spec = _relative_specifier(path, new_path)
            if not use:
                line = f"export {{ {name} }} from '{spec}';\n"
            else:
                line = f"import {{ {name} }} from '{spec}';\n"
                if export:
                    line += f"export {{ {name} }};\n"
        else:
            line = f"from {new_spelled} import {name}\n"
            if export and not use:
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
        guarded: list[str],
        guard_head: bool,
    ) -> str:
        lines = [n.statement.rstrip("\n") for n in needed]
        statement = self._from_old_statement(
            from_old, old_spelled, old_path, new_path, language
        )
        if statement is not None:
            lines.append(statement)
        if guard_head:
            lines.append(_TYPE_CHECKING_IMPORT)
        header = "\n".join(lines)
        if guarded:
            block = _TYPE_CHECKING_HEAD + "\n".join(_PY_INDENT + g for g in guarded)
            header = (header + "\n\n" if header else "") + block
        return (header + "\n\n\n" if header else "") + moved.rstrip("\n") + "\n"

    def _place_guarded(
        self,
        patcher: Patcher,
        existing: bytes | None,
        target_root: Node | None,
        guarded: dict[str, str],
        new_path: str,
    ) -> tuple[list[str], bool]:
        """Put the TYPE_CHECKING-only imports under the destination's guard.

        Merged into the destination's own `if TYPE_CHECKING:` when it has
        one (and an import it already holds is not repeated); otherwise
        returned for `_paste_text` to write in a new guard, with whether
        `TYPE_CHECKING` itself must be imported for it.
        """
        language = cs.SupportedLanguage.PYTHON
        if not guarded:
            return [], False
        if existing is None or target_root is None:
            return list(guarded.values()), True
        held = self._import_texts(existing, target_root, new_path)
        lines = [
            statement
            for local, statement in guarded.items()
            if not any(
                _binding_key(other, local, language, new_path)
                == _binding_key(statement, local, language, new_path)
                for other in held
            )
        ]
        block = _type_checking_block(target_root)
        if block is None:
            binds_flag = _TYPE_CHECKING in self._bindings(target_root) or any(
                _binding_key(other, _TYPE_CHECKING, language, new_path) is not None
                for other in held
            )
            return lines, bool(lines) and not binds_flag
        if lines:
            at = _line_end(existing, block)
            indent = " " * block.named_children[0].start_point[1]
            text = "".join(f"{indent}{line}\n" for line in lines)
            if at == len(existing) and not existing.endswith(b"\n"):
                text = "\n" + text
            patcher.replace_span(new_path, (at, at), text)
        return [], False

    @staticmethod
    def _import_texts(source: bytes, root: Node, path: str) -> list[str]:
        """A Python module's top-level imports and its TYPE_CHECKING ones."""
        top = [_text(c) for c in root.children if c.type in _IMPORT_TYPES]
        return top + list(_type_checking_imports(source, path, path).values())

    @staticmethod
    def _from_old_statement(
        from_old: list[str],
        old_spelled: str,
        old_path: str,
        new_path: str,
        language: cs.SupportedLanguage | None,
    ) -> str | None:
        """The import the destination needs of the old module's names."""
        if not from_old:
            return None
        if language in _JS_LANGUAGES:
            spec = _relative_specifier(new_path, old_path)
            return f"import {{ {', '.join(from_old)} }} from '{spec}';"
        return f"from {old_spelled} import {', '.join(from_old)}"

    def _refuse_collisions(
        self,
        existing: bytes,
        target_root: Node,
        old_module: str,
        new_module: str,
        new_path: str,
        name: str,
        needed: list[_NeededImport],
        from_old_statement: str | None,
        from_old: list[str],
        guarded: dict[str, str],
        language: cs.SupportedLanguage | None,
    ) -> None:
        """Refuse when anything the move binds at the destination rebinds
        a name the destination already binds to something else.

        Checking the moved name alone let a pasted `from pkg.util import
        RATE` replace the destination's own `RATE`, and every existing
        reader of it silently saw the old module's value. An import that
        binds the name to what it is already bound to replaces nothing.
        """
        defined = set(self._bindings(target_root))
        imports = [_text(c) for c in target_root.children if c.type in _IMPORT_TYPES]
        if language == cs.SupportedLanguage.PYTHON:
            carried, unsafe = _flow_bindings(existing)
            # A TYPE_CHECKING import is compared like any import: binding
            # the same name to the same thing under the guard is no clash.
            typed = _type_checking_imports(existing, new_path, new_path)
            defined |= carried | (unsafe - typed.keys())
            imports.extend(typed.values())
        # The destination's own import of the moved name from the old
        # module is rewired by the move, not rebound by it.
        rewired = any(
            row["module"] == new_module and row["imported_name"] == name
            for row in graph_query.importers(self.fetch_all, self.project, old_module)
        )
        if name in defined or (
            not rewired
            and any(
                _binding_key(other, name, language, new_path) is not None
                for other in imports
            )
        ):
            raise MoveRefused(self._COLLISION.format(path=new_path, name=name))
        added = [(n.local, n.statement) for n in needed]
        if from_old_statement is not None:
            added.extend((n, from_old_statement) for n in from_old)
        added.extend(guarded.items())
        for local, statement in added:
            key = _binding_key(statement, local, language, new_path)
            if local in defined or any(
                other_key is not None and other_key != key
                for other_key in (
                    _binding_key(other, local, language, new_path) for other in imports
                )
            ):
                raise MoveRefused(
                    cs.MOVE_IMPORT_COLLISION.format(path=new_path, name=local)
                )

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
        # What each staged file held when the move was planned: the patched
        # bytes the edits were built from, and nothing at a new destination.
        planned: dict[str, bytes | None] = {key: patcher.source(key) for key in results}
        if new_content is not None:
            planned[report.new_path] = None
            # `stage_into` parses every patched file; a new destination is
            # not patched, so it gets the same gate here.
            if self._parses(report.new_path, new_content):
                tx.stage(report.new_path, new_content)
            else:
                broken.append(report.new_path)
        # `stage` takes the disk as the baseline, so a file edited (or a
        # destination created) since planning would be overwritten with
        # content built from the old bytes, and commit's conflict check
        # would compare against the edit itself and pass.
        changed = [
            staged.path
            for staged in tx.staged
            if planned.get(staged.path, staged.before) != staged.before
        ]
        if changed:
            tx.rollback()
            raise MoveRefused(
                cs.MOVE_SOURCE_CHANGED.format(
                    files=cs.SEPARATOR_COMMA_SPACE.join(changed)
                )
            )
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
        try:
            delta = measure(
                self.fetch_all,
                self.project,
                self.repo_root,
                report.files,
                self.reingest,
                # An empty class has no fingerprint and no members to match
                # the two sides by, so the delta cannot infer this rename;
                # without it the move of one reads as a removal plus an
                # addition.
                declared_renames=(renamed,),
                reshaped_renames=(renamed,) if self._reshaped else (),
            )
        # The move has landed; a graph that cannot be measured is reported,
        # never raised past the committed edit (as for change_signature).
        except Exception as error:  # noqa: BLE001
            logger.warning(cs.MOVE_CONTRACT_UNMEASURED.format(error=error))
            return report._replace(
                verdict=None,
                # `ValueError` and `ReingestAborted` are raised before the
                # re-ingest writes anything, so the graph is still whole.
                graph_incomplete=not isinstance(error, ValueError | ReingestAborted),
                message=cs.MOVE_CONTRACT_UNMEASURED.format(error=error),
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
        try:
            self.reingest(list(report.files))
        # The files are restored; the graph may have lost the subtree the
        # re-ingest deleted before failing. Say so, never raise.
        except Exception as error:  # noqa: BLE001
            message = cs.MOVE_ROLLBACK_UNMEASURED.format(reasons=reasons, error=error)
            logger.warning(message)
            return report._replace(
                applied=False, graph_incomplete=True, message=message
            )
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
    the new file where the specifier is relative.

    The result is pasted at the top of the destination, so it must bind
    `alias` there and nothing else: a sibling binding copied along can
    shadow a name the destination already defines.
    """
    if language in _JS_LANGUAGES:
        return _narrow_js(statement, alias, old_path, new_path)
    if parsed := _match_py_from(statement):
        # main replaced the _PY_FROM regex with a token parser returning
        # (lead, module, mid, names); only those two fields are needed here.
        _lead, module, _mid, raw_names = parsed
        entries, _open, _close = _split_names(raw_names)
        kept = [e for e in entries if _local_name(e) == alias]
        if not kept:
            return None
        module = _rebase_py_relative(module, old_path, new_path)
        return f"from {module} import {kept[0]}"
    return _narrow_py_import(statement, alias)


def _binding_key(
    statement: str,
    local: str,
    language: cs.SupportedLanguage | None,
    path: str,
) -> tuple[str, ...] | None:
    """What the import `statement` binds `local` to, in `path`; None when
    it does not bind `local` at all.

    Two statements that agree on it bind the same thing however they were
    spelled: `import os` and `import os.path` both bind `os` to `os`.
    """
    if language in _JS_LANGUAGES:
        # A side-effect import (`import './x'`) binds nothing.
        if _JS_FROM.search(statement) is None:
            return None
        narrowed = _narrow_js(statement, local, path, path)
        return None if narrowed is None else (narrowed.rstrip(";").strip(),)
    try:
        tree = ast.parse(statement.strip())
    except SyntaxError:
        return None
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname == local:
                    return (alias.name, "", local)
                if (
                    alias.asname is None
                    and alias.name.split(cs.SEPARATOR_DOT)[0] == local
                ):
                    return (local, "", local)
        elif isinstance(node, ast.ImportFrom):
            module = cs.SEPARATOR_DOT * node.level + (node.module or "")
            for alias in node.names:
                if (alias.asname or alias.name) == local:
                    return (module, alias.name, local)
    return None


def _rebase_py_relative(module: str, old_path: str, new_path: str) -> str:
    """`module` as `new_path` must spell it to reach what `old_path` meant.

    A relative import counts its dots from the importing file's package,
    so copying `from .deps import X` from `pkg/sub/util.py` into
    `pkg/core.py` would silently switch it from `pkg.sub.deps` to
    `pkg.deps` -- the wrong module, or none. The target is resolved from
    the old file's directory and re-expressed from the new one's; both
    are directories, which is also right for `__init__.py`, whose `.` is
    its own package.
    """
    level = len(module) - len(module.lstrip(cs.SEPARATOR_DOT))
    if level == 0:
        return module
    rest = [p for p in module[level:].split(cs.SEPARATOR_DOT) if p]
    old_package = list(Path(old_path).parent.parts)
    ups = level - 1
    if ups > len(old_package):
        # Already escapes the tree it was written in; nothing to rebase.
        return module
    target = old_package[: len(old_package) - ups] + rest
    new_package = list(Path(new_path).parent.parts)
    common = 0
    while (
        common < min(len(target), len(new_package))
        and target[common] == new_package[common]
    ):
        common += 1
    if common == 0:
        # No shared package: a relative import cannot climb there.
        return cs.SEPARATOR_DOT.join(target)
    dots = cs.SEPARATOR_DOT * (len(new_package) - common + 1)
    return dots + cs.SEPARATOR_DOT.join(target[common:])


def _narrow_py_import(statement: str, alias: str) -> str:
    """`import a, b` reduced to the entry binding `alias`.

    `alias` is the graph's local name: the `as` name, else the dotted
    path itself or its root (`import pkg.dep` binds `pkg`). Unparseable
    or unmatched input is returned whole: copying a spare binding is
    recoverable, omitting the needed one is a NameError.
    """
    text = statement.strip()
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return text
    if len(tree.body) != 1 or not isinstance(tree.body[0], ast.Import):
        return text
    for entry in tree.body[0].names:
        bound = entry.asname or entry.name
        if bound == alias or (
            entry.asname is None and entry.name.split(cs.SEPARATOR_DOT)[0] == alias
        ):
            suffix = f" as {entry.asname}" if entry.asname else ""
            return f"import {entry.name}{suffix}"
    return text


def _js_entry_local(entry: str) -> str:
    return _local_name(_TS_INLINE_TYPE.sub("", entry, count=1))


def _narrow_js(statement: str, alias: str, old_path: str, new_path: str) -> str | None:
    """A JS/TS import reduced to the one clause that binds `alias`.

    `import D, * as ns, { a } from 'm'` binds three kinds of name; each is
    kept on its own. Looking only inside the braces lost the default and
    namespace bindings (the moved code then referred to an unbound name)
    and dragged the default along with a named entry.
    """
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
    keyword = _JS_KEYWORD.match(text)
    source_at = _JS_FROM.search(text)
    if keyword is None or source_at is None or source_at.start() < keyword.end():
        # A side-effect import (`import './x'`) binds nothing to narrow.
        return text.strip()
    clause = text[keyword.end() : source_at.start()].strip()
    tail = text[source_at.start() :]
    named = _JS_NAMED.search(clause)
    if named is not None:
        entries = [e.strip() for e in named.group("names").split(",") if e.strip()]
        kept = [e for e in entries if _js_entry_local(e) == alias]
        if kept:
            return f"{keyword.group(0)}{{ {', '.join(kept)} }}{tail}".strip()
        clause = (clause[: named.start()] + clause[named.end() :]).strip()
    for part in (p.strip() for p in clause.split(",")):
        namespace = _JS_NAMESPACE.fullmatch(part)
        if (namespace.group("name") if namespace else part) == alias:
            return f"{keyword.group(0)}{part}{tail}".strip()
    return None


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
