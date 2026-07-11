# (H) First-class function values hiding inside containers and class bodies
# (H) (django's backend data_types dispatch dicts, model field validators
# (H) lists, pickle __reduce__ tuples, conditional arguments): each stored
# (H) name is wired for later dynamic invocation the call graph cannot see,
# (H) so it must be REFERENCED from the scope that stores it.
from __future__ import annotations

from pathlib import Path

from evals.cgr_graph import _capture
from evals.dead_code import cgr_dead_code, default_dead_code_config

CLASS_BODY_PY = """\
def _get_varchar_column(data):
    return data


def _simple_validator(value):
    return value


def char_field(**kwargs):
    return kwargs


class DatabaseWrapper:
    data_types = {
        "CharField": _get_varchar_column,
    }

    domain = char_field(validators=[_simple_validator])
"""

REDUCE_TUPLE_PY = """\
def _load_field(app_label):
    return app_label


class Field:
    def __reduce__(self):
        return _load_field, (self.__class__,)
"""

CONDITIONAL_ARG_PY = """\
def local_setter_noop(obj):
    return obj


def local_setter_real(obj):
    return obj


def build(setter):
    return setter


def compile_it(flag):
    return build(local_setter_real if flag else local_setter_noop)
"""


def _references(root: Path) -> set[tuple[str, str]]:
    ingestor = _capture(root, "proj")
    return {
        (str(f), str(t)) for _fl, f, rel, _tl, t in ingestor.rels if rel == "REFERENCES"
    }


def test_class_attribute_dispatch_dict_references_values(tmp_path: Path) -> None:
    root = tmp_path / "classdict"
    root.mkdir()
    (root / "m.py").write_text(CLASS_BODY_PY, encoding="utf-8")

    refs = _references(root)
    assert ("proj.m", "proj.m._get_varchar_column") in refs


def test_list_literal_argument_references_elements(tmp_path: Path) -> None:
    root = tmp_path / "listarg"
    root.mkdir()
    (root / "m.py").write_text(CLASS_BODY_PY, encoding="utf-8")

    refs = _references(root)
    assert ("proj.m", "proj.m._simple_validator") in refs


def test_returned_tuple_references_function_elements(tmp_path: Path) -> None:
    root = tmp_path / "redtuple"
    root.mkdir()
    (root / "m.py").write_text(REDUCE_TUPLE_PY, encoding="utf-8")

    refs = _references(root)
    assert ("proj.m.Field.__reduce__", "proj.m._load_field") in refs


def test_conditional_argument_references_both_branches(tmp_path: Path) -> None:
    root = tmp_path / "condarg"
    root.mkdir()
    (root / "m.py").write_text(CONDITIONAL_ARG_PY, encoding="utf-8")

    refs = _references(root)
    assert ("proj.m.compile_it", "proj.m.local_setter_noop") in refs
    assert ("proj.m.compile_it", "proj.m.local_setter_real") in refs


def test_first_class_container_values_are_not_dead(tmp_path: Path) -> None:
    root = tmp_path / "alive"
    root.mkdir()
    (root / "m.py").write_text(CLASS_BODY_PY, encoding="utf-8")
    (root / "n.py").write_text(REDUCE_TUPLE_PY, encoding="utf-8")

    dead = cgr_dead_code(root, "proj", default_dead_code_config(False, False))
    assert not any(
        qn.endswith(("_get_varchar_column", "_simple_validator", "_load_field"))
        for qn in dead
    )
