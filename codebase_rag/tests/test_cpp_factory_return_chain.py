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
