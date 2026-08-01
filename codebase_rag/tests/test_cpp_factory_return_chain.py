# A C++ chained call on a factory-call receiver (`parser(ia, cb).parse(true, r)`,
# nlohmann/json's basic_json::parse -> detail::parser) has the return of a factory
# function/method as its receiver. Without inferring that return type the final
# method binds to nothing and the whole returned-class cluster (parser.parse/accept/
# sax_parse) reports as dead. cgr must type the factory's recorded return type and
# resolve the chained method on it.
from pathlib import Path

from codebase_rag import constants as cs
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
    # `make().run()` where `make` is a free function returning Widget must reach
    # Widget.run, not drop or mis-bind. Aaa.run is an alphabetical decoy: a bare
    # trie fallback would pick it, so a passing test proves real return typing.
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
    # nlohmann shape: a static factory METHOD on Owner returns a DIFFERENT class,
    # called as `parser(...).parse(...)` from inside Owner. The factory name is a
    # callable (Owner.parser), distinct from the returned class (Parser). Aaa.parse
    # is an alphabetical decoy the bare fallback would wrongly pick.
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
    # `make()` returns Widget (recorded), but Widget has NO `run`. Once the receiver
    # type is known the bare-method C/C++ fallback must NOT fire; binding
    # `make().run()` to an alphabetical `Aaa.run` is a false edge, so the chain
    # drops when the inferred type lacks the method.
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
    # Header/impl split: the factory method is DECLARED in the class body but DEFINED
    # out-of-class (`Parser Owner::parser(...) { ... }`). Its return type must still
    # be recorded (the out-of-class path returns before free-function recording) so
    # `parser(1).parse(true)` types the receiver as Parser, not the decoy Aaa.parse.
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
    # A return type qualified by a TEMPLATE_TYPE scope (`Outer<T>::Inner`) must
    # reduce to the dotted registry path "Outer.Inner"; the scope's raw text
    # leaks the `<T>` template arguments, which no class QN carries.
    from codebase_rag.parser_loader import load_parsers
    from codebase_rag.parsers.cpp.utils import extract_return_type_name

    parsers, _ = load_parsers()
    tree = parsers[cs.SupportedLanguage.CPP].parse(
        b"Outer<T>::Inner make() { return {}; }\n"
    )

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
    # nlohmann from_cbor shape: `Reader<decltype(ia)>(std::move(ia), fmt)
    # .sax_parse(...)` chains a method on a CONSTRUCTOR TEMPORARY -- the
    # callee is the class itself, so the receiver type IS that class. Aaa
    # is the alphabetical trie decoy.
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
    # The namespace-qualified form `detail::Reader<...>(...).sax_parse(...)`
    # (nlohmann json.hpp:4159) must resolve through the qualified path too.
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
    # nlohmann shape (JSON_HEDLEY_NON_NULL(3) before bool sax_parse(...)):
    # tree-sitter merges the attribute macro into the definition as a
    # parenthesized_declarator wrapping an ERROR plus the REAL
    # function_declarator. The name walk must descend through it, or the
    # method never registers and its whole callee cluster (binary_reader's
    # 37 methods) reads as dead.
    from tree_sitter import Node

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
    tree = parsers[cs.SupportedLanguage.CPP].parse(src.encode())

    def find(node: Node, node_type: str) -> Node | None:
        if node.type == node_type:
            return node
        for child in node.named_children:
            if (hit := find(child, node_type)) is not None:
                return hit
        return None

    defn = find(tree.root_node, "function_definition")
    assert defn is not None
    # Pin the mangled shape this test exists for: if a grammar bump starts
    # parsing the macro cleanly, this guard flags the test for revisit.
    assert find(defn, "parenthesized_declarator") is not None
    assert cpp_utils.extract_function_name(defn) == "sax_parse"

    # Macro-attributed CONSTRUCTOR with a member-initializer list
    # (nlohmann's exception hierarchy): recovery buries the REAL ctor
    # declarator inside the ERROR and leaves the base-initializer
    # (`: exception(...)`) as the sibling function_declarator. The walk
    # must take the first declarator in SOURCE order, entering the ERROR,
    # or every such ctor registers under the base class's name.
    ctor_src = (
        "class invalid_iterator : public exception {\n"
        "  private:\n"
        "    HEDLEY_NON_NULL(3)\n"
        "    invalid_iterator(int id_, const char* what_arg)\n"
        "        : exception(id_, what_arg) {}\n"
        "};\n"
    )
    ctor_tree = parsers[cs.SupportedLanguage.CPP].parse(ctor_src.encode())
    ctor_defn = find(ctor_tree.root_node, "function_definition")
    assert ctor_defn is not None
    assert find(ctor_defn, "parenthesized_declarator") is not None
    assert cpp_utils.extract_function_name(ctor_defn) == "invalid_iterator"


def test_braced_init_return_emits_ctor_call(tmp_path: Path) -> None:
    # nlohmann's exception factories: `static invalid_iterator create(...)
    # { return {id_, w.c_str()}; }` constructs via a braced initializer
    # list -- no call node exists, so the private ctor gets no CALLS edge
    # and reports dead even though its only factory is alive. The declared
    # return type names the constructed class.
    _make(
        tmp_path,
        "struct Aaa {\n"
        "    Aaa(int a, const char* b) {}\n"
        "};\n"
        "struct Widget {\n"
        "    Widget(int a, const char* b) {}\n"
        "    static Widget create(int a) {\n"
        '        return {a, "x"};\n'
        "    }\n"
        "};\n"
        "int helper() { return 1; }\n"
        "void unrelated() { helper(); }\n",
    )
    ingestor = _capture(tmp_path, "crate")
    calls = {
        (str(f), str(r), str(t))
        for _fl, f, r, _tl, t in ingestor.rels
        if str(r) in ("CALLS", "INSTANTIATES")
    }
    assert ("crate.f.Widget.create", "INSTANTIATES", "crate.f.Widget") in calls, sorted(
        c for c in calls if "create" in c[0]
    )
    assert ("crate.f.Widget.create", "CALLS", "crate.f.Widget.Widget") in calls, sorted(
        c for c in calls if "create" in c[0]
    )
    assert not any("Aaa" in t for _f, _r, t in calls if _f.endswith("create"))
