# A Java anonymous class (`new Base(){ @Override m(){} }`) is not modelled as a
# subclass, so its override methods register under the enclosing class with no
# OVERRIDES edge and look dead even though the base method is called and dispatch
# can land on them (gson's JavaTimeTypeAdapters `create`/`integerValues`). Recording
# the anon override and emitting OVERRIDES to the base, plus override-reachability,
# keeps them live when the base is reachable.
from __future__ import annotations

from pathlib import Path

from evals.dead_code import cgr_dead_code, default_dead_code_config


def _project(tmp_path: Path, files: dict[str, str]) -> Path:
    root = tmp_path / "janon"
    pkg = root / "com" / "example"
    pkg.mkdir(parents=True)
    for name, body in files.items():
        (pkg / name).write_text(f"package com.example;\n{body}\n", encoding="utf-8")
    return root


def test_anonymous_override_of_called_base_is_not_dead(tmp_path: Path) -> None:
    root = _project(
        tmp_path,
        {
            "Base.java": (
                "public abstract class Base {\n"
                "  public abstract int make(int[] v);\n"
                "  public int run(int[] v) { return make(v); }\n"
                "}\n"
            ),
            "Holder.java": (
                "public class Holder {\n"
                "  public static final Base B = new Base() {\n"
                "    @Override public int make(int[] v) { return v[0]; }\n"
                "  };\n"
                "}\n"
            ),
        },
    )
    dead = cgr_dead_code(root, "proj", default_dead_code_config(False, False))
    # run() is public (root) and calls make() -> Base.make live; the anonymous
    # override in Holder overrides Base.make, so it is a live dispatch target.
    anon_override = [d for d in dead if d.endswith(".make(int[])") and ".Holder" in d]
    assert not anon_override, f"anon override reported dead: {sorted(dead)}"


def test_anon_override_edge_source_label_matches_node(tmp_path: Path) -> None:
    # A method-body anonymous override is registered as a Function node; the OVERRIDES
    # edge from it must carry the Function label (not a hard-coded Method), else the
    # graph endpoint match fails and the edge is dropped in the production DB.
    from unittest.mock import MagicMock

    from codebase_rag.graph_updater import GraphUpdater
    from codebase_rag.parser_loader import load_parsers

    root = _project(
        tmp_path,
        {
            "M.java": (
                "public class M {\n"
                "  interface Reader { int read(); }\n"
                "  static Reader make() {\n"
                "    return new Reader() {\n"
                "      @Override public int read() { return 1; }\n"
                "    };\n"
                "  }\n"
                "}\n"
            )
        },
    )
    parsers, queries = load_parsers()
    if "java" not in parsers:
        import pytest

        pytest.skip("java parser not available")
    ing = MagicMock()
    GraphUpdater(ingestor=ing, repo_path=root, parsers=parsers, queries=queries).run()
    # node label per qn
    label_of = {
        c.args[1].get("qualified_name"): c.args[0]
        for c in ing.ensure_node_batch.call_args_list
    }
    for c in ing.ensure_relationship_batch.call_args_list:
        if c.args[1] != "OVERRIDES":
            continue
        src_label, _, src_qn = c.args[0]
        if src_qn in label_of:
            assert src_label == label_of[src_qn], (
                f"OVERRIDES source label {src_label} != node label "
                f"{label_of[src_qn]} for {src_qn}"
            )
