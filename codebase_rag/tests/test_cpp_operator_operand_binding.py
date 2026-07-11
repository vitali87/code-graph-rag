# (H) nlohmann finding: `last_token == token_type::end_of_input` (an ENUM
# (H) comparison, a builtin) synthesized an operator_equal call that bound to
# (H) an UNRELATED class's operator== (json_pointer) and then fanned out to
# (H) every duplicate-qn overload variant (9 edges of pure noise). When the
# (H) left operand's type is KNOWN, the operator must bind only to that type's
# (H) own operator (member, or a free overload in the type's module) or emit
# (H) nothing at all; only an UNTYPED operand keeps the old best-candidate
# (H) behaviour so no existing edge drops.
from pathlib import Path

from evals.cgr_graph import _capture


def _calls(tmp_path: Path) -> set[tuple[str, str]]:
    ingestor = _capture(tmp_path, "proj")
    return {
        (str(from_val), str(to_val))
        for _fl, from_val, rel, _tl, to_val in ingestor.rels
        if rel == "CALLS"
    }


def test_enum_comparison_does_not_bind_unrelated_operator(tmp_path: Path) -> None:
    (tmp_path / "ptr.hpp").write_text(
        "struct json_pointer {\n"
        "    bool operator==(const json_pointer& rhs) const { return true; }\n"
        "    bool operator==(const char* rhs) const { return false; }\n"
        "};\n",
        encoding="utf-8",
    )
    (tmp_path / "parser.hpp").write_text(
        "enum class token_type { begin, end_of_input };\n"
        "struct parser {\n"
        "    token_type last_token = token_type::begin;\n"
        "    bool exception_message() {\n"
        "        token_type t = last_token;\n"
        "        return t == token_type::end_of_input;\n"
        "    }\n"
        "};\n",
        encoding="utf-8",
    )
    calls = _calls(tmp_path)
    assert not any("json_pointer.operator_equal" in callee for _, callee in calls), (
        sorted(c for c in calls if "operator" in c[1])
    )


def test_typed_operand_binds_its_own_operator(tmp_path: Path) -> None:
    # (H) Aaa is the alphabetical decoy: a bare best-candidate sort would pick
    # (H) it, so a passing test proves operand-type-directed binding.
    (tmp_path / "ops.hpp").write_text(
        "struct Aaa {\n"
        "    bool operator==(const Aaa& rhs) const { return true; }\n"
        "};\n"
        "struct Widget {\n"
        "    bool operator==(const Widget& rhs) const { return true; }\n"
        "};\n"
        "bool driver(Widget a, Widget b) {\n"
        "    return a == b;\n"
        "}\n",
        encoding="utf-8",
    )
    calls = _calls(tmp_path)
    assert ("proj.ops.driver", "proj.ops.Widget.operator_equal") in calls, sorted(
        c for c in calls if "operator" in c[1]
    )
    assert ("proj.ops.driver", "proj.ops.Aaa.operator_equal") not in calls


def test_typed_operand_binds_free_operator_in_type_module(tmp_path: Path) -> None:
    (tmp_path / "free.hpp").write_text(
        "struct Aaa {\n"
        "    bool operator==(const Aaa& rhs) const { return true; }\n"
        "};\n"
        "struct Token { int v; };\n"
        "bool operator==(const Token& a, const Token& b) { return a.v == b.v; }\n"
        "bool driver(Token a, Token b) {\n"
        "    return a == b;\n"
        "}\n",
        encoding="utf-8",
    )
    calls = _calls(tmp_path)
    assert ("proj.free.driver", "proj.free.operator_equal") in calls, sorted(
        c for c in calls if "operator" in c[1]
    )
    assert ("proj.free.driver", "proj.free.Aaa.operator_equal") not in calls


def test_untyped_operand_keeps_existing_binding(tmp_path: Path) -> None:
    # (H) fmt regression guard shape: an operand the type inference cannot see
    # (H) (a macro-produced expression) must keep the pre-existing
    # (H) best-candidate behaviour rather than dropping edges wholesale.
    (tmp_path / "keep.hpp").write_text(
        "struct Only {\n"
        "    bool operator==(const Only& rhs) const { return true; }\n"
        "};\n"
        "bool driver() {\n"
        "    return MACRO_LHS_ == MACRO_RHS_;\n"
        "}\n",
        encoding="utf-8",
    )
    calls = _calls(tmp_path)
    assert ("proj.keep.driver", "proj.keep.Only.operator_equal") in calls, sorted(
        c for c in calls if "operator" in c[1]
    )


def test_typed_operand_binds_inherited_operator(tmp_path: Path) -> None:
    # (H) Derived inherits Base::operator==; suppressing the edge because
    # (H) Derived itself declares none would orphan the base overload.
    (tmp_path / "inh.hpp").write_text(
        "struct Aaa {\n"
        "    bool operator==(const Aaa& rhs) const { return true; }\n"
        "};\n"
        "struct Base {\n"
        "    bool operator==(const Base& rhs) const { return true; }\n"
        "};\n"
        "struct Derived : public Base {};\n"
        "bool driver(Derived a, Derived b) {\n"
        "    return a == b;\n"
        "}\n",
        encoding="utf-8",
    )
    calls = _calls(tmp_path)
    assert ("proj.inh.driver", "proj.inh.Base.operator_equal") in calls, sorted(
        c for c in calls if "operator" in c[1]
    )
    assert ("proj.inh.driver", "proj.inh.Aaa.operator_equal") not in calls


def test_typed_operand_binds_free_operator_in_enclosing_namespace(
    tmp_path: Path,
) -> None:
    # (H) ADL: a free overload for a nested type may live in any enclosing
    # (H) namespace, not only the type's immediate parent scope.
    (tmp_path / "adl.hpp").write_text(
        "struct Aaa {\n"
        "    bool operator==(const Aaa& rhs) const { return true; }\n"
        "};\n"
        "namespace outer {\n"
        "namespace inner {\n"
        "struct Token { int v; };\n"
        "}\n"
        "bool operator==(const inner::Token& a, const inner::Token& b) {\n"
        "    return a.v == b.v;\n"
        "}\n"
        "bool driver(inner::Token a, inner::Token b) {\n"
        "    return a == b;\n"
        "}\n"
        "}\n",
        encoding="utf-8",
    )
    calls = _calls(tmp_path)
    assert (
        "proj.adl.outer.driver",
        "proj.adl.outer.operator_equal",
    ) in calls, sorted(c for c in calls if "operator" in c[1])
    # (H) scoped to the typed driver call: the `a.v == b.v` comparison inside
    # (H) the free operator has FIELD operands (outside the bare-identifier
    # (H) scope) and keeps the legacy path.
    assert not any(
        "Aaa.operator_equal" in callee for caller, callee in calls if "driver" in caller
    )
