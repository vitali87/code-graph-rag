// SPDX-License-Identifier: MIT
//! Fuzz target: `json_text::push_string`, which must write a string exactly
//! as Python's `json.dumps` writes it with its default `ensure_ascii`.
//!
//! The bulk writer stores this text as node and relationship properties,
//! where names come from the repository being indexed, so any string can
//! reach it. The input is the string, up to 64 KiB, with each bad UTF-8
//! sequence replaced.
#![no_main]

use cgr_graph_rs::json_text::push_string;
use cgr_graph_rs_fuzz::require;
use libfuzzer_sys::fuzz_target;

const MAX_STRING_BYTES: usize = 64 * 1024;

fuzz_target!(|data: &[u8]| {
    let s = String::from_utf8_lossy(&data[..data.len().min(MAX_STRING_BYTES)]);
    let mut out = String::from("[");
    push_string(&mut out, &s);
    // It appends, and keeps what was there.
    // Mutant: in json_text.rs, replace `out` with the opening quote in
    // place of pushing it. Input: the empty string aborts.
    require!(
        out.starts_with('['),
        "push_string changed the text before it"
    );
    let literal = &out[1..];
    // `ensure_ascii`: the literal is printable ASCII between two quotes.
    // Mutant: in json_text.rs, push each character up to U+00FF as it is.
    // Input: b4 b4 b4 b4 7f a6 a9 07 aborts (DEL comes out raw).
    require!(
        literal.len() >= 2
            && literal.starts_with('"')
            && literal.ends_with('"')
            && literal.bytes().all(|b| (b' '..=b'~').contains(&b)),
        "{literal:?} is not printable ASCII between quotes"
    );
    // A JSON parser reads back the same string, and every escape is the
    // one `json.dumps` writes.
    // Mutant: in json_text.rs, push `\u{8}` as `\u0008` in place of `\b`.
    // Input: the byte 08 aborts. Mutant: make HEX uppercase. Input: the
    // byte c3 (read as U+FFFD) aborts. Mutant: build the low surrogate
    // from 0xd800. Input: f1 a3 a3 9f aborts. Mutant: push `\r` as `\n`.
    // Input: the byte 0d aborts.
    match decode(&literal[1..literal.len() - 1]) {
        Ok(decoded) => require!(
            decoded == *s,
            "{literal:?} decodes to {decoded:?}, not {s:?}"
        ),
        Err(why) => require!(false, "{literal:?} is not json.dumps text: {why}"),
    }
});

/// The characters `json.dumps` writes with a two-character escape, and the
/// escape letter of each.
const SHORT_ESCAPES: [(char, char); 7] = [
    ('"', '"'),
    ('\\', '\\'),
    ('\n', 'n'),
    ('\r', 'r'),
    ('\t', 't'),
    ('\u{8}', 'b'),
    ('\u{c}', 'f'),
];

/// The body of a JSON string literal, decoded; an error if any character
/// is not the form `json.dumps(ensure_ascii=True)` writes for it: printable
/// ASCII as it is, the seven short escapes, and `\u` with four lowercase
/// hex digits for every other character, as a surrogate pair above the
/// BMP.
fn decode(body: &str) -> Result<String, String> {
    let mut out = String::new();
    let mut chars = body.chars();
    while let Some(c) = chars.next() {
        if c != '\\' {
            if c == '"' {
                return Err("an unescaped quote".into());
            }
            out.push(c);
            continue;
        }
        let letter = chars.next().ok_or("a backslash at the end")?;
        if letter != 'u' {
            let &(c, _) = SHORT_ESCAPES
                .iter()
                .find(|&&(_, l)| l == letter)
                .ok_or_else(|| format!("the escape \\{letter}"))?;
            out.push(c);
            continue;
        }
        let unit = hex4(&mut chars)?;
        let c = match unit {
            0xd800..=0xdbff => {
                if chars.next() != Some('\\') || chars.next() != Some('u') {
                    return Err(format!("the high surrogate {unit:04x} is alone"));
                }
                let low = hex4(&mut chars)?;
                if !(0xdc00..=0xdfff).contains(&low) {
                    return Err(format!("{unit:04x} is followed by {low:04x}"));
                }
                let code = 0x10000 + ((unit - 0xd800) << 10) + (low - 0xdc00);
                char::from_u32(code).ok_or("a pair outside Unicode")?
            }
            0xdc00..=0xdfff => return Err(format!("the low surrogate {unit:04x} is alone")),
            _ => char::from_u32(unit).ok_or("not a scalar value")?,
        };
        if (' '..='~').contains(&c) || SHORT_ESCAPES.iter().any(|&(e, _)| e == c) {
            return Err(format!(
                "{c:?} has a \\u escape that json.dumps does not write"
            ));
        }
        out.push(c);
    }
    Ok(out)
}

fn hex4(chars: &mut std::str::Chars) -> Result<u32, String> {
    let mut unit = 0;
    for _ in 0..4 {
        let c = chars.next().ok_or("a short \\u escape")?;
        if !matches!(c, '0'..='9' | 'a'..='f') {
            return Err(format!("{c:?} in a \\u escape"));
        }
        unit = unit * 16 + c.to_digit(16).expect("a hex digit");
    }
    Ok(unit)
}
