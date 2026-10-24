//! JSON text exactly as Python's `json.dumps` writes it.
//!
//! The engine's writes store `json.dumps` output (or SQLite's rendering of
//! it, which keeps every string and number token verbatim), so the bulk
//! writer must produce the same characters: `ensure_ascii` escapes, with
//! lowercase hex and surrogate pairs above the BMP, as CPython's
//! `_json.c` (`ascii_escape_unichar`) spells them.

/// `s` as a JSON string literal with `json.dumps`' default `ensure_ascii`.
pub fn push_string(out: &mut String, s: &str) {
    fn push_unit(out: &mut String, unit: u32) {
        const HEX: &[u8; 16] = b"0123456789abcdef";
        out.push_str("\\u");
        for shift in [12, 8, 4, 0] {
            out.push(HEX[((unit >> shift) & 0xf) as usize] as char);
        }
    }
    out.push('"');
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{8}' => out.push_str("\\b"),
            '\u{c}' => out.push_str("\\f"),
            ' '..='~' => out.push(c),
            _ => {
                let code = c as u32;
                if code >= 0x10000 {
                    let v = code - 0x10000;
                    push_unit(out, 0xd800 | (v >> 10));
                    push_unit(out, 0xdc00 | (v & 0x3ff));
                } else {
                    push_unit(out, code);
                }
            }
        }
    }
    out.push('"');
}

#[cfg(test)]
mod tests {
    use super::push_string;

    fn text(s: &str) -> String {
        let mut out = String::new();
        push_string(&mut out, s);
        out
    }

    #[test]
    fn escapes_as_python_does() {
        assert_eq!(text("a\"b\\c/"), r#""a\"b\\c/""#);
        assert_eq!(text("\n\r\t\u{8}\u{c}\u{1}"), r#""\n\r\t\b\f\u0001""#);
        // DEL is outside json's printable range; so is everything non-ASCII.
        assert_eq!(text("\u{7f}\u{e9}\u{20ac}"), "\"\\u007f\\u00e9\\u20ac\"");
        assert_eq!(text("\u{1f600}"), "\"\\ud83d\\ude00\"");
    }
}
