"""Tokenizer for the embedded engine's Cypher dialect."""

from __future__ import annotations

from dataclasses import dataclass

from . import constants as ecs


class CypherSyntaxError(ValueError):
    def __init__(self, pos: int, message: str) -> None:
        super().__init__(ecs.EG_ERR_SYNTAX.format(pos=pos, message=message))
        self.pos = pos


@dataclass(frozen=True, slots=True)
class Token:
    kind: ecs.EgTok
    text: str
    value: str | int | float | None
    pos: int

    def is_word(self, word: str) -> bool:
        return self.kind == ecs.EgTok.IDENT and self.text.upper() == word

    def is_punct(self, text: str) -> bool:
        return self.kind == ecs.EgTok.PUNCT and self.text == text


def tokenize(text: str) -> list[Token]:
    tokens: list[Token] = []
    i, n = 0, len(text)
    while i < n:
        if text[i].isspace():
            i += 1
            continue
        comment_end = _comment_end(text, i)
        if comment_end is not None:
            i = comment_end
            continue
        token, i = _read_token(text, i, tokens)
        tokens.append(token)
    tokens.append(Token(ecs.EgTok.EOF, ecs.EG_EMPTY, None, n))
    return tokens


def _comment_end(text: str, i: int) -> int | None:
    """Where a comment starting at `i` ends; None if none starts there."""
    if text.startswith(ecs.EG_LINE_COMMENT, i):
        end = text.find(ecs.EG_NEWLINE, i)
        return len(text) if end < 0 else end + 1
    if text.startswith(ecs.EG_BLOCK_COMMENT_OPEN, i):
        end = text.find(ecs.EG_BLOCK_COMMENT_CLOSE, i + 2)
        if end < 0:
            raise CypherSyntaxError(i, ecs.EG_ERR_UNTERMINATED_COMMENT)
        return end + 2
    return None


def _read_token(text: str, i: int, tokens: list[Token]) -> tuple[Token, int]:
    ch = text[i]
    if ch in ecs.EG_QUOTES:
        value, end = _read_string(text, i)
        return Token(ecs.EgTok.STRING, text[i:end], value, i), end
    if ch == ecs.EG_BACKTICK:
        value, end = _read_backtick(text, i)
        return Token(ecs.EgTok.QUOTED_IDENT, value, value, i), end
    if ch == ecs.EG_PARAM_SIGIL:
        end = _word_end(text, i + 1)
        return Token(ecs.EgTok.PARAM, text[i:end], text[i + 1 : end], i), end
    if ch.isdigit() or (
        ch == ecs.EgPunct.DOT
        and i + 1 < len(text)
        and text[i + 1].isdigit()
        and not _after_value(tokens)
    ):
        return _read_number(text, i)
    if ch.isalpha() or ch == ecs.EG_IDENT_EXTRA:
        end = _word_end(text, i + 1)
        return Token(ecs.EgTok.IDENT, text[i:end], text[i:end], i), end
    two = text[i : i + 2]
    if two in ecs.EG_PUNCTUATION_2:
        return Token(ecs.EgTok.PUNCT, two, two, i), i + 2
    if ch in ecs.EG_PUNCTUATION_1:
        return Token(ecs.EgTok.PUNCT, ch, ch, i), i + 1
    raise CypherSyntaxError(i, repr(ch))


def _word_end(text: str, i: int) -> int:
    n = len(text)
    while i < n and (text[i].isalnum() or text[i] == ecs.EG_IDENT_EXTRA):
        i += 1
    return i


def _after_value(tokens: list[Token]) -> bool:
    # `n.prop` lexes `.` as an accessor, `1..2` as a range; only a `.` that
    # cannot continue a value starts a number such as `.5`.
    if not tokens:
        return False
    last = tokens[-1]
    return last.kind != ecs.EgTok.PUNCT or last.text in ecs.EG_VALUE_CLOSERS


def _read_number(text: str, i: int) -> tuple[Token, int]:
    int_end = _digits_end(text, i)
    end = _exponent_end(text, _fraction_end(text, int_end))
    raw = text[i:end]
    # Either a fraction or an exponent carries the number past its digits.
    if end > int_end:
        return Token(ecs.EgTok.FLOAT, raw, float(raw), i), end
    return Token(ecs.EgTok.INT, raw, int(raw), i), end


def _digits_end(text: str, i: int) -> int:
    n = len(text)
    while i < n and text[i].isdigit():
        i += 1
    return i


def _fraction_end(text: str, i: int) -> int:
    # `1..3` is a range, not the float `1.`
    if (
        i < len(text)
        and text[i] == ecs.EgPunct.DOT
        and not text.startswith(ecs.EgPunct.RANGE, i)
    ):
        return _digits_end(text, i + 1)
    return i


def _exponent_end(text: str, i: int) -> int:
    # `1e` and `1e+` keep the `e` out of the number; it lexes as a name.
    n = len(text)
    if i >= n or text[i] not in ecs.EG_EXPONENT:
        return i
    k = i + 1
    if k < n and text[k] in ecs.EG_SIGNS:
        k += 1
    if k < n and text[k].isdigit():
        return _digits_end(text, k)
    return i


def _read_string(text: str, start: int) -> tuple[str, int]:
    quote = text[start]
    out: list[str] = []
    i, n = start + 1, len(text)
    while i < n:
        ch = text[i]
        if ch == ecs.EG_BACKSLASH and i + 1 < n:
            nxt = text[i + 1]
            if nxt == ecs.EG_UNICODE_ESCAPE:
                code = text[i + 2 : i + 2 + ecs.EG_UNICODE_ESCAPE_LEN]
                out.append(chr(int(code, ecs.EG_HEX_BASE)))
                i += 2 + ecs.EG_UNICODE_ESCAPE_LEN
                continue
            out.append(ecs.EG_STRING_ESCAPES.get(nxt, nxt))
            i += 2
            continue
        if ch == quote:
            return ecs.EG_EMPTY.join(out), i + 1
        out.append(ch)
        i += 1
    raise CypherSyntaxError(start, ecs.EG_ERR_UNTERMINATED_STRING)


def _read_backtick(text: str, start: int) -> tuple[str, int]:
    out: list[str] = []
    i, n = start + 1, len(text)
    while i < n:
        if text[i] == ecs.EG_BACKTICK:
            if text.startswith(ecs.EG_BACKTICK * 2, i):
                out.append(ecs.EG_BACKTICK)
                i += 2
                continue
            return ecs.EG_EMPTY.join(out), i + 1
        out.append(text[i])
        i += 1
    raise CypherSyntaxError(start, ecs.EG_ERR_UNTERMINATED_IDENT)
