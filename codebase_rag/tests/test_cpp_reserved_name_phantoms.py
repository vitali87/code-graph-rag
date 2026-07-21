# nlohmann's binary_reader ends with a macro access-label
# (`JSON_PRIVATE_UNLESS_TESTED:`) followed by `const decltype(MACRO_)
# field = MACRO_;` members. Without the macro definitions tree-sitter
# recovers that region as a function declaration NAMED `decltype`, and the
# ingester registers a phantom Method reader.decltype. A C++ reserved
# keyword can never name a real function or method, so extraction must
# reject it.
from pathlib import Path

from evals.cgr_graph import _capture


def _method_leaves(tmp_path: Path) -> set[str]:
    ingestor = _capture(tmp_path, "proj")
    return {
        str(uid).rsplit(".", 1)[-1]
        for (label, uid) in ingestor.nodes
        if str(label) in ("Method", "Function")
    }


def test_macro_label_decltype_member_does_not_mint_keyword_method(
    tmp_path: Path,
) -> None:
    (tmp_path / "r.hpp").write_text(
        "#define MAKE_MARKERS_ \\\n"
        "    make_array<char>('F', 'H')\n"
        "struct reader {\n"
        "    void parse() {}\n"
        "  PRIVATE_UNLESS_TESTED:\n"
        "    const decltype(MAKE_MARKERS_) markers =\n"
        "        MAKE_MARKERS_;\n"
        "};\n",
        encoding="utf-8",
    )
    leaves = _method_leaves(tmp_path)
    assert "parse" in leaves, leaves
    assert "decltype" not in leaves, leaves


def test_reserved_keyword_names_never_register(tmp_path: Path) -> None:
    # The same recovery class can surface other keywords in declarator
    # position; none may become a definition node.
    (tmp_path / "k.hpp").write_text(
        "struct holder {\n"
        "    void real() {}\n"
        "  UNKNOWN_LABEL_A:\n"
        "    const decltype(SOME_MACRO_A_) a = SOME_MACRO_A_;\n"
        "  UNKNOWN_LABEL_B:\n"
        "    const decltype(SOME_MACRO_B_) b = SOME_MACRO_B_;\n"
        "};\n",
        encoding="utf-8",
    )
    leaves = _method_leaves(tmp_path)
    assert "real" in leaves, leaves
    assert not leaves & {"decltype", "sizeof", "alignof", "typeid"}, leaves
