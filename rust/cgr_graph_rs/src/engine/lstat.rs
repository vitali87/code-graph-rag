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
//! or file its attributes say. So Python's walk enters a junction and
//! indexes what it holds, and so does the frame's. Rust does not expose the
//! tag, so an entry Rust calls a link costs one more read, of its tag; no
//! other entry does, as Rust and CPython agree on every other entry.

use std::fs::{self, DirEntry, FileType};
use std::path::Path;

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
        if kind.is_symlink() {
            symlink_kind(kind, path)
        } else if kind.is_dir() {
            Kind::Folder
        } else if kind.is_file() {
            Kind::File
        } else {
            Kind::Special
        }
    }
}

/// The kind of a listed entry, `None` when its type cannot be read.
pub(super) fn entry_kind(entry: &DirEntry) -> Option<Kind> {
    let kind = entry.file_type().ok()?;
    Some(Kind::of(kind, || entry.path()))
}

/// The kind of the entry at `path`, `None` when it cannot be read.
pub(super) fn lstat_kind(path: &Path) -> Option<Kind> {
    let kind = fs::symlink_metadata(path).ok()?.file_type();
    Some(Kind::of(kind, || path))
}

#[cfg(not(windows))]
fn symlink_kind<P>(_kind: FileType, _path: impl FnOnce() -> P) -> Kind {
    Kind::Link
}

/// A name surrogate, typed by its tag as CPython types it. One whose tag
/// cannot be read is what its attributes say, as `os.path.islink` answers
/// no on an error.
#[cfg(windows)]
fn symlink_kind<P: AsRef<Path>>(kind: FileType, path: impl FnOnce() -> P) -> Kind {
    use std::os::windows::fs::FileTypeExt;

    match windows::attributes_and_tag(path().as_ref()) {
        Some((attributes, tag)) => windows_kind(attributes, tag),
        None if kind.is_symlink_dir() => Kind::Folder,
        None => Kind::File,
    }
}

/// winnt.h's values, which the decision below is tested with everywhere.
#[cfg(any(windows, test))]
const FILE_ATTRIBUTE_DIRECTORY: u32 = 0x10;
#[cfg(any(windows, test))]
const FILE_ATTRIBUTE_REPARSE_POINT: u32 = 0x400;
#[cfg(any(windows, test))]
const IO_REPARSE_TAG_SYMLINK: u32 = 0xA000_000C;

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

    // The values the decision is tested with are the bindings' own.
    const _: () = {
        use windows_sys::Win32::Storage::FileSystem::{
            FILE_ATTRIBUTE_DIRECTORY, FILE_ATTRIBUTE_REPARSE_POINT,
        };
        use windows_sys::Win32::System::SystemServices::IO_REPARSE_TAG_SYMLINK;
        assert!(super::FILE_ATTRIBUTE_DIRECTORY == FILE_ATTRIBUTE_DIRECTORY);
        assert!(super::FILE_ATTRIBUTE_REPARSE_POINT == FILE_ATTRIBUTE_REPARSE_POINT);
        assert!(super::IO_REPARSE_TAG_SYMLINK == IO_REPARSE_TAG_SYMLINK);
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
