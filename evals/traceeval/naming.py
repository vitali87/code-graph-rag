"""Name cgr's definitions the way TraceEval's tracers name them.

The ground truth spells every endpoint in its tracer's own scheme, so a cgr
edge can only be scored once both of its endpoints are renamed into it:

- Python: `<module>.<qualname>` with `<locals>` dropped, the module being the
  file's path with `/` turned into `.` -- cgr's qualified name minus its
  project and module prefix, rejoined onto that module.
- JavaScript: the same shape. Module-level code is always attributed to
  `main`, because the tracer concatenates every file into one script whose
  call stack starts at `main`.
- Java: `<package>.<Class>:<method>(<params>)`, built by the tracer from the
  method's source text with a regular expression. Rather than approximate the
  rule, `java_tracer_names` runs the tracer's own pattern over the file and
  the cgr method is matched to the header it covers, so quirks of the rule
  (a `final` parameter contributes `final`; the innermost enclosing *class or
  interface* names an enum's methods) are reproduced, not guessed.

A definition cgr names `anonymous_<line>_<col>` (or `iife_arrow_...`,
`lambda_...`) has no counterpart: the
tracer either leaves such a callback unwrapped, so calls made inside it are
credited to the enclosing function, or names it by an ordinal (`<arrow3>`)
that cannot be derived from cgr. Its calls are credited to its nearest named
ancestor, which matches the unwrapped case, and calls *to* it are not scored.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from typing import NamedTuple

from codebase_rag import constants as cs

from .. import constants as ec
from . import constants as tc
from .types_defs import CgrDefinition

_JAVA_PACKAGE = re.compile(tc.JAVA_PACKAGE_PATTERN)
_JAVA_LOCAL_TYPE = re.compile(tc.JAVA_LOCAL_TYPE_PATTERN)
_JAVA_SIGNATURE = re.compile(tc.JAVA_SIGNATURE_PATTERN)
# scripts/tracers/java_tracer.py, the pattern `trace_java` instruments with.
_JAVA_TRACED_HEADER = re.compile(
    r"((?:public|private|protected|static|final|synchronized)\s+)*"
    r"([\w<>\[\].]+\s+)?"
    r"(\w+)\s*\(([^)]*)\)\s*(?:throws\s+[\w.,\s]+)?\s*\{"
)
_JAVA_NOT_METHODS = frozenset(
    {"if", "for", "while", "switch", "catch", "return", "new", "try"}
)
_JAVA_DEFAULT_CLASS = "Main"
_DUPLICATE_SUFFIX = re.compile(tc.DUPLICATE_SUFFIX_PATTERN)
_CGR_ANONYMOUS = re.compile(ec.CGR_ANONYMOUS_PATTERN)
_JS_OBJECT_BINDING = re.compile(tc.JS_OBJECT_BINDING_PATTERN)
_JS_NESTED_PROPERTY = re.compile(tc.JS_NESTED_PROPERTY_PATTERN)


def _enclosing_brace(source: str, position: int) -> int | None:
    # Naive brace counting, as the tracer itself does: braces inside strings
    # or comments miscount here exactly as they miscount there.
    depth = 0
    for index in range(position - 1, -1, -1):
        char = source[index]
        if char == "}":
            depth += 1
        elif char == "{":
            if depth == 0:
                return index
            depth -= 1
    return None


class JavaTracedMethod(NamedTuple):
    line: int
    method: str
    qualified: str


class Translation(NamedTuple):
    # The TraceEval name, or None for a definition the tracer cannot name.
    name: str | None
    # Who a call made inside this definition is credited to: the name itself,
    # or for an anonymous definition its nearest named ancestor.
    owner: str | None


def _module_name(path: str, language: tc.Language) -> str:
    stem = path
    for suffix in tc.LANGUAGE_SUFFIXES[language]:
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    return stem.replace(tc.PATH_SEP, tc.DOT)


def _is_anonymous(segment: str) -> bool:
    return _CGR_ANONYMOUS.match(segment) is not None


def _java_tracer_param(token: str, package: str, local_types: frozenset[str]) -> str:
    if mapped := tc.JAVA_TRACER_TYPE_MAP.get(token):
        return mapped
    if token[:1].isupper():
        if package and token in local_types:
            return f"{package}{tc.DOT}{token}"
        return tc.JAVA_TRACER_LANG_ALIASES.get(token, token)
    return token


def _java_tracer_params(params: str, package: str, local_types: frozenset[str]) -> str:
    types = [
        _java_tracer_param(parts[0], package, local_types)
        for param in params.split(tc.JAVA_PARAM_JOIN)
        if (parts := param.strip().split())
    ]
    return tc.JAVA_PARAM_JOIN.join(types)


def _java_enclosing_class(before: str) -> str:
    # The tracer's own walk: the innermost `class|interface` whose opening
    # brace is still open at the header, else the last one declared.
    decls = [(m.start(), m.group(1)) for m in _JAVA_LOCAL_TYPE.finditer(before)]
    if not decls:
        return _JAVA_DEFAULT_CLASS
    opening = {before.find("{", pos): name for pos, name in decls}
    depth = 0
    stack: list[tuple[str, int]] = []
    for index, char in enumerate(before):
        if char == "{":
            depth += 1
            if (name := opening.get(index)) is not None:
                stack.append((name, depth))
        elif char == "}":
            depth -= 1
            while stack and stack[-1][1] > depth:
                stack.pop()
    return stack[-1][0] if stack else decls[-1][1]


def java_tracer_names(source: str) -> list[JavaTracedMethod]:
    """Every method header the Java tracer instruments in `source`, named as it names it."""
    package_match = _JAVA_PACKAGE.search(source)
    package = package_match.group(1) if package_match else ""
    local_types = frozenset(m.group(1) for m in _JAVA_LOCAL_TYPE.finditer(source))
    traced: list[JavaTracedMethod] = []
    for match in _JAVA_TRACED_HEADER.finditer(source):
        method = match.group(3)
        if method in _JAVA_NOT_METHODS:
            continue
        class_name = _java_enclosing_class(source[: match.start()])
        member = tc.JAVA_CTOR if method == class_name else method
        params = _java_tracer_params(match.group(4) or "", package, local_types)
        owner = f"{package}{tc.DOT}{class_name}" if package else class_name
        line = source.count("\n", 0, match.start(3)) + 1
        traced.append(
            JavaTracedMethod(
                line=line,
                method=method,
                qualified=f"{owner}{tc.JAVA_MEMBER_SEP}{member}"
                + tc.JAVA_PARAMS.format(params=params),
            )
        )
    return traced


class NameTranslator:
    """Renames the cgr definitions of one program into TraceEval's scheme."""

    def __init__(
        self,
        language: tc.Language,
        definitions: Mapping[str, CgrDefinition],
        end_lines: Mapping[str, int],
        read_source: Callable[[str], str],
    ) -> None:
        self._language = language
        self._definitions = definitions
        self._end_lines = end_lines
        self._read_source = read_source
        self._module_qns = {
            d.path: d.qualified_name
            for d in definitions.values()
            if d.label == cs.NodeLabel.MODULE.value
        }
        self._java_names: dict[str, list[JavaTracedMethod]] = {}
        self._cache: dict[str, Translation] = {}

    def translate(self, qualified_name: str) -> Translation:
        if (cached := self._cache.get(qualified_name)) is not None:
            return cached
        translation = self._translate(qualified_name)
        self._cache[qualified_name] = translation
        return translation

    def _translate(self, qualified_name: str) -> Translation:
        definition = self._definitions.get(qualified_name)
        if definition is None:
            return Translation(None, None)
        module_qn = self._module_qns.get(definition.path)
        if module_qn is None:
            return Translation(None, None)
        if definition.label == cs.NodeLabel.MODULE.value:
            name = self._module_caller(definition)
            return Translation(name, name)
        prefix = module_qn + tc.DOT
        if not qualified_name.startswith(prefix):
            return Translation(None, None)
        local = qualified_name[len(prefix) :]
        if self._language == tc.Language.JAVA:
            name = self._java_name(definition, local)
            return Translation(name, name)
        return self._dotted_name(definition, local)

    def _module_caller(self, definition: CgrDefinition) -> str:
        match self._language:
            case tc.Language.JAVASCRIPT:
                return tc.JS_ROOT_CALLER
            case tc.Language.PYTHON:
                return _module_name(definition.path, self._language)
            case tc.Language.JAVA:
                # No Java frame is a module: field initialisers run inside a
                # constructor. A distinct name keeps such cgr edges scored
                # (as false positives) instead of silently dropped.
                return tc.JAVA_MODULE_CALLER.format(path=definition.path)

    def _dotted_name(self, definition: CgrDefinition, local: str) -> Translation:
        module = _module_name(definition.path, self._language)
        # `name@line` is cgr's spelling for a second definition of one name;
        # the tracer has no such suffix, it just reuses the name.
        segments = [_DUPLICATE_SUFFIX.sub("", s) for s in local.split(tc.DOT)]
        named: list[str] = []
        for segment in segments:
            if _is_anonymous(segment):
                if not named:
                    return Translation(None, self._module_caller(definition))
                return Translation(None, f"{module}{tc.DOT}{tc.DOT.join(named)}")
            named.append(segment)
        if (marker := self._anonymous_binding(definition, named[-1])) is not None:
            name = f"{module}{tc.DOT}{marker}"
            return Translation(name, name)
        if self._language == tc.Language.JAVASCRIPT and (
            owners := self._js_object_owners(definition, named[-1])
        ):
            named = [*named[:-1], *owners, named[-1]]
        name = f"{module}{tc.DOT}{tc.DOT.join(named)}"
        return Translation(name, name)

    def _definition_line(self, definition: CgrDefinition) -> tuple[str, int] | None:
        if definition.start_line is None:
            return None
        source = self._read_source(definition.path)
        lines = source.splitlines(keepends=True)
        index = definition.start_line - 1
        if not 0 <= index < len(lines):
            return None
        return source, sum(len(line) for line in lines[:index])

    def _anonymous_binding(self, definition: CgrDefinition, name: str) -> str | None:
        # `const f = () => ...` and `f = lambda: ...` bind a name to an
        # anonymous function. cgr names the node after the binding; the
        # tracers name it by ordinal (`<arrow3>`, `<lambda1>`). The marker
        # keeps it out of reach of any truth name and lets the static
        # protocol see through it exactly as it sees through the tracer's.
        located = self._definition_line(definition)
        if located is None:
            return None
        source, offset = located
        end = source.find("\n", offset)
        line = source[offset:] if end == -1 else source[offset:end]
        escaped = re.escape(name)
        match self._language:
            case tc.Language.JAVASCRIPT:
                if re.search(tc.JS_ARROW_BINDING_PATTERN.format(name=escaped), line):
                    return tc.ARROW_MARKER.format(name=name)
            case tc.Language.PYTHON:
                if re.search(tc.PY_LAMBDA_BINDING_PATTERN.format(name=escaped), line):
                    return tc.LAMBDA_MARKER.format(name=name)
            case tc.Language.JAVA:
                return None
        return None

    def _js_object_owners(self, definition: CgrDefinition, name: str) -> list[str]:
        # cgr names a function stored in an object literal after the property
        # alone (`makeDropdown`); the JS tracer wraps the literal's binding and
        # names it through it (`editor.makeDropdown`, nested `a.b.f`).
        located = self._definition_line(definition)
        if located is None:
            return []
        source, offset = located
        property_match = re.compile(tc.JS_PROPERTY_PATTERN.format(name=re.escape(name)))
        found = property_match.search(source, offset)
        if found is None or source.count("\n", offset, found.start()) > 0:
            return []
        owners: list[str] = []
        position = found.start()
        while (brace := _enclosing_brace(source, position)) is not None:
            before = source[:brace].rstrip()
            if binding := _JS_OBJECT_BINDING.search(before):
                return [binding.group(1), *owners]
            if nested := _JS_NESTED_PROPERTY.search(before):
                owners.insert(0, nested.group(1))
                position = nested.start()
                continue
            return []
        return []

    def _java_name(self, definition: CgrDefinition, local: str) -> str | None:
        signature = _JAVA_SIGNATURE.match(local)
        if signature is None:
            return None
        head = signature.group("name")
        method = head.rsplit(tc.DOT, 1)[-1]
        if any(_is_anonymous(segment) for segment in head.split(tc.DOT)):
            return None
        traced = self._java_traced(definition.path)
        start = definition.start_line or 0
        end = self._end_lines.get(definition.qualified_name, start)
        for candidate in traced:
            if candidate.method == method and start <= candidate.line <= end:
                return candidate.qualified
        return None

    def _java_traced(self, path: str) -> list[JavaTracedMethod]:
        if (cached := self._java_names.get(path)) is None:
            cached = java_tracer_names(self._read_source(path))
            self._java_names[path] = cached
        return cached
