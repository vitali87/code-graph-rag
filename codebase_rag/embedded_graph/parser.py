"""Recursive-descent parser for the embedded engine's openCypher subset.

Covers every query cgr itself issues plus the shapes its Cypher prompt asks a
model to write. Anything outside the subset is a CypherSyntaxError, which the
query tool already turns into a repair attempt.
"""

from __future__ import annotations

from functools import lru_cache

from ..constants.embedded_graph import (
    EG_COMPILE_CACHE_SIZE,
    EG_ERR_UNEXPECTED,
    EG_EXPRESSION_STOP_WORDS,
    EgBinary,
    EgDirection,
    EgQuantifier,
    EgRemoveKind,
    EgSchemaKind,
    EgSetKind,
    EgTok,
    EgUnary,
)
from . import cypher_ast as a
from .lexer import CypherSyntaxError, Token, tokenize

_COMPARISON_OPS: dict[str, EgBinary] = {
    "=": EgBinary.EQ,
    "<>": EgBinary.NE,
    "!=": EgBinary.NE,
    "<": EgBinary.LT,
    ">": EgBinary.GT,
    "<=": EgBinary.LE,
    ">=": EgBinary.GE,
    "=~": EgBinary.REGEX,
}
_ADDITIVE_OPS: dict[str, EgBinary] = {"+": EgBinary.ADD, "-": EgBinary.SUB}
_MULTIPLICATIVE_OPS: dict[str, EgBinary] = {
    "*": EgBinary.MUL,
    "/": EgBinary.DIV,
    "%": EgBinary.MOD,
}
_QUANTIFIERS: dict[str, EgQuantifier] = {
    "ANY": EgQuantifier.ANY,
    "ALL": EgQuantifier.ALL,
    "NONE": EgQuantifier.NONE,
    "SINGLE": EgQuantifier.SINGLE,
}
_EXPECT_EXPRESSION = "an expression"
_EXPECT_NAME = "a name"
_EXPECT_INT = "an integer"
_EXPECT_CLAUSE = "a clause"
_EXPECT_PATTERN = "a pattern"
_EXPECT_SET_ITEM = "a SET item"
_EXPECT_SCHEMA = "an index or constraint statement"


@lru_cache(maxsize=EG_COMPILE_CACHE_SIZE)
def parse(text: str) -> a.Query | a.Explain | a.SchemaCommand:
    return _Parser(tokenize(text), text).statement()


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
        if tok.kind != EgTok.EOF:
            self.i += 1
        return tok

    def error(self, expected: str) -> CypherSyntaxError:
        tok = self.tok
        found = tok.text or "end of query"
        return CypherSyntaxError(
            tok.pos, EG_ERR_UNEXPECTED.format(found=found, expected=expected)
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

    def expect_punct(self, text: str) -> None:
        if not self.accept_punct(text):
            raise self.error(repr(text))

    def name(self) -> str:
        """Any identifier, keywords included: labels, keys, aliases."""
        tok = self.tok
        if tok.kind in (EgTok.IDENT, EgTok.QUOTED_IDENT):
            self.advance()
            return tok.text
        raise self.error(_EXPECT_NAME)

    def variable(self) -> str:
        tok = self.tok
        if tok.kind == EgTok.QUOTED_IDENT or (
            tok.kind == EgTok.IDENT and tok.text.upper() not in EG_EXPRESSION_STOP_WORDS
        ):
            self.advance()
            return tok.text
        raise self.error(_EXPECT_NAME)

    # ------------------------------------------------------------ statements

    def statement(self) -> a.Query | a.Explain | a.SchemaCommand:
        if self.accept_word("EXPLAIN") or self.accept_word("PROFILE"):
            result: a.Query | a.Explain | a.SchemaCommand = a.Explain(self.query())
        elif self._at_schema_command():
            result = self.schema_command()
        else:
            result = self.query()
        self.accept_punct(";")
        if self.tok.kind != EgTok.EOF:
            raise self.error(_EXPECT_CLAUSE)
        return result

    def query(self) -> a.Query:
        parts = [self.single_query()]
        union_all: list[bool] = []
        while self.accept_word("UNION"):
            union_all.append(self.accept_word("ALL"))
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
            raise self.error(_EXPECT_CLAUSE)
        return a.SingleQuery(tuple(clauses))

    def clause(self) -> a.Clause | None:
        if self.at_word("OPTIONAL"):
            self.advance()
            self.expect_word("MATCH")
            return self.match(optional=True)
        if self.accept_word("MATCH"):
            return self.match(optional=False)
        if self.accept_word("UNWIND"):
            expr = self.expression()
            self.expect_word("AS")
            return a.Unwind(expr, self.variable())
        if self.accept_word("WITH"):
            return self.projection(is_return=False)
        if self.accept_word("RETURN"):
            return self.projection(is_return=True)
        if self.accept_word("CREATE"):
            return a.Create(self.pattern_list())
        if self.accept_word("MERGE"):
            return self.merge()
        if self.accept_word("SET"):
            return a.SetClause(self.set_items())
        if self.accept_word("REMOVE"):
            return self.remove()
        if self.at_word("DETACH"):
            self.advance()
            self.expect_word("DELETE")
            return a.Delete(True, self.expression_list())
        if self.accept_word("DELETE"):
            return a.Delete(False, self.expression_list())
        if self.accept_word("FOREACH"):
            return self.foreach()
        if self.accept_word("CALL"):
            return self.call()
        return None

    def match(self, optional: bool) -> a.Match:
        patterns = self.pattern_list()
        where = self.expression() if self.accept_word("WHERE") else None
        return a.Match(optional, patterns, where)

    def projection(self, is_return: bool) -> a.Projection:
        distinct = self.accept_word("DISTINCT")
        star = False
        items: list[a.ProjectionItem] = []
        if self.accept_punct("*"):
            star = True
            if not self.accept_punct(","):
                return self._projection_tail(is_return, distinct, star, items)
        while True:
            start = self.tok.pos
            expr = self.expression()
            if self.accept_word("AS"):
                alias = self.name()
            else:
                alias = self._source_text(start)
            items.append(a.ProjectionItem(expr, alias))
            if not self.accept_punct(","):
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
        if self.accept_word("ORDER"):
            self.expect_word("BY")
            while True:
                expr = self.expression()
                desc = False
                if self.accept_word("DESC", "DESCENDING"):
                    desc = True
                else:
                    self.accept_word("ASC", "ASCENDING")
                order.append(a.SortItem(expr, desc))
                if not self.accept_punct(","):
                    break
        skip = self.expression() if self.accept_word("SKIP") else None
        limit = self.expression() if self.accept_word("LIMIT") else None
        where = None
        if not is_return and self.accept_word("WHERE"):
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
        while self.at_word("ON"):
            self.advance()
            if self.accept_word("CREATE"):
                self.expect_word("SET")
                on_create.extend(self.set_items())
            else:
                self.expect_word("MATCH")
                self.expect_word("SET")
                on_match.extend(self.set_items())
        return a.Merge(pattern, tuple(on_create), tuple(on_match))

    def set_items(self) -> tuple[a.SetItem, ...]:
        items: list[a.SetItem] = []
        while True:
            var = self.variable()
            if self.accept_punct("."):
                key = self.name()
                self.expect_punct("=")
                items.append(a.SetItem(EgSetKind.PROP, var, key, self.expression()))
            elif self.accept_punct("+="):
                items.append(a.SetItem(EgSetKind.MERGE, var, None, self.expression()))
            elif self.accept_punct("="):
                items.append(a.SetItem(EgSetKind.REPLACE, var, None, self.expression()))
            elif self.accept_punct(":"):
                items.append(a.SetItem(EgSetKind.LABEL, var, self.name(), None))
            else:
                raise self.error(_EXPECT_SET_ITEM)
            if not self.accept_punct(","):
                return tuple(items)

    def remove(self) -> a.Remove:
        items: list[a.RemoveItem] = []
        while True:
            var = self.variable()
            if self.accept_punct("."):
                items.append(a.RemoveItem(EgRemoveKind.PROP, var, self.name()))
            else:
                self.expect_punct(":")
                items.append(a.RemoveItem(EgRemoveKind.LABEL, var, self.name()))
            if not self.accept_punct(","):
                return a.Remove(tuple(items))

    def foreach(self) -> a.Foreach:
        self.expect_punct("(")
        var = self.variable()
        self.expect_word("IN")
        source = self.expression()
        self.expect_punct("|")
        clauses: list[a.Clause] = []
        while not self.at_punct(")"):
            clause = self.clause()
            if clause is None:
                raise self.error(_EXPECT_CLAUSE)
            clauses.append(clause)
        self.expect_punct(")")
        return a.Foreach(var, source, tuple(clauses))

    def call(self) -> a.Call:
        parts = [self.name()]
        while self.accept_punct("."):
            parts.append(self.name())
        args: list[a.Expr] = []
        self.expect_punct("(")
        if not self.at_punct(")"):
            args = list(self.expression_list())
        self.expect_punct(")")
        yields: list[a.YieldItem] = []
        where = None
        if self.accept_word("YIELD"):
            while True:
                column = self.name()
                alias = self.name() if self.accept_word("AS") else column
                yields.append(a.YieldItem(column, alias))
                if not self.accept_punct(","):
                    break
            if self.accept_word("WHERE"):
                where = self.expression()
        return a.Call(".".join(parts), tuple(args), tuple(yields), where)

    def expression_list(self) -> tuple[a.Expr, ...]:
        exprs = [self.expression()]
        while self.accept_punct(","):
            exprs.append(self.expression())
        return tuple(exprs)

    # ----------------------------------------------------------------- schema

    def _at_schema_command(self) -> bool:
        tok, nxt = self.tok, self.peek()
        if tok.is_word("SHOW"):
            return True
        if tok.is_word("CREATE") or tok.is_word("DROP"):
            return nxt.is_word("INDEX") or nxt.is_word("CONSTRAINT")
        return False

    def schema_command(self) -> a.SchemaCommand:
        if self.accept_word("SHOW"):
            words = []
            while self.tok.kind == EgTok.IDENT:
                words.append(self.advance().text.upper())
            if "CONSTRAINT" in words or "CONSTRAINTS" in words:
                return a.SchemaCommand(EgSchemaKind.SHOW_CONSTRAINTS)
            if "STORAGE" in words:
                return a.SchemaCommand(EgSchemaKind.SHOW_STORAGE)
            return a.SchemaCommand(EgSchemaKind.SHOW_INDEXES)
        create = self.accept_word("CREATE")
        if not create:
            self.expect_word("DROP")
        is_index = self.accept_word("INDEX")
        if not is_index:
            self.expect_word("CONSTRAINT")
        # Memgraph (`ON :L(p)`, `ON (n:L) ASSERT n.p`) and Neo4j (`name IF NOT
        # EXISTS FOR (n:L) ON (n.p)` / `REQUIRE n.p`) spellings both reduce to
        # a label and its properties; index names are the store's own.
        label: str | None = None
        props: list[str] = []
        while self.tok.kind != EgTok.EOF and not self.at_punct(";"):
            if self.accept_punct(":"):
                label = self.name()
                if self.accept_punct("("):
                    props.extend(self._name_list())
                    self.expect_punct(")")
                continue
            if self.accept_punct("("):
                self.variable()
                if self.accept_punct(":"):
                    label = self.name()
                else:
                    props.extend(self._dotted_props())
                self.expect_punct(")")
                continue
            if label is not None and self.accept_punct("."):
                props.append(self.name())
                continue
            self.advance()
        if label is None and create:
            raise self.error(_EXPECT_SCHEMA)
        if create:
            kind = (
                EgSchemaKind.CREATE_INDEX
                if is_index
                else EgSchemaKind.CREATE_CONSTRAINT
            )
        else:
            kind = EgSchemaKind.DROP_INDEX if is_index else EgSchemaKind.DROP_CONSTRAINT
        return a.SchemaCommand(kind, label, tuple(props))

    def _name_list(self) -> list[str]:
        names = [self.name()]
        while self.accept_punct(","):
            names.append(self.name())
        return names

    def _dotted_props(self) -> list[str]:
        """`.p` after a consumed variable, then any `, n.q` that follow."""
        self.expect_punct(".")
        props = [self.name()]
        while self.accept_punct(","):
            self.variable()
            self.expect_punct(".")
            props.append(self.name())
        return props

    # --------------------------------------------------------------- patterns

    def pattern_list(self) -> tuple[a.PathPattern, ...]:
        patterns = [self.path_pattern()]
        while self.accept_punct(","):
            patterns.append(self.path_pattern())
        return tuple(patterns)

    def path_pattern(self) -> a.PathPattern:
        var = None
        if self.tok.kind in (EgTok.IDENT, EgTok.QUOTED_IDENT) and self.peek().is_punct(
            "="
        ):
            var = self.variable()
            self.advance()
        if self.at_word("SHORTESTPATH") or self.at_word("ALLSHORTESTPATHS"):
            raise CypherSyntaxError(self.tok.pos, self.tok.text)
        nodes = [self.node_pattern()]
        rels: list[a.RelPattern] = []
        while self.at_punct("-") or self.at_punct("<"):
            rels.append(self.rel_pattern())
            nodes.append(self.node_pattern())
        return a.PathPattern(var, tuple(nodes), tuple(rels))

    def node_pattern(self) -> a.NodePattern:
        if not self.accept_punct("("):
            raise self.error(_EXPECT_PATTERN)
        var = None
        if self.tok.kind in (EgTok.IDENT, EgTok.QUOTED_IDENT) and not self.at_punct(
            ":"
        ):
            var = self.variable()
        labels = self._labels()
        props = self._pattern_props()
        self.expect_punct(")")
        return a.NodePattern(var, labels, props)

    def _labels(self) -> tuple[str, ...]:
        labels: list[str] = []
        if self.accept_punct(":"):
            labels.append(self.name())
            while self.accept_punct("|") or self.accept_punct(":"):
                labels.append(self.name())
        return tuple(labels)

    def _pattern_props(self) -> a.Expr | None:
        if self.at_punct("{"):
            return self.map_literal()
        if self.tok.kind == EgTok.PARAM:
            return a.Param(str(self.advance().value))
        return None

    def rel_pattern(self) -> a.RelPattern:
        left_arrow = self.accept_punct("<")
        self.expect_punct("-")
        var = None
        types: tuple[str, ...] = ()
        props = None
        var_length: tuple[int, int | None] | None = None
        if self.accept_punct("["):
            if self.tok.kind in (EgTok.IDENT, EgTok.QUOTED_IDENT):
                var = self.variable()
            if self.accept_punct(":"):
                names = [self.name()]
                while self.accept_punct("|"):
                    self.accept_punct(":")
                    names.append(self.name())
                types = tuple(names)
            if self.accept_punct("*"):
                var_length = self._var_length()
            props = self._pattern_props()
            self.expect_punct("]")
        self.expect_punct("-")
        right_arrow = self.accept_punct(">")
        if left_arrow and not right_arrow:
            direction = EgDirection.IN
        elif right_arrow and not left_arrow:
            direction = EgDirection.OUT
        else:
            direction = EgDirection.BOTH
        return a.RelPattern(var, types, direction, props, var_length)

    def _int_token(self) -> int:
        token = self.advance()
        if not isinstance(token.value, int):
            raise self.error(_EXPECT_INT)
        return token.value

    def _var_length(self) -> tuple[int, int | None]:
        low: int | None = None
        high: int | None = None
        if self.tok.kind == EgTok.INT:
            low = self._int_token()
        if self.accept_punct(".."):
            if self.tok.kind == EgTok.INT:
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
        while self.accept_word("OR"):
            left = a.BinOp(EgBinary.OR, left, self._xor())
        return left

    def _xor(self) -> a.Expr:
        left = self._and()
        while self.accept_word("XOR"):
            left = a.BinOp(EgBinary.XOR, left, self._and())
        return left

    def _and(self) -> a.Expr:
        left = self._not()
        while self.accept_word("AND"):
            left = a.BinOp(EgBinary.AND, left, self._not())
        return left

    def _not(self) -> a.Expr:
        if self.accept_word("NOT"):
            return a.UnaryOp(EgUnary.NOT, self._not())
        return self._comparison()

    def _comparison(self) -> a.Expr:
        left = self._additive()
        while True:
            tok = self.tok
            if tok.kind == EgTok.PUNCT and tok.text in _COMPARISON_OPS:
                self.advance()
                left = a.BinOp(_COMPARISON_OPS[tok.text], left, self._additive())
            elif tok.is_word("IN"):
                self.advance()
                left = a.BinOp(EgBinary.IN, left, self._additive())
            elif tok.is_word("STARTS"):
                self.advance()
                self.expect_word("WITH")
                left = a.BinOp(EgBinary.STARTS_WITH, left, self._additive())
            elif tok.is_word("ENDS"):
                self.advance()
                self.expect_word("WITH")
                left = a.BinOp(EgBinary.ENDS_WITH, left, self._additive())
            elif tok.is_word("CONTAINS"):
                self.advance()
                left = a.BinOp(EgBinary.CONTAINS, left, self._additive())
            elif tok.is_word("IS"):
                self.advance()
                negated = self.accept_word("NOT")
                self.expect_word("NULL")
                left = a.IsNull(left, negated)
            else:
                return left

    def _additive(self) -> a.Expr:
        left = self._multiplicative()
        while self.tok.kind == EgTok.PUNCT and self.tok.text in _ADDITIVE_OPS:
            op = _ADDITIVE_OPS[self.advance().text]
            left = a.BinOp(op, left, self._multiplicative())
        return left

    def _multiplicative(self) -> a.Expr:
        left = self._power()
        while self.tok.kind == EgTok.PUNCT and self.tok.text in _MULTIPLICATIVE_OPS:
            op = _MULTIPLICATIVE_OPS[self.advance().text]
            left = a.BinOp(op, left, self._power())
        return left

    def _power(self) -> a.Expr:
        left = self._unary()
        while self.accept_punct("^"):
            left = a.BinOp(EgBinary.POW, left, self._unary())
        return left

    def _unary(self) -> a.Expr:
        if self.accept_punct("-"):
            return a.UnaryOp(EgUnary.NEG, self._unary())
        if self.accept_punct("+"):
            return self._unary()
        return self._postfix()

    def _postfix(self) -> a.Expr:
        expr = self._atom()
        while True:
            if self.at_punct(".") and not self.peek().is_punct("."):
                self.advance()
                expr = a.Prop(expr, self.name())
            elif self.at_punct("["):
                self.advance()
                if self.accept_punct(".."):
                    end = None if self.at_punct("]") else self.expression()
                    self.expect_punct("]")
                    expr = a.Slice(expr, None, end)
                    continue
                index = self.expression()
                if self.accept_punct(".."):
                    end = None if self.at_punct("]") else self.expression()
                    self.expect_punct("]")
                    expr = a.Slice(expr, index, end)
                    continue
                self.expect_punct("]")
                expr = a.Index(expr, index)
            elif self.at_punct(":") and isinstance(expr, a.Var):
                labels = self._labels()
                expr = a.LabelTest(expr, labels)
            else:
                return expr

    def _atom(self) -> a.Expr:
        tok = self.tok
        if tok.kind in (EgTok.INT, EgTok.FLOAT, EgTok.STRING):
            self.advance()
            return a.Literal(tok.value)
        if tok.kind == EgTok.PARAM:
            self.advance()
            return a.Param(str(tok.value))
        if tok.is_punct("("):
            self.advance()
            expr = self.expression()
            self.expect_punct(")")
            return expr
        if tok.is_punct("["):
            return self._list()
        if tok.is_punct("{"):
            return self.map_literal()
        if tok.kind == EgTok.QUOTED_IDENT:
            self.advance()
            return a.Var(tok.text)
        if tok.kind != EgTok.IDENT:
            raise self.error(_EXPECT_EXPRESSION)
        upper = tok.text.upper()
        if upper == "TRUE":
            self.advance()
            return a.Literal(True)
        if upper == "FALSE":
            self.advance()
            return a.Literal(False)
        if upper == "NULL":
            self.advance()
            return a.Literal(None)
        if upper == "CASE":
            return self._case()
        if (
            upper in _QUANTIFIERS
            and self.peek().is_punct("(")
            and self.peek(3).is_word("IN")
        ):
            return self._quantifier(_QUANTIFIERS[upper])
        if self.peek().is_punct("(") or (
            self.peek().is_punct(".") and self._is_namespaced_call()
        ):
            return self._function()
        if upper in EG_EXPRESSION_STOP_WORDS:
            raise self.error(_EXPECT_EXPRESSION)
        self.advance()
        return a.Var(tok.text)

    def _is_namespaced_call(self) -> bool:
        # `toLower(x)` vs `n.prop`: a call is name(.name)* followed by `(`.
        j = self.i
        while (
            self.tokens[j].kind == EgTok.IDENT
            and self.tokens[j + 1].is_punct(".")
            and self.tokens[j + 2].kind == EgTok.IDENT
        ):
            j += 2
        return self.tokens[j].kind == EgTok.IDENT and self.tokens[j + 1].is_punct("(")

    def _function(self) -> a.Expr:
        parts = [self.advance().text]
        while self.accept_punct("."):
            parts.append(self.name())
        name = ".".join(parts).lower()
        self.expect_punct("(")
        if self.accept_punct("*"):
            self.expect_punct(")")
            return a.FuncCall(name, (), star=True)
        distinct = self.accept_word("DISTINCT")
        args: tuple[a.Expr, ...] = ()
        if not self.at_punct(")"):
            args = self.expression_list()
        self.expect_punct(")")
        return a.FuncCall(name, args, distinct=distinct)

    def _quantifier(self, kind: EgQuantifier) -> a.Expr:
        self.advance()
        self.expect_punct("(")
        var = self.variable()
        self.expect_word("IN")
        source = self.expression()
        where: a.Expr = a.Literal(True)
        if self.accept_word("WHERE"):
            where = self.expression()
        self.expect_punct(")")
        return a.Quantifier(kind, var, source, where)

    def _list(self) -> a.Expr:
        self.expect_punct("[")
        if self.tok.kind in (EgTok.IDENT, EgTok.QUOTED_IDENT) and self.peek().is_word(
            "IN"
        ):
            var = self.variable()
            self.expect_word("IN")
            source = self.expression()
            where = self.expression() if self.accept_word("WHERE") else None
            projection = self.expression() if self.accept_punct("|") else None
            self.expect_punct("]")
            return a.ListComp(var, source, where, projection)
        items: list[a.Expr] = []
        if not self.at_punct("]"):
            items = list(self.expression_list())
        self.expect_punct("]")
        return a.ListLit(tuple(items))

    def map_literal(self) -> a.MapLit:
        self.expect_punct("{")
        items: list[tuple[str, a.Expr]] = []
        if not self.at_punct("}"):
            while True:
                key = self.name()
                self.expect_punct(":")
                items.append((key, self.expression()))
                if not self.accept_punct(","):
                    break
        self.expect_punct("}")
        return a.MapLit(tuple(items))

    def _case(self) -> a.Expr:
        self.expect_word("CASE")
        subject = None if self.at_word("WHEN") else self.expression()
        whens: list[tuple[a.Expr, a.Expr]] = []
        while self.accept_word("WHEN"):
            cond = self.expression()
            self.expect_word("THEN")
            whens.append((cond, self.expression()))
        default = self.expression() if self.accept_word("ELSE") else None
        self.expect_word("END")
        return a.Case(subject, tuple(whens), default)


def _token_source(tok: Token) -> str:
    if tok.kind == EgTok.QUOTED_IDENT:
        return "`" + tok.text.replace("`", "``") + "`"
    return tok.text
