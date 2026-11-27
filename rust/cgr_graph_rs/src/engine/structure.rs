//! The structure pass and the File rows (docs/reports/PHASE2_FRAME.md,
//! section 7): the Package and Folder rows, the `structural_elements` map
//! that the rest of the index reads a folder's package from, and each
//! file's File row. A copy of `codebase_rag/parsers/structure_processor.py`
//! (`identify_structure()` over the whole repository, and
//! `process_generic_file`), rule for rule:
//!
//! - **The folders** are the structure pass's own walk, not the file
//!   plan's: `os.walk` from the root, a subfolder kept unless it is a link,
//!   its name is not UTF-8, it resolves outside the root, or
//!   `should_skip_path(..., is_file=False)` skips it. Those rules are not
//!   `should_keep_dir`'s, so the two walks can keep different folders. A
//!   kept folder that cannot be listed is still a folder, with nothing in
//!   it, as `os.walk` lists it in its parent and then skips it. The root is
//!   always one.
//! - **Order.** Python's path order, `sorted()` over the folders' paths:
//!   part by part, so `a/b` comes before `a-b`, each part by code point,
//!   and where paths compare without case (Windows) with every part
//!   lower-cased first, as `str.lower` lowers it. The root comes first,
//!   unless it was given as the current folder, `.`, which sorts after a
//!   folder whose name is below `.`, such as `-x`.
//! - **Kinds.** A folder holding a file or folder named as any of the
//!   package markers is a package; a marker is a name, never a pattern, so
//!   `*.vcxproj` is matched literally. Its qualified name is the project's
//!   and its parts joined by dots, the root's the project's own. Any other
//!   folder maps to none, and the root gets no Folder row. The markers are
//!   the language specs' (`LanguageSpec.package_indicators`), which Python
//!   passes in.
//! - **What contains a row**: the Project for the root and for anything at
//!   the root, else the parent's Package when the map names one for it,
//!   else its Folder, by its resolved path.
//! - **File rows** for the files a run indexes: the path from the root,
//!   the name, the extension as `PurePath.suffix` gives it, and the
//!   resolved folder joined with the name, so a link is named by itself,
//!   not its target. The map they read is passed in: the frame's own map in
//!   a run, Python's when a test holds one row to Python's. A file whose
//!   folder resolves into a loop of links has none where the running
//!   Python's `Path.resolve()` raises on one (before 3.13).
//!
//! Where Python's paths differ by version or system, `PathRules` says how
//! the running one behaves; Python fills it in.
//!
//! Paths are spelled as `Path.as_posix()` spells them: `/` between parts on
//! every system. Folders are listed on the engine's pool a level at a time,
//! and their markers checked on it, so the result is the same on any
//! number of threads.

use std::collections::HashMap;
use std::ffi::{OsStr, OsString};
use std::fs;
use std::path::{Component, Path, PathBuf};

use super::discover::{child_dirpath, list, ROOT_DIR_KEY};
use super::ignore::{IgnoreRules, PatternError};
use super::rows::{
    ContainedRow, NodeId, NodeRow, ABSOLUTE_PATH, CONTAINS_FILE, CONTAINS_FOLDER, CONTAINS_PACKAGE,
    EXTENSION, FILE, FOLDER, NAME, PACKAGE, PATH, PROJECT, QUALIFIED_NAME,
};
use super::state::resolve;
use super::Engine;

#[cfg(test)]
mod tests;

/// Between the parts of a path from the root (`as_posix()`).
const SLASH: &str = "/";
/// Between a qualified name's parts (`cs.SEPARATOR_DOT`).
const DOT: &str = ".";
/// `Path("..").name`: the name pathlib gives a path ending in `..`.
const PARENT_NAME: &str = "..";

/// How the running Python's paths behave where versions and systems
/// differ: whether they compare names without case, and then lower-case
/// them by `case`, this Python's own `str.lower` (Windows); whether a name
/// ending in a dot has a suffix (`PurePath("a.").suffix`, `.` from 3.14
/// on); and whether `Path.resolve()` raises on a loop of links (before
/// 3.13).
#[derive(Debug, Default)]
pub struct PathRules {
    pub case: Option<CaseTable>,
    pub dot_suffix: bool,
    pub loop_raises: bool,
}

/// `str.lower` as the running Python does it. Each character takes
/// Python's own lower case, which follows the Unicode version Python was
/// built with, not Rust's: Rust's is newer, and lowers letters Python's
/// does not. A capital sigma is the one character lowered by its context,
/// to the final form at the end of a word; Rust decides that by Python's
/// rule, so the two part only where a letter beside it has a case one of
/// their Unicode versions lacks.
#[derive(Debug, Default)]
pub struct CaseTable {
    lower: HashMap<char, String>,
}

/// The one character `str.lower` lowers by its context, and its form
/// inside a word.
const CAPITAL_SIGMA: char = '\u{3a3}';
const SMALL_SIGMA: char = '\u{3c3}';

impl CaseTable {
    /// The table of each character that `str.lower` changes, and what to.
    pub fn new<I: IntoIterator<Item = (char, String)>>(lower: I) -> CaseTable {
        CaseTable {
            lower: lower.into_iter().collect(),
        }
    }

    /// `text.lower()`.
    pub fn lower(&self, text: &str) -> String {
        if text.is_ascii() {
            return text.to_ascii_lowercase();
        }
        // Rust's own lowering, only to read each sigma's form off it: it
        // lowers every other character on its own, so it is followed
        // character by character alongside.
        let native: Vec<char> = if text.contains(CAPITAL_SIGMA) {
            text.to_lowercase().chars().collect()
        } else {
            Vec::new()
        };
        let mut at = 0;
        let mut lowered = String::with_capacity(text.len());
        for c in text.chars() {
            if c == CAPITAL_SIGMA {
                lowered.push(native.get(at).copied().unwrap_or(SMALL_SIGMA));
                at += 1;
                continue;
            }
            at += c.to_lowercase().count();
            match self.lower.get(&c) {
                Some(lower) => lowered.push_str(lower),
                None => lowered.push(c),
            }
        }
        lowered
    }
}

/// What the structure pass derives for a repository: the map, each
/// folder's path from the root (`.` for the root) and its package's
/// qualified name, or none for a plain folder, in the order Python fills
/// it; and the rows it writes, a node and its containment edge each, in
/// the order it writes them.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct Structure {
    pub elements: Vec<(String, Option<String>)>,
    pub rows: Vec<ContainedRow>,
}

/// One folder the walk keeps: its names from the root, its path as
/// `os.walk` joins it, and that path resolved.
struct Folder {
    parts: Vec<String>,
    path: OsString,
    resolved: PathBuf,
}

impl Engine {
    /// The structure of the repository at `root` under `rules`, the
    /// project named `project_name`, a folder holding any of `markers` a
    /// package, its folders compared as `paths` compares them; an error
    /// where Python's walk raises on a pattern pathspec refuses.
    pub fn structure(
        &self,
        root: &OsStr,
        project_name: &str,
        markers: &[String],
        rules: &IgnoreRules,
        paths: &PathRules,
    ) -> Result<Structure, PatternError> {
        let mut folders = self.folders(root, rules)?;
        let dot_root = lexical_parts(Path::new(root)).is_empty();
        let case = paths.case.as_ref();
        folders.sort_by_cached_key(|folder| order_key(&folder.parts, dot_root, case));
        let packages = self.map_ordered(&folders, |folder| is_package(&folder.path, markers));
        Ok(derive(
            Path::new(root),
            project_name,
            &folders,
            &packages,
            case,
        ))
    }

    /// Each file's File row and its containing edge, in the order given;
    /// `None` for a path that is not under `root`, or whose path from it
    /// is not UTF-8, or whose folder `Path.resolve()` raises on. `elements`
    /// is the structure map the rows read their folder's package from.
    pub fn file_rows<P: AsRef<Path> + Sync>(
        &self,
        root: &Path,
        project_name: &str,
        elements: &[(String, Option<String>)],
        paths: &[P],
        rules: &PathRules,
    ) -> Vec<Option<ContainedRow>> {
        let case = rules.case.as_ref();
        let map: HashMap<String, Option<&str>> = elements
            .iter()
            .map(|(key, qn)| (fold(key, case), qn.as_deref()))
            .collect();
        let root_parts = lexical_parts(root);
        // Each folder resolved once, as Python's resolution is cached.
        let mut index: HashMap<&Path, usize> = HashMap::new();
        let mut parents: Vec<&Path> = Vec::new();
        let slots: Vec<usize> = paths
            .iter()
            .map(|path| {
                let parent = path.as_ref().parent().unwrap_or(Path::new(""));
                *index.entry(parent).or_insert_with(|| {
                    parents.push(parent);
                    parents.len() - 1
                })
            })
            .collect();
        let resolved = self.map_ordered(&parents, |parent| {
            let resolved = resolve(parent);
            // `Path.resolve()` before 3.13 stats what it resolved, and
            // raises on a loop of links, which ends that file's row.
            (!(rules.loop_raises && meets_loop(&resolved))).then_some(resolved)
        });
        let place = FilePlace {
            root_parts: &root_parts,
            project_name,
            map: &map,
            rules,
        };
        paths
            .iter()
            .zip(slots)
            .map(|(path, slot)| place.row(path.as_ref(), resolved[slot].as_deref()?))
            .collect()
    }

    /// `keys`, folders' paths from `root` (`.` for the root itself), in
    /// the order the structure pass writes them, compared as `case` says.
    pub fn folder_order<'a>(
        root: &Path,
        keys: &'a [String],
        case: Option<&CaseTable>,
    ) -> Vec<&'a str> {
        let dot_root = lexical_parts(root).is_empty();
        let mut ordered: Vec<(Vec<String>, &str)> = keys
            .iter()
            .map(|key| {
                let parts: Vec<String> = if key == ROOT_DIR_KEY {
                    Vec::new()
                } else {
                    key.split(SLASH).map(str::to_owned).collect()
                };
                (order_key(&parts, dot_root, case), key.as_str())
            })
            .collect();
        ordered.sort_by(|a, b| a.0.cmp(&b.0));
        ordered.into_iter().map(|(_, key)| key).collect()
    }

    /// The root and every folder the structure pass keeps under it, the
    /// root first, then a level of the tree at a time.
    fn folders(&self, root: &OsStr, rules: &IgnoreRules) -> Result<Vec<Folder>, PatternError> {
        let resolved_root = resolve(Path::new(root));
        let mut folders = vec![Folder {
            parts: Vec::new(),
            path: root.to_owned(),
            resolved: resolved_root.clone(),
        }];
        let mut level = vec![0];
        while !level.is_empty() {
            let found: Vec<Result<Vec<Folder>, PatternError>> = self.map_ordered(&level, |&id| {
                subfolders(rules, &folders[id], &resolved_root)
            });
            let mut next = Vec::new();
            for kept in found {
                for folder in kept? {
                    next.push(folders.len());
                    folders.push(folder);
                }
            }
            level = next;
        }
        Ok(folders)
    }
}

/// The subfolders of `folder` the structure pass keeps, as
/// `_directories_to_derive` filters `os.walk`'s `dirnames`.
fn subfolders(
    rules: &IgnoreRules,
    folder: &Folder,
    resolved_root: &Path,
) -> Result<Vec<Folder>, PatternError> {
    let Some(entries) = list(&folder.path, resolved_root) else {
        return Ok(Vec::new());
    };
    let mut kept = Vec::new();
    for entry in entries {
        if !entry.is_dir || entry.is_link() {
            continue;
        }
        let Some(name) = entry.name.to_str() else {
            continue;
        };
        let path = child_dirpath(&folder.path, name);
        let resolved = resolve(Path::new(&path));
        // `should_skip_path`'s first check, ahead of the rules: a folder
        // that resolves outside the root (a Windows junction) is left out.
        if !resolved.starts_with(resolved_root) {
            continue;
        }
        let mut parts = folder.parts.clone();
        parts.push(name.to_owned());
        if rules.should_skip_rel_path(&parts.join(SLASH), false)? {
            continue;
        }
        kept.push(Folder {
            parts,
            path,
            resolved,
        });
    }
    Ok(kept)
}

/// The rows and map for the folders in Python's order, `packages` saying
/// which are packages.
fn derive(
    root: &Path,
    project_name: &str,
    folders: &[Folder],
    packages: &[bool],
    case: Option<&CaseTable>,
) -> Structure {
    let mut structure = Structure::default();
    // Each folder's map entry and resolved path, for its subfolders' rows.
    let mut derived: HashMap<String, (Option<String>, OsString)> = HashMap::new();
    for (folder, &is_package) in folders.iter().zip(packages) {
        let key = folder_key(&folder.parts);
        let absolute = posix(&folder.resolved);
        let parent = match folder.parts.split_last() {
            Some((_, parent)) if !parent.is_empty() => {
                let found = derived.get(&fold(&parent.join(SLASH), case));
                match found {
                    Some((Some(qn), _)) if !qn.is_empty() => package_id(qn),
                    Some((_, resolved)) => folder_id(resolved.clone()),
                    None => folder_id(posix(&resolve(&root.join(parent.join(SLASH))))),
                }
            }
            _ => project_id(project_name),
        };
        let name = match folder.parts.last() {
            Some(name) => OsString::from(name),
            None => python_name(root),
        };
        let entry = if is_package {
            let qn = qualified_name(project_name, &folder.parts);
            let node = NodeRow {
                label: PACKAGE,
                properties: vec![
                    (QUALIFIED_NAME, OsString::from(&qn)),
                    (NAME, name),
                    (PATH, OsString::from(&key)),
                    (ABSOLUTE_PATH, absolute.clone()),
                ],
            };
            structure.rows.push(ContainedRow::new(
                node,
                parent,
                CONTAINS_PACKAGE,
                QUALIFIED_NAME,
            ));
            Some(qn)
        } else {
            if !folder.parts.is_empty() {
                let node = NodeRow {
                    label: FOLDER,
                    properties: vec![
                        (PATH, OsString::from(&key)),
                        (NAME, name),
                        (ABSOLUTE_PATH, absolute.clone()),
                    ],
                };
                structure.rows.push(ContainedRow::new(
                    node,
                    parent,
                    CONTAINS_FOLDER,
                    ABSOLUTE_PATH,
                ));
            }
            None
        };
        structure.elements.push((key.clone(), entry.clone()));
        derived.insert(fold(&key, case), (entry, absolute));
    }
    structure
}

/// Where the File rows are placed: the root's parts, the project, the map
/// and the running Python's path rules.
struct FilePlace<'a> {
    root_parts: &'a [Part],
    project_name: &'a str,
    map: &'a HashMap<String, Option<&'a str>>,
    rules: &'a PathRules,
}

impl FilePlace<'_> {
    /// `process_generic_file(path, path.name)`'s rows, its folder resolved
    /// as `resolved_parent`.
    fn row(&self, path: &Path, resolved_parent: &Path) -> Option<ContainedRow> {
        let parts = lexical_parts(path);
        // `relative_to`: the root's parts, anchor and all, then names only.
        let under = parts.len() > self.root_parts.len()
            && parts.iter().zip(self.root_parts).all(|(part, root)| {
                let case = self.rules.case.as_ref();
                fold_os(&part.text, case) == fold_os(&root.text, case)
            })
            && !parts[self.root_parts.len()..]
                .iter()
                .any(|part| part.anchor);
        if !under {
            return None;
        }
        let rel: Vec<&str> = parts[self.root_parts.len()..]
            .iter()
            .map(|part| part.text.to_str())
            .collect::<Option<_>>()?;
        let (&name, folder) = rel.split_last()?;
        let parent = if folder.is_empty() {
            project_id(self.project_name)
        } else {
            match self
                .map
                .get(&fold(&folder.join(SLASH), self.rules.case.as_ref()))
            {
                Some(Some(qn)) if !qn.is_empty() => package_id(qn),
                _ => folder_id(posix(resolved_parent)),
            }
        };
        let node = NodeRow {
            label: FILE,
            properties: vec![
                (PATH, OsString::from(rel.join(SLASH))),
                (NAME, OsString::from(name)),
                (
                    EXTENSION,
                    OsString::from(suffix(name, self.rules.dot_suffix)),
                ),
                (ABSOLUTE_PATH, posix(&resolved_parent.join(name))),
            ],
        };
        Some(ContainedRow::new(
            node,
            parent,
            CONTAINS_FILE,
            ABSOLUTE_PATH,
        ))
    }
}

/// `PurePath(name).suffix`: before Python 3.14 the last dot's part unless
/// the dot starts or ends the name; from 3.14 (`dot_suffix`) the last
/// dot's part once the leading dots are set aside, a trailing dot too.
pub fn suffix(name: &str, dot_suffix: bool) -> &str {
    if dot_suffix {
        let stripped = name.trim_start_matches('.');
        return stripped.rfind('.').map_or("", |i| &stripped[i..]);
    }
    match name.rfind('.') {
        Some(i) if i > 0 && i + 1 < name.len() => &name[i..],
        _ => "",
    }
}

/// A folder's key in the map: its path from the root, `.` for the root.
fn folder_key(parts: &[String]) -> String {
    if parts.is_empty() {
        ROOT_DIR_KEY.to_owned()
    } else {
        parts.join(SLASH)
    }
}

/// The project's name and the folder's parts, joined by dots.
fn qualified_name(project_name: &str, parts: &[String]) -> String {
    let mut qn = project_name.to_owned();
    for part in parts {
        qn.push_str(DOT);
        qn.push_str(part);
    }
    qn
}

fn project_id(project_name: &str) -> NodeId {
    NodeId {
        label: PROJECT,
        key: NAME,
        value: OsString::from(project_name),
    }
}

fn package_id(qn: &str) -> NodeId {
    NodeId {
        label: PACKAGE,
        key: QUALIFIED_NAME,
        value: OsString::from(qn),
    }
}

fn folder_id(absolute: OsString) -> NodeId {
    NodeId {
        label: FOLDER,
        key: ABSOLUTE_PATH,
        value: absolute,
    }
}

/// Whether the folder holds a file or folder of any marker's name, links
/// followed, as `(root / marker).exists()` asks.
fn is_package(path: &OsStr, markers: &[String]) -> bool {
    markers
        .iter()
        .any(|marker| fs::metadata(Path::new(path).join(marker)).is_ok())
}

/// How `sorted()` orders folders: their paths' parts compared in turn,
/// each by code point, which for UTF-8 is byte order, lower-cased first
/// where `case` says, as `PureWindowsPath` compares (`str(path).lower()`,
/// split at each separator, which no lower-casing crosses). Every
/// folder's path starts with the root's, so the root's own parts can be
/// left out, unless the root is the current folder (`dot_root`):
/// `Path(".")` compares as `.`, and its folders' paths hold no `.`, so a
/// name below `.` sorts before the root.
fn order_key(parts: &[String], dot_root: bool, case: Option<&CaseTable>) -> Vec<String> {
    if parts.is_empty() && dot_root {
        return vec![DOT.to_owned()];
    }
    parts.iter().map(|part| fold(part, case)).collect()
}

/// A path's spelling as Python's paths compare it: lower-cased where they
/// compare without case.
fn fold(text: &str, case: Option<&CaseTable>) -> String {
    match case {
        Some(case) => case.lower(text),
        None => text.to_owned(),
    }
}

fn fold_os(part: &OsStr, case: Option<&CaseTable>) -> OsString {
    match case {
        Some(case) => OsString::from(case.lower(&part.to_string_lossy())),
        None => part.to_owned(),
    }
}

/// Whether reading `path` meets a loop of links: what `Path.resolve()`
/// before 3.13 raises on, by the errors it checks for.
fn meets_loop(path: &Path) -> bool {
    #[cfg(unix)]
    const LOOP: i32 = libc::ELOOP;
    // `ERROR_CANT_RESOLVE_FILENAME`, pathlib's `_WINERROR_CANT_RESOLVE_FILENAME`.
    #[cfg(windows)]
    const LOOP: i32 = 1921;
    #[cfg(not(any(unix, windows)))]
    const LOOP: i32 = -1;
    fs::metadata(path).is_err_and(|error| error.raw_os_error() == Some(LOOP))
}

/// One part of a path as pathlib holds it; an anchor is the root or a
/// Windows drive, which only an absolute path has.
struct Part {
    anchor: bool,
    text: OsString,
}

/// A path's parts as pathlib holds them: `.` parts dropped, the root and a
/// Windows drive each a part of their own.
fn lexical_parts(path: &Path) -> Vec<Part> {
    path.components()
        .filter_map(|component| match component {
            Component::CurDir => None,
            other => Some(Part {
                anchor: matches!(other, Component::RootDir | Component::Prefix(_)),
                text: other.as_os_str().to_owned(),
            }),
        })
        .collect()
}

/// `Path(path).name`: its last part, `..` included, and nothing for a root
/// or a path of `.` alone.
fn python_name(path: &Path) -> OsString {
    match path.components().next_back() {
        Some(Component::Normal(name)) => name.to_owned(),
        Some(Component::ParentDir) => OsString::from(PARENT_NAME),
        _ => OsString::new(),
    }
}

/// `Path.as_posix()`: the path with Windows' separators made `/`.
#[cfg(windows)]
fn posix(path: &Path) -> OsString {
    use std::os::windows::ffi::{OsStrExt, OsStringExt};

    const BACKSLASH: u16 = b'\\' as u16;
    const FORWARD: u16 = b'/' as u16;
    let wide: Vec<u16> = path
        .as_os_str()
        .encode_wide()
        .map(|unit| if unit == BACKSLASH { FORWARD } else { unit })
        .collect();
    OsString::from_wide(&wide)
}

#[cfg(not(windows))]
fn posix(path: &Path) -> OsString {
    path.as_os_str().to_owned()
}
