//! The Python face of `engine`: the `Engine` and `IgnoreRules` classes
//! that `codebase_rag/engine_frame.py` wraps. They are internal to cgr, not
//! part of its public surface.

use std::borrow::Cow;
use std::ffi::OsString;
use std::path::PathBuf;

use pyo3::exceptions::{PyRuntimeError, PyTypeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyList, PyString, PyTuple};

use super::discover;
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

    /// `cs.CGR_STATE_FILENAMES` as the walk holds it.
    #[classattr]
    #[pyo3(name = "STATE_FILENAMES")]
    fn state_filenames() -> Vec<&'static str> {
        discover::STATE_FILENAMES.to_vec()
    }

    /// `cs.ROOT_DIR_KEY` as the walk holds it.
    #[classattr]
    #[pyo3(name = "ROOT_DIR_KEY")]
    fn root_dir_key() -> &'static str {
        discover::ROOT_DIR_KEY
    }

    /// `cs.TRACKED_SOURCE_DIR_NAMES` as the rescue holds it.
    #[classattr]
    #[pyo3(name = "TRACKED_SOURCE_DIR_NAMES")]
    fn tracked_source_dir_names() -> Vec<&'static str> {
        ignore::TRACKED_SOURCE_DIR_NAMES.to_vec()
    }

    /// The characters `cs.TRACKED_PATH_PATTERN_CHARS` finds, as one str.
    #[classattr]
    #[pyo3(name = "TRACKED_PATH_PATTERN_CHARS")]
    fn tracked_path_pattern_chars() -> String {
        ignore::TRACKED_PATH_PATTERN_CHARS.iter().collect()
    }

    /// `cs.GIT_LS_FILES_TIMEOUT_S` as the rescue holds it.
    #[classattr]
    #[pyo3(name = "GIT_LS_FILES_TIMEOUT_S")]
    fn git_ls_files_timeout_s() -> u64 {
        ignore::GIT_LS_FILES_TIMEOUT.as_secs()
    }

    /// The repository's file plan: what `walk_eligible_files(root, exclude,
    /// unignore)` yields and reports, and each file's md5, as the tuple
    /// `(files, dirs, symlinks, special, undecodable, hashes)`. `files`
    /// holds `(dirpath, filename, rel_path)` in walk order, `dirs` what
    /// `on_dir` receives, in order, and each report list the path from the
    /// root of every entry reported, a name that is not UTF-8 spelled as
    /// Python spells it. Raises `ValueError` where Python's walk raises on
    /// a pattern pathspec refuses. The interpreter lock is released for the
    /// walk and the hashing.
    #[pyo3(signature = (root, exclude = None, unignore = None))]
    fn file_plan<'py>(
        &self,
        py: Python<'py>,
        root: PathBuf,
        exclude: Option<&Bound<'py, PyAny>>,
        unignore: Option<&Bound<'py, PyAny>>,
    ) -> PyResult<Bound<'py, PyTuple>> {
        let rules = ignore::IgnoreRules::new(strings(exclude)?, strings(unignore)?);
        let plan = py
            .detach(|| self.engine.file_plan(root.as_os_str(), &rules))
            .map_err(value_error)?;
        // One str per folder, shared by its files' tuples.
        let dirpaths: Vec<Bound<'py, PyString>> = plan
            .dirs
            .iter()
            .map(|dir| {
                let Ok(dirpath) = dir.dirpath.as_os_str().into_pyobject(py);
                dirpath
            })
            .collect();
        let files = PyList::new(
            py,
            plan.files.iter().map(|file| {
                (
                    dirpaths[file.dir].clone(),
                    file.name.as_str(),
                    file.rel_path.as_str(),
                )
            }),
        )?;
        let dirs = PyList::new(
            py,
            plan.dirs
                .iter()
                .zip(&dirpaths)
                .map(|(dir, dirpath)| (dir.key.as_str(), dirpath.clone())),
        )?;
        let reported = |paths: &[OsString]| PyList::new(py, paths.iter().map(OsString::as_os_str));
        PyTuple::new(
            py,
            [
                files.into_any(),
                dirs.into_any(),
                reported(&plan.links)?.into_any(),
                reported(&plan.special)?.into_any(),
                reported(&plan.undecodable)?.into_any(),
                PyList::new(py, plan.hashes)?.into_any(),
            ],
        )
    }

    /// `load_ignore_patterns(root)` as the frame reads the two ignore files
    /// and runs the tracked-source rescue: `(exclude, unignore,
    /// refused_lines, unreadable, tracked_dirs)`, the sets sorted,
    /// `refused_lines` each `(file name, line number)` Python warns of,
    /// `unreadable` each file name it warns of, and `tracked_dirs` the
    /// folders it logs. The interpreter lock is released meanwhile.
    fn ignore_patterns<'py>(
        &self,
        py: Python<'py>,
        root: PathBuf,
    ) -> PyResult<Bound<'py, PyTuple>> {
        let found = py.detach(|| ignore::load_ignore_patterns(&root));
        (
            found.exclude.into_iter().collect::<Vec<_>>(),
            found.unignore.into_iter().collect::<Vec<_>>(),
            found.refused_lines,
            found.unreadable,
            found.tracked_dirs.into_iter().collect::<Vec<_>>(),
        )
            .into_pyobject(py)
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
    fn matches_exclude(&self, rel_path: Text<'_>) -> PyResult<bool> {
        self.rules.matches_exclude(&rel_path).map_err(value_error)
    }

    /// `matches_ignore_patterns(rel_path, unignore)`.
    fn matches_unignore(&self, rel_path: Text<'_>) -> PyResult<bool> {
        self.rules.matches_unignore(&rel_path).map_err(value_error)
    }

    /// `should_keep_dir(dirname, dir_prefix, exclude, unignore)`.
    fn should_keep_dir(&self, dirname: Text<'_>, dir_prefix: Text<'_>) -> PyResult<bool> {
        self.rules
            .should_keep_dir(&dirname, &dir_prefix)
            .map_err(value_error)
    }

    /// `should_skip_rel_file(rel_path, dir_parts, exclude, unignore)`.
    fn should_skip_rel_file(
        &self,
        rel_path: Text<'_>,
        dir_parts: &Bound<'_, PyAny>,
    ) -> PyResult<bool> {
        let dir_parts = strings(Some(dir_parts))?;
        self.rules
            .should_skip_rel_file(&rel_path, &dir_parts)
            .map_err(value_error)
    }

    /// `should_skip_path(root / rel_path, root, exclude, unignore, is_file)`
    /// for a path already known to resolve inside the root.
    fn should_skip_rel_path(&self, rel_path: Text<'_>, is_file: bool) -> PyResult<bool> {
        self.rules
            .should_skip_rel_path(&rel_path, is_file)
            .map_err(value_error)
    }

    /// `unignore_names_this_file(rel_path, unignore)`.
    fn unignore_names_this_file(&self, rel_path: Text<'_>) -> bool {
        self.rules.unignore_names_this_file(&rel_path)
    }

    /// `any(unignore_could_match_within(u, rel_dir) for u in unignore)`.
    fn unignore_could_match_within(&self, rel_dir: Text<'_>) -> bool {
        self.rules.unignore_could_match_within(&rel_dir)
    }

    /// `unignore_names_vcs_dir(rel_dir, unignore)`.
    fn unignore_names_vcs_dir(&self, rel_dir: Text<'_>) -> PyResult<bool> {
        self.rules
            .unignore_names_vcs_dir(&rel_dir)
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
    values
        .try_iter()?
        .map(|value| Ok(value?.extract::<Text<'_>>()?.0.into_owned()))
        .collect()
}

/// `str.encode`'s codec and error handler for a str holding a lone
/// surrogate: four bytes per code point, the surrogates kept.
const UTF_32_LE: &str = "utf-32-le";
const SURROGATEPASS: &str = "surrogatepass";
const CODE_POINT_BYTES: usize = 4;

/// A str crossing into the rules, as `ignore::string_from_code_points`
/// holds it: one char per code point, so a name with a byte that is not
/// UTF-8, which reaches Python holding a lone surrogate, is judged as
/// Python judges it rather than refused as `&str` refuses it. Every str the
/// rules take, patterns and method arguments alike, comes through here; no
/// method hands a str back. A str with no surrogate is read in place.
struct Text<'a>(Cow<'a, str>);

impl<'a> FromPyObject<'a, '_> for Text<'a> {
    type Error = PyErr;

    fn extract(obj: Borrowed<'a, '_, PyAny>) -> PyResult<Self> {
        if let Ok(text) = <&'a str as FromPyObject<'a, '_>>::extract(obj) {
            return Ok(Text(Cow::Borrowed(text)));
        }
        let text = obj.cast::<PyString>()?;
        // `str.encode` itself, so a subclass is read as the fast path reads it.
        let encoded = obj.py().get_type::<PyString>().call_method1(
            pyo3::intern!(obj.py(), "encode"),
            (text, UTF_32_LE, SURROGATEPASS),
        )?;
        let code_points = encoded
            .cast::<PyBytes>()?
            .as_bytes()
            .chunks_exact(CODE_POINT_BYTES)
            .map(|bytes| u32::from_le_bytes([bytes[0], bytes[1], bytes[2], bytes[3]]));
        ignore::string_from_code_points(code_points)
            .map(|text| Text(Cow::Owned(text)))
            .ok_or_else(|| PyValueError::new_err("a str with a number that is no code point"))
    }
}

impl std::ops::Deref for Text<'_> {
    type Target = str;

    fn deref(&self) -> &str {
        &self.0
    }
}

fn value_error(error: PatternError) -> PyErr {
    PyValueError::new_err(error.to_string())
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<PyEngine>()?;
    m.add_class::<PyIgnoreRules>()
}
