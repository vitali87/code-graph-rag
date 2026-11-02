// SPDX-License-Identifier: MIT
//! Fuzz target: `formats::xml`, the reader for the XML project files
//! (`.csproj`, `.slnx`) cgr finds in the repositories it indexes.
//!
//! The bytes are untrusted, so any document can reach it. An error is an
//! expected outcome; a panic, a stack overflow or a hang is a bug, and so
//! is an answer that contradicts itself. The input is the document, up to
//! 64 KiB.
#![no_main]

use cgr_graph_rs::formats::xml::{elements, find_all, Error};
use cgr_graph_rs_fuzz::require;
use libfuzzer_sys::fuzz_target;

const MAX_DOCUMENT_BYTES: usize = 64 * 1024;
const UTF8_BOM: &[u8] = b"\xEF\xBB\xBF";

fuzz_target!(|data: &[u8]| {
    let doc = &data[..data.len().min(MAX_DOCUMENT_BYTES)];
    let found = match elements(doc) {
        Ok(found) => found,
        Err(Error::EntityDeclared { name }) => {
            require!(!name.is_empty(), "an entity refused with no name");
            return;
        }
        Err(Error::Syntax { line, .. }) => {
            require!(line >= 1, "an error on line {line}");
            return;
        }
    };
    require!(!found.is_empty(), "an answer with no root element");
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
        find_all(doc, name).as_ref() == Ok(&expected),
        "find_all disagrees with elements for {name:?}"
    );
    // A UTF-8 byte-order mark is skipped, not read, in a document that is
    // UTF-8 already (one declaring another encoding would contradict it).
    let utf8 = std::str::from_utf8(doc).is_ok()
        && !doc.contains(&0)
        && !doc.windows(8).any(|w| w == b"encoding");
    if utf8 && !doc.starts_with(UTF8_BOM) {
        let marked = [UTF8_BOM, doc].concat();
        require!(
            elements(&marked).as_ref() == Ok(&found),
            "a byte-order mark changed the answer"
        );
    }
});
