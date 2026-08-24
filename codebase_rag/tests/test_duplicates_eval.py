# Duplicates eval: the full parse -> graph -> duplicates pipeline runs over
# controlled fixture repos whose clone pairs are known by construction. The
# engine itself is unit-tested in test_duplicates_collect.py, so a fixture
# mismatch here indicts fingerprint ingest or the eval's graph-to-rows
# replay, not the grouping logic.
from pathlib import Path

from codebase_rag import constants as cs
from codebase_rag.duplicates import default_duplicates_config
from evals.duplicates import (
    cgr_duplicates,
    duplicate_pairs,
    score_duplicates,
)

_CONFIG = default_duplicates_config()

_LOOP_BODY_A = (
    "def total_price(items, tax, floor):\n"
    "    result = 0\n"
    "    for item in items:\n"
    "        if item.price > floor:\n"
    "            result += item.price * tax\n"
    "        else:\n"
    "            result -= item.rebate\n"
    "    return result\n"
)

# Same skeleton as _LOOP_BODY_A with every identifier renamed: a Type-2 clone.
_LOOP_BODY_B = (
    "def sum_weights(boxes, scale, cutoff):\n"
    "    acc = 0\n"
    "    for box in boxes:\n"
    "        if box.weight > cutoff:\n"
    "            acc += box.weight * scale\n"
    "        else:\n"
    "            acc -= box.slack\n"
    "    return acc\n"
)

_UNRELATED = (
    "def greet(name, punct):\n    upper = name.upper()\n    return upper + punct\n"
)


def _write_repo(root: Path, files: dict[str, str]) -> None:
    root.mkdir(parents=True)
    (root / "__init__.py").write_text("", encoding="utf-8")
    for filename, source in files.items():
        (root / filename).write_text(source, encoding="utf-8")


def test_cgr_duplicates_finds_renamed_copy(tmp_path: Path) -> None:
    # A renamed copy across files is a Type-2 clone: one exact group holding
    # both definitions, the structurally different function left out.
    src = tmp_path / "proj"
    _write_repo(
        src,
        {
            "billing.py": _LOOP_BODY_A,
            "shipping.py": _LOOP_BODY_B,
            "other.py": _UNRELATED,
        },
    )
    report = cgr_duplicates(src, "proj", _CONFIG)
    assert len(report.groups) == 1
    group = report.groups[0]
    assert group["kind"] == cs.KIND_EXACT
    assert {m["qualified_name"] for m in group["members"]} == {
        "proj.billing.total_price",
        "proj.shipping.sum_weights",
    }


def test_cgr_duplicates_groups_method_with_function_twin(tmp_path: Path) -> None:
    # Only the body is fingerprinted, so a method copying a module function
    # is the same clone (label-agnostic grouping).
    src = tmp_path / "proj"
    counter = (
        "class Counter:\n"
        + "\n".join("    " + line for line in _LOOP_BODY_A.splitlines())
        + "\n"
    )
    _write_repo(src, {"billing.py": _LOOP_BODY_A, "counter.py": counter})
    report = cgr_duplicates(src, "proj", _CONFIG)
    assert len(report.groups) == 1
    assert {m["label"] for m in report.groups[0]["members"]} == {
        cs.NodeLabel.FUNCTION.value,
        cs.NodeLabel.METHOD.value,
    }


def test_cgr_duplicates_ignores_small_functions(tmp_path: Path) -> None:
    # Identical one-liners are below the default min-nodes floor: reporting
    # every trivial getter pair is pure noise.
    src = tmp_path / "proj"
    _write_repo(
        src,
        {
            "a.py": "def get_x(o):\n    return o.x\n",
            "b.py": "def get_y(o):\n    return o.x\n",
        },
    )
    report = cgr_duplicates(src, "proj", _CONFIG)
    assert report.groups == []


def test_cgr_duplicates_finds_edited_copy(tmp_path: Path) -> None:
    # A copy with one statement swapped keeps >= 80% of its statement-level
    # branches: a Type-3 clone reported as a similar group below 1.0. The
    # shared statements must be structurally distinct (identifiers and
    # literals normalize away), or they dedupe into one branch digest.
    shared = [
        "    a = compute(data.f, w[0]) + bias.get(0)",
        "    b = compute(data.f) * w[1] - bias.get(1)",
        "    c = [transform(x) for x in data.rows]",
        "    d = {k: v for k, v in data.items()}",
        "    e = helper(a, b, c) if a else other(d)",
        "    f = data.name.strip().lower().split(',')",
        "    g = (a, b, [c, d], {'k': e})",
        "    h = sum(x * 2 for x in c) + len(d)",
        "    log.info('m', extra={'a': a})",
    ]
    original = "def transform_rows(data, w, bias, log):\n"
    original += "\n".join(shared) + "\n"
    original += "    total = merge_all(a, b, c, d)\n"
    original += "    return total\n"
    edited = "def transform_cols(data, w, bias, log):\n"
    edited += "\n".join(shared) + "\n"
    edited += "    total = [combine(x) for x in c]\n"
    edited += "    return total\n"
    src = tmp_path / "proj"
    _write_repo(src, {"orig.py": original, "edit.py": edited})
    report = cgr_duplicates(src, "proj", _CONFIG)
    similar = [g for g in report.groups if g["kind"] == cs.KIND_SIMILAR]
    assert len(similar) == 1
    assert {m["qualified_name"] for m in similar[0]["members"]} == {
        "proj.orig.transform_rows",
        "proj.edit.transform_cols",
    }
    assert 0.8 <= similar[0]["similarity"] < 1.0


def test_cgr_duplicates_does_not_pair_factory_with_its_closure(tmp_path: Path) -> None:
    # A factory function's body IS its nested closure plus a return: the
    # outer branch set contains the inner's, so overlap scoring sees a
    # near-perfect match. Reporting a function as a duplicate of its own
    # closure is unactionable noise (the create_query_tool shape).
    factory = (
        "def create_tool(db, log):\n"
        "    def run_query(query, limit):\n"
        "        cleaned = query.strip().lower()\n"
        "        rows = db.execute(cleaned, limit=limit)\n"
        "        counted = [decorate(row) for row in rows]\n"
        "        mapping = {row.key: row for row in counted}\n"
        "        picked = mapping.get(cleaned) if mapping else None\n"
        "        log.info('ran', extra={'q': cleaned})\n"
        "        total = sum(row.cost * 2 for row in counted)\n"
        "        return (picked, total, [rows, counted])\n"
        "    return run_query\n"
    )
    src = tmp_path / "proj"
    _write_repo(src, {"factory.py": factory})
    report = cgr_duplicates(src, "proj", _CONFIG)
    assert report.groups == []


def test_cgr_duplicates_honours_exclude_patterns(tmp_path: Path) -> None:
    # A generated twin excluded by glob may not leave a one-member "group".
    src = tmp_path / "proj"
    _write_repo(
        src,
        {"real.py": _LOOP_BODY_A, "gen_client.py": _LOOP_BODY_B},
    )
    config = default_duplicates_config(exclude_patterns=("*gen_*",))
    report = cgr_duplicates(src, "proj", config)
    assert report.groups == []


def test_duplicate_pairs_expands_groups_to_member_pairs() -> None:
    # Pair-level grading is the clone-detection standard: a trio group is
    # three pairs, and pairs are order-normalized.
    groups = [
        {
            "kind": cs.KIND_EXACT,
            "similarity": 1.0,
            "node_count": 20,
            "members": [
                {
                    "label": "Function",
                    "qualified_name": qn,
                    "name": qn.rsplit(".", 1)[-1],
                    "path": "p.py",
                    "start_line": 1,
                    "end_line": 9,
                }
                for qn in ("proj.c.three", "proj.a.one", "proj.b.two")
            ],
        }
    ]
    assert duplicate_pairs(groups) == {
        ("proj.a.one", "proj.b.two"),
        ("proj.a.one", "proj.c.three"),
        ("proj.b.two", "proj.c.three"),
    }


def test_score_duplicates_prf() -> None:
    cgr = {("a", "b"), ("a", "c")}
    oracle = {("a", "b"), ("b", "d")}
    result = score_duplicates(cgr, oracle)
    row = result.rows[0]
    assert (row["tp"], row["fp"], row["fn"]) == (1, 1, 1)
    bucket = next(iter(result.diff.values()))
    assert bucket["missing"] == ["b<->d"]
    assert bucket["extra"] == ["a<->c"]


def test_cgr_duplicates_end_to_end_scores_perfectly(tmp_path: Path) -> None:
    # The graded loop: fixture oracle pairs vs the pipeline's reported pairs
    # must agree exactly (precision = recall = 1.0).
    src = tmp_path / "proj"
    _write_repo(
        src,
        {
            "billing.py": _LOOP_BODY_A,
            "shipping.py": _LOOP_BODY_B,
            "other.py": _UNRELATED,
        },
    )
    report = cgr_duplicates(src, "proj", _CONFIG)
    oracle = {("proj.billing.total_price", "proj.shipping.sum_weights")}
    result = score_duplicates(duplicate_pairs(report.groups), oracle)
    assert result.rows[0]["precision"] == 1.0
    assert result.rows[0]["recall"] == 1.0
