// SPDX-License-Identifier: MIT
//! Fuzz target: `engine::state`'s readers of the sync's hash cache and
//! folder times, a copy of CPython's `json` scanner, and the writer whose
//! text they must read back.
//!
//! The files live in the user's state folder, where a crash, a disk that
//! filled, an older cgr or a person can leave anything, so any bytes reach
//! the readers. A file that cannot be used reads as no cache; a panic, a
//! hang, or a cache that does not survive being written and read again is
//! a bug. The input is the file, up to 64 KiB, read raw, then as a
//! document of JSON fragments, so the scanner's corners (escapes,
//! surrogates, numbers that end early, nesting) are reached quickly.
#![no_main]

use cgr_graph_rs::engine::state::{
    dir_mtimes_from_bytes, dir_mtimes_text, hash_cache_from_bytes, hash_cache_text, json,
};
use cgr_graph_rs_fuzz::{require, Input};
use libfuzzer_sys::fuzz_target;

const MAX_FILE_BYTES: usize = 64 * 1024;
const MAX_FRAGMENTS: usize = 64;
const FRAGMENTS: &[&str] = &[
    "{",
    "}",
    "[",
    "]",
    ",",
    ":",
    " ",
    "\n",
    "\"",
    "\\",
    "\"a.py\"",
    "\"x\"",
    "\\u",
    "d800",
    "dc00",
    "\\ud83d\\ude00",
    "\\udcff",
    "0",
    "1",
    "-",
    ".",
    "e",
    "E",
    "+",
    "1e400",
    "NaN",
    "Infinity",
    "-Infinity",
    "true",
    "false",
    "null",
    "\u{feff}",
    "é",
    "\t",
    "unreadable",
];

/// Each cache entry and folder time read back from the text written for it.
fn round_trips(bytes: &[u8]) {
    let cache = hash_cache_from_bytes(bytes);
    let written = hash_cache_text(&cache.entries);
    let again = hash_cache_from_bytes(written.as_bytes());
    // Mutant: in json.rs, drop the escape of `"` in names; a name holding
    // a quote then reads back as a refused document.
    require!(
        again.error.is_none() && again.entries == cache.entries,
        "the hash cache did not read back: {written:?}"
    );
    let mtimes = dir_mtimes_from_bytes(bytes);
    let written = dir_mtimes_text(&mtimes);
    let again = dir_mtimes_from_bytes(written.as_bytes());
    let bits = |entries: &cgr_graph_rs::engine::state::Entries<f64>| {
        entries
            .iter()
            .map(|(name, mtime)| (name.to_owned(), mtime.to_bits()))
            .collect::<Vec<_>>()
    };
    // NaN reads back as the canonical NaN, which `repr` cannot tell apart.
    let canonical = |entries: Vec<(String, u64)>| {
        entries
            .into_iter()
            .map(|(name, bits)| {
                let nan = f64::from_bits(bits).is_nan();
                (name, if nan { f64::NAN.to_bits() } else { bits })
            })
            .collect::<Vec<_>>()
    };
    require!(
        canonical(bits(&again)) == canonical(bits(&mtimes)),
        "the folder times did not read back: {written:?}"
    );
}

fuzz_target!(|data: &[u8]| {
    let mut input = Input::new(data);
    let raw = &data[..data.len().min(MAX_FILE_BYTES)];
    round_trips(raw);
    let mut document = String::new();
    for _ in 0..input.below(MAX_FRAGMENTS + 1) {
        let fragment: &&str = input.pick(FRAGMENTS);
        document.push_str(fragment);
    }
    round_trips(document.as_bytes());
    // Every finite float reads back from its `repr` exactly.
    let value = input.f64();
    if value.is_finite() {
        let repr = json::float_repr(value);
        require!(
            repr.parse::<f64>().map(f64::to_bits) == Ok(value.to_bits()),
            "{value:e} wrote {repr}"
        );
    }
});
