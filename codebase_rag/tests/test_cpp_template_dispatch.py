# (H) A C++ call through a template-parameter receiver (`template<typename SAX> ...
# (H) sax->start_object()`) has no concrete receiver type the graph can resolve, so the
# (H) trie fallback binds it to a single arbitrary same-named method and every OTHER
# (H) implementer reports as dead (nlohmann/json's json_sax_* SAX visitors). When the
# (H) receiver type is unresolved, cgr must fan the call out to EVERY class defining that
# (H) method so no structural interface implementer is missed.
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
    (root / "sax.hpp").write_text(body, encoding="utf-8")


def test_template_param_receiver_fans_out_to_all_implementers(tmp_path: Path) -> None:
    # (H) `sax->start_object()` inside a `template<typename SAX>` function must reach
    # (H) start_object on BOTH concrete visitor structs, not just one.
    _make(
        tmp_path,
        "struct Aaa {\n"
        "    bool start_object(int n) { return true; }\n"
        "};\n"
        "struct DomParser {\n"
        "    bool start_object(int n) { return true; }\n"
        "};\n"
        "template<typename SAX>\n"
        "bool run_parse(SAX* sax) {\n"
        "    return sax->start_object(1);\n"
        "}\n",
    )
    calls = _calls(tmp_path)
    assert ("crate.sax.run_parse", "crate.sax.Aaa.start_object") in calls
    assert ("crate.sax.run_parse", "crate.sax.DomParser.start_object") in calls


def test_typed_receiver_does_not_fan_out(tmp_path: Path) -> None:
    # (H) GUARDRAIL: when the receiver has a concrete type, the call resolves precisely
    # (H) and must NOT fan out to unrelated same-named methods. `p.work()` on a `Real p`
    # (H) local binds only Real.work, never Other.work.
    _make(
        tmp_path,
        "struct Other {\n"
        "    bool work() { return false; }\n"
        "};\n"
        "struct Real {\n"
        "    bool work() { return true; }\n"
        "};\n"
        "bool go() {\n"
        "    Real p;\n"
        "    return p.work();\n"
        "}\n",
    )
    calls = _calls(tmp_path)
    assert ("crate.sax.go", "crate.sax.Real.work") in calls
    assert ("crate.sax.go", "crate.sax.Other.work") not in calls


def test_default_type_arg_is_not_collected_as_template_param() -> None:
    # (H) A template parameter's DEFAULT type (`typename SAX = Real`) is a concrete type,
    # (H) NOT a template parameter -- collecting it would put `Real` in the template-param
    # (H) set and fan a real `Real r; r.work()` out to unrelated implementers. Only the
    # (H) parameter names (SAX, T, Ts) are collected, across default / plain / variadic
    # (H) forms; a non-type value param (`int N`) contributes no type name.
    from codebase_rag.parser_loader import load_parsers
    from codebase_rag.parsers.cpp import CppTypeInferenceEngine

    parsers, _ = load_parsers()
    src = b"template<typename SAX = Real, class T, typename... Ts, int N, MyEnum E>\nbool f() { return true; }\n"
    tree = parsers["cpp"].parse(src)

    def find_fn(node):
        if node.type == "function_definition":
            return node
        for child in node.children:
            if (found := find_fn(child)) is not None:
                return found
        return None

    fn = find_fn(tree.root_node)
    assert fn is not None
    params = CppTypeInferenceEngine().collect_template_param_names(fn)
    assert params == frozenset({"SAX", "T", "Ts"}), params
