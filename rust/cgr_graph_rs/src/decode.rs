//! The executor's result decoding, for a whole result set in one call.
//!
//! `executor.decode` turns each SQLite value into its Python result by the
//! column's decode kind: `raw` passes it through, `bool` takes its truth,
//! `null` is None, and `json` parses JSON text with `json.loads` (nodes and
//! relationships arrive as JSON objects built in SQL). This decodes every
//! row the same way without a Python call per value.
//!
//! JSON is parsed exactly as `json.loads` parses it: the same number rules
//! (an int unless a fraction or exponent follows; `NaN` and `Infinity`
//! accepted), duplicate keys keeping the first position and the last value,
//! only ` \t\n\r` as whitespace. Whatever this parser would have to judge
//! differently, or cannot represent, is handed to `json.loads` itself, so
//! the value, or the error, is Python's: malformed text, a lone surrogate
//! escape (a Python str holds it, a Rust one cannot), an integer beyond 64
//! bits (subject to Python's digit limit), and nesting deeper than `MAX_DEPTH`.

use std::collections::HashMap;

use pyo3::prelude::*;
use pyo3::types::{PyBool, PyDict, PyFloat, PyList, PyString, PyTuple};

/// The decode kinds as the caller (native.py) numbers them; any other
/// number, `raw` (0) among them, passes the value through.
pub const KIND_BOOL: u8 = 1;
pub const KIND_JSON: u8 = 2;
pub const KIND_NULL: u8 = 3;

/// Deeper than this, `json.loads` decides (it may raise RecursionError).
const MAX_DEPTH: usize = 256;

/// The parser met something `json.loads` must judge.
struct Defer;

type Parsed<'py> = Result<Bound<'py, PyAny>, Defer>;

struct Parser<'a, 'py> {
    py: Python<'py>,
    text: &'a [u8],
    at: usize,
    depth: usize,
    keys: &'a mut HashMap<String, Bound<'py, PyString>>,
}

fn is_ws(b: u8) -> bool {
    matches!(b, b' ' | b'\t' | b'\n' | b'\r')
}

fn is_digit(b: u8) -> bool {
    b.is_ascii_digit()
}

impl<'a, 'py> Parser<'a, 'py> {
    fn peek(&self) -> Option<u8> {
        self.text.get(self.at).copied()
    }

    fn skip_ws(&mut self) {
        while self.peek().is_some_and(is_ws) {
            self.at += 1;
        }
    }

    fn literal(&mut self, word: &[u8]) -> bool {
        if self.text[self.at..].starts_with(word) {
            self.at += word.len();
            true
        } else {
            false
        }
    }

    fn document(&mut self) -> Parsed<'py> {
        self.skip_ws();
        let value = self.value()?;
        self.skip_ws();
        if self.at == self.text.len() {
            Ok(value)
        } else {
            Err(Defer)
        }
    }

    fn value(&mut self) -> Parsed<'py> {
        let py = self.py;
        match self.peek().ok_or(Defer)? {
            b'{' => self.object(),
            b'[' => self.array(),
            b'"' => {
                self.at += 1;
                Ok(self.string()?.into_any())
            }
            b'n' if self.literal(b"null") => Ok(py.None().into_bound(py)),
            b't' if self.literal(b"true") => Ok(PyBool::new(py, true).to_owned().into_any()),
            b'f' if self.literal(b"false") => Ok(PyBool::new(py, false).to_owned().into_any()),
            b'N' if self.literal(b"NaN") => Ok(PyFloat::new(py, f64::NAN).into_any()),
            b'I' if self.literal(b"Infinity") => Ok(PyFloat::new(py, f64::INFINITY).into_any()),
            b'-' if self.literal(b"-Infinity") => {
                Ok(PyFloat::new(py, f64::NEG_INFINITY).into_any())
            }
            b'-' | b'0'..=b'9' => self.number(),
            _ => Err(Defer),
        }
    }

    fn enter(&mut self) -> Result<(), Defer> {
        self.at += 1;
        self.depth += 1;
        if self.depth > MAX_DEPTH {
            return Err(Defer);
        }
        self.skip_ws();
        Ok(())
    }

    fn object(&mut self) -> Parsed<'py> {
        self.enter()?;
        let dict = PyDict::new(self.py);
        if self.peek() == Some(b'}') {
            self.at += 1;
            self.depth -= 1;
            return Ok(dict.into_any());
        }
        loop {
            if self.peek() != Some(b'"') {
                return Err(Defer);
            }
            self.at += 1;
            let key = self.key()?;
            self.skip_ws();
            if self.peek() != Some(b':') {
                return Err(Defer);
            }
            self.at += 1;
            self.skip_ws();
            let value = self.value()?;
            dict.set_item(key, value).map_err(|_| Defer)?;
            self.skip_ws();
            match self.peek() {
                Some(b',') => {
                    self.at += 1;
                    self.skip_ws();
                }
                Some(b'}') => {
                    self.at += 1;
                    self.depth -= 1;
                    return Ok(dict.into_any());
                }
                _ => return Err(Defer),
            }
        }
    }

    fn array(&mut self) -> Parsed<'py> {
        self.enter()?;
        let mut items: Vec<Bound<'py, PyAny>> = Vec::new();
        if self.peek() == Some(b']') {
            self.at += 1;
            self.depth -= 1;
            return Ok(PyList::empty(self.py).into_any());
        }
        loop {
            items.push(self.value()?);
            self.skip_ws();
            match self.peek() {
                Some(b',') => {
                    self.at += 1;
                    self.skip_ws();
                }
                Some(b']') => {
                    self.at += 1;
                    self.depth -= 1;
                    return PyList::new(self.py, items)
                        .map(Bound::into_any)
                        .map_err(|_| Defer);
                }
                _ => return Err(Defer),
            }
        }
    }

    /// An object key, shared across the result set as `json.loads` shares
    /// it within one document.
    fn key(&mut self) -> Result<Bound<'py, PyString>, Defer> {
        let start = self.at;
        let raw = self.raw_string()?;
        if let Some(raw) = raw {
            if let Some(key) = self.keys.get(raw) {
                return Ok(key.clone());
            }
            let key = PyString::new(self.py, raw);
            self.keys.insert(raw.to_string(), key.clone());
            return Ok(key);
        }
        self.at = start;
        self.string()
    }

    /// A string with no escapes, borrowed from the text (None when it has
    /// escapes and must be decoded); `at` moves past its closing quote.
    fn raw_string(&mut self) -> Result<Option<&'a str>, Defer> {
        let text: &'a [u8] = self.text;
        let start = self.at;
        loop {
            match self.peek().ok_or(Defer)? {
                b'"' => {
                    self.at += 1;
                    // Ends on an ASCII quote, so a UTF-8 boundary of valid text.
                    let raw = std::str::from_utf8(&text[start..self.at - 1]).map_err(|_| Defer)?;
                    return Ok(Some(raw));
                }
                b'\\' => return Ok(None),
                // `json.loads` is strict: a raw control character is an error.
                b if b < 0x20 => return Err(Defer),
                _ => self.at += 1,
            }
        }
    }

    fn string(&mut self) -> Result<Bound<'py, PyString>, Defer> {
        let start = self.at;
        if let Some(raw) = self.raw_string()? {
            return Ok(PyString::new(self.py, raw));
        }
        self.at = start;
        let mut out: Vec<u8> = Vec::new();
        loop {
            let b = self.peek().ok_or(Defer)?;
            self.at += 1;
            match b {
                b'"' => break,
                b'\\' => {
                    let escape = self.peek().ok_or(Defer)?;
                    self.at += 1;
                    match escape {
                        b'"' => out.push(b'"'),
                        b'\\' => out.push(b'\\'),
                        b'/' => out.push(b'/'),
                        b'b' => out.push(0x08),
                        b'f' => out.push(0x0c),
                        b'n' => out.push(b'\n'),
                        b'r' => out.push(b'\r'),
                        b't' => out.push(b'\t'),
                        b'u' => {
                            let c = self.unicode_escape()?;
                            let mut buf = [0u8; 4];
                            out.extend_from_slice(c.encode_utf8(&mut buf).as_bytes());
                        }
                        _ => return Err(Defer),
                    }
                }
                b if b < 0x20 => return Err(Defer),
                b => out.push(b),
            }
        }
        let text = String::from_utf8(out).map_err(|_| Defer)?;
        Ok(PyString::new(self.py, &text))
    }

    fn hex4(&mut self) -> Result<u32, Defer> {
        let digits = self.text.get(self.at..self.at + 4).ok_or(Defer)?;
        let mut value = 0u32;
        for &d in digits {
            // Each must be a hex digit; `from_str_radix` would also take a sign.
            value = value * 16 + (d as char).to_digit(16).ok_or(Defer)?;
        }
        self.at += 4;
        Ok(value)
    }

    /// The character of a `\uXXXX` escape (after the `u`), joining a
    /// surrogate pair; a lone surrogate is Python's to represent.
    fn unicode_escape(&mut self) -> Result<char, Defer> {
        let first = self.hex4()?;
        if (0xd800..=0xdbff).contains(&first) {
            if self.text[self.at..].starts_with(b"\\u") {
                self.at += 2;
                let second = self.hex4()?;
                if (0xdc00..=0xdfff).contains(&second) {
                    let joined = 0x10000 + ((first - 0xd800) << 10) + (second - 0xdc00);
                    return char::from_u32(joined).ok_or(Defer);
                }
            }
            return Err(Defer);
        }
        char::from_u32(first).ok_or(Defer)
    }

    /// `_json._match_number_unicode`, step for step.
    fn number(&mut self) -> Parsed<'py> {
        let text = self.text;
        let start = self.at;
        let end = text.len();
        let mut at = start;
        if text[at] == b'-' {
            at += 1;
            if at >= end {
                return Err(Defer);
            }
        }
        match text[at] {
            b'1'..=b'9' => {
                at += 1;
                while at < end && is_digit(text[at]) {
                    at += 1;
                }
            }
            b'0' => at += 1,
            _ => return Err(Defer),
        }
        let mut is_float = false;
        if at + 1 < end && text[at] == b'.' && is_digit(text[at + 1]) {
            is_float = true;
            at += 2;
            while at < end && is_digit(text[at]) {
                at += 1;
            }
        }
        if at + 1 < end && (text[at] == b'e' || text[at] == b'E') {
            let e_start = at;
            at += 1;
            if at + 1 < end && (text[at] == b'-' || text[at] == b'+') {
                at += 1;
            }
            while at < end && is_digit(text[at]) {
                at += 1;
            }
            if is_digit(text[at - 1]) {
                is_float = true;
            } else {
                at = e_start;
            }
        }
        self.at = at;
        // The span holds only ASCII digits, signs, '.', 'e' and 'E'.
        let span = std::str::from_utf8(&text[start..at]).map_err(|_| Defer)?;
        if is_float {
            let value: f64 = span.parse().map_err(|_| Defer)?;
            Ok(PyFloat::new(self.py, value).into_any())
        } else {
            let value: i64 = span.parse().map_err(|_| Defer)?;
            value
                .into_pyobject(self.py)
                .map(Bound::into_any)
                .map_err(|_| Defer)
        }
    }
}

fn decode_json<'py>(
    value: &Bound<'py, PyAny>,
    loads: &Bound<'py, PyAny>,
    keys: &mut HashMap<String, Bound<'py, PyString>>,
) -> PyResult<Bound<'py, PyAny>> {
    let Ok(text) = value.cast::<PyString>() else {
        return Ok(value.clone());
    };
    if let Ok(utf8) = text.to_str() {
        let mut parser = Parser {
            py: value.py(),
            text: utf8.as_bytes(),
            at: 0,
            depth: 0,
            keys,
        };
        if let Ok(parsed) = parser.document() {
            return Ok(parsed);
        }
    }
    loads.call1((value,))
}

/// Every row of a result, each value decoded by its column's kind.
///
/// `loads` is `json.loads`, for the values this parser hands back to it.
#[pyfunction]
pub fn decode_rows<'py>(
    py: Python<'py>,
    rows: &Bound<'py, PyList>,
    kinds: Vec<u8>,
    loads: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyList>> {
    let mut keys: HashMap<String, Bound<'py, PyString>> = HashMap::new();
    let mut out: Vec<Bound<'py, PyTuple>> = Vec::with_capacity(rows.len());
    for row in rows.iter() {
        let row = row.cast_into::<PyTuple>()?;
        let mut values: Vec<Bound<'py, PyAny>> = Vec::with_capacity(kinds.len());
        // `zip` in the Python decoder stops at the shorter of the two.
        for (value, &kind) in row.iter().zip(&kinds) {
            let decoded = if value.is_none() {
                value
            } else {
                match kind {
                    KIND_BOOL => PyBool::new(py, value.is_truthy()?).to_owned().into_any(),
                    KIND_JSON => decode_json(&value, loads, &mut keys)?,
                    KIND_NULL => py.None().into_bound(py),
                    _ => value,
                }
            };
            values.push(decoded);
        }
        out.push(PyTuple::new(py, values)?);
    }
    PyList::new(py, out)
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(decode_rows, m)?)
}
