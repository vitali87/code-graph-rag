use std::fs;
use std::path::PathBuf;

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

#[test]
fn zero_threads_means_one_per_core() {
    let cores = std::thread::available_parallelism().map_or(1, |n| n.get());
    assert_eq!(Engine::new(0).unwrap().threads(), cores);
    assert_eq!(Engine::new(3).unwrap().threads(), 3);
}
