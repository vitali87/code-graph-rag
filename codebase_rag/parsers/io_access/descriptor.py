from __future__ import annotations

from dataclasses import dataclass

from ... import constants as cs


@dataclass(frozen=True)
class LanguageDescriptor:
    # (H) Per-language tree-sitter node types the I/O walk needs. Field names
    # (H) (function/arguments/body) are shared across grammars, so only the node
    # (H) types that actually differ from Python are captured here. Lets the
    # (H) direct-sink walk (issue 714) run on non-Python languages without a
    # (H) Python-specific rewrite.
    call_type: str
    string_type: str
    string_content_type: str
    # (H) None where the grammar has no keyword-argument node (JS/TS pass an
    # (H) options object instead), so every positional arg counts.
    keyword_arg_type: str | None
    # (H) Nested definitions whose body is a separate caller: the walk prunes them
    # (H) so a nested function's I/O is not credited to the enclosing one.
    nested_scope_types: frozenset[str]
    # (H) Local-binding detection so a name declared in the caller scope (a local
    # (H) `const fs`, `function fetch`, or a parameter) shadows the builtin sink.
    identifier_type: str
    declarator_type: str
    params_field: str
    # (H) A nested block introduces a new lexical scope: const/let declared inside
    # (H) it do not shadow the enclosing scope, so declarator collection stops here.
    block_scope_type: str
    # (H) Extra declaration node types (beyond declarator_type) whose bound names also
    # (H) shadow a builtin -- Go's `var`/`const`/`range` specs; empty for JS/TS.
    extra_declarator_types: frozenset[str]
    # (H) True when a dotted sink call requires its head to be an imported package
    # (H) (Go always imports stdlib; a package-scope `var os` is not the stdlib os).
    # (H) False for JS/TS, whose sinks include unimported globals (`console`, `fetch`).
    sinks_require_import: bool
    # (H) Member/subscript access node types + fields, for env reads like
    # (H) `process.env.X` (member) and `process.env['X']` (subscript).
    member_expression_type: str
    subscript_type: str
    object_field: str
    property_field: str
    subscript_index_field: str


_JS_TS_DESCRIPTOR = LanguageDescriptor(
    call_type=cs.TS_CALL_EXPRESSION,
    string_type=cs.TS_STRING,
    string_content_type=cs.TS_STRING_FRAGMENT,
    keyword_arg_type=None,
    nested_scope_types=frozenset(
        {
            cs.TS_FUNCTION_DECLARATION,
            cs.TS_GENERATOR_FUNCTION_DECLARATION,
            cs.TS_FUNCTION_EXPRESSION,
            cs.TS_ARROW_FUNCTION,
            cs.TS_METHOD_DEFINITION,
        }
    ),
    identifier_type=cs.TS_PY_IDENTIFIER,
    declarator_type=cs.TS_VARIABLE_DECLARATOR,
    params_field=cs.TS_FIELD_PARAMETERS,
    block_scope_type=cs.TS_STATEMENT_BLOCK,
    extra_declarator_types=frozenset(),
    sinks_require_import=False,
    member_expression_type=cs.TS_MEMBER_EXPRESSION,
    subscript_type=cs.TS_SUBSCRIPT_EXPRESSION,
    object_field=cs.FIELD_OBJECT,
    property_field=cs.FIELD_PROPERTY,
    subscript_index_field=cs.TS_FIELD_INDEX,
)

_GO_DESCRIPTOR = LanguageDescriptor(
    call_type=cs.TS_GO_CALL_EXPRESSION,
    string_type=cs.TS_GO_INTERPRETED_STRING,
    string_content_type=cs.TS_GO_INTERPRETED_STRING_CONTENT,
    keyword_arg_type=None,
    nested_scope_types=frozenset(
        {
            cs.TS_GO_FUNCTION_DECLARATION,
            cs.TS_GO_METHOD_DECLARATION,
            cs.TS_GO_FUNC_LITERAL,
        }
    ),
    # (H) Go local declarations that shadow a package name: `:=` (declarator_type),
    # (H) `var`/`const`/`range` (extra_declarator_types), and parameters. Go DOES
    # (H) allow a local to shadow an imported package, so these must be collected.
    identifier_type=cs.TS_GO_IDENTIFIER,
    declarator_type=cs.TS_GO_SHORT_VAR_DECLARATION,
    params_field=cs.TS_FIELD_PARAMETERS,
    block_scope_type=cs.TS_GO_BLOCK,
    extra_declarator_types=frozenset(
        {cs.TS_GO_VAR_SPEC, cs.TS_GO_CONST_SPEC, cs.TS_GO_RANGE_CLAUSE}
    ),
    sinks_require_import=True,
    # (H) Inert for Go (no IO_MEMBER_READS entry): Go env access is a call
    # (H) (`os.Getenv`), not member access. Filled with Go's selector/subscript shapes.
    member_expression_type=cs.TS_GO_SELECTOR_EXPRESSION,
    subscript_type=cs.TS_GO_INDEX_EXPRESSION,
    object_field=cs.TS_GO_FIELD_OPERAND,
    property_field=cs.TS_GO_FIELD_FIELD,
    subscript_index_field=cs.TS_GO_FIELD_INDEX,
)

# (H) Non-Python languages with a direct-sink descriptor. Python keeps its own
# (H) handle-aware walk; each new language lands one entry (plus registry rows).
LANGUAGE_DESCRIPTORS: dict[cs.SupportedLanguage, LanguageDescriptor] = {
    cs.SupportedLanguage.JS: _JS_TS_DESCRIPTOR,
    cs.SupportedLanguage.TS: _JS_TS_DESCRIPTOR,
    cs.SupportedLanguage.TSX: _JS_TS_DESCRIPTOR,
    cs.SupportedLanguage.GO: _GO_DESCRIPTOR,
}
