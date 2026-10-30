"""The `static` protocol: TraceEval minus what no static call graph can name.

The paper protocol (normalize.py) scores names as the tracers spelled them.
Two kinds of tracer output are not call edges a static graph could ever
emit under those names, so this protocol removes them, on both sides, before
the paper's own normalization runs:

- Class bodies (Python). `sys.settrace` reports a class body's execution as
  a call, and the tracer's filter only catches top-level capitalised classes,
  so `main -> main.admin` (a lower-case stub class) and `main.Model ->
  main.Model.Meta` survive as "calls". A class is not a callee; a call made
  *in* a class body runs as part of the scope that defines the class, which
  is also where cgr attributes it, so such a caller is credited there.
- Anonymous functions. The tracers name lambdas, generator expressions and
  wrapped arrows by ordinal (`<lambda1>`, `<genexpr>`, `<arrow3>`), which no
  source analysis can reproduce. Such a node is seen through: `P -> anon ->
  B` becomes `P -> B`, the edge a reader of either graph would want.
"""

from __future__ import annotations

import ast
from collections import defaultdict
from collections.abc import Iterable
from pathlib import Path

from codebase_rag import constants as cs

from . import constants as tc
from .types_defs import TraceEdge


def is_anonymous(name: str) -> bool:
    return any(
        segment.startswith(tc.ANON_OPEN)
        and segment.endswith(tc.ANON_CLOSE)
        and segment not in tc.NAMED_ANGLE_SEGMENTS
        for segment in name.split(tc.DOT)
    )


def see_through_anonymous(edges: Iterable[TraceEdge]) -> set[TraceEdge]:
    edge_list = list(edges)
    successors: dict[str, set[str]] = defaultdict(set)
    for caller, callee in edge_list:
        if is_anonymous(caller):
            successors[caller].add(callee)

    def named_targets(node: str) -> set[str]:
        found: set[str] = set()
        stack, seen = [node], {node}
        while stack:
            for nxt in successors.get(stack.pop(), ()):
                if not is_anonymous(nxt):
                    found.add(nxt)
                elif nxt not in seen:
                    seen.add(nxt)
                    stack.append(nxt)
        return found

    out: set[TraceEdge] = set()
    for caller, callee in edge_list:
        if is_anonymous(caller):
            continue
        if not is_anonymous(callee):
            out.add((caller, callee))
            continue
        # A self-edge here is an artefact of collapsing (f -> <lambda> -> f),
        # and neither tracer emits self-edges for Python or JavaScript.
        out.update((caller, t) for t in named_targets(callee) if t != caller)
    return out


def _collect_classes(body: list[ast.stmt], prefix: str, out: set[str]) -> None:
    for node in body:
        match node:
            case ast.ClassDef(name=name, body=inner):
                qualified = f"{prefix}{tc.DOT}{name}"
                out.add(qualified)
                _collect_classes(inner, qualified, out)
            case (
                ast.FunctionDef(name=name, body=inner)
                | ast.AsyncFunctionDef(name=name, body=inner)
            ):
                _collect_classes(inner, f"{prefix}{tc.DOT}{name}", out)
            case _:
                for child in ast.iter_child_nodes(node):
                    if isinstance(child, ast.stmt):
                        _collect_classes([child], prefix, out)


def python_class_names(root: Path, is_source_file: Iterable[Path]) -> set[str]:
    """Every class in the program, named the way the Python tracer names it."""
    classes: set[str] = set()
    for path in is_source_file:
        if path.suffix != cs.EXT_PY:
            continue
        module = path.relative_to(root).with_suffix("").as_posix()
        try:
            tree = ast.parse(path.read_text(encoding=cs.ENCODING_UTF8))
        except (SyntaxError, UnicodeDecodeError, ValueError):
            continue
        _collect_classes(tree.body, module.replace(tc.PATH_SEP, tc.DOT), classes)
    return classes


def drop_class_bodies(edges: Iterable[TraceEdge], classes: set[str]) -> set[TraceEdge]:
    out: set[TraceEdge] = set()
    for caller, callee in edges:
        if callee in classes:
            continue
        while caller in classes and tc.DOT in caller:
            caller = caller.rsplit(tc.DOT, 1)[0]
        if caller != callee:
            out.add((caller, callee))
    return out
