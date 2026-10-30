"""A small Rust crate shared by the tests that pin the Rust collaborators: an
inline module, a file module with a trait, a struct and impls of a project
and an external trait, a trait impl named through the crate root's
re-export, a function-body `use` and a body-local item sharing a module
item's name, a nested file module reaching back with `super::`, and a
manifest with one registry dependency."""

from __future__ import annotations

from tree_sitter import Node

from codebase_rag.tests.collaborator_support import nodes_of

RUST_CRATE = {
    "Cargo.toml": (
        '[package]\nname = "rp"\nversion = "0.1.0"\n\n[dependencies]\nserde = "1"\n'
    ),
    "src/lib.rs": (
        "mod clash;\n"
        "mod shapes;\n"
        "pub use shapes::{Area, Circle};\n"
        "use crate::util::helper;\n\n"
        "mod util {\n    pub fn helper() -> i32 { 1 }\n}\n\n"
        "pub fn run() -> f64 {\n"
        "    let c = Circle::new(1.0);\n"
        '    println!("{}", helper());\n'
        "    let add = |x: i32| x + 1;\n"
        "    add(1);\n"
        "    fn local() -> i32 { 2 }\n"
        "    local();\n"
        "    c.area()\n"
        "}\n"
    ),
    "src/shapes.rs": (
        "use std::fmt;\n\n"
        "pub trait Area {\n    fn area(&self) -> f64;\n}\n\n"
        "pub struct Circle {\n    r: f64,\n}\n\n"
        "impl Area for Circle {\n    fn area(&self) -> f64 { self.r }\n}\n\n"
        "impl Circle {\n    pub fn new(r: f64) -> Circle { Circle { r } }\n}\n\n"
        "impl fmt::Display for Circle {\n"
        "    fn fmt(&self, f: &mut fmt::Formatter) -> fmt::Result { Ok(()) }\n"
        "}\n\n"
        "pub mod inner;\n"
    ),
    "src/clash.rs": (
        "pub struct Square;\n\n"
        "impl crate::Area for Square {\n    fn area(&self) -> f64 { 0.0 }\n}\n\n"
        "pub fn outer() -> u8 {\n"
        "    use crate::util::helper;\n"
        "    fn tally() -> String { String::new() }\n"
        "    tally();\n"
        "    helper();\n"
        "    0\n"
        "}\n\n"
        "pub fn tally() -> u8 { 0 }\n"
    ),
    "src/shapes/inner.rs": (
        "pub fn deep() -> i32 {\n    super::super::util::helper()\n}\n"
    ),
}


def function_named(root: Node, name: str) -> Node:
    """The first `fn` item under `root` called `name`."""
    for node in nodes_of(root, "function_item"):
        name_node = node.child_by_field_name("name")
        if name_node is not None and name_node.text == name.encode():
            return node
    raise AssertionError(f"no fn {name} in {root.text!r}")
