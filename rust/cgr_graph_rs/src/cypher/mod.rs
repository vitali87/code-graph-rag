//! The embedded engine's Cypher frontend, ported from
//! `codebase_rag/embedded_graph/{lexer,parser}.py`.
//!
//! The Python parser is the reference: for every query this one accepts it
//! returns the same tree, and for every query it rejects it raises the same
//! `CypherSyntaxError` (offset and message). Where Python's answer rests on
//! Python itself (Unicode character classes, unbounded integers, the
//! recursion limit), `parse` returns `Error::Defer` and the engine asks the
//! Python parser instead, so the port never has to approximate it.
//! `codebase_rag/tests/test_embedded_graph_rust_parser.py` holds the two to
//! that over every query cgr ships and a fuzzed corpus.

pub mod ast;
pub mod error;
pub mod lexer;
pub mod parser;
#[cfg(feature = "python")]
pub mod python;
pub mod vocab;

pub use error::{Defer, DeferReason, Error, SyntaxError};
pub use parser::parse;

#[cfg(test)]
mod tests;
