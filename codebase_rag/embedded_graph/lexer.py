"""Tokenizer for the embedded engine's Cypher dialect."""

from __future__ import annotations

from dataclasses import dataclass

from ..constants.embedded_graph import (
    EG_BACKSLASH,
    EG_BACKTICK,
    EG_BLOCK_COMMENT_CLOSE,
    EG_BLOCK_COMMENT_OPEN,
    EG_EMPTY,
    EG_ERR_SYNTAX,
    EG_ERR_UNTERMINATED_COMMENT,
    EG_ERR_UNTERMINATED_IDENT,
    EG_ERR_UNTERMINATED_STRING,
    EG_EXPONENT,
    EG_HEX_BASE,
    EG_IDENT_EXTRA,
    EG_LINE_COMMENT,
    EG_NEWLINE,
    EG_PARAM_SIGIL,
    EG_PUNCTUATION_1,
    EG_PUNCTUATION_2,
    EG_QUOTES,
    EG_SIGNS,
    EG_STRING_ESCAPES,
    EG_UNICODE_ESCAPE,
    EG_UNICODE_ESCAPE_LEN,
    EG_VALUE_CLOSERS,
    EgPunct,
    EgTok,
)


class CypherSyntaxError(ValueError):
    def __init__(self, pos: int, message: str) -> None:
        super().__init__(EG_ERR_SYNTAX.format(pos=pos, message=message))
        self.pos = pos


@dataclass(frozen=True, slots=True)
class Token:
    kind: EgTok
    text: str
    value: str | int | float | None
    pos: int

    def is_word(self, word: str) -> bool:
        return self.kind == EgTok.IDENT and self.text.upper() == word

    def is_punct(self, text: str) -> bool:
        return self.kind == EgTok.PUNCT and self.text == text


def tokenize(text: str) -> list[Token]:
    tokens: list[Token] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch.isspace():
            i += 1
            continue
        if text.startswith(EG_LINE_COMMENT, i):
            end = text.find(EG_NEWLINE, i)
            i = n if end < 0 else end + 1
            continue
        if text.startswith(EG_BLOCK_COMMENT_OPEN, i):
            end = text.find(EG_BLOCK_COMMENT_CLOSE, i + 2)
            if end < 0:
                raise CypherSyntaxError(i, EG_ERR_UNTERMINATED_COMMENT)
            i = end + 2
            continue
        if ch in EG_QUOTES:
            value, i_end = _read_string(text, i)
            tokens.append(Token(EgTok.STRING, text[i:i_end], value, i))
            i = i_end
            continue
        if ch == EG_BACKTICK:
            value, i_end = _read_backtick(text, i)
            tokens.append(Token(EgTok.QUOTED_IDENT, value, value, i))
            i = i_end
            continue
        if ch == EG_PARAM_SIGIL:
            j = i + 1
            while j < n and (text[j].isalnum() or text[j] == EG_IDENT_EXTRA):
                j += 1
            tokens.append(Token(EgTok.PARAM, text[i:j], text[i + 1 : j], i))
            i = j
            continue
        if ch.isdigit() or (
            ch == EgPunct.DOT
            and i + 1 < n
            and text[i + 1].isdigit()
            and not _after_value(tokens)
        ):
            token, i = _read_number(text, i)
            tokens.append(token)
            continue
        if ch.isalpha() or ch == EG_IDENT_EXTRA:
            j = i + 1
            while j < n and (text[j].isalnum() or text[j] == EG_IDENT_EXTRA):
                j += 1
            tokens.append(Token(EgTok.IDENT, text[i:j], text[i:j], i))
            i = j
            continue
        two = text[i : i + 2]
        if two in EG_PUNCTUATION_2:
            tokens.append(Token(EgTok.PUNCT, two, two, i))
            i += 2
            continue
        if ch in EG_PUNCTUATION_1:
            tokens.append(Token(EgTok.PUNCT, ch, ch, i))
            i += 1
            continue
        raise CypherSyntaxError(i, repr(ch))
    tokens.append(Token(EgTok.EOF, EG_EMPTY, None, n))
    return tokens


def _after_value(tokens: list[Token]) -> bool:
    # `n.prop` lexes `.` as an accessor, `1..2` as a range; only a `.` that
    # cannot continue a value starts a number such as `.5`.
    if not tokens:
        return False
    last = tokens[-1]
    return last.kind != EgTok.PUNCT or last.text in EG_VALUE_CLOSERS


def _read_number(text: str, i: int) -> tuple[Token, int]:
    n = len(text)
    j = i
    is_float = False
    while j < n and text[j].isdigit():
        j += 1
    # `1..3` is a range, not the float `1.`
    if j < n and text[j] == EgPunct.DOT and not text.startswith(EgPunct.RANGE, j):
        is_float = True
        j += 1
        while j < n and text[j].isdigit():
            j += 1
    if j < n and text[j] in EG_EXPONENT:
        k = j + 1
        if k < n and text[k] in EG_SIGNS:
            k += 1
        if k < n and text[k].isdigit():
            is_float = True
            j = k
            while j < n and text[j].isdigit():
                j += 1
    raw = text[i:j]
    if is_float:
        return Token(EgTok.FLOAT, raw, float(raw), i), j
    return Token(EgTok.INT, raw, int(raw), i), j


def _read_string(text: str, start: int) -> tuple[str, int]:
    quote = text[start]
    out: list[str] = []
    i, n = start + 1, len(text)
    while i < n:
        ch = text[i]
        if ch == EG_BACKSLASH and i + 1 < n:
            nxt = text[i + 1]
            if nxt == EG_UNICODE_ESCAPE:
                code = text[i + 2 : i + 2 + EG_UNICODE_ESCAPE_LEN]
                out.append(chr(int(code, EG_HEX_BASE)))
                i += 2 + EG_UNICODE_ESCAPE_LEN
                continue
            out.append(EG_STRING_ESCAPES.get(nxt, nxt))
            i += 2
            continue
        if ch == quote:
            return EG_EMPTY.join(out), i + 1
        out.append(ch)
        i += 1
    raise CypherSyntaxError(start, EG_ERR_UNTERMINATED_STRING)


def _read_backtick(text: str, start: int) -> tuple[str, int]:
    out: list[str] = []
    i, n = start + 1, len(text)
    while i < n:
        if text[i] == EG_BACKTICK:
            if text.startswith(EG_BACKTICK * 2, i):
                out.append(EG_BACKTICK)
                i += 2
                continue
            return EG_EMPTY.join(out), i + 1
        out.append(text[i])
        i += 1
    raise CypherSyntaxError(start, EG_ERR_UNTERMINATED_IDENT)
