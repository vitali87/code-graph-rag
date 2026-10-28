//! `TreeBridge`: the tree walks, on py-tree-sitter's own trees.
//!
//! py-tree-sitter keeps no public handle on its C tree, but its objects are
//! plain C structs (tree_sitter/binding/types.h in its source):
//!
//! ```c
//! typedef struct { PyObject_HEAD TSNode node; PyObject *children; PyObject *tree; } Node;
//! typedef struct { PyObject_HEAD TSTree *tree; PyObject *source; PyObject *language; } Tree;
//! ```
//!
//! So a `Node`'s `TSNode` is copied out of the object and walked with this
//! crate's tree-sitter runtime, whose node, cursor and tree code is the very
//! code compiled into py-tree-sitter (see Cargo.toml). The tree itself is
//! only read, never freed or edited, and the Python `Node` the caller passes
//! keeps it and its source alive for the whole call.
//!
//! Before trusting that layout the bridge checks what it can without reading
//! through a pointer: the py-tree-sitter version it was written against, the
//! object sizes the struct above implies, and on every call that the object
//! is exactly a `tree_sitter.Node`, its `tree` is exactly a `tree_sitter.Tree`
//! and both name the same `TSTree`. `describe` then lets the caller compare
//! every node of a parsed tree as both runtimes see it before using the
//! bridge at all (`codebase_rag/parsers/native_trees.py`). Anything the
//! bridge cannot read exactly, such as a source that is not `bytes`, raises
//! `NotBorrowed` and the caller runs the Python twin.

use std::collections::{HashMap, HashSet};
use std::mem::size_of;

use pyo3::exceptions::{PyKeyError, PyRuntimeError, PyTypeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict, PyString, PyType};
use tree_sitter::{ffi, Node};

use super::interface::{interface_fingerprint, Flavor, InterfaceRules, LanguageRules, ScanRules};
use super::scopes::{inner_scope_only_names, scope_facts, ScopeRules};
use super::{anchor_hash, ast_fingerprint, kind, NodeMap, NodeTypes, Rules, Source, DIGEST_SIZE};

/// The py-tree-sitter release whose object layout and C runtime this was
/// checked against; any other turns the bridge off.
const PY_TREE_SITTER: &str = "0.26.0";
/// The `tree-sitter` crate release whose tree-walking C sources were found
/// identical to that py-tree-sitter's. Cargo.toml pins exactly this one, and
/// test_native_trees.py holds both pins to this pair (AGENTS.md says how to
/// move it).
const TREE_SITTER_CRATE: &str = "0.26.8";
const POINTER: usize = size_of::<usize>();

pyo3::create_exception!(
    cgr_graph_rs,
    NotBorrowed,
    PyTypeError,
    "The bridge cannot read this node exactly; run the Python twin."
);

/// One node as `describe` reports it: its symbol, type, named flag, byte
/// range, start and end points, child count and field name.
type Described = (u16, String, bool, usize, usize, (usize, usize), (usize, usize), usize, Option<String>);

/// `scope_facts` as Python receives it: parameters, plain names,
/// (name, import paths) pairs, declared names and `nonlocal` names.
type NativeScopeFacts = (Vec<String>, Vec<String>, Vec<(String, Vec<String>)>, Vec<String>, Vec<String>);

#[pyclass(frozen, module = "cgr_graph_rs")]
pub struct TreeBridge {
    node_type: Py<PyType>,
    tree_type: Py<PyType>,
    /// `sizeof(PyObject)`: where each struct's own fields start.
    header: usize,
    rules: Rules,
    scope_rules: ScopeRules,
    interface_rules: InterfaceRules,
}

/// Reads a `T` at `offset` bytes into a Python object.
///
/// # Safety
///
/// `base + offset .. + size_of::<T>()` must lie inside the object.
unsafe fn field<T: Copy>(base: *const u8, offset: usize) -> T {
    std::ptr::read_unaligned(base.add(offset).cast::<T>())
}

impl TreeBridge {
    /// Runs `f` on the tree node `node` holds and its tree's source.
    fn with_node<R>(&self, node: &Bound<'_, PyAny>, f: impl FnOnce(Node, Source) -> R) -> PyResult<R> {
        let py = node.py();
        if !node.get_type().is(self.node_type.bind(py)) {
            return Err(NotBorrowed::new_err("not a tree_sitter.Node"));
        }
        let base = node.as_ptr().cast::<u8>().cast_const();
        // SAFETY: `node` is exactly a tree_sitter.Node, whose size `new`
        // matched to the layout these offsets come from.
        let (raw, tree_ptr) = unsafe {
            (
                field::<ffi::TSNode>(base, self.header),
                field::<*mut pyo3::ffi::PyObject>(base, self.header + size_of::<ffi::TSNode>() + POINTER),
            )
        };
        if raw.id.is_null() || tree_ptr.is_null() {
            return Err(NotBorrowed::new_err("a node without a tree"));
        }
        // SAFETY: a non-null object pointer the node holds a reference to.
        let tree = unsafe { Bound::from_borrowed_ptr(py, tree_ptr) };
        if !tree.get_type().is(self.tree_type.bind(py)) {
            return Err(NotBorrowed::new_err("not a tree_sitter.Tree"));
        }
        let tree_base = tree.as_ptr().cast::<u8>().cast_const();
        // SAFETY: as above, for the Tree layout.
        let (ts_tree, source_ptr) = unsafe {
            (
                field::<*const ffi::TSTree>(tree_base, self.header),
                field::<*mut pyo3::ffi::PyObject>(tree_base, self.header + POINTER),
            )
        };
        if raw.tree != ts_tree {
            return Err(NotBorrowed::new_err("the node and its tree disagree"));
        }
        let source = if source_ptr.is_null() {
            None
        } else {
            // SAFETY: a non-null object pointer the tree holds a reference to.
            Some(unsafe { Bound::from_borrowed_ptr(py, source_ptr) })
        };
        let bytes = match &source {
            Some(source) if !source.is_none() => Some(
                source
                    .cast::<PyBytes>()
                    .map_err(|_| NotBorrowed::new_err("a source that is not bytes"))?
                    .as_bytes(),
            ),
            _ => None,
        };
        // SAFETY: a live node of a live tree, laid out as this runtime's.
        let ts_node = unsafe { Node::from_raw(raw) };
        Ok(f(ts_node, Source { bytes }))
    }

    /// Runs `f` on the tree node `node` holds and its tree's source bytes;
    /// a tree without them has no names to read.
    fn with_source<R>(&self, node: &Bound<'_, PyAny>, f: impl FnOnce(Node, &[u8]) -> R) -> PyResult<R> {
        self.with_node(node, |node, source| source.bytes.map(|bytes| f(node, bytes)))?
            .ok_or_else(|| NotBorrowed::new_err("a tree without its source"))
    }
}

#[pymethods]
impl TreeBridge {
    /// The py-tree-sitter release the bridge reads.
    #[classattr]
    #[pyo3(name = "PY_TREE_SITTER")]
    fn py_tree_sitter() -> &'static str {
        PY_TREE_SITTER
    }

    /// The tree-sitter crate release checked to walk that release's trees.
    #[classattr]
    #[pyo3(name = "TREE_SITTER_CRATE")]
    fn tree_sitter_crate() -> &'static str {
        TREE_SITTER_CRATE
    }

    /// A bridge applying `rules`, the Python twins' constants by name; raises
    /// when this py-tree-sitter is not laid out as the bridge reads it.
    #[new]
    fn new(py: Python<'_>, rules: &Bound<'_, PyDict>) -> PyResult<Self> {
        let installed: String = py
            .import("importlib.metadata")?
            .call_method1("version", ("tree-sitter",))?
            .extract()?;
        if installed != PY_TREE_SITTER {
            return Err(PyRuntimeError::new_err(format!(
                "py-tree-sitter {installed}, not {PY_TREE_SITTER}"
            )));
        }
        let module = py.import("tree_sitter")?;
        let node_type = module.getattr("Node")?.cast_into::<PyType>()?;
        let tree_type = module.getattr("Tree")?.cast_into::<PyType>()?;
        let basicsize = |t: &Bound<'_, PyAny>| -> PyResult<usize> { t.getattr("__basicsize__")?.extract() };
        let header = basicsize(py.get_type::<pyo3::types::PyAny>().as_any())?;
        let node_size = header + size_of::<ffi::TSNode>() + 2 * POINTER;
        let tree_size = header + 3 * POINTER;
        if basicsize(node_type.as_any())? != node_size || basicsize(tree_type.as_any())? != tree_size {
            return Err(PyRuntimeError::new_err("py-tree-sitter objects are not laid out as expected"));
        }
        Ok(TreeBridge {
            node_type: node_type.unbind(),
            tree_type: tree_type.unbind(),
            header,
            rules: rules_from(rules)?,
            scope_rules: scope_rules_from(&item(rules, "scopes")?.cast_into::<PyDict>()?)?,
            interface_rules: interface_rules_from(&item(rules, "interface")?.cast_into::<PyDict>()?)?,
        })
    }

    /// `anchor_hash(definition, decorators)`.
    fn anchor_hash(&self, definition: &Bound<'_, PyAny>, decorators: &Bound<'_, PyAny>) -> PyResult<String> {
        let decorators: Vec<String> = decorators
            .extract()
            .map_err(|_| NotBorrowed::new_err("decorators that are not a sequence of str"))?;
        self.with_node(definition, |node, source| anchor_hash(node, &decorators, source, &self.rules))
    }

    /// `compute_ast_fingerprint(func_node)` as `(fingerprint, node_count,
    /// branch_fingerprints)`, or None for a bodiless node.
    fn ast_fingerprint(&self, func_node: &Bound<'_, PyAny>) -> PyResult<Option<(String, u64, Vec<String>)>> {
        self.with_node(func_node, |node, _| {
            ast_fingerprint(node, &self.rules)
                .map(|fp| (fp.fingerprint, fp.node_count, fp.branch_fingerprints))
        })
    }

    /// `ScopeFacts` of a Python scope: what `_parameter_names`,
    /// `_bindings_in` and `_declared_non_local` find there.
    fn scope_facts(&self, scope: &Bound<'_, PyAny>) -> PyResult<NativeScopeFacts> {
        self.with_source(scope, |node, source| {
            let facts = scope_facts(node, source, &self.scope_rules);
            (facts.params, facts.plain, facts.conditional, facts.declared, facts.nonlocals)
        })
    }

    /// `_inner_scope_only_names(caller)`, sorted.
    fn inner_scope_only_names(&self, caller: &Bound<'_, PyAny>) -> PyResult<Vec<String>> {
        self.with_source(caller, |node, source| inner_scope_only_names(node, source, &self.scope_rules))
    }

    /// `interface_fingerprint(root_node, language)`, or None for a language
    /// without rules.
    fn interface_fingerprint(&self, root_node: &Bound<'_, PyAny>, language: &str) -> PyResult<Option<String>> {
        let Some(language) = self.interface_rules.languages.get(language) else {
            return Ok(None);
        };
        self.with_source(root_node, |node, source| {
            Some(interface_fingerprint(node, source, language, &self.interface_rules, &self.scope_rules))
        })
    }

    /// Every node under `node` in a cursor's pre-order, as this runtime reads
    /// it, for comparing with what py-tree-sitter reports.
    fn describe(&self, node: &Bound<'_, PyAny>) -> PyResult<Vec<Described>> {
        self.with_node(node, |root, _| {
            let mut out = Vec::new();
            let mut cursor = root.walk();
            loop {
                let n = cursor.node();
                let (start, end) = (n.start_position(), n.end_position());
                out.push((
                    n.kind_id(),
                    String::from_utf8_lossy(kind(n)).into_owned(),
                    n.is_named(),
                    n.start_byte(),
                    n.end_byte(),
                    (start.row, start.column),
                    (end.row, end.column),
                    n.child_count(),
                    cursor.field_name().map(str::to_owned),
                ));
                if cursor.goto_first_child() {
                    continue;
                }
                while !cursor.goto_next_sibling() {
                    if !cursor.goto_parent() {
                        return out;
                    }
                }
            }
        })
    }
}

// ------------------------------------------------------------------ rules

fn item<'py>(rules: &Bound<'py, PyDict>, key: &str) -> PyResult<Bound<'py, PyAny>> {
    rules.get_item(key)?.ok_or_else(|| PyKeyError::new_err(key.to_owned()))
}

/// A str rule as UTF-8, or a bytes rule as it is.
fn as_bytes(value: &Bound<'_, PyAny>) -> PyResult<Vec<u8>> {
    if let Ok(text) = value.cast::<PyString>() {
        return Ok(text.to_str()?.as_bytes().to_vec());
    }
    Ok(value.cast::<PyBytes>()?.as_bytes().to_vec())
}

fn text(rules: &Bound<'_, PyDict>, key: &str) -> PyResult<Vec<u8>> {
    as_bytes(&item(rules, key)?)
}

fn list(rules: &Bound<'_, PyDict>, key: &str) -> PyResult<Vec<Vec<u8>>> {
    item(rules, key)?.try_iter()?.map(|value| as_bytes(&value?)).collect()
}

fn set(rules: &Bound<'_, PyDict>, key: &str) -> PyResult<NodeTypes> {
    Ok(list(rules, key)?.into_iter().collect())
}

fn rules_from(rules: &Bound<'_, PyDict>) -> PyResult<Rules> {
    let digest_size: usize = item(rules, "digest_size")?.extract()?;
    if digest_size != DIGEST_SIZE {
        return Err(PyValueError::new_err(format!("digest_size {digest_size}, not {DIGEST_SIZE}")));
    }
    Ok(Rules {
        anchor_version: item(rules, "anchor_version")?.extract()?,
        comment: text(rules, "comment")?,
        punct: set(rules, "punct")?,
        id_token: text(rules, "id_token")?,
        lit_token: text(rules, "lit_token")?,
        id_substring: text(rules, "id_substring")?,
        id_extras: set(rules, "id_extras")?,
        lit_substrings: list(rules, "lit_substrings")?,
        lit_extras: set(rules, "lit_extras")?,
        block_substrings: list(rules, "block_substrings")?,
        block_extras: set(rules, "block_extras")?,
        min_branch_nodes: item(rules, "min_branch_nodes")?.extract()?,
        wrapped_def_substring: text(rules, "wrapped_def_substring")?,
        expression_body_types: set(rules, "expression_body_types")?,
        self_body_types: set(rules, "self_body_types")?,
        hash_open: text(rules, "hash_open")?,
        hash_close: text(rules, "hash_close")?,
        body_field: text(rules, "body_field")?,
        dart_signature_types: set(rules, "dart_signature_types")?,
        dart_signature_wrappers: set(rules, "dart_signature_wrappers")?,
        dart_function_body: text(rules, "dart_function_body")?,
    })
}

fn str_rule(rules: &Bound<'_, PyDict>, key: &str) -> PyResult<String> {
    item(rules, key)?.extract()
}

fn scope_rules_from(rules: &Bound<'_, PyDict>) -> PyResult<ScopeRules> {
    Ok(ScopeRules {
        identifier: text(rules, "identifier")?,
        nested_scopes: set(rules, "nested_scopes")?,
        left_binders: set(rules, "left_binders")?,
        name_binders: set(rules, "name_binders")?,
        imports: set(rules, "imports")?,
        patterns: set(rules, "patterns")?,
        declarations: set(rules, "declarations")?,
        comprehensions: set(rules, "comprehensions")?,
        nonlocal_statement: text(rules, "nonlocal_statement")?,
        with_statement: text(rules, "with_statement")?,
        except_clause: text(rules, "except_clause")?,
        case_pattern: text(rules, "case_pattern")?,
        as_pattern: text(rules, "as_pattern")?,
        aliased_import: text(rules, "aliased_import")?,
        dotted_name: text(rules, "dotted_name")?,
        wildcard: str_rule(rules, "wildcard")?,
        function_definition: text(rules, "function_definition")?,
        class_definition: text(rules, "class_definition")?,
        lambda: text(rules, "lambda_expression")?,
        for_in_clause: text(rules, "for_in_clause")?,
        attribute: text(rules, "attribute")?,
        keyword_argument: text(rules, "keyword_argument")?,
        separator: str_rule(rules, "separator")?,
        left_field: text(rules, "left_field")?,
        name_field: text(rules, "name_field")?,
        value_field: text(rules, "value_field")?,
        alias_field: text(rules, "alias_field")?,
        module_field: text(rules, "module_field")?,
        parameters_field: text(rules, "parameters_field")?,
        body_field: text(rules, "body_field")?,
        attribute_field: text(rules, "attribute_field")?,
    })
}

fn dict<'py>(rules: &Bound<'py, PyDict>, key: &str) -> PyResult<Bound<'py, PyDict>> {
    Ok(item(rules, key)?.cast_into::<PyDict>()?)
}

fn strings(rules: &Bound<'_, PyDict>, key: &str) -> PyResult<HashSet<String>> {
    item(rules, key)?.try_iter()?.map(|value| value?.extract()).collect()
}

/// The tokens' length prefix the Rust walk writes: 4 bytes, big-endian.
const LENGTH_BYTES: usize = 4;
const BYTE_ORDER: &str = "big";

fn flavor_from(flavor: &str) -> PyResult<Flavor> {
    match flavor {
        "python" => Ok(Flavor::Python),
        "js" => Ok(Flavor::Js),
        "other" => Ok(Flavor::Other),
        _ => Err(PyValueError::new_err(format!("interface flavor {flavor}"))),
    }
}

fn language_rules_from(rules: &Bound<'_, PyDict>) -> PyResult<LanguageRules> {
    Ok(LanguageRules {
        flavor: flavor_from(&str_rule(rules, "flavor")?)?,
        functions: set(rules, "functions")?,
        blocks: set(rules, "blocks")?,
        definitions: set(rules, "definitions")?,
        blockers: set(rules, "blockers")?,
        kept_names: strings(rules, "kept_names")?,
        calls: set(rules, "calls")?,
        returns: set(rules, "returns")?,
    })
}

fn scan_rules_from(rules: &Bound<'_, PyDict>) -> PyResult<ScanRules> {
    Ok(ScanRules {
        call: text(rules, "call")?,
        identifier: text(rules, "identifier")?,
        closures: set(rules, "closures")?,
        opaque: set(rules, "opaque")?,
    })
}

fn interface_rules_from(rules: &Bound<'_, PyDict>) -> PyResult<InterfaceRules> {
    let length_bytes: usize = item(rules, "length_bytes")?.extract()?;
    let byte_order = str_rule(rules, "byte_order")?;
    if length_bytes != LENGTH_BYTES || byte_order != BYTE_ORDER {
        return Err(PyValueError::new_err(format!(
            "a {length_bytes}-byte {byte_order}-endian length prefix, not {LENGTH_BYTES}-byte {BYTE_ORDER}-endian"
        )));
    }
    let mut languages = HashMap::new();
    for (name, language) in dict(rules, "languages")?.iter() {
        languages.insert(name.extract()?, language_rules_from(&language.cast_into::<PyDict>()?)?);
    }
    let mut js_binder_fields = NodeMap::default();
    for (node_type, fields) in dict(rules, "js_binder_fields")?.iter() {
        let fields = fields.try_iter()?.map(|field| as_bytes(&field?)).collect::<PyResult<Vec<_>>>()?;
        js_binder_fields.insert(as_bytes(&node_type)?, fields);
    }
    Ok(InterfaceRules {
        languages,
        version: text(rules, "version")?,
        open: text(rules, "open")?,
        close: text(rules, "close")?,
        leaf: text(rules, "leaf")?,
        missing: text(rules, "missing")?,
        elided_body: text(rules, "elided_body")?,
        returned: text(rules, "returned")?,
        call: text(rules, "call")?,
        argument: text(rules, "argument")?,
        local: text(rules, "local")?,
        comment: text(rules, "comment")?,
        dunder: str_rule(rules, "dunder")?,
        body_field: text(rules, "body_field")?,
        name_field: str_rule(rules, "name_field")?,
        left_field: text(rules, "left_field")?,
        arguments_field: text(rules, "arguments_field")?,
        value_field: text(rules, "value_field")?,
        key_field: text(rules, "key_field")?,
        function_field: text(rules, "function_field")?,
        property_field: text(rules, "property_field")?,
        object_field: text(rules, "object_field")?,
        parameters_field: text(rules, "parameters_field")?,
        parameter_field: text(rules, "parameter_field")?,
        pattern_field: text(rules, "pattern_field")?,
        alias_field: text(rules, "alias_field")?,
        callee_fields: list(rules, "callee_fields")?,
        identifier: text(rules, "identifier")?,
        keyword_argument: text(rules, "keyword_argument")?,
        name_arguments: set(rules, "name_arguments")?,
        cast_wrappers: set(rules, "cast_wrappers")?,
        call_expression: text(rules, "call_expression")?,
        member_expression: text(rules, "member_expression")?,
        prototype_methods: strings(rules, "prototype_methods")?,
        js_binder_fields,
        js_bound_names: set(rules, "js_bound_names")?,
        js_return: text(rules, "js_return")?,
        js_pair: text(rules, "js_pair")?,
        js_computed_property_name: text(rules, "js_computed_property_name")?,
        js_require: str_rule(rules, "js_require")?,
        js_typed_parameters: set(rules, "js_typed_parameters")?,
        py_identifier: text(rules, "py_identifier")?,
        py_call: text(rules, "py_call")?,
        py_attribute: text(rules, "py_attribute")?,
        py_return: text(rules, "py_return")?,
        py_read_returns: set(rules, "py_read_returns")?,
        receivers: strings(rules, "receivers")?,
        py_target_holders: set(rules, "py_target_holders")?,
        py_target_wrappers: set(rules, "py_target_wrappers")?,
        py_member_targets: set(rules, "py_member_targets")?,
        py_caller_scope: text(rules, "py_caller_scope")?,
        py_named_parameters: set(rules, "py_named_parameters")?,
        py_typed_parameter: text(rules, "py_typed_parameter")?,
        py_scope_boundaries: set(rules, "py_scope_boundaries")?,
        py_decorated_definition: text(rules, "py_decorated_definition")?,
        py_decorated_definitions: set(rules, "py_decorated_definitions")?,
        py_shadow_binders: set(rules, "py_shadow_binders")?,
        py_as_pattern_target: text(rules, "py_as_pattern_target")?,
        py_imports: set(rules, "py_imports")?,
        py_global: text(rules, "py_global")?,
        py_case_pattern: text(rules, "py_case_pattern")?,
        py_dotted_name: text(rules, "py_dotted_name")?,
        py_aliased_import: text(rules, "py_aliased_import")?,
        python_scan: scan_rules_from(&dict(rules, "python_scan")?)?,
        js_scan: scan_rules_from(&dict(rules, "js_scan")?)?,
    })
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<TreeBridge>()?;
    m.add("NotBorrowed", m.py().get_type::<NotBorrowed>())
}
