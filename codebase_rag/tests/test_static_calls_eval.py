from pathlib import Path

from evals import constants as ec
from evals.static_calls import (
    cgr_static_calls,
    oracle_static_calls,
    score_static_calls,
)


def _make_repo(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "__init__.py").write_text("", encoding="utf-8")
    (root / "mod_a.py").write_text(
        "def helper():\n    return 1\n\n\ndef use():\n    return helper()\n",
        encoding="utf-8",
    )
    (root / "mod_b.py").write_text(
        "from proj.mod_a import helper\n\n\ndef run():\n    return helper()\n",
        encoding="utf-8",
    )


def test_oracle_resolves_direct_first_party_calls(tmp_path: Path) -> None:
    src = tmp_path / "proj"
    _make_repo(src)
    edges = oracle_static_calls(src, "proj")

    # same-module direct call use() -> helper()
    assert ("proj.mod_a.use", "proj.mod_a.helper") in edges
    # import-resolved direct call run() -> helper()
    assert ("proj.mod_b.run", "proj.mod_a.helper") in edges


def test_decorator_application_is_not_a_call_edge(tmp_path: Path) -> None:
    # `@guard('k')` above a function is a decorator application, not a call the
    # decorated function makes; cgr emits no such edge, so the oracle must not.
    src = tmp_path / "proj"
    src.mkdir(parents=True, exist_ok=True)
    (src / "__init__.py").write_text("", encoding="utf-8")
    (src / "deco.py").write_text(
        "def guard(key):\n    def wrap(fn):\n        return fn\n    return wrap\n",
        encoding="utf-8",
    )
    (src / "use.py").write_text(
        "from proj.deco import guard\n\n\n@guard('k')\ndef job():\n    return 1\n",
        encoding="utf-8",
    )
    edges = oracle_static_calls(src, "proj")
    assert ("proj.use.job", "proj.deco.guard") not in edges


def test_oracle_attributes_method_nested_call_to_full_qn(tmp_path: Path) -> None:
    # A call inside a function nested in a method belongs to that nested
    # function's full qn (Class.method.nested); cgr must emit the same caller qn.
    src = tmp_path / "proj"
    src.mkdir(parents=True, exist_ok=True)
    (src / "__init__.py").write_text("", encoding="utf-8")
    (src / "m.py").write_text(
        "def target():\n    return 1\n\n\n"
        "class C:\n"
        "    def method(self):\n"
        "        def nested():\n"
        "            return target()\n"
        "        return nested()\n",
        encoding="utf-8",
    )
    edges = oracle_static_calls(src, "proj")
    assert ("proj.m.C.method.nested", "proj.m.target") in edges


def test_cgr_recall_on_direct_calls(tmp_path: Path) -> None:
    src = tmp_path / "proj"
    _make_repo(src)
    oracle = oracle_static_calls(src, "proj")
    cgr = cgr_static_calls(src, "proj")
    # every statically-resolvable direct call must be present in cgr's graph.
    assert oracle <= cgr


def test_score_static_calls_recall() -> None:
    oracle = {("a", "b"), ("c", "d")}
    cgr = {("a", "b")}  # cgr also has many method-call edges the oracle omits
    result = score_static_calls(cgr, oracle)
    row = next(r for r in result.rows if r["label"] == ec.STATIC_CALLS_LABEL)
    assert (row["tp"], row["fn"]) == (1, 1)
    assert row["recall"] == 0.5
