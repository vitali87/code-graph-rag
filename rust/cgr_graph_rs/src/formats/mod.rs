//! The two file formats cgr once read and wrote through third-party Python
//! packages: XML project files (`defusedxml`) and TOML workspace files
//! (`tomli-w`).
//!
//! Each needs only a sliver of its package, so it lives here instead, built
//! on no third-party crate: cgr no longer installs either package. The
//! module uses nothing else in this crate, and `python` is its only tie to
//! PyO3, so it can be lifted out into a library of its own.
//!
//! There is no Python fallback: `codebase_rag/utils/safe_xml.py` and
//! `toml_writer.py` call these functions, and must get what defusedxml and
//! tomli-w gave. `codebase_rag/tests/test_safe_xml.py` holds the XML reader
//! to Python's own expat (which defusedxml wrapped) over hand-written and
//! mutated documents; `test_toml_writer.py` holds the writer to tomli-w's
//! text.

mod charsets;
#[cfg(feature = "python")]
pub mod python;
pub mod toml;
pub mod xml;
mod xml_names;

#[cfg(test)]
mod tests;
