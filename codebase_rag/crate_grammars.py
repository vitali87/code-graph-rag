"""The tree-sitter grammars compiled into the Rust extension, for py-tree-sitter.

`cgr_graph_rs` compiles Go's grammar and the patched C and C++ forks in
(rust/cgr_graph_rs/src/grammars, docs/reports/PHASE2_FRAME.md, section 11),
each at exactly the release or commit `uv.lock` holds for its Python package.
For the languages in `cs.CRATE_GRAMMAR_LANGUAGES` the parser loader takes the
grammar and its highlights query from here, on every install, and never from
a Python package. PyPI refuses dependencies fetched from git, so a PyPI
install could only get the upstream C and C++ grammars, which drop the
declaration after a `#define` whose value holds a block comment (#555); and
the Python engine would parse C differently from the Rust engine, which uses
the compiled forks. Go's compiled copy is the Rust engine's; the Python
engine keeps loading Go from its package, pinned to the same release.

`grammar_fingerprint` is the crate's `Grammar.fingerprint` as py-tree-sitter
reads a grammar, so a compiled grammar can be compared with a Python
package's, or with itself as py-tree-sitter sees it.
"""

from __future__ import annotations

import hashlib
from typing import Protocol

from tree_sitter import Language

from . import constants as cs
from .embedded_graph.extension import rust_extension
from .types_defs import (
    DescribedNode,
    GrammarFingerprint,
    LanguageLoader,
    NativeGrammarFingerprint,
)


class CrateGrammar(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def pin(self) -> str: ...

    @property
    def highlights(self) -> str: ...

    def fingerprint(self) -> NativeGrammarFingerprint: ...

    def describe(self, source: bytes) -> list[DescribedNode]: ...


def crate_grammar(lang_name: str) -> CrateGrammar | None:
    """The grammar the extension compiles in for `lang_name`, or None."""
    grammar_type = getattr(rust_extension(), cs.EG_RUST_GRAMMAR, None)
    if grammar_type is None or lang_name not in grammar_type.NAMES:
        return None
    grammar: CrateGrammar = grammar_type(lang_name)
    return grammar


def language_loader(lang_name: str) -> LanguageLoader:
    """The compiled grammar's entry point, called as a grammar package's
    `language` is: it returns what `tree_sitter.Language` takes. None when
    the extension compiles no grammar for `lang_name`."""
    if (grammar := crate_grammar(lang_name)) is None:
        return None
    loader: LanguageLoader = getattr(grammar, cs.EG_RUST_GRAMMAR_LANGUAGE)
    return loader


def crate_language(lang_name: str) -> Language | None:
    """The compiled grammar as py-tree-sitter's `Language`, or None.

    Two `Language` objects are equal when they hold the same grammar in
    memory, so this is what tells a language loaded from the crate from the
    same grammar loaded from a Python package."""
    loader = language_loader(lang_name)
    return None if loader is None else Language(loader())


def highlights_query(lang_name: str) -> str | None:
    """The compiled grammar's highlights query, or None."""
    grammar = crate_grammar(lang_name)
    return None if grammar is None else grammar.highlights


def native_fingerprint(grammar: CrateGrammar) -> GrammarFingerprint:
    """The grammar's structure as the crate's own runtime reads it."""
    return GrammarFingerprint(*grammar.fingerprint())


def grammar_fingerprint(language: Language) -> GrammarFingerprint:
    """What `Grammar.fingerprint` reports, as py-tree-sitter reads `language`."""
    parts: list[bytes] = []
    for kind_id in range(language.node_kind_count):
        parts.append(
            bytes(
                (
                    language.node_kind_is_named(kind_id),
                    language.node_kind_is_visible(kind_id),
                )
            )
        )
        parts.append(_name_bytes(language.node_kind_for_id(kind_id)))
    parts.extend(
        _name_bytes(language.field_name_for_id(field_id))
        for field_id in range(1, language.field_count + 1)
    )
    return GrammarFingerprint(
        abi_version=language.abi_version,
        name=language.name,
        semantic_version=language.semantic_version,
        node_kind_count=language.node_kind_count,
        field_count=language.field_count,
        parse_state_count=language.parse_state_count,
        digest=hashlib.sha256(b"".join(parts)).hexdigest(),
    )


def _name_bytes(name: str | None) -> bytes:
    if name is None:
        return cs.GRAMMAR_FP_MISSING_NAME.to_bytes(
            cs.GRAMMAR_FP_LENGTH_BYTES, cs.GRAMMAR_FP_BYTE_ORDER
        )
    encoded = name.encode(cs.ENCODING_UTF8)
    return (
        len(encoded).to_bytes(cs.GRAMMAR_FP_LENGTH_BYTES, cs.GRAMMAR_FP_BYTE_ORDER)
        + encoded
    )
