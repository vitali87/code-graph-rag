//! The merge (docs/reports/PHASE2_FRAME.md, section 10): each file a
//! language engine reads hands the frame a summary, and the frame folds the
//! summaries into the project's index. The fold takes them in walk order,
//! each file's place in the run's file plan, whatever order they arrive in,
//! and never in sorted path order: Python registers files in walk order,
//! and the order decides which of two files claims a module's name (#1569).
//!
//! Phase 2 fills one section, the module names: which file claims each
//! module's bare name and which keep their extension, a copy of the
//! definition processor's (`base_module_qn` and `_disambiguate_module_qn`
//! in `codebase_rag/parsers/definition_processor.py`), rule for rule:
//!
//! - **From the path**: the project's name, the file's folders and its stem
//!   with the module extension removed, a compound one (`.d.ts`) whole,
//!   joined by dots. A package's `__init__.py` and `__init__.pyi`, and a
//!   Rust `mod.rs`, take their folder's name.
//! - **A notebook** keeps its extension (`proj.analysis.ipynb`, #2480).
//! - **A same-stem file of another language family on disk** makes both
//!   keep their extensions (#2586).
//! - **Otherwise a declaration** (`.d.ts`) **beside its implementation**
//!   keeps its declaration extension, so the implementation takes the bare
//!   name (#1720).
//! - **Otherwise the first file in walk order claims the name**, and a later
//!   one gets its last suffix appended (#1569), over whatever holds that
//!   name in turn, as Python's map is written.
//!
//! "On disk" is Python's question, asked the same way: a sibling counts
//! when the folder's listing holds its exact name, it is a regular file and
//! no link, and the ignore rules would not skip it
//! (`IgnoreRules::should_skip_rel_path`, the walk's own predicate). It is
//! not asked of the file plan: an implementation is found by name, so on a
//! file system that ignores case `Foo.ts` finds `foo.ts`, as Python's
//! `is_file` does, and a single-file run, whose plan holds one file, still
//! sees its siblings.
//!
//! The language tables (each extension's language, the families, the
//! module extensions, the names a folder's file takes) are the language
//! specs' and `codebase_rag/constants`', passed in as `NamingRules`, not
//! copied here.
//!
//! The folders are listed and each file's candidate name derived on the
//! engine's pool; only the claims are folded in order, so the result is the
//! same on any number of threads.

use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};
use std::ffi::OsString;
use std::fs;
use std::path::{Path, PathBuf};

use super::ignore::{IgnoreRules, PatternError};
use super::state::resolve;
use super::structure::suffix;
use super::Engine;

#[cfg(test)]
mod tests;

/// Between a qualified name's parts (`cs.SEPARATOR_DOT`), and before an
/// extension.
const DOT: char = '.';
/// Between the parts of a path from the root.
const SLASH: &str = "/";

/// A section no language fills yet: nothing can be put in it. Each
/// summary's and the index's other sections hold this until a language's
/// analysis moves to Rust and gives the section its own entries.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Unfilled {}

/// What a language engine hands the frame for one file it read.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FileSummary {
    /// The file's place in the run's file plan, the order the fold takes.
    pub walk_index: usize,
    /// Its path from the root, `/` between parts, as the plan names it.
    pub path: String,
    /// The language it was read as (`cs.SupportedLanguage`'s value); its
    /// family is the naming rules'.
    pub language: String,
    /// The function registry's entries: each function, method and class
    /// the file defines, its label and where. Fills when the first
    /// language's definitions move (Go, phase 3): the first registration
    /// takes the plain name, later ones `@line`, then `@line_col`, across
    /// files with an `_10000·k` slot; the label is last-wins, except
    /// TypeScript's type-and-value twins.
    pub definitions: Vec<Unfilled>,
    /// Each class's parents, in declaration order. Fills with the
    /// definitions: the last assignment wins, except a TypeScript class
    /// and interface of one name, whose parents append.
    pub parents: Vec<Unfilled>,
    /// The file's imports, by local name. Fills when a language's imports
    /// move: last-wins per module and local name; the Scala and Rust
    /// cross-file passes are their languages' own.
    pub imports: Vec<Unfilled>,
    /// Where each type is defined. Fills with the definitions: the first
    /// claim per module, line and column holds.
    pub type_locations: Vec<Unfilled>,
}

impl FileSummary {
    /// A summary of the file at `path`, `walk_index` in the plan, read as
    /// `language`, with nothing in the sections no language fills yet.
    pub fn new(walk_index: usize, path: String, language: String) -> FileSummary {
        FileSummary {
            walk_index,
            path,
            language,
            definitions: Vec::new(),
            parents: Vec::new(),
            imports: Vec::new(),
            type_locations: Vec::new(),
        }
    }
}

/// A module the index holds: the file that claimed its name, and the
/// language that file was read as.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Module {
    pub path: String,
    pub language: String,
}

/// The project's index, folded from its files' summaries: the modules by
/// qualified name (`module_qn_to_file_path`), and the sections that fill
/// as languages move, each its summaries' section folded by the rule its
/// field there names.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct ProjectIndex {
    pub modules: BTreeMap<String, Module>,
    pub definitions: Vec<Unfilled>,
    pub parents: Vec<Unfilled>,
    pub imports: Vec<Unfilled>,
    pub type_locations: Vec<Unfilled>,
}

/// The language tables a module's name is derived by, as Python passes
/// them in.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct NamingRules {
    /// Each extension's language (`get_language_for_extension`).
    languages: HashMap<String, String>,
    /// The extensions a module's stem drops whole, longest first
    /// (`JS_TS_MODULE_EXTENSIONS`).
    module_extensions: Vec<String>,
    /// Those of them that mark a declaration (`.d.ts`), in that order.
    declaration_extensions: Vec<String>,
    /// The rest: the implementations a declaration yields to.
    implementation_extensions: Vec<String>,
    /// The files that take their folder's name (`__init__.py`,
    /// `__init__.pyi`, `mod.rs`).
    package_files: HashSet<String>,
    /// A notebook's suffix (`.ipynb`).
    notebook_suffix: String,
    /// For each language, every extension a same-stem module of another
    /// family can carry, compound ones included, sorted.
    other_family_extensions: HashMap<String, Vec<String>>,
}

impl NamingRules {
    /// The rules from the tables: each `(extension, language)`, the
    /// families (a language in none is a family of its own), the module
    /// extensions, the prefix that marks a declaration among them, the
    /// files named after their folder, and a notebook's suffix.
    pub fn new(
        languages: Vec<(String, String)>,
        families: &[Vec<String>],
        module_extensions: Vec<String>,
        declaration_prefix: &str,
        package_files: Vec<String>,
        notebook_suffix: String,
    ) -> NamingRules {
        let languages: HashMap<String, String> = languages.into_iter().collect();
        let mut module_extensions = module_extensions;
        // Stable, as `sorted(key=len, reverse=True)` is: two extensions of
        // one length never both end a name, so their order is moot anyway.
        module_extensions.sort_by_key(|ext| std::cmp::Reverse(ext.len()));
        let (declaration_extensions, implementation_extensions) = module_extensions
            .iter()
            .cloned()
            .partition(|ext| ext.starts_with(declaration_prefix));
        let other_family_extensions =
            other_family_extensions(&languages, &module_extensions, families);
        NamingRules {
            languages,
            module_extensions,
            declaration_extensions,
            implementation_extensions,
            package_files: package_files.into_iter().collect(),
            notebook_suffix,
            other_family_extensions,
        }
    }

    /// `module_stem(name)`: the name less its module extension, a compound
    /// one whole, or else less its suffix.
    fn module_stem<'a>(&self, name: &'a str, dot_suffix: bool) -> &'a str {
        for ext in &self.module_extensions {
            if name.len() > ext.len() && name.ends_with(ext.as_str()) {
                return &name[..name.len() - ext.len()];
            }
        }
        &name[..name.len() - suffix(name, dot_suffix).len()]
    }

    /// `declaration_extension(name)`.
    fn declaration_extension<'a>(&'a self, name: &str) -> Option<&'a str> {
        self.declaration_extensions
            .iter()
            .find(|ext| name.len() > ext.len() && name.ends_with(ext.as_str()))
            .map(String::as_str)
    }

    /// `base_module_qn`: the project, the folders and the stem, by dots.
    fn base_name(&self, project_name: &str, file: &FilePath<'_>, dot_suffix: bool) -> String {
        let mut qn = project_name.to_owned();
        let stem = if self.package_files.contains(file.name) {
            None
        } else {
            Some(self.module_stem(file.name, dot_suffix))
        };
        for part in file.folders.iter().copied().chain(stem) {
            qn.push(DOT);
            qn.push_str(part);
        }
        qn
    }

    /// `own_extension_module_qn`: `qn` with the name's module extension,
    /// its leading dots gone, as its last part.
    fn own_extension(&self, qn: &str, name: &str, dot_suffix: bool) -> String {
        let extension = &name[self.module_stem(name, dot_suffix).len()..];
        format!("{qn}{DOT}{}", extension.trim_start_matches(DOT))
    }
}

/// For each language of the table, the extensions of every other family:
/// `_OTHER_FAMILY_EXTENSIONS`, each extension's language read off its last
/// suffix (`.d.ts` is TypeScript's).
fn other_family_extensions(
    languages: &HashMap<String, String>,
    module_extensions: &[String],
    families: &[Vec<String>],
) -> HashMap<String, Vec<String>> {
    let extensions: BTreeSet<&str> = languages
        .keys()
        .chain(module_extensions)
        .map(String::as_str)
        .collect();
    let last_suffix_language = |ext: &str| {
        let last = ext.rfind(DOT).map_or(ext, |i| &ext[i..]);
        languages.get(last)
    };
    let mut found = HashMap::new();
    for language in languages.values() {
        if found.contains_key(language) {
            continue;
        }
        let family = families
            .iter()
            .find(|family| family.contains(language))
            .map_or_else(|| vec![language.clone()], Clone::clone);
        let others = extensions
            .iter()
            .filter(|ext| last_suffix_language(ext).is_some_and(|other| !family.contains(other)))
            .map(|ext| (*ext).to_owned())
            .collect();
        found.insert(language.clone(), others);
    }
    found
}

/// A file's path from the root, split: its folders and its name.
struct FilePath<'a> {
    folders: Vec<&'a str>,
    name: &'a str,
}

impl<'a> FilePath<'a> {
    fn new(path: &'a str) -> FilePath<'a> {
        let mut folders: Vec<&str> = path.split(SLASH).collect();
        let name = folders.pop().unwrap_or_default();
        FilePath { folders, name }
    }

    /// The folder's path from the root, empty for the root.
    fn folder(&self) -> String {
        self.folders.join(SLASH)
    }

    /// A file of `name` in the same folder, its path from the root.
    fn sibling(&self, name: &str) -> String {
        if self.folders.is_empty() {
            name.to_owned()
        } else {
            format!("{}{SLASH}{name}", self.folder())
        }
    }
}

/// What a run's module names are derived under: the repository's root as
/// the run names it, the project, the ignore rules, the language tables,
/// and whether the running Python gives a name ending in a dot a suffix
/// (`PurePath("a.").suffix`, `.` from 3.14 on).
pub struct NamingInputs<'a> {
    pub root: &'a Path,
    pub project_name: &'a str,
    pub rules: &'a IgnoreRules,
    pub naming: &'a NamingRules,
    pub dot_suffix: bool,
}

/// The names a folder's listing holds, or none when it cannot be listed.
type Listing = Option<HashSet<OsString>>;

impl Engine {
    /// The project's index folded from `summaries`, in walk order whatever
    /// order they come in; an error where Python raises on a pattern
    /// pathspec refuses.
    pub fn merge(
        &self,
        mut summaries: Vec<FileSummary>,
        inputs: &NamingInputs<'_>,
    ) -> Result<ProjectIndex, PatternError> {
        summaries.sort_by_key(|summary| summary.walk_index);
        let paths: Vec<FilePath<'_>> = summaries
            .iter()
            .map(|summary| FilePath::new(&summary.path))
            .collect();
        // Each folder listed once, as Python's listing is cached.
        let mut folders: Vec<String> = Vec::new();
        let mut slots: HashMap<String, usize> = HashMap::new();
        let folder_of: Vec<usize> = paths
            .iter()
            .map(|path| {
                *slots.entry(path.folder()).or_insert_with_key(|folder| {
                    folders.push(folder.clone());
                    folders.len() - 1
                })
            })
            .collect();
        let listings = self.map_ordered(&folders, |folder| listing(&inputs.root.join(folder)));
        let resolved_root = resolve(inputs.root);
        let namer = Namer {
            inputs,
            resolved_root: &resolved_root,
        };
        let indices: Vec<usize> = (0..paths.len()).collect();
        let candidates = self.map_ordered(&indices, |&i| {
            namer.candidate(&paths[i], &listings[folder_of[i]])
        });
        let mut index = ProjectIndex::default();
        for (summary, candidate) in summaries.into_iter().zip(candidates) {
            let candidate = candidate?;
            let qn = match index.modules.get(&candidate) {
                Some(claimed) if claimed.path != summary.path => {
                    let name = FilePath::new(&summary.path).name;
                    let last = suffix(name, inputs.dot_suffix).trim_start_matches(DOT);
                    format!("{candidate}{DOT}{last}")
                }
                _ => candidate,
            };
            index.modules.insert(
                qn,
                Module {
                    path: summary.path,
                    language: summary.language,
                },
            );
            index.definitions.extend(summary.definitions);
            index.parents.extend(summary.parents);
            index.imports.extend(summary.imports);
            index.type_locations.extend(summary.type_locations);
        }
        Ok(index)
    }
}

/// `os.listdir`'s names, or none when the folder cannot be listed whole.
fn listing(folder: &Path) -> Listing {
    fs::read_dir(folder)
        .ok()?
        .map(|entry| entry.ok().map(|entry| entry.file_name()))
        .collect()
}

/// A file's name before the walk-order claim, derived on the pool.
struct Namer<'a> {
    inputs: &'a NamingInputs<'a>,
    resolved_root: &'a Path,
}

impl Namer<'_> {
    /// The name the file asks for: its path's, then the notebook, sibling
    /// and declaration rules, in `_disambiguate_module_qn`'s order.
    fn candidate(&self, file: &FilePath<'_>, listing: &Listing) -> Result<String, PatternError> {
        let naming = self.inputs.naming;
        let dot_suffix = self.inputs.dot_suffix;
        let mut qn = naming.base_name(self.inputs.project_name, file, dot_suffix);
        if suffix(file.name, dot_suffix) == naming.notebook_suffix {
            qn = naming.own_extension(&qn, file.name, dot_suffix);
        }
        if self.has_other_language_sibling(file, listing)? {
            qn = naming.own_extension(&qn, file.name, dot_suffix);
        } else if let Some(declaration) = naming.declaration_extension(file.name) {
            if self.has_implementation_sibling(file)? {
                qn = format!("{qn}{DOT}{}", declaration.trim_start_matches(DOT));
            }
        }
        Ok(qn)
    }

    /// `has_other_language_sibling`: whether the listing holds a same-stem
    /// module of another family the walk would index.
    fn has_other_language_sibling(
        &self,
        file: &FilePath<'_>,
        listing: &Listing,
    ) -> Result<bool, PatternError> {
        let naming = self.inputs.naming;
        let dot_suffix = self.inputs.dot_suffix;
        let Some(language) = naming.languages.get(suffix(file.name, dot_suffix)) else {
            return Ok(false);
        };
        if naming.package_files.contains(file.name) {
            return Ok(false);
        }
        let Some(names) = listing else {
            return Ok(false);
        };
        let stem = naming.module_stem(file.name, dot_suffix);
        let others = naming.other_family_extensions.get(language);
        for ext in others.into_iter().flatten() {
            let name = format!("{stem}{ext}");
            // `x.d` and `.ts` make `x.d.ts`, the declaration of `x`.
            if !names.contains(&OsString::from(&name))
                || naming.package_files.contains(&name)
                || naming.module_stem(&name, dot_suffix) != stem
            {
                continue;
            }
            if self.indexed(&file.sibling(&name))? {
                return Ok(true);
            }
        }
        Ok(false)
    }

    /// `has_implementation_sibling`: whether a same-stem implementation of
    /// a declaration is a file the walk would index.
    fn has_implementation_sibling(&self, file: &FilePath<'_>) -> Result<bool, PatternError> {
        let naming = self.inputs.naming;
        let stem = naming.module_stem(file.name, self.inputs.dot_suffix);
        for ext in &naming.implementation_extensions {
            if self.indexed(&file.sibling(&format!("{stem}{ext}")))? {
                return Ok(true);
            }
        }
        Ok(false)
    }

    /// Whether the file at `rel_path` is a regular file, no link, that
    /// `should_skip_path(..., is_file=True)` keeps: one that resolves
    /// outside the root is skipped.
    fn indexed(&self, rel_path: &str) -> Result<bool, PatternError> {
        let path: PathBuf = self.inputs.root.join(rel_path);
        let is_file = fs::metadata(&path).is_ok_and(|metadata| metadata.is_file());
        let is_link = fs::symlink_metadata(&path).is_ok_and(|metadata| metadata.is_symlink());
        if !is_file || is_link || !resolve(&path).starts_with(self.resolved_root) {
            return Ok(false);
        }
        Ok(!self.inputs.rules.should_skip_rel_path(rel_path, true)?)
    }
}
