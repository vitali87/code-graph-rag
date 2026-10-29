"""Recursive-descent parser for the embedded engine's openCypher subset.

Covers every query cgr itself issues plus the shapes its Cypher prompt asks a
model to write. Anything outside the subset is a CypherSyntaxError, which the
query tool already turns into a repair attempt.

This is the reference implementation. The Rust extension carries a port
(rust/cgr_graph_rs/src/cypher) that builds the same tree, or raises the same
error, about ten times faster; `parse` uses it when it is installed.
"""

from __future__ import annotations

from collections.abc import Callable
from functools import cache, lru_cache

from . import constants as ecs
from . import cypher_ast as a
from .extension import rust_extension
from .lexer import CypherSyntaxError, Token, tokenize

type Statement = a.Query | a.Explain | a.SchemaCommand

_COMPARISON_OPS: dict[str, ecs.EgBinary] = {
    ecs.EgPunct.EQ: ecs.EgBinary.EQ,
    ecs.EgPunct.NE: ecs.EgBinary.NE,
    ecs.EgPunct.BANG_EQ: ecs.EgBinary.NE,
    ecs.EgPunct.LT: ecs.EgBinary.LT,
    ecs.EgPunct.GT: ecs.EgBinary.GT,
    ecs.EgPunct.LE: ecs.EgBinary.LE,
    ecs.EgPunct.GE: ecs.EgBinary.GE,
    ecs.EgPunct.REGEX: ecs.EgBinary.REGEX,
}
_ADDITIVE_OPS: dict[str, ecs.EgBinary] = {
    ecs.EgPunct.PLUS: ecs.EgBinary.ADD,
    ecs.EgPunct.MINUS: ecs.EgBinary.SUB,
}
_MULTIPLICATIVE_OPS: dict[str, ecs.EgBinary] = {
    ecs.EgPunct.STAR: ecs.EgBinary.MUL,
    ecs.EgPunct.SLASH: ecs.EgBinary.DIV,
    ecs.EgPunct.PERCENT: ecs.EgBinary.MOD,
}
_QUANTIFIERS: dict[str, ecs.EgQuantifier] = {
    ecs.EgKw.ANY: ecs.EgQuantifier.ANY,
    ecs.EgKw.ALL: ecs.EgQuantifier.ALL,
    ecs.EgKw.NONE: ecs.EgQuantifier.NONE,
    ecs.EgKw.SINGLE: ecs.EgQuantifier.SINGLE,
}
_WORD_LITERALS: dict[str, bool | None] = {
    ecs.EgKw.TRUE: True,
    ecs.EgKw.FALSE: False,
    ecs.EgKw.NULL: None,
}
# Keyed by (CREATE rather than DROP, INDEX rather than CONSTRAINT).
_SCHEMA_KINDS: dict[tuple[bool, bool], ecs.EgSchemaKind] = {
    (True, True): ecs.EgSchemaKind.CREATE_INDEX,
    (True, False): ecs.EgSchemaKind.CREATE_CONSTRAINT,
    (False, True): ecs.EgSchemaKind.DROP_INDEX,
    (False, False): ecs.EgSchemaKind.DROP_CONSTRAINT,
}


@lru_cache(maxsize=ecs.EG_COMPILE_CACHE_SIZE)
def parse(text: str) -> Statement:
    rust = rust_parser()
    if rust is not None and (stmt := rust(text)) is not None:
        return stmt
    return parse_python(text)


def parse_python(text: str) -> Statement:
    return _Parser(tokenize(text), text).statement()


@cache
def rust_parser() -> Callable[[str], Statement | None] | None:
    """The extension's parser, if this build of it has one.

    It answers None for the rare query whose meaning rests on Python itself
    (non-ASCII outside strings and backtick names, integers beyond 64 bits,
    very deep nesting), which `parse` then hands to `parse_python`.
    """
    return getattr(rust_extension(), ecs.EG_RUST_PARSE_CYPHER, None)


class _Parser:
    def __init__(self, tokens: list[Token], source: str) -> None:
        self.tokens = tokens
        self.source = source
        self.i = 0

    # ------------------------------------------------------------ primitives

    @property
    def tok(self) -> Token:
        return self.tokens[self.i]

    def peek(self, offset: int = 1) -> Token:
        return self.tokens[min(self.i + offset, len(self.tokens) - 1)]

    def advance(self) -> Token:
        tok = self.tokens[self.i]
        if tok.kind != ecs.EgTok.EOF:
            self.i += 1
        return tok

    def error(self, expected: str) -> CypherSyntaxError:
        tok = self.tok
        found = tok.text or ecs.EG_END_OF_QUERY
        return CypherSyntaxError(
            tok.pos, ecs.EG_ERR_UNEXPECTED.format(found=found, expected=expected)
        )

    def at_word(self, *words: str) -> bool:
        return any(self.tok.is_word(w) for w in words)

    def accept_word(self, *words: str) -> bool:
        if self.at_word(*words):
            self.advance()
            return True
        return False

    def expect_word(self, word: str) -> None:
        if not self.accept_word(word):
            raise self.error(word)

    def at_punct(self, text: str) -> bool:
        return self.tok.is_punct(text)

    def accept_punct(self, text: str) -> bool:
        if self.at_punct(text):
            self.advance()
            return True
        return False

    def expect_punct(self, punct: ecs.EgPunct) -> None:
        if not self.accept_punct(punct):
            raise self.error(repr(punct.value))

    def name(self) -> str:
        """Any identifier, keywords included: labels, keys, aliases."""
        tok = self.tok
        if tok.kind in (ecs.EgTok.IDENT, ecs.EgTok.QUOTED_IDENT):
            self.advance()
            return tok.text
        raise self.error(ecs.EG_EXPECT_NAME)

    def variable(self) -> str:
        tok = self.tok
        if tok.kind == ecs.EgTok.QUOTED_IDENT or (
            tok.kind == ecs.EgTok.IDENT
            and tok.text.upper() not in ecs.EG_EXPRESSION_STOP_WORDS
        ):
            self.advance()
            return tok.text
        raise self.error(ecs.EG_EXPECT_NAME)

    # ------------------------------------------------------------ statements

    def statement(self) -> Statement:
        if self.accept_word(ecs.EgKw.EXPLAIN) or self.accept_word(ecs.EgKw.PROFILE):
            result: Statement = a.Explain(self.query())
        elif self._at_schema_command():
            result = self.schema_command()
        else:
            result = self.query()
        self.accept_punct(ecs.EgPunct.SEMICOLON)
        if self.tok.kind != ecs.EgTok.EOF:
            raise self.error(ecs.EG_EXPECT_CLAUSE)
        return result

    def query(self) -> a.Query:
        parts = [self.single_query()]
        union_all: list[bool] = []
        while self.accept_word(ecs.EgKw.UNION):
            union_all.append(self.accept_word(ecs.EgKw.ALL))
            parts.append(self.single_query())
        return a.Query(tuple(parts), tuple(union_all))

    def single_query(self) -> a.SingleQuery:
        clauses: list[a.Clause] = []
        while True:
            clause = self.clause()
            if clause is None:
                break
            clauses.append(clause)
        if not clauses:
            raise self.error(ecs.EG_EXPECT_CLAUSE)
        return a.SingleQuery(tuple(clauses))

    def clause(self) -> a.Clause | None:
        if self.at_word(ecs.EgKw.OPTIONAL):
            self.advance()
            self.expect_word(ecs.EgKw.MATCH)
            return self.match(optional=True)
        if self.accept_word(ecs.EgKw.MATCH):
            return self.match(optional=False)
        if self.accept_word(ecs.EgKw.UNWIND):
            expr = self.expression()
            self.expect_word(ecs.EgKw.AS)
            return a.Unwind(expr, self.variable())
        if self.accept_word(ecs.EgKw.WITH):
            return self.projection(is_return=False)
        if self.accept_word(ecs.EgKw.RETURN):
            return self.projection(is_return=True)
        if self.accept_word(ecs.EgKw.CREATE):
            return a.Create(self.pattern_list())
        if self.accept_word(ecs.EgKw.MERGE):
            return self.merge()
        if self.accept_word(ecs.EgKw.SET):
            return a.SetClause(self.set_items())
        if self.accept_word(ecs.EgKw.REMOVE):
            return self.remove()
        if self.at_word(ecs.EgKw.DETACH):
            self.advance()
            self.expect_word(ecs.EgKw.DELETE)
            return a.Delete(True, self.expression_list())
        if self.accept_word(ecs.EgKw.DELETE):
            return a.Delete(False, self.expression_list())
        if self.accept_word(ecs.EgKw.FOREACH):
            return self.foreach()
        if self.accept_word(ecs.EgKw.CALL):
            return self.call()
        return None

    def match(self, optional: bool) -> a.Match:
        patterns = self.pattern_list()
        where = self.expression() if self.accept_word(ecs.EgKw.WHERE) else None
        return a.Match(optional, patterns, where)

    def projection(self, is_return: bool) -> a.Projection:
        distinct = self.accept_word(ecs.EgKw.DISTINCT)
        star = False
        items: list[a.ProjectionItem] = []
        if self.accept_punct(ecs.EgPunct.STAR):
            star = True
            if not self.accept_punct(ecs.EgPunct.COMMA):
                return self._projection_tail(is_return, distinct, star, items)
        while True:
            start = self.tok.pos
            expr = self.expression()
            if self.accept_word(ecs.EgKw.AS):
                alias = self.name()
            else:
                alias = self._source_text(start)
            items.append(a.ProjectionItem(expr, alias))
            if not self.accept_punct(ecs.EgPunct.COMMA):
                break
        return self._projection_tail(is_return, distinct, star, items)

    def _projection_tail(
        self,
        is_return: bool,
        distinct: bool,
        star: bool,
        items: list[a.ProjectionItem],
    ) -> a.Projection:
        order: list[a.SortItem] = []
        if self.accept_word(ecs.EgKw.ORDER):
            self.expect_word(ecs.EgKw.BY)
            while True:
                expr = self.expression()
                desc = False
                if self.accept_word(ecs.EgKw.DESC, ecs.EgKw.DESCENDING):
                    desc = True
                else:
                    self.accept_word(ecs.EgKw.ASC, ecs.EgKw.ASCENDING)
                order.append(a.SortItem(expr, desc))
                if not self.accept_punct(ecs.EgPunct.COMMA):
                    break
        skip = self.expression() if self.accept_word(ecs.EgKw.SKIP) else None
        limit = self.expression() if self.accept_word(ecs.EgKw.LIMIT) else None
        where = None
        if not is_return and self.accept_word(ecs.EgKw.WHERE):
            where = self.expression()
        return a.Projection(
            is_return, distinct, star, tuple(items), tuple(order), skip, limit, where
        )

    def _source_text(self, start: int) -> str:
        # An unaliased projection is named by its own text, as Memgraph does
        # (`RETURN labels(n)[0]` yields a column `labels(n)[0]`).
        end_tok = self.tokens[self.i - 1]
        end = end_tok.pos + len(_token_source(end_tok))
        return self.source[start:end]

    def merge(self) -> a.Merge:
        pattern = self.path_pattern()
        on_create: list[a.SetItem] = []
        on_match: list[a.SetItem] = []
        while self.at_word(ecs.EgKw.ON):
            self.advance()
            if self.accept_word(ecs.EgKw.CREATE):
                self.expect_word(ecs.EgKw.SET)
                on_create.extend(self.set_items())
            else:
                self.expect_word(ecs.EgKw.MATCH)
                self.expect_word(ecs.EgKw.SET)
                on_match.extend(self.set_items())
        return a.Merge(pattern, tuple(on_create), tuple(on_match))

    def set_items(self) -> tuple[a.SetItem, ...]:
        items: list[a.SetItem] = []
        while True:
            var = self.variable()
            if self.accept_punct(ecs.EgPunct.DOT):
                key = self.name()
                self.expect_punct(ecs.EgPunct.EQ)
                items.append(a.SetItem(ecs.EgSetKind.PROP, var, key, self.expression()))
            elif self.accept_punct(ecs.EgPunct.PLUS_EQ):
                items.append(
                    a.SetItem(ecs.EgSetKind.MERGE, var, None, self.expression())
                )
            elif self.accept_punct(ecs.EgPunct.EQ):
                items.append(
                    a.SetItem(ecs.EgSetKind.REPLACE, var, None, self.expression())
                )
            elif self.accept_punct(ecs.EgPunct.COLON):
                items.append(a.SetItem(ecs.EgSetKind.LABEL, var, self.name(), None))
            else:
                raise self.error(ecs.EG_EXPECT_SET_ITEM)
            if not self.accept_punct(ecs.EgPunct.COMMA):
                return tuple(items)

    def remove(self) -> a.Remove:
        items: list[a.RemoveItem] = []
        while True:
            var = self.variable()
            if self.accept_punct(ecs.EgPunct.DOT):
                items.append(a.RemoveItem(ecs.EgRemoveKind.PROP, var, self.name()))
            else:
                self.expect_punct(ecs.EgPunct.COLON)
                items.append(a.RemoveItem(ecs.EgRemoveKind.LABEL, var, self.name()))
            if not self.accept_punct(ecs.EgPunct.COMMA):
                return a.Remove(tuple(items))

    def foreach(self) -> a.Foreach:
        self.expect_punct(ecs.EgPunct.LPAREN)
        var = self.variable()
        self.expect_word(ecs.EgKw.IN)
        source = self.expression()
        self.expect_punct(ecs.EgPunct.PIPE)
        clauses: list[a.Clause] = []
        while not self.at_punct(ecs.EgPunct.RPAREN):
            clause = self.clause()
            if clause is None:
                raise self.error(ecs.EG_EXPECT_CLAUSE)
            clauses.append(clause)
        self.expect_punct(ecs.EgPunct.RPAREN)
        return a.Foreach(var, source, tuple(clauses))

    def call(self) -> a.Call:
        parts = [self.name()]
        while self.accept_punct(ecs.EgPunct.DOT):
            parts.append(self.name())
        args: list[a.Expr] = []
        self.expect_punct(ecs.EgPunct.LPAREN)
        if not self.at_punct(ecs.EgPunct.RPAREN):
            args = list(self.expression_list())
        self.expect_punct(ecs.EgPunct.RPAREN)
        yields: list[a.YieldItem] = []
        where = None
        if self.accept_word(ecs.EgKw.YIELD):
            while True:
                column = self.name()
                alias = self.name() if self.accept_word(ecs.EgKw.AS) else column
                yields.append(a.YieldItem(column, alias))
                if not self.accept_punct(ecs.EgPunct.COMMA):
                    break
            if self.accept_word(ecs.EgKw.WHERE):
                where = self.expression()
        return a.Call(ecs.EgPunct.DOT.join(parts), tuple(args), tuple(yields), where)

    def expression_list(self) -> tuple[a.Expr, ...]:
        exprs = [self.expression()]
        while self.accept_punct(ecs.EgPunct.COMMA):
            exprs.append(self.expression())
        return tuple(exprs)

    # ----------------------------------------------------------------- schema

    def _at_schema_command(self) -> bool:
        tok, nxt = self.tok, self.peek()
        if tok.is_word(ecs.EgKw.SHOW):
            return True
        if tok.is_word(ecs.EgKw.CREATE) or tok.is_word(ecs.EgKw.DROP):
            return nxt.is_word(ecs.EgKw.INDEX) or nxt.is_word(ecs.EgKw.CONSTRAINT)
        return False

    def schema_command(self) -> a.SchemaCommand:
        if self.accept_word(ecs.EgKw.SHOW):
            return self._show_command()
        create = self.accept_word(ecs.EgKw.CREATE)
        if not create:
            self.expect_word(ecs.EgKw.DROP)
        is_index = self.accept_word(ecs.EgKw.INDEX)
        if not is_index:
            self.expect_word(ecs.EgKw.CONSTRAINT)
        label, props = self._schema_target()
        if label is None and create:
            raise self.error(ecs.EG_EXPECT_SCHEMA)
        kind = _SCHEMA_KINDS[create, is_index]
        return a.SchemaCommand(kind, label, tuple(props))

    def _show_command(self) -> a.SchemaCommand:
        words = []
        while self.tok.kind == ecs.EgTok.IDENT:
            words.append(self.advance().text.upper())
        if ecs.EgKw.CONSTRAINT in words or ecs.EgKw.CONSTRAINTS in words:
            return a.SchemaCommand(ecs.EgSchemaKind.SHOW_CONSTRAINTS)
        if ecs.EgKw.STORAGE in words:
            return a.SchemaCommand(ecs.EgSchemaKind.SHOW_STORAGE)
        return a.SchemaCommand(ecs.EgSchemaKind.SHOW_INDEXES)

    def _schema_target(self) -> tuple[str | None, list[str]]:
        # Memgraph (`ON :L(p)`, `ON (n:L) ASSERT n.p`) and Neo4j (`name IF NOT
        # EXISTS FOR (n:L) ON (n.p)` / `REQUIRE n.p`) spellings both reduce to
        # a label and its properties; index names are the store's own.
        label: str | None = None
        props: list[str] = []
        while self.tok.kind != ecs.EgTok.EOF and not self.at_punct(
            ecs.EgPunct.SEMICOLON
        ):
            if self.accept_punct(ecs.EgPunct.COLON):
                label = self.name()
                props.extend(self._label_props())
            elif self.accept_punct(ecs.EgPunct.LPAREN):
                self.variable()
                if self.accept_punct(ecs.EgPunct.COLON):
                    label = self.name()
                else:
                    props.extend(self._dotted_props())
                self.expect_punct(ecs.EgPunct.RPAREN)
            elif label is not None and self.accept_punct(ecs.EgPunct.DOT):
                props.append(self.name())
            else:
                self.advance()
        return label, props

    def _label_props(self) -> list[str]:
        """The `(p, q)` that may follow `:L`."""
        if not self.accept_punct(ecs.EgPunct.LPAREN):
            return []
        props = self._name_list()
        self.expect_punct(ecs.EgPunct.RPAREN)
        return props

    def _name_list(self) -> list[str]:
        names = [self.name()]
        while self.accept_punct(ecs.EgPunct.COMMA):
            names.append(self.name())
        return names

    def _dotted_props(self) -> list[str]:
        """`.p` after a consumed variable, then any `, n.q` that follow."""
        self.expect_punct(ecs.EgPunct.DOT)
        props = [self.name()]
        while self.accept_punct(ecs.EgPunct.COMMA):
            self.variable()
            self.expect_punct(ecs.EgPunct.DOT)
            props.append(self.name())
        return props

    # --------------------------------------------------------------- patterns

    def pattern_list(self) -> tuple[a.PathPattern, ...]:
        patterns = [self.path_pattern()]
        while self.accept_punct(ecs.EgPunct.COMMA):
            patterns.append(self.path_pattern())
        return tuple(patterns)

    def path_pattern(self) -> a.PathPattern:
        var = None
        if self.tok.kind in (
            ecs.EgTok.IDENT,
            ecs.EgTok.QUOTED_IDENT,
        ) and self.peek().is_punct(ecs.EgPunct.EQ):
            var = self.variable()
            self.advance()
        if self.at_word(ecs.EgKw.SHORTESTPATH) or self.at_word(
            ecs.EgKw.ALLSHORTESTPATHS
        ):
            raise CypherSyntaxError(self.tok.pos, self.tok.text)
        nodes = [self.node_pattern()]
        rels: list[a.RelPattern] = []
        while self.at_punct(ecs.EgPunct.MINUS) or self.at_punct(ecs.EgPunct.LT):
            rels.append(self.rel_pattern())
            nodes.append(self.node_pattern())
        return a.PathPattern(var, tuple(nodes), tuple(rels))

    def node_pattern(self) -> a.NodePattern:
        if not self.accept_punct(ecs.EgPunct.LPAREN):
            raise self.error(ecs.EG_EXPECT_PATTERN)
        var = None
        if self.tok.kind in (
            ecs.EgTok.IDENT,
            ecs.EgTok.QUOTED_IDENT,
        ) and not self.at_punct(ecs.EgPunct.COLON):
            var = self.variable()
        labels = self._labels()
        props = self._pattern_props()
        self.expect_punct(ecs.EgPunct.RPAREN)
        return a.NodePattern(var, labels, props)

    def _labels(self) -> tuple[str, ...]:
        labels: list[str] = []
        if self.accept_punct(ecs.EgPunct.COLON):
            labels.append(self.name())
            while self.accept_punct(ecs.EgPunct.PIPE) or self.accept_punct(
                ecs.EgPunct.COLON
            ):
                labels.append(self.name())
        return tuple(labels)

    def _pattern_props(self) -> a.Expr | None:
        if self.at_punct(ecs.EgPunct.LBRACE):
            return self.map_literal()
        if self.tok.kind == ecs.EgTok.PARAM:
            return a.Param(str(self.advance().value))
        return None

    def rel_pattern(self) -> a.RelPattern:
        left_arrow = self.accept_punct(ecs.EgPunct.LT)
        self.expect_punct(ecs.EgPunct.MINUS)
        var = None
        types: tuple[str, ...] = ()
        props = None
        var_length: tuple[int, int | None] | None = None
        if self.accept_punct(ecs.EgPunct.LBRACKET):
            if self.tok.kind in (ecs.EgTok.IDENT, ecs.EgTok.QUOTED_IDENT):
                var = self.variable()
            if self.accept_punct(ecs.EgPunct.COLON):
                names = [self.name()]
                while self.accept_punct(ecs.EgPunct.PIPE):
                    self.accept_punct(ecs.EgPunct.COLON)
                    names.append(self.name())
                types = tuple(names)
            if self.accept_punct(ecs.EgPunct.STAR):
                var_length = self._var_length()
            props = self._pattern_props()
            self.expect_punct(ecs.EgPunct.RBRACKET)
        self.expect_punct(ecs.EgPunct.MINUS)
        right_arrow = self.accept_punct(ecs.EgPunct.GT)
        if left_arrow and not right_arrow:
            direction = ecs.EgDirection.IN
        elif right_arrow and not left_arrow:
            direction = ecs.EgDirection.OUT
        else:
            direction = ecs.EgDirection.BOTH
        return a.RelPattern(var, types, direction, props, var_length)

    def _int_token(self) -> int:
        token = self.advance()
        if not isinstance(token.value, int):
            raise self.error(ecs.EG_EXPECT_INT)
        return token.value

    def _var_length(self) -> tuple[int, int | None]:
        low: int | None = None
        high: int | None = None
        if self.tok.kind == ecs.EgTok.INT:
            low = self._int_token()
        if self.accept_punct(ecs.EgPunct.RANGE):
            if self.tok.kind == ecs.EgTok.INT:
                high = self._int_token()
            return (1 if low is None else low, high)
        if low is not None:
            return (low, low)
        return (1, None)

    # ------------------------------------------------------------ expressions

    def expression(self) -> a.Expr:
        return self._or()

    def _or(self) -> a.Expr:
        left = self._xor()
        while self.accept_word(ecs.EgKw.OR):
            left = a.BinOp(ecs.EgBinary.OR, left, self._xor())
        return left

    def _xor(self) -> a.Expr:
        left = self._and()
        while self.accept_word(ecs.EgKw.XOR):
            left = a.BinOp(ecs.EgBinary.XOR, left, self._and())
        return left

    def _and(self) -> a.Expr:
        left = self._not()
        while self.accept_word(ecs.EgKw.AND):
            left = a.BinOp(ecs.EgBinary.AND, left, self._not())
        return left

    def _not(self) -> a.Expr:
        if self.accept_word(ecs.EgKw.NOT):
            return a.UnaryOp(ecs.EgUnary.NOT, self._not())
        return self._comparison()

    def _comparison(self) -> a.Expr:
        left = self._additive()
        while True:
            tok = self.tok
            if tok.kind == ecs.EgTok.PUNCT and tok.text in _COMPARISON_OPS:
                self.advance()
                left = a.BinOp(_COMPARISON_OPS[tok.text], left, self._additive())
            elif tok.is_word(ecs.EgKw.IN):
                self.advance()
                left = a.BinOp(ecs.EgBinary.IN, left, self._additive())
            elif tok.is_word(ecs.EgKw.STARTS):
                self.advance()
                self.expect_word(ecs.EgKw.WITH)
                left = a.BinOp(ecs.EgBinary.STARTS_WITH, left, self._additive())
            elif tok.is_word(ecs.EgKw.ENDS):
                self.advance()
                self.expect_word(ecs.EgKw.WITH)
                left = a.BinOp(ecs.EgBinary.ENDS_WITH, left, self._additive())
            elif tok.is_word(ecs.EgKw.CONTAINS):
                self.advance()
                left = a.BinOp(ecs.EgBinary.CONTAINS, left, self._additive())
            elif tok.is_word(ecs.EgKw.IS):
                self.advance()
                negated = self.accept_word(ecs.EgKw.NOT)
                self.expect_word(ecs.EgKw.NULL)
                left = a.IsNull(left, negated)
            else:
                return left

    def _additive(self) -> a.Expr:
        left = self._multiplicative()
        while self.tok.kind == ecs.EgTok.PUNCT and self.tok.text in _ADDITIVE_OPS:
            op = _ADDITIVE_OPS[self.advance().text]
            left = a.BinOp(op, left, self._multiplicative())
        return left

    def _multiplicative(self) -> a.Expr:
        left = self._power()
        while self.tok.kind == ecs.EgTok.PUNCT and self.tok.text in _MULTIPLICATIVE_OPS:
            op = _MULTIPLICATIVE_OPS[self.advance().text]
            left = a.BinOp(op, left, self._power())
        return left

    def _power(self) -> a.Expr:
        left = self._unary()
        while self.accept_punct(ecs.EgPunct.CARET):
            left = a.BinOp(ecs.EgBinary.POW, left, self._unary())
        return left

    def _unary(self) -> a.Expr:
        if self.accept_punct(ecs.EgPunct.MINUS):
            return a.UnaryOp(ecs.EgUnary.NEG, self._unary())
        if self.accept_punct(ecs.EgPunct.PLUS):
            return self._unary()
        return self._postfix()

    def _postfix(self) -> a.Expr:
        expr = self._atom()
        while True:
            if self.at_punct(ecs.EgPunct.DOT) and not self.peek().is_punct(
                ecs.EgPunct.DOT
            ):
                self.advance()
                expr = a.Prop(expr, self.name())
            elif self.accept_punct(ecs.EgPunct.LBRACKET):
                expr = self._subscript(expr)
            elif self.at_punct(ecs.EgPunct.COLON) and isinstance(expr, a.Var):
                labels = self._labels()
                expr = a.LabelTest(expr, labels)
            else:
                return expr

    def _subscript(self, expr: a.Expr) -> a.Expr:
        """`[i]`, `[i..j]`, `[i..]` or `[..j]` after `expr`, its `[` consumed."""
        if self.accept_punct(ecs.EgPunct.RANGE):
            return a.Slice(expr, None, self._slice_end())
        index = self.expression()
        if self.accept_punct(ecs.EgPunct.RANGE):
            return a.Slice(expr, index, self._slice_end())
        self.expect_punct(ecs.EgPunct.RBRACKET)
        return a.Index(expr, index)

    def _slice_end(self) -> a.Expr | None:
        end = None if self.at_punct(ecs.EgPunct.RBRACKET) else self.expression()
        self.expect_punct(ecs.EgPunct.RBRACKET)
        return end

    def _atom(self) -> a.Expr:
        tok = self.tok
        if tok.kind in (ecs.EgTok.INT, ecs.EgTok.FLOAT, ecs.EgTok.STRING):
            self.advance()
            return a.Literal(tok.value)
        if tok.kind == ecs.EgTok.PARAM:
            self.advance()
            return a.Param(str(tok.value))
        if tok.is_punct(ecs.EgPunct.LPAREN):
            self.advance()
            expr = self.expression()
            self.expect_punct(ecs.EgPunct.RPAREN)
            return expr
        if tok.is_punct(ecs.EgPunct.LBRACKET):
            return self._list()
        if tok.is_punct(ecs.EgPunct.LBRACE):
            return self.map_literal()
        if tok.kind == ecs.EgTok.QUOTED_IDENT:
            self.advance()
            return a.Var(tok.text)
        if tok.kind != ecs.EgTok.IDENT:
            raise self.error(ecs.EG_EXPECT_EXPRESSION)
        upper = tok.text.upper()
        if upper in _WORD_LITERALS:
            self.advance()
            return a.Literal(_WORD_LITERALS[upper])
        if upper == ecs.EgKw.CASE:
            return self._case()
        if (
            upper in _QUANTIFIERS
            and self.peek().is_punct(ecs.EgPunct.LPAREN)
            and self.peek(3).is_word(ecs.EgKw.IN)
        ):
            return self._quantifier(_QUANTIFIERS[upper])
        if self._at_call():
            return self._function()
        if upper in ecs.EG_EXPRESSION_STOP_WORDS:
            raise self.error(ecs.EG_EXPECT_EXPRESSION)
        self.advance()
        return a.Var(tok.text)

    def _at_call(self) -> bool:
        return self.peek().is_punct(ecs.EgPunct.LPAREN) or (
            self.peek().is_punct(ecs.EgPunct.DOT) and self._is_namespaced_call()
        )

    def _is_namespaced_call(self) -> bool:
        # `toLower(x)` vs `n.prop`: a call is name(.name)* followed by `(`.
        j = self.i
        while (
            self.tokens[j].kind == ecs.EgTok.IDENT
            and self.tokens[j + 1].is_punct(ecs.EgPunct.DOT)
            and self.tokens[j + 2].kind == ecs.EgTok.IDENT
        ):
            j += 2
        return self.tokens[j].kind == ecs.EgTok.IDENT and self.tokens[j + 1].is_punct(
            ecs.EgPunct.LPAREN
        )

    def _function(self) -> a.Expr:
        parts = [self.advance().text]
        while self.accept_punct(ecs.EgPunct.DOT):
            parts.append(self.name())
        name = ecs.EgPunct.DOT.join(parts).lower()
        self.expect_punct(ecs.EgPunct.LPAREN)
        if self.accept_punct(ecs.EgPunct.STAR):
            self.expect_punct(ecs.EgPunct.RPAREN)
            return a.FuncCall(name, (), star=True)
        distinct = self.accept_word(ecs.EgKw.DISTINCT)
        args: tuple[a.Expr, ...] = ()
        if not self.at_punct(ecs.EgPunct.RPAREN):
            args = self.expression_list()
        self.expect_punct(ecs.EgPunct.RPAREN)
        return a.FuncCall(name, args, distinct=distinct)

    def _quantifier(self, kind: ecs.EgQuantifier) -> a.Expr:
        self.advance()
        self.expect_punct(ecs.EgPunct.LPAREN)
        var = self.variable()
        self.expect_word(ecs.EgKw.IN)
        source = self.expression()
        where: a.Expr = a.Literal(True)
        if self.accept_word(ecs.EgKw.WHERE):
            where = self.expression()
        self.expect_punct(ecs.EgPunct.RPAREN)
        return a.Quantifier(kind, var, source, where)

    def _list(self) -> a.Expr:
        self.expect_punct(ecs.EgPunct.LBRACKET)
        if self.tok.kind in (
            ecs.EgTok.IDENT,
            ecs.EgTok.QUOTED_IDENT,
        ) and self.peek().is_word(ecs.EgKw.IN):
            var = self.variable()
            self.expect_word(ecs.EgKw.IN)
            source = self.expression()
            where = self.expression() if self.accept_word(ecs.EgKw.WHERE) else None
            projection = (
                self.expression() if self.accept_punct(ecs.EgPunct.PIPE) else None
            )
            self.expect_punct(ecs.EgPunct.RBRACKET)
            return a.ListComp(var, source, where, projection)
        items: list[a.Expr] = []
        if not self.at_punct(ecs.EgPunct.RBRACKET):
            items = list(self.expression_list())
        self.expect_punct(ecs.EgPunct.RBRACKET)
        return a.ListLit(tuple(items))

    def map_literal(self) -> a.MapLit:
        self.expect_punct(ecs.EgPunct.LBRACE)
        items: list[tuple[str, a.Expr]] = []
        if not self.at_punct(ecs.EgPunct.RBRACE):
            while True:
                key = self.name()
                self.expect_punct(ecs.EgPunct.COLON)
                items.append((key, self.expression()))
                if not self.accept_punct(ecs.EgPunct.COMMA):
                    break
        self.expect_punct(ecs.EgPunct.RBRACE)
        return a.MapLit(tuple(items))

    def _case(self) -> a.Expr:
        self.expect_word(ecs.EgKw.CASE)
        subject = None if self.at_word(ecs.EgKw.WHEN) else self.expression()
        whens: list[tuple[a.Expr, a.Expr]] = []
        while self.accept_word(ecs.EgKw.WHEN):
            cond = self.expression()
            self.expect_word(ecs.EgKw.THEN)
            whens.append((cond, self.expression()))
        default = self.expression() if self.accept_word(ecs.EgKw.ELSE) else None
        self.expect_word(ecs.EgKw.END)
        return a.Case(subject, tuple(whens), default)


def _token_source(tok: Token) -> str:
    if tok.kind == ecs.EgTok.QUOTED_IDENT:
        doubled = ecs.EG_BACKTICK * 2
        return (
            ecs.EG_BACKTICK
            + tok.text.replace(ecs.EG_BACKTICK, doubled)
            + ecs.EG_BACKTICK
        )
    return tok.text
