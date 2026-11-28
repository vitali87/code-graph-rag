//! The Python face of `grammars`: the `Grammar` class the parser loader takes
//! C and C++ from, and `bounded_scanner`, which it passes Python's grammar
//! through (`codebase_rag/crate_grammars.py`). Internal to cgr, not part of
//! its public surface.

use std::ffi::{c_void, CStr};
use std::ptr::NonNull;

use pyo3::exceptions::{PyKeyError, PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::PyCapsule;

use super::bounded_scanner::bounded;
use super::{fingerprint, grammar, parse_described, Grammar, GRAMMARS};
use crate::trees::Described;

/// The name py-tree-sitter's `Language` requires of the capsule it is given,
/// the one each grammar package's own `language()` returns.
const LANGUAGE_CAPSULE: &CStr = c"tree_sitter.Language";

/// `Fingerprint` as Python receives it: the ABI version, the grammar's name
/// and semantic version, its node-kind, field and parse-state counts, and the
/// digest of its kind and field names.
type NativeFingerprint = (
    usize,
    Option<String>,
    Option<(u8, u8, u8)>,
    usize,
    usize,
    usize,
    String,
);

#[pyclass(frozen, name = "Grammar", module = "cgr_graph_rs")]
pub struct PyGrammar {
    grammar: &'static Grammar,
}

#[pymethods]
impl PyGrammar {
    /// The names of the grammars compiled in.
    #[classattr]
    #[pyo3(name = "NAMES")]
    fn names() -> Vec<&'static str> {
        GRAMMARS.iter().map(|grammar| grammar.name).collect()
    }

    /// The grammar compiled in for `name`; KeyError when none is.
    #[new]
    fn new(name: &str) -> PyResult<Self> {
        grammar(name)
            .map(|grammar| PyGrammar { grammar })
            .ok_or_else(|| PyKeyError::new_err(name.to_owned()))
    }

    #[getter]
    fn name(&self) -> &'static str {
        self.grammar.name
    }

    /// The crate release or fork commit it was compiled from.
    #[getter]
    fn pin(&self) -> &'static str {
        self.grammar.pin
    }

    /// Its highlights query, as its Python package's `HIGHLIGHTS_QUERY`.
    #[getter]
    fn highlights(&self) -> &'static str {
        self.grammar.highlights
    }

    /// The compiled grammar as the capsule `tree_sitter.Language(...)` takes,
    /// as a grammar package's `language()` returns it.
    fn language<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyCapsule>> {
        let pointer = NonNull::new(self.grammar.raw().cast_mut().cast::<c_void>())
            .ok_or_else(|| PyRuntimeError::new_err("a grammar without a language"))?;
        // SAFETY: the pointer is the grammar's static `TSLanguage`, which
        // lives as long as this library: the extension module stays loaded
        // once imported, as a grammar package's does, and py-tree-sitter
        // only reads the language. No destructor: nothing was allocated.
        unsafe { PyCapsule::new_with_pointer(py, pointer, LANGUAGE_CAPSULE) }
    }

    /// The grammar's structure as this crate's runtime reads it.
    fn fingerprint(&self) -> NativeFingerprint {
        let found = fingerprint(&self.grammar.language());
        (
            found.abi_version,
            found.name,
            found.semantic_version,
            found.node_kind_count,
            found.field_count,
            found.parse_state_count,
            found.digest,
        )
    }

    /// `source` parsed by this crate's runtime with the grammar, every node
    /// as `TreeBridge.describe` reports one.
    fn describe(&self, source: &[u8]) -> PyResult<Vec<Described>> {
        parse_described(self.grammar, source)
            .map_err(|error| PyValueError::new_err(error.to_string()))?
            .ok_or_else(|| PyRuntimeError::new_err("the parse did not finish"))
    }
}

/// `language`, what a grammar package's `language()` returns, as a copy
/// whose external scanner writes nothing past the runtime's buffer (issue
/// #3831); None for anything `bounded` does not copy, which the caller keeps
/// as it is: a grammar of another ABI or without a scanner, or a binding old
/// enough to hand out its language as an int.
#[pyfunction]
fn bounded_scanner<'py>(
    py: Python<'py>,
    language: &Bound<'py, PyAny>,
) -> PyResult<Option<Bound<'py, PyCapsule>>> {
    let Ok(capsule) = language.cast::<PyCapsule>() else {
        return Ok(None);
    };
    if !capsule.is_valid_checked(Some(LANGUAGE_CAPSULE)) {
        return Ok(None);
    }
    let raw = capsule.pointer_checked(Some(LANGUAGE_CAPSULE))?;
    // SAFETY: a `tree_sitter.Language` capsule holds its grammar's static
    // `TSLanguage`, and the module that holds it stays loaded once imported.
    let Some(copy) = (unsafe { bounded(raw.as_ptr().cast_const().cast()) }) else {
        return Ok(None);
    };
    let pointer = NonNull::new(copy.cast_mut().cast::<c_void>())
        .ok_or_else(|| PyRuntimeError::new_err("a bounded grammar without a language"))?;
    // SAFETY: the copy is a static of this library, as `PyGrammar::language`'s
    // grammars are; py-tree-sitter only reads it. No destructor: it is never
    // freed.
    unsafe { PyCapsule::new_with_pointer(py, pointer, LANGUAGE_CAPSULE) }.map(Some)
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<PyGrammar>()?;
    m.add_function(wrap_pyfunction!(bounded_scanner, m)?)
}
