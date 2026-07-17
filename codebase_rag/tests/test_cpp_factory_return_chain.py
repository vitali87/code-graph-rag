# (H) A C++ chained call on a factory-call receiver (`parser(ia, cb).parse(true, r)`,
# (H) nlohmann/json's basic_json::parse -> detail::parser) has the return of a factory
# (H) function/method as its receiver. Without inferring that return type the final
# (H) method binds to nothing and the whole returned-class cluster (parser.parse/accept/
# (H) sax_parse) reports as dead. cgr must type the factory's recorded return type and
# (H) resolve the chained method on it.
from pathlib import Path

from evals.cgr_graph import _capture


def _calls(tmp_path: Path) -> set[tuple[str, str]]:
    ingestor = _capture(tmp_path, "crate")
    return {
        (str(from_val), str(to_val))
        for _fl, from_val, rel, _tl, to_val in ingestor.rels
        if rel == "CALLS"
    }


def _make(root: Path, body: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "f.hpp").write_text(body, encoding="utf-8")


def test_free_factory_return_chain_resolves(tmp_path: Path) -> None:
    # (H) `make().run()` where `make` is a free function returning Widget must reach
    # (H) Widget.run, not drop or mis-bind. Aaa.run is an alphabetical decoy: a bare
    # (H) trie fallback would pick it, so a passing test proves real return typing.
    _make(
        tmp_path,
        "struct Aaa {\n"
        "    void run() {}\n"
        "};\n"
        "struct Widget {\n"
        "    void run() {}\n"
        "};\n"
        "Widget make() { return Widget(); }\n"
        "void driver() {\n"
        "    make().run();\n"
        "}\n",
    )
    calls = _calls(tmp_path)
    assert ("crate.f.driver", "crate.f.Widget.run") in calls, sorted(
        c for c in calls if "run" in c[1]
    )
    assert ("crate.f.driver", "crate.f.Aaa.run") not in calls


def test_static_factory_method_return_chain_resolves(tmp_path: Path) -> None:
    # (H) nlohmann shape: a static factory METHOD on Owner returns a DIFFERENT class,
    # (H) called as `parser(...).parse(...)` from inside Owner. The factory name is a
    # (H) callable (Owner.parser), distinct from the returned class (Parser). Aaa.parse
    # (H) is an alphabetical decoy the bare fallback would wrongly pick.
    _make(
        tmp_path,
        "struct Aaa {\n"
        "    void parse(bool strict) {}\n"
        "};\n"
        "struct Parser {\n"
        "    void parse(bool strict) {}\n"
        "};\n"
        "struct Owner {\n"
        "    static Parser parser(int x) { return Parser(); }\n"
        "    void parse_impl() {\n"
        "        parser(1).parse(true);\n"
        "    }\n"
        "};\n",
    )
    calls = _calls(tmp_path)
    assert ("crate.f.Owner.parse_impl", "crate.f.Parser.parse") in calls, sorted(
        c for c in calls if "parse" in c[1]
    )
    assert ("crate.f.Owner.parse_impl", "crate.f.Aaa.parse") not in calls


def test_inferred_receiver_missing_method_does_not_bind_bare(tmp_path: Path) -> None:
    # (H) `make()` returns Widget (recorded), but Widget has NO `run`. The bare-method
    # (H) C/C++ fallback must NOT fire once the receiver type is known -- binding
    # (H) `make().run()` to an unrelated alphabetical `Aaa.run` is a false edge. When the
    # (H) type is inferred and lacks the method, the chain drops (returns nothing).
    _make(
        tmp_path,
        "struct Aaa {\n"
        "    void run() {}\n"
        "};\n"
        "struct Widget {\n"
        "    void other() {}\n"
        "};\n"
        "Widget make() { return Widget(); }\n"
        "void driver() {\n"
        "    make().run();\n"
        "}\n",
    )
    calls = _calls(tmp_path)
    assert ("crate.f.driver", "crate.f.Aaa.run") not in calls, sorted(
        c for c in calls if "run" in c[1]
    )


def test_out_of_class_factory_return_chain_resolves(tmp_path: Path) -> None:
    # (H) The header/impl split shape: the factory method is DECLARED in the class body
    # (H) but DEFINED out-of-class (`Parser Owner::parser(...) { ... }`). Its return type
    # (H) must still be recorded (the out-of-class path returns before the free-function
    # (H) recording runs) so `parser(1).parse(true)` types the receiver as Parser rather
    # (H) than drop or mis-bind to the alphabetical decoy Aaa.parse.
    _make(
        tmp_path,
        "struct Aaa {\n"
        "    void parse(bool strict) {}\n"
        "};\n"
        "struct Parser {\n"
        "    void parse(bool strict) {}\n"
        "};\n"
        "struct Owner {\n"
        "    static Parser parser(int x);\n"
        "    void parse_impl();\n"
        "};\n"
        "Parser Owner::parser(int x) { return Parser(); }\n"
        "void Owner::parse_impl() {\n"
        "    parser(1).parse(true);\n"
        "}\n",
    )
    calls = _calls(tmp_path)
    assert ("crate.f.Owner.parse_impl", "crate.f.Parser.parse") in calls, sorted(
        c for c in calls if "parse" in c[1]
    )
    assert ("crate.f.Owner.parse_impl", "crate.f.Aaa.parse") not in calls


def test_return_type_path_normalizes_template_qualified_scope() -> None:
    # (H) A return type qualified by a TEMPLATE_TYPE scope (`Outer<T>::Inner`) must
    # (H) reduce to the dotted registry path "Outer.Inner" -- the scope's raw text
    # (H) leaks the `<T>` template arguments, which no class QN carries.
    from codebase_rag.parser_loader import load_parsers
    from codebase_rag.parsers.cpp.utils import extract_return_type_name

    parsers, _ = load_parsers()
    tree = parsers["cpp"].parse(b"Outer<T>::Inner make() { return {}; }\n")

    def find_fn(node):
        if node.type == "function_definition":
            return node
        for child in node.children:
            if (found := find_fn(child)) is not None:
                return found
        return None

    fn = find_fn(tree.root_node)
    assert fn is not None
    assert extract_return_type_name(fn) == "Outer.Inner"


def test_constructor_temporary_chain_resolves(tmp_path: Path) -> None:
    # (H) nlohmann from_cbor shape: `Reader<decltype(ia)>(std::move(ia), fmt)
    # (H) .sax_parse(...)` chains a method on a CONSTRUCTOR TEMPORARY -- the
    # (H) callee is the class itself, so the receiver type IS that class. Aaa
    # (H) is the alphabetical trie decoy.
    _make(
        tmp_path,
        "struct Aaa {\n"
        "    bool sax_parse(int f) { return true; }\n"
        "};\n"
        "template<typename T>\n"
        "struct Reader {\n"
        "    Reader(T&& t, int f) {}\n"
        "    bool sax_parse(int f) { return true; }\n"
        "};\n"
        "template<typename T>\n"
        "bool driver(T&& ia) {\n"
        "    return Reader<T>(static_cast<T&&>(ia), 1).sax_parse(1);\n"
        "}\n",
    )
    calls = _calls(tmp_path)
    assert ("crate.f.driver", "crate.f.Reader.sax_parse") in calls, sorted(
        c for c in calls if "sax_parse" in c[1]
    )
    assert ("crate.f.driver", "crate.f.Aaa.sax_parse") not in calls


def test_qualified_constructor_temporary_chain_resolves(tmp_path: Path) -> None:
    # (H) The namespace-qualified form `detail::Reader<...>(...).sax_parse(...)`
    # (H) (nlohmann json.hpp:4159) must resolve through the qualified path too.
    _make(
        tmp_path,
        "struct Aaa {\n"
        "    bool sax_parse(int f) { return true; }\n"
        "};\n"
        "namespace detail {\n"
        "template<typename T>\n"
        "struct Reader {\n"
        "    Reader(T&& t, int f) {}\n"
        "    bool sax_parse(int f) { return true; }\n"
        "};\n"
        "}\n"
        "template<typename T>\n"
        "bool driver(T&& ia) {\n"
        "    return detail::Reader<T>(static_cast<T&&>(ia), 1).sax_parse(1);\n"
        "}\n",
    )
    calls = _calls(tmp_path)
    assert ("crate.f.driver", "crate.f.detail.Reader.sax_parse") in calls, sorted(
        c for c in calls if "sax_parse" in c[1]
    )
    assert ("crate.f.driver", "crate.f.Aaa.sax_parse") not in calls


def test_macro_attributed_definition_name_extracted() -> None:
    # (H) nlohmann shape (JSON_HEDLEY_NON_NULL(3) before bool sax_parse(...)):
    # (H) tree-sitter merges the attribute macro into the definition as a
    # (H) parenthesized_declarator wrapping an ERROR plus the REAL
    # (H) function_declarator. The name walk must descend through it, or the
    # (H) method never registers and its whole callee cluster (binary_reader's
    # (H) 37 methods) reads as dead.
    from codebase_rag.parser_loader import load_parsers
    from codebase_rag.parsers.cpp import utils as cpp_utils

    parsers, _queries = load_parsers()
    src = (
        "class Reader {\n"
        "  public:\n"
        "    HEDLEY_NON_NULL(2)\n"
        "    bool sax_parse(const int format, void* sax_, const bool strict = true)\n"
        "    {\n"
        "        return true;\n"
        "    }\n"
        "    bool other() { return true; }\n"
        "};\n"
    )
    tree = parsers["cpp"].parse(src.encode())

    def find(node: object, node_type: str) -> object | None:
        if node.type == node_type:
            return node
        for child in node.named_children:
            if (hit := find(child, node_type)) is not None:
                return hit
        return None

    defn = find(tree.root_node, "function_definition")
    assert defn is not None
    # (H) Pin the mangled shape this test exists for: if a grammar bump starts
    # (H) parsing the macro cleanly, this guard flags the test for revisit.
    assert find(defn, "parenthesized_declarator") is not None
    assert cpp_utils.extract_function_name(defn) == "sax_parse"
