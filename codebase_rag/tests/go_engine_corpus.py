"""The Go engine's differential corpus: file names and file heads.

`test_engine_go.py` writes these heads to files under these names and asks
`parsers/go/builds.py` and `read_package_clause`, and the Rust engine, for
each file's constraint, for whether files build together, and for each
file's package clause. The hand-written cases aim at the corners of the
reader `builds.py` ports from `go/build`: where a constraint may stand, the
two forms, block comments, Python's spaces and line breaks, bytes that are
not UTF-8; the generated ones mix the same pieces at random. The limits are
the test's own, written beside the answers they must change.
"""

from __future__ import annotations

import random
import sys

# Tags of every kind `decide` tells apart: GOOS (an implied one among them),
# GOARCH, `unix`, tags of a project's own, and near misses of the first two.
OS_TAGS = ("linux", "windows", "darwin", "android", "ios", "illumos", "solaris")
ARCH_TAGS = ("amd64", "arm64", "386", "wasm", "mips64p32le")
OWN_TAGS = ("cgo", "go1.21", "t1", "t2", "a.b", "_x", "9")
NEAR_TAGS = ("unix", "Linux", "AMD64", "linuxx")
TAGS = (*OS_TAGS, *ARCH_TAGS, *OWN_TAGS, *NEAR_TAGS)

# Every character Python strips and splits at, and some it does not.
PY_SPACES = tuple(
    chr(code) for code in range(sys.maxunicode + 1) if chr(code).isspace()
)
NOT_SPACES = ("\x00", "\x08", "\u180e", "\u200b", "\ufeff", "\U0010ffff")
# Where `str.splitlines` ends a line, and a pair it reads as two.
LINE_BREAKS = (
    "\n",
    "\r\n",
    "\r",
    "\v",
    "\f",
    "\x1c",
    "\x1d",
    "\x1e",
    "\x85",
    "\u2028",
    "\u2029",
    "\n\r",
)

HAND_NAMES = (
    "a.go",
    "i_linux.go",
    "i_windows.go",
    "i_linux_amd64.go",
    "i_linux_arm64.go",
    "i_windows_amd64.go",
    "i_amd64.go",
    "i_linux_test.go",
    "i_android.go",
    "linux.go",
    "windows.go",
    "i_linux_extra.go",
    "_linux.go",
    "x_Linux.go",
    "x_linux.Go",
    "x_linux.go.go",
    ".go",
    "_test.go",
    "x_test.go",
    "x_test_linux.go",
    "x_linux_test_test.go",
    "x_amd64_linux.go",
    "x__linux.go",
    "x_linux_.go",
    "x_unix.go",
    "x_js_wasm.go",
    "x_ios_arm64_test.go",
    "dir/x_darwin.go",
    "dir/./x_solaris.go",
    "dir//x_illumos.go",
    "x_linux",
    "x_linux.txt",
    "x.go_linux",
    "\u00e9_linux.go",
    "x_linux .go",
)
_NAME_PARTS = (*OS_TAGS, *ARCH_TAGS, "test", "x", "", "extra", "Linux", "unix")
_NAME_SUFFIXES = (".go", ".go", ".go", "", ".go.go", ".Go")
_NAME_DIRS = ("", "", "a/", "a/./", "a/b/")


def generated_names(rng: random.Random, count: int) -> list[str]:
    names = []
    for _ in range(count):
        parts = [rng.choice(_NAME_PARTS) for _ in range(rng.randint(1, 4))]
        stem = "_".join(parts) or "x"
        names.append(f"{rng.choice(_NAME_DIRS)}{stem}{rng.choice(_NAME_SUFFIXES)}")
    return names


# Heads `test_go_build_constraints.py` and the canonical text's tests read,
# and the corners of the reader they leave out.
HAND_HEADS: tuple[str | bytes, ...] = (
    "//go:build linux\n",
    "//go:build !linux\n",
    "//go:build linux || darwin\n",
    "//go:build windows\n",
    "//go:build unix\n",
    "//go:build darwin\n",
    "//go:build linux && !(arm64 || cgo)\n",
    "//go:build arm64\n",
    "//go:build foo\n",
    "//go:build !foo\n",
    "//go:build bar\n",
    "//go:build go1.21\n",
    "//go:build !go1.22\n",
    "// +build linux,amd64 darwin\n\n",
    "// +build windows\n\n",
    "// +build linux\n\n",
    "// +build linux\npackage p\n",
    "package p\n\n//go:build linux\n",
    "//go:buildlinux\n\npackage p\n",
    "//go:build ((linux)\n",
    "//go:build linux))\n",
    "//go:build ) linux (\n",
    "//go:build linux && || windows\n",
    "//go:build linux $ windows\n",
    "//go:build \x00\n",
    "//go:build linux &&\n",
    "// +build ,,, !\n\n",
    "//go:build " + "(" * 300 + "linux" + ")" * 300 + "\n",
    "//go:build " + "!" * 5000 + "linux\n",
    "//go:build " + " && ".join(["linux"] * 900) + "\n",
    "//go:build " + "(" * 50 + "linux" + ")" * 50 + "\n",
    "/* license */\n\n//go:build linux\n\npackage p\n",
    "/*\n * Copyright 2026\n */\n//go:build linux\n\npackage p\n",
    "// Copyright\n/* more */\n\n//go:build linux\npackage p\n",
    "/* //go:build windows */\n//go:build linux\n\npackage p\n",
    "/*\n//go:build linux\n*/\n\npackage p\n",
    "//go:build linux\n/* x */ package p\n",
    "/* x */\n\n// +build linux\n\npackage p\n",
    "// +build linux\n\n/* x */\npackage p\n",
    "/* unterminated\n//go:build linux\n",
    "//go:build linux && amd64\n\npackage p\n",
    "//go:build !windows && (amd64 || arm64)\n\npackage p\n",
    "//go:build (linux && cgo) || go1.21\n\npackage p\n",
    "//go:build linux && cgo || go1.21\n\npackage p\n",
    "//go:build !(linux && amd64)\n\npackage p\n",
    "//go:build ((linux))\n\npackage p\n",
    "//go:build linux && linux\n\npackage p\n",
    "//go:build custom_tag && !other.tag\n\npackage p\n",
    "//go:build linux && !linux\n\npackage p\n",
    "//go:build linux || !linux\n\npackage p\n",
    "//go:build (a && b) || (a && c)\n\npackage p\n",
    "//go:build (a && b && c) || (b && a)\n\npackage p\n",
    "//go:build t1 && (t2 || !t1) && !t2\n\npackage p\n",
    "//go:build linux\n//go:build windows\n\npackage p\n",
    "// +build linux\n// +build amd64\n\npackage p\n",
    "// +build linux\n\n// +build !linux\n\npackage p\n",
    "// +build !!linux\n\npackage p\n",
    "// +build linux,,amd64\n\npackage p\n",
    "// +build \n\npackage p\n",
    "//+build linux\n\npackage p\n",
    "//go:build\tlinux\n\npackage p\n",
    "//go:build\n\npackage p\n",
    "//go:build &\n\npackage p\n",
    "//go:build linux|darwin\n\npackage p\n",
    "//go:build linux&&amd64\n\npackage p\n",
    "//go:build !\n\npackage p\n",
    "//go:build ()\n\npackage p\n",
    "/* a */ /* b */ package p\n//go:build linux\n",
    "/* a */ //go:build linux\n\npackage p\n",
    "/*/ //go:build linux */\n\npackage p\n",
    "/**/\n//go:build linux\n\npackage p\n",
    "*/\n//go:build linux\n\npackage p\n",
    "//go:build linux\n\npackage p\r\n",
    "//go:build linux\r\n\r\npackage p\r\n",
    "\u3000//go:build \u00a0linux\x1f\n\x85package p",
    "//go:build linux\x1fpackage p",
    "//go:build linux\u200b\npackage p",
    "\ufeff//go:build linux\n\npackage p\n",
    "",
    "\n\n\n",
    "package p\n",
    b"//go:build linux\xff\n\npackage p\n",
    b"\xff//go:build linux\n\npackage p\n",
    b"//go:build li\xc3nux\n\npackage p\n",
    b"\xef\xbb\xbf//go:build linux\n\npackage p\n",
    b"//go:build linux\n// \xed\xa0\x80 \xc0\xaf \xe2\x82\n\npackage p\n",
    b"// \xf4\x90\x80\x80\n//go:build linux\n\npackage p\n",
)


def whitespace_heads() -> list[str]:
    """Each of Python's spaces, and characters that are none, wherever the
    reader strips, splits or tokenizes; each line break between lines."""
    heads = []
    for space in (*PY_SPACES, *NOT_SPACES):
        heads += [
            f"//go:build{space}linux{space}&&{space}amd64{space}\n\npackage p\n",
            f"//go:build linux{space}&&{space}!amd64{space}\n{space}\npackage p\n",
            f"// +build linux,amd64{space}darwin{space}\n{space}\npackage p\n",
            f"//go:build linux{space}package p",
            f"{space}/*{space}x{space}*/{space}//go:build windows\n\npackage p\n",
            f"{space}package{space}p{space}\n",
        ]
    for line_break in LINE_BREAKS:
        heads += [
            line_break.join(
                ("// +build linux", "", "//go:build amd64", "/* x", "*/", "package p")
            ),
            line_break.join(("// +build linux", "// +build cgo", "", "package p")),
            f"/* a{line_break}//go:build windows */{line_break}package p",
        ]
    return heads


_BAD_TOKENS = ("&", "|", "$", "(", ")", "!", "&&", "||", ",", "\u00e9", "()")


def _expression(rng: random.Random, depth: int = 0) -> str:
    roll = rng.random()
    if depth > 4 or roll < 0.35:
        return rng.choice(TAGS)
    if roll < 0.5:
        return "!" + _expression(rng, depth + 1)
    if roll < 0.62:
        return f"({_expression(rng, depth + 1)})"
    operator = rng.choice((" && ", " || ", "&&", "||", "  &&\t"))
    return _expression(rng, depth + 1) + operator + _expression(rng, depth + 1)


def _malformed(rng: random.Random, text: str) -> str:
    at = rng.randint(0, len(text))
    return f"{text[:at]}{rng.choice(_BAD_TOKENS)}{text[at:]}"


def _plus_terms(rng: random.Random) -> str:
    options = []
    for _ in range(rng.randint(0, 3)):
        terms = [
            (rng.choice(("!", "!!")) if rng.random() < 0.4 else "") + rng.choice(TAGS)
            for _ in range(rng.randint(1, 3))
        ]
        if rng.random() < 0.08:
            terms.append("")
        options.append(",".join(terms))
    return " ".join(options)


def _line(rng: random.Random) -> str:
    roll = rng.random()
    expression = _expression(rng)
    if rng.random() < 0.15:
        expression = _malformed(rng, expression)
    gap = rng.choice(("", "\t", "  "))
    lines = (
        "",
        "",
        "// comment",
        "/* license */",
        "/*",
        "*/",
        " * text",
        "/* a */ /* b */",
        f"//go:build {expression}",
        f"//go:build{gap}{expression}",
        f"// +build {_plus_terms(rng)}",
        f"// +build {_plus_terms(rng)}",
        "//+build linux",
        "package p",
        "/* x */ package p",
        "func f() {}",
    )
    return lines[int(roll * len(lines))]


def generated_heads(rng: random.Random, count: int) -> list[str]:
    heads = []
    for _ in range(count):
        lines = [_line(rng) for _ in range(rng.randint(0, 7))]
        if rng.random() < 0.6:
            lines.append("package p")
        separator = rng.choice((*LINE_BREAKS, *("\n",) * 8))
        head = separator.join(lines) + rng.choice(("", separator))
        if rng.random() < 0.1:
            head = head.replace(" ", rng.choice((*PY_SPACES, *NOT_SPACES)))
        heads.append(head)
    return heads


# Package clauses: the first code line, past comments that may hide one.
HAND_CLAUSE_HEADS: tuple[str | bytes, ...] = (
    "package p\n",
    "// doc\n\npackage main\n",
    "/*\npackage docs describes why this stub exists.\n*/\npackage main\n",
    "package main;\n\nfunc main() {}\n",
    "package main;;\n",
    "package/*doc*/gen\n",
    "package/*doc*/\n",
    "/* a */ /* b */ package q // c\n",
    "/*/ package p */ package q\n",
    "/* a *//* b */package r\n",
    "package//c\n",
    "package p q\n",
    "packagep\n",
    "Package p\n",
    "package\n",
    "package\tp\n",
    "package \u00e9t\u00e9\n",
    "func f() {}\npackage p\n",
    "/* never closed\npackage p\n",
    "*/ package p\n",
    "\u3000package\u00a0p\x85",
    "package p\r\n",
    "\ufeffpackage p\n",
    "",
    "\n\n",
    b"package \xff\xfe\n",
    b"/* \xff */ package p\n",
)
_CLAUSE_FRAGMENTS = (
    "package",
    "package",
    " ",
    " ",
    "p",
    "main",
    ";",
    "/*",
    "*/",
    "//",
    "x",
    "\t",
    "\n",
    "\n",
    "\u3000",
    "\u00e9",
    "\r",
    "\x85",
)


def generated_clause_heads(rng: random.Random, count: int) -> list[str]:
    return [
        "".join(rng.choice(_CLAUSE_FRAGMENTS) for _ in range(rng.randint(0, 12)))
        for _ in range(count)
    ]


# Go sources for the parse: every Go test's snippets come from the test
# files themselves; these add what a snippet rarely holds.
EXTRA_SOURCES: tuple[bytes, ...] = (
    b"",
    b"package p\n\nfunc f( {\n\tx := \n}\n",
    b'// caf\xc3\xa9\npackage p\n\nvar s = "\xff\xfe"\n',
    b"package p\r\n\r\ntype Box[T any] struct{ v T }\r\n",
)
