# Rust tree-sitter node types and resolution constants.

from .ast_java import TS_GENERIC_TYPE
from .ast_nodes import TS_IDENTIFIER, TS_SCOPED_IDENTIFIER, TS_TYPE_IDENTIFIER
from .ast_scala import TS_GENERIC_FUNCTION
from .core import KEYWORD_SELF, KEYWORD_SUPER
from .graph import NodeLabel

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
TS_RS_CONST_ITEM = "const_item"
TS_RS_STATIC_ITEM = "static_item"
TS_RS_IMPL_ITEM = "impl_item"
TS_RS_FUNCTION_SIGNATURE_ITEM = "function_signature_item"
TS_RS_CLOSURE_EXPRESSION = "closure_expression"
TS_RS_UNION_ITEM = "union_item"
TS_RS_TYPE_PARAMETERS = "type_parameters"
TS_RS_TYPE_PARAMETER = "type_parameter"
TS_RS_WHERE_CLAUSE = "where_clause"
TS_RS_WHERE_PREDICATE = "where_predicate"
# Items whose generic parameter lists and where clauses put trait bounds in
# scope for the calls beneath them.
RS_GENERIC_SCOPE_ITEMS = (TS_RS_FUNCTION_ITEM, TS_RS_IMPL_ITEM, TS_RS_TRAIT_ITEM)
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
# The `#[cfg(test)]` gate in whitespace-normalised form: attributes are
# token streams, so extracted text is compared after dropping ALL
# whitespace (`#[cfg( test )]` names the same gate). Recorded on a mod
# declaration's TARGET module and read by dead-code test detection
# (issue #1010).
RS_CFG_TEST_ATTRIBUTE = "#[cfg(test)]"

# Rust I/O direct-sink walk node types (issue #714). call_expression keeps a
# `function` field (a scoped_identifier like `std::fs::write`), so call_name works
# unchanged; `macro_invocation` (`println!`) needs its own handling via macro_type.
# A string_literal wraps a `string_content`; `block` is the fn-body lexical scope.
TS_RS_STRING_LITERAL = "string_literal"
TS_RS_STRING_CONTENT = "string_content"
TS_RS_BLOCK = "block"
TS_RS_FIELD_MACRO = "macro"
# A macro body is a flat `token_tree` of raw tokens, not a parse tree, so a
# call inside `println!(..)` has no call_expression node.
TS_RS_TOKEN_TREE = "token_tree"
TS_RS_TOKEN_SCOPE = "::"
# `s.field` is a field_expression; `arr[i]` an index_expression. Inert for I/O
# (Rust env access is a call), wired for correctness and future value sinks.
TS_RS_INDEX_EXPRESSION = "index_expression"
RS_FIELD_FIELD = "field"
RS_FIELD_INDEX = "index"

# Rust node types for local-variable type inference (receiver-dispatch)
TS_RS_LET_DECLARATION = "let_declaration"
TS_RS_PARAMETER = "parameter"
TS_RS_SELF_PARAMETER = "self_parameter"
TS_RS_REFERENCE_PATTERN = "reference_pattern"
TS_RS_REF_PATTERN = "ref_pattern"
TS_RS_MUT_PATTERN = "mut_pattern"
TS_RS_MUTABLE_SPECIFIER = "mutable_specifier"
TS_RS_STRUCT_EXPRESSION = "struct_expression"
TS_RS_FIELD_DECLARATION_LIST = "field_declaration_list"
TS_RS_FIELD_DECLARATION = "field_declaration"
TS_RS_FIELD_IDENTIFIER = "field_identifier"
TS_RS_MATCH_EXPRESSION = "match_expression"
TS_RS_MATCH_ARM = "match_arm"
TS_RS_IF_EXPRESSION = "if_expression"
# `&s` / `&mut s`: a borrow is value-preserving, unwrapped by the lean flow
# walk so the referent's taint carries through (issue #714).
TS_RS_REFERENCE_EXPRESSION = "reference_expression"
TS_RS_FOR_EXPRESSION = "for_expression"
TS_RS_WHILE_EXPRESSION = "while_expression"
TS_RS_LOOP_EXPRESSION = "loop_expression"
# A Rust call node whose callee is descended for chain flattening: a plain call
# or a turbofish generic_function (`f::<T>()`).
RS_CALL_OR_GENERIC_FN = (TS_RS_CALL_EXPRESSION, TS_GENERIC_FUNCTION)
TS_RS_TUPLE_STRUCT_PATTERN = "tuple_struct_pattern"
TS_RS_TYPE_ARGUMENTS = "type_arguments"
TS_RS_TRY_EXPRESSION = "try_expression"
TS_RS_FIELD_EXPRESSION = "field_expression"
# Result-unwrapping method names: `File::open(p)?` / `.unwrap()` / `.expect(..)`
# all yield the inner handle, so the I/O handle binder unwraps through them.
RS_RESULT_UNWRAP_METHODS = frozenset({"unwrap", "expect"})
TS_RS_FIELD_PATH = "path"
TS_RS_TOKEN_DOT = "."
# A receiver/chain base that is a plain identifier or the `self` keyword (used
# both for macro-token receiver reconstruction and value-chain base flattening).
RS_IDENT_OR_SELF = (TS_IDENTIFIER, KEYWORD_SELF)
RS_MACRO_RECEIVER_TYPES = RS_IDENT_OR_SELF
# Rust `Self` return type resolves to the enclosing impl target.
RS_SELF_TYPE = "Self"
# Transparent smart pointers that auto-deref to their inner type: a method call
# on the pointer dispatches to the inner type's method, so strip them from any
# type name (receiver OR return) to reach the real type.
RS_DEREF_WRAPPERS = frozenset({"Arc", "Rc", "Box", "Pin"})
# Guard containers that do NOT deref-coerce: the inner value is only reachable
# through a lock/borrow guard accessor. Stripped to the inner type ONLY in field
# extraction (the field is nearly always via a lock chain, e.g.
# `self.shared.state.lock().unwrap()`); a bare local/param/return guard type is
# preserved so a direct wrapper call (`m.is_poisoned()`) is not mis-resolved.
RS_GUARD_WRAPPERS = frozenset({"Mutex", "RwLock", "RefCell", "Cell"})
# Result<T>/Option<T>: stripped to their inner T only for a RETURN type (the
# value a `?`/`.unwrap()` yields). NOT stripped for a receiver type, where a
# method call `opt.map(..)` dispatches to Option itself.
RS_RESULT_WRAPPERS = frozenset({"Result", "Option"})
# Full strip set for return types (deref pointers + Result/Option unwrap).
RS_RETURN_STRIP_WRAPPERS = RS_DEREF_WRAPPERS | RS_RESULT_WRAPPERS
TS_RS_REFERENCE_TYPE = "reference_type"
TS_RS_POINTER_TYPE = "pointer_type"
# Trait-object and impl-Trait wrappers: `dyn Svc` / `impl Svc` / `dyn Svc + Send`.
# The trait IS the value's static type (a method call dispatches through it), so
# type walkers descend through these to the trait name, like the Java
# interface-receiver design.
TS_RS_DYNAMIC_TYPE = "dynamic_type"
TS_RS_ABSTRACT_TYPE = "abstract_type"
TS_RS_BOUNDED_TYPE = "bounded_type"
# A parenthesized type (`&(dyn Svc + Send)`) parses as tuple_type; only a
# single-element one is grouping (a real tuple has no single bare type).
TS_RS_TUPLE_TYPE = "tuple_type"
# Node types that can stand for a Rust return/field type. Reference/pointer
# wrappers (`&Frame`, `*const T`) let a generic inner argument (`Result<&Frame>`)
# and a bare `-> &Frame` descend to the referent; dyn/impl/bounded wrappers let a
# trait-object type descend to its trait.
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
# Wrapper-passthrough methods: they return the receiver's own (inner) type, so
# a call-bound local keeps its type across them (`Type::mk().unwrap().m()`).
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
# Guard accessors: called on a guard container (Mutex/RwLock/RefCell) to obtain a
# guard that derefs to the inner type. In a receiver chain, one immediately after
# a guard-wrapped field unwraps the wrapper to its inner type (recorded in
# class_field_guard_inner), the only sound unwrap point, since guard containers
# do not deref-coerce.
RS_GUARD_ACCESSORS = frozenset(
    {"lock", "read", "write", "try_lock", "borrow", "borrow_mut"}
)

RS_IDENTIFIER_TYPES = (TS_IDENTIFIER, TS_TYPE_IDENTIFIER)
RS_SCOPED_TYPES = (TS_SCOPED_IDENTIFIER, TS_RS_SCOPED_TYPE_IDENTIFIER)
RS_PATH_KEYWORDS = (TS_RS_CRATE, KEYWORD_SUPER, KEYWORD_SELF)

RS_USE_LIST_DELIMITERS = frozenset({"{", "}", ","})

RS_ENCODING_UTF8 = "utf8"

RS_WILDCARD_PREFIX = "*"
# Marks a `use path::{self}` entry, whose name the base path supplies rather
# than the source. Rust keeps types and values in separate namespaces while
# the import map holds one slot per name, so such a binding is WEAK: it never
# displaces a name another `use` in the same scope already claimed (#1054).
# No Rust identifier can contain the marker character.
RS_SELF_MODULE_PREFIX = "@"

RS_FIELD_ARGUMENT = "argument"

# Cargo target layout (issue #1007 crate-root discovery). Auto-target
# directories root every .rs file directly inside them; explicit target
# `path` overrides in the manifest sections below root the named file
# wherever it sits. Entry stems never root themselves as file targets.
RS_AUTO_TARGET_DIRS = frozenset({"examples", "tests", "benches"})
RS_BIN_DIR = "bin"
RS_BUILD_STEM = "build"
RS_ENTRY_STEMS = frozenset({"lib", "main", "mod"})
RS_MANIFEST_TARGET_SECTIONS = ("bin", "lib", "example", "test", "bench")
RS_MANIFEST_PATH_KEY = "path"
RS_MANIFEST_PACKAGE_KEY = "package"
RS_MANIFEST_BUILD_KEY = "build"
RS_MANIFEST_AUTOLIB_KEY = "autolib"
RS_MANIFEST_AUTOBINS_KEY = "autobins"
RS_MANIFEST_AUTOEXAMPLES_KEY = "autoexamples"
RS_MANIFEST_AUTOTESTS_KEY = "autotests"
RS_MANIFEST_AUTOBENCHES_KEY = "autobenches"
RS_MANIFEST_AUTO_KEYS = (
    RS_MANIFEST_AUTOLIB_KEY,
    RS_MANIFEST_AUTOBINS_KEY,
    RS_MANIFEST_AUTOEXAMPLES_KEY,
    RS_MANIFEST_AUTOTESTS_KEY,
    RS_MANIFEST_AUTOBENCHES_KEY,
)
RS_AUTO_DIR_KEYS = {
    "examples": RS_MANIFEST_AUTOEXAMPLES_KEY,
    "tests": RS_MANIFEST_AUTOTESTS_KEY,
    "benches": RS_MANIFEST_AUTOBENCHES_KEY,
}
# Candidate locations of a PATHLESS manifest target table, matched against
# the files that actually exist (cargo errors on an ambiguous pair, so only
# a single existing candidate resolves). [lib] is fixed at src/lib.rs; a bin
# whose name equals the package name may be src/main.rs.
RS_DEFAULT_LIB_PATH = "src/lib.rs"
RS_DEFAULT_MAIN_PATH = "src/main.rs"
RS_MANIFEST_BIN_SECTION = "bin"
RS_MANIFEST_KIND_DIRS = {
    "bin": "src/bin",
    "example": "examples",
    "test": "tests",
    "bench": "benches",
}
RS_MANIFEST_WORKSPACE_KEY = "workspace"
RS_MANIFEST_MEMBERS_KEY = "members"
RS_MANIFEST_NAME_KEY = "name"
RS_MANIFEST_LIB_SECTION = "lib"
RS_MANIFEST_DEP_SECTIONS = ("dependencies", "dev-dependencies", "build-dependencies")
RS_MANIFEST_TARGET_TABLE_KEY = "target"
# Crates shipped with the toolchain: external by construction, no
# manifest needed to know a use head naming one is outside the project.
RS_STDLIB_CRATES = frozenset({"std", "core", "alloc", "proc_macro"})

# Node labels a Rust qn segment carries when it is a TYPE the caller sits
# inside (an impl block), not an inline `mod`. An impl adds no module level,
# so `super::` inside a method counts from the file module's parent (#1093).
RS_TYPE_SCOPE_LABELS = frozenset(
    {
        NodeLabel.CLASS.value,
        NodeLabel.INTERFACE.value,
        NodeLabel.ENUM.value,
        NodeLabel.TYPE.value,
        NodeLabel.UNION.value,
    }
)

# Traits the Rust prelude puts in scope with no `use`: an impl naming one
# without importing it and without a same-named first-party declaration
# implements the standard trait, whose dispatch lives outside the graph
# (issue #1048). Marker traits with no methods are omitted as pointless.
RS_PRELUDE_TRAITS = frozenset(
    {
        "AsMut",
        "AsRef",
        "Clone",
        "Default",
        "DoubleEndedIterator",
        "Drop",
        "ExactSizeIterator",
        "Extend",
        "Fn",
        "FnMut",
        "FnOnce",
        "From",
        "FromIterator",
        "Into",
        "IntoIterator",
        "Iterator",
        "Ord",
        "PartialEq",
        "PartialOrd",
        "ToOwned",
        "ToString",
        "TryFrom",
        "TryInto",
    }
)

# Iterator-adaptor closure typing (issue #1045): a closure argument of one
# of these adaptors receives the sequence's element (by value or
# reference, indistinguishable for method binding), so its parameter can
# type from the iterated collection's element type.
RS_ITER_ADAPTORS = frozenset(
    {
        "map",
        "filter",
        "for_each",
        "inspect",
        "take_while",
        "skip_while",
        "filter_map",
        "find",
        "position",
        "any",
        "all",
    }
)
# Chain hops between the collection and the adaptor that preserve the
# element type. Element-changing adaptors (enumerate, zip, flat_map) are
# deliberately absent: crossing one loses the element.
RS_ITER_NEUTRAL_HOPS = frozenset(
    {"iter", "into_iter", "iter_mut", "by_ref", "rev", "cloned", "copied", "filter"}
)
# Sequence containers whose FIRST generic argument is the element type.
RS_ELEMENT_CONTAINERS = frozenset({"Vec", "VecDeque"})
RS_ITER_MAP = "map"
RS_ITER_COLLECT = "collect"
TS_RS_ARRAY_TYPE = "array_type"
RS_FIELD_ELEMENT = "element"
