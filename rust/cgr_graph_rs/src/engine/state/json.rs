//! The two state files' JSON, as Python's `json` module reads and writes it.
//!
//! **Reading** copies CPython's scanner (`Modules/_json.c`), which
//! `json.loads` runs, rule for rule, so a file is accepted or refused as the
//! sync's own reader accepts or refuses it, and decodes to the same values:
//!
//! - whitespace is only space, tab, line feed and carriage return; a
//!   byte-order mark is refused, and so is anything after the value;
//! - `NaN`, `Infinity` and `-Infinity` are numbers;
//! - a raw control character in a string is refused; a `\uXXXX` escape of a
//!   high surrogate joins a low one that follows, and any other surrogate
//!   stays alone, which no Rust string holds, so such a string is `None`;
//! - a number with a fraction or an exponent is a float, rounded correctly
//!   (`f64::from_str`, as `float()`), overflowing to an infinity; any other
//!   number is an int, refused past 4300 digits, as `int()` refuses it;
//! - an object's members are kept in order, duplicates included: the
//!   caller folds them as a `dict` does, the first place and the last value.
//!
//! The scanner keeps a stack of its own rather than recursing, so no
//! document can exhaust the thread's stack. Python refuses nesting past its
//! recursion limit, a depth that depends on its version and on the stack
//! of the call; the scanner accepts any depth. Neither file cgr writes nests.
//!
//! **Writing** produces `json.dump`'s text: `ensure_ascii` strings
//! (`crate::json_text`), `float.__repr__` numbers, and the hash cache's
//! `indent=2` layout.

use crate::json_text::push_string;

/// `sys.get_int_max_str_digits()`'s default: `int()` refuses a longer
/// string of digits, so `json.loads` refuses the document.
pub const INT_MAX_STR_DIGITS: usize = 4300;

/// A decoded string; `None` when it holds a lone surrogate.
pub type Text = Option<String>;

/// A member's value, as far as the state files' readers look at it.
#[derive(Debug, Clone, PartialEq)]
pub enum Value {
    Str(Text),
    /// An int as `float()` converts it; `None` when that overflows.
    Int(Option<f64>),
    Float(f64),
    /// `True` and `False`, which are ints to `isinstance`.
    Bool(bool),
    /// `null`, an array or an object.
    Other,
}

/// Why a document is refused, and where.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Error {
    pub what: &'static str,
    pub at: usize,
}

impl std::fmt::Display for Error {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{} at byte {}", self.what, self.at)
    }
}

impl std::error::Error for Error {}

const EXPECTING_VALUE: &str = "Expecting value";
const EXPECTING_NAME: &str = "Expecting property name enclosed in double quotes";
const EXPECTING_COLON: &str = "Expecting ':' delimiter";
const EXPECTING_COMMA: &str = "Expecting ',' delimiter";
const EXTRA_DATA: &str = "Extra data";
const UNTERMINATED: &str = "Unterminated string";
const CONTROL_CHARACTER: &str = "Invalid control character";
const INVALID_ESCAPE: &str = "Invalid \\escape";
const INVALID_UNICODE_ESCAPE: &str = "Invalid \\uXXXX escape";
const TOO_MANY_DIGITS: &str = "Exceeds the limit for integer string conversion";

/// The object `text` holds: its members in document order, or `Ok(None)`
/// for a document that is JSON but no object.
pub fn object_members(text: &str) -> Result<Option<Vec<(Text, Value)>>, Error> {
    let mut scanner = Scanner {
        s: text.as_bytes(),
        i: 0,
    };
    scanner.whitespace();
    let members = if scanner.peek() == Some(b'{') {
        Some(scanner.top_object()?)
    } else {
        scanner.value(false)?;
        None
    };
    scanner.whitespace();
    if scanner.i != scanner.s.len() {
        return Err(scanner.error(EXTRA_DATA));
    }
    Ok(members)
}

struct Scanner<'a> {
    s: &'a [u8],
    i: usize,
}

#[derive(Clone, Copy)]
enum Step {
    /// At the start of a value.
    Value,
    /// Just past a value, inside the innermost open container.
    AfterValue,
}

impl Scanner<'_> {
    fn peek(&self) -> Option<u8> {
        self.s.get(self.i).copied()
    }

    fn error(&self, what: &'static str) -> Error {
        Error { what, at: self.i }
    }

    fn whitespace(&mut self) {
        while matches!(self.peek(), Some(b' ' | b'\t' | b'\n' | b'\r')) {
            self.i += 1;
        }
    }

    fn top_object(&mut self) -> Result<Vec<(Text, Value)>, Error> {
        self.i += 1;
        self.whitespace();
        let mut members = Vec::new();
        if self.peek() == Some(b'}') {
            self.i += 1;
            return Ok(members);
        }
        loop {
            let name = self.member_name(true)?;
            let value = self.value(true)?;
            members.push((name, value));
            self.whitespace();
            match self.peek() {
                Some(b'}') => {
                    self.i += 1;
                    return Ok(members);
                }
                Some(b',') => {
                    self.i += 1;
                    self.whitespace();
                }
                _ => return Err(self.error(EXPECTING_COMMA)),
            }
        }
    }

    /// A member's name and its `:`, with the whitespace after each.
    fn member_name(&mut self, keep: bool) -> Result<Text, Error> {
        if self.peek() != Some(b'"') {
            return Err(self.error(EXPECTING_NAME));
        }
        let name = self.string(keep)?;
        self.whitespace();
        if self.peek() != Some(b':') {
            return Err(self.error(EXPECTING_COLON));
        }
        self.i += 1;
        self.whitespace();
        Ok(name)
    }

    /// One value; a string is decoded only when `keep` asks for it.
    fn value(&mut self, keep: bool) -> Result<Value, Error> {
        match self.peek() {
            Some(b'{' | b'[') => {
                self.container()?;
                Ok(Value::Other)
            }
            _ => self.scalar(keep),
        }
    }

    /// An array or an object, checked and passed over, on a stack of its own.
    fn container(&mut self) -> Result<(), Error> {
        let mut open: Vec<u8> = Vec::new();
        let mut step = Step::Value;
        loop {
            match step {
                Step::Value => match self.peek() {
                    Some(kind @ (b'{' | b'[')) => {
                        self.i += 1;
                        self.whitespace();
                        if self.peek() == Some(closer(kind)) {
                            self.i += 1;
                            step = Step::AfterValue;
                        } else {
                            open.push(kind);
                            if kind == b'{' {
                                self.member_name(false)?;
                            }
                        }
                    }
                    _ => {
                        self.scalar(false)?;
                        step = Step::AfterValue;
                    }
                },
                Step::AfterValue => {
                    let Some(&kind) = open.last() else {
                        return Ok(());
                    };
                    self.whitespace();
                    match self.peek() {
                        Some(c) if c == closer(kind) => {
                            self.i += 1;
                            open.pop();
                        }
                        Some(b',') => {
                            self.i += 1;
                            self.whitespace();
                            if kind == b'{' {
                                self.member_name(false)?;
                            }
                            step = Step::Value;
                        }
                        _ => return Err(self.error(EXPECTING_COMMA)),
                    }
                }
            }
        }
    }

    fn scalar(&mut self, keep: bool) -> Result<Value, Error> {
        let rest = &self.s[self.i..];
        let literal = |word: &[u8]| rest.starts_with(word);
        let (value, len) = match self.peek() {
            Some(b'"') => return Ok(Value::Str(self.string(keep)?)),
            Some(b'n') if literal(b"null") => (Value::Other, 4),
            Some(b't') if literal(b"true") => (Value::Bool(true), 4),
            Some(b'f') if literal(b"false") => (Value::Bool(false), 5),
            Some(b'N') if literal(b"NaN") => (Value::Float(f64::NAN), 3),
            Some(b'I') if literal(b"Infinity") => (Value::Float(f64::INFINITY), 8),
            Some(b'-') if literal(b"-Infinity") => (Value::Float(f64::NEG_INFINITY), 9),
            _ => return self.number(),
        };
        self.i += len;
        Ok(value)
    }

    /// `_match_number_unicode`: an optional `-`, an integer part with no
    /// leading zero, then a fraction and an exponent, each read only when
    /// complete, so `1.` and `1e` end the number before the `.` or `e`.
    fn number(&mut self) -> Result<Value, Error> {
        let s = self.s;
        let start = self.i;
        let mut i = start;
        let digit = |at: usize| s.get(at).is_some_and(u8::is_ascii_digit);
        if s.get(i) == Some(&b'-') {
            i += 1;
        }
        match s.get(i) {
            Some(b'1'..=b'9') => {
                i += 1;
                while digit(i) {
                    i += 1;
                }
            }
            Some(b'0') => i += 1,
            _ => return Err(self.error(EXPECTING_VALUE)),
        }
        let mut is_float = false;
        if s.get(i) == Some(&b'.') && digit(i + 1) {
            is_float = true;
            i += 2;
            while digit(i) {
                i += 1;
            }
        }
        if matches!(s.get(i), Some(b'e' | b'E')) && i + 1 < s.len() {
            let exponent = i;
            i += 1;
            if matches!(s.get(i), Some(b'-' | b'+')) && i + 1 < s.len() {
                i += 1;
            }
            while digit(i) {
                i += 1;
            }
            if s[i - 1].is_ascii_digit() {
                is_float = true;
            } else {
                i = exponent;
            }
        }
        self.i = i;
        // Only ASCII bytes were taken, so this slices whole characters.
        let token = std::str::from_utf8(&s[start..i]).unwrap_or_default();
        if is_float {
            return Ok(Value::Float(token.parse().unwrap_or(f64::NAN)));
        }
        let digits = token.strip_prefix('-').unwrap_or(token);
        if digits.len() > INT_MAX_STR_DIGITS {
            return Err(Error {
                what: TOO_MANY_DIGITS,
                at: start,
            });
        }
        // `float(int(token))`: rounded correctly, `-0` is 0, and a value
        // no float holds overflows.
        let as_float: f64 = token.parse().unwrap_or(f64::NAN);
        Ok(Value::Int(if as_float.is_infinite() {
            None
        } else if as_float == 0.0 {
            Some(0.0)
        } else {
            Some(as_float)
        }))
    }

    /// `scanstring_unicode` in strict mode, from the opening quote.
    fn string(&mut self, keep: bool) -> Result<Text, Error> {
        let s = self.s;
        let begin = self.i;
        self.i += 1;
        let mut out = String::new();
        let mut lone = false;
        loop {
            let chunk = self.i;
            while let Some(&c) = s.get(self.i) {
                if c == b'"' || c == b'\\' || c < 0x20 {
                    break;
                }
                self.i += 1;
            }
            if keep && !lone {
                // The chunk ends before an ASCII byte, so it is whole UTF-8.
                out.push_str(std::str::from_utf8(&s[chunk..self.i]).unwrap_or_default());
            }
            match s.get(self.i) {
                None => {
                    return Err(Error {
                        what: UNTERMINATED,
                        at: begin,
                    })
                }
                Some(b'"') => {
                    self.i += 1;
                    return Ok((keep && !lone).then_some(out));
                }
                Some(b'\\') => {}
                Some(_) => return Err(self.error(CONTROL_CHARACTER)),
            }
            self.i += 1;
            let escaped = match s.get(self.i) {
                None => {
                    return Err(Error {
                        what: UNTERMINATED,
                        at: begin,
                    })
                }
                Some(b'"') => '"',
                Some(b'\\') => '\\',
                Some(b'/') => '/',
                Some(b'b') => '\u{8}',
                Some(b'f') => '\u{c}',
                Some(b'n') => '\n',
                Some(b'r') => '\r',
                Some(b't') => '\t',
                Some(b'u') => {
                    match self.unicode_escape()? {
                        Some(c) => {
                            if keep {
                                out.push(c);
                            }
                        }
                        None => lone = true,
                    }
                    continue;
                }
                Some(_) => return Err(self.error(INVALID_ESCAPE)),
            };
            self.i += 1;
            if keep {
                out.push(escaped);
            }
        }
    }

    /// The character a `\uXXXX` escape (with its `u` at `self.i`) spells,
    /// joined with a low surrogate's escape after a high surrogate's; `None`
    /// for a surrogate left alone.
    fn unicode_escape(&mut self) -> Result<Option<char>, Error> {
        self.i += 1;
        // Four digits and at least one byte after them, as `_json.c` asks.
        if self.i + 4 >= self.s.len() {
            return Err(self.error(INVALID_UNICODE_ESCAPE));
        }
        let high = self.hex4(self.i)?;
        self.i += 4;
        if (0xd800..=0xdbff).contains(&high)
            && self.i + 6 < self.s.len()
            && self.s[self.i] == b'\\'
            && self.s[self.i + 1] == b'u'
        {
            let low = self.hex4(self.i + 2)?;
            if (0xdc00..=0xdfff).contains(&low) {
                self.i += 6;
                let joined = 0x10000 + (((high - 0xd800) << 10) | (low - 0xdc00));
                return Ok(char::from_u32(joined));
            }
        }
        Ok(char::from_u32(high))
    }

    fn hex4(&self, at: usize) -> Result<u32, Error> {
        let mut value = 0;
        for offset in 0..4 {
            let digit = self
                .s
                .get(at + offset)
                .and_then(|&b| char::from(b).to_digit(16))
                .ok_or(Error {
                    what: INVALID_UNICODE_ESCAPE,
                    at,
                })?;
            value = value << 4 | digit;
        }
        Ok(value)
    }
}

fn closer(open: u8) -> u8 {
    if open == b'{' {
        b'}'
    } else {
        b']'
    }
}

const INDENT: &str = "  ";
const KEY_SEPARATOR: &str = ": ";
const ITEM_SEPARATOR: &str = ", ";
const INDENTED_ITEM_SEPARATOR: &str = ",";
const LINE_FEED: &str = "\n";
const EMPTY_OBJECT: &str = "{}";

/// `json.dumps(entries, indent=2)`: each member on a line of its own, no
/// line after the closing brace. Lines end with `\n`; a file opened in text
/// mode on Windows writes `\r\n` instead (`super::text_mode_bytes`).
pub fn indented_object<'a, I>(entries: I) -> String
where
    I: IntoIterator<Item = (&'a str, &'a str)>,
{
    let mut out = String::from("{");
    let mut first = true;
    for (name, value) in entries {
        if !first {
            out.push_str(INDENTED_ITEM_SEPARATOR);
        }
        first = false;
        out.push_str(LINE_FEED);
        out.push_str(INDENT);
        push_string(&mut out, name);
        out.push_str(KEY_SEPARATOR);
        push_string(&mut out, value);
    }
    if first {
        return EMPTY_OBJECT.to_owned();
    }
    out.push_str(LINE_FEED);
    out.push('}');
    out
}

/// `json.dumps(entries)` of str keys and float values, on one line.
pub fn float_object<'a, I>(entries: I) -> String
where
    I: IntoIterator<Item = (&'a str, f64)>,
{
    let mut out = String::from("{");
    for (index, (name, value)) in entries.into_iter().enumerate() {
        if index > 0 {
            out.push_str(ITEM_SEPARATOR);
        }
        push_string(&mut out, name);
        out.push_str(KEY_SEPARATOR);
        out.push_str(&json_float(value));
    }
    out.push('}');
    out
}

/// A float as `json.dumps` writes it: `float.__repr__`, and `NaN`,
/// `Infinity` and `-Infinity` for the rest.
pub fn json_float(value: f64) -> String {
    if value.is_nan() {
        "NaN".to_owned()
    } else if value.is_infinite() {
        if value > 0.0 { "Infinity" } else { "-Infinity" }.to_owned()
    } else {
        float_repr(value)
    }
}

/// Exponents `repr` writes in positional notation: from 1e-4 up to 1e16.
const REPR_LOWEST_POSITIONAL: i32 = -4;
const REPR_HIGHEST_POSITIONAL: i32 = 16;

/// `float.__repr__` of a finite float: the shortest digits that read back
/// as it, positional between 1e-4 and 1e16 with at least one digit after
/// the point, else `d.ddde+XX`.
pub fn float_repr(value: f64) -> String {
    let scientific = shortest_scientific(value);
    let (mantissa, exponent) = scientific.split_once('e').unwrap_or((&scientific, "0"));
    let exponent: i32 = exponent.parse().unwrap_or(0);
    let (sign, mantissa) = match mantissa.strip_prefix('-') {
        Some(rest) => ("-", rest),
        None => ("", mantissa),
    };
    let digits: String = mantissa.chars().filter(|c| *c != '.').collect();
    // Where the point goes, counted from the first digit.
    let point = exponent + 1;
    let mut out = String::from(sign);
    if point <= REPR_LOWEST_POSITIONAL || point > REPR_HIGHEST_POSITIONAL {
        out.push_str(&digits[..1]);
        if digits.len() > 1 {
            out.push('.');
            out.push_str(&digits[1..]);
        }
        let exponent_sign = if exponent < 0 { '-' } else { '+' };
        out.push_str(&format!("e{exponent_sign}{:02}", exponent.unsigned_abs()));
    } else if point <= 0 {
        out.push_str("0.");
        out.push_str(&"0".repeat(point.unsigned_abs() as usize));
        out.push_str(&digits);
    } else {
        let point = point as usize;
        if point >= digits.len() {
            out.push_str(&digits);
            out.push_str(&"0".repeat(point - digits.len()));
            out.push_str(".0");
        } else {
            out.push_str(&digits[..point]);
            out.push('.');
            out.push_str(&digits[point..]);
        }
    }
    out
}

/// The shortest digits that read back as `value`, in `{:e}` form, chosen
/// as `repr` chooses them: of the strings that short, the one nearest the
/// value. `{:e}` finds the length, but when the value lies exactly halfway
/// between two such strings it rounds up where `repr`'s `dtoa` rounds to
/// even, so the length's correctly rounded digits (`{:.N$e}`, which round
/// to even) are taken whenever they read back too.
fn shortest_scientific(value: f64) -> String {
    let shortest = format!("{value:e}");
    let mantissa = shortest.split('e').next().unwrap_or_default();
    let digits = mantissa.bytes().filter(u8::is_ascii_digit).count();
    let nearest = format!("{value:.*e}", digits.saturating_sub(1));
    if nearest.parse::<f64>() == Ok(value) {
        nearest
    } else {
        shortest
    }
}
