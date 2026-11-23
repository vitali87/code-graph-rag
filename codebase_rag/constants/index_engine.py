# The per-language index engine: the CGR_INDEX_ENGINE setting
# (docs/reports/PHASE1_SEAMS.md, section 1).

from enum import StrEnum

from .ast_nodes import JS_TS_LANGUAGES
from .languages import C_FAMILY_LANGUAGES, SupportedLanguage


# `auto` is the engine this release runs a language on, read from
# INDEX_ENGINE_DEFAULTS; a resolved selection holds only the other two.
class IndexEngine(StrEnum):
    AUTO = "auto"
    PYTHON = "python"
    RUST = "rust"


# `LANGUAGE=ENGINE` in the setting's value.
INDEX_ENGINE_ASSIGN = "="

# What `auto` means, for each language this release indexes. A key is a
# language, or a family that resolves its members' imports and includes
# across files and so must index on one engine: one entry is all a family can
# have, so the defaults never split one. When a language's default moves to
# Rust, its entry changes here and the setting's default stays `auto`.
INDEX_ENGINE_DEFAULTS: dict[frozenset[SupportedLanguage], IndexEngine] = {
    frozenset({SupportedLanguage.PYTHON}): IndexEngine.PYTHON,
    JS_TS_LANGUAGES: IndexEngine.PYTHON,
    frozenset({SupportedLanguage.RUST}): IndexEngine.PYTHON,
    frozenset({SupportedLanguage.GO}): IndexEngine.PYTHON,
    frozenset({SupportedLanguage.SCALA}): IndexEngine.PYTHON,
    frozenset({SupportedLanguage.JAVA}): IndexEngine.PYTHON,
    C_FAMILY_LANGUAGES: IndexEngine.PYTHON,
    frozenset({SupportedLanguage.PHP}): IndexEngine.PYTHON,
    frozenset({SupportedLanguage.LUA}): IndexEngine.PYTHON,
    frozenset({SupportedLanguage.CSHARP}): IndexEngine.PYTHON,
    frozenset({SupportedLanguage.DART}): IndexEngine.PYTHON,
    frozenset({SupportedLanguage.SQL}): IndexEngine.PYTHON,
    frozenset({SupportedLanguage.JULIA}): IndexEngine.PYTHON,
}


# The tier `GraphUpdater._route` sends a file to, which is how it is indexed:
# libclang already emitted it, a loaded tree-sitter grammar parses it, it is a
# dependency manifest or a notebook, the ast-grep or document tier takes it,
# or none does and it keeps its File node alone. The hand-off's file plan
# records it (docs/reports/PHASE1_SEAMS.md, section 3).
class FileTier(StrEnum):
    TREE_SITTER = "tree_sitter"
    CPP_FRONTEND = "cpp_frontend"
    MANIFEST = "manifest"
    NOTEBOOK = "notebook"
    AST_GREP = "ast_grep"
    DOCUMENT = "document"
    NONE = "none"
