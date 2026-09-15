"""Direct pins for `_parse_rule` edges the YAML-driven tests cannot reach.

`test_ast_grep_tier_languages.py` already covers the main validation branches
end to end through `load_pattern_configs`, which is the right level for them.
Going through YAML constrains what those can express, so this file calls
`_parse_rule` directly to pin what is left: the truthiness edges (an empty
string is not an absent value), stringification of non-string field values,
the ORDER the three applies-to guards fire in, and `_parse_rules` itself.

The guard order matters because #1669 turned three sequential `if`s into one
loop; the loop's order is now what preserves which error a multi-violation
rule reports. Each test asserts the WHOLE rule rather than one field, so a
refactor that drops or invents a field fails here.
"""

import pytest

from codebase_rag.parsers.ast_grep_tier import _parse_rule, _parse_rules, _Rule

PATH = "nix.yaml"
SECTION = "functions"


def _parse(raw: object) -> _Rule:
    return _parse_rule(raw, PATH, SECTION)


# --- the string shorthand ---------------------------------------------------


def test_an_empty_string_is_still_a_pattern_rule() -> None:
    # isinstance(str) is checked before any truthiness, so "" never reaches
    # the mapping branch's exactly-one-of validation.
    assert _parse("") == _Rule(pattern="")


# --- exactly one of pattern/kind --------------------------------------------


def test_a_falsy_pattern_counts_as_absent() -> None:
    # bool(pattern) == bool(kind), so an empty pattern beside an empty kind
    # is "neither", not "both".
    with pytest.raises(ValueError, match="exactly one of"):
        _parse({"pattern": "", "kind": ""})


def test_an_empty_pattern_beside_a_real_kind_is_a_kind_rule() -> None:
    assert _parse({"pattern": "", "kind": "binding"}) == _Rule(kind="binding")


# --- the three applies-to guards --------------------------------------------


def test_a_falsy_name_head_on_a_kind_rule_is_allowed() -> None:
    # The guard is on the BOOL, so name_head: false is not a violation.
    assert _parse({"kind": "binding", "name_head": False}) == _Rule(kind="binding")


def test_a_falsy_has_child_on_a_pattern_rule_is_allowed() -> None:
    assert _parse({"pattern": "p", "has_child": ""}) == _Rule(pattern="p")


# --- construction -----------------------------------------------------------


def test_non_string_field_values_are_stringified() -> None:
    assert _parse({"kind": 42, "name_child": 7}) == _Rule(kind="42", name_child="7")


def test_unknown_keys_are_ignored() -> None:
    assert _parse({"pattern": "p", "nonsense": "x"}) == _Rule(pattern="p")


# --- the type guard ---------------------------------------------------------


# --- _parse_rules, the caller ------------------------------------------------


def test_no_rules_yields_an_empty_tuple() -> None:
    assert _parse_rules(None, PATH, SECTION) == ()
    assert _parse_rules([], PATH, SECTION) == ()


def test_every_item_is_parsed_in_order() -> None:
    assert _parse_rules(["a", {"kind": "k"}, "b"], PATH, SECTION) == (
        _Rule(pattern="a"),
        _Rule(kind="k"),
        _Rule(pattern="b"),
    )


# --- precedence when a rule breaks more than one guard ----------------------
#
# The three guards are checked in a fixed order (name_head, has_child,
# name_child), so a rule violating several reports a specific one. Pinned
# because the refactor turned three sequential `if`s into one loop, and the
# loop's order is what preserves this.


def test_has_child_is_reported_before_name_child() -> None:
    with pytest.raises(ValueError, match="'has_child' applies to 'kind'"):
        _parse({"pattern": "p", "has_child": "c", "name_child": "n"})


def test_name_head_is_not_reported_when_the_rule_has_a_pattern() -> None:
    # name_head is legal here (pattern rule), so the has_child violation is
    # the one that surfaces even though name_head appears first in the order.
    with pytest.raises(ValueError, match="'has_child' applies to 'kind'"):
        _parse({"pattern": "p", "name_head": True, "has_child": "c"})


def test_name_head_is_reported_first_on_a_kind_rule() -> None:
    with pytest.raises(ValueError, match="'name_head' applies to 'pattern'"):
        _parse({"kind": "k", "name_head": True, "name_child": "n"})
