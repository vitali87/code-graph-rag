// Authoritative Rust structure oracle for the cgr eval harness.
//
// Parses every .rs file under a directory with `syn` (the de-facto standard Rust
// parser) and emits one JSON record per declaration. The "kind" field uses cgr's
// NodeLabel vocabulary so records join cgr's graph on (kind, file, line).
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
// A `syn::visit::Visit` walk recurses into function bodies too, so function-local
// definitions are captured — cgr captures those by default, so the oracle must as
// well to stay an apples-to-apples ground truth.
//
// proc-macro2's "span-locations" feature is what makes `.span().start().line`
// return real source lines when parsing a file (outside a proc-macro context).
//
// Run: cargo run --release -- <dir>

use std::env;
use std::fs;
use std::path::Path;
use syn::spanned::Spanned;
use syn::visit::Visit;

const IGNORED_DIRS: [&str; 4] = [".git", "target", "vendor", "node_modules"];

fn esc(s: &str) -> String {
    s.replace('\\', "\\\\").replace('"', "\\\"")
}

struct Collector<'a> {
    file: &'a str,
    out: &'a mut Vec<String>,
}

impl<'a> Collector<'a> {
    fn emit(&mut self, kind: &str, line: usize, name: &str) {
        self.out.push(format!(
            "{{\"kind\":\"{}\",\"file\":\"{}\",\"line\":{},\"name\":\"{}\"}}",
            kind,
            esc(self.file),
            line,
            esc(name)
        ));
    }
}

impl<'ast, 'a> Visit<'ast> for Collector<'a> {
    fn visit_item_struct(&mut self, node: &'ast syn::ItemStruct) {
        self.emit("Class", node.ident.span().start().line, &node.ident.to_string());
        syn::visit::visit_item_struct(self, node);
    }
    fn visit_item_enum(&mut self, node: &'ast syn::ItemEnum) {
        self.emit("Enum", node.ident.span().start().line, &node.ident.to_string());
        syn::visit::visit_item_enum(self, node);
    }
    fn visit_item_union(&mut self, node: &'ast syn::ItemUnion) {
        self.emit("Union", node.ident.span().start().line, &node.ident.to_string());
        syn::visit::visit_item_union(self, node);
    }
    fn visit_item_type(&mut self, node: &'ast syn::ItemType) {
        self.emit("Type", node.ident.span().start().line, &node.ident.to_string());
        syn::visit::visit_item_type(self, node);
    }
    fn visit_impl_item_type(&mut self, node: &'ast syn::ImplItemType) {
        self.emit("Type", node.ident.span().start().line, &node.ident.to_string());
        syn::visit::visit_impl_item_type(self, node);
    }
    fn visit_trait_item_type(&mut self, node: &'ast syn::TraitItemType) {
        self.emit("Type", node.ident.span().start().line, &node.ident.to_string());
        syn::visit::visit_trait_item_type(self, node);
    }
    fn visit_expr_closure(&mut self, node: &'ast syn::ExprClosure) {
        // (H) cgr models Rust closures as anonymous Function nodes; match that so
        // (H) the (kind, file, line) join lines up. The synthetic name is unused
        // (H) by scoring (NodeKey is kind/file/line only).
        self.emit("Function", node.span().start().line, "closure");
        syn::visit::visit_expr_closure(self, node);
    }
    fn visit_item_trait(&mut self, node: &'ast syn::ItemTrait) {
        self.emit("Interface", node.ident.span().start().line, &node.ident.to_string());
        syn::visit::visit_item_trait(self, node);
    }
    fn visit_item_fn(&mut self, node: &'ast syn::ItemFn) {
        self.emit("Function", node.sig.ident.span().start().line, &node.sig.ident.to_string());
        syn::visit::visit_item_fn(self, node);
    }
    fn visit_impl_item_fn(&mut self, node: &'ast syn::ImplItemFn) {
        self.emit("Method", node.sig.ident.span().start().line, &node.sig.ident.to_string());
        syn::visit::visit_impl_item_fn(self, node);
    }
    fn visit_trait_item_fn(&mut self, node: &'ast syn::TraitItemFn) {
        self.emit("Method", node.sig.ident.span().start().line, &node.sig.ident.to_string());
        syn::visit::visit_trait_item_fn(self, node);
    }
}

fn visit_dir(dir: &Path, root: &Path, out: &mut Vec<String>) {
    let entries = match fs::read_dir(dir) {
        Ok(entries) => entries,
        Err(_) => return,
    };
    for entry in entries.flatten() {
        let path = entry.path();
        if path.is_dir() {
            let name = path.file_name().and_then(|n| n.to_str()).unwrap_or("");
            if !IGNORED_DIRS.contains(&name) {
                visit_dir(&path, root, out);
            }
        } else if path.extension().and_then(|e| e.to_str()) == Some("rs") {
            if let Ok(src) = fs::read_to_string(&path) {
                if let Ok(ast) = syn::parse_file(&src) {
                    let rel = path
                        .strip_prefix(root)
                        .unwrap_or(&path)
                        .to_string_lossy()
                        .replace('\\', "/");
                    let mut collector = Collector { file: &rel, out };
                    collector.visit_file(&ast);
                }
            }
        }
    }
}

fn main() {
    let root = env::args().nth(1).unwrap_or_else(|| ".".into());
    let root = Path::new(&root);
    let mut out = Vec::new();
    visit_dir(root, root, &mut out);
    println!("[{}]", out.join(","));
}
