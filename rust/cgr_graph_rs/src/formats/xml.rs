//! A strict reader for the XML project files cgr reads (`.csproj`, `.slnx`).
//!
//! It reports each element's name and attributes as `xml.etree.ElementTree`
//! does on top of expat: `{uri}local` for a namespaced name, the plain local
//! name otherwise, `xmlns` declarations consumed rather than reported, and
//! attribute values normalised (every literal tab, newline and carriage
//! return becomes a space; a CR LF pair becomes one).
//!
//! It never expands an entity a document declares: any `<!ENTITY>` in the
//! DTD, general or parameter, refuses the whole document
//! (`Error::EntityDeclared`), which is how XML bombs and external-entity
//! reads are stopped. Only the five predefined entities and character
//! references are ever replaced.
//!
//! What it reads: UTF-8 (with or without a byte-order mark), UTF-16 (either
//! byte order), ISO-8859-1, US-ASCII and windows-1252; a DTD holding element
//! and notation declarations, comments and processing instructions, which
//! are skipped. It rejects, as not well-formed, attribute-list declarations
//! (their defaults would add attributes) and any other encoding.

use std::borrow::Cow;
use std::collections::{HashMap, HashSet};
use std::fmt;

use super::charsets::{NAMES, TABLES};
use super::xml_names::{NAME_CHAR, NAME_START};

const XML_NAMESPACE: &str = "http://www.w3.org/XML/1998/namespace";
const XMLNS_NAMESPACE: &str = "http://www.w3.org/2000/xmlns/";

/// One element: its name and attributes, spelled as ElementTree spells them.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Element {
    pub name: String,
    pub attributes: Vec<(String, String)>,
}

/// Why a document was not read.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Error {
    /// Not well-formed, or outside what this reader reads. `line` counts
    /// from 1 and `column` from 0, in characters, as expat counts them.
    Syntax {
        message: String,
        line: usize,
        column: usize,
    },
    /// The DTD declares an entity, which is never expanded.
    EntityDeclared { name: String },
}

impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Error::Syntax {
                message,
                line,
                column,
            } => write!(f, "{message}: line {line}, column {column}"),
            Error::EntityDeclared { name } => {
                write!(f, "XML entity declarations are refused: {name}")
            }
        }
    }
}

impl std::error::Error for Error {}

/// Every element of `doc` in document order (the order of `Element.iter()`).
pub fn elements(doc: &[u8]) -> Result<Vec<Element>, Error> {
    match decode(doc) {
        Ok((text, letters)) => Reader::new(&text, letters)
            .document()
            .map_err(|failure| failure.into_error(&text)),
        Err(Undecodable { read, error }) => {
            // expat reads up to the bad byte, so an entity declared before
            // it is refused first, as expat's handler would refuse it.
            match Reader::new(&read, false).document() {
                Err(Failure {
                    entity: Some(name), ..
                }) => Err(Error::EntityDeclared { name }),
                _ => Err(error),
            }
        }
    }
}

/// The attributes of every element named `name`, in document order:
/// `[e.attrib for e in root.iter(name)]`.
pub fn find_all(doc: &[u8], name: &str) -> Result<Vec<Vec<(String, String)>>, Error> {
    Ok(elements(doc)?
        .into_iter()
        .filter(|element| element.name == name)
        .map(|element| element.attributes)
        .collect())
}

/// A failure at a byte offset of the decoded text.
struct Failure {
    message: Cow<'static, str>,
    offset: usize,
    entity: Option<String>,
}

impl Failure {
    fn into_error(self, text: &str) -> Error {
        if let Some(name) = self.entity {
            return Error::EntityDeclared { name };
        }
        let (line, column) = position(text, self.offset);
        Error::Syntax {
            message: self.message.into_owned(),
            line,
            column,
        }
    }
}

fn position(text: &str, offset: usize) -> (usize, usize) {
    let before = &text[..offset.min(text.len())];
    let line_start = before.rfind(['\n', '\r']).map_or(0, |at| at + 1);
    let lines = before.matches('\n').count() + before.matches('\r').count()
        - before.matches("\r\n").count();
    (lines + 1, before[line_start..].chars().count())
}

fn syntax_at(message: impl Into<Cow<'static, str>>, offset: usize) -> Failure {
    Failure {
        message: message.into(),
        offset,
        entity: None,
    }
}

fn whole_document(message: impl Into<Cow<'static, str>>) -> Error {
    Error::Syntax {
        message: message.into().into_owned(),
        line: 1,
        column: 0,
    }
}

// ---- Encodings -------------------------------------------------------------

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Encoding {
    Utf8,
    /// `UTF-16`, either byte order.
    Utf16,
    Utf16Le,
    Utf16Be,
    /// `ISO-8859-1` by expat's own name for it.
    Latin1Native,
    /// `US-ASCII` by expat's own name for it.
    AsciiNative,
    /// Any other name, read through Python's codec of that name, one byte
    /// at a time: an index into `charsets::TABLES`.
    Table(usize),
}

/// A document that does not decode, with the text before the bad byte.
struct Undecodable<'a> {
    read: Cow<'a, str>,
    error: Error,
}

impl Undecodable<'_> {
    fn whole(message: impl Into<Cow<'static, str>>) -> Self {
        Undecodable {
            read: Cow::Borrowed(""),
            error: whole_document(message),
        }
    }
}

const INVALID_TOKEN: &str = "not well-formed (invalid token)";
/// Content-model groups nest no deeper than this, so reading one never
/// exhausts the stack.
const MAX_CONTENT_DEPTH: usize = 256;
const INCORRECT_ENCODING: &str = "encoding specified in XML declaration is incorrect";

/// The document as text, decoded as its byte-order mark, its first bytes
/// or its XML declaration say, and with every character a legal XML one.
/// With the text, whether `ª`, `µ` and `º` are letters: expat counts them
/// in names in UTF-16 and in ISO-8859-1 named as such, not elsewhere.
fn decode(doc: &[u8]) -> Result<(Cow<'_, str>, bool), Undecodable<'_>> {
    let (detected, body) = if let Some(rest) = doc.strip_prefix(b"\xEF\xBB\xBF") {
        (Some(Encoding::Utf8), rest)
    } else if let Some(rest) = doc.strip_prefix(b"\xFF\xFE") {
        (Some(Encoding::Utf16Le), rest)
    } else if let Some(rest) = doc.strip_prefix(b"\xFE\xFF") {
        (Some(Encoding::Utf16Be), rest)
    } else if doc.starts_with(b"<\x00?\x00") {
        (Some(Encoding::Utf16Le), doc)
    } else if doc.starts_with(b"\x00<\x00?") {
        (Some(Encoding::Utf16Be), doc)
    } else {
        (None, doc)
    };
    let mut letters = false;
    let text: Cow<'_, str> = match detected {
        Some(Encoding::Utf16Le) => utf16(body, u16::from_le_bytes)?,
        Some(Encoding::Utf16Be) => utf16(body, u16::from_be_bytes)?,
        _ => {
            let declared = match declared_encoding(body) {
                None => None,
                Some(name) => Some(
                    encoding_named(&name)
                        .ok_or_else(|| Undecodable::whole(format!("unknown encoding: {name}")))?,
                ),
            };
            match (detected, declared) {
                (_, None | Some(Encoding::Utf8)) => match std::str::from_utf8(body) {
                    Ok(text) => Cow::Borrowed(text),
                    Err(error) => {
                        let read = std::str::from_utf8(&body[..error.valid_up_to()])
                            .expect("valid up to here");
                        // An illegal character before the bad byte stops
                        // expat first.
                        let read = &read[..read.find(|c| !is_xml_char(c)).unwrap_or(read.len())];
                        return Err(undecodable_at(Cow::Borrowed(read)));
                    }
                },
                // After a UTF-8 byte-order mark, expat still switches to a
                // declared single-byte encoding.
                (_, Some(Encoding::Latin1Native)) => {
                    letters = true;
                    Cow::Owned(body.iter().map(|&b| b as char).collect())
                }
                (_, Some(Encoding::AsciiNative)) => {
                    let ascii = body
                        .iter()
                        .position(|b| !b.is_ascii())
                        .unwrap_or(body.len());
                    let read = std::str::from_utf8(&body[..ascii]).expect("ASCII is UTF-8");
                    if ascii < body.len() {
                        return Err(undecodable_at(Cow::Borrowed(read)));
                    }
                    Cow::Borrowed(read)
                }
                (_, Some(Encoding::Table(index))) => single_byte(body, &TABLES[index])?,
                (_, Some(Encoding::Utf16 | Encoding::Utf16Le | Encoding::Utf16Be)) => {
                    return Err(Undecodable::whole(INCORRECT_ENCODING))
                }
            }
        }
    };
    if let Some(order) = detected.filter(|e| matches!(e, Encoding::Utf16Le | Encoding::Utf16Be)) {
        letters = true;
        // A UTF-16 document may declare itself UTF-16, or name its own
        // byte order, and nothing else.
        if let Some(name) = declared_encoding(text.as_bytes()) {
            let declared = encoding_named(&name);
            if declared != Some(Encoding::Utf16) && declared != Some(order) {
                return Err(Undecodable::whole(INCORRECT_ENCODING));
            }
        }
    }
    if let Some(at) = text.find(|c: char| !is_xml_char(c)) {
        let read = match &text {
            Cow::Borrowed(text) => Cow::Borrowed(&text[..at]),
            Cow::Owned(text) => Cow::Owned(text[..at].to_string()),
        };
        return Err(undecodable_at(read));
    }
    Ok((text, letters))
}

/// An invalid byte or character just past `read`.
fn undecodable_at(read: Cow<'_, str>) -> Undecodable<'_> {
    let (line, column) = position(&read, read.len());
    Undecodable {
        error: Error::Syntax {
            message: INVALID_TOKEN.to_string(),
            line,
            column,
        },
        read,
    }
}

/// How a declared encoding is read, as expat reads it. expat knows six
/// names itself, whatever their case; any other it asks Python's codecs
/// for, one byte at a time (`charsets`), and a name no codec answers to is
/// unknown.
fn encoding_named(name: &str) -> Option<Encoding> {
    const NATIVE: [(&str, Encoding); 6] = [
        ("UTF-8", Encoding::Utf8),
        ("UTF-16", Encoding::Utf16),
        ("UTF-16LE", Encoding::Utf16Le),
        ("UTF-16BE", Encoding::Utf16Be),
        ("ISO-8859-1", Encoding::Latin1Native),
        ("US-ASCII", Encoding::AsciiNative),
    ];
    if let Some((_, encoding)) = NATIVE
        .iter()
        .find(|(known, _)| known.eq_ignore_ascii_case(name))
    {
        return Some(*encoding);
    }
    // Python looks the normalised name up, then the same with its dots made
    // underscores.
    let normalized = python_codec_name(name);
    [normalized.clone(), normalized.replace('.', "_")]
        .iter()
        .find_map(|candidate| {
            NAMES
                .binary_search_by(|(known, _)| (*known).cmp(candidate.as_str()))
                .ok()
        })
        .map(|at| Encoding::Table(NAMES[at].1))
}

/// `encodings.normalize_encoding` after the lowercasing `codecs.lookup`
/// does: each run of characters other than letters, digits and dots
/// becomes one underscore, and none is kept at either end.
fn python_codec_name(name: &str) -> String {
    let mut out = String::with_capacity(name.len());
    let mut gap = false;
    for c in name.chars() {
        if c.is_alphanumeric() || c == '.' {
            if gap && !out.is_empty() {
                out.push('_');
            }
            if c.is_ascii() {
                out.push(c.to_ascii_lowercase());
            }
            gap = false;
        } else {
            gap = true;
        }
    }
    out
}

/// The `encoding` pseudo-attribute of a leading XML declaration, read from
/// ASCII-compatible bytes. A malformed declaration is the reader's to report.
fn declared_encoding(doc: &[u8]) -> Option<String> {
    let decl = doc.strip_prefix(b"<?xml")?;
    if !decl.first().copied().is_some_and(is_space) {
        return None;
    }
    let end = decl.windows(2).position(|w| w == b"?>")?;
    let decl = &decl[..end];
    let at = decl.windows(8).position(|w| w == b"encoding")?;
    let rest = decl[at + 8..]
        .trim_ascii_start()
        .strip_prefix(b"=")?
        .trim_ascii_start();
    let quote = *rest.first().filter(|&&b| b == b'"' || b == b'\'')?;
    let len = rest[1..].iter().position(|&b| b == quote)?;
    String::from_utf8(rest[1..1 + len].to_vec()).ok()
}

fn utf16(body: &[u8], unit: fn([u8; 2]) -> u16) -> Result<Cow<'static, str>, Undecodable<'static>> {
    let units = body.chunks_exact(2).map(|pair| unit([pair[0], pair[1]]));
    let mut text = String::with_capacity(body.len() / 2);
    for c in char::decode_utf16(units) {
        match c {
            Ok(c) => text.push(c),
            Err(_) => return Err(undecodable_at(Cow::Owned(text))),
        }
    }
    if !body.len().is_multiple_of(2) {
        return Err(undecodable_at(Cow::Owned(text)));
    }
    Ok(Cow::Owned(text))
}

/// Bytes read through a single-byte table: ASCII below 0x80, the table's
/// character above, and a 0 there is not a character.
fn single_byte(body: &[u8], high: &[u16; 128]) -> Result<Cow<'static, str>, Undecodable<'static>> {
    let mut text = String::with_capacity(body.len());
    for &b in body {
        let c = if b < 0x80 {
            Some(b as char)
        } else {
            match high[usize::from(b - 0x80)] {
                0 => None,
                code => char::from_u32(u32::from(code)),
            }
        };
        match c {
            Some(c) => text.push(c),
            None => return Err(undecodable_at(Cow::Owned(text))),
        }
    }
    Ok(Cow::Owned(text))
}

// ---- Characters and names ---------------------------------------------------

fn is_xml_char(c: char) -> bool {
    matches!(c, '\t' | '\n' | '\r' | '\u{20}'..='\u{D7FF}' | '\u{E000}'..='\u{FFFD}' | '\u{10000}'..)
}

fn is_space(b: u8) -> bool {
    matches!(b, b' ' | b'\t' | b'\r' | b'\n')
}

fn in_ranges(c: char, ranges: &[(u32, u32)]) -> bool {
    let code = u32::from(c);
    ranges
        .binary_search_by(|&(low, high)| {
            if high < code {
                std::cmp::Ordering::Less
            } else if low > code {
                std::cmp::Ordering::Greater
            } else {
                std::cmp::Ordering::Equal
            }
        })
        .is_ok()
}

/// The three characters expat counts as letters in UTF-16 and ISO-8859-1
/// only (its Latin-1 table has them, its Unicode one does not).
const LATIN1_LETTERS: [char; 3] = ['\u{AA}', '\u{B5}', '\u{BA}'];

/// A character that may start a name, by expat's tables; the colon too,
/// which `split_qname` then judges.
fn is_name_start(c: char, letters: bool) -> bool {
    if c.is_ascii() {
        return c.is_ascii_alphabetic() || c == '_' || c == ':';
    }
    in_ranges(c, NAME_START) || (letters && LATIN1_LETTERS.contains(&c))
}

/// A character that may continue a name, by expat's tables.
fn is_name_char(c: char, letters: bool) -> bool {
    if c.is_ascii() {
        return c.is_ascii_alphanumeric() || matches!(c, '_' | ':' | '-' | '.');
    }
    in_ranges(c, NAME_CHAR) || (letters && LATIN1_LETTERS.contains(&c))
}

/// A name without a colon that may follow one: `p:5` and `p:-x` are not
/// names under namespaces, though `5` and `-x` are name characters.
fn is_ncname(name: &str, letters: bool) -> bool {
    name.chars()
        .next()
        .is_some_and(|c| c != ':' && is_name_start(c, letters))
        && !name.contains(':')
}

/// `(prefix, local)`, or None for a name namespaces reject: a colon at
/// either end, more than one, or a part that cannot start a name.
fn split_qname(name: &str, letters: bool) -> Option<(Option<&str>, &str)> {
    match name.split_once(':') {
        None => Some((None, name)),
        Some((prefix, local)) if is_ncname(prefix, letters) && is_ncname(local, letters) => {
            Some((Some(prefix), local))
        }
        Some(_) => None,
    }
}

fn expanded(uri: Option<&str>, local: &str) -> String {
    match uri {
        Some(uri) if !uri.is_empty() => format!("{{{uri}}}{local}"),
        _ => local.to_string(),
    }
}

// ---- The reader --------------------------------------------------------------

type Step<T = ()> = Result<T, Failure>;

struct Open<'a> {
    qname: &'a str,
    declared: Vec<&'a str>,
}

struct Reader<'a> {
    text: &'a str,
    bytes: &'a [u8],
    pos: usize,
    found: Vec<Element>,
    open: Vec<Open<'a>>,
    /// Prefix ("" for the default namespace) to its bindings, innermost last.
    bindings: HashMap<&'a str, Vec<String>>,
    /// `standalone="yes"` in the XML declaration.
    standalone: bool,
    /// The DTD names an external subset or references a parameter entity,
    /// so an entity this reader never saw may be declared where it cannot
    /// look; expat then drops an unknown reference in an attribute value.
    unseen_declarations: bool,
    /// Whether declarations still take effect: expat stops processing them
    /// after a reference to an undeclared parameter entity, unless the
    /// document is standalone.
    processing: bool,
    /// Element name to its declared attributes, in declaration order.
    attlists: HashMap<&'a str, Vec<Declared<'a>>>,
    /// `ª`, `µ` and `º` count as letters in names (see `decode`).
    letters: bool,
}

/// An attribute an `<!ATTLIST>` declares for an element.
struct Declared<'a> {
    name: &'a str,
    /// Its default, if it has one (not `#IMPLIED` or `#REQUIRED`).
    default: Option<String>,
    /// Any type but CDATA: values lose leading and trailing spaces and
    /// collapse inner runs of them.
    tokenized: bool,
}

/// What a tokenized attribute type does to a value.
fn collapse_spaces(value: &str) -> String {
    value
        .split(' ')
        .filter(|part| !part.is_empty())
        .collect::<Vec<_>>()
        .join(" ")
}

impl<'a> Reader<'a> {
    fn new(text: &'a str, letters: bool) -> Self {
        Reader {
            text,
            bytes: text.as_bytes(),
            pos: 0,
            found: Vec::new(),
            open: Vec::new(),
            bindings: HashMap::new(),
            standalone: false,
            unseen_declarations: false,
            processing: true,
            attlists: HashMap::new(),
            letters,
        }
    }

    fn document(mut self) -> Step<Vec<Element>> {
        self.xml_declaration()?;
        self.misc()?;
        if self.at("<!DOCTYPE") {
            self.doctype()?;
            self.misc()?;
        }
        if !self.at_start_tag() {
            return Err(self.fail(if self.pos == self.bytes.len() {
                "no element found"
            } else {
                "syntax error"
            }));
        }
        self.start_tag()?;
        while !self.open.is_empty() {
            self.content()?;
        }
        self.misc()?;
        if self.pos != self.bytes.len() {
            return Err(self.fail("junk after document element"));
        }
        Ok(self.found)
    }

    fn fail(&self, message: impl Into<Cow<'static, str>>) -> Failure {
        syntax_at(message, self.pos)
    }

    fn peek(&self) -> Option<u8> {
        self.bytes.get(self.pos).copied()
    }

    fn peek_char(&self) -> Option<char> {
        self.text[self.pos..].chars().next()
    }

    fn at(&self, literal: &str) -> bool {
        self.bytes[self.pos..].starts_with(literal.as_bytes())
    }

    fn at_start_tag(&self) -> bool {
        self.peek() == Some(b'<')
            && self.text[self.pos + 1..]
                .chars()
                .next()
                .is_some_and(|c| is_name_start(c, self.letters))
    }

    fn eat(&mut self, literal: &str) -> bool {
        let found = self.at(literal);
        if found {
            self.pos += literal.len();
        }
        found
    }

    fn expect(&mut self, literal: &str) -> Step {
        if self.eat(literal) {
            Ok(())
        } else {
            Err(self.unexpected())
        }
    }

    fn unexpected(&self) -> Failure {
        self.fail(if self.pos >= self.bytes.len() {
            "unclosed token"
        } else {
            "not well-formed (invalid token)"
        })
    }

    fn skip_space(&mut self) -> bool {
        let start = self.pos;
        while self.peek().is_some_and(is_space) {
            self.pos += 1;
        }
        self.pos > start
    }

    fn require_space(&mut self) -> Step {
        if self.skip_space() {
            Ok(())
        } else {
            Err(self.unexpected())
        }
    }

    /// The index just past the next `literal`, searching from the cursor.
    fn find(&self, literal: &str) -> Step<usize> {
        self.text[self.pos..]
            .find(literal)
            .map(|at| self.pos + at + literal.len())
            .ok_or_else(|| syntax_at("unclosed token", self.bytes.len()))
    }

    fn name(&mut self) -> Step<&'a str> {
        let start = self.pos;
        if !self
            .peek_char()
            .is_some_and(|c| is_name_start(c, self.letters))
        {
            return Err(self.unexpected());
        }
        while let Some(c) = self.peek_char().filter(|&c| is_name_char(c, self.letters)) {
            self.pos += c.len_utf8();
        }
        Ok(&self.text[start..self.pos])
    }

    fn quoted(&mut self) -> Step<&'a str> {
        let quote = self
            .peek()
            .filter(|&b| b == b'"' || b == b'\'')
            .ok_or_else(|| self.unexpected())?;
        let start = self.pos + 1;
        let len = self.bytes[start..]
            .iter()
            .position(|&b| b == quote)
            .ok_or_else(|| syntax_at("unclosed token", self.bytes.len()))?;
        self.pos = start + len + 1;
        Ok(&self.text[start..start + len])
    }

    /// `<?xml version="1.0" encoding="..." standalone="yes"?>`, only at the
    /// very start. `decode` has already acted on the encoding.
    fn xml_declaration(&mut self) -> Step {
        let is_declaration = self.at("<?xml")
            && !self.text[5..]
                .chars()
                .next()
                .is_some_and(|c| is_name_char(c, self.letters));
        if !is_declaration {
            return Ok(());
        }
        self.pos = 5;
        self.require_space()?;
        self.expect("version")?;
        self.equals()?;
        let version = self.quoted()?;
        let valid = version
            .strip_prefix("1.")
            .is_some_and(|minor| !minor.is_empty() && minor.bytes().all(|b| b.is_ascii_digit()));
        if !valid {
            return Err(self.fail("XML declaration not well-formed"));
        }
        let mut spaced = self.skip_space();
        if spaced && self.eat("encoding") {
            self.equals()?;
            let name = self.quoted()?;
            let valid = name.bytes().next().is_some_and(|b| b.is_ascii_alphabetic())
                && name
                    .bytes()
                    .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'.' | b'_' | b'-'));
            if !valid {
                return Err(self.fail("XML declaration not well-formed"));
            }
            spaced = self.skip_space();
        }
        if spaced && self.eat("standalone") {
            self.equals()?;
            let value = self.quoted()?;
            if !matches!(value, "yes" | "no") {
                return Err(self.fail("XML declaration not well-formed"));
            }
            self.standalone = value == "yes";
            self.skip_space();
        }
        if !self.eat("?>") {
            return Err(self.fail("XML declaration not well-formed"));
        }
        Ok(())
    }

    fn equals(&mut self) -> Step {
        self.skip_space();
        self.expect("=")?;
        self.skip_space();
        Ok(())
    }

    /// Whitespace, comments and processing instructions, as may surround
    /// the root element.
    fn misc(&mut self) -> Step {
        loop {
            self.skip_space();
            if self.at("<!--") {
                self.comment()?;
            } else if self.at("<?") {
                self.processing_instruction()?;
            } else {
                return Ok(());
            }
        }
    }

    fn comment(&mut self) -> Step {
        self.pos += "<!--".len();
        // `--` may appear only as the start of `-->`.
        let end = self.find("--")?;
        if self.bytes.get(end) != Some(&b'>') {
            return Err(syntax_at("not well-formed (invalid token)", end - 2));
        }
        self.pos = end + 1;
        Ok(())
    }

    fn processing_instruction(&mut self) -> Step {
        self.pos += "<?".len();
        let start = self.pos;
        let target = self.name()?;
        if target.eq_ignore_ascii_case("xml") {
            return Err(syntax_at(
                "XML or text declaration not at start of entity",
                start - 2,
            ));
        }
        if target.contains(':') {
            return Err(syntax_at("not well-formed (invalid token)", start));
        }
        if self.eat("?>") {
            return Ok(());
        }
        self.require_space()?;
        self.pos = self.find("?>")?;
        Ok(())
    }

    /// `<!DOCTYPE name ExternalID? [internal subset]? >`. The external
    /// subset is never read; the internal one may hold element and notation
    /// declarations, comments and processing instructions.
    fn doctype(&mut self) -> Step {
        self.pos += "<!DOCTYPE".len();
        self.require_space()?;
        self.qname()?;
        let spaced = self.skip_space();
        if spaced && (self.at("SYSTEM") || self.at("PUBLIC")) {
            self.external_id(false)?;
            self.unseen_declarations = true;
        }
        self.skip_space();
        if self.eat("[") {
            self.internal_subset()?;
            self.skip_space();
        }
        self.expect(">")
    }

    fn internal_subset(&mut self) -> Step {
        loop {
            self.skip_space();
            if self.eat("]") {
                return Ok(());
            }
            if self.at("<!--") {
                self.comment()?;
            } else if self.at("<?") {
                self.processing_instruction()?;
            } else if self.at("<!ENTITY") {
                self.entity_declaration()?;
            } else if self.at("<!ELEMENT") {
                self.element_declaration()?;
            } else if self.at("<!NOTATION") {
                self.notation_declaration()?;
            } else if self.at("<!ATTLIST") {
                self.attlist_declaration()?;
            } else if self.eat("%") {
                // A parameter entity this reader cannot have seen declared
                // (any declaration of one is refused): expat skips it, and
                // stops processing the declarations after it.
                self.entity_name()?;
                self.expect(";")?;
                self.unseen_declarations = true;
                self.processing = self.standalone;
            } else {
                return Err(self.unexpected());
            }
        }
    }

    /// A name, checked as namespaces check names (one colon at most, with a
    /// name on either side).
    fn qname(&mut self) -> Step<&'a str> {
        let start = self.pos;
        let name = self.name()?;
        if split_qname(name, self.letters).is_none() {
            return Err(syntax_at(INVALID_TOKEN, start));
        }
        Ok(name)
    }

    /// A quoted literal in the DTD. expat's tokenizer takes one only when a
    /// space, `>`, `%` or `[` follows it.
    fn dtd_literal(&mut self) -> Step<&'a str> {
        let value = self.quoted()?;
        self.after_dtd_literal()?;
        Ok(value)
    }

    fn after_dtd_literal(&self) -> Step {
        match self.peek() {
            None => Err(syntax_at("unclosed token", self.bytes.len())),
            Some(b) if is_space(b) || matches!(b, b'>' | b'%' | b'[') => Ok(()),
            Some(_) => Err(self.fail(INVALID_TOKEN)),
        }
    }

    /// `SYSTEM "uri"` or `PUBLIC "id" "uri"`; a notation (`notation`) may
    /// stop after the public identifier.
    fn external_id(&mut self, notation: bool) -> Step {
        if self.eat("SYSTEM") {
            self.require_space()?;
            self.dtd_literal()?;
            return Ok(());
        }
        self.expect("PUBLIC")?;
        self.require_space()?;
        let start = self.pos;
        let quote = self.peek();
        let id = self.dtd_literal()?;
        let public_char = |c: char| {
            c.is_ascii_alphanumeric()
                || matches!(c, ' ' | '\r' | '\n')
                || "-()+,./:=?;!*#@$_%".contains(c)
                || (c == '\'' && quote == Some(b'"'))
        };
        if let Some(at) = id.find(|c: char| !public_char(c)) {
            return Err(syntax_at(INVALID_TOKEN, start + 1 + at));
        }
        let before = self.pos;
        if self.skip_space() && matches!(self.peek(), Some(b'"' | b'\'')) {
            self.dtd_literal()?;
        } else if notation {
            self.pos = before;
        } else {
            return Err(self.unexpected());
        }
        Ok(())
    }

    /// `<!ENTITY [%] name (value | ExternalID [NDATA name]) >`. While
    /// declarations are processed, it refuses the document at the point
    /// expat hands the declaration to its handler: just after an internal
    /// entity's value, after an unparsed entity's notation, otherwise at
    /// the closing `>`. Past that point expat reads no further, so neither
    /// does this.
    fn entity_declaration(&mut self) -> Step {
        self.pos += "<!ENTITY".len();
        self.require_space()?;
        let parameter = self.eat("%");
        if parameter {
            self.require_space()?;
        }
        let name = self.entity_name()?;
        self.require_space()?;
        if matches!(self.peek(), Some(b'"' | b'\'')) {
            // The value is not looked into: nothing in it is ever replaced.
            self.dtd_literal()?;
            self.refuse_entity(name)?;
        } else {
            self.external_id(false)?;
            let before = self.pos;
            if !parameter && self.skip_space() && self.eat("NDATA") {
                self.require_space()?;
                self.name()?;
                self.refuse_entity(name)?;
            } else {
                self.pos = before;
            }
        }
        self.skip_space();
        self.expect(">")?;
        self.refuse_entity(name)
    }

    /// An entity's name, which namespaces forbid a colon in.
    fn entity_name(&mut self) -> Step<&'a str> {
        let start = self.pos;
        let name = self.name()?;
        if name.contains(':') {
            return Err(syntax_at("syntax error", start));
        }
        Ok(name)
    }

    fn refuse_entity(&self, name: &str) -> Step {
        if !self.processing {
            return Ok(());
        }
        Err(Failure {
            message: Cow::Borrowed("entity declared"),
            offset: self.pos,
            entity: Some(name.to_string()),
        })
    }

    /// `<!ATTLIST element (name type default)* >`. While declarations are
    /// processed, the first declaration of each attribute counts: its
    /// default is added to every element that leaves it out, and a type
    /// other than CDATA collapses the spaces in its values.
    fn attlist_declaration(&mut self) -> Step {
        self.pos += "<!ATTLIST".len();
        self.require_space()?;
        let element = self.qname()?;
        loop {
            let spaced = self.skip_space();
            if self.eat(">") {
                return Ok(());
            }
            if !spaced {
                return Err(self.unexpected());
            }
            let name = self.qname()?;
            self.require_space()?;
            let tokenized = self.attribute_type()?;
            self.require_space()?;
            let default = if self.eat("#REQUIRED") || self.eat("#IMPLIED") {
                None
            } else {
                if self.eat("#FIXED") {
                    self.require_space()?;
                }
                if !self.processing {
                    // expat does not look into a declaration it ignores.
                    self.dtd_literal()?;
                    continue;
                }
                let value = self.attribute_value()?;
                self.after_dtd_literal()?;
                Some(if tokenized {
                    collapse_spaces(&value)
                } else {
                    value
                })
            };
            if self.processing {
                let declared = self.attlists.entry(element).or_default();
                if declared.iter().all(|d| d.name != name) {
                    declared.push(Declared {
                        name,
                        default,
                        tokenized,
                    });
                }
            }
        }
    }

    /// An attribute type; whether it is tokenized (anything but CDATA).
    fn attribute_type(&mut self) -> Step<bool> {
        if self.eat("CDATA") {
            return Ok(false);
        }
        for keyword in [
            "IDREFS", "IDREF", "ID", "ENTITIES", "ENTITY", "NMTOKENS", "NMTOKEN",
        ] {
            if self.eat(keyword) {
                return Ok(true);
            }
        }
        let notation = self.eat("NOTATION");
        if notation {
            self.require_space()?;
        }
        self.expect("(")?;
        loop {
            self.skip_space();
            if notation {
                self.name()?;
            } else {
                self.name_token()?;
            }
            self.skip_space();
            if self.eat(")") {
                return Ok(true);
            }
            self.expect("|")?;
        }
    }

    /// One or more name characters.
    fn name_token(&mut self) -> Step {
        let start = self.pos;
        while let Some(c) = self.peek_char().filter(|&c| is_name_char(c, self.letters)) {
            self.pos += c.len_utf8();
        }
        if self.pos == start {
            return Err(self.unexpected());
        }
        Ok(())
    }

    /// `<!ELEMENT name (EMPTY | ANY | mixed | children) >`.
    fn element_declaration(&mut self) -> Step {
        self.pos += "<!ELEMENT".len();
        self.require_space()?;
        self.qname()?;
        self.require_space()?;
        if !(self.eat("EMPTY") || self.eat("ANY")) {
            self.expect("(")?;
            self.skip_space();
            if self.eat("#PCDATA") {
                self.mixed_content()?;
            } else {
                self.content_group(0)?;
            }
        }
        self.skip_space();
        self.expect(">")
    }

    /// The rest of `(#PCDATA | a | b)*`, or of `(#PCDATA)`.
    fn mixed_content(&mut self) -> Step {
        let mut named = false;
        loop {
            self.skip_space();
            if self.eat(")") {
                if named {
                    self.expect("*")?;
                } else {
                    self.eat("*");
                }
                return Ok(());
            }
            self.expect("|")?;
            self.skip_space();
            self.qname()?;
            named = true;
        }
    }

    /// The rest of a content group after its `(`: particles joined all by
    /// `|` or all by `,`, then `)` and an optional `?`, `*` or `+`.
    fn content_group(&mut self, depth: usize) -> Step {
        if depth > MAX_CONTENT_DEPTH {
            return Err(self.fail("content model nested too deeply"));
        }
        let mut separator = None;
        loop {
            self.skip_space();
            if self.eat("(") {
                self.content_group(depth + 1)?;
            } else {
                self.qname()?;
                self.occurrence();
            }
            self.skip_space();
            if self.eat(")") {
                self.occurrence();
                return Ok(());
            }
            let next = self.peek().filter(|b| matches!(b, b'|' | b','));
            if next.is_none() || separator.is_some_and(|s| Some(s) != next) {
                return Err(self.unexpected());
            }
            separator = next;
            self.pos += 1;
        }
    }

    fn occurrence(&mut self) {
        if matches!(self.peek(), Some(b'?' | b'*' | b'+')) {
            self.pos += 1;
        }
    }

    /// `<!NOTATION name (ExternalID | PUBLIC "id") >`.
    fn notation_declaration(&mut self) -> Step {
        self.pos += "<!NOTATION".len();
        self.require_space()?;
        self.qname()?;
        self.require_space()?;
        self.external_id(true)?;
        self.skip_space();
        self.expect(">")
    }

    /// One step inside an open element: a tag, a comment, a processing
    /// instruction, a CDATA section, a reference or a run of text.
    fn content(&mut self) -> Step {
        match self.peek() {
            None => Err(syntax_at("no element found", self.bytes.len())),
            Some(b'<') => {
                if self.at("</") {
                    self.end_tag()
                } else if self.at("<!--") {
                    self.comment()
                } else if self.at("<![CDATA[") {
                    self.pos = self.find("]]>")?;
                    Ok(())
                } else if self.at("<?") {
                    self.processing_instruction()
                } else if self.at_start_tag() {
                    self.start_tag()
                } else {
                    self.pos += 1;
                    Err(self.unexpected())
                }
            }
            Some(b'&') => self.reference().map(drop),
            Some(_) => {
                let len = self.bytes[self.pos..]
                    .iter()
                    .position(|&b| b == b'<' || b == b'&')
                    .unwrap_or(self.bytes.len() - self.pos);
                if let Some(at) = self.text[self.pos..self.pos + len].find("]]>") {
                    return Err(syntax_at("not well-formed (invalid token)", self.pos + at));
                }
                self.pos += len;
                Ok(())
            }
        }
    }

    /// `&lt;`, `&#60;` or `&#x3c;`, as the character it stands for. The DTD
    /// may declare no entity, so the five predefined ones are all there are.
    fn reference(&mut self) -> Step<char> {
        let start = self.pos;
        self.pos += 1;
        let code = if self.eat("#x") {
            self.number(16)?
        } else if self.eat("#") {
            self.number(10)?
        } else {
            let c = match self.name()? {
                "lt" => '<',
                "gt" => '>',
                "amp" => '&',
                "apos" => '\'',
                "quot" => '"',
                _ => return Err(syntax_at("undefined entity", start)),
            };
            self.expect(";")?;
            return Ok(c);
        };
        self.expect(";")?;
        char::from_u32(code)
            .filter(|&c| is_xml_char(c))
            .ok_or_else(|| syntax_at("reference to invalid character number", start))
    }

    /// A reference in an attribute value. One to an entity this reader
    /// does not know is an error, unless the DTD may declare it where it
    /// cannot look and the document is not standalone: expat then drops it.
    fn attribute_reference(&mut self) -> Step<Option<char>> {
        let start = self.pos;
        match self.reference() {
            Ok(c) => Ok(Some(c)),
            Err(failure) if self.unseen_declarations && !self.standalone => {
                self.pos = start + 1;
                if self.at("#") || self.entity_name().is_err() || !self.eat(";") {
                    return Err(failure);
                }
                Ok(None)
            }
            Err(failure) => Err(failure),
        }
    }

    fn number(&mut self, radix: u32) -> Step<u32> {
        let start = self.pos;
        let mut value: u32 = 0;
        while let Some(digit) = self.peek().and_then(|b| (b as char).to_digit(radix)) {
            value = value
                .saturating_mul(radix)
                .saturating_add(digit)
                .min(0x11_0000);
            self.pos += 1;
        }
        if self.pos == start {
            return Err(self.unexpected());
        }
        Ok(value)
    }

    fn attribute_value(&mut self) -> Step<String> {
        let quote = self
            .peek()
            .filter(|&b| b == b'"' || b == b'\'')
            .ok_or_else(|| self.unexpected())?;
        self.pos += 1;
        let mut value = String::new();
        let mut run = self.pos;
        loop {
            let Some(b) = self.peek() else {
                return Err(syntax_at("unclosed token", self.bytes.len()));
            };
            if b == quote || matches!(b, b'<' | b'&' | b'\t' | b'\n' | b'\r') {
                value.push_str(&self.text[run..self.pos]);
            }
            match b {
                _ if b == quote => {
                    self.pos += 1;
                    return Ok(value);
                }
                b'<' => return Err(self.unexpected()),
                b'&' => {
                    if let Some(c) = self.attribute_reference()? {
                        value.push(c);
                    }
                }
                b'\t' | b'\n' => {
                    value.push(' ');
                    self.pos += 1;
                }
                b'\r' => {
                    value.push(' ');
                    self.pos += 1;
                    if self.peek() == Some(b'\n') {
                        self.pos += 1;
                    }
                }
                _ => {
                    self.pos += 1;
                    continue;
                }
            }
            run = self.pos;
        }
    }

    fn start_tag(&mut self) -> Step {
        let tag_start = self.pos;
        self.pos += 1;
        let qname = self.name()?;
        let mut raw: Vec<(&'a str, String, usize)> = Vec::new();
        let empty = loop {
            let spaced = self.skip_space();
            if self.eat("/>") {
                break true;
            }
            if self.eat(">") {
                break false;
            }
            if !spaced {
                return Err(self.unexpected());
            }
            let at = self.pos;
            let name = self.name()?;
            self.equals()?;
            raw.push((name, self.attribute_value()?, at));
        };
        let mut seen = HashSet::with_capacity(raw.len());
        if let Some((_, _, at)) = raw.iter().find(|(name, _, _)| !seen.insert(*name)) {
            return Err(syntax_at("duplicate attribute", *at));
        }
        if let Some(declared) = self.attlists.get(qname) {
            for (name, value, _) in &mut raw {
                if declared.iter().any(|d| d.name == *name && d.tokenized) {
                    *value = collapse_spaces(value);
                }
            }
            for attribute in declared {
                if let Some(default) = &attribute.default {
                    if !seen.contains(attribute.name) {
                        raw.push((attribute.name, default.clone(), tag_start));
                    }
                }
            }
        }

        // Declarations first: they bind for the element's own name and
        // attributes as well as for its content.
        let mut declared = Vec::new();
        let mut attributes = Vec::with_capacity(raw.len());
        for (name, value, at) in raw {
            let prefix = match name.strip_prefix("xmlns") {
                Some("") => "",
                Some(rest) if rest.starts_with(':') => {
                    let prefix = &rest[1..];
                    if !is_ncname(prefix, self.letters) {
                        return Err(syntax_at("not well-formed (invalid token)", at));
                    }
                    prefix
                }
                _ => {
                    attributes.push((name, value, at));
                    continue;
                }
            };
            self.check_binding(prefix, &value)
                .map_err(|message| syntax_at(message, at))?;
            if prefix == "xml" {
                // Binding `xml` to its own namespace changes nothing.
                continue;
            }
            self.bindings.entry(prefix).or_default().push(value);
            declared.push(prefix);
        }

        let name = match split_qname(qname, self.letters) {
            Some((Some(prefix), local)) => {
                expanded(Some(self.bound(prefix, tag_start + 1)?), local)
            }
            Some((None, local)) => expanded(self.default_namespace(), local),
            None => return Err(syntax_at("not well-formed (invalid token)", tag_start + 1)),
        };
        let mut resolved = Vec::with_capacity(attributes.len());
        let mut unique = HashSet::with_capacity(attributes.len());
        for (qname, value, at) in attributes {
            let key = match split_qname(qname, self.letters) {
                Some((Some(prefix), local)) => expanded(Some(self.bound(prefix, at)?), local),
                Some((None, local)) => local.to_string(),
                None => return Err(syntax_at("not well-formed (invalid token)", at)),
            };
            if !unique.insert(key.clone()) {
                return Err(syntax_at("duplicate attribute", at));
            }
            resolved.push((key, value));
        }
        self.found.push(Element {
            name,
            attributes: resolved,
        });

        let element = Open { qname, declared };
        if empty {
            self.close(element);
        } else {
            self.open.push(element);
        }
        Ok(())
    }

    /// The namespace rules expat enforces on a declaration.
    fn check_binding(&self, prefix: &str, uri: &str) -> Result<(), &'static str> {
        if uri.contains('}') {
            // expat's separator, refused inside a URI since CVE-2022-25236.
            return Err("syntax error");
        }
        match prefix {
            "xml" if uri != XML_NAMESPACE => Err(
                "reserved prefix (xml) must not be undeclared or bound to another namespace name",
            ),
            "xml" => Ok(()),
            "xmlns" => Err("reserved prefix (xmlns) must not be declared or undeclared"),
            _ if uri == XML_NAMESPACE || uri == XMLNS_NAMESPACE => {
                Err("prefix must not be bound to one of the reserved namespace names")
            }
            "" => Ok(()),
            _ if uri.is_empty() => Err("unbinding a prefix is not allowed"),
            _ => Ok(()),
        }
    }

    fn default_namespace(&self) -> Option<&str> {
        self.bindings
            .get("")
            .and_then(|uris| uris.last())
            .map(String::as_str)
    }

    fn bound(&self, prefix: &str, at: usize) -> Step<&str> {
        if prefix == "xml" {
            return Ok(XML_NAMESPACE);
        }
        self.bindings
            .get(prefix)
            .and_then(|uris| uris.last())
            .map(String::as_str)
            .filter(|_| prefix != "xmlns")
            .ok_or_else(|| syntax_at("unbound prefix", at))
    }

    fn end_tag(&mut self) -> Step {
        self.pos += "</".len();
        let at = self.pos;
        let qname = self.name()?;
        self.skip_space();
        self.expect(">")?;
        let element = self
            .open
            .pop()
            .ok_or_else(|| syntax_at("junk after document element", at))?;
        if element.qname != qname {
            return Err(syntax_at("mismatched tag", at - 2));
        }
        self.close(element);
        Ok(())
    }

    fn close(&mut self, element: Open<'a>) {
        for prefix in element.declared {
            if let Some(uris) = self.bindings.get_mut(prefix) {
                uris.pop();
            }
        }
    }
}
