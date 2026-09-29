# Unit tests for the parameter specs and mapping of `change_signature`
# (issue #1533): the `NEW=SOURCE` mapping, the new parameter list, and the
# literal-against-annotation check. No graph and no tree-sitter walking.

from __future__ import annotations

import pytest

from codebase_rag.editing.signature_spec import (
    ParamSpec,
    SignatureRefused,
    SignatureSite,
    UnmappedSite,
    _check_literal,
    _new_specs,
    _parse_new_param,
    _resolve_sources,
    _Source,
    parse_mapping,
    sites_for,
    unmapped_for,
)


def _spec(name: str, annotation: str | None = None, default: bool = False) -> ParamSpec:
    text = name if annotation is None else f"{name}: {annotation}"
    if default:
        text += " = None"
    return ParamSpec(name, text, annotation, default)


OLD = [_spec("a"), _spec("b", "int"), _spec("c", "str", default=True)]


# --- parse_mapping -----------------------------------------------------------------


def test_parse_mapping_splits_on_first_equals() -> None:
    assert parse_mapping(["x=a", "n==1", "k=0"]) == {"x": "a", "n": "=1", "k": "0"}


@pytest.mark.parametrize("entry", ["noequals", "=a"])
def test_parse_mapping_refuses_malformed_entry(entry: str) -> None:
    with pytest.raises(SignatureRefused, match="NEW=SOURCE"):
        parse_mapping([entry])


# --- _resolve_sources ----------------------------------------------------------------


def test_unmentioned_parameter_keeps_its_old_position() -> None:
    new = [_spec("b"), _spec("a"), _spec("z")]
    assert _resolve_sources(new, OLD, {}) == [
        _Source(index=1),
        _Source(index=0),
        None,
    ]


def test_mapping_by_name_index_and_literal() -> None:
    new = [_spec("x"), _spec("y"), _spec("z")]
    sources = _resolve_sources(new, OLD, {"x": "b", "y": "2", "z": "= 'hi' "})
    assert sources == [_Source(index=1), _Source(index=2), _Source(literal="'hi'")]


def test_mapping_naming_an_unknown_new_parameter_is_refused() -> None:
    with pytest.raises(SignatureRefused, match="not a new parameter"):
        _resolve_sources([_spec("a")], OLD, {"q": "a"})


def test_one_old_parameter_cannot_feed_two_new_ones() -> None:
    with pytest.raises(SignatureRefused, match="cannot feed both x and y"):
        _resolve_sources([_spec("x"), _spec("y")], OLD, {"x": "a", "y": "0"})


def test_empty_literal_is_refused() -> None:
    with pytest.raises(SignatureRefused, match="empty literal"):
        _resolve_sources([_spec("x")], OLD, {"x": "=  "})


def test_out_of_range_index_is_refused() -> None:
    with pytest.raises(SignatureRefused, match="only 3 old"):
        _resolve_sources([_spec("x")], OLD, {"x": "3"})


def test_unknown_old_source_is_refused() -> None:
    with pytest.raises(SignatureRefused, match="neither an old parameter"):
        _resolve_sources([_spec("x")], OLD, {"x": "nope"})


# --- _parse_new_param / _new_specs -------------------------------------------------


def test_bare_old_name_carries_the_old_spec_over() -> None:
    by_name = {spec.name: spec for spec in OLD}
    assert _parse_new_param(" c ", by_name) is by_name["c"]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("d", ParamSpec("d", "d", None, False)),
        ("d: int", ParamSpec("d", "d: int", "int", False)),
        ("d: list[str] = []", ParamSpec("d", "d: list[str] = []", "list[str]", True)),
        ("d=3", ParamSpec("d", "d=3", None, True)),
    ],
)
def test_new_parameter_is_parsed(text: str, expected: ParamSpec) -> None:
    assert _parse_new_param(text, {}) == expected


@pytest.mark.parametrize(
    "text",
    ["*args", "**kw", "a, b", "1bad", "a): pass\ndef g(", "/", "*, k"],
)
def test_non_plain_parameter_is_refused(text: str) -> None:
    with pytest.raises(SignatureRefused, match="Not a parameter"):
        _parse_new_param(text, {})


def test_new_specs_keeps_order_and_carries_old_specs() -> None:
    specs = _new_specs(["b", "e: str", "c"], OLD)
    assert [s.name for s in specs] == ["b", "e", "c"]
    assert specs[0] is OLD[1]
    assert specs[2] is OLD[2]


def test_new_specs_refuses_a_duplicate() -> None:
    with pytest.raises(SignatureRefused, match="listed twice"):
        _new_specs(["a", "a"], OLD)


def test_new_specs_refuses_required_after_default() -> None:
    with pytest.raises(SignatureRefused, match="comes after a defaulted"):
        _new_specs(["c", "a"], OLD)


# --- _check_literal ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("annotation", "literal"),
    [
        (None, "'anything'"),
        ("int", "3"),
        ("float", "3"),
        ("complex", "1.5"),
        ("str | None", "None"),
        ("Optional[int]", "None"),
        ("Union[int, str]", "'x'"),
        ("Union[int]", "4"),
        ("list[int]", "[1, 2]"),
        ("dict", "{}"),
        ("MyClass", "3"),  # not a builtin: not checked
        ("int | MyClass", "'x'"),  # an unread member: not checked
        ("Optional[MyClass]", "'x'"),
        ("pkg.Type", "'x'"),
        ("int", "LIMIT"),  # not a literal: nothing to compare
        ("int[", "3"),  # an annotation Python cannot parse
    ],
)
def test_literal_that_fits_or_cannot_be_checked_passes(
    annotation: str | None, literal: str
) -> None:
    _check_literal(_spec("p", annotation), literal)


@pytest.mark.parametrize(
    ("annotation", "literal"),
    [
        ("int", "'x'"),
        ("int", "True"),
        ("int", "1.5"),
        ("str", "None"),
        ("Optional[int]", "'x'"),
        ("Union[int, str]", "1.5"),
        ("int | None", "b'x'"),
        ("list[int]", "(1,)"),
    ],
)
def test_literal_of_the_wrong_type_is_refused(annotation: str, literal: str) -> None:
    with pytest.raises(SignatureRefused, match="does not fit the declared type"):
        _check_literal(_spec("p", annotation), literal)


# --- report helpers ----------------------------------------------------------------


def test_sites_and_unmapped_serialise_to_dicts() -> None:
    site = SignatureSite("call", "m.py", 3, 4, "pkg.m.f", "exact")
    unmapped = UnmappedSite("pkg.m.f", "m.py", None, None, "why")
    assert sites_for([site]) == [
        {
            "kind": "call",
            "path": "m.py",
            "line": 3,
            "col": 4,
            "owner": "pkg.m.f",
            "resolution": "exact",
        }
    ]
    assert unmapped_for([unmapped]) == [
        {"owner": "pkg.m.f", "path": "m.py", "line": None, "col": None, "reason": "why"}
    ]
