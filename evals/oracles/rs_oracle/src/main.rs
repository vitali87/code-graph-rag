// Authoritative Rust structure oracle for the cgr eval harness.
//
// Parses every .rs file under a directory with `syn` (the de-facto standard Rust
// parser) and emits a JSON payload {nodes, edges}. Node "kind" fields use cgr's
// NodeLabel vocabulary and edges use cgr's RelationshipType vocabulary, so both
// join cgr's graph on (kind, file, line).
//
// Mapping (Rust item -> cgr NodeLabel):
//
//   struct      -> Class
//   enum        -> Enum
//   union       -> Union
//   trait       -> Interface  (its methods -> Method)
//   type alias  -> Type
//   fn          -> Function   (free fns, including those nested in fn bodies)
//   impl method -> Method
//
// Containment edges (matching how cgr models Rust containment):
//
//   DEFINES        : enclosing module -> item / nested module
//   DEFINES_METHOD : the method's owner type (or trait) -> Method
//
// cgr models a Rust module per file (keyed at line 0) plus a Module node per
// inline `mod` (keyed at its declaration line). An item inside `mod inner` is
// DEFINEd by the inner module; an impl method binds to its target type resolved
// within the impl's enclosing module path (falling back to ancestor modules).
//
// The node walk uses `syn::visit::Visit` so function-local definitions and
// closures are captured too; edges use an explicit item recursion that tracks
// the enclosing module, which is what carries containment.
//
// Run: cargo run --release -- <dir>

use std::collections::HashMap;
use std::env;
use std::fs;
use std::path::Path;
use syn::spanned::Spanned;
use syn::visit::Visit;

const IGNORED_DIRS: [&str; 4] = [".git", "target", "vendor", "node_modules"];

const KIND_CLASS: &str = "Class";
const KIND_ENUM: &str = "Enum";
const KIND_UNION: &str = "Union";
const KIND_INTERFACE: &str = "Interface";
const KIND_TYPE: &str = "Type";
const KIND_FUNCTION: &str = "Function";
const KIND_METHOD: &str = "Method";
const KIND_MODULE: &str = "Module";
const REL_DEFINES: &str = "DEFINES";
const REL_DEFINES_METHOD: &str = "DEFINES_METHOD";
const MODULE_LINE: usize = 0;

fn esc(s: &str) -> String {
    s.replace('\\', "\\\\").replace('"', "\\\"")
}

fn node_json(kind: &str, file: &str, line: usize, name: &str) -> String {
    format!(
        "{{\"kind\":\"{}\",\"file\":\"{}\",\"line\":{},\"name\":\"{}\"}}",
        kind,
        esc(file),
        line,
        esc(name)
    )
}

fn edge_json(
    rel: &str,
    file: &str,
    pkind: &str,
    pline: usize,
    ckind: &str,
    cline: usize,
) -> String {
    format!(
        "{{\"rel\":\"{}\",\"parent\":{{\"kind\":\"{}\",\"file\":\"{}\",\"line\":{}}},\"child\":{{\"kind\":\"{}\",\"file\":\"{}\",\"line\":{}}}}}",
        rel,
        pkind,
        esc(file),
        pline,
        ckind,
        esc(file),
        cline
    )
}

// ---- node collection (every declaration, including nested/closures) ----

struct NodeCollector<'a> {
    file: &'a str,
    out: &'a mut Vec<String>,
}

impl<'a> NodeCollector<'a> {
    fn emit(&mut self, kind: &str, line: usize, name: &str) {
        self.out.push(node_json(kind, self.file, line, name));
    }
}

impl<'ast, 'a> Visit<'ast> for NodeCollector<'a> {
    fn visit_item_struct(&mut self, node: &'ast syn::ItemStruct) {
        self.emit(KIND_CLASS, node.ident.span().start().line, &node.ident.to_string());
        syn::visit::visit_item_struct(self, node);
    }
    fn visit_item_enum(&mut self, node: &'ast syn::ItemEnum) {
        self.emit(KIND_ENUM, node.ident.span().start().line, &node.ident.to_string());
        syn::visit::visit_item_enum(self, node);
    }
    fn visit_item_union(&mut self, node: &'ast syn::ItemUnion) {
        self.emit(KIND_UNION, node.ident.span().start().line, &node.ident.to_string());
        syn::visit::visit_item_union(self, node);
    }
    fn visit_item_type(&mut self, node: &'ast syn::ItemType) {
        self.emit(KIND_TYPE, node.ident.span().start().line, &node.ident.to_string());
        syn::visit::visit_item_type(self, node);
    }
    fn visit_impl_item_type(&mut self, node: &'ast syn::ImplItemType) {
        self.emit(KIND_TYPE, node.ident.span().start().line, &node.ident.to_string());
        syn::visit::visit_impl_item_type(self, node);
    }
    fn visit_trait_item_type(&mut self, node: &'ast syn::TraitItemType) {
        self.emit(KIND_TYPE, node.ident.span().start().line, &node.ident.to_string());
        syn::visit::visit_trait_item_type(self, node);
    }
    fn visit_expr_closure(&mut self, node: &'ast syn::ExprClosure) {
        self.emit(KIND_FUNCTION, node.span().start().line, "closure");
        syn::visit::visit_expr_closure(self, node);
    }
    fn visit_item_trait(&mut self, node: &'ast syn::ItemTrait) {
        self.emit(KIND_INTERFACE, node.ident.span().start().line, &node.ident.to_string());
        syn::visit::visit_item_trait(self, node);
    }
    fn visit_item_fn(&mut self, node: &'ast syn::ItemFn) {
        self.emit(KIND_FUNCTION, node.sig.ident.span().start().line, &node.sig.ident.to_string());
        syn::visit::visit_item_fn(self, node);
    }
    fn visit_impl_item_fn(&mut self, node: &'ast syn::ImplItemFn) {
        self.emit(KIND_METHOD, node.sig.ident.span().start().line, &node.sig.ident.to_string());
        syn::visit::visit_impl_item_fn(self, node);
    }
    fn visit_trait_item_fn(&mut self, node: &'ast syn::TraitItemFn) {
        self.emit(KIND_METHOD, node.sig.ident.span().start().line, &node.sig.ident.to_string());
        syn::visit::visit_trait_item_fn(self, node);
    }
}

// ---- edge collection (containment) ----

fn type_table_key(modpath: &str, name: &str) -> String {
    format!("{}\u{0}{}", modpath, name)
}

// collect_types records each module-scoped type so an impl can resolve its
// target to the type's (kind, line).
fn collect_types(items: &[syn::Item], modpath: &str, table: &mut HashMap<String, (String, usize)>) {
    for item in items {
        match item {
            syn::Item::Struct(s) => {
                table.insert(
                    type_table_key(modpath, &s.ident.to_string()),
                    (KIND_CLASS.into(), s.ident.span().start().line),
                );
            }
            syn::Item::Enum(e) => {
                table.insert(
                    type_table_key(modpath, &e.ident.to_string()),
                    (KIND_ENUM.into(), e.ident.span().start().line),
                );
            }
            syn::Item::Union(u) => {
                table.insert(
                    type_table_key(modpath, &u.ident.to_string()),
                    (KIND_UNION.into(), u.ident.span().start().line),
                );
            }
            syn::Item::Type(t) => {
                table.insert(
                    type_table_key(modpath, &t.ident.to_string()),
                    (KIND_TYPE.into(), t.ident.span().start().line),
                );
            }
            syn::Item::Trait(tr) => {
                table.insert(
                    type_table_key(modpath, &tr.ident.to_string()),
                    (KIND_INTERFACE.into(), tr.ident.span().start().line),
                );
            }
            syn::Item::Mod(m) => {
                if let Some((_, content)) = &m.content {
                    let child = child_modpath(modpath, &m.ident.to_string());
                    collect_types(content, &child, table);
                }
            }
            _ => {}
        }
    }
}

fn child_modpath(modpath: &str, name: &str) -> String {
    if modpath.is_empty() {
        name.to_string()
    } else {
        format!("{}::{}", modpath, name)
    }
}

// resolve_type finds a type by name starting in modpath and walking outward to
// ancestor modules and the crate root (Rust name resolution is lexical).
fn resolve_type(
    modpath: &str,
    name: &str,
    table: &HashMap<String, (String, usize)>,
) -> Option<(String, usize)> {
    let mut parts: Vec<&str> = if modpath.is_empty() {
        Vec::new()
    } else {
        modpath.split("::").collect()
    };
    loop {
        let mp = parts.join("::");
        if let Some(v) = table.get(&type_table_key(&mp, name)) {
            return Some(v.clone());
        }
        if parts.is_empty() {
            break;
        }
        parts.pop();
    }
    None
}

// impl_target_name pulls the bare type name off an impl's self type.
fn impl_target_name(ty: &syn::Type) -> Option<String> {
    match ty {
        syn::Type::Path(tp) => tp.path.segments.last().map(|s| s.ident.to_string()),
        syn::Type::Reference(r) => impl_target_name(&r.elem),
        _ => None,
    }
}

fn process_edges(
    items: &[syn::Item],
    file: &str,
    module_line: usize,
    modpath: &str,
    table: &HashMap<String, (String, usize)>,
    edges: &mut Vec<String>,
) {
    for item in items {
        match item {
            syn::Item::Struct(s) => edges.push(edge_json(
                REL_DEFINES, file, KIND_MODULE, module_line, KIND_CLASS, s.ident.span().start().line,
            )),
            syn::Item::Enum(e) => edges.push(edge_json(
                REL_DEFINES, file, KIND_MODULE, module_line, KIND_ENUM, e.ident.span().start().line,
            )),
            syn::Item::Union(u) => edges.push(edge_json(
                REL_DEFINES, file, KIND_MODULE, module_line, KIND_UNION, u.ident.span().start().line,
            )),
            syn::Item::Type(t) => edges.push(edge_json(
                REL_DEFINES, file, KIND_MODULE, module_line, KIND_TYPE, t.ident.span().start().line,
            )),
            syn::Item::Fn(f) => edges.push(edge_json(
                REL_DEFINES, file, KIND_MODULE, module_line, KIND_FUNCTION, f.sig.ident.span().start().line,
            )),
            syn::Item::Trait(tr) => {
                let tline = tr.ident.span().start().line;
                edges.push(edge_json(
                    REL_DEFINES, file, KIND_MODULE, module_line, KIND_INTERFACE, tline,
                ));
                for ti in &tr.items {
                    match ti {
                        syn::TraitItem::Fn(m) => edges.push(edge_json(
                            REL_DEFINES_METHOD, file, KIND_INTERFACE, tline, KIND_METHOD,
                            m.sig.ident.span().start().line,
                        )),
                        // (H) An associated type is a module-scoped Type declaration
                        // (H) in cgr's model (DEFINEd by the enclosing module).
                        syn::TraitItem::Type(t) => edges.push(edge_json(
                            REL_DEFINES, file, KIND_MODULE, module_line, KIND_TYPE,
                            t.ident.span().start().line,
                        )),
                        _ => {}
                    }
                }
            }
            syn::Item::Impl(im) => {
                let owner = impl_target_name(&im.self_ty)
                    .and_then(|name| resolve_type(modpath, &name, table));
                for ii in &im.items {
                    match ii {
                        syn::ImplItem::Fn(m) => {
                            if let Some((kind, tline)) = &owner {
                                edges.push(edge_json(
                                    REL_DEFINES_METHOD, file, kind, *tline, KIND_METHOD,
                                    m.sig.ident.span().start().line,
                                ));
                            }
                        }
                        syn::ImplItem::Type(t) => edges.push(edge_json(
                            REL_DEFINES, file, KIND_MODULE, module_line, KIND_TYPE,
                            t.ident.span().start().line,
                        )),
                        _ => {}
                    }
                }
            }
            syn::Item::Mod(m) => {
                if let Some((_, content)) = &m.content {
                    let mline = m.ident.span().start().line;
                    edges.push(edge_json(
                        REL_DEFINES, file, KIND_MODULE, module_line, KIND_MODULE, mline,
                    ));
                    let child = child_modpath(modpath, &m.ident.to_string());
                    process_edges(content, file, mline, &child, table, edges);
                }
            }
            _ => {}
        }
    }
}

fn visit_dir(dir: &Path, root: &Path, nodes: &mut Vec<String>, edges: &mut Vec<String>) {
    let entries = match fs::read_dir(dir) {
        Ok(entries) => entries,
        Err(_) => return,
    };
    for entry in entries.flatten() {
        let path = entry.path();
        if path.is_dir() {
            let name = path.file_name().and_then(|n| n.to_str()).unwrap_or("");
            if !IGNORED_DIRS.contains(&name) {
                visit_dir(&path, root, nodes, edges);
            }
        } else if path.extension().and_then(|e| e.to_str()) == Some("rs") {
            if let Ok(src) = fs::read_to_string(&path) {
                if let Ok(ast) = syn::parse_file(&src) {
                    let rel = path
                        .strip_prefix(root)
                        .unwrap_or(&path)
                        .to_string_lossy()
                        .replace('\\', "/");
                    let mut collector = NodeCollector { file: &rel, out: nodes };
                    collector.visit_file(&ast);
                    let mut table: HashMap<String, (String, usize)> = HashMap::new();
                    collect_types(&ast.items, "", &mut table);
                    process_edges(&ast.items, &rel, MODULE_LINE, "", &table, edges);
                }
            }
        }
    }
}

fn main() {
    let root = env::args().nth(1).unwrap_or_else(|| ".".into());
    let root = Path::new(&root);
    let mut nodes = Vec::new();
    let mut edges = Vec::new();
    visit_dir(root, root, &mut nodes, &mut edges);
    println!(
        "{{\"nodes\":[{}],\"edges\":[{}]}}",
        nodes.join(","),
        edges.join(",")
    );
}
