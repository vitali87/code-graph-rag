//! The Python face of `engine`: the `Engine`, `IgnoreRules` and
//! `ChangeBaseline` classes that `codebase_rag/engine_frame.py` wraps. They
//! are internal to cgr, not part of its public surface.
//!
//! A row the frame builds for the ingestor crosses as the tuple
//! `(label, [(key, value), ...], start, rel_type, end)`: the node's label
//! and its properties in order, then the edge from what contains it, each
//! end a `(label, key, value)` node identifier.

use std::borrow::Cow;
use std::collections::{BTreeSet, HashSet};
use std::ffi::OsString;
use std::path::PathBuf;

use pyo3::exceptions::{PyRuntimeError, PyTypeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyList, PyString, PyTuple};

use super::discover;
use super::ignore::{self, PatternError};
use super::rows::{self, ContainedRow, NodeId};
use super::state::{self, json, Entries};
use super::structure;

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

    /// `cs.HASH_CACHE_FILENAME`, `cs.DIR_MTIMES_FILENAME`,
    /// `cs.HASH_CACHE_UNREADABLE`, `cs.CGR_STATE_DIRNAME`,
    /// `cs.CGR_STATE_LOCK_FILENAMES`, `cs.EDIT_HISTORY_FILENAME`,
    /// `cs.PROJECT_NAME_DIGEST_MARKER` and `cs.PROJECT_NAME_DIGEST_LEN` as
    /// the state files hold them, and the digit limit the reader takes as
    /// `sys.get_int_max_str_digits()`.
    #[classattr]
    #[pyo3(name = "HASH_CACHE_FILENAME")]
    fn hash_cache_filename() -> &'static str {
        state::HASH_CACHE_FILENAME
    }

    #[classattr]
    #[pyo3(name = "DIR_MTIMES_FILENAME")]
    fn dir_mtimes_filename() -> &'static str {
        state::DIR_MTIMES_FILENAME
    }

    #[classattr]
    #[pyo3(name = "HASH_CACHE_UNREADABLE")]
    fn hash_cache_unreadable() -> &'static str {
        state::UNREADABLE
    }

    #[classattr]
    #[pyo3(name = "STATE_DIRNAME")]
    fn state_dirname() -> &'static str {
        state::STATE_DIRNAME
    }

    #[classattr]
    #[pyo3(name = "STATE_LOCK_FILENAMES")]
    fn state_lock_filenames() -> Vec<&'static str> {
        state::LOCK_FILENAMES.to_vec()
    }

    #[classattr]
    #[pyo3(name = "EDIT_HISTORY_FILENAME")]
    fn edit_history_filename() -> &'static str {
        state::EDIT_HISTORY_FILENAME
    }

    #[classattr]
    #[pyo3(name = "PROJECT_NAME_DIGEST_MARKER")]
    fn project_name_digest_marker() -> &'static str {
        state::DIGEST_MARKER
    }

    #[classattr]
    #[pyo3(name = "PROJECT_NAME_DIGEST_LEN")]
    fn project_name_digest_len() -> usize {
        state::DIGEST_LEN
    }

    #[classattr]
    #[pyo3(name = "INT_MAX_STR_DIGITS")]
    fn int_max_str_digits() -> usize {
        json::INT_MAX_STR_DIGITS
    }

    /// The checkout's state folder, `state_dir(repo)` under `cgr_home`
    /// (CGR_HOME, its `~` expanded), or None for a path Python cannot name.
    /// Nothing is created.
    fn state_dir(&self, py: Python<'_>, cgr_home: PathBuf, repo: PathBuf) -> Option<OsString> {
        py.detach(|| state::state_dir(&cgr_home, &repo).map(PathBuf::into_os_string))
    }

    /// `prepare_state_dir(repo)` under `cgr_home`, but for the edit
    /// history, which stays Python's: `(dir, adopted, failed,
    /// unavailable)`, the names moved in, each name left in the checkout
    /// with why, and why the folder could not be created. None for a path
    /// Python cannot name.
    #[allow(clippy::type_complexity)]
    fn prepare_state_dir(
        &self,
        py: Python<'_>,
        cgr_home: PathBuf,
        repo: PathBuf,
    ) -> Option<(OsString, Vec<String>, Vec<(String, String)>, Option<String>)> {
        let prepared = py.detach(|| state::prepare_state_dir(&cgr_home, &repo))?;
        Some((
            prepared.dir.into_os_string(),
            prepared.adopted,
            prepared
                .failed
                .into_iter()
                .map(|(name, error)| (name, error.to_string()))
                .collect(),
            prepared.unavailable.map(|error| error.to_string()),
        ))
    }

    /// The hash cache at `path` as `_load_hash_cache` reads it: `(entries,
    /// error)`, each `(name, hash)` in the file's order, and why a file
    /// that is there could not be used.
    fn read_hash_cache(
        &self,
        py: Python<'_>,
        path: PathBuf,
    ) -> (Vec<(String, String)>, Option<String>) {
        let read = py.detach(|| state::read_hash_cache(&path));
        (read.entries.into_items(), read.error)
    }

    /// The folder times at `path` as `_load_dir_mtimes` reads them.
    fn read_dir_mtimes(&self, py: Python<'_>, path: PathBuf) -> Vec<(String, f64)> {
        py.detach(|| state::read_dir_mtimes(&path)).into_items()
    }

    /// `json.dumps(dict(entries), indent=2)`, as the frame writes the cache.
    fn hash_cache_text(&self, entries: Vec<(String, String)>) -> String {
        state::hash_cache_text(&entries.into_iter().collect())
    }

    /// `json.dumps(dict(entries))`, as the frame writes the folder times.
    fn dir_mtimes_text(&self, entries: Vec<(String, f64)>) -> String {
        state::dir_mtimes_text(&entries.into_iter().collect())
    }

    /// `_publish_hash_cache(path, dict(entries), stamp)`: written to a
    /// temporary file, stamped, forced to disk and renamed. Raises OSError,
    /// the previous cache left as it was.
    fn publish_hash_cache(
        &self,
        py: Python<'_>,
        path: PathBuf,
        entries: Vec<(String, String)>,
        stamp: f64,
    ) -> PyResult<()> {
        let entries: Entries<String> = entries.into_iter().collect();
        Ok(py.detach(|| state::publish_hash_cache(&path, &entries, stamp))?)
    }

    /// `_save_dir_mtimes(path, dict(entries))`. Raises OSError.
    fn save_dir_mtimes(
        &self,
        py: Python<'_>,
        path: PathBuf,
        entries: Vec<(String, f64)>,
    ) -> PyResult<()> {
        let entries: Entries<f64> = entries.into_iter().collect();
        Ok(py.detach(|| state::save_dir_mtimes(&path, &entries))?)
    }

    /// `_is_already_in_sync`'s file half for the repository whose hash
    /// cache and folder times are at these paths, under these sets;
    /// `discard_cache` for a run that ignores the cache. Raises
    /// `ValueError` where Python raises on a pattern pathspec refuses. The
    /// interpreter lock is released meanwhile.
    #[pyo3(signature = (repo, cache_path, dir_mtimes_path, discard_cache, exclude = None, unignore = None))]
    #[allow(clippy::too_many_arguments)]
    fn in_sync<'py>(
        &self,
        py: Python<'py>,
        repo: PathBuf,
        cache_path: PathBuf,
        dir_mtimes_path: PathBuf,
        discard_cache: bool,
        exclude: Option<&Bound<'py, PyAny>>,
        unignore: Option<&Bound<'py, PyAny>>,
    ) -> PyResult<bool> {
        let rules = ignore::IgnoreRules::new(strings(exclude)?, strings(unignore)?);
        let inputs = state::SyncInputs {
            repo: repo.as_os_str(),
            cache_path: &cache_path,
            dir_mtimes_path: &dir_mtimes_path,
            discard_cache,
        };
        py.detach(|| self.engine.in_sync(&inputs, &rules))
            .map_err(value_error)
    }

    /// A run's baseline, read as `_hash_baseline` reads it, before the run
    /// writes its placeholder cache: the hash cache at `cache_path` unless
    /// the run is forced (`force`), ignores it (`cache_dead`) or the cache
    /// is another project's (`project_mismatch`), less `stale_keys`, every
    /// file forced when `parser_changed`.
    #[allow(clippy::too_many_arguments)]
    fn change_baseline(
        &self,
        py: Python<'_>,
        cache_path: PathBuf,
        force: bool,
        single: bool,
        cache_dead: bool,
        project_mismatch: bool,
        parser_changed: bool,
        stale_keys: &Bound<'_, PyAny>,
    ) -> PyResult<PyChangeBaseline> {
        let stale: BTreeSet<String> = strings(Some(stale_keys))?.into_iter().collect();
        let baseline = py.detach(|| {
            state::baseline(state::BaselineInputs {
                cache_path: &cache_path,
                force,
                single,
                cache_dead,
                project_mismatch,
                parser_changed,
                stale,
            })
        });
        Ok(PyChangeBaseline { baseline })
    }

    /// A run's change detection from `baseline`: the repository walked under
    /// these sets, or for a single-file run its target (`single_key` None
    /// when the rules leave it out), with the graph's module paths when the
    /// run asked the graph (None when it could not answer): what
    /// `engine_frame.FileChanges` holds but the state folder, in its order,
    /// the cache to publish and the folder times as the text the frame
    /// would write. Raises
    /// `ValueError` where Python's walk raises on a pattern. The
    /// interpreter lock is released for the walk and the hashing.
    #[pyo3(signature = (baseline, repo, exclude = None, unignore = None, single_path = None, single_key = None, graph_paths = None))]
    #[allow(clippy::too_many_arguments)]
    fn changes<'py>(
        &self,
        py: Python<'py>,
        baseline: &PyChangeBaseline,
        repo: PathBuf,
        exclude: Option<&Bound<'py, PyAny>>,
        unignore: Option<&Bound<'py, PyAny>>,
        single_path: Option<PathBuf>,
        single_key: Option<String>,
        graph_paths: Option<&Bound<'py, PyAny>>,
    ) -> PyResult<Bound<'py, PyTuple>> {
        let rules = ignore::IgnoreRules::new(strings(exclude)?, strings(unignore)?);
        let graph_paths: Option<HashSet<String>> = match graph_paths {
            Some(paths) if !paths.is_none() => Some(strings(Some(paths))?.into_iter().collect()),
            _ => None,
        };
        let inputs = state::ChangeInputs {
            repo: repo.as_os_str(),
            single: single_path.map(|path| state::SingleTarget {
                path,
                key: single_key,
            }),
            graph_paths,
        };
        let changes = py
            .detach(|| self.engine.changes(&baseline.baseline, inputs, &rules))
            .map_err(value_error)?;
        let baseline = &baseline.baseline;
        let fields: [Bound<'py, PyAny>; 15] = [
            baseline.cache_mtime.into_pyobject(py)?.into_any(),
            baseline.full_build.into_pyobject(py)?.to_owned().into_any(),
            sorted(py, &baseline.forced)?,
            PyList::new(py, changes.hashes.iter())?.into_any(),
            sorted(py, &changes.unreadable)?,
            sorted(py, &changes.current)?,
            PyList::new(py, changes.changed)?.into_any(),
            changes.gone.into_pyobject(py)?.into_any(),
            PyList::new(py, changes.deleted_first)?.into_any(),
            sorted(py, &changes.deleted)?,
            changes
                .merge_base
                .map(Entries::into_items)
                .into_pyobject(py)?
                .into_any(),
            changes.stamp.into_pyobject(py)?.into_any(),
            changes
                .mtimes
                .as_ref()
                .map(|mtimes| mtimes.iter().map(|(k, &v)| (k, v)).collect::<Vec<_>>())
                .into_pyobject(py)?
                .into_any(),
            changes
                .prepared
                .as_ref()
                .map(state::hash_cache_text)
                .into_pyobject(py)?
                .into_any(),
            changes
                .mtimes
                .as_ref()
                .map(state::dir_mtimes_text)
                .into_pyobject(py)?
                .into_any(),
        ];
        PyTuple::new(py, fields)
    }

    /// The graph schema's names the frame's rows carry, as it holds them:
    /// `cs.NodeLabel`'s, the property keys and `cs.RelationshipType`'s.
    #[classattr]
    #[pyo3(name = "NODE_LABELS")]
    fn node_labels() -> Vec<&'static str> {
        rows::NODE_LABELS.to_vec()
    }

    #[classattr]
    #[pyo3(name = "PROPERTY_KEYS")]
    fn property_keys() -> Vec<&'static str> {
        rows::PROPERTY_KEYS.to_vec()
    }

    #[classattr]
    #[pyo3(name = "RELATIONSHIP_TYPES")]
    fn relationship_types() -> Vec<&'static str> {
        rows::RELATIONSHIP_TYPES.to_vec()
    }

    /// The structure pass over the repository at `root`, for the project
    /// `project_name`, a folder holding a file named as any of `markers` a
    /// package: `(elements, rows)`, the `structural_elements` map as
    /// `(path from the root, qualified name or None)` pairs in Python's
    /// order, and the Package and Folder rows in the order Python writes
    /// them. `case_table`, `str.lower`'s table of each character it
    /// changes as `(character, lowered)` pairs, is given where Python's
    /// paths compare without case, and folders are then compared as their
    /// lowered names. Raises `ValueError` where Python's walk raises on a
    /// pattern. The interpreter lock is released for the walk.
    #[pyo3(signature = (root, project_name, markers, exclude = None, unignore = None, case_table = None))]
    #[allow(clippy::too_many_arguments)]
    fn structure<'py>(
        &self,
        py: Python<'py>,
        root: PathBuf,
        project_name: String,
        markers: &Bound<'py, PyAny>,
        exclude: Option<&Bound<'py, PyAny>>,
        unignore: Option<&Bound<'py, PyAny>>,
        case_table: Option<Vec<(char, String)>>,
    ) -> PyResult<Bound<'py, PyTuple>> {
        let rules = ignore::IgnoreRules::new(strings(exclude)?, strings(unignore)?);
        let markers = strings(Some(markers))?;
        let paths = structure::PathRules {
            case: case_table.map(structure::CaseTable::new),
            ..structure::PathRules::default()
        };
        let found = py
            .detach(|| {
                self.engine
                    .structure(root.as_os_str(), &project_name, &markers, &rules, &paths)
            })
            .map_err(value_error)?;
        let rows = found
            .rows
            .iter()
            .map(|row| row_tuple(py, row))
            .collect::<PyResult<Vec<_>>>()?;
        (found.elements, rows).into_pyobject(py)
    }

    /// The File row of each of `paths`, in order, as `process_generic_file`
    /// writes it for a repository at `root` whose structure map is
    /// `elements`; None for a path not under `root`, or one whose folder
    /// `Path.resolve()` raises on. The running Python's rules: whether a
    /// name ending in a dot has a suffix (`dot_suffix`), whether resolving
    /// a loop of links raises (`loop_raises`), and `case_table`, as
    /// `structure` takes it. The interpreter lock is released meanwhile.
    #[pyo3(signature = (root, project_name, elements, paths, dot_suffix, loop_raises = false, case_table = None))]
    #[allow(clippy::too_many_arguments)]
    fn file_rows<'py>(
        &self,
        py: Python<'py>,
        root: PathBuf,
        project_name: String,
        elements: Vec<(String, Option<String>)>,
        paths: Vec<PathBuf>,
        dot_suffix: bool,
        loop_raises: bool,
        case_table: Option<Vec<(char, String)>>,
    ) -> PyResult<Bound<'py, PyList>> {
        let rules = structure::PathRules {
            case: case_table.map(structure::CaseTable::new),
            dot_suffix,
            loop_raises,
        };
        let found = py.detach(|| {
            self.engine
                .file_rows(&root, &project_name, &elements, &paths, &rules)
        });
        let rows = found
            .iter()
            .map(|row| row.as_ref().map(|row| row_tuple(py, row)).transpose())
            .collect::<PyResult<Vec<_>>>()?;
        PyList::new(py, rows)
    }

    /// `keys`, folders' paths from `root` as `as_posix()` spells them (`.`
    /// for the root), in the order the structure pass writes them, compared
    /// as `case_table` says (see `structure`). The order alone, for a test
    /// to hold to `sorted()` over each flavour of path on any system.
    #[staticmethod]
    #[pyo3(signature = (root, keys, case_table = None))]
    fn folder_order(
        root: PathBuf,
        keys: Vec<String>,
        case_table: Option<Vec<(char, String)>>,
    ) -> Vec<String> {
        let case = case_table.map(structure::CaseTable::new);
        super::Engine::folder_order(&root, &keys, case.as_ref())
            .into_iter()
            .map(str::to_owned)
            .collect()
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

/// A row as it crosses to Python (see the module's doc).
fn row_tuple<'py>(py: Python<'py>, row: &ContainedRow) -> PyResult<Bound<'py, PyTuple>> {
    let identifier = |node: &NodeId| (node.label, node.key, node.value.clone());
    let properties = PyList::new(
        py,
        row.node
            .properties
            .iter()
            .map(|(key, value)| (*key, value.as_os_str())),
    )?;
    (
        row.node.label,
        properties,
        identifier(&row.relationship.start),
        row.relationship.rel_type,
        identifier(&row.relationship.end),
    )
        .into_pyobject(py)
}

/// A run's change-detection baseline (`engine::state::Baseline`), read
/// before the run writes its placeholder cache and handed back to
/// `Engine.changes` once the run's own scan is done.
#[pyclass(frozen, name = "ChangeBaseline", module = "cgr_graph_rs")]
pub struct PyChangeBaseline {
    baseline: state::Baseline,
}

#[pymethods]
impl PyChangeBaseline {
    /// The cache's mtime the run's files are compared against.
    #[getter]
    fn cache_mtime(&self) -> f64 {
        self.baseline.cache_mtime
    }

    /// Whether the run re-parses every file as a first build.
    #[getter]
    fn full_build(&self) -> bool {
        self.baseline.full_build
    }
}

fn sorted<'py>(py: Python<'py>, keys: &BTreeSet<String>) -> PyResult<Bound<'py, PyAny>> {
    Ok(PyList::new(py, keys)?.into_any())
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
    m.add_class::<PyIgnoreRules>()?;
    m.add_class::<PyChangeBaseline>()
}
