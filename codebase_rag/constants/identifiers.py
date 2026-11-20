"""Reserved words a rename's new name may not take, per language (issue #3225).

Contextual or soft keywords (Python `match`/`type`, C# `record`, Kotlin
`data`) are valid names and are not listed. C# and Lua reuse the sets their
parsers already hold (`CSHARP_RESERVED_KEYWORDS`, `LUA_RESERVED_WORDS`).
"""

import keyword

# The Unicode general categories Go spells identifiers with: every letter
# category (`Lu`, `Ll`, `Lo`, ...) and the decimal digits.
UNICODE_LETTER_CATEGORY_PREFIX = "L"
UNICODE_DECIMAL_DIGIT_CATEGORY = "Nd"

PY_RESERVED_WORDS = frozenset(keyword.kwlist)
# ECMAScript reserved words everywhere, literals included. The words reserved
# only in some code are below: a CommonJS script may name a function `let` or
# `await` (`function await() {}` is legal outside modules and async bodies).
JS_RESERVED_WORDS = frozenset(
    {
        "break",
        "case",
        "catch",
        "class",
        "const",
        "continue",
        "debugger",
        "default",
        "delete",
        "do",
        "else",
        "enum",
        "export",
        "extends",
        "false",
        "finally",
        "for",
        "function",
        "if",
        "import",
        "in",
        "instanceof",
        "new",
        "null",
        "return",
        "super",
        "switch",
        "this",
        "throw",
        "true",
        "try",
        "typeof",
        "var",
        "void",
        "while",
        "with",
    }
)
# Reserved in strict code alone: modules, class bodies, a "use strict"
# prologue, and TypeScript, which is compiled as strict code.
JS_STRICT_RESERVED_WORDS = frozenset(
    {
        "implements",
        "interface",
        "let",
        "package",
        "private",
        "protected",
        "public",
        "static",
        "yield",
    }
)
# Reserved in a module and inside an async function's body.
JS_AWAIT = "await"
# Reserved inside a generator's body.
JS_YIELD = "yield"
# No `let` or `const` declaration may bind `let`.
JS_LET = "let"
# The token that makes a method or function a generator (`*gen() {}`).
JS_GENERATOR_STAR = "*"
# A package.json `type` that makes its `.js` files ES modules, and the one
# that keeps them CommonJS.
JS_PACKAGE_TYPE_MODULE = "module"
JS_PACKAGE_TYPE_COMMONJS = "commonjs"
# Names strict code may read but not bind.
JS_STRICT_RESTRICTED_BINDINGS = frozenset({"eval", "arguments"})
# The `#!` line tree-sitter-javascript parses before a script's statements.
TS_JS_HASH_BANG_LINE = "hash_bang_line"
# ZWNJ and ZWJ, which a JS name may hold after its first character.
JS_IDENTIFIER_JOINERS = frozenset({"\u200c", "\u200d"})
# The characters in Unicode's ID_Start, which a JS name may begin and
# continue with, that its XID_Start leaves out, as NFKC does not keep them
# (UAX #31). Python's `isidentifier` reads XID, so `aͺ` (U+037A)
# failed it.
JS_ID_ONLY_CHARACTERS = frozenset(
    "\u037a\u0e33\u0eb3\u309b\u309c"
    "\ufc5e\ufc5f\ufc60\ufc61\ufc62\ufc63\ufdfa\ufdfb"
    "\ufe70\ufe72\ufe74\ufe76\ufe78\ufe7a\ufe7c\ufe7e\uff9e\uff9f"
)
# How Python reads an identifier: NFKC-normalised, so `K` (the Kelvin sign)
# binds `K` (PEP 3131).
PY_IDENTIFIER_NORMAL_FORM = "NFKC"

JAVA_RESERVED_WORDS = frozenset(
    {
        "_",
        "abstract",
        "assert",
        "boolean",
        "break",
        "byte",
        "case",
        "catch",
        "char",
        "class",
        "const",
        "continue",
        "default",
        "do",
        "double",
        "else",
        "enum",
        "extends",
        "false",
        "final",
        "finally",
        "float",
        "for",
        "goto",
        "if",
        "implements",
        "import",
        "instanceof",
        "int",
        "interface",
        "long",
        "native",
        "new",
        "null",
        "package",
        "private",
        "protected",
        "public",
        "return",
        "short",
        "static",
        "strictfp",
        "super",
        "switch",
        "synchronized",
        "this",
        "throw",
        "throws",
        "transient",
        "true",
        "try",
        "void",
        "volatile",
        "while",
    }
)
GO_RESERVED_WORDS = frozenset(
    {
        "break",
        "case",
        "chan",
        "const",
        "continue",
        "default",
        "defer",
        "else",
        "fallthrough",
        "for",
        "func",
        "go",
        "goto",
        "if",
        "import",
        "interface",
        "map",
        "package",
        "range",
        "return",
        "select",
        "struct",
        "switch",
        "type",
        "var",
    }
)
# Strict and reserved keywords (2021 edition); `r#name` raw identifiers are
# left to the caller.
RUST_RESERVED_WORDS = frozenset(
    {
        "Self",
        "abstract",
        "as",
        "async",
        "await",
        "become",
        "box",
        "break",
        "const",
        "continue",
        "crate",
        "do",
        "dyn",
        "else",
        "enum",
        "extern",
        "false",
        "final",
        "fn",
        "for",
        "if",
        "impl",
        "in",
        "let",
        "loop",
        "macro",
        "match",
        "mod",
        "move",
        "mut",
        "override",
        "priv",
        "pub",
        "ref",
        "return",
        "self",
        "static",
        "struct",
        "super",
        "trait",
        "true",
        "try",
        "type",
        "typeof",
        "unsafe",
        "unsized",
        "use",
        "virtual",
        "where",
        "while",
        "yield",
    }
)
C_RESERVED_WORDS = frozenset(
    {
        "auto",
        "break",
        "case",
        "char",
        "const",
        "continue",
        "default",
        "do",
        "double",
        "else",
        "enum",
        "extern",
        "float",
        "for",
        "goto",
        "if",
        "inline",
        "int",
        "long",
        "register",
        "restrict",
        "return",
        "short",
        "signed",
        "sizeof",
        "static",
        "struct",
        "switch",
        "typedef",
        "union",
        "unsigned",
        "void",
        "volatile",
        "while",
    }
)
CPP_RESERVED_WORDS = C_RESERVED_WORDS - {"restrict"} | frozenset(
    {
        "alignas",
        "alignof",
        "and",
        "asm",
        "bool",
        "catch",
        "class",
        "co_await",
        "co_return",
        "co_yield",
        "concept",
        "const_cast",
        "constexpr",
        "decltype",
        "delete",
        "dynamic_cast",
        "explicit",
        "export",
        "false",
        "friend",
        "mutable",
        "namespace",
        "new",
        "noexcept",
        "not",
        "nullptr",
        "operator",
        "or",
        "private",
        "protected",
        "public",
        "reinterpret_cast",
        "requires",
        "static_assert",
        "static_cast",
        "template",
        "this",
        "thread_local",
        "throw",
        "true",
        "try",
        "typeid",
        "typename",
        "using",
        "virtual",
        "wchar_t",
        "xor",
    }
)
