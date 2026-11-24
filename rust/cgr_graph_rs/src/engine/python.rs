//! The Python face of `engine`: the `Engine` and `IgnoreRules` classes
//! that `codebase_rag/engine_frame.py` wraps. They are internal to cgr, not
//! part of its public surface.

use std::path::PathBuf;

use pyo3::exceptions::{PyRuntimeError, PyTypeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::PyString;

use super::ignore::{self, PatternError};

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

/// The ignore rules over one exclude set and one unignore set
/// (`engine::ignore::IgnoreRules`). Each method answers what the
/// path_utils function of its name answers for those sets, and raises
/// `ValueError` where that function raises on a pattern pathspec refuses.
#[pyclass(frozen, name = "IgnoreRules", module = "cgr_graph_rs")]
pub struct PyIgnoreRules {
    rules: ignore::IgnoreRules,
}

#[pymethods]
impl PyIgnoreRules {
    /// The rules for `exclude` and `unignore`, any iterables of str (None
    /// for none). A pattern that cannot compile raises only when a method
    /// reaches its set, as in Python.
    #[new]
    #[pyo3(signature = (exclude = None, unignore = None))]
    fn new(
        exclude: Option<&Bound<'_, PyAny>>,
        unignore: Option<&Bound<'_, PyAny>>,
    ) -> PyResult<Self> {
        Ok(PyIgnoreRules {
            rules: ignore::IgnoreRules::new(strings(exclude)?, strings(unignore)?),
        })
    }

    /// `cs.IGNORE_PATTERNS` as the rules hold it.
    #[classattr]
    #[pyo3(name = "BUILTIN_IGNORED_DIRS")]
    fn builtin_ignored_dirs() -> Vec<&'static str> {
        ignore::BUILTIN_IGNORED_DIRS.to_vec()
    }

    /// `cs.VCS_METADATA_DIRS` as the rules hold it.
    #[classattr]
    #[pyo3(name = "VCS_METADATA_DIRS")]
    fn vcs_metadata_dirs() -> Vec<&'static str> {
        ignore::VCS_METADATA_DIRS.to_vec()
    }

    /// `cs.UNCONDITIONAL_IGNORE_SUFFIXES` as the rules hold it.
    #[classattr]
    #[pyo3(name = "UNCONDITIONAL_IGNORE_SUFFIXES")]
    fn unconditional_ignore_suffixes() -> Vec<&'static str> {
        ignore::UNCONDITIONAL_IGNORE_SUFFIXES.to_vec()
    }

    /// `cs.RESCUABLE_IGNORE_SUFFIXES` as the rules hold it.
    #[classattr]
    #[pyo3(name = "RESCUABLE_IGNORE_SUFFIXES")]
    fn rescuable_ignore_suffixes() -> Vec<&'static str> {
        ignore::RESCUABLE_IGNORE_SUFFIXES.to_vec()
    }

    /// `matches_ignore_patterns(rel_path, exclude)`.
    fn matches_exclude(&self, rel_path: &str) -> PyResult<bool> {
        self.rules.matches_exclude(rel_path).map_err(value_error)
    }

    /// `matches_ignore_patterns(rel_path, unignore)`.
    fn matches_unignore(&self, rel_path: &str) -> PyResult<bool> {
        self.rules.matches_unignore(rel_path).map_err(value_error)
    }

    /// `should_keep_dir(dirname, dir_prefix, exclude, unignore)`.
    fn should_keep_dir(&self, dirname: &str, dir_prefix: &str) -> PyResult<bool> {
        self.rules
            .should_keep_dir(dirname, dir_prefix)
            .map_err(value_error)
    }

    /// `should_skip_rel_file(rel_path, dir_parts, exclude, unignore)`.
    fn should_skip_rel_file(&self, rel_path: &str, dir_parts: &Bound<'_, PyAny>) -> PyResult<bool> {
        let dir_parts = strings(Some(dir_parts))?;
        self.rules
            .should_skip_rel_file(rel_path, &dir_parts)
            .map_err(value_error)
    }

    /// `should_skip_path(root / rel_path, root, exclude, unignore, is_file)`
    /// for a path already known to resolve inside the root.
    fn should_skip_rel_path(&self, rel_path: &str, is_file: bool) -> PyResult<bool> {
        self.rules
            .should_skip_rel_path(rel_path, is_file)
            .map_err(value_error)
    }

    /// `unignore_names_this_file(rel_path, unignore)`.
    fn unignore_names_this_file(&self, rel_path: &str) -> bool {
        self.rules.unignore_names_this_file(rel_path)
    }

    /// `any(unignore_could_match_within(u, rel_dir) for u in unignore)`.
    fn unignore_could_match_within(&self, rel_dir: &str) -> bool {
        self.rules.unignore_could_match_within(rel_dir)
    }

    /// `unignore_names_vcs_dir(rel_dir, unignore)`.
    fn unignore_names_vcs_dir(&self, rel_dir: &str) -> PyResult<bool> {
        self.rules
            .unignore_names_vcs_dir(rel_dir)
            .map_err(value_error)
    }
}

/// The str items of an iterable; a lone str is refused rather than read as
/// its characters.
fn strings(values: Option<&Bound<'_, PyAny>>) -> PyResult<Vec<String>> {
    let Some(values) = values.filter(|values| !values.is_none()) else {
        return Ok(Vec::new());
    };
    if values.is_instance_of::<PyString>() {
        return Err(PyTypeError::new_err(
            "expected an iterable of str, not a str",
        ));
    }
    values.try_iter()?.map(|value| value?.extract()).collect()
}

fn value_error(error: PatternError) -> PyErr {
    PyValueError::new_err(error.to_string())
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<PyEngine>()?;
    m.add_class::<PyIgnoreRules>()
}
