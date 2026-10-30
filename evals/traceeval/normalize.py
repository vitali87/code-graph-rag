"""TraceEval's edge normalization, applied symmetrically to truth and to cgr.

A port of the benchmark's own `scripts/normalize_edges.py` (Apache-2.0), kept
behaviourally identical so a precision/recall row here is computed the way
the paper's Table 3 is. Rewrites run before drops, both on each endpoint:

- `init` collapses a constructor endpoint to its class (`main.Foo.__init__`,
  `main.Foo.constructor`, `pkg.Foo:<init>(int)` all become the bare class);
- `nested_class` keeps only the innermost class of a Java type path, which is
  what the source-level Java tracer records;
- `stdlib` drops an edge whose endpoint is a standard-library symbol.
"""

from __future__ import annotations

import builtins
import re
import sys
from collections.abc import Iterable

from . import constants as tc

_PY_STDLIB_MODULES: frozenset[str] = frozenset(sys.stdlib_module_names)
_PY_BUILTINS: frozenset[str] = frozenset(dir(builtins))
_PY_TYPE_METHODS: dict[str, frozenset[str]] = {
    t.__name__: frozenset(dir(t))
    for t in (
        list,
        dict,
        set,
        tuple,
        str,
        bytes,
        bytearray,
        frozenset,
        int,
        float,
        complex,
        bool,
        type,
        object,
        memoryview,
        range,
        slice,
    )
}
_JAVA_GENERIC = re.compile(tc.JAVA_GENERIC_PATTERN)
_JAVA_STDLIB_HEAD = re.compile(tc.JAVA_STDLIB_HEAD_PATTERN)
_JAVA_TYPE_PATH_SPLIT = re.compile(r"[.$]")


def _strip_call_args(name: str) -> str:
    return name.split(tc.PAREN_OPEN, 1)[0].strip()


def is_python_stdlib(name: str) -> bool:
    if not (name := _strip_call_args(name)):
        return False
    parts = name.split(tc.DOT)
    head = parts[0]
    if head == tc.PY_BUILTINS_HEAD or head in _PY_STDLIB_MODULES:
        return True
    if len(parts) >= 2 and parts[1] in _PY_TYPE_METHODS.get(head, frozenset()):
        return True
    return name in _PY_BUILTINS


def is_js_stdlib(name: str) -> bool:
    if not (name := _strip_call_args(name)):
        return False
    parts = name.split(tc.DOT)
    head = parts[0]
    if head in tc.JS_BUILTIN_GLOBALS or tc.JS_PROTOTYPE in parts:
        return True
    if name in tc.JS_GLOBAL_FUNCTIONS:
        return True
    if head in tc.NODE_BUILTIN_MODULES or head.startswith(tc.JS_NODE_SCHEME):
        return True
    # A user-module prefix in front of a builtin (`main.console.log`,
    # `main.fs.readFile`, `main.Date`): the benchmark drops these too, even
    # when `fs` is a user-defined stub object, so dropping here keeps both
    # sides of the comparison aligned.
    if len(parts) >= 2:
        return (
            parts[-2] in tc.JS_BUILTIN_GLOBALS
            or parts[-2] in tc.NODE_BUILTIN_MODULES
            or parts[-1] in tc.JS_BUILTIN_GLOBALS
            or parts[-1] in tc.JS_GLOBAL_FUNCTIONS
        )
    return False


def is_java_stdlib(name: str) -> bool:
    if not name:
        return False
    cleaned = name
    while (stripped := _JAVA_GENERIC.sub("", cleaned)) != cleaned:
        cleaned = stripped
    head = _JAVA_STDLIB_HEAD.split(cleaned, 1)[0].rstrip(tc.DOT).strip()
    if not head:
        return False
    if head.startswith(tc.JAVA_STDLIB_PACKAGE_PREFIXES):
        return True
    return tc.DOT not in head and head in tc.JAVA_BARE_STDLIB_CLASSES


def is_stdlib(name: str, language: tc.Language) -> bool:
    match language:
        case tc.Language.PYTHON:
            return is_python_stdlib(name)
        case tc.Language.JAVASCRIPT:
            return is_js_stdlib(name)
        case tc.Language.JAVA:
            return is_java_stdlib(name)


def canonicalize_init(name: str, language: tc.Language) -> str:
    match language:
        case tc.Language.PYTHON:
            for suffix in (tc.PY_CTOR, tc.PY_NEW):
                if name.endswith(tc.DOT + suffix):
                    return name[: -len(suffix) - 1]
            return name
        case tc.Language.JAVASCRIPT:
            suffix = tc.DOT + tc.JS_CTOR
            return name[: -len(suffix)] if name.endswith(suffix) else name
        case tc.Language.JAVA:
            for marker in (tc.JAVA_CTOR, tc.JAVA_CLINIT):
                if (idx := name.find(tc.JAVA_MEMBER_SEP + marker)) != -1:
                    return name[:idx]
            return name


def canonicalize_nested_class(name: str, language: tc.Language) -> str:
    if language != tc.Language.JAVA or tc.JAVA_MEMBER_SEP not in name:
        return name
    type_path, rest = name.split(tc.JAVA_MEMBER_SEP, 1)
    segments = _JAVA_TYPE_PATH_SPLIT.split(type_path)
    if len(segments) < 2:
        return name
    # Packages are lower-case and classes capitalised by convention, so the
    # trailing run of capitalised segments is the class nesting.
    start = len(segments) - 1
    while start > 0 and segments[start - 1] and segments[start - 1][0].isupper():
        start -= 1
    if len(segments) - start <= 1:
        return name
    package = tc.DOT.join(segments[:start])
    inner = segments[-1]
    path = f"{package}{tc.DOT}{inner}" if package else inner
    return f"{path}{tc.JAVA_MEMBER_SEP}{rest}"


def normalize_name(
    name: str, language: tc.Language, modes: Iterable[tc.NormMode]
) -> str | None:
    """`name` rewritten by every active transform, or None when it is dropped."""
    active = frozenset(modes)
    if tc.NormMode.INIT in active:
        name = canonicalize_init(name, language)
    if tc.NormMode.NESTED_CLASS in active:
        name = canonicalize_nested_class(name, language)
    if tc.NormMode.STDLIB in active and is_stdlib(name, language):
        return None
    return name


def normalize_edges(
    edges: Iterable[tuple[str, str]],
    language: tc.Language,
    modes: Iterable[tc.NormMode] = tc.ALL_NORM_MODES,
) -> set[tuple[str, str]]:
    active = tuple(modes)
    out: set[tuple[str, str]] = set()
    for caller, callee in edges:
        new_caller = normalize_name(caller, language, active)
        new_callee = normalize_name(callee, language, active)
        if new_caller is not None and new_callee is not None:
            out.add((new_caller, new_callee))
    return out
