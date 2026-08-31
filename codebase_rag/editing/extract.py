"""`extract(qn, span, new_name)` and `inline(qn)`: edit-algebra ops 4 and 5
(issue #1535).

Extract and inline are the two operations that reduce duplication in
practice: `cgr duplicates` finds clones, these act on them. Together with
rename, change_signature and move they complete the minimal algebra.

`extract` takes a run of whole statements inside a function, works out the
names it reads from the enclosing scope (inputs) and the names it binds that
the rest of the function still reads (outputs), writes a new function with
those as parameters and return values, and replaces the span with a call.
Spans that leave the function early (`return`, `break`, `continue`, `yield`)
cannot be expressed as one call and are refused.

`inline` substitutes a single-return function at each call site, binding
arguments to parameters, and deletes the definition (and the imports that
bound its name) once no caller remains. A callee with `heuristic` or
`dynamic` callers is refused: a site the graph only guessed cannot be
rewritten with confidence.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple

from tree_sitter import Node

from .. import constants as cs
from .. import graph_query
from ..graph_query import QueryFn
from ..language_spec import get_language_for_extension
from ..parser_loader import load_parsers
from ..parsers.call_processor import _find_call_arguments_node, _split_call_arguments
from .contract import Expectation, Reingest, Verdict, measure, verify
from .imports import (
    _JS_NAMED,
    ImportSite,
    _local_name,
    _match_py_from,
    _split_names,
)
from .move import _cut_span, _definition_at, _statement_text, _text
from .patcher import Patcher, line_col_to_byte
from .rename import _name_token
from .signature import _call_at
from .transaction import EditTransaction, StagedTree, VerificationResult, undo_last

_AMBIGUOUS = frozenset(
    {
        cs.EdgeResolution.HEURISTIC.value,
        cs.EdgeResolution.OVERLOAD.value,
        cs.EdgeResolution.DYNAMIC.value,
    }
)
_JS_LANGUAGES = frozenset({cs.SupportedLanguage.JS, cs.SupportedLanguage.TS})
_IDENTIFIERS = frozenset({cs.TS_IDENTIFIER, cs.TS_PY_IDENTIFIER})
_EARLY_EXITS = frozenset(
    {
        cs.TS_PY_RETURN_STATEMENT,
        cs.TS_PY_BREAK_STATEMENT,
        cs.TS_PY_CONTINUE_STATEMENT,
        cs.TS_PY_YIELD,
        cs.TS_RETURN_STATEMENT,
        cs.TS_BREAK_STATEMENT,
        cs.TS_CONTINUE_STATEMENT,
    }
)
_NESTED_SCOPES = frozenset(
    {
        cs.TS_PY_FUNCTION_DEFINITION,
        cs.TS_PY_CLASS_DEFINITION,
        cs.TS_PY_LAMBDA,
        cs.TS_FUNCTION_DECLARATION,
        cs.TS_ARROW_FUNCTION,
        cs.TS_CLASS_DECLARATION,
    }
)
# Identifier positions that are names of things, not reads of variables.
_NON_READ_FIELDS = frozenset({cs.TS_PY_FIELD_ATTRIBUTE, cs.FIELD_PROPERTY})
_JS_DECLARATORS = frozenset({cs.TS_VARIABLE_DECLARATOR})
_SIMPLE_ARG = re.compile(r"^[\w.]+$|^-?\d+(\.\d+)?$|^(['\"]).*\2$")


class ExtractRefused(ValueError):
    """The span cannot be extracted as asked; nothing was written."""


class InlineRefused(ValueError):
    """The function cannot be inlined as asked; nothing was written."""

    def __init__(self, message: str, sites: list[str] | None = None) -> None:
        super().__init__(message)
        self.sites = sites or []


class ExtractReport(NamedTuple):
    qualified_name: str
    new_qualified_name: str
    path: str
    span: tuple[int, int]
    inputs: tuple[str, ...]
    outputs: tuple[str, ...]
    applied: bool
    transaction_id: str
    files: tuple[str, ...]
    diff: str
    message: str
    verdict: Verdict | None = None


class InlineReport(NamedTuple):
    qualified_name: str
    sites: tuple[str, ...]
    definition_removed: bool
    applied: bool
    transaction_id: str
    files: tuple[str, ...]
    diff: str
    message: str
    verdict: Verdict | None = None


# --- scope analysis -------------------------------------------------------------


def _reads(node: Node, out: list[str]) -> None:
    """Identifiers read in `node`, in order, attribute and property names
    excluded; nested scopes are descended (a closure still reads)."""
    stack = [node]
    while stack:
        current = stack.pop()
        if current.type in _IDENTIFIERS:
            parent = current.parent
            field = (
                parent.field_name_for_child(_index_in(parent, current))
                if parent
                else None
            )
            if field not in _NON_READ_FIELDS and not (
                parent is not None
                and parent.type == cs.TS_PY_KEYWORD_ARGUMENT
                and field == cs.FIELD_NAME
            ):
                name = _text(current)
                if name and name not in out:
                    out.append(name)
            continue
        stack.extend(reversed(current.children))


def _index_in(parent: Node, child: Node) -> int:
    for index, candidate in enumerate(parent.children):
        if candidate.id == child.id:
            return index
    return -1


def _binds(node: Node, out: list[str]) -> None:
    """Names bound by `node` (assignment targets, declarators, for
    targets, `as` targets, nested def/class names), in order."""
    stack = [node]
    while stack:
        current = stack.pop()
        kind = current.type
        if kind in (cs.TS_PY_ASSIGNMENT, cs.TS_PY_AUGMENTED_ASSIGNMENT):
            left = current.child_by_field_name(cs.TS_FIELD_LEFT)
            if left is not None:
                _targets(left, out)
        elif kind == cs.TS_PY_FOR_STATEMENT:
            left = current.child_by_field_name(cs.TS_FIELD_LEFT)
            if left is not None:
                _targets(left, out)
        elif kind == cs.TS_PY_AS_PATTERN_TARGET:
            _targets(current, out)
        elif kind in _JS_DECLARATORS:
            named = current.child_by_field_name(cs.FIELD_NAME)
            if named is not None:
                _targets(named, out)
        elif kind == cs.TS_ASSIGNMENT_EXPRESSION:
            left = current.child_by_field_name(cs.TS_FIELD_LEFT)
            if left is not None and left.type in _IDENTIFIERS:
                _targets(left, out)
        elif kind in _NESTED_SCOPES:
            named = current.child_by_field_name(cs.FIELD_NAME)
            if named is not None:
                _targets(named, out)
            continue
        stack.extend(reversed(current.children))


def _targets(node: Node, out: list[str]) -> None:
    if node.type in _IDENTIFIERS:
        name = _text(node)
        if name and name not in out:
            out.append(name)
        return
    if node.type in (cs.TS_PY_ATTRIBUTE, cs.TS_PY_SUBSCRIPT, cs.TS_MEMBER_EXPRESSION):
        # `obj.x = ...` binds nothing in this scope.
        return
    for child in node.children:
        _targets(child, out)


_LOOPS = frozenset(
    {
        cs.TS_PY_FOR_STATEMENT,
        cs.TS_PY_WHILE_STATEMENT,
        cs.TS_FOR_STATEMENT,
        cs.TS_FOR_IN_STATEMENT,
        cs.TS_WHILE_STATEMENT,
        cs.TS_DO_STATEMENT,
    }
)
_LOOP_EXITS = frozenset(
    {
        cs.TS_PY_BREAK_STATEMENT,
        cs.TS_PY_CONTINUE_STATEMENT,
        cs.TS_BREAK_STATEMENT,
        cs.TS_CONTINUE_STATEMENT,
    }
)


def _early_exit(node: Node) -> Node | None:
    """A statement that leaves the span: a return or yield anywhere, or a
    break/continue whose loop lies outside the span."""
    stack: list[tuple[Node, bool]] = [(node, False)]
    while stack:
        current, in_loop = stack.pop()
        if current.type in _EARLY_EXITS and (
            current.type not in _LOOP_EXITS or not in_loop
        ):
            return current
        if current.type in _NESTED_SCOPES:
            continue
        inner = in_loop or current.type in _LOOPS
        stack.extend((child, inner) for child in current.children)
    return None


def _body_statements(definition: Node) -> list[Node]:
    body = definition.child_by_field_name(cs.FIELD_BODY)
    if body is None:
        return []
    return [c for c in body.named_children if c.type != cs.TS_COMMENT]


def _parameter_names(definition: Node) -> list[str]:
    params = definition.child_by_field_name(cs.FIELD_PARAMETERS)
    names: list[str] = []
    if params is None:
        return names
    for child in params.named_children:
        _targets(child, names)
    return names


class _Span(NamedTuple):
    statements: list[Node]
    before: list[Node]
    after: list[Node]


def _split_span(definition: Node, start: int, end: int) -> _Span:
    statements = _body_statements(definition)
    inside, before, after = [], [], []
    for statement in statements:
        first, last = statement.start_point[0] + 1, statement.end_point[0] + 1
        if last < start:
            before.append(statement)
        elif first > end:
            after.append(statement)
        elif first >= start and last <= end:
            inside.append(statement)
        else:
            raise ExtractRefused(
                cs.EXTRACT_SPLITS_STATEMENT.format(line=first, end=last)
            )
    if not inside:
        raise ExtractRefused(cs.EXTRACT_EMPTY_SPAN.format(start=start, end=end))
    return _Span(inside, before, after)


def _analyse(definition: Node, span: _Span) -> tuple[list[str], list[str]]:
    """(inputs, outputs) of the span within its function."""
    params = _parameter_names(definition)
    bound_before: list[str] = list(params)
    for statement in span.before:
        _binds(statement, bound_before)
    bound_in: list[str] = []
    inputs: list[str] = []
    for statement in span.statements:
        reads: list[str] = []
        _reads(statement, reads)
        for name in reads:
            if name in bound_before and name not in bound_in and name not in inputs:
                inputs.append(name)
        _binds(statement, bound_in)
    read_after: list[str] = []
    for statement in span.after:
        _reads(statement, read_after)
    outputs = [name for name in bound_in if name in read_after]
    return inputs, outputs


# --- extract -----------------------------------------------------------------------


def _indent_of(source: bytes, node: Node) -> str:
    line_start = source.rfind(b"\n", 0, node.start_byte) + 1
    text = source[line_start : node.start_byte].decode(
        cs.ENCODING_UTF8, errors="replace"
    )
    return text if text.strip() == "" else ""


def _dedent(text: str, indent: str) -> str:
    out = []
    for line in text.split("\n"):
        out.append(
            line[len(indent) :]
            if line.startswith(indent)
            else line.lstrip()
            if line.strip()
            else ""
        )
    return "\n".join(out)


def _reindent(text: str, indent: str) -> str:
    return "\n".join(indent + line if line.strip() else "" for line in text.split("\n"))


class Extractor:
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
            raise ExtractRefused(cs.EXTRACT_NO_GRAMMAR.format(path=path))
        return language, parser.parse(source).root_node

    def _locate(
        self, qn: str, patcher: Patcher
    ) -> tuple[str, Node, cs.SupportedLanguage | None, bytes, str]:
        row = graph_query.definition(self.fetch_all, self.project, qn, self.repo_root)
        if not row["found"] or not row["path"]:
            raise ExtractRefused(cs.EXTRACT_UNKNOWN.format(qn=qn))
        path = row["path"]
        source = patcher.source(path)
        language, root = self._parse(path, source)
        name = (row["name"] or qn.rsplit(cs.SEPARATOR_DOT, 1)[-1]).split("(")[0]
        token = _name_token(
            source, language, row["start_line"] or 1, row["end_line"] or 1, name
        )
        node = _definition_at(root, *token) if token else None
        if node is None or node.child_by_field_name(cs.FIELD_BODY) is None:
            raise ExtractRefused(
                cs.EXTRACT_NO_DEFINITION_TOKEN.format(qn=qn, path=path)
            )
        return path, node, language, source, str(row["label"])

    def plan(
        self, qn: str, span: tuple[int, int], new_name: str
    ) -> tuple[ExtractReport, Patcher]:
        if not re.fullmatch(r"[A-Za-z_]\w*", new_name):
            raise ExtractRefused(cs.RENAME_BAD_NAME.format(name=new_name))
        patcher = Patcher(self.repo_root)
        path, node, language, source, label = self._locate(qn, patcher)
        start, end = span
        parts = _split_span(node, start, end)
        exit_node = next(
            (e for e in (_early_exit(s) for s in parts.statements) if e is not None),
            None,
        )
        if exit_node is not None:
            raise ExtractRefused(
                cs.EXTRACT_EARLY_EXIT.format(
                    kind=exit_node.type, line=exit_node.start_point[0] + 1
                )
            )
        inputs, outputs = _analyse(node, parts)
        is_method = label == cs.NodeLabel.METHOD.value
        receiver = None
        if is_method and language == cs.SupportedLanguage.PYTHON:
            params = _parameter_names(node)
            if params and params[0] in (cs.PY_KEYWORD_SELF, cs.PY_KEYWORD_CLS):
                receiver = params[0]
                inputs = [i for i in inputs if i != receiver]
        first, last = parts.statements[0], parts.statements[-1]
        span_start = source.rfind(b"\n", 0, first.start_byte) + 1
        span_end = source.find(b"\n", last.end_byte)
        span_end = len(source) if span_end < 0 else span_end + 1
        body_indent = _indent_of(source, first)
        body_text = _dedent(
            source[span_start:span_end]
            .decode(cs.ENCODING_UTF8, errors="replace")
            .rstrip("\n"),
            body_indent,
        )
        def_indent = _indent_of(
            source,
            node
            if node.parent is None
            or node.parent.type
            not in (cs.TS_PY_DECORATED_DEFINITION, cs.TS_EXPORT_STATEMENT)
            else node.parent,
        )
        if language in _JS_LANGUAGES:
            function_text, call_text = _js_pieces(
                node,
                new_name,
                inputs,
                outputs,
                body_text,
                def_indent,
                body_indent,
                parts,
            )
        else:
            function_text, call_text = _py_pieces(
                new_name, inputs, outputs, body_text, def_indent, body_indent, receiver
            )
        patcher.replace_span(path, (span_start, span_end), call_text)
        # The new function goes right after the enclosing definition (or
        # its decorator/export wrapper), at the same indentation.
        holder = (
            node.parent
            if node.parent is not None
            and node.parent.type
            in (cs.TS_PY_DECORATED_DEFINITION, cs.TS_EXPORT_STATEMENT)
            else node
        )
        after_def = source.find(b"\n", holder.end_byte)
        after_def = len(source) if after_def < 0 else after_def + 1
        patcher.replace_span(path, (after_def, after_def), function_text)
        owner = (
            qn.rsplit(cs.SEPARATOR_DOT, 1)[0]
            if is_method
            else qn.rsplit(cs.SEPARATOR_DOT, 1)[0]
        )
        report = ExtractReport(
            qualified_name=qn,
            new_qualified_name=f"{owner}{cs.SEPARATOR_DOT}{new_name}",
            path=path,
            span=(start, end),
            inputs=tuple(inputs),
            outputs=tuple(outputs),
            applied=False,
            transaction_id="",
            files=(path,),
            diff="",
            message=cs.EXTRACT_PLANNED.format(inputs=len(inputs), outputs=len(outputs)),
        )
        return report, patcher

    def apply(self, qn: str, span: tuple[int, int], new_name: str) -> ExtractReport:
        report, patcher = self.plan(qn, span, new_name)
        outcome, broken = _commit(patcher, self.repo_root, self.verify)
        if broken:
            return report._replace(
                message=cs.EXTRACT_PARSE_FAILED.format(files=", ".join(broken))
            )
        report = report._replace(
            applied=outcome.applied,
            transaction_id=outcome.transaction_id,
            files=outcome.files,
            diff=outcome.diff,
            message=outcome.message,
        )
        if outcome.applied and self.reingest is not None:
            expectation = Expectation(
                operation=cs.CONTRACT_OP_EXTRACT,
                added=(report.new_qualified_name,),
                caller_count_unchanged=False,
            )
            report = _enforce(
                report,
                expectation,
                self.fetch_all,
                self.project,
                self.repo_root,
                self.reingest,
                cs.EXTRACT_CONTRACT_FAILED,
            )
        return report


def _py_pieces(
    new_name: str,
    inputs: list[str],
    outputs: list[str],
    body_text: str,
    def_indent: str,
    body_indent: str,
    receiver: str | None,
) -> tuple[str, str]:
    params = ([receiver] if receiver else []) + inputs
    lines = [f"def {new_name}({', '.join(params)}):", _reindent(body_text, "    ")]
    if outputs:
        lines.append(f"    return {', '.join(outputs)}")
    function_text = "\n\n" + _reindent("\n".join(lines), def_indent) + "\n"
    callee = f"{receiver}.{new_name}" if receiver else new_name
    call = f"{callee}({', '.join(inputs)})"
    if outputs:
        call = f"{', '.join(outputs)} = {call}"
    return function_text, f"{body_indent}{call}\n"


def _js_pieces(
    definition: Node,
    new_name: str,
    inputs: list[str],
    outputs: list[str],
    body_text: str,
    def_indent: str,
    body_indent: str,
    parts: _Span,
) -> tuple[str, str]:
    annotations = _js_param_annotations(definition)
    params = [f"{name}{annotations.get(name, '')}" for name in inputs]
    lines = [f"function {new_name}({', '.join(params)}) {{", _reindent(body_text, "  ")]
    if len(outputs) == 1:
        lines.append(f"  return {outputs[0]};")
    elif outputs:
        lines.append(f"  return {{ {', '.join(outputs)} }};")
    lines.append("}")
    function_text = "\n" + _reindent("\n".join(lines), def_indent) + "\n"
    call = f"{new_name}({', '.join(inputs)})"
    declared_in_span: list[str] = []
    for statement in parts.statements:
        _binds(statement, declared_in_span)
    fresh = all(
        name in declared_in_span and _declared_here(parts.statements, name)
        for name in outputs
    )
    if not outputs:
        text = f"{call};"
    elif len(outputs) == 1:
        text = f"const {outputs[0]} = {call};" if fresh else f"{outputs[0]} = {call};"
    else:
        text = (
            f"const {{ {', '.join(outputs)} }} = {call};"
            if fresh
            else f"({{ {', '.join(outputs)} }} = {call});"
        )
    return function_text, f"{body_indent}{text}\n"


def _declared_here(statements: list[Node], name: str) -> bool:
    for statement in statements:
        stack = [statement]
        while stack:
            current = stack.pop()
            if current.type in _JS_DECLARATORS:
                named = current.child_by_field_name(cs.FIELD_NAME)
                if named is not None and _text(named) == name:
                    return True
            stack.extend(current.children)
    return False


def _js_param_annotations(definition: Node) -> dict[str, str]:
    params = definition.child_by_field_name(cs.FIELD_PARAMETERS)
    out: dict[str, str] = {}
    if params is None:
        return out
    for child in params.named_children:
        pattern = child.child_by_field_name(cs.TS_FIELD_PATTERN)
        annotation = child.child_by_field_name(cs.FIELD_TYPE)
        if pattern is not None and annotation is not None:
            out[_text(pattern)] = _text(annotation)
    return out


# --- inline ------------------------------------------------------------------------


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
        expression = _text(returned)
        params = _parameter_names(node)
        receiver = (
            params[0]
            if label == cs.NodeLabel.METHOD.value
            and params
            and params[0] in (cs.PY_KEYWORD_SELF, cs.PY_KEYWORD_CLS)
            else None
        )
        positional = [p for p in params if p != receiver]
        defaults = _defaults(node)
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
        rewritten_all = True
        for row in callers:
            c_path, line, col = row["path"], row["line"], row["col"]
            if not isinstance(c_path, str) or line is None or col is None:
                rewritten_all = False
                continue
            c_source = patcher.source(c_path)
            _lang, root = self._extractor._parse(c_path, c_source)
            call = _call_at(root, line, col)
            args_node = _find_call_arguments_node(call) if call is not None else None
            if call is None or args_node is None:
                rewritten_all = False
                continue
            substituted = _substitute(
                expression, returned, positional, defaults, receiver, call, args_node
            )
            if substituted is None:
                rewritten_all = False
                continue
            patcher.replace_span(c_path, (call.start_byte, call.end_byte), substituted)
            sites.append((c_path, line))
        removed = False
        if rewritten_all:
            cut = _cut_span(source, node)
            patcher.replace_span(path, (cut.start, cut.end), "")
            self._drop_imports(patcher, qn, path)
            removed = True
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

    def _drop_imports(self, patcher: Patcher, qn: str, path: str) -> None:
        from ..utils.path_utils import base_module_qn

        module = base_module_qn(Path(path), self.project)
        name = qn.rsplit(cs.SEPARATOR_DOT, 1)[-1]
        for row in graph_query.importers(self.fetch_all, self.project, module):
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
            source = patcher.source(site.path)
            statement = _statement_text(source, site)
            replacement = _without_entry(statement, name)
            start = line_col_to_byte(source, site.line, site.col)
            end = line_col_to_byte(source, site.end_line, site.end_col)
            if replacement is None:
                # The statement bound only this name: drop its whole line.
                start = source.rfind(b"\n", 0, start) + 1
                nl = source.find(b"\n", end)
                end = len(source) if nl < 0 else nl + 1
                patcher.replace_span(site.path, (start, end), "")
            elif replacement != statement:
                patcher.replace_span(site.path, (start, end), replacement)

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


def _defaults(definition: Node) -> dict[str, str]:
    params = definition.child_by_field_name(cs.FIELD_PARAMETERS)
    out: dict[str, str] = {}
    if params is None:
        return out
    for child in params.named_children:
        named = child.child_by_field_name(cs.FIELD_NAME)
        value = child.child_by_field_name(cs.FIELD_VALUE)
        if named is not None and value is not None:
            out[_text(named)] = _text(value)
    return out


def _substitute(
    expression: str,
    returned: Node,
    params: list[str],
    defaults: dict[str, str],
    receiver: str | None,
    call: Node,
    args_node: Node,
) -> str | None:
    positional, keyword = _split_call_arguments(args_node)
    bindings: dict[str, str] = {}
    for index, name in enumerate(params):
        if index < len(positional):
            bindings[name] = _text(positional[index])
        elif name in keyword:
            bindings[name] = _text(keyword[name])
        elif name in defaults:
            bindings[name] = defaults[name]
        else:
            return None
    if receiver is not None:
        function = call.child_by_field_name(cs.FIELD_FUNCTION)
        obj = (
            function.child_by_field_name(cs.TS_FIELD_OBJECT)
            if function is not None
            else None
        )
        if obj is None:
            return None
        bindings[receiver] = _text(obj)
    # Substitute by token position so `a` never touches `a.b`'s attribute or
    # a longer name; arguments that are not atoms are parenthesised.
    reads: list[Node] = []
    stack = [returned]
    while stack:
        current = stack.pop()
        if current.type in _IDENTIFIERS:
            parent = current.parent
            field = (
                parent.field_name_for_child(_index_in(parent, current))
                if parent
                else None
            )
            if field not in _NON_READ_FIELDS and _text(current) in bindings:
                reads.append(current)
            continue
        stack.extend(current.children)
    text = expression.encode(cs.ENCODING_UTF8)
    base = returned.start_byte
    for node in sorted(reads, key=lambda n: n.start_byte, reverse=True):
        value = bindings[_text(node)]
        if not _SIMPLE_ARG.match(value):
            value = f"({value})"
        text = (
            text[: node.start_byte - base]
            + value.encode(cs.ENCODING_UTF8)
            + text[node.end_byte - base :]
        )
    result = text.decode(cs.ENCODING_UTF8, errors="replace")
    if not _SIMPLE_ARG.match(result) and returned.type not in (
        cs.TS_PY_CALL,
        cs.TS_CALL_EXPRESSION,
        cs.TS_PY_PARENTHESIZED_EXPRESSION,
    ):
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


def _commit(
    patcher: Patcher,
    repo_root: Path,
    verifier: Callable[[StagedTree], VerificationResult | bool | None] | None,
):
    tx = EditTransaction(repo_root)
    results = patcher.stage_into(tx)
    broken = [key for key, result in results.items() if result.parses is False]
    if broken:
        tx.rollback()
        return None, broken

    def check(tree: StagedTree) -> VerificationResult | bool | None:
        return verifier(tree) if verifier is not None else True

    return tx.commit(check), []


def _enforce(
    report,
    expectation: Expectation,
    fetch_all: QueryFn,
    project: str,
    repo_root: Path,
    reingest: Reingest,
    failure: str,
):
    delta = measure(fetch_all, project, repo_root, report.files, reingest)
    verdict = verify(expectation, delta)
    if verdict.ok:
        return report._replace(verdict=verdict)
    undo_last(repo_root)
    reingest(list(report.files))
    return report._replace(
        applied=False,
        verdict=verdict,
        message=failure.format(reasons="; ".join(verdict.failures)),
    )


def extract(
    repo_root: Path,
    fetch_all: QueryFn,
    project_name: str,
    qualified_name: str,
    span: tuple[int, int],
    new_name: str,
    dry_run: bool = False,
    verify: Callable[[StagedTree], VerificationResult | bool | None] | None = None,
    reingest: Reingest | None = None,
) -> ExtractReport:
    extractor = Extractor(
        repo_root, fetch_all, project_name, verify=verify, reingest=reingest
    )
    if dry_run:
        report, _patcher = extractor.plan(qualified_name, span, new_name)
        return report
    return extractor.apply(qualified_name, span, new_name)


def inline(
    repo_root: Path,
    fetch_all: QueryFn,
    project_name: str,
    qualified_name: str,
    dry_run: bool = False,
    verify: Callable[[StagedTree], VerificationResult | bool | None] | None = None,
    reingest: Reingest | None = None,
) -> InlineReport:
    inliner = Inliner(
        repo_root, fetch_all, project_name, verify=verify, reingest=reingest
    )
    if dry_run:
        report, _patcher = inliner.plan(qualified_name)
        return report
    return inliner.apply(qualified_name)
