"""Inline-function planning and rewriting."""

from __future__ import annotations

import builtins
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import NamedTuple

from tree_sitter import Node

from .. import constants as cs
from .. import cypher_queries as cq
from .. import graph_query
from ..graph_query import QueryFn
from ..parsers.call_processor import _find_call_arguments_node, _split_call_arguments
from .contract import Expectation, Reingest
from .extract_operation import Extractor
from .extract_scope import (
    _AMBIGUOUS,
    _IDENTIFIERS,
    _NON_READ_FIELDS,
    _body_statements,
    _index_in,
)
from .extract_transaction import _commit, _enforce
from .extract_types import ExtractRefused, InlineRefused, InlineReport
from .imports import _JS_NAMED, ImportSite, _local_name, _match_py_from, _split_names
from .move import _cut_span, _statement_text, _text
from .patcher import Patcher, SpanEdit, apply_span_edits, line_col_to_byte
from .signature import _call_at, _site_end
from .transaction import StagedTree, VerificationResult

# Evaluating any of these runs code, so an argument moved past one of them
# no longer runs first (Greptile, PR #2058).
_EVALUATING = frozenset(
    {
        cs.TS_PY_CALL,
        cs.TS_CALL_EXPRESSION,
        cs.TS_NEW_EXPRESSION,
        cs.TS_PY_YIELD,
        cs.TS_JS_YIELD_EXPRESSION,
        cs.TS_AWAIT_EXPRESSION,
    }
)
# A read under one of these may run zero times or many times.
_LAZY = frozenset(
    {
        cs.TS_PY_CONDITIONAL_EXPRESSION,
        cs.TS_PY_BOOLEAN_OPERATOR,
        cs.TS_JS_TERNARY_EXPRESSION,
    }
)
_SHORT_CIRCUIT = frozenset({"&&", "||", "??"})
# Nested scopes rebind names and defer evaluation; assignments rebind them.
_REFUSED_IN_EXPRESSION = frozenset(
    {
        cs.TS_PY_LAMBDA,
        cs.TS_PY_LIST_COMPREHENSION,
        cs.TS_PY_SET_COMPREHENSION,
        cs.TS_PY_DICTIONARY_COMPREHENSION,
        cs.TS_PY_GENERATOR_EXPRESSION,
        cs.TS_PY_NAMED_EXPRESSION,
        cs.TS_ARROW_FUNCTION,
        cs.TS_FUNCTION_EXPRESSION,
        cs.TS_GENERATOR_FUNCTION,
        cs.TS_CLASS_EXPRESSION,
        cs.TS_JS_ASSIGNMENT_EXPRESSION,
        cs.TS_JS_AUGMENTED_ASSIGNMENT_EXPRESSION,
    }
)
_LITERALS = frozenset(
    {
        cs.TS_PY_INTEGER,
        cs.TS_PY_FLOAT,
        cs.TS_PY_NONE,
        cs.TS_TRUE,
        cs.TS_FALSE,
        cs.TS_JS_NUMBER,
        cs.TS_JS_NULL,
    }
)
_STRINGS = frozenset({cs.TS_PY_STRING, cs.TS_TEMPLATE_STRING})
_INTERPOLATIONS = frozenset({cs.TS_PY_INTERPOLATION, cs.TS_TEMPLATE_SUBSTITUTION})
_ACCESSES = frozenset({cs.TS_PY_ATTRIBUTE, cs.TS_MEMBER_EXPRESSION})
_NEGATIONS = frozenset({cs.TS_PY_UNARY_OPERATOR, cs.TS_JS_UNARY_EXPRESSION})
# Postfix forms bind tighter than any operator at the call site, so a result
# of one of these shapes needs no parentheses of its own.
_POSTFIX = frozenset(
    {
        cs.TS_PY_CALL,
        cs.TS_CALL_EXPRESSION,
        cs.TS_PY_PARENTHESIZED_EXPRESSION,
        cs.TS_PY_SUBSCRIPT,
        cs.TS_SUBSCRIPT_EXPRESSION,
        *_ACCESSES,
        *_IDENTIFIERS,
        *_LITERALS,
        *_STRINGS,
    }
)
_SPREADS = frozenset(
    {cs.TS_PY_LIST_SPLAT, cs.TS_PY_DICTIONARY_SPLAT, cs.TS_SPREAD_ELEMENT}
)
# Bindings the callee body gets from how it is called, not from its
# parameters: copied into a caller they bind to the caller's receiver.
_IMPLICIT_TYPES = frozenset({cs.TS_THIS, cs.TS_SUPER})
_IMPLICIT_NAMES = frozenset({cs.KEYWORD_SUPER, cs.TS_JS_ARGUMENTS_NAME})
_SCOPES = frozenset(
    {
        cs.TS_PY_FUNCTION_DEFINITION,
        cs.TS_PY_CLASS_DEFINITION,
        cs.TS_FUNCTION_DECLARATION,
        cs.TS_GENERATOR_FUNCTION_DECLARATION,
        cs.TS_METHOD_DEFINITION,
        cs.TS_CLASS_DECLARATION,
        *_REFUSED_IN_EXPRESSION,
    }
)
_PY_BUILTINS = frozenset(dir(builtins))


class _Param(NamedTuple):
    name: str
    default: Node | None
    positional: bool
    keyword: bool


class Inliner:
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
        self._extractor = Extractor(repo_root, fetch_all, project_name)

    def plan(self, qn: str) -> tuple[InlineReport, Patcher]:
        patcher = Patcher(self.repo_root)
        try:
            path, node, language, source, label = self._extractor._locate(qn, patcher)
        except ExtractRefused as refused:
            raise InlineRefused(str(refused)) from refused
        statements = [s for s in _body_statements(node) if not _is_docstring(s)]
        if len(statements) != 1 or statements[0].type not in (
            cs.TS_PY_RETURN_STATEMENT,
            cs.TS_RETURN_STATEMENT,
        ):
            raise InlineRefused(cs.INLINE_NOT_SINGLE_RETURN.format(qn=qn))
        returned = next((c for c in statements[0].named_children), None)
        if returned is None:
            raise InlineRefused(cs.INLINE_NOT_SINGLE_RETURN.format(qn=qn))
        _refuse_unsupported(qn, node, returned)
        params = _parameters(qn, node)
        receiver = (
            params[0].name
            if label == cs.NodeLabel.METHOD.value
            and params
            and params[0].name in (cs.PY_KEYWORD_SELF, cs.PY_KEYWORD_CLS)
            else None
        )
        if receiver is not None:
            params = params[1:]
        bound_names = {p.name for p in params} | ({receiver} if receiver else set())
        free = _free_names(returned, bound_names)
        callers = graph_query.callers(self.fetch_all, self.project, qn)
        guessed = [
            f"{r['path']}:{r['line']}"
            for r in callers
            if isinstance(r.get("resolution"), str) and r["resolution"] in _AMBIGUOUS
        ]
        if guessed:
            raise InlineRefused(
                cs.INLINE_GUESSED_CALLERS.format(sites=", ".join(sorted(guessed))),
                guessed,
            )
        sites: list[tuple[str, int]] = []
        # Every queued rewrite, so the post-inline text of a file can be
        # checked before the definition and its imports are removed.
        edits: dict[str, list[SpanEdit]] = {}
        rewritten_all = True
        for row in callers:
            c_path, line, col = row["path"], row["line"], row["col"]
            if not isinstance(c_path, str) or line is None or col is None:
                rewritten_all = False
                continue
            c_source = patcher.source(c_path)
            c_language, root = self._extractor._parse(c_path, c_source)
            # The recorded end picks the exact call: `helper(2).upper()` starts
            # where `helper(2)` does (Copilot, PR #2064).
            call = _call_at(root, line, col, _site_end(row))
            args_node = _find_call_arguments_node(call) if call is not None else None
            if call is None or args_node is None:
                rewritten_all = False
                continue
            substituted = None
            if _scope_safe(free, c_path == path, c_language, root, call):
                substituted = _substitute(returned, params, receiver, call, args_node)
            if substituted is None:
                rewritten_all = False
                continue
            patcher.replace_span(c_path, (call.start_byte, call.end_byte), substituted)
            edits.setdefault(c_path, []).append(
                SpanEdit(
                    call.start_byte,
                    call.end_byte,
                    substituted.encode(cs.ENCODING_UTF8),
                )
            )
            sites.append((c_path, line))
        removed = False
        # A definition still used as a value (a callback, a registry entry)
        # is a REFERENCES edge, not a caller row: rewriting every call does
        # not make it dead (Copilot, PR #2060).
        if rewritten_all and not self._referenced(qn):
            removed = self._remove(patcher, qn, path, node, source, edits)
        report = InlineReport(
            qualified_name=qn,
            sites=tuple(f"{p}:{n}" for p, n in sorted(sites)),
            definition_removed=removed,
            applied=False,
            transaction_id="",
            files=tuple(sorted(patcher.pending)),
            diff="",
            message=cs.INLINE_PLANNED.format(count=len(sites), removed=removed),
        )
        return report, patcher

    def _referenced(self, qn: str) -> bool:
        params = {
            cs.KEY_PROJECT_PREFIX: f"{self.project}{cs.SEPARATOR_DOT}",
            cs.KEY_QN: qn,
        }
        return bool(self.fetch_all(cq.CYPHER_GRAPH_REFERENCES, params))

    def _remove(
        self,
        patcher: Patcher,
        qn: str,
        path: str,
        node: Node,
        source: bytes,
        edits: dict[str, list[SpanEdit]],
    ) -> bool:
        """Queue the definition's removal and its imports'; False keeps both.

        Every removal is checked against the file as it will read after the
        inline: a binding still named there (a value use, a decorator, a
        base, `__all__`, a re-export) keeps the import, and then the
        definition too, since an import of a deleted symbol fails at load
        (Greptile, PR #2058).
        """
        name = qn.rsplit(cs.SEPARATOR_DOT, 1)[-1]
        cut = _cut_span(source, node)
        cut_edit = SpanEdit(cut.start, cut.end, b"")
        if self._still_named(path, source, [*edits.get(path, []), cut_edit], name):
            return False
        drops = self._import_drops(patcher, qn, path, edits)
        if drops is None:
            return False
        patcher.replace_span(path, (cut.start, cut.end), "")
        for site_path, edit in drops:
            patcher.replace_span(site_path, (edit.start, edit.end), edit.text)
        return True

    def _still_named(
        self, path: str, source: bytes, edits: list[SpanEdit], name: str
    ) -> bool:
        final = apply_span_edits(source, edits)
        _language, root = self._extractor._parse(path, final)
        return _names(root, name)

    def _import_drops(
        self,
        patcher: Patcher,
        qn: str,
        path: str,
        edits: dict[str, list[SpanEdit]],
    ) -> list[tuple[str, SpanEdit]] | None:
        from ..utils.path_utils import base_module_qn

        module = base_module_qn(Path(path), self.project)
        name = qn.rsplit(cs.SEPARATOR_DOT, 1)[-1]
        drops: list[tuple[str, SpanEdit]] = []
        for row in graph_query.importers(self.fetch_all, self.project, module):
            if (
                row["imported_name"] != name
                or row["path"] is None
                or row["line"] is None
            ):
                continue
            local = row["alias"] or name
            # A package `__init__` import is its public surface, and a module
            # that others import the name from re-exports it: neither shows
            # a use inside the importing file.
            if Path(row["path"]).name == cs.INIT_PY or self._reexported(
                row["path"], local
            ):
                return None
            site = ImportSite(
                row["path"],
                row["line"],
                row["col"] or 0,
                row["end_line"] or row["line"],
                row["end_col"] or 0,
                row["alias"],
                row["imported_name"],
            )
            source = patcher.source(site.path)
            statement = _statement_text(source, site)
            replacement = _without_entry(statement, local)
            start = line_col_to_byte(source, site.line, site.col)
            end = line_col_to_byte(source, site.end_line, site.end_col)
            if replacement is None:
                # The statement bound only this name: drop its whole line.
                start = source.rfind(b"\n", 0, start) + 1
                nl = source.find(b"\n", end)
                end = len(source) if nl < 0 else nl + 1
                replacement = ""
            elif replacement == statement:
                continue
            drop = SpanEdit(start, end, replacement.encode(cs.ENCODING_UTF8))
            if self._still_named(
                site.path, source, [*edits.get(site.path, []), drop], local
            ):
                return None
            drops.append((site.path, drop))
        return drops

    def _reexported(self, importer_path: str, local: str) -> bool:
        from ..utils.path_utils import base_module_qn

        module = base_module_qn(Path(importer_path), self.project)
        return any(
            row["imported_name"] == local
            for row in graph_query.importers(self.fetch_all, self.project, module)
        )

    def apply(self, qn: str) -> InlineReport:
        report, patcher = self.plan(qn)
        outcome, broken = _commit(patcher, self.repo_root, self.verify)
        if broken:
            return report._replace(
                message=cs.INLINE_PARSE_FAILED.format(files=", ".join(broken))
            )
        report = report._replace(
            applied=outcome.applied,
            transaction_id=outcome.transaction_id,
            files=outcome.files,
            diff=outcome.diff,
            message=outcome.message,
        )
        if outcome.applied and self.reingest is not None:
            # The callee is meant to disappear and its sites were replaced
            # by its body, so "callers of the removed symbol" is the plan,
            # not a dangling reference.
            expectation = Expectation(
                operation=cs.CONTRACT_OP_INLINE,
                removed=(qn,) if report.definition_removed else (),
                caller_count_unchanged=False,
                no_dangling=False,
            )
            report = _enforce(
                report,
                expectation,
                self.fetch_all,
                self.project,
                self.repo_root,
                self.reingest,
                cs.INLINE_CONTRACT_FAILED,
            )
        return report


def _is_docstring(statement: Node) -> bool:
    return statement.type == cs.TS_PY_EXPRESSION_STATEMENT and any(
        c.type == cs.TS_PY_STRING for c in statement.named_children
    )


def _walk(node: Node) -> Iterator[Node]:
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(reversed(current.children))


def _refuse_unsupported(qn: str, definition: Node, returned: Node) -> None:
    """Refuse definitions whose call is not just its returned expression.

    An async call returns a coroutine or promise and a generator call an
    iterator; the bare expression is neither (Greptile and Copilot, PR
    #2058). `this`, `super` and `arguments` are bound by the call itself, so
    copied into a caller they read the caller's binding (Greptile and
    Copilot, PR #2060).
    """
    tokens = {c.type for c in definition.children if not c.is_named}
    if cs.TS_ASYNC_KEYWORD in tokens:
        raise InlineRefused(cs.INLINE_REFUSED_ASYNC.format(qn=qn))
    if (
        cs.TS_GENERATOR_STAR in tokens
        or definition.type
        in (cs.TS_GENERATOR_FUNCTION_DECLARATION, cs.TS_GENERATOR_FUNCTION)
        or any(
            n.type in (cs.TS_PY_YIELD, cs.TS_JS_YIELD_EXPRESSION)
            for n in _walk(returned)
        )
    ):
        raise InlineRefused(cs.INLINE_REFUSED_GENERATOR.format(qn=qn))
    for n in _walk(returned):
        if n.type in _IMPLICIT_TYPES or (
            n.type in _IDENTIFIERS and _text(n) in _IMPLICIT_NAMES
        ):
            raise InlineRefused(cs.INLINE_REFUSED_IMPLICIT.format(qn=qn, name=_text(n)))


def _parameters(qn: str, definition: Node) -> list[_Param]:
    """Each parameter's name, default and how a call may bind it.

    Variadic and destructured parameters refuse: their binding is not one
    argument expression per name (Greptile, PR #2058).
    """
    params = definition.child_by_field_name(cs.FIELD_PARAMETERS)
    out: list[_Param] = []
    if params is None:
        return out
    keyword_only = False
    for child in params.named_children:
        if child.type == cs.TS_COMMENT:
            continue
        if child.type == cs.TS_PY_KEYWORD_SEPARATOR:
            keyword_only = True
            continue
        if child.type == cs.TS_PY_POSITIONAL_SEPARATOR:
            out = [p._replace(keyword=False) for p in out]
            continue
        name, default = _parameter(child)
        if name is None:
            raise InlineRefused(
                cs.INLINE_REFUSED_PARAMETER.format(qn=qn, parameter=_text(child))
            )
        out.append(_Param(name, default, not keyword_only, True))
    return out


def _parameter(node: Node) -> tuple[str | None, Node | None]:
    if node.type in _IDENTIFIERS:
        return _text(node), None
    if node.type in (cs.TS_PY_DEFAULT_PARAMETER, cs.TS_PY_TYPED_DEFAULT_PARAMETER):
        target = node.child_by_field_name(cs.FIELD_NAME)
        default = node.child_by_field_name(cs.FIELD_VALUE)
    elif node.type == cs.TS_ASSIGNMENT_PATTERN:
        target = node.child_by_field_name(cs.FIELD_LEFT)
        default = node.child_by_field_name(cs.FIELD_RIGHT)
    elif node.type in (cs.TS_REQUIRED_PARAMETER, cs.TS_OPTIONAL_PARAMETER):
        target = node.child_by_field_name(cs.TS_FIELD_PATTERN)
        default = node.child_by_field_name(cs.FIELD_VALUE)
    elif node.type == cs.TS_PY_TYPED_PARAMETER:
        target = node.named_children[0] if node.named_children else None
        default = None
    else:
        return None, None
    if target is None or target.type not in _IDENTIFIERS:
        return None, None
    return _text(target), default


def _is_literal(node: Node) -> bool:
    if node.type in _NEGATIONS:
        operand = node.named_children[-1] if node.named_children else None
        return operand is not None and operand.type in _LITERALS
    if node.type in _STRINGS:
        return not any(n.type in _INTERPOLATIONS for n in _walk(node))
    return node.type in _LITERALS


def _is_atomic(node: Node) -> bool:
    """A name, a literal, or an attribute chain without calls: evaluating it
    has no effect, so it may be read any number of times, or none."""
    if node.type in _IDENTIFIERS or node.type in _IMPLICIT_TYPES:
        return True
    if node.type in _ACCESSES:
        obj = node.child_by_field_name(cs.TS_FIELD_OBJECT)
        return obj is not None and obj.type not in _LITERALS and _is_atomic(obj)
    return _is_literal(node)


def _bare(node: Node) -> bool:
    """Safe to splice without parentheses: `-1` is atomic but `x ** 2`
    would bind the minus last."""
    return _is_atomic(node) and node.type not in _NEGATIONS


def _read_field(node: Node) -> str | None:
    parent = node.parent
    return parent.field_name_for_child(_index_in(parent, node)) if parent else None


def _is_read(node: Node) -> bool:
    if node.type not in _IDENTIFIERS or _read_field(node) in _NON_READ_FIELDS:
        return False
    parent = node.parent
    # `f(x=...)` names a keyword, it does not read `x`.
    return not (
        parent is not None
        and parent.type == cs.TS_PY_KEYWORD_ARGUMENT
        and _read_field(node) == cs.FIELD_NAME
    )


def _free_names(returned: Node, bound: set[str]) -> set[str]:
    names = {
        _text(n)
        for n in _walk(returned)
        if (_is_read(n) or n.type == cs.TS_SHORTHAND_PROPERTY_IDENTIFIER)
        and _text(n) not in bound
    }
    return names


def _names(root: Node, name: str) -> bool:
    """Whether `name` is still spelled in the file as a binding use or a
    string (`__all__`, a string-keyed registry)."""
    for n in _walk(root):
        if (
            n.type in _IDENTIFIERS or n.type == cs.TS_SHORTHAND_PROPERTY_IDENTIFIER
        ) and _text(n) == name:
            if _read_field(n) not in _NON_READ_FIELDS:
                return True
        elif n.type in _STRINGS and _text(n).strip("'\"`") == name:
            return True
    return False


def _scope_safe(
    free: set[str],
    same_module: bool,
    language: cs.SupportedLanguage | None,
    root: Node,
    call: Node,
) -> bool:
    """Whether the callee's free names mean the same thing at `call`.

    Copied into another module they would bind to that module's names, or
    to nothing (Greptile, PR #2058); only builtins travel, and only into a
    file that never rebinds them. In the same module a local of the
    caller's enclosing function can still shadow the global the callee
    reads.
    """
    if not free:
        return True
    if not same_module:
        if language != cs.SupportedLanguage.PYTHON or not free <= _PY_BUILTINS:
            return False
        return not (free & _spelled(root))
    scope: Node | None = None
    current = call.parent
    while current is not None:
        if current.type in _SCOPES:
            scope = current
        current = current.parent
    if scope is None:
        return True
    return not (free & _spelled(scope))


def _spelled(node: Node) -> set[str]:
    return {
        _text(n)
        for n in _walk(node)
        if n.type in _IDENTIFIERS or n.type == cs.TS_SHORTHAND_PROPERTY_IDENTIFIER
    }


def _bind(
    params: list[_Param],
    receiver: str | None,
    call: Node,
    args_node: Node,
) -> tuple[dict[str, Node], list[tuple[str | None, Node]]] | None:
    """Each name's argument node, and every evaluated argument in call order.

    None refuses the call: a splat or spread has no single node per name
    (Greptile, PR #2058), and an argument the definition cannot accept
    fails at runtime today, which inlining must not hide.
    """
    if any(a.type in _SPREADS for a in args_node.named_children):
        return None
    positional, keyword = _split_call_arguments(args_node)
    positional = [a for a in positional if a.type != cs.TS_COMMENT]
    bound: dict[str, Node] = {}
    evaluated: list[tuple[str | None, Node]] = []
    function = call.child_by_field_name(cs.FIELD_FUNCTION)
    obj = (
        function.child_by_field_name(cs.TS_FIELD_OBJECT)
        if function is not None and function.type in _ACCESSES
        else None
    )
    if receiver is not None:
        if obj is None:
            return None
        bound[receiver] = obj
        evaluated.append((receiver, obj))
    elif obj is not None:
        evaluated.append((None, obj))
    slots = [p for p in params if p.positional]
    if len(positional) > len(slots):
        return None
    arguments: list[tuple[str, Node]] = [
        (slot.name, arg) for slot, arg in zip(slots, positional)
    ]
    by_name = {p.name: p for p in params}
    for key, value in keyword.items():
        param = by_name.get(key)
        if param is None or not param.keyword or key in dict(arguments):
            return None
        arguments.append((key, value))
    arguments.sort(key=lambda pair: pair[1].start_byte)
    for key, value in arguments:
        bound[key] = value
        evaluated.append((key, value))
    for param in params:
        if param.name in bound:
            continue
        # Python evaluates a default once, at definition time: copying a
        # non-literal default re-evaluates it at every site (Greptile,
        # PR #2058).
        if param.default is None or not _is_literal(param.default):
            return None
        bound[param.name] = param.default
    return bound, evaluated


def _evaluation_kept(
    returned: Node,
    reads: dict[str, list[Node]],
    evaluated: list[tuple[str | None, Node]],
) -> bool:
    """Whether every argument with an effect still runs once, in call order,
    before anything else the callee runs (Greptile, PRs #2058/#2060)."""
    effects = [n for n in _walk(returned) if n.type in _EVALUATING]
    last = -1
    for name, arg in evaluated:
        if _is_atomic(arg):
            continue
        uses = reads.get(name, []) if name is not None else []
        if len(uses) != 1:
            return False
        use = uses[0]
        if use.start_byte < last or _lazy(use, returned):
            return False
        if any(e.end_byte <= use.start_byte for e in effects):
            return False
        last = use.start_byte
    return True


def _lazy(node: Node, returned: Node) -> bool:
    current = node.parent
    while current is not None and current.start_byte >= returned.start_byte:
        if current.type in _LAZY:
            return True
        if current.type == cs.TS_BINARY_EXPRESSION:
            operator = current.child_by_field_name(cs.FIELD_OPERATOR)
            if operator is not None and _text(operator) in _SHORT_CIRCUIT:
                return True
        if current == returned:
            break
        current = current.parent
    return False


def _substitute(
    returned: Node,
    params: list[_Param],
    receiver: str | None,
    call: Node,
    args_node: Node,
) -> str | None:
    binding = _bind(params, receiver, call, args_node)
    if binding is None:
        return None
    bound, evaluated = binding
    reads: dict[str, list[Node]] = {}
    for current in _walk(returned):
        if current.type in _REFUSED_IN_EXPRESSION:
            return None
        if current.type == cs.TS_SHORTHAND_PROPERTY_IDENTIFIER and (
            _text(current) in bound
        ):
            return None
        if _is_read(current) and _text(current) in bound:
            reads.setdefault(_text(current), []).append(current)
    if not _evaluation_kept(returned, reads, evaluated):
        return None
    # Substitute by token position so `a` never touches `a.b`'s attribute or
    # a longer name; arguments that are not bare atoms are parenthesised.
    text = _text(returned).encode(cs.ENCODING_UTF8)
    base = returned.start_byte
    ordered = sorted(
        (n for uses in reads.values() for n in uses),
        key=lambda n: n.start_byte,
        reverse=True,
    )
    for node in ordered:
        arg = bound[_text(node)]
        value = _text(arg) if _bare(arg) else f"({_text(arg)})"
        text = (
            text[: node.start_byte - base]
            + value.encode(cs.ENCODING_UTF8)
            + text[node.end_byte - base :]
        )
    result = text.decode(cs.ENCODING_UTF8, errors="replace")
    # Decided on the node, not the text: `'a' + 'b'` looks like one string
    # to a regex and would bind to a trailing `.upper()` at the call site.
    if returned.type not in _POSTFIX:
        result = f"({result})"
    return result


def _without_entry(statement: str, name: str) -> str | None:
    """The import statement without `name`; None when nothing is left."""
    if parsed := _match_py_from(statement):
        # main replaced the _PY_FROM regex with a token parser returning
        # (lead, module, mid, names); the trailing whitespace the old `tail`
        # group captured now splits off the names, as imports.py does.
        lead, module, mid, raw_names = parsed
        names = raw_names.rstrip()
        tail = raw_names[len(names) :]
        entries, _open, _close = _split_names(names)
        kept = [e for e in entries if _local_name(e) != name]
        if not kept:
            return None
        return f"{lead}{module}{mid}{', '.join(kept)}{tail}"
    named = _JS_NAMED.search(statement)
    if named is not None:
        entries = [e.strip() for e in named.group("names").split(",") if e.strip()]
        kept = [e for e in entries if _local_name(e) != name]
        if not kept:
            return None
        return (
            statement[: named.start()]
            + "{ "
            + ", ".join(kept)
            + " }"
            + statement[named.end() :]
        )
    return statement


# --- shared -----------------------------------------------------------------------
