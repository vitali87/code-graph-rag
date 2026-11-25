//! The walk's rules on small trees. `codebase_rag/tests/test_engine_discovery.py`
//! holds the whole plan to Python's own walk on these corners and more.

use std::ffi::OsString;
use std::fs;
use std::path::Path;

use super::super::ignore::{IgnoreRules, PatternError};
use super::super::tests::Scratch;
use super::super::{md5_file, Engine};
use super::{FilePlan, ROOT_DIR_KEY, STATE_FILENAMES};

fn write(root: &Path, rel: &str) {
    let path = root.join(rel);
    fs::create_dir_all(path.parent().unwrap()).unwrap();
    fs::write(&path, rel.as_bytes()).unwrap();
}

fn no_rules() -> IgnoreRules {
    IgnoreRules::new([""; 0], [""; 0])
}

fn plan(root: &Path, rules: &IgnoreRules) -> FilePlan {
    Engine::new(2)
        .unwrap()
        .file_plan(root.as_os_str(), rules)
        .unwrap()
}

fn rel_paths(plan: &FilePlan) -> Vec<&str> {
    plan.files
        .iter()
        .map(|file| file.rel_path.as_str())
        .collect()
}

fn keys(plan: &FilePlan) -> Vec<&str> {
    plan.dirs.iter().map(|dir| dir.key.as_str()).collect()
}

#[test]
fn each_folder_gives_its_files_then_its_subfolders_in_name_order() {
    let scratch = Scratch::new("walk-order");
    let root = &scratch.0;
    for rel in [
        "a-b/x.py",
        "a/b.py",
        "a.b",
        "a_b",
        "B.py",
        "a.py",
        "é.py",
        "z.py",
        "中文/文件.py",
        "🎉.py",
        "a/c/d/e/f/g.py",
        "a/c/d/e/f/h/i.py",
    ] {
        write(root, rel);
    }

    let found = plan(root, &no_rules());

    // The root's files first; `a/` before `a-b/`, though `a-b/x.py` sorts
    // before `a/b.py` as a path; upper case before lower; by code point.
    assert_eq!(
        rel_paths(&found),
        [
            "B.py",
            "a.b",
            "a.py",
            "a_b",
            "z.py",
            "é.py",
            "🎉.py",
            "a/b.py",
            "a/c/d/e/f/g.py",
            "a/c/d/e/f/h/i.py",
            "a-b/x.py",
            "中文/文件.py",
        ]
    );
    assert_eq!(
        keys(&found),
        [
            ROOT_DIR_KEY,
            "a",
            "a/c",
            "a/c/d",
            "a/c/d/e",
            "a/c/d/e/f",
            "a/c/d/e/f/h",
            "a-b",
            "中文"
        ]
    );
    let deepest = &found.dirs[6];
    assert_eq!(
        Path::new(&deepest.dirpath),
        root.join("a")
            .join("c")
            .join("d")
            .join("e")
            .join("f")
            .join("h")
    );
    assert_eq!(found.dirs[0].dirpath, root.as_os_str());
}

#[test]
fn every_file_is_hashed_as_md5_file_hashes_it() {
    let scratch = Scratch::new("walk-hashes");
    write(&scratch.0, "a.py");
    write(&scratch.0, "pkg/b.py");

    let found = plan(&scratch.0, &no_rules());

    let expected: Vec<Option<String>> = found.paths().iter().map(|path| md5_file(path)).collect();
    assert_eq!(found.hashes, expected);
    assert!(found.hashes.iter().all(Option::is_some));
}

#[test]
fn the_plan_is_the_same_on_any_number_of_threads() {
    let scratch = Scratch::new("walk-threads");
    for i in 0..40 {
        for j in 0..5 {
            write(&scratch.0, &format!("d{i:02}/e{j}/f{}.py", (i * j) % 7));
        }
        write(&scratch.0, &format!("d{i:02}/top.py"));
    }
    let rules = IgnoreRules::new(["e3/"], [""; 0]);

    let one = Engine::new(1)
        .unwrap()
        .file_plan(scratch.0.as_os_str(), &rules)
        .unwrap();
    for threads in [2, 3, 0] {
        let other = Engine::new(threads)
            .unwrap()
            .file_plan(scratch.0.as_os_str(), &rules)
            .unwrap();
        assert_eq!(other, one, "{threads} threads");
    }
    assert_eq!(one.files.len(), 40 * 5);
}

#[test]
fn the_rules_prune_folders_and_skip_files() {
    let scratch = Scratch::new("walk-rules");
    let root = &scratch.0;
    for rel in [
        "keep.py",
        "skip.min.js",
        "lib.o",
        "node_modules/x.js",
        "src/bin/main.rs",
        "bin/tool.sh",
        ".git/config",
        "gen/out.py",
        "nested/deeper/.cgr-hash-cache.json",
        ".cgr-sync-lock",
    ] {
        write(root, rel);
    }
    let rules = IgnoreRules::new(["gen/"], [""; 0]);

    let found = plan(root, &rules);

    assert_eq!(rel_paths(&found), ["keep.py", "src/bin/main.rs"]);
    assert_eq!(
        keys(&found),
        [ROOT_DIR_KEY, "nested", "nested/deeper", "src", "src/bin"]
    );
}

#[test]
fn every_state_file_is_dropped_at_any_depth() {
    let scratch = Scratch::new("walk-state");
    for name in STATE_FILENAMES {
        write(&scratch.0, name);
        write(&scratch.0, &format!("a/{name}"));
    }
    write(&scratch.0, "a/kept.py");

    assert_eq!(rel_paths(&plan(&scratch.0, &no_rules())), ["a/kept.py"]);
}

#[test]
fn a_pattern_python_refuses_fails_the_walk() {
    let scratch = Scratch::new("walk-refused");
    write(&scratch.0, "a.py");
    let rules = IgnoreRules::new(["[z-a]"], [""; 0]);

    let walked = Engine::new(1)
        .unwrap()
        .file_plan(scratch.0.as_os_str(), &rules);

    assert!(matches!(walked, Err(PatternError::BadRange { .. })));
}

#[test]
fn a_root_that_cannot_be_listed_gives_nothing() {
    let scratch = Scratch::new("walk-missing");

    let found = plan(&scratch.0.join("absent"), &no_rules());

    assert_eq!(found, FilePlan::default());
}

#[test]
fn a_root_given_with_a_trailing_separator_joins_no_second_one() {
    let scratch = Scratch::new("walk-separator");
    write(&scratch.0, "pkg/a.py");
    let mut root = scratch.0.clone().into_os_string();
    root.push(std::path::MAIN_SEPARATOR_STR);

    let found = Engine::new(1)
        .unwrap()
        .file_plan(&root, &no_rules())
        .unwrap();

    let mut expected = root.clone();
    expected.push("pkg");
    assert_eq!(found.dirs[1].dirpath, expected);
}

#[cfg(unix)]
mod unix {
    use std::ffi::OsStr;
    use std::os::unix::ffi::OsStrExt;
    use std::os::unix::fs::{symlink, PermissionsExt};
    use std::os::unix::net::UnixListener;

    use super::*;

    fn reported(paths: &[OsString]) -> Vec<&OsStr> {
        let mut sorted: Vec<&OsStr> = paths.iter().map(OsString::as_os_str).collect();
        sorted.sort();
        sorted
    }

    #[test]
    fn links_are_reported_and_never_followed() {
        let scratch = Scratch::new("walk-links");
        let root = &scratch.0;
        write(root, "real/a.py");
        write(root, "b.py");
        symlink("b.py", root.join("file_link.py")).unwrap();
        symlink("real", root.join("dir_link")).unwrap();
        symlink("loop", root.join("loop")).unwrap();
        symlink("absent", root.join("dangling")).unwrap();
        // A link to a folder the rules prune is pruned, not reported.
        symlink("real", root.join("node_modules")).unwrap();

        let found = plan(root, &no_rules());

        assert_eq!(rel_paths(&found), ["b.py", "real/a.py"]);
        assert_eq!(
            reported(&found.links),
            ["dangling", "dir_link", "file_link.py", "loop"]
        );
    }

    #[test]
    fn a_root_that_is_a_link_is_followed() {
        let scratch = Scratch::new("walk-root-link");
        write(&scratch.0, "repo/a.py");
        let link = scratch.0.join("link");
        symlink("repo", &link).unwrap();

        let found = plan(&link, &no_rules());

        assert_eq!(rel_paths(&found), ["a.py"]);
        assert_eq!(found.dirs[0].dirpath, link.as_os_str());
    }

    #[test]
    fn a_socket_is_reported_unless_the_rules_skip_it() {
        let scratch = Scratch::new("walk-special");
        write(&scratch.0, "a.py");
        let _socket = UnixListener::bind(scratch.0.join("live.sock")).unwrap();
        let _skipped = UnixListener::bind(scratch.0.join("gone.sock")).unwrap();

        let found = plan(&scratch.0, &IgnoreRules::new(["gone.*"], [""; 0]));

        assert_eq!(rel_paths(&found), ["a.py"]);
        assert_eq!(reported(&found.special), ["live.sock"]);
    }

    /// `parent/name` made with these raw bytes, or `None` where the file
    /// system refuses a name that is not UTF-8.
    fn undecodable(parent: &Path, name: &[u8], folder: bool) -> Option<std::path::PathBuf> {
        let path = parent.join(OsStr::from_bytes(name));
        let made = if folder {
            fs::create_dir(&path)
        } else {
            fs::write(&path, b"x = 1\n")
        };
        made.ok().map(|()| path)
    }

    #[test]
    fn a_name_that_is_not_utf8_is_reported_unless_the_rules_skip_it() {
        let scratch = Scratch::new("walk-undecodable");
        let root = &scratch.0;
        write(root, "a.py");
        let Some(folder) = undecodable(root, b"dir\xff", true) else {
            return;
        };
        write(&folder, "c.py");
        undecodable(root, b"bad\xff.py", false).unwrap();
        undecodable(root, b"gen\xfe", true).unwrap();
        undecodable(root, b"gen\xfe.py", false).unwrap();

        // `?` takes the one byte, as it takes Python's one surrogate.
        let found = plan(root, &IgnoreRules::new(["gen?", "gen?.py"], [""; 0]));

        assert_eq!(rel_paths(&found), ["a.py"]);
        assert_eq!(
            reported(&found.undecodable),
            [
                OsStr::from_bytes(b"bad\xff.py"),
                OsStr::from_bytes(b"dir\xff")
            ]
        );
    }

    #[test]
    fn a_folder_that_cannot_be_listed_is_skipped_whole() {
        let scratch = Scratch::new("walk-unreadable");
        let root = &scratch.0;
        write(root, "a.py");
        write(root, "locked/b.py");
        write(root, "open/c.py");
        let locked = root.join("locked");
        fs::set_permissions(&locked, fs::Permissions::from_mode(0o000)).unwrap();
        // Root reads any folder, so there is nothing to see.
        let readable = fs::read_dir(&locked).is_ok();

        let found = plan(root, &no_rules());

        fs::set_permissions(&locked, fs::Permissions::from_mode(0o755)).unwrap();
        if readable {
            return;
        }
        assert_eq!(rel_paths(&found), ["a.py", "open/c.py"]);
        assert_eq!(keys(&found), [ROOT_DIR_KEY, "open"]);
    }
}
