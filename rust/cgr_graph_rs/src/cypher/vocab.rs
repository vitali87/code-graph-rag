//! The dialect's words and symbols, mirroring `EgKw`, `EgPunct` and the
//! `EG_EXPECT_*` / `EG_ERR_*` messages in
//! `codebase_rag/constants/embedded_graph.py`. The Python parser is the
//! reference, so these are copies rather than a second definition: the
//! parity suite (`test_embedded_graph_rust_parser.py`) compares every AST and
//! error message the two produce and fails on any drift.

/// Defines a keyword enum whose variants each spell one upper-case word.
macro_rules! keywords {
    ($($variant:ident = $text:literal),+ $(,)?) => {
        /// A Cypher keyword (`EgKw`), as the parser compares it: upper case.
        #[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
        pub enum Kw {
            $($variant),+
        }

        impl Kw {
            pub const fn as_str(self) -> &'static str {
                match self {
                    $(Kw::$variant => $text),+
                }
            }

            fn from_upper(word: &str) -> Option<Kw> {
                match word {
                    $($text => Some(Kw::$variant),)+
                    _ => None,
                }
            }
        }
    };
}

keywords! {
    Match = "MATCH",
    Optional = "OPTIONAL",
    Where = "WHERE",
    With = "WITH",
    Return = "RETURN",
    Unwind = "UNWIND",
    Union = "UNION",
    All = "ALL",
    Create = "CREATE",
    Merge = "MERGE",
    Set = "SET",
    Remove = "REMOVE",
    Delete = "DELETE",
    Detach = "DETACH",
    Foreach = "FOREACH",
    Call = "CALL",
    Yield = "YIELD",
    Order = "ORDER",
    By = "BY",
    Skip = "SKIP",
    Limit = "LIMIT",
    On = "ON",
    As = "AS",
    Distinct = "DISTINCT",
    Asc = "ASC",
    Ascending = "ASCENDING",
    Desc = "DESC",
    Descending = "DESCENDING",
    Explain = "EXPLAIN",
    Profile = "PROFILE",
    Show = "SHOW",
    Drop = "DROP",
    Index = "INDEX",
    Constraint = "CONSTRAINT",
    Constraints = "CONSTRAINTS",
    Storage = "STORAGE",
    In = "IN",
    Starts = "STARTS",
    Ends = "ENDS",
    Contains = "CONTAINS",
    Is = "IS",
    Not = "NOT",
    Null = "NULL",
    Or = "OR",
    Xor = "XOR",
    And = "AND",
    True = "TRUE",
    False = "FALSE",
    Case = "CASE",
    When = "WHEN",
    Then = "THEN",
    Else = "ELSE",
    End = "END",
    Any = "ANY",
    None = "NONE",
    Single = "SINGLE",
    ShortestPath = "SHORTESTPATH",
    AllShortestPaths = "ALLSHORTESTPATHS",
}

/// No keyword is longer than `ALLSHORTESTPATHS`; a longer identifier is
/// never one, and the case-folding buffer below never needs to grow.
const LONGEST_KEYWORD: usize = 16;

impl Kw {
    /// The keyword an ASCII identifier spells in any case, as Python's
    /// `text.upper() == word` decides it. Identifiers with other characters
    /// never reach here: the lexer hands those queries to Python.
    pub fn lookup(ident: &str) -> Option<Kw> {
        if ident.len() > LONGEST_KEYWORD {
            return None;
        }
        let mut buf = [0u8; LONGEST_KEYWORD];
        let upper = &mut buf[..ident.len()];
        upper.copy_from_slice(ident.as_bytes());
        upper.make_ascii_uppercase();
        std::str::from_utf8(upper).ok().and_then(Kw::from_upper)
    }

    /// `EG_EXPRESSION_STOP_WORDS`: a clause boundary or operator, never a
    /// bare variable (labels, keys and aliases may still use the word).
    pub const fn is_stop_word(self) -> bool {
        matches!(
            self,
            Kw::Match
                | Kw::Optional
                | Kw::Where
                | Kw::With
                | Kw::Return
                | Kw::Unwind
                | Kw::Union
                | Kw::Create
                | Kw::Merge
                | Kw::Set
                | Kw::Remove
                | Kw::Delete
                | Kw::Detach
                | Kw::Foreach
                | Kw::Call
                | Kw::Yield
                | Kw::Order
                | Kw::Skip
                | Kw::Limit
                | Kw::On
                | Kw::As
                | Kw::Then
                | Kw::Else
                | Kw::End
                | Kw::When
                | Kw::Asc
                | Kw::Ascending
                | Kw::Desc
                | Kw::Descending
                | Kw::By
        )
    }
}

/// Punctuation (`EgPunct`): one or two characters.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum Punct {
    LParen,
    RParen,
    LBracket,
    RBracket,
    LBrace,
    RBrace,
    Comma,
    Dot,
    Colon,
    Pipe,
    Semicolon,
    Eq,
    Lt,
    Gt,
    Plus,
    Minus,
    Star,
    Slash,
    Percent,
    Caret,
    Le,
    Ge,
    Ne,
    BangEq,
    Regex,
    PlusEq,
    Range,
}

impl Punct {
    pub const fn as_str(self) -> &'static str {
        match self {
            Punct::LParen => "(",
            Punct::RParen => ")",
            Punct::LBracket => "[",
            Punct::RBracket => "]",
            Punct::LBrace => "{",
            Punct::RBrace => "}",
            Punct::Comma => ",",
            Punct::Dot => ".",
            Punct::Colon => ":",
            Punct::Pipe => "|",
            Punct::Semicolon => ";",
            Punct::Eq => "=",
            Punct::Lt => "<",
            Punct::Gt => ">",
            Punct::Plus => "+",
            Punct::Minus => "-",
            Punct::Star => "*",
            Punct::Slash => "/",
            Punct::Percent => "%",
            Punct::Caret => "^",
            Punct::Le => "<=",
            Punct::Ge => ">=",
            Punct::Ne => "<>",
            Punct::BangEq => "!=",
            Punct::Regex => "=~",
            Punct::PlusEq => "+=",
            Punct::Range => "..",
        }
    }

    /// `EG_PUNCTUATION_2`, which the lexer tries before single characters.
    pub const fn two(first: char, second: char) -> Option<Punct> {
        Some(match (first, second) {
            ('<', '=') => Punct::Le,
            ('>', '=') => Punct::Ge,
            ('<', '>') => Punct::Ne,
            ('!', '=') => Punct::BangEq,
            ('=', '~') => Punct::Regex,
            ('+', '=') => Punct::PlusEq,
            ('.', '.') => Punct::Range,
            _ => return None,
        })
    }

    /// `EG_PUNCTUATION_1`.
    pub const fn one(ch: char) -> Option<Punct> {
        Some(match ch {
            '(' => Punct::LParen,
            ')' => Punct::RParen,
            '[' => Punct::LBracket,
            ']' => Punct::RBracket,
            '{' => Punct::LBrace,
            '}' => Punct::RBrace,
            ',' => Punct::Comma,
            '.' => Punct::Dot,
            ':' => Punct::Colon,
            '|' => Punct::Pipe,
            ';' => Punct::Semicolon,
            '=' => Punct::Eq,
            '<' => Punct::Lt,
            '>' => Punct::Gt,
            '+' => Punct::Plus,
            '-' => Punct::Minus,
            '*' => Punct::Star,
            '/' => Punct::Slash,
            '%' => Punct::Percent,
            '^' => Punct::Caret,
            _ => return None,
        })
    }

    /// `EG_VALUE_CLOSERS`: a `.` after one of these is an accessor
    /// (`f(x).y`), never the start of a number such as `.5`.
    pub const fn closes_value(self) -> bool {
        matches!(self, Punct::RParen | Punct::RBracket | Punct::RBrace)
    }
}

pub const LINE_COMMENT: [char; 2] = ['/', '/'];
pub const BLOCK_COMMENT_OPEN: [char; 2] = ['/', '*'];
pub const BLOCK_COMMENT_CLOSE: [char; 2] = ['*', '/'];
pub const NEWLINE: char = '\n';
pub const IDENT_EXTRA: char = '_';
pub const BACKTICK: char = '`';
pub const QUOTES: [char; 2] = ['\'', '"'];
pub const BACKSLASH: char = '\\';
pub const UNICODE_ESCAPE: char = 'u';
pub const UNICODE_ESCAPE_LEN: usize = 4;
pub const HEX_BASE: u32 = 16;
pub const PARAM_SIGIL: char = '$';
pub const EXPONENT: [char; 2] = ['e', 'E'];
pub const SIGNS: [char; 2] = ['+', '-'];

/// `EG_STRING_ESCAPES`; any other escaped character stands for itself.
pub const fn string_escape(ch: char) -> char {
    match ch {
        'n' => '\n',
        't' => '\t',
        'r' => '\r',
        'b' => '\u{8}',
        'f' => '\u{c}',
        other => other,
    }
}

pub const EXPECT_EXPRESSION: &str = "an expression";
pub const EXPECT_NAME: &str = "a name";
pub const EXPECT_INT: &str = "an integer";
pub const EXPECT_CLAUSE: &str = "a clause";
pub const EXPECT_PATTERN: &str = "a pattern";
pub const EXPECT_SET_ITEM: &str = "a SET item";
pub const EXPECT_SCHEMA: &str = "an index or constraint statement";
pub const END_OF_QUERY: &str = "end of query";

pub const ERR_UNTERMINATED_STRING: &str = "unterminated string literal";
pub const ERR_UNTERMINATED_COMMENT: &str = "unterminated block comment";
pub const ERR_UNTERMINATED_IDENT: &str = "unterminated backtick identifier";

/// `EG_ERR_SYNTAX`, which Python's `CypherSyntaxError` wraps every message in.
pub fn syntax_error(pos: usize, message: &str) -> String {
    format!("Cypher syntax error at offset {pos}: {message}")
}

/// `EG_ERR_UNEXPECTED`, given `found` already rendered by `repr`.
pub fn unexpected(found_repr: &str, expected: &str) -> String {
    format!("unexpected {found_repr}, expected {expected}")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn keywords_match_in_any_case() {
        assert_eq!(Kw::lookup("match"), Some(Kw::Match));
        assert_eq!(Kw::lookup("allShortestPaths"), Some(Kw::AllShortestPaths));
        assert_eq!(Kw::lookup("matches"), None);
        assert_eq!(Kw::lookup("a_much_longer_identifier"), None);
    }

    #[test]
    fn two_character_punctuation_wins_over_one() {
        assert_eq!(Punct::two('<', '='), Some(Punct::Le));
        assert_eq!(Punct::two('<', '-'), None);
        assert_eq!(Punct::one('<'), Some(Punct::Lt));
        assert_eq!(Punct::one('!'), None);
    }
}
