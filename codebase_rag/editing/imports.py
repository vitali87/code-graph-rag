"""Import statement rewriting for rename and move (issue #1530).

`rename` and `move` must update the statements that import a symbol, not
only the sites that use it. The `IMPORTS` edges know every statement
(issue #1522: file, span, bound alias, imported symbol), so a rewrite is a
span replacement per statement, applied through the span-preserving
patcher (issue #1529) so nothing around the statement changes.

Each language handler takes the statement text at the site and returns its
replacement:

- retarget a symbol to a new module (the move), keeping the alias and,
  when the statement imported several names, splitting off the moved one;
- rename the imported symbol, keeping the alias so use sites stay valid;
- retarget a whole-module import when the module itself moved.

Re-export chains: a barrel (`export { x } from './a'`, `from a import x` in
an `__init__`) is an importer like any other and is retargeted at the leaf;
its own re-export of the name is left intact, so files importing through the
barrel keep working. `__all__` entries are string literals and are rewritten
on a rename through the same span mechanism.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path
from typing import NamedTuple

from .. import constants as cs
from ..language_spec import get_language_for_extension
from .patcher import Patcher


class ImportSite(NamedTuple):
    """One IMPORTS edge: where a statement binds a name from a module."""

    path: str
    line: int
    col: int
    end_line: int
    end_col: int
    alias: str | None
    imported_name: str | None


class SymbolMove(NamedTuple):
    """A symbol leaving `old_module` for `new_module`, optionally renamed.

    Module names are language-native import paths (`pkg.util`, `./util`,
    `crate::util`, `a.b`, `mod/pkg`), the form the statements spell them in;
    `new_module_path` is the moved file's repo-relative path, needed for the
    relative specifiers JS/TS write.
    """

    symbol: str
    old_module: str
    new_module: str
    new_name: str | None = None
    new_module_path: str | None = None


class RewriteError(ValueError):
    """The statement at a site could not be rewritten as asked."""


class Rewrite(NamedTuple):
    path: str
    line: int
    col: int
    before: str
    after: str


# --- helpers ------------------------------------------------------------------


def _span_bytes(source: bytes, site: ImportSite) -> tuple[int, int]:
    from .patcher import line_col_to_byte

    start = line_col_to_byte(source, site.line, site.col)
    end = line_col_to_byte(source, site.end_line, site.end_col)
    return start, end


def _split_on_as(entry: str) -> list[str]:
    # `a as b as c` -> ["a", "b", "c"] without a regex: scanning a
    # whitespace-delimited pattern over a long run is super-linear (S8786),
    # a single whitespace tokenisation is not.
    tokens = entry.split()
    parts: list[str] = []
    current: list[str] = []
    for index, token in enumerate(tokens):
        if token == "as" and current and index < len(tokens) - 1:
            parts.append(" ".join(current))
            current = []
        else:
            current.append(token)
    parts.append(" ".join(current))
    return parts


def _local_name(entry: str) -> str:
    # `a as b` -> b, `a` -> a (Python/TS/Rust forms alike).
    return _split_on_as(entry)[-1]


def _imported(entry: str) -> str:
    return _split_on_as(entry)[0]


def _rewrite_entry(entry: str, new_name: str | None, keep_local: bool) -> str:
    """`a as b` with the symbol renamed to `new_name`, keeping the local name.

    A bare `a` renamed to `c` binds `c` unless `keep_local` asks to keep the
    old local name through an alias (`c as a`).
    """
    imported = _imported(entry)
    local = _local_name(entry)
    target = new_name or imported
    if local != imported or (keep_local and target != local):
        return f"{target} as {local}"
    return target


def _relative_specifier(importer_path: str, target_path: str) -> str:
    importer_dir = Path(importer_path).parent
    target = Path(target_path)
    rel = Path(*[".."] * 0)
    try:
        rel = target.relative_to(importer_dir)
        spec = f"./{rel.as_posix()}"
    except ValueError:
        ups = []
        current = importer_dir
        while True:
            try:
                rel = target.relative_to(current)
                break
            except ValueError:
                ups.append("..")
                if current == Path():
                    raise
                current = current.parent
        spec = "/".join([*ups, rel.as_posix()])
    spec = re.sub(r"\.(ts|tsx|js|jsx|mjs|cjs)$", "", spec)
    return spec[:-6] if spec.endswith("/index") else spec


# --- Python -------------------------------------------------------------------

# `from module import names` is matched in three anchored steps: each
# sub-pattern is unambiguous at its own start position, so matching stays
# linear where one statement-wide regex is super-linear (S8786), and the
# names run to the end of the statement by construction.
_PY_FROM_LEAD = re.compile(r"\s*from\s+")
_PY_FROM_MODULE = re.compile(r"[\w.]+")
_PY_FROM_MID = re.compile(r"\s+import\s+")


def _match_py_from(statement: str) -> tuple[str, str, str, str] | None:
    lead = _PY_FROM_LEAD.match(statement)
    if lead is None:
        return None
    module = _PY_FROM_MODULE.match(statement, lead.end())
    if module is None:
        return None
    mid = _PY_FROM_MID.match(statement, module.end())
    if mid is None:
        return None
    names = statement[mid.end() :]
    if not names:
        return None
    return lead.group(0), module.group(0), mid.group(0), names


_PY_IMPORT = re.compile(
    r"^(?P<lead>\s*import\s+)(?P<module>[\w.]+)(?P<rest>(?:\s+as\s+\w+)?\s*)$", re.S
)


def _split_names(names: str) -> tuple[list[str], str, str]:
    # Returns the entries, the opening decoration (`(` + whitespace) and the
    # closing one, so a parenthesised list keeps its shape.
    stripped = names.strip()
    open_deco = close_deco = ""
    if stripped.startswith("("):
        inner = stripped[1:-1]
        open_deco = "(" + inner[: len(inner) - len(inner.lstrip())]
        close_deco = inner[len(inner.rstrip()) :] + ")"
        stripped = inner.strip().rstrip(",")
    entries = [e.strip() for e in stripped.split(",") if e.strip()]
    return entries, open_deco, close_deco


def _py_rewrite(statement: str, move: SymbolMove) -> str | None:
    if parsed := _match_py_from(statement):
        lead, module, mid, raw_names = parsed
        if module != move.old_module:
            return None
        names = raw_names.rstrip()
        tail = raw_names[len(names) :]
        entries, open_deco, close_deco = _split_names(names)
        moved = [e for e in entries if _imported(e) == move.symbol]
        if not moved:
            return None
        kept = [e for e in entries if _imported(e) != move.symbol]
        # Every entry binding the symbol moves: `helper, helper as h` binds it
        # twice, and keeping only the first would drop the `h` binding.
        moved_entries = [
            _rewrite_entry(entry, move.new_name, keep_local=True) for entry in moved
        ]
        moved_stmt = f"{lead}{move.new_module}{mid}{', '.join(moved_entries)}"
        if not kept:
            return f"{moved_stmt}{tail}"
        kept_stmt = f"{lead}{module}{mid}{open_deco}{', '.join(kept)}{close_deco}"
        indent = re.match(r"\s*", statement.splitlines()[0]).group(0)  # type: ignore[union-attr]
        return f"{kept_stmt}\n{indent}{moved_stmt.lstrip()}{tail}"
    if (
        (m := _PY_IMPORT.match(statement))
        and m.group("module") == move.old_module
        and move.symbol == move.old_module
    ):
        return f"{m.group('lead')}{move.new_module}{m.group('rest')}"
    return None


# --- JavaScript / TypeScript ---------------------------------------------------

_JS_SPEC = re.compile(r"""(?P<q>['"])(?P<spec>[^'"]+)(?P=q)""")
_JS_NAMED = re.compile(r"\{(?P<names>[^}]*)\}")
# The leading keyword and any type-only modifier ("import ", "export type "),
# so a default clause after it can be separated from the moved statement.
# `type` must be absorbed here: left as residue it reads as a default binding,
# and `import type from './util'` is VALID TypeScript (a default import named
# `type`), so the patcher's parse gate would pass corrupt output through.
_JS_KEYWORD = re.compile(r"\s*(?:import|export)\s+(?:type\s+)?")


def _js_rewrite(statement: str, move: SymbolMove, importer_path: str) -> str | None:
    spec_match = _JS_SPEC.search(statement)
    if spec_match is None or spec_match.group("spec") != move.old_module:
        return None
    if move.new_module_path is not None:
        new_spec = _relative_specifier(importer_path, move.new_module_path)
    else:
        new_spec = move.new_module
    named = _JS_NAMED.search(statement)
    if named is None:
        # Namespace, default or require of the whole module: retarget only
        # when the module itself moved.
        if move.symbol == move.old_module:
            return (
                statement[: spec_match.start("spec")]
                + new_spec
                + statement[spec_match.end("spec") :]
            )
        return None
    entries = [e.strip() for e in named.group("names").split(",") if e.strip()]
    moved = [e for e in entries if _imported(e) == move.symbol]
    if not moved:
        return None
    kept = [e for e in entries if _imported(e) != move.symbol]
    moved_entries = ", ".join(
        _rewrite_entry(entry, move.new_name, keep_local=True) for entry in moved
    )
    head = statement[: named.start()]
    between = statement[named.end() : spec_match.start("spec")]
    tail = statement[spec_match.end("spec") :]
    # `head` carries any default/namespace clause ("import def, "), which binds
    # a name from the ORIGINAL module: it must stay there, never be redeclared
    # in the moved statement nor follow the symbol to the new module.
    keyword = _JS_KEYWORD.match(head)
    moved_head = keyword.group(0) if keyword else head
    moved_stmt = f"{moved_head}{{ {moved_entries} }}{between}{new_spec}{tail}"
    indent = re.match(r"\s*", statement).group(0)  # type: ignore[union-attr]
    if not kept:
        default_clause = head[len(moved_head) :].rstrip().rstrip(",").rstrip()
        if not default_clause:
            return moved_stmt
        # The named list emptied but a default binding remains behind.
        kept_stmt = (
            f"{head[: len(moved_head)]}{default_clause}{between}{move.old_module}{tail}"
        )
        return f"{kept_stmt}\n{indent}{moved_stmt.lstrip()}"
    kept_stmt = f"{head}{{ {', '.join(kept)} }}{between}{move.old_module}{tail}"
    return f"{kept_stmt}\n{indent}{moved_stmt.lstrip()}"


# --- Java / Go / Rust ----------------------------------------------------------

_JAVA_IMPORT = re.compile(
    r"^(?P<lead>\s*import\s+(?:static\s+)?)(?P<path>[\w.]+)(?P<tail>\s*;\s*)$", re.S
)


def _java_rewrite(statement: str, move: SymbolMove) -> str | None:
    m = _JAVA_IMPORT.match(statement)
    if m is None:
        return None
    path = m.group("path")
    if path != f"{move.old_module}.{move.symbol}":
        return None
    return f"{m.group('lead')}{move.new_module}.{move.new_name or move.symbol}{m.group('tail')}"


def _go_rewrite(statement: str, move: SymbolMove) -> str | None:
    m = _JS_SPEC.search(statement)
    if (
        m is None
        or m.group("spec") != move.old_module
        or move.symbol != move.old_module
    ):
        return None
    return statement[: m.start("spec")] + move.new_module + statement[m.end("spec") :]


# The statement is taken apart in two steps (the `use` head, then the path
# with its optional group and alias) rather than by one regex whose
# alternation count trips the complexity bar.
_RS_USE_HEAD = re.compile(r"^(?P<lead>\s*(?:pub(?:\([^)]*\))?\s+)?use\s+)")
_RS_USE_BODY = re.compile(
    r"^(?P<path>[\w:]+)(?P<group>::\{(?P<names>[^}]*)\})?(?P<alias>\s+as\s+\w+)?$"
)


def _split_rs_use(statement: str) -> tuple[str, re.Match[str], str] | None:
    """`(lead, body match, tail)` of a `use` statement, None if it is not one."""
    head = _RS_USE_HEAD.match(statement)
    if head is None:
        return None
    rest = statement[head.end() :]
    semicolon = rest.rfind(";")
    if semicolon < 0 or rest[semicolon + 1 :].strip():
        return None
    body = _RS_USE_BODY.match(rest[:semicolon].rstrip())
    if body is None:
        return None
    return head.group("lead"), body, rest[semicolon:]


def _rs_rewrite(statement: str, move: SymbolMove) -> str | None:
    parts = _split_rs_use(statement)
    if parts is None:
        return None
    lead, m, tail = parts
    path = m.group("path")
    if m.group("group") is None:
        if path != f"{move.old_module}::{move.symbol}":
            return None
        alias = m.group("alias") or ""
        new_name = move.new_name or move.symbol
        if not alias and move.new_name:
            alias = f" as {move.symbol}"
        return f"{lead}{move.new_module}::{new_name}{alias}{tail}"
    if path != move.old_module:
        return None
    entries = [e.strip() for e in m.group("names").split(",") if e.strip()]
    moved = [e for e in entries if _imported(e) == move.symbol]
    if not moved:
        return None
    kept = [e for e in entries if _imported(e) != move.symbol]
    moved_entries = [
        _rewrite_entry(entry, move.new_name, keep_local=True) for entry in moved
    ]
    moved_body = (
        moved_entries[0]
        if len(moved_entries) == 1
        else "{" + ", ".join(moved_entries) + "}"
    )
    moved_stmt = f"{lead}{move.new_module}::{moved_body}{tail}"
    if not kept:
        return moved_stmt
    kept_body = kept[0] if len(kept) == 1 else "{" + ", ".join(kept) + "}"
    kept_stmt = f"{lead}{path}::{kept_body}{tail}"
    indent = re.match(r"\s*", statement).group(0)  # type: ignore[union-attr]
    return f"{kept_stmt.rstrip()}\n{indent}{moved_stmt.lstrip()}"


# --- the rewriter -------------------------------------------------------------


class ImportRewriter:
    """Rewrite the import statements the graph knows for a symbol move."""

    def __init__(self, repo_root: Path, patcher: Patcher | None = None) -> None:
        self.repo_root = repo_root.resolve()
        self.patcher = patcher or Patcher(self.repo_root)
        self.rewrites: list[Rewrite] = []
        self.untouched: list[ImportSite] = []

    def _statement(self, site: ImportSite) -> tuple[bytes, int, int]:
        source = self.patcher.source(site.path)
        start, end = _span_bytes(source, site)
        return source, start, end

    def retarget(self, sites: Iterable[ImportSite], move: SymbolMove) -> list[Rewrite]:
        """Queue a replacement for every site that imports the moved symbol.

        Sites whose statement does not import the symbol from the old module
        (a same-named symbol from elsewhere, a wildcard) are left untouched
        and listed in `untouched`, so the caller can see what the rewrite
        deliberately did not do.
        """
        applied: list[Rewrite] = []
        seen: set[tuple[str, int, int]] = set()
        for site in sites:
            # `from a import b as c, d` yields one edge per bound name with
            # the same statement span; the statement is rewritten once.
            span_key = (site.path, site.line, site.col)
            if span_key in seen:
                continue
            seen.add(span_key)
            language = get_language_for_extension(Path(site.path).suffix)
            source, start, end = self._statement(site)
            statement = source[start:end].decode(cs.ENCODING_UTF8)
            replacement = self._rewrite_for(language, statement, move, site.path)
            if replacement is None or replacement == statement:
                self.untouched.append(site)
                continue
            self.patcher.replace_span(site.path, (start, end), replacement)
            rewrite = Rewrite(site.path, site.line, site.col, statement, replacement)
            applied.append(rewrite)
            self.rewrites.append(rewrite)
        return applied

    @staticmethod
    def _rewrite_for(
        language: cs.SupportedLanguage | None,
        statement: str,
        move: SymbolMove,
        importer_path: str,
    ) -> str | None:
        if language == cs.SupportedLanguage.PYTHON:
            return _py_rewrite(statement, move)
        if language in cs.JS_TS_LANGUAGES:
            return _js_rewrite(statement, move, importer_path)
        if language == cs.SupportedLanguage.JAVA:
            return _java_rewrite(statement, move)
        if language == cs.SupportedLanguage.GO:
            return _go_rewrite(statement, move)
        if language == cs.SupportedLanguage.RUST:
            return _rs_rewrite(statement, move)
        return None

    def rename_in_all(self, path: str, old_name: str, new_name: str) -> int:
        """Rewrite `"old_name"` entries of a Python `__all__` list in `path`."""
        source = self.patcher.source(path)
        text = source.decode(cs.ENCODING_UTF8)
        count = 0
        for m in re.finditer(
            r"__all__\s*(?::[^=]+)?=\s*[\[(]([^\])]*)[\])]", text, re.S
        ):
            for literal in re.finditer(
                r"""(['"])(?P<name>[A-Za-z_]\w*)\1""", m.group(1)
            ):
                if literal.group("name") != old_name:
                    continue
                start = m.start(1) + literal.start("name")
                self.patcher.replace_span(
                    path,
                    (
                        len(text[:start].encode(cs.ENCODING_UTF8)),
                        len(text[: start + len(old_name)].encode(cs.ENCODING_UTF8)),
                    ),
                    new_name,
                )
                count += 1
        return count
