# (H) Rust tree-sitter node types and resolution constants.

from .ast_java import TS_GENERIC_TYPE
from .ast_nodes import TS_IDENTIFIER, TS_SCOPED_IDENTIFIER, TS_TYPE_IDENTIFIER
from .ast_scala import TS_GENERIC_FUNCTION
from .core import KEYWORD_SELF, KEYWORD_SUPER

# (H) Tree-sitter Rust node types
TS_RS_SCOPED_TYPE_IDENTIFIER = "scoped_type_identifier"
TS_RS_PRIMITIVE_TYPE = "primitive_type"
TS_RS_USE_AS_CLAUSE = "use_as_clause"
TS_RS_USE_WILDCARD = "use_wildcard"
TS_RS_USE_LIST = "use_list"
TS_RS_SCOPED_USE_LIST = "scoped_use_list"
TS_RS_SOURCE_FILE = "source_file"
TS_RS_MOD_ITEM = "mod_item"
TS_RS_CRATE = "crate"
TS_RS_KEYWORD_AS = "as"
TS_RS_STRUCT_ITEM = "struct_item"
TS_RS_ENUM_ITEM = "enum_item"
TS_RS_TRAIT_ITEM = "trait_item"
TS_RS_TYPE_ITEM = "type_item"
TS_RS_FUNCTION_ITEM = "function_item"
TS_RS_IMPL_ITEM = "impl_item"
TS_RS_FUNCTION_SIGNATURE_ITEM = "function_signature_item"
TS_RS_CLOSURE_EXPRESSION = "closure_expression"
TS_RS_UNION_ITEM = "union_item"
TS_RS_USE_DECLARATION = "use_declaration"
TS_RS_EXTERN_CRATE_DECLARATION = "extern_crate_declaration"
TS_RS_CALL_EXPRESSION = "call_expression"
TS_RS_MACRO_INVOCATION = "macro_invocation"
TS_RS_MACRO_DEFINITION = "macro_definition"
RS_MACRO_EXPORT_ATTR = "macro_export"
TS_RS_LINE_COMMENT = "line_comment"
TS_RS_BLOCK_COMMENT = "block_comment"
RS_COMMENT_TYPES = (TS_RS_LINE_COMMENT, TS_RS_BLOCK_COMMENT)
TS_RS_ATTRIBUTE_ITEM = "attribute_item"
TS_RS_INNER_ATTRIBUTE_ITEM = "inner_attribute_item"

# (H) Rust I/O direct-sink walk node types (issue #714). call_expression keeps a
# (H) `function` field (a scoped_identifier like `std::fs::write`), so call_name works
# (H) unchanged; `macro_invocation` (`println!`) needs its own handling via macro_type.
# (H) A string_literal wraps a `string_content`; `block` is the fn-body lexical scope.
TS_RS_STRING_LITERAL = "string_literal"
TS_RS_STRING_CONTENT = "string_content"
TS_RS_BLOCK = "block"
TS_RS_FIELD_MACRO = "macro"
# (H) A macro body is a flat `token_tree` of raw tokens (`::` and `(...)` included),
# (H) not a parse tree, so a call inside `println!(..)` has no call_expression node.
TS_RS_TOKEN_TREE = "token_tree"
TS_RS_TOKEN_SCOPE = "::"
# (H) `s.field` is a field_expression (value/field); `arr[i]` an index_expression
# (H) (unnamed children in this grammar). Inert for I/O (Rust env access is a call),
# (H) wired for correctness / future value-level sinks.
TS_RS_INDEX_EXPRESSION = "index_expression"
RS_FIELD_FIELD = "field"
RS_FIELD_INDEX = "index"

# (H) Rust node types for local-variable type inference (receiver-dispatch)
TS_RS_LET_DECLARATION = "let_declaration"
TS_RS_PARAMETER = "parameter"
TS_RS_SELF_PARAMETER = "self_parameter"
TS_RS_STRUCT_EXPRESSION = "struct_expression"
TS_RS_FIELD_DECLARATION_LIST = "field_declaration_list"
TS_RS_FIELD_DECLARATION = "field_declaration"
TS_RS_FIELD_IDENTIFIER = "field_identifier"
TS_RS_MATCH_EXPRESSION = "match_expression"
TS_RS_MATCH_ARM = "match_arm"
# (H) A Rust call node whose callee is descended for chain flattening: a plain call
# (H) or a turbofish generic_function (`f::<T>()`).
RS_CALL_OR_GENERIC_FN = (TS_RS_CALL_EXPRESSION, TS_GENERIC_FUNCTION)
TS_RS_TUPLE_STRUCT_PATTERN = "tuple_struct_pattern"
TS_RS_TYPE_ARGUMENTS = "type_arguments"
TS_RS_TRY_EXPRESSION = "try_expression"
TS_RS_FIELD_EXPRESSION = "field_expression"
TS_RS_FIELD_PATH = "path"
TS_RS_TOKEN_DOT = "."
# (H) Nodes that can be a receiver token preceding `.method` in a macro token
# (H) stream: a plain identifier or the `self` keyword.
# (H) A receiver/chain base that is a plain identifier or the `self` keyword (used
# (H) both for macro-token receiver reconstruction and value-chain base flattening).
RS_IDENT_OR_SELF = (TS_IDENTIFIER, KEYWORD_SELF)
RS_MACRO_RECEIVER_TYPES = RS_IDENT_OR_SELF
# (H) Rust `Self` return type resolves to the enclosing impl target.
RS_SELF_TYPE = "Self"
# (H) Transparent smart pointers that auto-deref (Rust deref coercion) to their
# (H) inner type: a method call on the pointer dispatches to the inner type's method,
# (H) so strip them from any type name (receiver OR return) to reach the real type.
RS_DEREF_WRAPPERS = frozenset({"Arc", "Rc", "Box", "Pin"})
# (H) Guard containers that do NOT deref-coerce: the inner value is only reachable
# (H) through a lock/borrow guard accessor. Stripped to the inner type ONLY in field
# (H) extraction (where the field is virtually always accessed via a lock chain, e.g.
# (H) `self.shared.state.lock().unwrap()`); a bare local/param/return of a guard type
# (H) is preserved so a direct wrapper-method call (`m.is_poisoned()`) is not
# (H) mis-resolved to an inner-type method.
RS_GUARD_WRAPPERS = frozenset({"Mutex", "RwLock", "RefCell", "Cell"})
# (H) Result<T>/Option<T>: stripped to their inner T only for a RETURN type (the
# (H) value a `?`/`.unwrap()` yields). NOT stripped for a receiver type, where a
# (H) method call `opt.map(..)` dispatches to Option itself.
RS_RESULT_WRAPPERS = frozenset({"Result", "Option"})
# (H) Full strip set for return types (deref pointers + Result/Option unwrap).
RS_RETURN_STRIP_WRAPPERS = RS_DEREF_WRAPPERS | RS_RESULT_WRAPPERS
TS_RS_REFERENCE_TYPE = "reference_type"
TS_RS_POINTER_TYPE = "pointer_type"
# (H) Trait-object and impl-Trait wrappers: `dyn Svc` / `impl Svc` /
# (H) `dyn Svc + Send`. The trait IS the value's static type (a method call on
# (H) the value dispatches through the trait), so type walkers descend through
# (H) these to the trait name, mirroring the Java interface-receiver design.
TS_RS_DYNAMIC_TYPE = "dynamic_type"
TS_RS_ABSTRACT_TYPE = "abstract_type"
TS_RS_BOUNDED_TYPE = "bounded_type"
# (H) A parenthesized type (`&(dyn Svc + Send)`) parses as tuple_type; only a
# (H) single-element one is grouping (a real tuple has no single bare type).
TS_RS_TUPLE_TYPE = "tuple_type"
# (H) Node types that can stand for a Rust return/field type. Reference/pointer
# (H) wrappers (`&Frame`, `*const T`) are included so a generic inner argument
# (H) (`Result<&Frame>`) and a bare `-> &Frame` return descend to the referent;
# (H) dyn/impl/bounded wrappers so a trait-object type descends to its trait.
RS_RETURN_TYPE_NODE_TYPES = (
    TS_TYPE_IDENTIFIER,
    TS_RS_PRIMITIVE_TYPE,
    TS_GENERIC_TYPE,
    TS_RS_SCOPED_TYPE_IDENTIFIER,
    TS_RS_REFERENCE_TYPE,
    TS_RS_POINTER_TYPE,
    TS_RS_DYNAMIC_TYPE,
    TS_RS_ABSTRACT_TYPE,
    TS_RS_BOUNDED_TYPE,
    TS_RS_TUPLE_TYPE,
)
# (H) Wrapper-passthrough methods: they return the receiver's own (inner) type, so
# (H) a call-bound local keeps its type across them (`Type::mk().unwrap().m()`).
RS_IDENTITY_METHODS = frozenset(
    {
        "unwrap",
        "expect",
        "clone",
        "unwrap_or_default",
        "to_owned",
        "borrow",
        "borrow_mut",
        "as_ref",
        "as_mut",
        "as_deref",
        "as_deref_mut",
    }
)
# (H) Guard accessors: called on a guard container (Mutex/RwLock/RefCell) to obtain a
# (H) guard that derefs to the inner type. In a receiver chain, one of these
# (H) immediately after a guard-wrapped field unwraps the wrapper to its inner type
# (H) (recorded in class_field_guard_inner) -- the only sound unwrap point, since
# (H) guard containers do not deref-coerce.
RS_GUARD_ACCESSORS = frozenset(
    {"lock", "read", "write", "try_lock", "borrow", "borrow_mut"}
)

# (H) Rust identifier tuples
RS_IDENTIFIER_TYPES = (TS_IDENTIFIER, TS_TYPE_IDENTIFIER)
RS_SCOPED_TYPES = (TS_SCOPED_IDENTIFIER, TS_RS_SCOPED_TYPE_IDENTIFIER)
RS_PATH_KEYWORDS = (TS_RS_CRATE, KEYWORD_SUPER, KEYWORD_SELF)

# (H) Delimiter tokens for Rust use lists
RS_USE_LIST_DELIMITERS = frozenset({"{", "}", ","})

# (H) Rust encoding
RS_ENCODING_UTF8 = "utf8"

# (H) Rust wildcard prefix
RS_WILDCARD_PREFIX = "*"

# (H) Rust field names
RS_FIELD_ARGUMENT = "argument"
