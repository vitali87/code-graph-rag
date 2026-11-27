//! How an entry's type is read, as Python reads it. The Windows decision is
//! a pure function of an entry's attributes and reparse tag, so it is
//! checked here on every system; `codebase_rag/tests/test_engine_discovery.py`
//! and its neighbours hold the walk, the structure pass and the in-sync
//! check to Python's on real junctions, on Windows.

use super::{walk_windows_kind, windows_kind, without_verbatim_prefix, Kind};

const FILE_ATTRIBUTE_READONLY: u32 = 0x1;
const FILE_ATTRIBUTE_DIRECTORY: u32 = 0x10;
const FILE_ATTRIBUTE_ARCHIVE: u32 = 0x20;
const FILE_ATTRIBUTE_REPARSE_POINT: u32 = 0x400;
const FILE_ATTRIBUTE_SPARSE_FILE: u32 = 0x200;

/// The tags of winnt.h this decision meets: the name surrogates Rust calls
/// links, and others it does not.
const IO_REPARSE_TAG_SYMLINK: u32 = 0xA000_000C;
const IO_REPARSE_TAG_MOUNT_POINT: u32 = 0xA000_0003;
const IO_REPARSE_TAG_LX_SYMLINK: u32 = 0xA000_001D;
const IO_REPARSE_TAG_WCI_LINK_1: u32 = 0xA000_0027;
const IO_REPARSE_TAG_APPEXECLINK: u32 = 0x8000_001B;
const IO_REPARSE_TAG_CLOUD: u32 = 0x9000_001A;
const IO_REPARSE_TAG_DEDUP: u32 = 0x8000_0013;

const NAME_SURROGATE: u32 = 0x2000_0000;

fn reparse(attributes: u32) -> u32 {
    attributes | FILE_ATTRIBUTE_REPARSE_POINT
}

#[test]
fn a_symbolic_link_is_a_link_to_a_file_or_a_folder() {
    let file = reparse(FILE_ATTRIBUTE_ARCHIVE);
    let folder = reparse(FILE_ATTRIBUTE_DIRECTORY);

    assert_eq!(windows_kind(file, IO_REPARSE_TAG_SYMLINK), Kind::Link);
    assert_eq!(windows_kind(folder, IO_REPARSE_TAG_SYMLINK), Kind::Link);
}

#[test]
fn a_junction_is_the_folder_it_says_it_is() {
    let junction = reparse(FILE_ATTRIBUTE_DIRECTORY);

    // Rust's `is_symlink` holds for it, a name surrogate; Python's walk
    // enters it (#3740).
    assert_ne!(IO_REPARSE_TAG_MOUNT_POINT & NAME_SURROGATE, 0);
    assert_eq!(
        windows_kind(junction, IO_REPARSE_TAG_MOUNT_POINT),
        Kind::Folder
    );
}

#[test]
fn every_other_name_surrogate_is_no_link_either() {
    for tag in [IO_REPARSE_TAG_LX_SYMLINK, IO_REPARSE_TAG_WCI_LINK_1] {
        assert_ne!(tag & NAME_SURROGATE, 0);
        assert_eq!(
            windows_kind(reparse(FILE_ATTRIBUTE_ARCHIVE), tag),
            Kind::File
        );
        assert_eq!(
            windows_kind(reparse(FILE_ATTRIBUTE_DIRECTORY), tag),
            Kind::Folder
        );
    }
}

#[test]
fn reparse_points_that_are_no_name_surrogates_are_what_they_say() {
    // An app execution alias, a cloud placeholder, a deduplicated file.
    let file = reparse(FILE_ATTRIBUTE_ARCHIVE | FILE_ATTRIBUTE_SPARSE_FILE);
    for tag in [
        IO_REPARSE_TAG_APPEXECLINK,
        IO_REPARSE_TAG_CLOUD,
        IO_REPARSE_TAG_DEDUP,
    ] {
        assert_eq!(tag & NAME_SURROGATE, 0);
        assert_eq!(windows_kind(file, tag), Kind::File);
    }
    assert_eq!(
        windows_kind(reparse(FILE_ATTRIBUTE_DIRECTORY), IO_REPARSE_TAG_CLOUD),
        Kind::Folder
    );
}

#[test]
fn a_symlink_tag_without_the_reparse_attribute_is_no_link() {
    // CPython reads the tag only off a reparse point.
    assert_eq!(
        windows_kind(FILE_ATTRIBUTE_ARCHIVE, IO_REPARSE_TAG_SYMLINK),
        Kind::File
    );
    assert_eq!(
        windows_kind(FILE_ATTRIBUTE_DIRECTORY, IO_REPARSE_TAG_SYMLINK),
        Kind::Folder
    );
}

#[test]
fn plain_files_and_folders_keep_their_kind() {
    assert_eq!(windows_kind(FILE_ATTRIBUTE_ARCHIVE, 0), Kind::File);
    assert_eq!(windows_kind(FILE_ATTRIBUTE_READONLY, 0), Kind::File);
    assert_eq!(windows_kind(0, 0), Kind::File);
    assert_eq!(windows_kind(FILE_ATTRIBUTE_DIRECTORY, 0), Kind::Folder);
}

#[test]
fn a_walk_enters_a_junction_only_when_it_names_a_folder_inside_the_root() {
    let junction = reparse(FILE_ATTRIBUTE_DIRECTORY);
    let at = |inside| walk_windows_kind(junction, IO_REPARSE_TAG_MOUNT_POINT, || inside);

    assert_eq!(at(Some(true)), Kind::Folder);
    assert_eq!(at(Some(false)), Kind::Link);
    // A target that cannot be read is left out, as Python leaves it.
    assert_eq!(at(None), Kind::Link);
}

#[test]
fn a_walk_asks_where_only_a_junction_points() {
    let never = || -> Option<bool> { panic!("asked where a non-junction points") };
    let folder = reparse(FILE_ATTRIBUTE_DIRECTORY);

    assert_eq!(
        walk_windows_kind(folder, IO_REPARSE_TAG_SYMLINK, never),
        Kind::Link
    );
    assert_eq!(
        walk_windows_kind(folder, IO_REPARSE_TAG_LX_SYMLINK, never),
        Kind::Folder
    );
    assert_eq!(
        walk_windows_kind(FILE_ATTRIBUTE_DIRECTORY, IO_REPARSE_TAG_MOUNT_POINT, never),
        Kind::Folder
    );
    assert_eq!(
        walk_windows_kind(FILE_ATTRIBUTE_ARCHIVE, 0, never),
        Kind::File
    );
}

#[test]
fn a_target_s_verbatim_prefix_goes_where_a_drive_or_share_follows() {
    let cases = [
        (r"\\?\C:\repo\x", r"C:\repo\x"),
        (r"\\?\UNC\host\share\x", r"\\host\share\x"),
        (r"\\?\Volume{0}\x", r"\\?\Volume{0}\x"),
        (r"C:\repo\x", r"C:\repo\x"),
        ("/repo/x", "/repo/x"),
    ];
    for (target, spelled) in cases {
        assert_eq!(without_verbatim_prefix(target), spelled, "{target}");
    }
}

#[cfg(unix)]
mod on_posix {
    use std::ffi::CString;
    use std::fs;
    use std::os::unix::ffi::OsStrExt;
    use std::os::unix::fs::symlink;

    use super::super::super::tests::Scratch;
    use super::super::{lstat_kind, walk_kind, Kind};

    #[test]
    fn the_kind_is_the_one_rust_reads() {
        let scratch = Scratch::new("lstat-kinds");
        let root = &scratch.0;
        fs::write(root.join("file"), b"x").unwrap();
        fs::create_dir(root.join("folder")).unwrap();
        symlink("folder", root.join("folder_link")).unwrap();
        symlink("absent", root.join("dangling")).unwrap();
        let pipe = CString::new(root.join("pipe").as_os_str().as_bytes()).unwrap();
        // SAFETY: a NUL-terminated path, as mkfifo takes.
        assert_eq!(unsafe { libc::mkfifo(pipe.as_ptr(), 0o600) }, 0);

        let expected = [
            ("dangling", Kind::Link),
            ("file", Kind::File),
            ("folder", Kind::Folder),
            ("folder_link", Kind::Link),
            ("pipe", Kind::Special),
        ];
        for (name, kind) in expected {
            assert_eq!(lstat_kind(&root.join(name)), Some(kind), "{name}");
        }
        let mut listed: Vec<_> = fs::read_dir(root)
            .unwrap()
            .map(|entry| {
                let entry = entry.unwrap();
                (entry.file_name(), walk_kind(&entry, root))
            })
            .collect();
        listed.sort_by(|a, b| a.0.cmp(&b.0));
        let listed: Vec<_> = listed
            .iter()
            .map(|(name, kind)| (name.to_str().unwrap(), kind.unwrap()))
            .collect();
        assert_eq!(listed, expected);
        assert_eq!(lstat_kind(&root.join("absent")), None);
    }
}
