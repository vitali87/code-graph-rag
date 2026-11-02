// SPDX-License-Identifier: MIT
//! Fuzz target: `formats::xml`, the reader for the XML project files
//! (`.csproj`, `.slnx`) cgr finds in the repositories it indexes.
//!
//! The bytes are untrusted, so any document can reach it. Declining (None)
//! is an expected outcome, and hands the document to the Python parser; a
//! panic, a stack overflow or a hang is a bug, and so is an answer that
//! contradicts itself. The input is the document, up to 64 KiB.
#![no_main]

use cgr_graph_rs::formats::xml::{elements, find_all};
use cgr_graph_rs_fuzz::require;
use libfuzzer_sys::fuzz_target;

const MAX_DOCUMENT_BYTES: usize = 64 * 1024;
const UTF8_BOM: &[u8] = b"\xEF\xBB\xBF";

fuzz_target!(|data: &[u8]| {
    let doc = &data[..data.len().min(MAX_DOCUMENT_BYTES)];
    let Some(found) = elements(doc) else {
        return;
    };
    // An answer comes only for a document with a root element and no DTD:
    // a DTD can declare entities, and those are the Python side's to refuse.
    require!(!found.is_empty(), "an answer with no root element");
    require!(
        !doc.windows(9).any(|w| w == b"<!DOCTYPE"),
        "answered for a document with a DTD"
    );
    for element in &found {
        require!(!element.name.is_empty(), "an element with no name");
        // The namespace declarations are consumed, as ElementTree's are.
        require!(
            element
                .attributes
                .iter()
                .all(|(key, _)| key != "xmlns" && !key.starts_with("xmlns:")),
            "a namespace declaration reported as an attribute"
        );
    }
    // `find_all` is `elements` filtered by name, in the same order.
    let name = &found[0].name;
    let expected: Vec<_> = found
        .iter()
        .filter(|element| &element.name == name)
        .map(|element| element.attributes.clone())
        .collect();
    require!(
        find_all(doc, name).as_ref() == Some(&expected),
        "find_all disagrees with elements for {name:?}"
    );
    // A leading byte-order mark is skipped, not read.
    if !doc.starts_with(UTF8_BOM) {
        let marked = [UTF8_BOM, doc].concat();
        require!(
            elements(&marked).as_ref() == Some(&found),
            "a byte-order mark changed the answer"
        );
    }
});
