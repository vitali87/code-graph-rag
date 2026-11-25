//! The state files and change detection on small cases.
//! `codebase_rag/tests/test_engine_change_detection.py` holds them to
//! Python's own readers, writers and sync, byte for byte.

use std::collections::{BTreeSet, HashSet};
use std::fs;
use std::path::Path;

use super::super::ignore::IgnoreRules;
use super::super::tests::Scratch;
use super::super::{md5_file, Engine};
use super::json::{self, object_members, Value};
use super::*;

fn members(text: &str) -> Vec<(Option<String>, Value)> {
    object_members(text).unwrap().unwrap()
}

fn refused(text: &str) -> bool {
    object_members(text).is_err()
}

#[test]
fn json_accepts_what_python_accepts() {
    assert_eq!(object_members(" [1, {\"a\": [[]]}] \n").unwrap(), None);
    assert_eq!(
        members("{\"a\": NaN, \"b\": -Infinity, \"c\": 1e400, \"d\": -0}")
            .into_iter()
            .map(|(_, value)| format!("{value:?}"))
            .collect::<Vec<_>>(),
        ["Float(NaN)", "Float(-inf)", "Float(inf)", "Int(Some(0.0))"]
    );
    // Duplicates are kept in order; the reader folds them as a dict does.
    assert_eq!(members("{\"a\": 1, \"a\": 2}").len(), 2);
    assert_eq!(
        members(r#"{"\ud83d\ude00\u00e9\/\n": "x\"y"}"#),
        [(
            Some("\u{1f600}\u{e9}/\n".to_owned()),
            Value::Str(Some("x\"y".to_owned()))
        )]
    );
}

#[test]
fn json_refuses_what_python_refuses() {
    for text in [
        "",
        "\u{feff}{}",
        "{} x",
        "{\"a\": 1,}",
        "[1,]",
        "{\"a\" 1}",
        "{'a': 1}",
        "{\"a\": 01}",
        "{\"a\": 1.}",
        "{\"a\": -}",
        "{\"a\": \"\t\"}",
        "{\"a\": \"\\x\"}",
        "{\"a\": \"\\u12g4\"}",
        "{\"a\": \"\\ud800\\u12g4\"}",
        "{\"a\": \"unterminated}",
        "{\"a\": nul}",
    ] {
        assert!(refused(text), "{text:?} should be refused");
    }
    let digits = "1".repeat(json::INT_MAX_STR_DIGITS + 1);
    assert!(refused(&format!("{{\"a\": [{digits}]}}")));
    assert!(!refused(&format!("{{\"a\": {}}}", &digits[1..])));
    // A float has no digit limit.
    assert!(!refused(&format!("{{\"a\": {digits}.0}}")));
}

#[test]
fn json_numbers_end_where_python_s_do() {
    // `1e` and `1.` are a number and then a stray character.
    assert!(refused("[1e]"));
    assert!(refused("[1.]"));
    assert_eq!(members("{\"a\": 1E+2}")[0].1, Value::Float(100.0));
    assert_eq!(
        members(&format!("{{\"a\": 1{}}}", "0".repeat(400)))[0].1,
        Value::Int(None)
    );
}

#[test]
fn json_keeps_a_lone_surrogate_out_of_rust_strings() {
    let found = members(r#"{"a\udcff": "x", "b": "\ud800", "c": "\ud800\u0041"}"#);
    assert_eq!(found[0].0, None);
    assert_eq!(found[1].1, Value::Str(None));
    assert_eq!(found[2].1, Value::Str(None));
}

#[test]
fn json_nesting_stops_where_python_s_does_and_never_exhausts_the_stack() {
    let nested = |depth: usize| format!("{{\"a\": {}{}}}", "[".repeat(depth), "]".repeat(depth));
    // The object is the first of the levels.
    assert_eq!(members(&nested(json::MAX_NESTING - 1))[0].1, Value::Other);
    assert!(refused(&nested(json::MAX_NESTING)));
    assert!(refused(&nested(1_000_000)));
    let top = |depth: usize| format!("{}{}", "[".repeat(depth), "]".repeat(depth));
    assert_eq!(object_members(&top(json::MAX_NESTING)).unwrap(), None);
    assert!(refused(&top(json::MAX_NESTING + 1)));
}

#[test]
fn floats_are_written_as_repr_writes_them() {
    for (value, repr) in [
        (0.0, "0.0"),
        (-0.0, "-0.0"),
        (1.0, "1.0"),
        (1.5, "1.5"),
        (0.1, "0.1"),
        (1795611622.6097696, "1795611622.6097696"),
        (1e15, "1000000000000000.0"),
        (1e16, "1e+16"),
        (1.5e16, "1.5e+16"),
        (0.0001, "0.0001"),
        (0.00001, "1e-05"),
        (5e-324, "5e-324"),
        (1.7976931348623157e308, "1.7976931348623157e+308"),
        (123456789.12345679, "123456789.12345679"),
        // Exactly halfway between two shortest strings: `repr` rounds to
        // even, where `{:e}` alone rounds up.
        (26_363_981_746_409.0 + 0.3125, "26363981746409.312"),
        (1_059_438_285_926_254.0 + 0.25, "1059438285926254.2"),
    ] {
        assert_eq!(json::float_repr(value), repr);
    }
    assert_eq!(json::json_float(f64::NAN), "NaN");
    assert_eq!(json::json_float(f64::NEG_INFINITY), "-Infinity");
}

#[test]
fn the_files_are_laid_out_as_json_dump_lays_them_out() {
    let hashes: Entries<String> = [("a.py", "x"), ("é", "unreadable")]
        .into_iter()
        .map(|(k, v)| (k.to_owned(), v.to_owned()))
        .collect();
    assert_eq!(
        hash_cache_text(&hashes),
        "{\n  \"a.py\": \"x\",\n  \"\\u00e9\": \"unreadable\"\n}"
    );
    assert_eq!(hash_cache_text(&Entries::new()), "{}");
    let mtimes: Entries<f64> = [(".", 1.5), ("src", 2.0)]
        .into_iter()
        .map(|(k, v)| (k.to_owned(), v))
        .collect();
    assert_eq!(dir_mtimes_text(&mtimes), "{\".\": 1.5, \"src\": 2.0}");
    assert_eq!(dir_mtimes_text(&Entries::new()), "{}");
}

#[test]
fn entries_keep_a_dict_s_order() {
    let mut entries: Entries<u8> = [("a", 1), ("b", 2), ("a", 3)]
        .into_iter()
        .map(|(k, v)| (k.to_owned(), v))
        .collect();
    assert_eq!(entries.iter().collect::<Vec<_>>(), [("a", &3), ("b", &2)]);
    entries.remove_all(&BTreeSet::from(["a".to_owned()]));
    entries.insert("a".to_owned(), 4);
    assert_eq!(entries.keys().collect::<Vec<_>>(), ["b", "a"]);
}

#[test]
fn the_readers_fold_and_filter_as_python_s_do() {
    let scratch = Scratch::new("state-readers");
    let cache = scratch.0.join("cache.json");
    fs::write(
        &cache,
        r#"{"a": 1, "b": "y", "a": "x", "c\udcff": "z", "d": "\udcff", "e": null}"#,
    )
    .unwrap();
    let read = read_hash_cache(&cache);
    assert_eq!(read.error, None);
    assert_eq!(
        read.entries.iter().collect::<Vec<_>>(),
        [("a", &"x".to_owned()), ("b", &"y".to_owned())]
    );
    fs::write(&cache, b"\xff{}").unwrap();
    assert!(read_hash_cache(&cache).error.is_some());
    assert_eq!(
        read_hash_cache(&scratch.0.join("missing")),
        HashCacheRead::default()
    );

    let mtimes = scratch.0.join("mtimes.json");
    fs::write(
        &mtimes,
        r#"{".": 1, "a": true, "b": "x", "c\udcff": 2.5, "d": 2.5}"#,
    )
    .unwrap();
    assert_eq!(
        read_dir_mtimes(&mtimes).iter().collect::<Vec<_>>(),
        [(".", &1.0), ("a", &1.0), ("d", &2.5)]
    );
    fs::write(
        &mtimes,
        format!("{{\".\": 1, \"a\": 1{}}}", "0".repeat(400)),
    )
    .unwrap();
    assert!(read_dir_mtimes(&mtimes).is_empty());
}

#[test]
fn the_state_folder_is_named_as_python_names_it() {
    let scratch = Scratch::new("state-name");
    let digest = |path: &Path| {
        let resolved = resolve(path);
        super::super::hex(&Sha256::digest(resolved.to_str().unwrap().as_bytes()))[..8].to_owned()
    };
    for (name, base) in [
        ("plain", "plain"),
        ("a.b c", "a_b_c"),
        ("мой-repo", "repo"),
        ("__x--", "x"),
        ("...", "repo"),
        ("é-é", "repo"),
    ] {
        let dir = scratch.0.join(name);
        fs::create_dir_all(&dir).unwrap();
        assert_eq!(
            state_dir_name(&dir).unwrap(),
            format!("{base}__{}", digest(&dir)),
            "{name}"
        );
    }
    // A path that does not exist resolves as far as it does.
    let missing = scratch.0.join("gone").join("..").join("plain");
    assert_eq!(resolve(&missing), resolve(&scratch.0.join("plain")));
}

#[test]
fn a_first_use_moves_old_state_in_and_never_replaces_a_file() {
    let scratch = Scratch::new("state-adopt");
    let repo = scratch.0.join("repo");
    let home = scratch.0.join("home");
    fs::create_dir_all(&repo).unwrap();
    for name in [
        HASH_CACHE_FILENAME,
        DIR_MTIMES_FILENAME,
        ".cgr-sync-lock",
        EDIT_HISTORY_FILENAME,
    ] {
        fs::write(repo.join(name), name).unwrap();
    }
    let stamp = utime_instant(1_000_000_000.25).unwrap();
    let file = fs::OpenOptions::new()
        .write(true)
        .open(repo.join(HASH_CACHE_FILENAME))
        .unwrap();
    file.set_times(fs::FileTimes::new().set_modified(stamp))
        .unwrap();
    drop(file);
    let dir = state_dir(&home, &repo).unwrap();
    fs::create_dir_all(&dir).unwrap();
    fs::write(dir.join(DIR_MTIMES_FILENAME), "kept").unwrap();

    let prepared = prepare_state_dir(&home, &repo).unwrap();

    assert_eq!(prepared.dir, dir);
    assert_eq!(prepared.adopted, [HASH_CACHE_FILENAME]);
    assert!(prepared.failed.is_empty());
    assert_eq!(
        fs::read_to_string(dir.join(DIR_MTIMES_FILENAME)).unwrap(),
        "kept"
    );
    let moved = dir.join(HASH_CACHE_FILENAME);
    assert_eq!(fs::read_to_string(&moved).unwrap(), HASH_CACHE_FILENAME);
    assert_eq!(fs::metadata(&moved).unwrap().modified().unwrap(), stamp);
    // The checkout's copies are gone, but for the lock and the history.
    let mut left: Vec<String> = fs::read_dir(&repo)
        .unwrap()
        .map(|entry| entry.unwrap().file_name().into_string().unwrap())
        .collect();
    left.sort();
    assert_eq!(left, [".cgr-edit-history.json", ".cgr-sync-lock"]);
    // No temporary file is left beside the state.
    assert_eq!(fs::read_dir(&dir).unwrap().count(), 2);
}

#[test]
fn a_published_cache_carries_its_stamp_and_replaces_the_last() {
    let scratch = Scratch::new("state-publish");
    let path = scratch.0.join("state").join(HASH_CACHE_FILENAME);
    let hashes: Entries<String> = [("a.py".to_owned(), "x".to_owned())].into_iter().collect();
    let stamp = 1_795_611_622.609_769_6;

    publish_hash_cache(&path, &hashes, stamp).unwrap();
    publish_hash_cache(&path, &hashes, stamp).unwrap();

    assert_eq!(
        fs::read(&path).unwrap(),
        text_mode_bytes(&hash_cache_text(&hashes))
    );
    let modified = fs::metadata(&path).unwrap().modified().unwrap();
    assert_eq!(Some(modified), utime_instant(stamp));
    // Read back as Python reads it, the stamp is the same float.
    assert_eq!(python_mtime(&fs::metadata(&path).unwrap()), stamp);
    assert_eq!(fs::read_dir(path.parent().unwrap()).unwrap().count(), 1);
    // A cache whose folder is a file fails, leaving nothing behind.
    let blocked = scratch.0.join("file");
    fs::write(&blocked, "").unwrap();
    assert!(publish_hash_cache(&blocked.join(HASH_CACHE_FILENAME), &hashes, stamp).is_err());
}

#[test]
fn utime_rounds_nanoseconds_down_as_python_does() {
    let instant = |seconds: f64| {
        utime_instant(seconds)
            .unwrap()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
    };
    assert_eq!(instant(1.5).subsec_nanos(), 500_000_000);
    assert_eq!(instant(0.0).as_nanos(), 0);
    // 0.1 is a little over a tenth: floor keeps 100000000 ns.
    assert_eq!(instant(0.1).subsec_nanos(), 100_000_000);
    assert!(utime_instant(f64::NAN).is_none());
}

fn no_rules() -> IgnoreRules {
    IgnoreRules::new([""; 0], [""; 0])
}

fn write(root: &Path, rel: &str, text: &str) {
    let path = root.join(rel);
    fs::create_dir_all(path.parent().unwrap()).unwrap();
    fs::write(path, text).unwrap();
}

/// A repository synced once by the frame: its cache and folder times
/// published, the cache stamped now.
fn synced(scratch: &Scratch) -> (std::path::PathBuf, std::path::PathBuf, std::path::PathBuf) {
    let repo = scratch.0.join("repo");
    write(&repo, "a.py", "a");
    write(&repo, "pkg/b.py", "b");
    let state = scratch.0.join("state");
    let cache = state.join(HASH_CACHE_FILENAME);
    let mtimes = state.join(DIR_MTIMES_FILENAME);
    let engine = Engine::new(2).unwrap();
    let base = baseline(BaselineInputs {
        cache_path: &cache,
        force: false,
        single: false,
        cache_dead: false,
        project_mismatch: false,
        parser_changed: false,
        stale: BTreeSet::new(),
    });
    let changes = engine
        .changes(
            &base,
            ChangeInputs {
                repo: repo.as_os_str(),
                single: None,
                graph_paths: Some(HashSet::new()),
            },
            &no_rules(),
        )
        .unwrap();
    publish_hash_cache(&cache, changes.prepared.as_ref().unwrap(), now()).unwrap();
    save_dir_mtimes(&mtimes, changes.mtimes.as_ref().unwrap()).unwrap();
    (repo, cache, mtimes)
}

fn in_sync(repo: &Path, cache: &Path, mtimes: &Path) -> bool {
    Engine::new(2)
        .unwrap()
        .in_sync(
            &SyncInputs {
                repo: repo.as_os_str(),
                cache_path: cache,
                dir_mtimes_path: mtimes,
                discard_cache: false,
            },
            &no_rules(),
        )
        .unwrap()
}

#[test]
fn a_first_run_finds_every_file_new_and_records_each_folder() {
    let scratch = Scratch::new("state-first");
    let (repo, cache, mtimes) = synced(&scratch);

    let recorded = read_hash_cache(&cache).entries;
    assert_eq!(recorded.keys().collect::<Vec<_>>(), ["a.py", "pkg/b.py"]);
    assert_eq!(recorded.get("a.py"), md5_file(&repo.join("a.py")).as_ref());
    assert_eq!(
        read_dir_mtimes(&mtimes).keys().collect::<Vec<_>>(),
        [".", "pkg"]
    );
    assert!(in_sync(&repo, &cache, &mtimes));
}

#[test]
fn the_in_sync_check_sees_an_added_and_a_removed_file() {
    let scratch = Scratch::new("state-insync");
    let (repo, cache, mtimes) = synced(&scratch);

    write(&repo, "pkg/c.py", "c");
    assert!(!in_sync(&repo, &cache, &mtimes));
    fs::remove_file(repo.join("pkg/c.py")).unwrap();
    // The folder's mtime moved, but its listing is what was recorded.
    assert!(in_sync(&repo, &cache, &mtimes));
    fs::remove_file(repo.join("pkg/b.py")).unwrap();
    assert!(!in_sync(&repo, &cache, &mtimes));
}

#[test]
fn a_cached_hash_at_or_under_the_cache_mtime_is_kept_unread() {
    let scratch = Scratch::new("state-reuse");
    let (repo, cache, _mtimes) = synced(&scratch);
    // The cache vouches for a hash no read of the file would give.
    let mut forged = read_hash_cache(&cache).entries;
    forged.insert("a.py".to_owned(), "0".repeat(32));
    publish_hash_cache(&cache, &forged, now()).unwrap();
    let engine = Engine::new(2).unwrap();
    let base = baseline(BaselineInputs {
        cache_path: &cache,
        force: false,
        single: false,
        cache_dead: false,
        project_mismatch: false,
        parser_changed: false,
        stale: BTreeSet::new(),
    });

    let changes = engine
        .changes(
            &base,
            ChangeInputs {
                repo: repo.as_os_str(),
                single: None,
                graph_paths: Some(HashSet::new()),
            },
            &no_rules(),
        )
        .unwrap();

    assert_eq!(changes.hashes.get("a.py"), Some(&"0".repeat(32)));
    assert!(changes.changed.is_empty());
    assert!(changes.deleted.is_empty());
}
