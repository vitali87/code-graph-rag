//! The Python face of `engine`: the `Engine` class that
//! `codebase_rag/engine_frame.py` wraps. It is internal to cgr, not part of
//! its public surface.

use std::path::PathBuf;

use pyo3::exceptions::PyRuntimeError;
use pyo3::prelude::*;

#[pyclass(frozen, name = "Engine", module = "cgr_graph_rs")]
pub struct PyEngine {
    engine: super::Engine,
}

#[pymethods]
impl PyEngine {
    /// An engine on `threads` worker threads; 0, the default, means one per
    /// available core.
    #[new]
    #[pyo3(signature = (threads = 0))]
    fn new(threads: usize) -> PyResult<Self> {
        super::Engine::new(threads)
            .map(|engine| PyEngine { engine })
            .map_err(|error| PyRuntimeError::new_err(error.to_string()))
    }

    /// How many worker threads the engine runs.
    #[getter]
    fn threads(&self) -> usize {
        self.engine.threads()
    }

    /// Each file's md5 as lowercase hex, in the order given, or `None` for a
    /// file that cannot be read. The interpreter lock is released while the
    /// files are read.
    fn md5_files(&self, py: Python<'_>, paths: Vec<PathBuf>) -> Vec<Option<String>> {
        py.detach(|| self.engine.md5_files(&paths))
    }
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<PyEngine>()
}
