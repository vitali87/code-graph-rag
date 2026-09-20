"""Inline-function planning and rewriting."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from tree_sitter import Node

from .. import constants as cs
from .. import graph_query
from ..graph_query import QueryFn
from ..parsers.call_processor import _find_call_arguments_node, _split_call_arguments
from .contract import Expectation, Reingest
from .extract_operation import Extractor
from .extract_scope import (
    _AMBIGUOUS,
    _IDENTIFIERS,
    _NON_READ_FIELDS,
    _SIMPLE_ARG,
    _body_statements,
    _index_in,
    _parameter_names,
)
from .extract_transaction import _commit, _enforce
from .extract_types import ExtractRefused, InlineRefused, InlineReport
from .imports import _JS_NAMED, ImportSite, _local_name, _match_py_from, _split_names
from .move import _cut_span, _statement_text, _text
from .patcher import Patcher, line_col_to_byte
from .signature import _call_at
from .transaction import StagedTree, VerificationResult


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
