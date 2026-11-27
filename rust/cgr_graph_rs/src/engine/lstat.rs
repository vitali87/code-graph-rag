//! An entry's own type, a link not followed, as Python reads it
//! (`os.lstat`, `os.path.islink`, `DirEntry.is_symlink()`): what every place
//! in the frame that asks whether an entry is a link goes by (issue #3740).
//!
//! On POSIX that is the type Rust reads. On Windows Rust's `is_symlink`
//! holds for every reparse point whose tag is a name surrogate: a symbolic
//! link, but also a junction (a mount point), a WSL link and more. CPython's
//! holds only for a reparse point tagged `IO_REPARSE_TAG_SYMLINK`
//! (`_Py_attribute_data_to_stat` for `os.lstat` and `DirEntry`, `_testInfo`
//! for `os.path.islink`); any other reparse point, of any tag, is the folder
//! or file its attributes say. Rust does not expose the tag, so an entry
//! Rust calls a link costs one more read, of its tag; no other entry does,
//! as Rust and CPython agree on every other entry.
//!
//! The walks (the file walk, the structure pass and the in-sync listing)
//! judge a listed entry one step further, as `is_junction_left_out` in
//! `codebase_rag/utils/path_utils.py` does: a junction naming a folder
//! outside the repository is left out as a link, so nothing beyond the root
//! is indexed through one, and so is one naming a folder the walk entered
//! on its way down to it (its own, the root, or one reached through other
//! junctions, each by its real path), which would be entered again at every
//! level. Any other junction is entered. It is judged by its target as the
//! reparse point records it, so a dangling one by where it points, and one
//! whose target cannot be read is a link.

use std::fs::{self, DirEntry, FileType};
use std::path::{Path, PathBuf};

use super::state::resolve;

#[cfg(test)]
mod tests;

/// An entry's own type as `os.lstat` gives it.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(super) enum Kind {
    Link,
    Folder,
    File,
    /// A pipe, socket or device.
    Special,
}

impl Kind {
    /// The kind of the entry at `path` whose type, read without following a
    /// link, is `kind`. `path` is asked for only when Windows needs the tag.
    pub(super) fn of<P: AsRef<Path>>(kind: FileType, path: impl FnOnce() -> P) -> Kind {
        Kind::judged(kind, path, None)
    }

    /// `of`, with a junction judged by its target against `root` when one
    /// is given.
    fn judged<P: AsRef<Path>>(
        kind: FileType,
        path: impl FnOnce() -> P,
        root: Option<&WalkRoot>,
    ) -> Kind {
        if kind.is_symlink() {
            symlink_kind(kind, path, root)
        } else if kind.is_dir() {
            Kind::Folder
        } else if kind.is_file() {
            Kind::File
        } else {
            Kind::Special
        }
    }
}

/// The repository a walk judges its entries against: its root as the walk
/// was given it, which the path of every folder it lists starts with, and
/// resolved.
pub(super) struct WalkRoot {
    #[cfg_attr(not(windows), allow(dead_code))]
    given: PathBuf,
    resolved: PathBuf,
}

impl WalkRoot {
    pub(super) fn new(root: &Path) -> WalkRoot {
        WalkRoot {
            given: root.to_path_buf(),
            resolved: resolve(root),
        }
    }

    pub(super) fn resolved(&self) -> &Path {
        &self.resolved
    }
}

/// The kind of an entry a walk of the repository at `root` lists: a
/// junction it leaves out is a link. `None` when its type cannot be read.
pub(super) fn walk_kind(entry: &DirEntry, root: &WalkRoot) -> Option<Kind> {
    let kind = entry.file_type().ok()?;
    Some(Kind::judged(kind, || entry.path(), Some(root)))
}

/// The kind of the entry at `path`, `None` when it cannot be read.
pub(super) fn lstat_kind(path: &Path) -> Option<Kind> {
    let kind = fs::symlink_metadata(path).ok()?.file_type();
    Some(Kind::of(kind, || path))
}

#[cfg(not(windows))]
fn symlink_kind<P>(_kind: FileType, _path: impl FnOnce() -> P, _root: Option<&WalkRoot>) -> Kind {
    Kind::Link
}

/// A name surrogate, typed by its tag as CPython types it, and a junction
/// by its target when `root` is given. One whose tag cannot be read is what
/// its attributes say, as `os.path.islink` and `os.path.isjunction` answer
/// no on an error.
#[cfg(windows)]
fn symlink_kind<P: AsRef<Path>>(
    kind: FileType,
    path: impl FnOnce() -> P,
    root: Option<&WalkRoot>,
) -> Kind {
    use std::os::windows::fs::FileTypeExt;

    let path = path();
    let path = path.as_ref();
    match windows::attributes_and_tag(path) {
        Some((attributes, tag)) => match root {
            Some(root) => walk_windows_kind(attributes, tag, || junction_entered(path, root)),
            None => windows_kind(attributes, tag),
        },
        None if kind.is_symlink_dir() => Kind::Folder,
        None => Kind::File,
    }
}

/// Whether a walk of `root` enters the junction at `path`: its target,
/// without the verbatim prefix where a drive or share follows it, taken
/// from the junction's folder and resolved as `Path.resolve()` resolves it,
/// is inside the root and none of the folders on the way down to it.
/// `None` when the target cannot be read.
#[cfg(windows)]
fn junction_entered(path: &Path, root: &WalkRoot) -> Option<bool> {
    let target = fs::read_link(path).ok()?;
    let spelled = match target.to_str() {
        Some(text) => PathBuf::from(without_verbatim_prefix(text).as_ref()),
        None => target,
    };
    let folder = path.parent().unwrap_or(Path::new(""));
    let resolved = resolve(&folder.join(spelled));
    let way_down: Vec<String> = way_down(folder, &root.given)
        .iter()
        .map(|entered| resolve(entered).to_string_lossy().into_owned())
        .collect();
    Some(!junction_left_out(
        &resolved.to_string_lossy(),
        way_down.iter().map(String::as_str),
        &root.resolved.to_string_lossy(),
    ))
}

/// `_way_down`: the folders a walk of `root` entered to reach `folder`, the
/// root first and `folder` last.
#[cfg(windows)]
fn way_down(folder: &Path, root: &Path) -> Vec<PathBuf> {
    let Ok(below) = folder.strip_prefix(root) else {
        return vec![folder.to_path_buf()];
    };
    let mut way = vec![root.to_path_buf()];
    for part in below.components() {
        let next = way[way.len() - 1].join(part);
        way.push(next);
    }
    way
}

/// winnt.h's values, which the decisions below are tested with everywhere.
#[cfg(any(windows, test))]
const FILE_ATTRIBUTE_DIRECTORY: u32 = 0x10;
#[cfg(any(windows, test))]
const FILE_ATTRIBUTE_REPARSE_POINT: u32 = 0x400;
#[cfg(any(windows, test))]
const IO_REPARSE_TAG_SYMLINK: u32 = 0xA000_000C;
#[cfg(any(windows, test))]
const IO_REPARSE_TAG_MOUNT_POINT: u32 = 0xA000_0003;
/// `cs.WINDOWS_VERBATIM_PREFIX`, its form for a share, the form a share's
/// path takes without it, and what follows a drive's letter.
#[cfg(any(windows, test))]
const VERBATIM_PREFIX: &str = r"\\?\";
#[cfg(any(windows, test))]
const VERBATIM_UNC_PREFIX: &str = r"\\?\UNC\";
#[cfg(any(windows, test))]
const UNC_PREFIX: &str = r"\\";
#[cfg(any(windows, test))]
const DRIVE_SEPARATOR: char = ':';
#[cfg(any(windows, test))]
const BACKSLASH: char = '\\';
#[cfg(any(windows, test))]
const BACKSLASH_STR: &str = "\\";
#[cfg(any(windows, test))]
const SLASH: char = '/';

/// How CPython types a Windows entry from its attributes and reparse tag: a
/// link only when it is a reparse point tagged `IO_REPARSE_TAG_SYMLINK`, and
/// anything else a folder when its attributes say so, else a file. No disk
/// entry is special to it.
#[cfg(any(windows, test))]
fn windows_kind(attributes: u32, reparse_tag: u32) -> Kind {
    if attributes & FILE_ATTRIBUTE_REPARSE_POINT != 0 && reparse_tag == IO_REPARSE_TAG_SYMLINK {
        Kind::Link
    } else if attributes & FILE_ATTRIBUTE_DIRECTORY != 0 {
        Kind::Folder
    } else {
        Kind::File
    }
}

/// `windows_kind` for an entry a walk lists: a junction (`os.path.isjunction`,
/// a reparse point tagged `IO_REPARSE_TAG_MOUNT_POINT`) is a folder only
/// when `entered` says the walk enters it, and a link when it does not or
/// its target cannot be read.
#[cfg(any(windows, test))]
fn walk_windows_kind(
    attributes: u32,
    reparse_tag: u32,
    entered: impl FnOnce() -> Option<bool>,
) -> Kind {
    let junction =
        attributes & FILE_ATTRIBUTE_REPARSE_POINT != 0 && reparse_tag == IO_REPARSE_TAG_MOUNT_POINT;
    match windows_kind(attributes, reparse_tag) {
        Kind::Folder if junction && entered() != Some(true) => Kind::Link,
        kind => kind,
    }
}

/// `junction_left_out`: whether a junction whose resolved target is
/// `target` is left out, as it is outside the resolved `root` or the same
/// folder as one of `way_down`, the real paths of the folders entered to
/// reach it. Each is compared as `PureWindowsPath` compares it: without the
/// verbatim prefix where a drive or share follows it, `/` read as `\`, and
/// without case.
#[cfg(any(windows, test))]
fn junction_left_out<'a>(
    target: &str,
    way_down: impl IntoIterator<Item = &'a str>,
    root: &str,
) -> bool {
    let target = comparable(target);
    let root = comparable(root);
    let within = target == root
        || target
            .strip_prefix(root.as_str())
            .is_some_and(|rest| root.ends_with(BACKSLASH) || rest.starts_with(BACKSLASH));
    !within
        || way_down
            .into_iter()
            .any(|entered| comparable(entered) == target)
}

#[cfg(any(windows, test))]
fn comparable(path: &str) -> String {
    without_verbatim_prefix(path)
        .replace(SLASH, BACKSLASH_STR)
        .to_lowercase()
}

/// `without_verbatim_prefix`: a junction's target without the `\\?\` prefix
/// where a drive (any character, then `:`) or a share follows it, as
/// `Path.resolve()` keeps a prefix it is given; any other target as it is.
#[cfg(any(windows, test))]
fn without_verbatim_prefix(target: &str) -> std::borrow::Cow<'_, str> {
    if let Some(share) = target.strip_prefix(VERBATIM_UNC_PREFIX) {
        return format!("{UNC_PREFIX}{share}").into();
    }
    match target.strip_prefix(VERBATIM_PREFIX) {
        Some(rest) if rest.chars().nth(1) == Some(DRIVE_SEPARATOR) => rest.into(),
        _ => target.into(),
    }
}

#[cfg(windows)]
mod windows {
    use std::fs::OpenOptions;
    use std::mem;
    use std::os::windows::fs::OpenOptionsExt;
    use std::os::windows::io::AsRawHandle;
    use std::path::Path;

    use windows_sys::Win32::Storage::FileSystem::{
        FileAttributeTagInfo, GetFileInformationByHandleEx, FILE_ATTRIBUTE_TAG_INFO,
        FILE_FLAG_BACKUP_SEMANTICS, FILE_FLAG_OPEN_REPARSE_POINT,
    };

    // The values the decisions are tested with are the bindings' own.
    const _: () = {
        use windows_sys::Win32::Storage::FileSystem::{
            FILE_ATTRIBUTE_DIRECTORY, FILE_ATTRIBUTE_REPARSE_POINT,
        };
        use windows_sys::Win32::System::SystemServices::{
            IO_REPARSE_TAG_MOUNT_POINT, IO_REPARSE_TAG_SYMLINK,
        };
        assert!(super::FILE_ATTRIBUTE_DIRECTORY == FILE_ATTRIBUTE_DIRECTORY);
        assert!(super::FILE_ATTRIBUTE_REPARSE_POINT == FILE_ATTRIBUTE_REPARSE_POINT);
        assert!(super::IO_REPARSE_TAG_SYMLINK == IO_REPARSE_TAG_SYMLINK);
        assert!(super::IO_REPARSE_TAG_MOUNT_POINT == IO_REPARSE_TAG_MOUNT_POINT);
    };

    /// The entry's own attributes and reparse tag, read through a handle on
    /// the reparse point itself, as `os.path.islink` reads them; `None` when
    /// it cannot be opened or read.
    pub(super) fn attributes_and_tag(path: &Path) -> Option<(u32, u32)> {
        let file = OpenOptions::new()
            // No access right, as `fs::symlink_metadata` opens it: reading
            // attributes needs none.
            .access_mode(0)
            .custom_flags(FILE_FLAG_OPEN_REPARSE_POINT | FILE_FLAG_BACKUP_SEMANTICS)
            .open(path)
            .ok()?;
        let mut info = FILE_ATTRIBUTE_TAG_INFO {
            FileAttributes: 0,
            ReparseTag: 0,
        };
        let size = u32::try_from(mem::size_of::<FILE_ATTRIBUTE_TAG_INFO>()).ok()?;
        // SAFETY: the handle stays open for the call, and `info` is a
        // FILE_ATTRIBUTE_TAG_INFO of the size passed, which the call fills.
        let read = unsafe {
            GetFileInformationByHandleEx(
                file.as_raw_handle(),
                FileAttributeTagInfo,
                (&raw mut info).cast(),
                size,
            )
        };
        (read != 0).then_some((info.FileAttributes, info.ReparseTag))
    }
}
