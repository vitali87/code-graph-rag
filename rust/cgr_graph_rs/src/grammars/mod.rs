//! The tree-sitter grammars compiled into the crate
//! (docs/reports/PHASE2_FRAME.md, section 11).
//!
//! A grammar is compiled in when its language moves to the Rust engine, at
//! exactly the release, or git commit, that `uv.lock` holds for its Python
//! package, so the two engines parse with one grammar. Go's is here for
//! phase 3. C's and C++'s are the patched forks (issues #555 and #2424),
//! which a checkout already installed from git but a PyPI install could not:
//! PyPI refuses dependencies fetched from git, so PyPI users parsed C and C++
//! with the unpatched upstream grammars. `codebase_rag/parser_loader.py` now
//! hands py-tree-sitter the C and C++ grammars compiled here
//! (`Grammar.language()` in `python`), so the Python engine parses them with
//! the patched grammars on every install, and with the very ones the Rust
//! engine will.
//!
//! A matching version is not proof of a matching grammar: SQL 0.3.11's Python
//! package was generated at ABI 15 and its crate at ABI 14. So `fingerprint`
//! describes a grammar's structure, and `codebase_rag/tests/test_grammar_pins.py`
//! compares it with what py-tree-sitter reads from the Python package.

use sha2::{Digest, Sha256};
use tree_sitter::{Language, LanguageError, Parser};
use tree_sitter_language::LanguageFn;

use crate::trees::{describe, hex, Described};

#[cfg(feature = "python")]
pub mod python;
#[cfg(test)]
mod tests;

/// A grammar compiled into the crate.
pub struct Grammar {
    /// The language's name, as cgr's `SupportedLanguage` spells it.
    pub name: &'static str,
    /// What it was compiled from: the crate's release, or the fork's commit.
    /// Cargo.toml pins exactly this, and test_grammar_pins.py holds it to
    /// uv.lock.
    pub pin: &'static str,
    /// Its `queries/highlights.scm`: the file its Python package's
    /// `HIGHLIGHTS_QUERY` reads, at the same release or commit.
    pub highlights: &'static str,
    language: LanguageFn,
}

impl Grammar {
    pub fn language(&self) -> Language {
        Language::new(self.language)
    }

    /// The grammar's `TSLanguage`, a static of this library, so it lives as
    /// long as the library stays loaded.
    pub fn raw(&self) -> *const () {
        // SAFETY: a grammar's entry point takes nothing and returns its
        // static language.
        unsafe { self.language.into_raw()() }
    }
}

/// Every grammar compiled in, by name.
pub static GRAMMARS: [Grammar; 3] = [
    Grammar {
        name: "c",
        pin: "fa327ad98f847f8173fc46926be4165c7971f960",
        highlights: tree_sitter_c::HIGHLIGHT_QUERY,
        language: tree_sitter_c::LANGUAGE,
    },
    Grammar {
        name: "cpp",
        pin: "b4a7be2f62f36dd460a14b2d1728d42d81d2c486",
        highlights: tree_sitter_cpp::HIGHLIGHT_QUERY,
        language: tree_sitter_cpp::LANGUAGE,
    },
    Grammar {
        name: "go",
        pin: "0.25.0",
        highlights: tree_sitter_go::HIGHLIGHTS_QUERY,
        language: tree_sitter_go::LANGUAGE,
    },
];

/// The grammar compiled in for `name`, if one is.
pub fn grammar(name: &str) -> Option<&'static Grammar> {
    GRAMMARS.iter().find(|grammar| grammar.name == name)
}

/// A grammar's structure, as a runtime reads it from the grammar itself.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Fingerprint {
    pub abi_version: usize,
    /// None for a grammar generated before ABI 15, which records none.
    pub name: Option<String>,
    /// `tree-sitter.json`'s version, from ABI 15 on.
    pub semantic_version: Option<(u8, u8, u8)>,
    pub node_kind_count: usize,
    pub field_count: usize,
    pub parse_state_count: usize,
    /// The sha256, as lowercase hex, of every node kind in id order, each as
    /// its named and visible flags (one byte each, 0 or 1) and its name, then
    /// every field name from id 1 on. A name is its UTF-8 length as 4
    /// big-endian bytes and the bytes; a missing one is the length
    /// 0xFFFFFFFF alone. test_grammar_pins.py computes the same through
    /// py-tree-sitter.
    pub digest: String,
}

pub fn fingerprint(language: &Language) -> Fingerprint {
    let mut digest = Sha256::new();
    // A node kind and a field are numbered by u16 (`TSSymbol`, `TSFieldId`).
    for id in (0..language.node_kind_count()).map(|id| id as u16) {
        digest.update([
            u8::from(language.node_kind_is_named(id)),
            u8::from(language.node_kind_is_visible(id)),
        ]);
        name_into(&mut digest, language.node_kind_for_id(id));
    }
    for id in (1..=language.field_count()).map(|id| id as u16) {
        name_into(&mut digest, language.field_name_for_id(id));
    }
    Fingerprint {
        abi_version: language.abi_version(),
        name: language.name().map(str::to_owned),
        semantic_version: language
            .metadata()
            .map(|m| (m.major_version, m.minor_version, m.patch_version)),
        node_kind_count: language.node_kind_count(),
        field_count: language.field_count(),
        parse_state_count: language.parse_state_count(),
        digest: hex(&digest.finalize()),
    }
}

fn name_into(digest: &mut Sha256, name: Option<&str>) {
    match name {
        Some(name) => {
            digest.update((name.len() as u32).to_be_bytes());
            digest.update(name.as_bytes());
        }
        None => digest.update(u32::MAX.to_be_bytes()),
    }
}

/// `source` parsed with `grammar` by this crate's own runtime, every node as
/// `trees::describe` reports it; None if the parse was cancelled.
pub fn parse_described(
    grammar: &Grammar,
    source: &[u8],
) -> Result<Option<Vec<Described>>, LanguageError> {
    let mut parser = Parser::new();
    parser.set_language(&grammar.language())?;
    Ok(parser
        .parse(source, None)
        .map(|tree| describe(tree.root_node())))
}
