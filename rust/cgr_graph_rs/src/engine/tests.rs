use std::fs;
use std::path::{Path, PathBuf};

use super::{md5_file, Engine};

/// A scratch directory under the system's temporary folder, removed when
/// dropped.
pub(super) struct Scratch(pub(super) PathBuf);

impl Scratch {
    pub(super) fn new(name: &str) -> Scratch {
        let dir = std::env::temp_dir().join(format!("cgr-engine-{name}-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        Scratch(dir)
    }
}

impl Drop for Scratch {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

#[test]
fn md5_matches_the_known_digests() {
    let scratch = Scratch::new("known");
    let empty = scratch.0.join("empty");
    let text = scratch.0.join("text");
    fs::write(&empty, b"").unwrap();
    fs::write(&text, b"The quick brown fox jumps over the lazy dog").unwrap();

    assert_eq!(
        md5_file(&empty).as_deref(),
        Some("d41d8cd98f00b204e9800998ecf8427e")
    );
    assert_eq!(
        md5_file(&text).as_deref(),
        Some("9e107d9d372bb6826bd81d3542a419d6")
    );
}

#[test]
fn md5_spans_more_than_one_read() {
    let scratch = Scratch::new("large");
    let large = scratch.0.join("large");
    // Three and a half chunks of a repeating pattern.
    let bytes: Vec<u8> = (0..(super::READ_CHUNK * 7 / 2))
        .map(|i| (i % 251) as u8)
        .collect();
    fs::write(&large, &bytes).unwrap();

    let whole = super::hex(&<md5::Md5 as md5::Digest>::digest(&bytes));
    assert_eq!(md5_file(&large), Some(whole));
}

#[test]
fn a_file_that_cannot_be_read_has_no_hash() {
    let scratch = Scratch::new("missing");
    assert_eq!(md5_file(&scratch.0.join("absent")), None);
    // A directory opens on some systems but never reads as a file.
    assert_eq!(md5_file(&scratch.0), None);
}

#[test]
fn results_keep_input_order_on_any_number_of_threads() {
    let scratch = Scratch::new("order");
    let paths: Vec<PathBuf> = (0..200)
        .map(|i| {
            let path = scratch.0.join(format!("f{i:03}"));
            // Sizes vary so threads finish out of order.
            fs::write(&path, vec![i as u8; (200 - i) * 97]).unwrap();
            path
        })
        .chain(std::iter::once(scratch.0.join("absent")))
        .collect();

    let one = Engine::new(1).unwrap().md5_files(&paths);
    for threads in [2, 3, 8, 0] {
        assert_eq!(
            Engine::new(threads).unwrap().md5_files(&paths),
            one,
            "{threads} threads"
        );
    }
    let expected: Vec<Option<String>> = paths.iter().map(|path| md5_file(path)).collect();
    assert_eq!(one, expected);
    assert_eq!(one.last(), Some(&None));
}

/// Batches on each side of `POOLED_FROM`, and one far past it.
const BATCH_SIZES: [usize; 6] = [0, 1, 15, 16, 17, 200];

#[test]
fn a_small_batch_runs_on_the_calling_thread_and_a_large_one_on_the_pool() {
    assert_eq!(super::POOLED_FROM, 16);
    let engine = Engine::new(2).unwrap();
    let pooled = Engine::always_pooled(2).unwrap();
    let on_pool = |_: &usize| rayon::current_thread_index().is_some();
    let work = |&i: &usize| format!("{i:04}:{}", i * 7 % 13);
    for size in BATCH_SIZES {
        let items: Vec<usize> = (0..size).collect();
        let shared = size >= super::POOLED_FROM;
        assert!(
            engine
                .map_ordered(&items, on_pool)
                .into_iter()
                .all(|on| on == shared),
            "{size} items"
        );
        assert!(pooled.map_ordered(&items, on_pool).into_iter().all(|on| on));
        assert_eq!(
            engine.map_ordered(&items, work),
            pooled.map_ordered(&items, work),
            "{size} items"
        );
        for limit in [0, size / 2, size] {
            let below = |&i: &usize| i < limit;
            assert_eq!(engine.all_of(&items, below), pooled.all_of(&items, below));
        }
    }
}

/// A repository whose batches are `size` long: that many files in the
/// root, or, `nested`, that many folders under it, a third of them
/// packages, each holding a file.
fn tree_of(root: &Path, size: usize, nested: bool) {
    fs::create_dir_all(root).unwrap();
    for i in 0..size {
        if !nested {
            fs::write(root.join(format!("r{i:03}.py")), vec![b'#'; i]).unwrap();
            continue;
        }
        let folder = root.join(format!("d{i:03}"));
        fs::create_dir_all(&folder).unwrap();
        fs::write(folder.join(format!("m{i}.py")), format!("x = {i}\n")).unwrap();
        if i % 3 == 0 {
            fs::write(folder.join("__init__.py"), b"").unwrap();
        }
    }
}

#[test]
fn every_answer_on_the_calling_thread_is_the_pool_s() {
    use std::collections::{BTreeSet, HashSet};

    use super::ignore::IgnoreRules;
    use super::merge::{FileSummary, NamingInputs, NamingRules};
    use super::state::{
        baseline, dir_mtimes_text, hash_cache_text, publish_hash_cache, save_dir_mtimes,
        BaselineInputs, ChangeInputs, Changes, SyncInputs, DIR_MTIMES_FILENAME,
        HASH_CACHE_FILENAME,
    };
    use super::structure::PathRules;

    let scratch = Scratch::new("pooled-or-not");
    let rules = IgnoreRules::new([""; 0], [""; 0]);
    let markers = ["__init__.py".to_owned()];
    let naming = NamingRules::new(
        vec![(".py".to_owned(), "python".to_owned())],
        &[],
        Vec::new(),
        ".d.",
        vec!["__init__.py".to_owned()],
        ".ipynb".to_owned(),
    );
    let engines = [Engine::new(2).unwrap(), Engine::always_pooled(2).unwrap()];
    for (size, nested) in BATCH_SIZES
        .into_iter()
        .flat_map(|size| [(size, false), (size, true)])
    {
        let root = scratch.0.join(format!("repo{size}-{nested}"));
        tree_of(&root, size, nested);
        let state = scratch.0.join(format!("state{size}-{nested}"));
        fs::create_dir_all(&state).unwrap();
        let cache = state.join(HASH_CACHE_FILENAME);
        let mtimes = state.join(DIR_MTIMES_FILENAME);
        let base = || {
            baseline(BaselineInputs {
                cache_path: &cache,
                force: false,
                single: false,
                cache_dead: false,
                project_mismatch: false,
                parser_changed: false,
                stale: BTreeSet::new(),
            })
        };
        let changes = |engine: &Engine| {
            let found = engine
                .changes(
                    &base(),
                    ChangeInputs {
                        repo: root.as_os_str(),
                        single: None,
                        graph_paths: Some(HashSet::new()),
                    },
                    &rules,
                )
                .unwrap();
            // Each run's own clock.
            Changes {
                watermark: 0.0,
                stamp: 0.0,
                ..found
            }
        };
        let answers: Vec<_> = engines
            .iter()
            .map(|engine| {
                let plan = engine.file_plan(root.as_os_str(), &rules).unwrap();
                let paths = plan.paths();
                let structure = engine
                    .structure(
                        root.as_os_str(),
                        "proj",
                        &markers,
                        &rules,
                        &PathRules::default(),
                    )
                    .unwrap();
                let rows = engine.file_rows(
                    &root,
                    "proj",
                    &structure.elements,
                    &paths,
                    &PathRules::default(),
                );
                let summaries = plan
                    .files
                    .iter()
                    .enumerate()
                    .map(|(i, file)| {
                        FileSummary::new(i, file.rel_path.clone(), "python".to_owned())
                    })
                    .collect();
                let index = engine
                    .merge(
                        summaries,
                        &NamingInputs {
                            root: &root,
                            project_name: "proj",
                            rules: &rules,
                            naming: &naming,
                            dot_suffix: false,
                        },
                    )
                    .unwrap();
                let found = changes(engine);
                // The state files' bytes, as the run would write them.
                let texts = (
                    found.prepared.as_ref().map(hash_cache_text),
                    found.mtimes.as_ref().map(dir_mtimes_text),
                );
                let hashes = engine.md5_files(&paths);
                (plan, hashes, structure, rows, index, found, texts)
            })
            .collect();
        assert_eq!(answers[0], answers[1], "{size} items, nested {nested}");
        // The in-sync check over the cache the run would publish.
        let first = changes(&engines[1]);
        publish_hash_cache(
            &cache,
            first.prepared.as_ref().unwrap(),
            super::state::now(),
        )
        .unwrap();
        save_dir_mtimes(&mtimes, first.mtimes.as_ref().unwrap()).unwrap();
        let sync = SyncInputs {
            repo: root.as_os_str(),
            cache_path: &cache,
            dir_mtimes_path: &mtimes,
            discard_cache: false,
        };
        let verdicts: Vec<bool> = engines
            .iter()
            .map(|engine| engine.in_sync(&sync, &rules).unwrap())
            .collect();
        assert_eq!(verdicts, [size > 0; 2], "{size} items, nested {nested}");
    }
}

#[test]
fn zero_threads_means_one_per_core() {
    let cores = std::thread::available_parallelism().map_or(1, |n| n.get());
    assert_eq!(Engine::new(0).unwrap().threads(), cores);
    assert_eq!(Engine::new(3).unwrap().threads(), 3);
}
