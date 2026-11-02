//! A strict reader for the XML project files cgr reads (`.csproj`, `.slnx`).
//!
//! It reports each element's name and attributes as `xml.etree.ElementTree`
//! does on top of expat: `{uri}local` for a namespaced name, the plain local
//! name otherwise, `xmlns` declarations consumed rather than reported, and
//! attribute values normalised (every literal tab, newline and carriage
//! return becomes a space; a CR LF pair becomes one).
//!
//! It never expands an entity a document declares: a document with a
//! DOCTYPE is declined, so the safe parser on the Python side
//! (`codebase_rag/utils/safe_xml.py`) refuses its entity declarations as
//! before. It declines, too, whenever expat's answer would rest on something
//! it does not model: an encoding other than UTF-8, a non-ASCII name (expat's
//! name tables predate XML 1.0's fifth edition), a namespace declaration
//! outside the plain cases, or any document that is not well-formed. So when
//! it does answer, the answer is the one ElementTree gives; when it is not
//! sure, the Python side decides.

use std::collections::{HashMap, HashSet};

const XML_NAMESPACE: &str = "http://www.w3.org/XML/1998/namespace";
const XMLNS_NAMESPACE: &str = "http://www.w3.org/2000/xmlns/";
const UTF8_BOM: &[u8] = b"\xEF\xBB\xBF";

/// One element: its name and attributes, spelled as ElementTree spells them.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Element {
    pub name: String,
    pub attributes: Vec<(String, String)>,
}

/// Every element of `doc` in document order (the order of `Element.iter()`),
/// or None when this reader leaves the document to the Python parser.
pub fn elements(doc: &[u8]) -> Option<Vec<Element>> {
    let doc = doc.strip_prefix(UTF8_BOM).unwrap_or(doc);
    let text = std::str::from_utf8(doc).ok()?;
    // XML's Char production covers the whole document, comments included,
    // so one pass settles it.
    if !text.chars().all(is_xml_char) {
        return None;
    }
    Reader::new(text).document()
}

/// The attributes of every element named `name`, in document order:
/// `[e.attrib for e in root.iter(name)]`.
pub fn find_all(doc: &[u8], name: &str) -> Option<Vec<Vec<(String, String)>>> {
    Some(
        elements(doc)?
            .into_iter()
            .filter(|element| element.name == name)
            .map(|element| element.attributes)
            .collect(),
    )
}

fn is_xml_char(c: char) -> bool {
    matches!(c, '\t' | '\n' | '\r' | '\u{20}'..='\u{D7FF}' | '\u{E000}'..='\u{FFFD}' | '\u{10000}'..)
}

fn is_space(b: u8) -> bool {
    matches!(b, b' ' | b'\t' | b'\r' | b'\n')
}

fn is_name_start(b: u8) -> bool {
    b.is_ascii_alphabetic() || b == b'_' || b == b':'
}

fn is_name_char(b: u8) -> bool {
    is_name_start(b) || b.is_ascii_digit() || b == b'-' || b == b'.'
}

/// A name without a colon that may start a name after one: `p:5` and
/// `p:-x` are not names to expat, though `5` and `-x` are name characters.
fn is_ncname(name: &str) -> bool {
    name.bytes()
        .next()
        .is_some_and(|b| b.is_ascii_alphabetic() || b == b'_')
        && !name.contains(':')
}

/// `(prefix, local)`, or None for a name namespaces reject: a colon at
/// either end, more than one, or a part that cannot start a name.
fn split_qname(name: &str) -> Option<(Option<&str>, &str)> {
    match name.split_once(':') {
        None => Some((None, name)),
        Some((prefix, local)) if is_ncname(prefix) && is_ncname(local) => {
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
}

impl<'a> Reader<'a> {
    fn new(text: &'a str) -> Self {
        Reader {
            text,
            bytes: text.as_bytes(),
            pos: 0,
            found: Vec::new(),
            open: Vec::new(),
            bindings: HashMap::new(),
        }
    }

    fn document(mut self) -> Option<Vec<Element>> {
        self.xml_declaration()?;
        self.misc()?;
        let at_start_tag = self.peek() == Some(b'<')
            && self
                .bytes
                .get(self.pos + 1)
                .is_some_and(|&b| is_name_start(b));
        if !at_start_tag {
            // A DOCTYPE, text before the root, or no root at all.
            return None;
        }
        self.start_tag()?;
        while !self.open.is_empty() {
            self.content()?;
        }
        self.misc()?;
        (self.pos == self.bytes.len()).then_some(self.found)
    }

    fn peek(&self) -> Option<u8> {
        self.bytes.get(self.pos).copied()
    }

    fn at(&self, literal: &str) -> bool {
        self.bytes[self.pos..].starts_with(literal.as_bytes())
    }

    fn eat(&mut self, literal: &str) -> Option<()> {
        self.at(literal).then(|| self.pos += literal.len())
    }

    fn skip_space(&mut self) -> bool {
        let start = self.pos;
        while self.peek().is_some_and(is_space) {
            self.pos += 1;
        }
        self.pos > start
    }

    /// The index just past the next `literal`, searching from the cursor.
    fn find(&self, literal: &str) -> Option<usize> {
        self.text[self.pos..]
            .find(literal)
            .map(|at| self.pos + at + literal.len())
    }

    /// An ASCII XML name. A non-ASCII byte inside one declines: expat may
    /// count it a name character or not.
    fn name(&mut self) -> Option<&'a str> {
        let start = self.pos;
        if !self.peek().is_some_and(is_name_start) {
            return None;
        }
        while let Some(b) = self.peek() {
            if is_name_char(b) {
                self.pos += 1;
            } else if b.is_ascii() {
                break;
            } else {
                return None;
            }
        }
        Some(&self.text[start..self.pos])
    }

    /// `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>`, only at
    /// the very start and only as written there; anything else declines.
    fn xml_declaration(&mut self) -> Option<()> {
        if !self.at("<?xml") || self.bytes.get(5).is_some_and(|&b| is_name_char(b)) {
            return Some(());
        }
        self.pos = 5;
        if !self.skip_space() || self.eat("version").is_none() {
            return None;
        }
        if self.declaration_value()? != "1.0" {
            return None;
        }
        let mut spaced = self.skip_space();
        if spaced && self.eat("encoding").is_some() {
            if !self.declaration_value()?.eq_ignore_ascii_case("utf-8") {
                return None;
            }
            spaced = self.skip_space();
        }
        if spaced && self.eat("standalone").is_some() {
            if !matches!(self.declaration_value()?, "yes" | "no") {
                return None;
            }
            self.skip_space();
        }
        self.eat("?>")
    }

    fn declaration_value(&mut self) -> Option<&'a str> {
        self.equals()?;
        let quote = self.peek().filter(|&b| b == b'"' || b == b'\'')?;
        let start = self.pos + 1;
        let len = self.bytes[start..].iter().position(|&b| b == quote)?;
        self.pos = start + len + 1;
        Some(&self.text[start..start + len])
    }

    fn equals(&mut self) -> Option<()> {
        self.skip_space();
        self.eat("=")?;
        self.skip_space();
        Some(())
    }

    /// Whitespace, comments and processing instructions, as may surround
    /// the root element.
    fn misc(&mut self) -> Option<()> {
        loop {
            self.skip_space();
            if self.at("<!--") {
                self.comment()?;
            } else if self.at("<?") {
                self.processing_instruction()?;
            } else {
                return Some(());
            }
        }
    }

    fn comment(&mut self) -> Option<()> {
        self.pos += "<!--".len();
        // `--` may appear only as the start of `-->`.
        let end = self.find("--")?;
        if self.bytes.get(end) != Some(&b'>') {
            return None;
        }
        self.pos = end + 1;
        Some(())
    }

    fn processing_instruction(&mut self) -> Option<()> {
        self.pos += "<?".len();
        let target = self.name()?;
        if target.contains(':') || target.eq_ignore_ascii_case("xml") {
            return None;
        }
        if self.eat("?>").is_some() {
            return Some(());
        }
        if !self.skip_space() {
            return None;
        }
        self.pos = self.find("?>")?;
        Some(())
    }

    /// One step inside an open element: a tag, a comment, a processing
    /// instruction, a CDATA section, a reference or a run of text.
    fn content(&mut self) -> Option<()> {
        match self.peek()? {
            b'<' => {
                if self.at("</") {
                    self.end_tag()
                } else if self.at("<!--") {
                    self.comment()
                } else if self.at("<![CDATA[") {
                    self.pos = self.find("]]>")?;
                    Some(())
                } else if self.at("<?") {
                    self.processing_instruction()
                } else {
                    self.start_tag()
                }
            }
            b'&' => self.reference().map(drop),
            _ => {
                let len = self.bytes[self.pos..]
                    .iter()
                    .position(|&b| b == b'<' || b == b'&')
                    .unwrap_or(self.bytes.len() - self.pos);
                if self.text[self.pos..self.pos + len].contains("]]>") {
                    return None;
                }
                self.pos += len;
                Some(())
            }
        }
    }

    /// `&lt;`, `&#60;` or `&#x3c;`, as the character it stands for. With no
    /// DTD the five predefined entities are the only names there are.
    fn reference(&mut self) -> Option<char> {
        self.pos += 1;
        let code = if self.eat("#x").is_some() {
            self.number(16)?
        } else if self.eat("#").is_some() {
            self.number(10)?
        } else {
            let c = match self.name()? {
                "lt" => '<',
                "gt" => '>',
                "amp" => '&',
                "apos" => '\'',
                "quot" => '"',
                _ => return None,
            };
            self.eat(";")?;
            return Some(c);
        };
        self.eat(";")?;
        char::from_u32(code).filter(|&c| is_xml_char(c))
    }

    fn number(&mut self, radix: u32) -> Option<u32> {
        let mut value: u32 = 0;
        let start = self.pos;
        while let Some(digit) = self.peek().and_then(|b| (b as char).to_digit(radix)) {
            value = value.checked_mul(radix)?.checked_add(digit)?;
            if value > 0x10_FFFF {
                return None;
            }
            self.pos += 1;
        }
        (self.pos > start).then_some(value)
    }

    fn attribute_value(&mut self) -> Option<String> {
        let quote = self.peek().filter(|&b| b == b'"' || b == b'\'')?;
        self.pos += 1;
        let mut value = String::new();
        let mut run = self.pos;
        loop {
            let b = self.peek()?;
            if b == quote || matches!(b, b'<' | b'&' | b'\t' | b'\n' | b'\r') {
                value.push_str(&self.text[run..self.pos]);
            }
            match b {
                _ if b == quote => {
                    self.pos += 1;
                    return Some(value);
                }
                b'<' => return None,
                b'&' => value.push(self.reference()?),
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

    fn start_tag(&mut self) -> Option<()> {
        self.pos += 1;
        let qname = self.name()?;
        let mut raw: Vec<(&'a str, String)> = Vec::new();
        let empty = loop {
            let spaced = self.skip_space();
            if self.eat("/>").is_some() {
                break true;
            }
            if self.eat(">").is_some() {
                break false;
            }
            if !spaced {
                return None;
            }
            let name = self.name()?;
            self.equals()?;
            raw.push((name, self.attribute_value()?));
        };
        let mut seen = HashSet::with_capacity(raw.len());
        if !raw.iter().all(|(name, _)| seen.insert(*name)) {
            return None;
        }

        // Declarations first: they bind for the element's own name and
        // attributes as well as for its content.
        let mut declared = Vec::new();
        let mut attributes = Vec::with_capacity(raw.len());
        for (name, value) in raw {
            let prefix = match name.strip_prefix("xmlns") {
                Some("") => "",
                Some(rest) if rest.starts_with(':') => {
                    let prefix = &rest[1..];
                    if !is_ncname(prefix)
                        || prefix
                            .get(..3)
                            .is_some_and(|p| p.eq_ignore_ascii_case("xml"))
                        || value.is_empty()
                    {
                        return None;
                    }
                    prefix
                }
                _ => {
                    attributes.push((name, value));
                    continue;
                }
            };
            // expat refuses its separator inside a URI (CVE-2022-25236), and
            // the two reserved namespaces have rules of their own.
            if value.contains('}') || value == XML_NAMESPACE || value == XMLNS_NAMESPACE {
                return None;
            }
            self.bindings.entry(prefix).or_default().push(value);
            declared.push(prefix);
        }

        let name = match split_qname(qname)? {
            (Some(prefix), local) => expanded(Some(self.bound(prefix)?), local),
            (None, local) => expanded(self.bound(""), local),
        };
        let mut resolved = Vec::with_capacity(attributes.len());
        let mut unique = HashSet::with_capacity(attributes.len());
        for (qname, value) in attributes {
            let key = match split_qname(qname)? {
                (Some("xml"), local) => expanded(Some(XML_NAMESPACE), local),
                (Some(prefix), local) => expanded(Some(self.bound(prefix)?), local),
                (None, local) => local.to_string(),
            };
            if !unique.insert(key.clone()) {
                return None;
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
        Some(())
    }

    /// The URI `prefix` is bound to; the default namespace may be unbound
    /// (None, or Some("") once undeclared), any other prefix may not.
    fn bound(&self, prefix: &str) -> Option<&str> {
        if prefix == "xml" || prefix == "xmlns" {
            return None;
        }
        self.bindings
            .get(prefix)
            .and_then(|uris| uris.last())
            .map(String::as_str)
    }

    fn end_tag(&mut self) -> Option<()> {
        self.pos += "</".len();
        let qname = self.name()?;
        self.skip_space();
        self.eat(">")?;
        let element = self.open.pop()?;
        if element.qname != qname {
            return None;
        }
        self.close(element);
        Some(())
    }

    fn close(&mut self, element: Open<'a>) {
        for prefix in element.declared {
            if let Some(uris) = self.bindings.get_mut(prefix) {
                uris.pop();
            }
        }
    }
}
