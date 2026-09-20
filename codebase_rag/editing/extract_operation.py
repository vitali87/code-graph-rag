"""Extract-function planning and rewriting."""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

from tree_sitter import Node

from .. import constants as cs
from .. import graph_query
from ..graph_query import QueryFn
from ..language_spec import get_language_for_extension
from ..parser_loader import load_parsers
from .contract import Expectation, Reingest
from .extract_scope import (
    _JS_DECLARATORS,
    _JS_LANGUAGES,
    _analyse,
    _binds,
    _dedent,
    _early_exit,
    _indent_of,
    _parameter_names,
    _reindent,
    _Span,
    _split_span,
)
from .extract_transaction import _commit, _enforce
from .extract_types import ExtractRefused, ExtractReport
from .move import _definition_at, _text
from .patcher import Patcher
from .rename import _name_token
from .transaction import StagedTree, VerificationResult


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
