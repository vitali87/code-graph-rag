//! TOML text exactly as `tomli_w.dumps` writes it with its defaults (four
//! space indent, no multi-line strings), for the values cgr's workspace files
//! hold: strings, booleans, integers, arrays and tables.
//!
//! The layout follows tomli-w's: a table's plain values first, then its
//! sub-tables; an array of tables stays inline (`[\n    { ... },\n]`)
//! unless one of its tables renders longer than a 100-character line, in
//! which case every one becomes a `[[name]]` section.

const INDENT: &str = "    ";
const MAX_LINE_LENGTH: usize = 100;

/// A value TOML can hold, as far as this writer goes.
#[derive(Clone, Debug, PartialEq)]
pub enum Value {
    Bool(bool),
    Int(i64),
    Str(String),
    Array(Vec<Value>),
    Table(Vec<(String, Value)>),
}

/// A sub-table to write as its own section: key, entries, and whether it
/// is one of an array of tables.
type Section<'a> = (&'a str, &'a [(String, Value)], bool);

/// The document whose top level is `table`.
pub fn dumps(table: &[(String, Value)]) -> String {
    let mut out = String::new();
    table_section(&mut out, table, "", false);
    out
}

fn table_section(out: &mut String, table: &[(String, Value)], name: &str, inside_aot: bool) {
    let mut literals = Vec::new();
    let mut sections: Vec<Section> = Vec::new();
    for (key, value) in table {
        match value {
            Value::Table(sub) => sections.push((key, sub, false)),
            Value::Array(items) if is_array_of_tables(items) && !items.iter().all(fits_inline) => {
                sections.extend(items.iter().filter_map(|item| match item {
                    Value::Table(sub) => Some((key.as_str(), sub.as_slice(), true)),
                    _ => None,
                }));
            }
            _ => literals.push((key, value)),
        }
    }

    let mut written = false;
    if inside_aot || (!name.is_empty() && (!literals.is_empty() || sections.is_empty())) {
        written = true;
        let (open, close) = if inside_aot { ("[[", "]]") } else { ("[", "]") };
        out.push_str(open);
        out.push_str(name);
        out.push_str(close);
        out.push('\n');
    }
    for (key, value) in &literals {
        written = true;
        push_key(out, key);
        out.push_str(" = ");
        push_literal(out, value, 0);
        out.push('\n');
    }
    for (key, sub, in_aot) in sections {
        if written {
            out.push('\n');
        }
        written = true;
        let mut child = String::from(name);
        if !name.is_empty() {
            child.push('.');
        }
        push_key(&mut child, key);
        table_section(out, sub, &child, in_aot);
    }
}

fn is_array_of_tables(items: &[Value]) -> bool {
    !items.is_empty() && items.iter().all(|item| matches!(item, Value::Table(_)))
}

fn fits_inline(item: &Value) -> bool {
    let mut rendered = String::from(INDENT);
    push_literal(&mut rendered, item, 0);
    rendered.push(',');
    rendered.chars().count() <= MAX_LINE_LENGTH && !rendered.contains('\n')
}

fn push_literal(out: &mut String, value: &Value, nest_level: usize) {
    match value {
        Value::Bool(b) => out.push_str(if *b { "true" } else { "false" }),
        Value::Int(i) => out.push_str(&i.to_string()),
        Value::Str(s) => push_string(out, s),
        Value::Array(items) if items.is_empty() => out.push_str("[]"),
        Value::Array(items) => {
            out.push_str("[\n");
            for item in items {
                for _ in 0..=nest_level {
                    out.push_str(INDENT);
                }
                push_literal(out, item, nest_level + 1);
                out.push_str(",\n");
            }
            for _ in 0..nest_level {
                out.push_str(INDENT);
            }
            out.push(']');
        }
        Value::Table(entries) if entries.is_empty() => out.push_str("{}"),
        Value::Table(entries) => {
            out.push_str("{ ");
            for (i, (key, value)) in entries.iter().enumerate() {
                if i > 0 {
                    out.push_str(", ");
                }
                push_key(out, key);
                out.push_str(" = ");
                push_literal(out, value, 0);
            }
            out.push_str(" }");
        }
    }
}

fn push_key(out: &mut String, key: &str) {
    let bare = !key.is_empty()
        && key
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_');
    if bare {
        out.push_str(key);
    } else {
        push_string(out, key);
    }
}

/// A basic string: quotes, backslashes and control characters other than
/// tab escaped, the short escapes where TOML has one, `\uXXXX` (lowercase
/// hex) otherwise.
fn push_string(out: &mut String, s: &str) {
    out.push('"');
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\u{8}' => out.push_str("\\b"),
            '\n' => out.push_str("\\n"),
            '\u{c}' => out.push_str("\\f"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push('\t'),
            '\u{0}'..='\u{1f}' | '\u{7f}' => out.push_str(&format!("\\u{:04x}", c as u32)),
            _ => out.push(c),
        }
    }
    out.push('"');
}
