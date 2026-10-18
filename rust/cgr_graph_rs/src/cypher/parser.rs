//! Recursive-descent parser: a port of
//! `codebase_rag/embedded_graph/parser.py`, method for method, with the
//! same precedence, associativity, lookahead and error positions.

use super::ast::{
    BinaryOp, Clause, Direction, Expr, Literal, NodePattern, PathPattern, Projection,
    ProjectionItem, Quantifier, Query, RelPattern, RemoveItem, RemoveKind, SchemaCommand,
    SchemaKind, SetItem, SetKind, SingleQuery, SortItem, Statement, UnaryOp, YieldItem,
};
use super::error::{Defer, DeferReason, Error, Expected, SyntaxError, SyntaxErrorKind};
use super::lexer::{tokenize, Token, TokenKind};
use super::vocab::{Kw, Punct, END_OF_QUERY};

/// Nesting (parenthesised and nested expressions, NOT, unary minus,
/// FOREACH) past which the query goes to Python. The reference spends a
/// dozen or so frames per level, so at this depth it is still far from its
/// recursion limit and is sure to return the tree this parser would; this
/// thread's stack stays small too.
const MAX_DEPTH: usize = 32;

/// Operators applied in a loop rather than by recursion (`a OR b OR ...`,
/// `n.k`, `l[0]`, `x IS NULL`) past which the query goes to Python. Each can
/// deepen the tree by a level, and building the Python tree and dropping
/// this one both recurse over its height; the cap bounds that height.
const MAX_CHAINED: usize = 1024;

type Parsed<T> = Result<T, Error>;

/// The statement `text` holds, or why there is none.
pub fn parse(text: &str) -> Parsed<Statement> {
    let source: Vec<char> = text.chars().collect();
    let tokens = tokenize(&source)?;
    Parser {
        tokens,
        source: &source,
        i: 0,
        depth: 0,
        chained: 0,
    }
    .statement()
}

struct Parser<'s> {
    tokens: Vec<Token>,
    source: &'s [char],
    i: usize,
    depth: usize,
    chained: usize,
}

fn comparison_op(punct: Punct) -> Option<BinaryOp> {
    Some(match punct {
        Punct::Eq => BinaryOp::Eq,
        Punct::Ne | Punct::BangEq => BinaryOp::Ne,
        Punct::Lt => BinaryOp::Lt,
        Punct::Gt => BinaryOp::Gt,
        Punct::Le => BinaryOp::Le,
        Punct::Ge => BinaryOp::Ge,
        Punct::Regex => BinaryOp::Regex,
        _ => return None,
    })
}

fn additive_op(punct: Punct) -> Option<BinaryOp> {
    Some(match punct {
        Punct::Plus => BinaryOp::Add,
        Punct::Minus => BinaryOp::Sub,
        _ => return None,
    })
}

fn multiplicative_op(punct: Punct) -> Option<BinaryOp> {
    Some(match punct {
        Punct::Star => BinaryOp::Mul,
        Punct::Slash => BinaryOp::Div,
        Punct::Percent => BinaryOp::Mod,
        _ => return None,
    })
}

fn quantifier(word: Kw) -> Option<Quantifier> {
    Some(match word {
        Kw::Any => Quantifier::Any,
        Kw::All => Quantifier::All,
        Kw::None => Quantifier::None,
        Kw::Single => Quantifier::Single,
        _ => return None,
    })
}

fn boxed(expr: Expr) -> Box<Expr> {
    Box::new(expr)
}

impl Parser<'_> {
    // ------------------------------------------------------------ primitives

    fn tok(&self) -> &Token {
        &self.tokens[self.i]
    }

    fn peek(&self, offset: usize) -> &Token {
        &self.tokens[(self.i + offset).min(self.tokens.len() - 1)]
    }

    /// Consumes the current token (never the end) and returns its index.
    fn advance(&mut self) -> usize {
        let at = self.i;
        if self.tokens[at].kind != TokenKind::Eof {
            self.i += 1;
        }
        at
    }

    /// Moves a consumed token's name or value out: the parser never looks
    /// back at one, only at its end offset.
    fn take(&mut self, at: usize) -> String {
        std::mem::take(&mut self.tokens[at].value)
    }

    fn error(&self, expected: Expected) -> Error {
        let tok = self.tok();
        let mut found = tok.text(self.source);
        if found.is_empty() {
            found = END_OF_QUERY.to_owned();
        }
        SyntaxError::new(tok.pos, SyntaxErrorKind::Unexpected { found, expected }).into()
    }

    fn decline(&self) -> Error {
        Defer {
            pos: self.tok().pos,
            reason: DeferReason::Nesting,
        }
        .into()
    }

    fn descend(&mut self) -> Parsed<()> {
        self.depth += 1;
        if self.depth > MAX_DEPTH {
            return Err(self.decline());
        }
        Ok(())
    }

    fn chain(&mut self) -> Parsed<()> {
        self.chained += 1;
        if self.chained > MAX_CHAINED {
            return Err(self.decline());
        }
        Ok(())
    }

    fn at_word(&self, word: Kw) -> bool {
        self.tok().is_word(word)
    }

    fn accept_word(&mut self, word: Kw) -> bool {
        if self.at_word(word) {
            self.advance();
            return true;
        }
        false
    }

    fn accept_any_word(&mut self, words: &[Kw]) -> bool {
        words.iter().any(|&word| self.accept_word(word))
    }

    fn expect_word(&mut self, word: Kw) -> Parsed<()> {
        if self.accept_word(word) {
            return Ok(());
        }
        Err(self.error(Expected::Word(word)))
    }

    fn at_punct(&self, punct: Punct) -> bool {
        self.tok().is_punct(punct)
    }

    fn accept_punct(&mut self, punct: Punct) -> bool {
        if self.at_punct(punct) {
            self.advance();
            return true;
        }
        false
    }

    fn expect_punct(&mut self, punct: Punct) -> Parsed<()> {
        if self.accept_punct(punct) {
            return Ok(());
        }
        Err(self.error(Expected::Punct(punct)))
    }

    /// Any identifier, keywords included: labels, keys, aliases.
    fn name(&mut self) -> Parsed<String> {
        if self.tok().is_name() {
            let at = self.advance();
            return Ok(self.take(at));
        }
        Err(self.error(Expected::Name))
    }

    fn variable(&mut self) -> Parsed<String> {
        let usable = match self.tok().kind {
            TokenKind::QuotedIdent => true,
            TokenKind::Ident(word) => !word.is_some_and(Kw::is_stop_word),
            _ => false,
        };
        if usable {
            let at = self.advance();
            return Ok(self.take(at));
        }
        Err(self.error(Expected::Name))
    }

    // ------------------------------------------------------------ statements

    fn statement(mut self) -> Parsed<Statement> {
        let result = if self.accept_word(Kw::Explain) || self.accept_word(Kw::Profile) {
            Statement::Explain(self.query()?)
        } else if self.at_schema_command() {
            Statement::Schema(self.schema_command()?)
        } else {
            Statement::Query(self.query()?)
        };
        self.accept_punct(Punct::Semicolon);
        if self.tok().kind != TokenKind::Eof {
            return Err(self.error(Expected::Clause));
        }
        Ok(result)
    }

    fn query(&mut self) -> Parsed<Query> {
        let mut parts = vec![self.single_query()?];
        let mut union_all = Vec::new();
        while self.accept_word(Kw::Union) {
            union_all.push(self.accept_word(Kw::All));
            parts.push(self.single_query()?);
        }
        Ok(Query { parts, union_all })
    }

    fn single_query(&mut self) -> Parsed<SingleQuery> {
        let mut clauses = Vec::new();
        while let Some(clause) = self.clause()? {
            clauses.push(clause);
        }
        if clauses.is_empty() {
            return Err(self.error(Expected::Clause));
        }
        Ok(SingleQuery { clauses })
    }

    fn clause(&mut self) -> Parsed<Option<Clause>> {
        let clause = if self.at_word(Kw::Optional) {
            self.advance();
            self.expect_word(Kw::Match)?;
            self.match_clause(true)?
        } else if self.accept_word(Kw::Match) {
            self.match_clause(false)?
        } else if self.accept_word(Kw::Unwind) {
            let expr = self.expression()?;
            self.expect_word(Kw::As)?;
            Clause::Unwind {
                expr,
                var: self.variable()?,
            }
        } else if self.accept_word(Kw::With) {
            Clause::Projection(self.projection(false)?)
        } else if self.accept_word(Kw::Return) {
            Clause::Projection(self.projection(true)?)
        } else if self.accept_word(Kw::Create) {
            Clause::Create {
                patterns: self.pattern_list()?,
            }
        } else if self.accept_word(Kw::Merge) {
            self.merge()?
        } else if self.accept_word(Kw::Set) {
            Clause::Set {
                items: self.set_items()?,
            }
        } else if self.accept_word(Kw::Remove) {
            self.remove()?
        } else if self.at_word(Kw::Detach) {
            self.advance();
            self.expect_word(Kw::Delete)?;
            Clause::Delete {
                detach: true,
                exprs: self.expression_list()?,
            }
        } else if self.accept_word(Kw::Delete) {
            Clause::Delete {
                detach: false,
                exprs: self.expression_list()?,
            }
        } else if self.accept_word(Kw::Foreach) {
            self.foreach()?
        } else if self.accept_word(Kw::Call) {
            self.call()?
        } else {
            return Ok(None);
        };
        Ok(Some(clause))
    }

    fn match_clause(&mut self, optional: bool) -> Parsed<Clause> {
        let patterns = self.pattern_list()?;
        let filter = if self.accept_word(Kw::Where) {
            Some(self.expression()?)
        } else {
            None
        };
        Ok(Clause::Match {
            optional,
            patterns,
            filter,
        })
    }

    fn projection(&mut self, is_return: bool) -> Parsed<Projection> {
        let distinct = self.accept_word(Kw::Distinct);
        let mut items = Vec::new();
        let star = self.accept_punct(Punct::Star);
        if star && !self.accept_punct(Punct::Comma) {
            return self.projection_tail(is_return, distinct, star, items);
        }
        loop {
            let start = self.tok().pos;
            let expr = self.expression()?;
            let alias = if self.accept_word(Kw::As) {
                self.name()?
            } else {
                self.source_text(start)
            };
            items.push(ProjectionItem { expr, alias });
            if !self.accept_punct(Punct::Comma) {
                break;
            }
        }
        self.projection_tail(is_return, distinct, star, items)
    }

    fn projection_tail(
        &mut self,
        is_return: bool,
        distinct: bool,
        star: bool,
        items: Vec<ProjectionItem>,
    ) -> Parsed<Projection> {
        let mut order = Vec::new();
        if self.accept_word(Kw::Order) {
            self.expect_word(Kw::By)?;
            loop {
                let expr = self.expression()?;
                let descending = self.accept_any_word(&[Kw::Desc, Kw::Descending]);
                if !descending {
                    self.accept_any_word(&[Kw::Asc, Kw::Ascending]);
                }
                order.push(SortItem { expr, descending });
                if !self.accept_punct(Punct::Comma) {
                    break;
                }
            }
        }
        let skip = if self.accept_word(Kw::Skip) {
            Some(self.expression()?)
        } else {
            None
        };
        let limit = if self.accept_word(Kw::Limit) {
            Some(self.expression()?)
        } else {
            None
        };
        let mut filter = None;
        if !is_return && self.accept_word(Kw::Where) {
            filter = Some(self.expression()?);
        }
        Ok(Projection {
            is_return,
            distinct,
            star,
            items,
            order,
            skip,
            limit,
            filter,
        })
    }

    /// An unaliased projection is named by its own text, as Memgraph does
    /// (`RETURN labels(n)[0]` yields a column `labels(n)[0]`).
    fn source_text(&self, start: usize) -> String {
        let end = self.tokens[self.i - 1].end;
        self.source
            .get(start..end)
            .map(|chars| chars.iter().collect())
            .unwrap_or_default()
    }

    fn merge(&mut self) -> Parsed<Clause> {
        let pattern = self.path_pattern()?;
        let mut on_create = Vec::new();
        let mut on_match = Vec::new();
        while self.at_word(Kw::On) {
            self.advance();
            if self.accept_word(Kw::Create) {
                self.expect_word(Kw::Set)?;
                on_create.extend(self.set_items()?);
            } else {
                self.expect_word(Kw::Match)?;
                self.expect_word(Kw::Set)?;
                on_match.extend(self.set_items()?);
            }
        }
        Ok(Clause::Merge {
            pattern,
            on_create,
            on_match,
        })
    }

    fn set_items(&mut self) -> Parsed<Vec<SetItem>> {
        let mut items = Vec::new();
        loop {
            let var = self.variable()?;
            let item = if self.accept_punct(Punct::Dot) {
                let key = self.name()?;
                self.expect_punct(Punct::Eq)?;
                let value = self.expression()?;
                SetItem {
                    kind: SetKind::Prop,
                    var,
                    key: Some(key),
                    value: Some(value),
                }
            } else if self.accept_punct(Punct::PlusEq) {
                let value = self.expression()?;
                SetItem {
                    kind: SetKind::Merge,
                    var,
                    key: None,
                    value: Some(value),
                }
            } else if self.accept_punct(Punct::Eq) {
                let value = self.expression()?;
                SetItem {
                    kind: SetKind::Replace,
                    var,
                    key: None,
                    value: Some(value),
                }
            } else if self.accept_punct(Punct::Colon) {
                SetItem {
                    kind: SetKind::Label,
                    var,
                    key: Some(self.name()?),
                    value: None,
                }
            } else {
                return Err(self.error(Expected::SetItem));
            };
            items.push(item);
            if !self.accept_punct(Punct::Comma) {
                return Ok(items);
            }
        }
    }

    fn remove(&mut self) -> Parsed<Clause> {
        let mut items = Vec::new();
        loop {
            let var = self.variable()?;
            let kind = if self.accept_punct(Punct::Dot) {
                RemoveKind::Prop
            } else {
                self.expect_punct(Punct::Colon)?;
                RemoveKind::Label
            };
            items.push(RemoveItem {
                kind,
                var,
                key: self.name()?,
            });
            if !self.accept_punct(Punct::Comma) {
                return Ok(Clause::Remove { items });
            }
        }
    }

    fn foreach(&mut self) -> Parsed<Clause> {
        self.descend()?;
        self.expect_punct(Punct::LParen)?;
        let var = self.variable()?;
        self.expect_word(Kw::In)?;
        let source = self.expression()?;
        self.expect_punct(Punct::Pipe)?;
        let mut clauses = Vec::new();
        while !self.at_punct(Punct::RParen) {
            let Some(clause) = self.clause()? else {
                return Err(self.error(Expected::Clause));
            };
            clauses.push(clause);
        }
        self.expect_punct(Punct::RParen)?;
        self.depth -= 1;
        Ok(Clause::Foreach {
            var,
            source,
            clauses,
        })
    }

    fn call(&mut self) -> Parsed<Clause> {
        let mut procedure = self.name()?;
        while self.accept_punct(Punct::Dot) {
            procedure.push_str(Punct::Dot.as_str());
            procedure.push_str(&self.name()?);
        }
        let mut args = Vec::new();
        self.expect_punct(Punct::LParen)?;
        if !self.at_punct(Punct::RParen) {
            args = self.expression_list()?;
        }
        self.expect_punct(Punct::RParen)?;
        let mut yields = Vec::new();
        let mut filter = None;
        if self.accept_word(Kw::Yield) {
            loop {
                let name = self.name()?;
                let alias = if self.accept_word(Kw::As) {
                    self.name()?
                } else {
                    name.clone()
                };
                yields.push(YieldItem { name, alias });
                if !self.accept_punct(Punct::Comma) {
                    break;
                }
            }
            if self.accept_word(Kw::Where) {
                filter = Some(self.expression()?);
            }
        }
        Ok(Clause::Call {
            procedure,
            args,
            yields,
            filter,
        })
    }

    fn expression_list(&mut self) -> Parsed<Vec<Expr>> {
        let mut exprs = vec![self.expression()?];
        while self.accept_punct(Punct::Comma) {
            exprs.push(self.expression()?);
        }
        Ok(exprs)
    }

    // ----------------------------------------------------------------- schema

    fn at_schema_command(&self) -> bool {
        let (tok, next) = (self.tok(), self.peek(1));
        if tok.is_word(Kw::Show) {
            return true;
        }
        if tok.is_word(Kw::Create) || tok.is_word(Kw::Drop) {
            return next.is_word(Kw::Index) || next.is_word(Kw::Constraint);
        }
        false
    }

    fn schema_command(&mut self) -> Parsed<SchemaCommand> {
        if self.accept_word(Kw::Show) {
            let mut words = Vec::new();
            while let TokenKind::Ident(word) = self.tok().kind {
                words.push(word);
                self.advance();
            }
            let kind = if words.contains(&Some(Kw::Constraint))
                || words.contains(&Some(Kw::Constraints))
            {
                SchemaKind::ShowConstraints
            } else if words.contains(&Some(Kw::Storage)) {
                SchemaKind::ShowStorage
            } else {
                SchemaKind::ShowIndexes
            };
            return Ok(SchemaCommand {
                kind,
                label: None,
                props: Vec::new(),
            });
        }
        let create = self.accept_word(Kw::Create);
        if !create {
            self.expect_word(Kw::Drop)?;
        }
        let is_index = self.accept_word(Kw::Index);
        if !is_index {
            self.expect_word(Kw::Constraint)?;
        }
        // Memgraph (`ON :L(p)`, `ON (n:L) ASSERT n.p`) and Neo4j (`name IF
        // NOT EXISTS FOR (n:L) ON (n.p)` / `REQUIRE n.p`) spellings both
        // reduce to a label and its properties; index names are the store's.
        let mut label = None;
        let mut props = Vec::new();
        while self.tok().kind != TokenKind::Eof && !self.at_punct(Punct::Semicolon) {
            if self.accept_punct(Punct::Colon) {
                label = Some(self.name()?);
                if self.accept_punct(Punct::LParen) {
                    props.extend(self.name_list()?);
                    self.expect_punct(Punct::RParen)?;
                }
                continue;
            }
            if self.accept_punct(Punct::LParen) {
                self.variable()?;
                if self.accept_punct(Punct::Colon) {
                    label = Some(self.name()?);
                } else {
                    props.extend(self.dotted_props()?);
                }
                self.expect_punct(Punct::RParen)?;
                continue;
            }
            if label.is_some() && self.accept_punct(Punct::Dot) {
                props.push(self.name()?);
                continue;
            }
            self.advance();
        }
        if label.is_none() && create {
            return Err(self.error(Expected::Schema));
        }
        let kind = match (create, is_index) {
            (true, true) => SchemaKind::CreateIndex,
            (true, false) => SchemaKind::CreateConstraint,
            (false, true) => SchemaKind::DropIndex,
            (false, false) => SchemaKind::DropConstraint,
        };
        Ok(SchemaCommand { kind, label, props })
    }

    fn name_list(&mut self) -> Parsed<Vec<String>> {
        let mut names = vec![self.name()?];
        while self.accept_punct(Punct::Comma) {
            names.push(self.name()?);
        }
        Ok(names)
    }

    /// `.p` after a consumed variable, then any `, n.q` that follow.
    fn dotted_props(&mut self) -> Parsed<Vec<String>> {
        self.expect_punct(Punct::Dot)?;
        let mut props = vec![self.name()?];
        while self.accept_punct(Punct::Comma) {
            self.variable()?;
            self.expect_punct(Punct::Dot)?;
            props.push(self.name()?);
        }
        Ok(props)
    }

    // --------------------------------------------------------------- patterns

    fn pattern_list(&mut self) -> Parsed<Vec<PathPattern>> {
        let mut patterns = vec![self.path_pattern()?];
        while self.accept_punct(Punct::Comma) {
            patterns.push(self.path_pattern()?);
        }
        Ok(patterns)
    }

    fn path_pattern(&mut self) -> Parsed<PathPattern> {
        let mut var = None;
        if self.tok().is_name() && self.peek(1).is_punct(Punct::Eq) {
            var = Some(self.variable()?);
            self.advance();
        }
        if self.at_word(Kw::ShortestPath) || self.at_word(Kw::AllShortestPaths) {
            let tok = self.tok();
            return Err(
                SyntaxError::new(tok.pos, SyntaxErrorKind::Unsupported(tok.value.clone())).into(),
            );
        }
        let mut nodes = vec![self.node_pattern()?];
        let mut rels = Vec::new();
        while self.at_punct(Punct::Minus) || self.at_punct(Punct::Lt) {
            rels.push(self.rel_pattern()?);
            nodes.push(self.node_pattern()?);
        }
        Ok(PathPattern { var, nodes, rels })
    }

    fn node_pattern(&mut self) -> Parsed<NodePattern> {
        if !self.accept_punct(Punct::LParen) {
            return Err(self.error(Expected::Pattern));
        }
        let var = if self.tok().is_name() {
            Some(self.variable()?)
        } else {
            None
        };
        let labels = self.labels()?;
        let props = self.pattern_props()?;
        self.expect_punct(Punct::RParen)?;
        Ok(NodePattern { var, labels, props })
    }

    fn labels(&mut self) -> Parsed<Vec<String>> {
        let mut labels = Vec::new();
        if self.accept_punct(Punct::Colon) {
            labels.push(self.name()?);
            while self.accept_punct(Punct::Pipe) || self.accept_punct(Punct::Colon) {
                labels.push(self.name()?);
            }
        }
        Ok(labels)
    }

    fn pattern_props(&mut self) -> Parsed<Option<Expr>> {
        if self.at_punct(Punct::LBrace) {
            return self.map_literal().map(Some);
        }
        if self.tok().kind == TokenKind::Param {
            let at = self.advance();
            return Ok(Some(Expr::Param(self.take(at))));
        }
        Ok(None)
    }

    fn rel_pattern(&mut self) -> Parsed<RelPattern> {
        let left_arrow = self.accept_punct(Punct::Lt);
        self.expect_punct(Punct::Minus)?;
        let mut var = None;
        let mut types = Vec::new();
        let mut props = None;
        let mut var_length = None;
        if self.accept_punct(Punct::LBracket) {
            if self.tok().is_name() {
                var = Some(self.variable()?);
            }
            if self.accept_punct(Punct::Colon) {
                types.push(self.name()?);
                while self.accept_punct(Punct::Pipe) {
                    self.accept_punct(Punct::Colon);
                    types.push(self.name()?);
                }
            }
            if self.accept_punct(Punct::Star) {
                var_length = Some(self.var_length()?);
            }
            props = self.pattern_props()?;
            self.expect_punct(Punct::RBracket)?;
        }
        self.expect_punct(Punct::Minus)?;
        let right_arrow = self.accept_punct(Punct::Gt);
        let direction = match (left_arrow, right_arrow) {
            (true, false) => Direction::In,
            (false, true) => Direction::Out,
            _ => Direction::Both,
        };
        Ok(RelPattern {
            var,
            types,
            direction,
            props,
            var_length,
        })
    }

    fn int_token(&mut self) -> Parsed<i64> {
        let at = self.advance();
        match self.tokens[at].kind {
            TokenKind::Int(value) => Ok(value),
            _ => Err(self.error(Expected::Int)),
        }
    }

    fn var_length(&mut self) -> Parsed<(i64, Option<i64>)> {
        let mut low = None;
        if let TokenKind::Int(_) = self.tok().kind {
            low = Some(self.int_token()?);
        }
        if self.accept_punct(Punct::Range) {
            let mut high = None;
            if let TokenKind::Int(_) = self.tok().kind {
                high = Some(self.int_token()?);
            }
            return Ok((low.unwrap_or(1), high));
        }
        Ok(match low {
            Some(low) => (low, Some(low)),
            None => (1, None),
        })
    }

    // ------------------------------------------------------------ expressions

    fn expression(&mut self) -> Parsed<Expr> {
        self.descend()?;
        let expr = self.or()?;
        self.depth -= 1;
        Ok(expr)
    }

    fn or(&mut self) -> Parsed<Expr> {
        let mut left = self.xor()?;
        while self.accept_word(Kw::Or) {
            self.chain()?;
            left = Expr::BinOp {
                op: BinaryOp::Or,
                left: boxed(left),
                right: boxed(self.xor()?),
            };
        }
        Ok(left)
    }

    fn xor(&mut self) -> Parsed<Expr> {
        let mut left = self.and()?;
        while self.accept_word(Kw::Xor) {
            self.chain()?;
            left = Expr::BinOp {
                op: BinaryOp::Xor,
                left: boxed(left),
                right: boxed(self.and()?),
            };
        }
        Ok(left)
    }

    fn and(&mut self) -> Parsed<Expr> {
        let mut left = self.not()?;
        while self.accept_word(Kw::And) {
            self.chain()?;
            left = Expr::BinOp {
                op: BinaryOp::And,
                left: boxed(left),
                right: boxed(self.not()?),
            };
        }
        Ok(left)
    }

    fn not(&mut self) -> Parsed<Expr> {
        if self.accept_word(Kw::Not) {
            self.descend()?;
            let operand = self.not()?;
            self.depth -= 1;
            return Ok(Expr::UnaryOp {
                op: UnaryOp::Not,
                operand: boxed(operand),
            });
        }
        self.comparison()
    }

    fn comparison(&mut self) -> Parsed<Expr> {
        let mut left = self.additive()?;
        loop {
            let op = match self.tok().kind {
                TokenKind::Punct(punct) => match comparison_op(punct) {
                    Some(op) => op,
                    None => return Ok(left),
                },
                TokenKind::Ident(Some(Kw::In)) => BinaryOp::In,
                TokenKind::Ident(Some(Kw::Starts)) => BinaryOp::StartsWith,
                TokenKind::Ident(Some(Kw::Ends)) => BinaryOp::EndsWith,
                TokenKind::Ident(Some(Kw::Contains)) => BinaryOp::Contains,
                TokenKind::Ident(Some(Kw::Is)) => {
                    self.advance();
                    let negated = self.accept_word(Kw::Not);
                    self.expect_word(Kw::Null)?;
                    self.chain()?;
                    left = Expr::IsNull {
                        operand: boxed(left),
                        negated,
                    };
                    continue;
                }
                _ => return Ok(left),
            };
            self.advance();
            if matches!(op, BinaryOp::StartsWith | BinaryOp::EndsWith) {
                self.expect_word(Kw::With)?;
            }
            self.chain()?;
            left = Expr::BinOp {
                op,
                left: boxed(left),
                right: boxed(self.additive()?),
            };
        }
    }

    fn additive(&mut self) -> Parsed<Expr> {
        let mut left = self.multiplicative()?;
        while let TokenKind::Punct(punct) = self.tok().kind {
            let Some(op) = additive_op(punct) else { break };
            self.advance();
            self.chain()?;
            left = Expr::BinOp {
                op,
                left: boxed(left),
                right: boxed(self.multiplicative()?),
            };
        }
        Ok(left)
    }

    fn multiplicative(&mut self) -> Parsed<Expr> {
        let mut left = self.power()?;
        while let TokenKind::Punct(punct) = self.tok().kind {
            let Some(op) = multiplicative_op(punct) else {
                break;
            };
            self.advance();
            self.chain()?;
            left = Expr::BinOp {
                op,
                left: boxed(left),
                right: boxed(self.power()?),
            };
        }
        Ok(left)
    }

    fn power(&mut self) -> Parsed<Expr> {
        let mut left = self.unary()?;
        while self.accept_punct(Punct::Caret) {
            self.chain()?;
            left = Expr::BinOp {
                op: BinaryOp::Pow,
                left: boxed(left),
                right: boxed(self.unary()?),
            };
        }
        Ok(left)
    }

    fn unary(&mut self) -> Parsed<Expr> {
        let negate = self.accept_punct(Punct::Minus);
        if negate || self.accept_punct(Punct::Plus) {
            self.descend()?;
            let operand = self.unary()?;
            self.depth -= 1;
            if negate {
                return Ok(Expr::UnaryOp {
                    op: UnaryOp::Neg,
                    operand: boxed(operand),
                });
            }
            return Ok(operand);
        }
        self.postfix()
    }

    fn postfix(&mut self) -> Parsed<Expr> {
        let mut expr = self.atom()?;
        loop {
            if self.at_punct(Punct::Dot) && !self.peek(1).is_punct(Punct::Dot) {
                self.advance();
                self.chain()?;
                expr = Expr::Prop {
                    subject: boxed(expr),
                    key: self.name()?,
                };
            } else if self.at_punct(Punct::LBracket) {
                self.advance();
                self.chain()?;
                if self.accept_punct(Punct::Range) {
                    let end = self.slice_end()?;
                    expr = Expr::Slice {
                        subject: boxed(expr),
                        start: None,
                        end,
                    };
                    continue;
                }
                let index = self.expression()?;
                if self.accept_punct(Punct::Range) {
                    let end = self.slice_end()?;
                    expr = Expr::Slice {
                        subject: boxed(expr),
                        start: Some(boxed(index)),
                        end,
                    };
                    continue;
                }
                self.expect_punct(Punct::RBracket)?;
                expr = Expr::Index {
                    subject: boxed(expr),
                    index: boxed(index),
                };
            } else if self.at_punct(Punct::Colon) && matches!(expr, Expr::Var(_)) {
                self.chain()?;
                let labels = self.labels()?;
                expr = Expr::LabelTest {
                    subject: boxed(expr),
                    labels,
                };
            } else {
                return Ok(expr);
            }
        }
    }

    /// What follows `..` in a slice, through the closing bracket.
    fn slice_end(&mut self) -> Parsed<Option<Box<Expr>>> {
        let end = if self.at_punct(Punct::RBracket) {
            None
        } else {
            Some(boxed(self.expression()?))
        };
        self.expect_punct(Punct::RBracket)?;
        Ok(end)
    }

    fn atom(&mut self) -> Parsed<Expr> {
        let word = match self.tok().kind {
            TokenKind::Int(value) => {
                self.advance();
                return Ok(Expr::Literal(Literal::Int(value)));
            }
            TokenKind::Float(value) => {
                self.advance();
                return Ok(Expr::Literal(Literal::Float(value)));
            }
            TokenKind::Str => {
                let at = self.advance();
                return Ok(Expr::Literal(Literal::Str(self.take(at))));
            }
            TokenKind::Param => {
                let at = self.advance();
                return Ok(Expr::Param(self.take(at)));
            }
            TokenKind::Punct(Punct::LParen) => {
                self.advance();
                let expr = self.expression()?;
                self.expect_punct(Punct::RParen)?;
                return Ok(expr);
            }
            TokenKind::Punct(Punct::LBracket) => return self.list(),
            TokenKind::Punct(Punct::LBrace) => return self.map_literal(),
            TokenKind::QuotedIdent => {
                let at = self.advance();
                return Ok(Expr::Var(self.take(at)));
            }
            TokenKind::Ident(word) => word,
            _ => return Err(self.error(Expected::Expression)),
        };
        let literal = match word {
            Some(Kw::True) => Some(Literal::Bool(true)),
            Some(Kw::False) => Some(Literal::Bool(false)),
            Some(Kw::Null) => Some(Literal::Null),
            Some(Kw::Case) => return self.case(),
            _ => None,
        };
        if let Some(literal) = literal {
            self.advance();
            return Ok(Expr::Literal(literal));
        }
        if let Some(kind) = word.and_then(quantifier) {
            if self.peek(1).is_punct(Punct::LParen) && self.peek(3).is_word(Kw::In) {
                return self.quantifier(kind);
            }
        }
        if self.peek(1).is_punct(Punct::LParen)
            || (self.peek(1).is_punct(Punct::Dot) && self.is_namespaced_call())
        {
            return self.function();
        }
        if word.is_some_and(Kw::is_stop_word) {
            return Err(self.error(Expected::Expression));
        }
        let at = self.advance();
        Ok(Expr::Var(self.take(at)))
    }

    /// `toLower(x)` vs `n.prop`: a call is name(.name)* followed by `(`.
    fn is_namespaced_call(&self) -> bool {
        let is_ident = |j: usize| {
            matches!(
                self.tokens.get(j),
                Some(Token {
                    kind: TokenKind::Ident(_),
                    ..
                })
            )
        };
        let is_punct = |j: usize, punct| self.tokens.get(j).is_some_and(|tok| tok.is_punct(punct));
        let mut j = self.i;
        while is_ident(j) && is_punct(j + 1, Punct::Dot) && is_ident(j + 2) {
            j += 2;
        }
        is_ident(j) && is_punct(j + 1, Punct::LParen)
    }

    fn function(&mut self) -> Parsed<Expr> {
        let first = self.advance();
        let mut name = self.take(first);
        while self.accept_punct(Punct::Dot) {
            name.push_str(Punct::Dot.as_str());
            name.push_str(&self.name()?);
        }
        // Every part is a bare ASCII identifier (`is_namespaced_call` saw
        // to that), so ASCII lowering is Python's `str.lower()` here.
        name.make_ascii_lowercase();
        self.expect_punct(Punct::LParen)?;
        if self.accept_punct(Punct::Star) {
            self.expect_punct(Punct::RParen)?;
            return Ok(Expr::FuncCall {
                name,
                args: Vec::new(),
                distinct: false,
                star: true,
            });
        }
        let distinct = self.accept_word(Kw::Distinct);
        let mut args = Vec::new();
        if !self.at_punct(Punct::RParen) {
            args = self.expression_list()?;
        }
        self.expect_punct(Punct::RParen)?;
        Ok(Expr::FuncCall {
            name,
            args,
            distinct,
            star: false,
        })
    }

    fn quantifier(&mut self, kind: Quantifier) -> Parsed<Expr> {
        self.advance();
        self.expect_punct(Punct::LParen)?;
        let var = self.variable()?;
        self.expect_word(Kw::In)?;
        let source = boxed(self.expression()?);
        let mut filter = boxed(Expr::Literal(Literal::Bool(true)));
        if self.accept_word(Kw::Where) {
            filter = boxed(self.expression()?);
        }
        self.expect_punct(Punct::RParen)?;
        Ok(Expr::Quantifier {
            kind,
            var,
            source,
            filter,
        })
    }

    fn list(&mut self) -> Parsed<Expr> {
        self.expect_punct(Punct::LBracket)?;
        if self.tok().is_name() && self.peek(1).is_word(Kw::In) {
            let var = self.variable()?;
            self.expect_word(Kw::In)?;
            let source = boxed(self.expression()?);
            let filter = if self.accept_word(Kw::Where) {
                Some(boxed(self.expression()?))
            } else {
                None
            };
            let projection = if self.accept_punct(Punct::Pipe) {
                Some(boxed(self.expression()?))
            } else {
                None
            };
            self.expect_punct(Punct::RBracket)?;
            return Ok(Expr::ListComp {
                var,
                source,
                filter,
                projection,
            });
        }
        let mut items = Vec::new();
        if !self.at_punct(Punct::RBracket) {
            items = self.expression_list()?;
        }
        self.expect_punct(Punct::RBracket)?;
        Ok(Expr::ListLit(items))
    }

    fn map_literal(&mut self) -> Parsed<Expr> {
        self.expect_punct(Punct::LBrace)?;
        let mut items = Vec::new();
        if !self.at_punct(Punct::RBrace) {
            loop {
                let key = self.name()?;
                self.expect_punct(Punct::Colon)?;
                items.push((key, self.expression()?));
                if !self.accept_punct(Punct::Comma) {
                    break;
                }
            }
        }
        self.expect_punct(Punct::RBrace)?;
        Ok(Expr::MapLit(items))
    }

    fn case(&mut self) -> Parsed<Expr> {
        self.expect_word(Kw::Case)?;
        let subject = if self.at_word(Kw::When) {
            None
        } else {
            Some(boxed(self.expression()?))
        };
        let mut whens = Vec::new();
        while self.accept_word(Kw::When) {
            let condition = self.expression()?;
            self.expect_word(Kw::Then)?;
            whens.push((condition, self.expression()?));
        }
        let default = if self.accept_word(Kw::Else) {
            Some(boxed(self.expression()?))
        } else {
            None
        };
        self.expect_word(Kw::End)?;
        Ok(Expr::Case {
            subject,
            whens,
            default,
        })
    }
}
