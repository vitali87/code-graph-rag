"""Derive a first-party call graph from a SCIP index.

SCIP records which symbol every identifier occurrence resolves to, computed
by a compiler-grade indexer (type inference, imports, overload resolution),
but it has no notion of a "call". A call edge is read off an occurrence when
all of these hold:

- the occurrence is a reference (not a definition or an import);
- its symbol is a first-party callable, i.e. defined in an indexed document
  as a method/function/constructor (a method descriptor `name().`, a callable
  kind, or for JS/TS a variable bound to a function expression);
- the source text after it is a call: `(` (past whitespace, `?.` and type
  arguments), a JS tagged template, or for Python a decorator use or a read
  of an `@property` getter, which runs the getter the way cgr models it.

A Python `Cls(...)` resolves to the class symbol; it is a call of
`Cls.__init__` when the class defines one, as cgr models it too.

The caller is the innermost callable whose body contains the occurrence, by
the `enclosing_range` the indexer emits for definitions (skipping a callable
whose own decorators or annotations hold the occurrence, which run in the
outer scope), or the file's module when no callable contains it.
"""

from __future__ import annotations

import bisect
import re
from collections import defaultdict
from collections.abc import Callable, Iterable
from pathlib import Path

from codebase_rag import constants as cs

from . import constants as sc
from .indexers import IndexerSpec
from .symbols import is_local, is_method_symbol, is_type_symbol, member_symbol
from .types_defs import (
    Definition,
    OracleEdges,
    ScipDocument,
    ScipIndex,
    ScipOccurrence,
    ScipRange,
    ScipSymbol,
)

_TYPE_ARGS = re.compile(sc.TYPE_ARGS_PATTERN)
_ARROW_INIT = re.compile(sc.ARROW_INIT_PATTERN)
_FUNCTION_KEYWORD = re.compile(sc.FUNCTION_KEYWORD_PATTERN)
_NAMED_FUNCTION = re.compile(sc.NAMED_FUNCTION_PATTERN)


def module_definition(path: str) -> Definition:
    return Definition(path, sc.MODULE_LINE, "")


class _Source:
    def __init__(self, text: str) -> None:
        self.text = text
        self.line_starts = [0]
        for index, char in enumerate(text):
            if char == "\n":
                self.line_starts.append(index + 1)

    def offset(self, line: int, char: int) -> int:
        if line >= len(self.line_starts):
            return len(self.text)
        return min(self.line_starts[line] + char, len(self.text))

    def position(self, offset: int) -> tuple[int, int]:
        line = bisect.bisect_right(self.line_starts, offset) - 1
        return line, offset - self.line_starts[line]

    def slice(self, span: ScipRange) -> str:
        return self.text[
            self.offset(span.start_line, span.start_char) : self.offset(
                span.end_line, span.end_char
            )
        ]


def _symbol_key(document: ScipDocument, symbol: str) -> str:
    # A `local N` symbol is unique within its document only.
    return f"{document.path}{sc.LOCAL_KEY_SEP}{symbol}" if is_local(symbol) else symbol


def _is_definition(occurrence: ScipOccurrence) -> bool:
    return bool(occurrence.roles & sc.ROLE_DEFINITION)


def _is_call(source: _Source, span: ScipRange, spec: IndexerSpec) -> bool:
    text = source.text
    index = source.offset(span.end_line, span.end_char)
    while index < len(text) and text[index] in sc.CALL_GAP_CHARS:
        index += 1
    if spec.tagged_templates and text.startswith("`", index):
        return True
    if text.startswith(sc.OPTIONAL_CALL, index):
        index += len(sc.OPTIONAL_CALL)
    if spec.type_arguments and (match := _TYPE_ARGS.match(text, index)):
        index = match.end()
    return text.startswith("(", index)


def _is_decorator_use(source: _Source, span: ScipRange) -> bool:
    start = source.offset(span.start_line, span.start_char)
    line_start = source.line_starts[span.start_line]
    head = source.text[line_start:start].strip()
    # `@name` or `@pkg.mod.name`: everything before the name on its line is
    # one `@` followed by a dotted prefix.
    return head.startswith("@") and all(
        part.isidentifier() for part in head[1:].split(".")[:-1]
    )


def _decorated_with(
    source: _Source, body: ScipRange | None, name_line: int, decorators: frozenset[str]
) -> bool:
    if body is None or not decorators:
        return False
    for line in range(body.start_line, name_line):
        decorator = source.slice(ScipRange(line, 0, line + 1, 0)).strip()
        if decorator.startswith("@"):
            leaf = decorator[1:].split("(")[0].rsplit(".", 1)[-1].strip()
            if leaf in decorators:
                return True
    return False


def _arrow_body(source: _Source, span: ScipRange) -> ScipRange | None:
    """The body of `name = (...) => ...` / `name = function ...`, syntactically."""
    start = source.offset(span.end_line, span.end_char)
    match = _ARROW_INIT.match(source.text, start)
    if match is None:
        return None
    text = source.text
    index = match.end()
    depth = 0
    end = len(text)
    for cursor in range(index, len(text)):
        char = text[cursor]
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
            if depth < 0:
                end = cursor
                break
            if depth == 0 and char == "}" and text[index:cursor].lstrip()[:1] == "{":
                end = cursor + 1
                break
        elif depth == 0 and char in ";\n":
            end = cursor
            break
    first = source.position(source.offset(span.start_line, span.start_char))
    last = source.position(end)
    return ScipRange(first[0], first[1], last[0], last[1])


def _matching(text: str, index: int, opener: str, closer: str) -> int | None:
    """The index just past the `closer` matching the `opener` at `index`."""
    depth = 0
    for cursor in range(index, len(text)):
        if text[cursor] == opener:
            depth += 1
        elif text[cursor] == closer:
            depth -= 1
            if depth == 0:
                return cursor + 1
    return None


def _function_expression_body(source: _Source, span: ScipRange) -> ScipRange | None:
    """The body of `function name(...) { ... }` when SCIP gives it no range.

    scip-typescript emits a named function *expression* (an argument, an
    `export default` operand) as a local symbol with no enclosing range, so
    without this the calls inside it would fall through to the module.
    """
    text = source.text
    start = source.offset(span.start_line, span.start_char)
    line_start = source.line_starts[span.start_line]
    if _FUNCTION_KEYWORD.search(text, line_start, start) is None:
        return None
    params = text.find("(", source.offset(span.end_line, span.end_char))
    if params == -1 or (close := _matching(text, params, "(", ")")) is None:
        return None
    brace = text.find("{", close)
    if brace == -1 or (end := _matching(text, brace, "{", "}")) is None:
        return None
    last = source.position(end)
    return ScipRange(span.start_line, span.start_char, last[0], last[1])


class _Callables:
    def __init__(self) -> None:
        self.definitions: dict[str, Definition] = {}
        self.bodies: dict[str, ScipRange] = {}
        self.properties: set[str] = set()
        self.types: set[str] = set()
        self.overrides: dict[str, set[str]] = defaultdict(set)


def _collect(
    documents: Iterable[ScipDocument],
    read: Callable[[str], _Source | None],
    spec: IndexerSpec,
) -> _Callables:
    found = _Callables()
    for document in documents:
        source = read(document.path)
        if source is None:
            continue
        infos: dict[str, ScipSymbol] = {s.symbol: s for s in document.symbols}
        for info in document.symbols:
            for relation in info.relationships:
                if relation.is_implementation and not is_local(relation.symbol):
                    found.overrides[relation.symbol].add(info.symbol)
        for occurrence in document.occurrences:
            if not _is_definition(occurrence):
                continue
            symbol = occurrence.symbol
            key = _symbol_key(document, symbol)
            if is_type_symbol(symbol):
                found.types.add(key)
                continue
            info = infos.get(symbol)
            kind = info.kind if info is not None else 0
            body = occurrence.enclosing
            callable_symbol = is_method_symbol(symbol) or kind in sc.CALLABLE_KINDS
            if not callable_symbol and spec.function_bindings:
                syntactic = _arrow_body(source, occurrence.range) or (
                    _function_expression_body(source, occurrence.range)
                )
                if syntactic is not None:
                    callable_symbol, body = True, body or syntactic
            if not callable_symbol:
                continue
            name = source.slice(occurrence.range)
            if not name or occurrence.range.start_line != occurrence.range.end_line:
                continue
            found.definitions[key] = Definition(
                document.path, occurrence.range.start_line, name
            )
            if body is not None:
                found.bodies[key] = body
            if _decorated_with(
                source, body, occurrence.range.start_line, spec.property_decorators
            ):
                found.properties.add(key)
        if spec.function_bindings:
            _add_unindexed_functions(document.path, source, found)
    return found


def _add_unindexed_functions(path: str, source: _Source, found: _Callables) -> None:
    """Named function expressions the indexer emitted no definition for.

    scip-typescript gives `export default ok && function adapter(config) {...}`
    or `wrap(async function run() {...})` no symbol at all, so the calls inside
    would be credited to the module. Such a function is nobody's callee (its
    name is only in scope inside it), but it is the caller of what it calls.
    """
    defined = {d for d in found.definitions.values() if d.path == path}
    for match in _NAMED_FUNCTION.finditer(source.text):
        line, col = source.position(match.start(1))
        name = match.group(1)
        if Definition(path, line, name) in defined:
            continue
        span = ScipRange(line, col, line, col + len(name))
        if (body := _function_expression_body(source, span)) is None:
            continue
        key = f"{path}{sc.LOCAL_KEY_SEP}{sc.UNINDEXED_KEY.format(line=line, col=col)}"
        found.definitions[key] = Definition(path, line, name)
        found.bodies[key] = body


def _start(span: ScipRange) -> tuple[int, int]:
    return span.start_line, span.start_char


def _end(span: ScipRange) -> tuple[int, int]:
    return span.end_line, span.end_char


def _callers(
    path: str,
    sites: list[tuple[ScipRange, str]],
    bodies: list[tuple[ScipRange, str]],
    found: _Callables,
) -> list[tuple[Definition, str]]:
    """Each call site's caller, by one sweep over the file's nested bodies.

    Bodies nest, so walking the sites in source order with a stack of the
    bodies open at the current position finds the innermost one in time
    proportional to the nesting depth rather than the number of callables.
    """
    ordered = sorted(
        bodies, key=lambda b: (_start(b[0]), tuple(-x for x in _end(b[0])))
    )
    stack: list[tuple[ScipRange, str]] = []
    pending = 0
    out: list[tuple[Definition, str]] = []
    for span, target in sorted(sites, key=lambda site: _start(site[0])):
        here = _start(span)
        while pending < len(ordered) and _start(ordered[pending][0]) <= here:
            stack.append(ordered[pending])
            pending += 1
        while stack and _end(stack[-1][0]) <= here:
            stack.pop()
        caller = module_definition(path)
        for body, key in reversed(stack):
            definition = found.definitions[key]
            # A decorator or annotation above the name runs in the outer
            # scope; an overlapping (ill-nested) body is skipped, not trusted.
            if span.start_line >= definition.line and _start(body) <= here < _end(body):
                caller = definition
                break
        out.append((caller, target))
    return out


def _widen(
    edges: set[tuple[Definition, Definition]],
    callee_keys: dict[tuple[Definition, Definition], set[str]],
    found: _Callables,
) -> set[tuple[Definition, Definition]]:
    widened = set(edges)
    for edge, keys in callee_keys.items():
        stack = list(keys)
        seen = set(keys)
        while stack:
            for impl in found.overrides.get(stack.pop(), ()):
                if impl not in seen:
                    seen.add(impl)
                    stack.append(impl)
                    if (target := found.definitions.get(impl)) is not None:
                        widened.add((edge[0], target))
    return widened


def oracle_edges(index: ScipIndex, root: Path, spec: IndexerSpec) -> OracleEdges:
    documents = [d for d in index.documents if d.path.endswith(spec.suffixes)]
    cache: dict[str, _Source | None] = {}

    def read(path: str) -> _Source | None:
        if path not in cache:
            try:
                text = (root / path).read_text(encoding=cs.ENCODING_UTF8)
            except (OSError, UnicodeDecodeError):
                cache[path] = None
            else:
                cache[path] = _Source(text)
        return cache[path]

    found = _collect(documents, read, spec)
    bodies_by_path: dict[str, list[tuple[ScipRange, str]]] = defaultdict(list)
    for key, body in found.bodies.items():
        bodies_by_path[found.definitions[key].path].append((body, key))
    edges: set[tuple[Definition, Definition]] = set()
    callee_keys: dict[tuple[Definition, Definition], set[str]] = defaultdict(set)
    for document in documents:
        source = read(document.path)
        if source is None:
            continue
        sites: list[tuple[ScipRange, str]] = []
        for occurrence in document.occurrences:
            if occurrence.roles & (sc.ROLE_DEFINITION | sc.ROLE_IMPORT):
                continue
            key = _symbol_key(document, occurrence.symbol)
            span = occurrence.range
            if key in found.definitions:
                if (
                    key in found.properties
                    or _is_call(source, span, spec)
                    or (spec.decorator_calls and _is_decorator_use(source, span))
                ):
                    sites.append((span, key))
            elif spec.constructor_member and key in found.types:
                ctor = member_symbol(key, spec.constructor_member)
                if ctor in found.definitions and _is_call(source, span, spec):
                    sites.append((span, ctor))
        for caller, target in _callers(
            document.path, sites, bodies_by_path[document.path], found
        ):
            edge = (caller, found.definitions[target])
            edges.add(edge)
            callee_keys[edge].add(target)
    return OracleEdges(
        edges=edges,
        dispatch_edges=_widen(edges, callee_keys, found),
        callables=frozenset(found.definitions.values()),
    )
