//! The two file formats cgr once read and wrote through third-party Python
//! packages: XML project files (`defusedxml`) and TOML workspace files
//! (`tomli-w`).
//!
//! Each needs only a sliver of its package, so it lives here instead, with no
//! crate of its own: cgr no longer installs either package. The module uses
//! nothing else in this crate, and `python` is its only tie to PyO3, so it
//! can be lifted out into a library of its own.
//!
//! As elsewhere in the crate, a Python implementation is the reference
//! (`codebase_rag/utils/safe_xml.py`, `codebase_rag/utils/toml_writer.py`)
//! and runs where the extension is not installed. These functions return
//! what it returns, and decline (`None`) on any input they do not reproduce
//! exactly, which the caller then hands to Python.

#[cfg(feature = "python")]
pub mod python;
pub mod toml;
pub mod xml;

#[cfg(test)]
mod tests;
