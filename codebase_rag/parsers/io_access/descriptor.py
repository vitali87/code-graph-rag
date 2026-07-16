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
    # (H) Loop-clause declaration nodes (Java for-each, Go `range`) whose bound var is
    # (H) in scope in the loop BODY only -- NOT the iterable header (evaluated before
    # (H) the var binds) nor sibling statements. The source-order walk seeds only the
    # (H) body with these, so a sink in the iterable still resolves to the global.
    loop_declarator_types: frozenset[str]
    # (H) A wrapper node that holds a block's statements as its children (Go's
    # (H) `statement_list`); None where a block's children ARE the statements (JS,
    # (H) Java). The source-order walk unwraps it so per-statement shadowing sees the
    # (H) real statement boundaries instead of one giant container "statement".
    statement_container_type: str | None
    # (H) True when a dotted sink call requires its head to be an imported package
    # (H) (Go always imports stdlib; a package-scope `var os` is not the stdlib os).
    # (H) False for JS/TS, whose sinks include unimported globals (`console`, `fetch`).
    sinks_require_import: bool
    # (H) True when declarations hoist over the whole block (JS `function`/`var`, and
    # (H) const/let are lexically in scope before their line via the TDZ), so a local
    # (H) shadows every use in the block. False for declare-at-point languages (Go,
    # (H) Java), where a local shadows only the uses that FOLLOW its declaration -- so
    # (H) the walk must add declarations in source order, not block-wide up front.
    hoisted_declarations: bool
    # (H) Whether a local is in scope in its OWN initializer (and later comma-declarators
    # (H) in the same statement). True for Java (JLS 6.3: `T System = System.getenv()`
    # (H) resolves the local) -> add the name BEFORE walking the statement. False for Go
    # (H) (scope starts AFTER the ShortVarDecl: `os := os.Getenv()` reads the package)
    # (H) -> add it AFTER. Only consulted when hoisted_declarations is False.
    decl_in_own_initializer: bool
    # (H) The declaration-statement node whose declarators must be scoped in source
    # (H) order within the statement (Java `local_variable_declaration`): in
    # (H) `T x = sink(), System = ...` the first initializer runs before `System` binds,
    # (H) so each declarator's initializer sees only the declarators up to itself. None
    # (H) where no such per-declarator ordering is needed (Go's list-assign RHS is
    # (H) evaluated wholesale; JS is hoisted).
    declaration_statement_type: str | None
    # (H) Macro-call node whose name is matched against a per-language macro sink table
    # (H) (Rust `println!`/`eprintln!` write STDOUT); None where the language has no such
    # (H) macro I/O. The macro name lives in the `macro` field.
    macro_type: str | None
    # (H) Member/subscript access node types + fields, for env reads like
    # (H) `process.env.X` (member) and `process.env['X']` (subscript).
    member_expression_type: str
    subscript_type: str
    object_field: str
    property_field: str
    subscript_index_field: str
    # (H) Path separator for scoped call names (Rust `::`). When set, sink resolution
    # (H) expands the head segment through the import map on THIS separator (`use
    # (H) std::fs; fs::write` -> `std::fs::write`) instead of the default `.`, so a bare
    # (H) short path matches std only when its head is genuinely imported. None = `.`.
    scope_separator: str | None = None
    # (H) Stream-insertion sink (C++ `std::cout << x`): a binary_expression whose
    # (H) `operator` field equals stream_sink_operator and whose left-spine base
    # (H) (cout/cerr) is a stream sink. None where the language has no such operator I/O.
    stream_sink_type: str | None = None
    stream_sink_operator: str | None = None
    # (H) Field on `declarator_type` holding the bound name when it is NOT a plain
    # (H) `name`/`left` field but a nested declarator (C++ `init_declarator` ->
    # (H) `declarator`, unwrapped through pointer/reference declarators). None = the
    # (H) language binds via `name`/`left` (JS/Go/Java/Rust). Used by the flow walk.
    declarator_name_field: str | None = None
    # (H) `new`-expression node (Java object_creation_expression) whose `type` names
    # (H) a handle constructor (`new FileWriter("x")`); None where handles are only
    # (H) call-shaped (issue #714 handle walk).
    new_expression_type: str | None = None
    # (H) Stream-extraction operator (C++ `>>`): on a bound stream handle it is a
    # (H) READ of that handle's resource. None where the language has none.
    stream_extract_operator: str | None = None


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
    loop_declarator_types=frozenset(),
    statement_container_type=None,
    sinks_require_import=False,
    hoisted_declarations=True,
    decl_in_own_initializer=True,
    declaration_statement_type=None,
    macro_type=None,
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
    loop_declarator_types=frozenset({cs.TS_GO_RANGE_CLAUSE}),
    statement_container_type=cs.TS_GO_STATEMENT_LIST,
    sinks_require_import=True,
    # (H) Go is declare-at-point (`:=`/`var` bind from that line on), so a local named
    # (H) after a valid package call must not retroactively shadow it -- source order.
    hoisted_declarations=False,
    decl_in_own_initializer=False,
    declaration_statement_type=None,
    # (H) Inert for Go (no IO_MEMBER_READS entry): Go env access is a call
    # (H) (`os.Getenv`), not member access. Filled with Go's selector/subscript shapes.
    macro_type=None,
    member_expression_type=cs.TS_GO_SELECTOR_EXPRESSION,
    subscript_type=cs.TS_GO_INDEX_EXPRESSION,
    object_field=cs.TS_GO_FIELD_OPERAND,
    property_field=cs.TS_GO_FIELD_FIELD,
    subscript_index_field=cs.TS_GO_FIELD_INDEX,
)

_JAVA_DESCRIPTOR = LanguageDescriptor(
    call_type=cs.TS_JAVA_METHOD_INVOCATION,
    string_type=cs.TS_JAVA_STRING_LITERAL,
    string_content_type=cs.TS_STRING_FRAGMENT,
    keyword_arg_type=None,
    nested_scope_types=frozenset(
        {
            cs.TS_METHOD_DECLARATION,
            cs.TS_CONSTRUCTOR_DECLARATION,
            cs.TS_JAVA_LAMBDA_EXPRESSION,
        }
    )
    | cs.JAVA_CLASS_NODE_TYPES,
    # (H) Java locals that shadow the `System`/`Files` global head: a
    # (H) `variable_declarator` (`Object System = ...`), a `formal_parameter`, an
    # (H) `enhanced_for_statement` (for-each) loop var, and a try-with-resources
    # (H) `resource` declaration (extra_declarator_types) -- the latter binds via
    # (H) `name`/`value` exactly like a declarator, so it also carries handle and
    # (H) taint bindings.
    identifier_type=cs.TS_IDENTIFIER,
    declarator_type=cs.TS_VARIABLE_DECLARATOR,
    params_field=cs.TS_FIELD_PARAMETERS,
    block_scope_type=cs.TS_JAVA_BLOCK,
    extra_declarator_types=frozenset(
        {cs.TS_ENHANCED_FOR_STATEMENT, cs.TS_JAVA_RESOURCE}
    ),
    loop_declarator_types=frozenset({cs.TS_ENHANCED_FOR_STATEMENT}),
    statement_container_type=None,
    # (H) Java's System/Files sink heads are java.lang / java.nio globals that never
    # (H) appear in import_map, so requiring an import would reject every sink.
    sinks_require_import=False,
    # (H) Java locals are declare-at-point (in scope from their statement on), so a
    # (H) later local must not shadow an earlier same-named sink call -- source order.
    hoisted_declarations=False,
    decl_in_own_initializer=True,
    declaration_statement_type=cs.TS_LOCAL_VARIABLE_DECLARATION,
    # (H) Inert (no IO_MEMBER_READS for Java): env access is a call. Filled with Java's
    # (H) field_access (object/field) and array_access (index) shapes for correctness.
    macro_type=None,
    member_expression_type=cs.TS_FIELD_ACCESS,
    subscript_type=cs.TS_JAVA_ARRAY_ACCESS,
    object_field=cs.FIELD_OBJECT,
    property_field=cs.JAVA_FIELD_FIELD,
    subscript_index_field=cs.JAVA_FIELD_INDEX,
    new_expression_type=cs.TS_OBJECT_CREATION_EXPRESSION,
)

_RUST_DESCRIPTOR = LanguageDescriptor(
    call_type=cs.TS_RS_CALL_EXPRESSION,
    string_type=cs.TS_RS_STRING_LITERAL,
    string_content_type=cs.TS_RS_STRING_CONTENT,
    keyword_arg_type=None,
    nested_scope_types=frozenset({cs.TS_RS_FUNCTION_ITEM, cs.TS_RS_CLOSURE_EXPRESSION}),
    # (H) Rust `let x = ...` binds via a `pattern` field; params via `parameter`'s
    # (H) `pattern` field (handled by _param_names' pattern unwrap). Shadowing is inert
    # (H) for Rust's `::`-path and macro sinks (a local can't shadow `std::fs::write`
    # (H) or `println!`), but wired for correctness / future value-level sinks.
    identifier_type=cs.TS_IDENTIFIER,
    declarator_type=cs.TS_RS_LET_DECLARATION,
    params_field=cs.TS_FIELD_PARAMETERS,
    block_scope_type=cs.TS_RS_BLOCK,
    extra_declarator_types=frozenset(),
    loop_declarator_types=frozenset(),
    statement_container_type=None,
    sinks_require_import=False,
    # (H) Rust `let` is declare-at-point and its scope starts AFTER the statement
    # (H) (`let x = x` reads the outer x), like Go: source-order, add name after.
    hoisted_declarations=False,
    decl_in_own_initializer=False,
    declaration_statement_type=None,
    macro_type=cs.TS_RS_MACRO_INVOCATION,
    # (H) Inert (no IO_MEMBER_READS for Rust): env access is a call (`std::env::var`).
    # (H) Filled with Rust's own field_expression / index_expression shape for
    # (H) correctness (not Java's field_access), so a future value-level sink is right.
    member_expression_type=cs.TS_RS_FIELD_EXPRESSION,
    subscript_type=cs.TS_RS_INDEX_EXPRESSION,
    object_field=cs.FIELD_VALUE,
    property_field=cs.RS_FIELD_FIELD,
    subscript_index_field=cs.RS_FIELD_INDEX,
    scope_separator=cs.TS_RS_TOKEN_SCOPE,
    # (H) `let x = env::var(..)` binds via the `pattern` field (a plain identifier for
    # (H) a simple binding), the init under `value`. Used by the flow walk.
    declarator_name_field=cs.TS_FIELD_PATTERN,
)

_CPP_DESCRIPTOR = LanguageDescriptor(
    call_type=cs.TS_CPP_CALL_EXPRESSION,
    string_type=cs.TS_CPP_STRING_LITERAL,
    string_content_type=cs.TS_CPP_STRING_CONTENT,
    keyword_arg_type=None,
    nested_scope_types=frozenset(
        {cs.TS_CPP_FUNCTION_DEFINITION, cs.TS_CPP_LAMBDA_EXPRESSION}
    ),
    # (H) C++ shadowing of an I/O sink name (a local/param named `getenv`/`printf`/
    # (H) `cout`) is pathological, so the shadow machinery is left inert: init_declarator
    # (H) has no name/left/pattern field (-> _declarator_names yields nothing) and
    # (H) function_definition has no direct `parameters` field (params nest under
    # (H) declarator), so no names are collected. Plain-name matching is sound here.
    identifier_type=cs.TS_CPP_IDENTIFIER,
    declarator_type=cs.TS_CPP_INIT_DECLARATOR,
    params_field=cs.KEY_PARAMETERS,
    block_scope_type=cs.TS_CPP_COMPOUND_STATEMENT,
    extra_declarator_types=frozenset(),
    loop_declarator_types=frozenset(),
    statement_container_type=None,
    sinks_require_import=False,
    # (H) C++ is declare-at-point; sinks are shadow-inert (above) so the flags below
    # (H) never actually suppress anything.
    hoisted_declarations=False,
    decl_in_own_initializer=False,
    declaration_statement_type=None,
    macro_type=None,
    # (H) Inert (no IO_MEMBER_READS for C++): env access is a call (`std::getenv`).
    # (H) Wired with C++'s own field_expression / subscript_expression shape.
    member_expression_type=cs.TS_CPP_FIELD_EXPRESSION,
    subscript_type=cs.TS_CPP_SUBSCRIPT_EXPRESSION,
    object_field=cs.CPP_FIELD_ARGUMENT,
    property_field=cs.CPP_FIELD_FIELD,
    subscript_index_field=cs.CPP_FIELD_INDICES,
    # (H) `std::cout << x` / `std::cerr << x` write STDOUT via the `<<` operator;
    # (H) `in >> word` on a bound fstream handle reads its file (issue #714).
    stream_sink_type=cs.TS_CPP_BINARY_EXPRESSION,
    stream_sink_operator=cs.CPP_OP_LEFT_SHIFT,
    stream_extract_operator=cs.CPP_OP_RIGHT_SHIFT,
    # (H) `int x = getenv(...)` binds via the `declarator` field (a nested
    # (H) pointer/reference declarator unwrapped to its identifier), not `name`.
    declarator_name_field=cs.FIELD_DECLARATOR,
)

# (H) Non-Python languages with a direct-sink descriptor. Python keeps its own
# (H) handle-aware walk; each new language lands one entry (plus registry rows).
LANGUAGE_DESCRIPTORS: dict[cs.SupportedLanguage, LanguageDescriptor] = {
    cs.SupportedLanguage.JS: _JS_TS_DESCRIPTOR,
    cs.SupportedLanguage.TS: _JS_TS_DESCRIPTOR,
    cs.SupportedLanguage.TSX: _JS_TS_DESCRIPTOR,
    cs.SupportedLanguage.GO: _GO_DESCRIPTOR,
    cs.SupportedLanguage.JAVA: _JAVA_DESCRIPTOR,
    cs.SupportedLanguage.RUST: _RUST_DESCRIPTOR,
    cs.SupportedLanguage.CPP: _CPP_DESCRIPTOR,
}
