"""A Rust source file read for its crate structure without a parse.

The import pass needs a crate entry's bodyless `mod NAME;` declarations,
their `#[path = ...]` redirects and the items a file declares at its top
level, before tree-sitter has parsed it. This scanner strips comments and
string/char literals, keeps only top-level text (outside braces and macro
bodies), and matches those declarations there.
"""

from __future__ import annotations

import re
from typing import NamedTuple

# Bodyless `mod NAME;` declarations in a Rust crate entry file: the mod graph
# is what assigns a file to the lib or the bin crate (issue #1007). Attribute
# prefixes on the same line (`#[cfg(unix)] mod unix;`) are idiomatic; the
# source is lexed through strip_comments_and_strings first so neither a
# commented-out declaration nor one hidden behind a string literal containing
# a comment marker can flip the crate attribution.
MOD_DECL_PATTERN = re.compile(
    r"^[^\S\n]*(?:#\[[^\]\n]*\][^\S\n]*)*(?:pub\s*(?:\([^)]*\))?\s+)?"
    r"mod\s+(?:r#)?([A-Za-z_][A-Za-z0-9_]*)\s*;",
    re.MULTILINE,
)


# Top-level item declarations in an entry file: `crate::Item` attaches under
# the entry module's qn when the entry declares Item, and the declaring entry
# disambiguates the path when BOTH src/lib.rs and src/main.rs declare the
# importing module. `mod` here catches INLINE `mod x { ... }` blocks, which
# declare a name in the entry module but pull no file into the crate (the
# bodyless pattern above is what assigns files).
# A `#[path = "..."]` attribute among a bodyless declaration's attributes
# names the file backing it, which is where the qn scheme keys the module.
# Only the plain string form is read: a cfg_attr wrapper is conditional and
# no single target speaks for it (issue #1035).
MOD_REDIRECT_PATTERN = re.compile(
    r"^[^\S\n]*((?:#\[[^\]\n]*\][^\S\n]*)*)"
    r"(?:pub\s*(?:\([^)]*\))?\s+)?"
    r"mod\s+(?:r#)?([A-Za-z_][A-Za-z0-9_]*)\s*;",
    re.MULTILINE,
)


# One attribute-only line, for the linear back-walk that gathers the block
# ABOVE a declaration. Keeping every attribute group line-local is what makes
# a long unbroken attribute run scan linearly: with `\s*` crossing newlines,
# every line of the run restarted a match attempt that consumed the rest of
# the run before failing, which is quadratic in the run length (issue #1089).
_ATTRIBUTE_LINE_PATTERN = re.compile(r"^[^\S\n]*((?:#\[[^\]\n]*\][^\S\n]*)+)$")


PATH_ATTRIBUTE_PATTERN = re.compile(r'#\[\s*path\s*=\s*"([^"\n]+)"\s*\]')


# The opening of a path attribute, matched against the code the lexer has
# already emitted, so the literal that follows can be kept verbatim.
_PATH_ATTRIBUTE_OPEN = re.compile(r"#\[\s*path\s*=\s*$")


ITEM_DECL_PATTERN = re.compile(
    r"^[^\S\n]*(?:#\[[^\]\n]*\][^\S\n]*)*(?:pub\s*(?:\([^)]*\))?\s+)?"
    r'(?:(?:unsafe|async|const|extern\s+"[^"]*")\s+)*'
    r"(?:trait|struct|enum|fn|type|const|static|union|mod)"
    r"\s+(?:mut\s+)?(?:r#)?([A-Za-z_][A-Za-z0-9_]*)",
    re.MULTILINE,
)


# A depth-0 `{` opens a macro body rather than an item body when the text
# before it ends in a macro NAME followed by `!` (an invocation: `cfg_if! {`)
# or names a macro_rules definition. Macro bodies stay visible to the
# declaration scans: libc, backtrace and getrandom declare their platform
# `mod` files inside cfg_if!. A diverging `fn abort() -> ! {` also ends in
# `!`, but with no name touching it: that brace opens an item body.
_MACRO_OPEN_RE = re.compile(r"(?:\w!|\bmacro_rules!\s*(?:r#)?[A-Za-z_]\w*)\s*$")


def strip_comments_and_strings(source: str) -> str:
    """Blank Rust comments and string/char contents, keeping line structure.

    A regex cannot do this: block comments NEST, and string literals may
    contain comment markers (`const P: &str = "/*";`) or newline-separated
    text that would fake a `mod x;` line.

    The one literal kept is a `#[path = "..."]` value, which names the file
    backing a mod declaration and so has to survive for the declaration
    scan to read it (issue #1035). Keeping it is safe precisely because
    this lexer decides it: the opening `#[path =` is matched in code the
    lexer has already walked, so no comment or outer string can present
    one.
    """
    out: list[str] = []
    i, n = 0, len(source)
    while i < n:
        token_end = _scan_token(source, i, out)
        if token_end is None:
            out.append(source[i])
            i += 1
        else:
            i = token_end
    return "".join(out)


def _scan_token(source: str, i: int, out: list[str]) -> int | None:
    """Consume the comment or literal starting at `i`, appending what it
    leaves behind to `out` and returning the index just past it; None when
    `source[i]` opens neither and passes through as plain code."""
    n = len(source)
    c = source[i]
    nxt = source[i + 1] if i + 1 < n else ""
    if c == "/" and nxt == "/":
        j = source.find("\n", i)
        return n if j == -1 else j
    if c == "/" and nxt == "*":
        return _skip_block_comment(source, i + 2, out)
    if c == "r" and nxt in ('"', "#"):
        raw_end = _raw_string_end(source, i)
        if raw_end is not None:
            out.append('""')
            return raw_end
    if c == '"':
        return _scan_string(source, i, out)
    if c == "'":
        char_end = _char_literal_end(source, i)
        if char_end is not None:
            out.append("''")
            return char_end
    return None


def _skip_block_comment(source: str, i: int, out: list[str]) -> int:
    n = len(source)
    depth = 1
    while i < n and depth:
        if source.startswith("/*", i):
            depth += 1
            i += 2
        elif source.startswith("*/", i):
            depth -= 1
            i += 2
        else:
            if source[i] == "\n":
                out.append("\n")
            i += 1
    return i


def _raw_string_end(source: str, i: int) -> int | None:
    n = len(source)
    j = i + 1
    hashes = 0
    while j < n and source[j] == "#":
        hashes += 1
        j += 1
    if j < n and source[j] == '"':
        end_marker = '"' + "#" * hashes
        k = source.find(end_marker, j + 1)
        return n if k == -1 else k + len(end_marker)
    return None


def _scan_string(source: str, i: int, out: list[str]) -> int:
    n = len(source)
    start = i
    i += 1
    while i < n:
        if source[i] == "\\":
            i += 2
            continue
        if source[i] == '"':
            i += 1
            break
        i += 1
    literal = source[start:i]
    keep = _PATH_ATTRIBUTE_OPEN.search("".join(out[-80:])) is not None
    out.append(literal if keep and "\n" not in literal else '""')
    return i


def _char_literal_end(source: str, i: int) -> int | None:
    # A char literal ('x', '\n', '\u{7f}'); a lifetime ('a) has no
    # closing quote and passes through untouched. The escape must be
    # honoured: pairing '\'' at its FIRST following quote leaves an
    # orphan quote that swallows the rest of the file.
    n = len(source)
    j = i + 1
    if j < n and source[j] == "\\":
        k = _char_escape_end(source, j + 1)
        if k < n and source[k] == "'":
            return k + 1
    elif j + 1 < n and source[j] not in ("'", "\n") and source[j + 1] == "'":
        return j + 2
    return None


def _char_escape_end(source: str, k: int) -> int:
    n = len(source)
    if source.startswith("u{", k):
        brace = source.find("}", k + 2)
        return k + 2 if brace == -1 else brace + 1
    if k < n and source[k] == "x":
        return k + 3
    return k + 1


class RustEntryDecls(NamedTuple):
    """What an entry file's top level declares, for crate attribution."""

    mods: set[str]
    items: set[str]
    # Declared name -> the file path its `#[path]` attribute names, kept
    # separate because the qn scheme keys the module by that FILE while
    # every crate path names it by the declared name (issue #1035).
    redirects: dict[str, str]


def _preceding_attribute_lines(top_level: str, decl_start: int) -> str:
    collected: list[str] = []
    end = decl_start
    while end > 0:
        line_start = top_level.rfind("\n", 0, end - 1) + 1
        line = top_level[line_start : end - 1]
        if not line.strip():
            # Whitespace between an attribute and its item is legal; keep
            # walking, exactly as the old newline-crossing `\s*` matched.
            end = line_start
            continue
        line_match = _ATTRIBUTE_LINE_PATTERN.fullmatch(line)
        if line_match is None:
            break
        collected.append(line_match.group(1))
        end = line_start
    return "".join(reversed(collected))


def entry_decls_of(top_level: str) -> RustEntryDecls:
    # One name may be declared once per cfg (`#[cfg(unix)] mod platform;`
    # beside its windows twin), each redirected somewhere else. Exactly one
    # of them compiles and nothing here knows which, so disagreeing targets
    # are ambiguous and the name keeps no redirect at all.
    redirects: dict[str, str] = {}
    ambiguous: set[str] = set()
    for decl in MOD_REDIRECT_PATTERN.finditer(top_level):
        attributes, name = decl.group(1), decl.group(2)
        # The pattern only sees SAME-LINE attributes now (line-local group,
        # issue #1089); the block above the declaration is gathered by a
        # linear walk over the preceding attribute-only lines.
        attributes = _preceding_attribute_lines(top_level, decl.start()) + attributes
        match = PATH_ATTRIBUTE_PATTERN.search(attributes)
        target = match.group(1) if match else None
        if name in redirects and redirects[name] != target:
            ambiguous.add(name)
        elif target is not None:
            redirects[name] = target
    for name in ambiguous:
        redirects.pop(name, None)
    return RustEntryDecls(
        set(MOD_DECL_PATTERN.findall(top_level)),
        set(ITEM_DECL_PATTERN.findall(top_level)),
        redirects,
    )


def top_level_only(stripped: str) -> str:
    """Keep only brace-depth-zero text of a comment-stripped source.

    A `mod unix;` nested in an inline `mod sys { ... }` block declares a
    file in a DIFFERENT directory, and a method inside an `impl` block is
    not a crate-root item; both would otherwise match the line-anchored
    declaration patterns. Depth-0 MACRO bodies (`cfg_if! { ... }`,
    `macro_rules! m { ... }`) are kept instead: the declarations they emit
    are top-level, so their braces and semicolons become newlines to keep
    each one line-anchored. A depth-0 `}` becomes a newline so a
    declaration following it on the same line stays anchored.
    """
    out: list[str] = []
    depth = 0
    in_macro = False
    for c in stripped:
        if c == "{":
            in_macro = _open_brace(out, depth, in_macro)
            depth += 1
        elif c == "}":
            depth = max(depth - 1, 0)
            if depth == 0 or in_macro:
                out.append("\n")
            in_macro = in_macro and depth > 0
        else:
            out.extend(_top_level_char(c, depth, in_macro))
    return "".join(out)


def _open_brace(out: list[str], depth: int, in_macro: bool) -> bool:
    # A depth-0 `{` is kept and decides whether a macro body opens (its
    # braces then become newlines); returns the new in-macro state.
    if depth == 0:
        in_macro = bool(_MACRO_OPEN_RE.search("".join(out[-80:])))
        out.append("{")
    if in_macro:
        out.append("\n")
    return in_macro


def _top_level_char(c: str, depth: int, in_macro: bool) -> str:
    # What a non-brace character contributes: depth-0 text and newlines pass
    # through; inside a depth-0 macro body it passes too, with each `;`
    # followed by a newline so emitted declarations stay line-anchored.
    if depth == 0 or c == "\n":
        return c
    if not in_macro:
        return ""
    return c + "\n" if c == ";" else c
