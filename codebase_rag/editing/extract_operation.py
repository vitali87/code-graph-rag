"""Extract-function planning and rewriting."""

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
from .contract import Expectation, Reingest
from .extract_scope import (
    _JS_DECLARATORS,
    _JS_LANGUAGES,
    _analyse,
    _binds,
    _body_statements,
    _dedent,
    _early_exit,
    _indent_of,
    _parameter_names,
    _reindent,
    _Span,
    _split_span,
    _string_lines,
)
from .extract_transaction import _commit, _enforce
from .extract_types import ExtractRefused, ExtractReport
from .move import _definition_at, _text
from .patcher import Patcher
from .rename import _name_token
from .transaction import StagedTree, VerificationResult

# Only a function or method body can become a call: a class or module body
# also has a `body` field, but its statements are declarations whose scope
# the extraction would change (Copilot, PR #2062).
_CALLABLE_LABELS = frozenset({cs.NodeLabel.FUNCTION.value, cs.NodeLabel.METHOD.value})
# Every function form starts its own async context, so an `await` inside a
# nested function (arrows included) does not make the span itself await.
_FUNCTION_SCOPES = frozenset(
    {
        cs.TS_PY_FUNCTION_DEFINITION,
        cs.TS_PY_LAMBDA,
        cs.TS_PY_CLASS_DEFINITION,
        cs.TS_FUNCTION_DECLARATION,
        cs.TS_FUNCTION_EXPRESSION,
        cs.TS_GENERATOR_FUNCTION,
        cs.TS_GENERATOR_FUNCTION_DECLARATION,
        cs.TS_METHOD_DEFINITION,
        cs.TS_ARROW_FUNCTION,
        cs.TS_CLASS_DECLARATION,
        cs.TS_CLASS_EXPRESSION,
    }
)
# An arrow function inherits `this` and `arguments` from its enclosing
# function, so only the other function forms hide them.
_JS_RECEIVER_SCOPES = _FUNCTION_SCOPES - {cs.TS_ARROW_FUNCTION}
# `await` (Python and JS), the `async` of `async for`/`async with`, and the
# `await (X)()` call the JS grammar spells with an identifier.
_AWAIT_TOKENS = frozenset({cs.JS_AWAIT_IDENTIFIER, cs.JS_ASYNC_KEYWORD})


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
        if row["label"] not in _CALLABLE_LABELS:
            raise ExtractRefused(
                cs.EXTRACT_NOT_CALLABLE.format(qn=qn, label=row["label"])
            )
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
        awaited = _first_in_scope(
            parts.statements,
            lambda n: (
                n.type in _AWAIT_TOKENS
                or (n.type == cs.TS_IDENTIFIER and _text(n) == cs.JS_AWAIT_IDENTIFIER)
            ),
            _FUNCTION_SCOPES,
        )
        if awaited is not None:
            # The helper would be synchronous and its call un-awaited, which
            # neither parser accepts (Greptile, PR #2062).
            raise ExtractRefused(
                cs.EXTRACT_AWAITS.format(
                    token=_text(awaited), line=awaited.start_point[0] + 1
                )
            )
        inputs, outputs = _analyse(node, parts)
        is_method = label == cs.NodeLabel.METHOD.value
        py_method = None
        js_method = (
            _js_method_form(qn, node)
            if language in _JS_LANGUAGES
            and (is_method or node.type == cs.TS_METHOD_DEFINITION)
            else None
        )
        if language in _JS_LANGUAGES:
            _refuse_lost_js_context(parts, receiver_kept=js_method is not None)
        if language == cs.SupportedLanguage.PYTHON:
            _refuse_shared_names(node, parts)
        if is_method and language == cs.SupportedLanguage.PYTHON:
            py_method = _py_method_form(qn, node)
            inputs = [i for i in inputs if i != py_method.receiver]
        # Generated text follows the file's newline style, so a CRLF file does
        # not gain LF-only lines (Greptile, PRs #2060 and #2062).
        newline = "\r\n" if b"\r\n" in source else "\n"
        first, last = parts.statements[0], parts.statements[-1]
        span_start = source.rfind(b"\n", 0, first.start_byte) + 1
        span_end = source.find(b"\n", last.end_byte)
        span_end = len(source) if span_end < 0 else span_end + 1
        body_indent = _indent_of(source, first)
        literal_lines = _string_lines(parts.statements, first.start_point[0])
        body_text = _dedent(
            source[span_start:span_end]
            .decode(cs.ENCODING_UTF8, errors="replace")
            .replace("\r\n", "\n")
            .rstrip("\n"),
            body_indent,
            literal_lines,
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
            function_text, call_text = _js_helper_and_call(
                node,
                new_name,
                inputs,
                outputs,
                body_text,
                def_indent,
                body_indent,
                parts,
                js_method,
                language == cs.SupportedLanguage.TS,
                literal_lines,
            )
        else:
            function_text, call_text = _python_helper_and_call(
                new_name,
                inputs,
                outputs,
                body_text,
                def_indent,
                body_indent,
                py_method,
                literal_lines,
            )
        function_text = function_text.replace("\n", newline)
        call_text = call_text.replace("\n", newline)
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
        # `_commit` returns no outcome only alongside broken files.
        assert outcome is not None
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
            enforced = _enforce(
                report.files,
                report.transaction_id,
                report.message,
                expectation,
                self.fetch_all,
                self.project,
                self.repo_root,
                self.reingest,
                cs.EXTRACT_CONTRACT_FAILED,
            )
            report = report._replace(**enforced._asdict())
        return report


def _python_helper_and_call(
    new_name: str,
    inputs: list[str],
    outputs: list[str],
    body_text: str,
    def_indent: str,
    body_indent: str,
    method: _PyMethod | None,
    literal_lines: frozenset[int],
) -> tuple[str, str]:
    receiver = method.receiver if method else None
    params = ([receiver] if receiver else []) + inputs
    lines = [method.decorator] if method and method.decorator else []
    lines.append(f"def {new_name}({', '.join(params)}):")
    # The body's string continuation lines keep their exact text through
    # both indentations; `keep` follows them past the header lines.
    keep = frozenset(n + len(lines) for n in literal_lines)
    lines.append(_reindent(body_text, "    ", literal_lines))
    if outputs:
        lines.append(f"    return {', '.join(outputs)}")
    function_text = "\n\n" + _reindent("\n".join(lines), def_indent, keep) + "\n"
    callee = f"{method.caller}.{new_name}" if method else new_name
    call = f"{callee}({', '.join(inputs)})"
    if outputs:
        call = f"{', '.join(outputs)} = {call}"
    return function_text, f"{body_indent}{call}\n"


class _PyMethod(NamedTuple):
    """How a helper extracted from a Python method is declared and called."""

    decorator: str
    receiver: str | None
    caller: str


def _py_method_form(qn: str, definition: Node) -> _PyMethod:
    """A helper keeps its method's binding: a class method's helper is a
    class method called through its `cls`, and a static method's helper is
    a static method called through the class, since a bare name inside a
    method does not see the class body (Greptile, PR #2932)."""
    holder = definition.parent
    decorators = (
        [
            _text(d.named_children[0])
            for d in holder.children
            if d.type == cs.TS_PY_DECORATOR and d.named_children
        ]
        if holder is not None and holder.type == cs.TS_PY_DECORATED_DEFINITION
        else []
    )
    params = _parameter_names(definition)
    if static := next((d for d in decorators if d in cs.STATIC_DECORATORS), None):
        owner = _py_class_name(holder or definition)
        if owner is None:
            raise ExtractRefused(cs.EXTRACT_PY_UNSUPPORTED_METHOD.format(qn=qn))
        return _PyMethod(f"{cs.DECORATOR_AT}{static}", None, owner)
    if bound := next((d for d in decorators if d in cs.CLASS_DECORATORS), None):
        if not params:
            raise ExtractRefused(cs.EXTRACT_PY_UNSUPPORTED_METHOD.format(qn=qn))
        return _PyMethod(f"{cs.DECORATOR_AT}{bound}", params[0], params[0])
    if not params or params[0] not in (cs.PY_KEYWORD_SELF, cs.PY_KEYWORD_CLS):
        raise ExtractRefused(cs.EXTRACT_PY_UNSUPPORTED_METHOD.format(qn=qn))
    return _PyMethod("", params[0], params[0])


def _py_class_name(member: Node) -> str | None:
    """The name a method body reaches its class by: the class's own name,
    unless the class is itself nested in a class body, which a method does
    not see."""
    body = member.parent
    owner = body.parent if body is not None else None
    if owner is None or owner.type != cs.TS_PY_CLASS_DEFINITION:
        return None
    outer = owner.parent
    if outer is not None and outer.type == cs.TS_PY_DECORATED_DEFINITION:
        outer = outer.parent
    enclosing = outer.parent if outer is not None else None
    if enclosing is not None and enclosing.type == cs.TS_PY_CLASS_DEFINITION:
        return None
    name = owner.child_by_field_name(cs.FIELD_NAME)
    return _text(name) if name is not None else None


_PY_SHARED = frozenset({cs.TS_PY_GLOBAL_STATEMENT, cs.TS_PY_NONLOCAL_STATEMENT})


def _refuse_shared_names(definition: Node, parts: _Span) -> None:
    """A `global` or `nonlocal` name belongs to the module or the outer
    function; written in the helper it would be the helper's own local, read
    before it is bound (Greptile, PR #2932). Carrying the declaration over
    is no fix for `nonlocal`: the helper is not nested where the original is."""
    moved = _first_in_scope(
        parts.statements, lambda n: n.type in _PY_SHARED, _FUNCTION_SCOPES
    )
    if moved is not None:
        raise ExtractRefused(
            cs.EXTRACT_SHARED_DECLARATION.format(
                keyword=moved.children[0].type, line=moved.start_point[0] + 1
            )
        )
    declared: dict[str, str] = {}
    stack = list(_body_statements(definition))
    while stack:
        current = stack.pop()
        if current.type in _FUNCTION_SCOPES:
            continue
        if current.type in _PY_SHARED:
            for name in current.named_children:
                declared.setdefault(_text(name), current.children[0].type)
            continue
        stack.extend(current.children)
    written: list[str] = []
    for statement in parts.statements:
        _binds(statement, written)
    if hit := next((name for name in written if name in declared), None):
        raise ExtractRefused(
            cs.EXTRACT_SHARED_NAME.format(name=hit, keyword=declared[hit])
        )


class _JsMethod(NamedTuple):
    """How a helper extracted from a class method is declared and called."""

    modifier: str
    receiver: str


def _js_method_form(qn: str, definition: Node) -> _JsMethod:
    """A class method's helper must itself be a method: a class body cannot
    hold a `function` declaration (Greptile and Copilot, PRs #2060 and
    #2062). A static method calls through the class name, since `this` is
    unbound when the static is called detached."""
    body = definition.parent
    if (
        definition.type != cs.TS_METHOD_DEFINITION
        or body is None
        or body.type != cs.TS_CLASS_BODY
    ):
        raise ExtractRefused(cs.EXTRACT_JS_UNSUPPORTED_METHOD.format(qn=qn))
    if not any(child.type == cs.TS_STATIC for child in definition.children):
        return _JsMethod(modifier="", receiver=cs.TS_THIS)
    owner = body.parent.child_by_field_name(cs.FIELD_NAME) if body.parent else None
    if owner is None:
        raise ExtractRefused(cs.EXTRACT_JS_UNSUPPORTED_METHOD.format(qn=qn))
    return _JsMethod(modifier=f"{cs.TS_STATIC} ", receiver=_text(owner))


def _refuse_lost_js_context(parts: _Span, receiver_kept: bool) -> None:
    """`this` and `arguments` mean the enclosing function's receiver and
    argument list; a helper called on its own sees neither, so the result
    would change silently (Greptile, PR #2062). A helper method keeps
    `this`, never `arguments`."""
    used = _first_in_scope(
        parts.statements,
        lambda n: (
            (n.type == cs.TS_THIS and not receiver_kept)
            or (n.type == cs.TS_IDENTIFIER and _text(n) == cs.JS_ARGUMENTS_OBJECT)
        ),
        _JS_RECEIVER_SCOPES,
    )
    if used is not None:
        raise ExtractRefused(
            cs.EXTRACT_JS_CONTEXT.format(word=_text(used), line=used.start_point[0] + 1)
        )


def _first_in_scope(
    statements: list[Node], match: Callable[[Node], bool], scopes: frozenset[str]
) -> Node | None:
    """First node in the statements satisfying `match`, not descending into
    the nested `scopes`."""
    stack = list(reversed(statements))
    while stack:
        current = stack.pop()
        if current.type in scopes:
            continue
        if match(current):
            return current
        stack.extend(reversed(current.children))
    return None


def _js_helper_and_call(
    definition: Node,
    new_name: str,
    inputs: list[str],
    outputs: list[str],
    body_text: str,
    def_indent: str,
    body_indent: str,
    parts: _Span,
    method: _JsMethod | None,
    typed: bool,
    literal_lines: frozenset[int],
) -> tuple[str, str]:
    annotations = _js_param_annotations(definition)
    if typed:
        annotations = _ts_input_types(definition, parts, inputs, annotations)
    params = [f"{name}{annotations.get(name, '')}" for name in inputs]
    header = f"{method.modifier}{new_name}" if method else f"function {new_name}"
    lines = [f"{header}({', '.join(params)}) {{"]
    # A name the span assigns without declaring it is the enclosing
    # function's local; moved into the helper it would be an undeclared
    # global, which strict code rejects. Once overwrites stopped counting as
    # reads such a name is no longer a parameter either, so declare it here.
    if undeclared := [
        name for name in _assigned_names(parts.statements) if name not in inputs
    ]:
        lines.append(f"  let {', '.join(undeclared)};")
    keep = frozenset(n + len(lines) for n in literal_lines)
    lines.append(_reindent(body_text, "  ", literal_lines))
    if len(outputs) == 1:
        lines.append(f"  return {outputs[0]};")
    elif outputs:
        lines.append(f"  return {{ {', '.join(outputs)} }};")
    lines.append("}")
    function_text = "\n" + _reindent("\n".join(lines), def_indent, keep) + "\n"
    callee = f"{method.receiver}.{new_name}" if method else new_name
    call = f"{callee}({', '.join(inputs)})"
    declared_in_span: list[str] = []
    for statement in parts.statements:
        _binds(statement, declared_in_span)
    fresh = [
        name
        for name in outputs
        if name in declared_in_span and _declared_here(parts.statements, name)
    ]
    keyword = _declaration_keyword(parts, fresh)
    if not outputs:
        text = f"{call};"
    elif len(outputs) == 1:
        text = (
            f"{keyword} {outputs[0]} = {call};" if fresh else f"{outputs[0]} = {call};"
        )
    elif len(fresh) == len(outputs):
        text = f"{keyword} {{ {', '.join(outputs)} }} = {call};"
    else:
        # Some outputs are new and some reassign outer names: declare the new
        # ones first, or the destructuring assigns undeclared names and
        # strict code throws (Greptile, PR #2062).
        declare = f"let {', '.join(fresh)};\n{body_indent}" if fresh else ""
        text = f"{declare}({{ {', '.join(outputs)} }} = {call});"
    return function_text, f"{body_indent}{text}\n"


_JS_ASSIGNMENTS = frozenset(
    {
        cs.TS_JS_ASSIGNMENT_EXPRESSION,
        cs.TS_JS_AUGMENTED_ASSIGNMENT_EXPRESSION,
        cs.TS_JS_UPDATE_EXPRESSION,
    }
)


def _assigned_names(statements: list[Node]) -> list[str]:
    """Plain names the span assigns but does not declare, in order.

    Nested functions are skipped: their assignments run in their own scope
    or, for a closure over an outer name, are not the helper's to declare.
    """
    found: list[str] = []
    stack = list(reversed(statements))
    while stack:
        current = stack.pop()
        if current.type in cs.JS_TS_FUNCTION_NODES:
            continue
        if current.type in _JS_ASSIGNMENTS:
            target = current.child_by_field_name(
                cs.FIELD_LEFT
            ) or current.child_by_field_name(cs.TS_JS_FIELD_ARGUMENT)
            if target is not None and target.type == cs.TS_IDENTIFIER:
                name = _text(target)
                if name not in found and not _declared_here(statements, name):
                    found.append(name)
        stack.extend(reversed(current.children))
    return found


def _declared_here(statements: list[Node], name: str) -> bool:
    return _declarator(statements, name) is not None


def _declarator(
    statements: list[Node], name: str, skip: frozenset[str] = frozenset()
) -> Node | None:
    for statement in statements:
        stack = [statement]
        while stack:
            current = stack.pop()
            if current.type in skip:
                continue
            if current.type in _JS_DECLARATORS:
                named = current.child_by_field_name(cs.FIELD_NAME)
                if named is not None and _text(named) == name:
                    return current
            stack.extend(current.children)
    return None


def _declaration_keyword(parts: _Span, fresh: list[str]) -> str:
    """How the call declares the outputs the span declared: `const` unless
    later code writes one, which a `const` rejects at runtime (Greptile,
    PR #2932); a `var` stays a `var`, so a later redeclaration still parses."""
    kinds: set[str | None] = set()
    for name in fresh:
        declarator = _declarator(parts.statements, name)
        holder = declarator.parent if declarator is not None else None
        kinds.add(holder.type if holder is not None else None)
    if kinds == {cs.TS_VARIABLE_DECLARATION}:
        return cs.TS_JS_VAR_KIND
    written = _written_names(parts.after)
    if cs.TS_VARIABLE_DECLARATION in kinds or any(n in written for n in fresh):
        return cs.JS_LET_KIND
    return cs.JS_CONST_KIND


def _written_names(statements: list[Node]) -> set[str]:
    """Plain names any assignment or update in `statements` writes, nested
    functions included: a closure that writes the name runs later."""
    found: set[str] = set()
    stack = list(statements)
    while stack:
        current = stack.pop()
        if current.type in _JS_ASSIGNMENTS:
            target = current.child_by_field_name(
                cs.FIELD_LEFT
            ) or current.child_by_field_name(cs.TS_JS_FIELD_ARGUMENT)
            if target is not None and target.type == cs.TS_IDENTIFIER:
                found.add(_text(target))
        stack.extend(current.children)
    return found


def _ts_input_types(
    definition: Node, parts: _Span, inputs: list[str], annotations: dict[str, str]
) -> dict[str, str]:
    """Each input's declared type: its parameter's annotation, or the one on
    the local declaration before the span. An untyped parameter stays
    untyped, as implicit as in the original; any other input with no
    annotation refuses, since strict mode rejects an untyped parameter the
    original had inferred (Greptile, PR #2932)."""
    typed = dict(annotations)
    implicit = _implicit_parameters(definition)
    for name in inputs:
        if name in typed or name in implicit:
            continue
        declarator = _declarator(parts.before, name, _FUNCTION_SCOPES)
        annotation = (
            declarator.child_by_field_name(cs.FIELD_TYPE)
            if declarator is not None
            else None
        )
        if annotation is None:
            raise ExtractRefused(cs.EXTRACT_TS_UNTYPED_INPUT.format(name=name))
        typed[name] = _text(annotation)
    return typed


def _implicit_parameters(definition: Node) -> set[str]:
    """Plain parameters with neither a type nor a default: implicitly `any`."""
    params = definition.child_by_field_name(cs.FIELD_PARAMETERS)
    out: set[str] = set()
    for child in params.named_children if params is not None else []:
        pattern = child.child_by_field_name(cs.TS_FIELD_PATTERN)
        if (
            pattern is not None
            and pattern.type == cs.TS_IDENTIFIER
            and child.child_by_field_name(cs.FIELD_TYPE) is None
            and child.child_by_field_name(cs.FIELD_VALUE) is None
        ):
            out.add(_text(pattern))
    return out


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
