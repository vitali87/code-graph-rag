"""Jedi in-process Python semantic frontend (issue #1183, stage 1).

Python gets the deepest FLOWS_TO walk in the repo and had no semantic layer:
re-export chains, decorated callables, and MRO dispatch were all approximated
by the parsers/py heuristics plus the shared name trie. Jedi is pure Python
and needs no external toolchain, making it the cheapest semantic win in the
roadmap. The frontend enumerates call sites with the stdlib ast (byte-exact
name-token positions matching the tree-sitter CallSiteKey convention), asks
jedi to infer each callee, and emits the two standard fact families:

- resolved_call_sites: the definition a call binds to, following re-exports,
  decorators, and class hierarchies, keyed and targeted at NAME tokens.
- external_sites: callees resolving outside the repo (stdlib, site-packages,
  builtins) — the compiler-grade proof the trie fallback must not fabricate a
  first-party CALLS edge.

Cost control: only attribute calls and import-bound bare calls are queried
(module-local bare calls are the heuristics' home turf), one jedi Project is
shared across files, and a per-file time budget degrades that file to the
heuristics rather than stalling the index. Ambiguity is a ceiling: multiple
or empty inferences emit no fact, never a guess.
"""

from __future__ import annotations

import ast
import time
from pathlib import Path

from loguru import logger

from ... import constants as cs
from ... import logs as ls
from ...config import settings
from ..frontends.protocol import CallSiteKey, ResolvedCallSite, SemanticFacts

# Generous enough for jedi's cold-start typeshed parse (the dominant cost,
# amortized by its on-disk cache after the first file); a stuck module still
# degrades to heuristics instead of stalling the index.
_FILE_BUDGET_SECONDS = 10.0
_RESOLVABLE_TYPES = frozenset({"function", "class"})


def python_frontend_available() -> bool:
    try:
        import jedi  # noqa: F401
    except ImportError:
        return False
    return True


def resolve_python_frontend() -> cs.PythonFrontend:
    # The parser fingerprint records the RESOLVED mode: a graph built with
    # jedi facts and one without must never share an identity.
    mode = settings.PYTHON_FRONTEND
    if mode == cs.PythonFrontend.HEURISTIC:
        return mode
    if not python_frontend_available():
        return cs.PythonFrontend.HEURISTIC
    return cs.PythonFrontend.JEDI


class _CallSite(ast.NodeVisitor):
    """Collects (name, name_line, name_byte_col) for the call sites worth a
    jedi query: attribute calls and bare calls bound by an import."""

    def __init__(self) -> None:
        self.imported_names: set[str] = set()
        self.sites: list[tuple[str, int, int]] = []

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            head = (alias.asname or alias.name).partition(".")[0]
            self.imported_names.add(head)
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for alias in node.names:
            self.imported_names.add(alias.asname or alias.name)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if isinstance(func, ast.Attribute):
            # The attribute NAME token starts len(attr)-bytes before the
            # expression's end; ast offsets are UTF-8 byte offsets, matching
            # the tree-sitter key convention.
            if func.end_lineno is not None and func.end_col_offset is not None:
                col = func.end_col_offset - len(func.attr.encode(cs.ENCODING_UTF8))
                self.sites.append((func.attr, func.end_lineno, col))
        elif isinstance(func, ast.Name) and func.id in self.imported_names:
            self.sites.append((func.id, func.lineno, func.col_offset))
        self.generic_visit(node)


def _byte_to_char_col(line_text: str, byte_col: int) -> int:
    return len(
        line_text.encode(cs.ENCODING_UTF8)[:byte_col].decode(
            cs.ENCODING_UTF8, errors="replace"
        )
    )


def _char_to_byte_col(line_text: str, char_col: int) -> int:
    return len(line_text[:char_col].encode(cs.ENCODING_UTF8))


def _single_resolvable_target(names):
    # Exactly one inferred function/class is a usable fact; anything else
    # (ambiguity, modules, instances) is the ceiling: no fact, never a guess.
    if len(names) != 1:
        return None
    name = names[0]
    if name.type not in _RESOLVABLE_TYPES:
        return None
    return name


def _record_site(
    script,
    source_lines: list[str],
    rel_file: str,
    site: tuple[str, int, int],
    repo_path: Path,
    facts: SemanticFacts,
) -> None:
    import jedi

    simple_name, line, byte_col = site
    if line - 1 >= len(source_lines):
        return
    char_col = _byte_to_char_col(source_lines[line - 1], byte_col)
    try:
        names = script.infer(line, char_col)
    except (jedi.InternalError, RecursionError, ValueError, OSError, EOFError):
        # EOFError/OSError surface from jedi's on-disk parser cache when
        # concurrent processes race it; a lost site degrades to heuristics.
        return
    target = _single_resolvable_target(names)
    if target is None:
        return
    key: CallSiteKey = (rel_file, line, byte_col, simple_name)
    module_path = target.module_path
    if module_path is None or not Path(module_path).is_relative_to(repo_path):
        facts.external_sites.add(key)
        return
    target_rel = Path(module_path).relative_to(repo_path).as_posix()
    try:
        target_lines = (
            Path(module_path).read_text(encoding=cs.ENCODING_UTF8).splitlines()
        )
    except (OSError, UnicodeDecodeError):
        return
    if target.line is None or target.line - 1 >= len(target_lines):
        return
    # The Pass-2 span index keys Python definitions at the def/class
    # KEYWORD (the line's first code byte), not the name token jedi
    # reports; the indent is ASCII so its byte and char widths agree.
    def_line = target_lines[target.line - 1]
    target_byte_col = len(def_line) - len(def_line.lstrip())
    facts.resolved_call_sites[key] = ResolvedCallSite(
        simple_name, target_rel, target.line, target_byte_col
    )


def _resolve_file(
    script,
    source_lines: list[str],
    rel_file: str,
    sites: list[tuple[str, int, int]],
    repo_path: Path,
    facts: SemanticFacts,
    deadline: float,
) -> bool:
    # File-local collection makes degradation ATOMIC: a file that blows its
    # budget contributes nothing, instead of a half-resolved prefix. jedi has
    # no cancellation API, so one slow infer() can overshoot the deadline;
    # the post-call check then discards the whole file's facts, bounding the
    # damage to a single call's wall time.
    file_facts = SemanticFacts()
    for site in sites:
        if time.monotonic() > deadline:
            return False
        _record_site(script, source_lines, rel_file, site, repo_path, file_facts)
    if time.monotonic() > deadline:
        return False
    facts.resolved_call_sites.update(file_facts.resolved_call_sites)
    facts.external_sites.update(file_facts.external_sites)
    return True


def run_python_frontend(repo_path: Path, files: list[Path]) -> SemanticFacts:
    facts = SemanticFacts()
    if not files:
        return facts
    import jedi

    repo_path = repo_path.resolve()
    project = jedi.Project(str(repo_path))
    degraded = 0
    for file_path in files:
        try:
            source = file_path.read_text(encoding=cs.ENCODING_UTF8)
            tree = ast.parse(source)
        except (OSError, SyntaxError, ValueError):
            continue
        collector = _CallSite()
        collector.visit(tree)
        if not collector.sites:
            continue
        try:
            rel_file = file_path.resolve().relative_to(repo_path).as_posix()
        except ValueError:
            continue
        script = jedi.Script(path=str(file_path), project=project)
        deadline = time.monotonic() + _FILE_BUDGET_SECONDS
        if not _resolve_file(
            script,
            source.splitlines(),
            rel_file,
            collector.sites,
            repo_path,
            facts,
            deadline,
        ):
            degraded += 1
    if degraded:
        logger.info(ls.PY_FRONTEND_BUDGET_DEGRADED, count=degraded)
    return facts
